package github

import (
	"strings"
	"time"

	"github.com/jay-withers/repo-agent/internal/jv"
	"github.com/jay-withers/repo-agent/internal/model"
	"github.com/jay-withers/repo-agent/internal/pyfmt"
)

var splitLines = pyfmt.SplitLines

// RepoSnapshot builds a snapshot from one /installation/repositories entry.
func RepoSnapshot(raw map[string]any) model.RepoSnapshot {
	defaultBranch := "main"
	if b, ok := raw["default_branch"].(string); ok {
		defaultBranch = b
	}
	var topics []string
	for _, t := range jv.List(raw["topics"]) {
		if s, ok := t.(string); ok {
			topics = append(topics, s)
		}
	}
	license, hasLicense := raw["license"]
	return model.RepoSnapshot{
		Name:          jv.Str(raw, "name"),
		FullName:      jv.Str(raw, "full_name"),
		Description:   jv.StrPtr(raw, "description"),
		DefaultBranch: defaultBranch,
		Archived:      jv.Bool(raw, "archived"),
		PushedAt:      Timestamp(raw["pushed_at"]),
		CreatedAt:     Timestamp(raw["created_at"]),
		Topics:        topics,
		HasLicense:    hasLicense && license != nil,
		URL:           jv.Str(raw, "html_url"),
		Private:       jv.Bool(raw, "private"),
	}
}

// Timestamp parses one of GitHub's ISO-8601 timestamps, or nil: a malformed
// timestamp costs one finding its age, not the scan.
func Timestamp(raw any) *time.Time {
	s, ok := raw.(string)
	if !ok {
		return nil
	}
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02T15:04:05", "2006-01-02"} {
		if t, err := time.Parse(layout, s); err == nil {
			// A naive timestamp is UTC, as the checks' _age_days assumed.
			return model.Ptr(t.UTC())
		}
	}
	return nil
}

// PullRequest builds a pull request from one GraphQL pullRequests node. A
// deleted account's author is null and reads "unknown".
func PullRequest(raw map[string]any) model.PullRequest {
	author := jv.Str(jv.Obj(raw["author"]), "login")
	if author == "" {
		author = "unknown"
	}
	return model.PullRequest{
		Number:     jv.IntOr(raw, "number", 0),
		Title:      jv.Str(raw, "title"),
		Author:     author,
		CreatedAt:  Timestamp(raw["createdAt"]),
		URL:        jv.Str(raw, "url"),
		Draft:      jv.Bool(raw, "isDraft"),
		UpdatedAt:  Timestamp(raw["updatedAt"]),
		MergeState: jv.Str(raw, "mergeStateStatus"),
	}
}

// ParseAlerts builds alerts from one page, keeping nil as nil.
func ParseAlerts(raw []any) *[]model.CodeScanningAlert {
	if raw == nil {
		return nil
	}
	alerts := []model.CodeScanningAlert{}
	for _, item := range raw {
		entry := jv.Obj(item)
		if entry == nil {
			continue
		}
		rule := jv.Obj(entry["rule"])
		name := jv.Str(rule, "id")
		if name == "" {
			name = jv.Str(rule, "name")
		}
		if name == "" {
			name = "unknown"
		}
		// Security severity first: it is what a person triages by.
		severity := jv.Str(rule, "security_severity_level")
		if severity == "" {
			severity = jv.Str(rule, "severity")
		}
		alerts = append(alerts, model.CodeScanningAlert{
			Number:    jv.IntOr(entry, "number", 0),
			Rule:      name,
			Severity:  strings.ToLower(severity),
			CreatedAt: Timestamp(entry["created_at"]),
			URL:       jv.Str(entry, "html_url"),
			Tool:      jv.Str(jv.Obj(entry["tool"]), "name"),
		})
	}
	return &alerts
}

// ParseIssues builds issues from one /issues page, keeping nil as nil and
// dropping the pull requests the endpoint also lists.
func ParseIssues(raw []any) *[]model.Issue {
	if raw == nil {
		return nil
	}
	issues := []model.Issue{}
	for _, item := range raw {
		entry := jv.Obj(item)
		if entry == nil {
			continue
		}
		if _, isPR := entry["pull_request"]; isPR {
			continue
		}
		var labels []string
		for _, l := range jv.List(entry["labels"]) {
			if name := jv.Str(jv.Obj(l), "name"); name != "" {
				labels = append(labels, name)
			}
		}
		issues = append(issues, model.Issue{
			Number:    jv.IntOr(entry, "number", 0),
			Title:     jv.Str(entry, "title"),
			Labels:    labels,
			CreatedAt: Timestamp(entry["created_at"]),
			URL:       jv.Str(entry, "html_url"),
		})
	}
	return &issues
}

