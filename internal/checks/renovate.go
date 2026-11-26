package checks

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"github.com/jay-withers/repo-agent/internal/model"
	"github.com/jay-withers/repo-agent/internal/pyfmt"
)

// Renovate thresholds. BacklogMinAgeDays and BlockedPRDays are coupled to
// the Sunday 18:00 UTC scan: move the cron and they move with it (see
// docs/checks.md, "The scan runs on Sunday").
const (
	StalePRDays          = 21
	BlockedPRDays        = 5
	PRBacklogCount       = 5
	BacklogMinAgeDays    = 2
	FirstUpdateGraceDays = 8
)

const renovatePRsURL = "/pulls?q=is%3Apr+is%3Aopen+author%3Aapp%2Frenovate"

// MissingConfig: no Renovate config at any path Renovate reads.
func MissingConfig(s model.RepoSnapshot) []model.Finding {
	if s.RenovateConfig != nil {
		return nil
	}
	return finding(s, "renovate.missing_config", "high", "no Renovate configuration",
		"No renovate.json, renovate.json5 or .renovaterc at any path Renovate "+
			"reads, so dependencies here are never updated automatically.", s.URL, nil)
}

func isOnboarding(pr model.PullRequest) bool {
	return strings.Contains(strings.ToLower(pr.Title), "configure renovate")
}

// OnboardingUnmerged: Renovate's onboarding PR is still open, so it opens
// no update PRs at all.
func OnboardingUnmerged(s model.RepoSnapshot) []model.Finding {
	for _, pr := range s.RenovatePRs() {
		if isOnboarding(pr) {
			return finding(s, "renovate.onboarding_unmerged", "high", "Renovate onboarding PR never merged",
				fmt.Sprintf("PR #%d is Renovate's onboarding pull request. Until it "+
					"is merged Renovate opens no update PRs, so this repository looks "+
					"configured but receives nothing.", pr.Number),
				pr.URL, model.AgeDays(pr.CreatedAt))
		}
	}
	return nil
}

func blocked(pr model.PullRequest) bool {
	return pr.MergeState == "BLOCKED" || pr.MergeState == "DIRTY"
}

func whyBlocked(pr model.PullRequest) string {
	if pr.MergeState == "DIRTY" {
		return "conflicts"
	}
	return "checks failing"
}

// StalledPRs: Renovate PRs nobody merges, as one finding.
func StalledPRs(s model.RepoSnapshot) []model.Finding {
	var prs []model.PullRequest
	for _, pr := range s.RenovatePRs() {
		if !pr.Draft && !isOnboarding(pr) && !blocked(pr) {
			prs = append(prs, pr)
		}
	}
	if len(prs) == 0 {
		return nil
	}
	var oldest *int
	for _, pr := range prs {
		if age := model.AgeDays(pr.CreatedAt); age != nil && (oldest == nil || *age > *oldest) {
			oldest = age
		}
	}
	stale := oldest != nil && *oldest >= StalePRDays
	backlog := len(prs) >= PRBacklogCount && oldest != nil && *oldest >= BacklogMinAgeDays
	if !stale && !backlog {
		return nil
	}
	var reason []string
	if backlog {
		reason = append(reason, fmt.Sprintf("%d open Renovate PRs", len(prs)))
	}
	reason = append(reason, fmt.Sprintf("the oldest has been open %d days", *oldest))
	severity := "medium"
	if stale && backlog {
		severity = "high"
	}
	return finding(s, "renovate.stalled_prs", severity, "Renovate updates are not being merged",
		strings.Join(reason, " and ")+". Renovate stops opening new PRs once it hits "+
			"prConcurrentLimit, so a backlog eventually becomes silence.",
		s.URL+renovatePRsURL, oldest)
}

