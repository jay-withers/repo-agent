package github

import (
	"strings"
	"testing"
)

func TestRepoSnapshotFromREST(t *testing.T) {
	s := RepoSnapshot(map[string]any{
		"name": "a", "full_name": "o/a", "description": nil, "archived": true,
		"pushed_at": "2026-01-01T00:00:00Z", "created_at": "not a date", "topics": []any{"x", 1},
		"license": nil, "html_url": "u", "private": true,
	})
	if s.Description != nil || !s.Archived || s.PushedAt == nil || s.CreatedAt != nil || len(s.Topics) != 1 ||
		s.HasLicense || !s.Private || s.DefaultBranch != "main" {
		t.Errorf("got %+v", s)
	}
	if !RepoSnapshot(map[string]any{"license": map[string]any{}}).HasLicense {
		t.Error("a licence object is a licence")
	}
}

func TestTimestampForms(t *testing.T) {
	for _, s := range []string{"2026-01-02T03:04:05Z", "2026-01-02T03:04:05.123+01:00", "2026-01-02T03:04:05", "2026-01-02"} {
		if Timestamp(s) == nil {
			t.Errorf("%s did not parse", s)
		}
	}
	if Timestamp(5) != nil || Timestamp("x") != nil {
		t.Error("junk is nil")
	}
}

func TestPullRequestWithADeletedAuthor(t *testing.T) {
	pr := PullRequest(map[string]any{"number": 3, "author": nil, "isDraft": true, "mergeStateStatus": nil})
	if pr.Author != "unknown" || !pr.Draft || pr.MergeState != "" || pr.Number != 3 {
		t.Errorf("got %+v", pr)
	}
}

func TestParseAlertsAndIssuesKeepNilAsNil(t *testing.T) {
	if ParseAlerts(nil) != nil || ParseIssues(nil) != nil {
		t.Error("nil is could-not-tell")
	}
	alerts := ParseAlerts([]any{"junk", map[string]any{"rule": map[string]any{"name": "n", "severity": "Warning"}}, map[string]any{}})
	if len(*alerts) != 2 || (*alerts)[0].Rule != "n" || (*alerts)[0].Severity != "warning" || (*alerts)[1].Rule != "unknown" {
		t.Errorf("got %+v", *alerts)
	}
	issues := ParseIssues([]any{"junk", map[string]any{"pull_request": map[string]any{}}, map[string]any{
		"number": 1, "labels": []any{map[string]any{"name": "x"}, map[string]any{"name": ""}, "y"},
	}})
	if len(*issues) != 1 || len((*issues)[0].Labels) != 1 {
		t.Errorf("got %+v", *issues)
	}
}

func TestMergeDetailWithNothingLeavesTheSnapshot(t *testing.T) {
	s := RepoSnapshot(map[string]any{"name": "a"})
	if got := MergeDetail(s, nil); got.Branches != nil || got.RenovatePREver != nil {
		t.Errorf("got %+v", got)
	}
}

func TestMergeDetail(t *testing.T) {
	long := strings.Repeat("é", MaxFileChars+5)
	detail := map[string]any{
		"renovate1":   map[string]any{"text": "{}"},
		"renovate3":   map[string]any{"text": "second"},
		"readme":      map[string]any{"text": long},
		"tflock":      map[string]any{"text": ""},
		"tflock_root": map[string]any{"text": long},
		"workflows": map[string]any{"entries": []any{
			map[string]any{"name": "a.yml", "object": map[string]any{"text": long}},
			map[string]any{"name": "b.yaml", "object": nil},
			map[string]any{"name": "dir"},
		}},
		"pullRequests":       map[string]any{"totalCount": 31, "nodes": []any{map[string]any{"author": map[string]any{"login": "someone"}}}},
		"closedPullRequests": map[string]any{"totalCount": 31, "nodes": []any{map[string]any{"author": map[string]any{"login": "someone"}}}},
		"branches":           map[string]any{"nodes": []any{map[string]any{"name": "b", "associatedPullRequests": map[string]any{"totalCount": 2}}}},
		"releases":           map[string]any{"nodes": []any{map[string]any{"tagName": "v1", "publishedAt": "2026-01-01T00:00:00Z"}}},
	}
	s := MergeDetail(RepoSnapshot(map[string]any{"name": "a"}), detail)
	if *s.RenovateConfigPath != "renovate.json5" || *s.RenovateConfig != "{}" {
		t.Errorf("first config in Renovate's order: %v", *s.RenovateConfigPath)
	}
	if !s.HasReadme || !strings.HasSuffix(*s.Readme, "\n… [truncated]") || len([]rune(*s.Readme)) != MaxFileChars+len([]rune("\n… [truncated]")) {
		t.Error("the README is capped by characters")
	}
	if len(s.Workflows) != 2 || s.Workflows[0].Text != long || s.Workflows[1].Text != "" {
		t.Error("workflows are read whole, non-workflow entries dropped")
	}
	if *s.TerraformLock != long {
		t.Error("an empty terraform/ lock falls back to the root, uncapped")
	}
	if *s.OpenPRTotal != 31 || s.RenovatePREver != nil {
		t.Error("a full history page with no Renovate PR cannot answer")
	}
	if len(*s.Branches) != 1 || !(*s.Branches)[0].HasOpenPR || s.BranchTotal != nil {
		t.Errorf("branches %+v", *s.Branches)
	}
	if *s.LastRelease != "v1" || s.LastReleaseAt == nil {
		t.Error("release")
	}
}

func TestRenovatePREver(t *testing.T) {
	closed := func(total int, logins ...string) map[string]any {
		var nodes []any
		for _, l := range logins {
			nodes = append(nodes, map[string]any{"author": map[string]any{"login": l}})
		}
		return map[string]any{"closedPullRequests": map[string]any{"totalCount": total, "nodes": nodes}}
	}
	if v := renovatePREver(closed(2, "someone", "renovate[bot]"), nil); v == nil || !*v {
		t.Error("a closed Renovate PR is proof")
	}
	if v := renovatePREver(closed(1, "someone"), nil); v == nil || *v {
		t.Error("a complete page with none is False")
	}
	if v := renovatePREver(map[string]any{}, nil); v != nil {
		t.Error("no history is unknown")
	}
}