// MergeDetail folds one repository's GraphQL detail into its REST
// snapshot, returning it unchanged when there is no detail.
func MergeDetail(s model.RepoSnapshot, detail map[string]any) model.RepoSnapshot {
	if len(detail) == 0 {
		return s
	}
	s.RenovateConfigPath, s.RenovateConfig = renovateConfig(detail)
	s.Readme = blobText(detail["readme"], true)
	s.Dockerfile = blobText(detail["dockerfile"], true)
	s.HasReadme = s.Readme != nil

	s.Workflows = nil
	for _, e := range jv.List(jv.Obj(detail["workflows"])["entries"]) {
		entry := jv.Obj(e)
		name := jv.Str(entry, "name")
		// A Tree lists directories too, and non-workflow files Actions ignores.
		if !strings.HasSuffix(name, ".yml") && !strings.HasSuffix(name, ".yaml") {
			continue
		}
		text := ""
		if t := blobText(entry["object"], false); t != nil {
			text = *t
		}
		s.Workflows = append(s.Workflows, model.Workflow{Name: name, Text: text})
	}

	// terraform/ first, this estate's layout; the root for a module repo.
	s.TerraformLock = blobText(detail["tflock"], false)
	if s.TerraformLock == nil || *s.TerraformLock == "" {
		s.TerraformLock = blobText(detail["tflock_root"], false)
	}

	prPage := jv.Obj(detail["pullRequests"])
	s.OpenPRs = nil
	for _, n := range jv.List(prPage["nodes"]) {
		s.OpenPRs = append(s.OpenPRs, PullRequest(jv.Obj(n)))
	}
	s.OpenPRTotal = jv.IntPtr(prPage, "totalCount")
	s.Branches, s.BranchTotal = branches(detail["branches"])
	s.RenovatePREver = renovatePREver(detail, s.OpenPRs)

	release := map[string]any{}
	if releases := jv.List(jv.Obj(detail["releases"])["nodes"]); len(releases) > 0 {
		release = jv.Obj(releases[0])
	}
	s.LastRelease = jv.StrPtr(release, "tagName")
	s.LastReleaseAt = Timestamp(release["publishedAt"])
	return s
}

// branches is one page of branches and the true count, or (nil, nil) when
// absent: a repository always has its default branch, so a missing field
// means the query did not answer.
func branches(raw any) (*[]model.Branch, *int) {
	page, ok := raw.(map[string]any)
	if !ok {
		return nil, nil
	}
	out := []model.Branch{}
	for _, n := range jv.List(page["nodes"]) {
		node := jv.Obj(n)
		open := jv.IntPtr(jv.Obj(node["associatedPullRequests"]), "totalCount")
		out = append(out, model.Branch{
			Name:        jv.Str(node, "name"),
			CommittedAt: Timestamp(jv.Obj(node["target"])["committedDate"]),
			HasOpenPR:   open != nil && *open > 0,
		})
	}
	return &out, jv.IntPtr(page, "totalCount")
}

// renovatePREver is whether Renovate has ever opened a PR here, or nil
// where the one-page history cannot answer it.
func renovatePREver(detail map[string]any, open []model.PullRequest) *bool {
	for _, pr := range open {
		if pr.IsRenovate() {
			return model.Ptr(true)
		}
	}
	closed := jv.Obj(detail["closedPullRequests"])
	nodes := jv.List(closed["nodes"])
	for _, n := range nodes {
		if model.IsRenovateLogin(jv.Str(jv.Obj(jv.Obj(n)["author"]), "login")) {
			return model.Ptr(true)
		}
	}
	total, ok := jv.Int(closed["totalCount"])
	if !ok || total > len(nodes) {
		return nil
	}
	return model.Ptr(false)
}

// renovateConfig is the first Renovate config present, as (path, text).
func renovateConfig(detail map[string]any) (path, text *string) {
	for i, p := range RenovateConfigPaths {
		if t := blobText(detail["renovate"+itoa(i)], true); t != nil {
			return model.Ptr(p), t
		}
	}
	return nil, nil
}

func itoa(i int) string { return string(rune('0' + i)) }

// blobText is a GraphQL Blob's text, or nil where the path does not exist
// (or is binary, which reads the same). capped truncates at MaxFileChars;
// lock files and workflows are read whole, since only the checks see them
// and a truncated one reads as broken.
func blobText(node any, capped bool) *string {
	text, ok := jv.Obj(node)["text"].(string)
	if !ok {
		return nil
	}
	if !capped {
		return &text
	}
	if cut, truncated := pyfmt.Truncate(text, MaxFileChars); truncated {
		return model.Ptr(cut + "\n… [truncated]")
	}
	return &text
}
