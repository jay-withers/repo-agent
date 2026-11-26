// Package scan is the job: authenticate, read every repository, check it,
// reconcile against last week, triage, report.
//
// **Reconcile runs before triage**, so a suppressed finding is never sent
// to DeepSeek. **State is saved last, after the email, and only if one was
// sent or attempted**: a run that fails to send must not record its
// findings as seen, and `render` never saves.
package scan

import (
	"fmt"
	"net/http"
	"os"
	"sort"
	"strings"

	"github.com/jay-withers/repo-agent/internal/checks"
	"github.com/jay-withers/repo-agent/internal/digest"
	"github.com/jay-withers/repo-agent/internal/github"
	"github.com/jay-withers/repo-agent/internal/llm"
	"github.com/jay-withers/repo-agent/internal/logging"
	"github.com/jay-withers/repo-agent/internal/mailer"
	"github.com/jay-withers/repo-agent/internal/model"
	"github.com/jay-withers/repo-agent/internal/settings"
	"github.com/jay-withers/repo-agent/internal/state"
)

var log = logging.Named("repoagent.jobs.scan")

var severityRank = map[string]int{"high": 0, "medium": 1, "low": 2}

func rank(s string) int {
	if r, ok := severityRank[s]; ok {
		return r
	}
	return 9
}

// Run scans every repository the App is installed on. sendEmail=false is
// `render`: everything but the send and the save.
func Run(client *http.Client, sendEmail bool) (model.ScanResult, error) {
	repos, ignored, err := snapshots(client)
	if err != nil {
		return model.ScanResult{}, err
	}
	found := findings(repos)

	prior := state.Load()
	reconciled := state.Reconcile(prior, found, model.Now())
	log.Info(fmt.Sprintf("%d finding(s): %d new, %d resolved, %d suppressed",
		len(reconciled.Findings), len(reconciled.New()), len(reconciled.Resolved), len(reconciled.Suppressed)))

	triaged, err := llm.Triage(client, reconciled.Findings, repos)
	if err != nil {
		return model.ScanResult{}, err
	}

	tag := "unknown"
	if v, ok := os.LookupEnv("IMAGE_TAG"); ok {
		tag = v
	}
	resolved := make([]model.Resolved, len(reconciled.Resolved))
	for i, k := range reconciled.Resolved {
		resolved[i] = model.Resolved{Repo: k.Repo, Title: k.Title}
	}
	result := model.ScanResult{
		Repos:           repos,
		Findings:        triaged.Findings,
		ImageTag:        tag,
		Summary:         triaged.Summary,
		Themes:          triaged.Themes,
		Usage:           triaged.Usage,
		Suggestions:     triaged.Suggestions,
		Ignored:         ignored,
		Resolved:        resolved,
		SuppressedCount: len(reconciled.Suppressed),
	}
	if !sendEmail {
		return result, nil
	}

	outcome, err := mailer.Send(client, digest.Subject(result), digest.HTML(result), digest.Text(result))
	if err != nil {
		return result, err
	}
	log.Info(fmt.Sprintf("scanned %d repositories, %d finding(s), email %s",
		len(repos), len(triaged.Findings), outcome.Status))
	if outcome.Status != "skipped" {
		state.Save(reconciled.State)
	}
	return result, nil
}

// snapshots is every repository worth checking, plus the ones skipped by
// topic. Alerts, issues and the detail query each fail independently: a
// failure costs that input, never the digest. Only the REST listing is fatal.
func snapshots(client *http.Client) ([]model.RepoSnapshot, []model.Ignored, error) {
	raw, err := github.InstallationRepos(client)
	if err != nil {
		return nil, nil, err
	}
	exempt := settings.Get().IgnoredTopics()
	var repos []model.RepoSnapshot
	var ignored []model.Ignored
	for _, r := range raw {
		repo := github.RepoSnapshot(r)
		matched := ""
		for _, t := range repo.Topics {
			if exempt[strings.ToLower(t)] {
				matched = t
				break
			}
		}
		if matched != "" {
			ignored = append(ignored, model.Ignored{FullName: repo.FullName, Topic: matched})
		} else {
			repos = append(repos, repo)
		}
	}
	if len(ignored) > 0 {
		noun := "ies"
		if len(ignored) == 1 {
			noun = "y"
		}
		parts := make([]string, len(ignored))
		for i, x := range ignored {
			parts[i] = fmt.Sprintf("%s (%s)", x.FullName, x.Topic)
		}
		log.Info(fmt.Sprintf("ignoring %d repositor%s by topic: %s", len(ignored), noun, strings.Join(parts, ", ")))
	}

	var wanted []string
	for _, r := range repos {
		if !r.Archived {
			wanted = append(wanted, r.FullName)
		}
	}

	alerts, err := github.CodeScanningAlerts(client, wanted)
	if err != nil {
		log.Warn(fmt.Sprintf("code scanning alerts unavailable, continuing without them: %s", err))
		alerts = map[string][]any{}
	}
	issues, err := github.OpenIssues(client, wanted)
	if err != nil {
		log.Warn(fmt.Sprintf("issues unavailable, continuing without them: %s", err))
		issues = map[string][]any{}
	}
	for i := range repos {
		repos[i].CodeScanningAlerts = github.ParseAlerts(alerts[repos[i].FullName])
		repos[i].OpenIssues = github.ParseIssues(issues[repos[i].FullName])
	}

	details, err := github.RepoDetails(client, wanted)
	if err != nil {
		log.Warn(fmt.Sprintf("detail query failed, continuing without it: %s", err))
		return repos, ignored, nil
	}
	declared := github.Catalogue(client)
	for i, r := range repos {
		merged := github.MergeDetail(r, details[r.FullName])
		if declared != nil {
			entry, ok := declared[r.Name]
			merged.InCatalogue = model.Ptr(ok)
			if ok {
				required := make([]string, 0, len(entry.RequiredChecks))
				for c := range entry.RequiredChecks {
					required = append(required, c)
				}
				sort.Strings(required)
				merged.RequiredChecks = &required
				merged.CodeScanningEnforced = model.Ptr(entry.CodeScanning)
			}
		}
		repos[i] = merged
	}
	return repos, ignored, nil
}

// findings runs every check on every repository, ordered severity, repo,
// check (a stable sort, as the Python's was).
func findings(repos []model.RepoSnapshot) []model.Finding {
	var out []model.Finding
	for _, r := range repos {
		out = append(out, checks.RunAll(r)...)
	}
	sort.SliceStable(out, func(i, j int) bool {
		a, b := out[i], out[j]
		if rank(a.Severity) != rank(b.Severity) {
			return rank(a.Severity) < rank(b.Severity)
		}
		if a.Repo != b.Repo {
			return a.Repo < b.Repo
		}
		return a.Check < b.Check
	})
	return out
}