// BlockedPRs: Renovate PRs BLOCKED or DIRTY since the Monday batch, named
// one by one, because the fix is in each PR.
func BlockedPRs(s model.RepoSnapshot) []model.Finding {
	type aged struct {
		age int
		pr  model.PullRequest
	}
	var found []aged
	for _, pr := range s.RenovatePRs() {
		age := model.AgeDays(pr.CreatedAt)
		if !pr.Draft && blocked(pr) && age != nil && *age >= BlockedPRDays {
			found = append(found, aged{*age, pr})
		}
	}
	if len(found) == 0 {
		return nil
	}
	sort.SliceStable(found, func(i, j int) bool { return found[i].age > found[j].age })
	var named []string
	for _, f := range found[:min(maxNamed, len(found))] {
		named = append(named, fmt.Sprintf("#%d %s (%s, open %dd)", f.pr.Number, pyfmt.Repr(f.pr.Title), whyBlocked(f.pr), f.age))
	}
	return finding(s, "renovate.blocked_prs", "high", fmt.Sprintf("%d Renovate PR(s) cannot merge", len(found)),
		strings.Join(named, ", ")+". A required check is failing or the branch conflicts, so auto-merge "+
			"will never take it and every update it carries stays unapplied. Open it and fix "+
			"the failing check.",
		s.URL+renovatePRsURL, model.Ptr(found[0].age))
}

// NeverOpenedAPR: configured, past its first weekly windows, and Renovate
// has never opened a single PR. Silent when the history cannot answer.
func NeverOpenedAPR(s model.RepoSnapshot) []model.Finding {
	if s.RenovateConfig == nil || s.RenovatePREver == nil || *s.RenovatePREver {
		return nil
	}
	age := model.AgeDays(s.CreatedAt)
	if age == nil || *age < FirstUpdateGraceDays {
		return nil
	}
	return finding(s, "renovate.never_opened_a_pr", "high", "Renovate has never opened a pull request here",
		fmt.Sprintf("%s is present and Renovate has run, but in "+
			"%d days it has never opened a single update PR. Check the Dependency "+
			"Dashboard issue: updates sitting under \"Awaiting Schedule\" mean no "+
			"Renovate job has landed inside the preset's weekly window. "+
			"\"Create all awaiting schedule PRs at once\" clears the backlog; "+
			"triggering a run on its own does not, because the run re-evaluates the "+
			"schedule and parks them again.", pyStr(s.RenovateConfigPath), *age),
		s.URL, age)
}

// DefaultConfigOnly: a Renovate config that is the generated default and
// nothing else. Extending a shared preset counts as a decision.
func DefaultConfigOnly(s model.RepoSnapshot) []model.Finding {
	if s.RenovateConfig == nil {
		return nil
	}
	keys, extends := configShape(*s.RenovateConfig)
	for k := range keys {
		if k != "$schema" && k != "extends" {
			return nil
		}
	}
	for _, preset := range extends {
		if !strings.HasPrefix(preset, "config:") {
			return nil
		}
	}
	return finding(s, "renovate.default_config_only", "low", "Renovate config is the untouched default",
		pyStr(s.RenovateConfigPath)+" extends only Renovate's stock presets "+
			"and sets nothing else — no shared preset, no packageRules, no schedule "+
			"and no grouping, so every update arrives as its own unscheduled PR.",
		fmt.Sprintf("%s/blob/%s/%s", s.URL, s.DefaultBranch, pyStr(s.RenovateConfigPath)), nil)
}

// knownKeys are only for the JSON5 fallback: the keys that mean a decision.
var knownKeys = []string{
	"extends", "packageRules", "schedule", "automerge", "autoApprove", "groupName",
	"labels", "customManagers", "prConcurrentLimit", "dependencyDashboard", "ignoreDeps",
	"rangeStrategy",
}

// configShape is the config's top-level keys and its extends list, parsed
// as JSON where it can be, falling back to a substring scan for JSON5,
// which errs towards finding a key.
func configShape(config string) (map[string]bool, []string) {
	var parsed any
	if err := json.Unmarshal([]byte(config), &parsed); err != nil {
		keys := map[string]bool{}
		for _, k := range knownKeys {
			if strings.Contains(config, `"`+k+`"`) || strings.Contains(config, k+":") {
				keys[k] = true
			}
		}
		if keys["extends"] {
			return keys, []string{"unparsed"}
		}
		return keys, nil
	}
	obj, ok := parsed.(map[string]any)
	if !ok {
		return map[string]bool{}, nil
	}
	keys := map[string]bool{}
	for k := range obj {
		keys[k] = true
	}
	var extends []string
	switch e := obj["extends"].(type) {
	case string:
		extends = []string{e}
	case []any:
		for _, item := range e {
			if s, ok := item.(string); ok {
				extends = append(extends, s)
			}
		}
	}
	return keys, extends
}
