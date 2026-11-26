package checks

import (
	"fmt"
	"regexp"
	"slices"
	"sort"
	"strings"

	"github.com/jay-withers/repo-agent/internal/model"
	"github.com/jay-withers/repo-agent/internal/pyfmt"
)

// SharedPresetMarker is the estate's shared Renovate preset repository.
const SharedPresetMarker = "jay-withers/renovate"

const catalogueURL = "https://github.com/jay-withers/github-repos"

// Unmanaged: on GitHub, absent from the catalogue. Silent when the
// catalogue could not be read.
func Unmanaged(s model.RepoSnapshot) []model.Finding {
	if s.InCatalogue == nil || *s.InCatalogue {
		return nil
	}
	return finding(s, "estate.unmanaged", "high", "not in the github-repos catalogue",
		"No entry in github-repos' terraform.tfvars, so nothing applies branch "+
			"protection, required status checks, description or topics to it. It is "+
			"outside the estate's controls rather than configured differently.", catalogueURL, nil)
}

// NotSharedPreset: a Renovate config that does not extend the shared preset.
func NotSharedPreset(s model.RepoSnapshot) []model.Finding {
	if s.RenovateConfig == nil || strings.Contains(*s.RenovateConfig, SharedPresetMarker) {
		return nil
	}
	path := pyStr(s.RenovateConfigPath)
	return finding(s, "estate.not_shared_preset", "low", "Renovate config does not extend the shared preset",
		path+" does not extend github>"+SharedPresetMarker+", so changes to the estate's central "+
			"Renovate policy never reach this repository.",
		fmt.Sprintf("%s/blob/%s/%s", s.URL, s.DefaultBranch, path), nil)
}

// producibleContexts is what one workflow can report: direct contexts, and
// the ids of jobs calling a reusable workflow (whose far half lives in
// another repository).
func producibleContexts(text string) (direct, callers map[string]bool) {
	direct, callers = map[string]bool{}, map[string]bool{}
	for _, j := range Jobs(text) {
		if j.Calls() {
			callers[j.ID] = true
		} else {
			direct[j.Context()] = true
		}
	}
	return direct, callers
}

var pathFilterRe = regexp.MustCompile(`^    paths(-ignore)?:`)

// gatesEveryPullRequest: triggered by pull_request with no path filter.
// A required check that does not always run never reports.
func gatesEveryPullRequest(text string) bool {
	lines := pyfmt.SplitLines(text)
	for i, line := range lines {
		m := onRe.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		if inline := beforeHash(m[1]); inline != "" {
			return slices.Contains(listSplitRe.Split(inline, -1), "pull_request")
		}
		triggered, inPR := false, false
		for _, rest := range lines[i+1:] {
			if strings.TrimSpace(rest) == "" {
				continue
			}
			if !strings.HasPrefix(rest, "  ") {
				break
			}
			if e := eventRe.FindStringSubmatch(rest); e != nil {
				inPR = e[1] == "pull_request"
				triggered = triggered || inPR
			} else if inPR && pathFilterRe.MatchString(rest) {
				return false
			}
		}
		return triggered
	}
	return false
}

// UnreportableRequiredCheck: a required context no workflow can report,
// which leaves every pull request pending for ever.
func UnreportableRequiredCheck(s model.RepoSnapshot) []model.Finding {
	if s.RequiredChecks == nil || len(*s.RequiredChecks) == 0 || len(s.Workflows) == 0 {
		return nil
	}
	direct, callers := map[string]bool{}, map[string]bool{}
	for _, w := range s.Workflows {
		d, c := producibleContexts(w.Text)
		for k := range d {
			direct[k] = true
		}
		for k := range c {
			callers[k] = true
		}
	}
	var missing []string
	for _, ctx := range *s.RequiredChecks {
		if direct[ctx] {
			continue
		}
		if caller, _, found := strings.Cut(ctx, " / "); found && callers[caller] {
			continue
		}
		missing = append(missing, ctx)
	}
	if len(missing) == 0 {
		return nil
	}
	listed := make([]string, len(missing))
	for i, c := range missing {
		listed[i] = "`" + c + "`"
	}
	return finding(s, "estate.unreportable_required_check", "high",
		fmt.Sprintf("%d required status check(s) nothing reports", len(missing)),
		strings.Join(listed, ", ")+" required by the github-repos catalogue, and no workflow in "+
			"this repository produces that context. A required check that never "+
			"reports leaves every pull request pending rather than failing it, so "+
			"nothing merges except by bypassing the ruleset.", workflowsURL(s), nil)
}

var codeqlRe = regexp.MustCompile(`/codeql\.ya?ml(@|$)`)

func isRequired(j Job, required []string) bool {
	if j.Calls() {
		prefix := j.ID + " / "
		for _, c := range required {
			if c == j.ID || strings.HasPrefix(c, prefix) {
				return true
			}
		}
		return false
	}
	return slices.Contains(required, j.Context())
}

// coveredByRequired is job ids a required job waits on, directly or
// through another job: the always-reporting gates are required instead of
// the path-filtered jobs behind them.
func coveredByRequired(found []Job, required []string) map[string]bool {
	byID := map[string]Job{}
	for _, j := range found {
		byID[j.ID] = j
	}
	var pending []string
	for _, j := range found {
		if isRequired(j, required) {
			pending = append(pending, j.Needs...)
		}
	}
	covered := map[string]bool{}
	for len(pending) > 0 {
		id := pending[len(pending)-1]
		pending = pending[:len(pending)-1]
		job, ok := byID[id]
		if covered[id] || !ok {
			continue
		}
		covered[id] = true
		pending = append(pending, job.Needs...)
	}
	return covered
}

// UnenforcedCheck: a workflow gating every pull request that nothing
// requires, so a red run does not block a merge.
func UnenforcedCheck(s model.RepoSnapshot) []model.Finding {
	if s.RequiredChecks == nil || len(s.Workflows) == 0 {
		return nil
	}
	required := *s.RequiredChecks
	type unenforced struct{ context, workflow string }
	var missing []unenforced
	var unscanned []string
	for _, w := range s.Workflows {
		if !gatesEveryPullRequest(w.Text) {
			continue
		}
		found := Jobs(w.Text)
		covered := coveredByRequired(found, required)
		for _, j := range found {
			if j.Matrix || covered[j.ID] || isRequired(j, required) {
				continue
			}
			if j.Uses != nil && codeqlRe.MatchString(*j.Uses) {
				// Enforced through the ruleset's code scanning rule.
				if s.CodeScanningEnforced == nil || !*s.CodeScanningEnforced {
					unscanned = append(unscanned, w.Name)
				}
				continue
			}
			if j.Calls() {
				missing = append(missing, unenforced{j.ID + " / …", w.Name})
			} else {
				missing = append(missing, unenforced{j.Context(), w.Name})
			}
		}
	}
	if len(missing) == 0 && len(unscanned) == 0 {
		return nil
	}
	var parts []string
	if len(missing) > 0 {
		listed := make([]string, len(missing))
		for i, m := range missing {
			listed[i] = fmt.Sprintf("`%s` (%s)", m.context, m.workflow)
		}
		parts = append(parts, strings.Join(listed, ", ")+" not in this repository's `required_status_checks` in the "+
			"github-repos catalogue, so a red run does not block a merge — and a "+
			"Renovate pull request, auto-approved and auto-merged, never has a human "+
			"to notice. Each runs on every pull request, so each can be required "+
			"without leaving one pending. Read the exact context off `gh pr checks` "+
			"rather than inferring it.")
	}
	if len(unscanned) > 0 {
		parts = append(parts, "CodeQL runs ("+strings.Join(unscanned, ", ")+") but the catalogue entry does not set "+
			"`code_scanning = true`, so a new alert does not block a merge. Set that "+
			"rather than requiring the CodeQL status check.")
	}
	return finding(s, "estate.unenforced_check", "medium",
		fmt.Sprintf("%d pull request check(s) nothing requires", len(missing)+len(unscanned)),
		strings.Join(parts, " "), catalogueURL, nil)
}

// ExpectedLockPlatforms: linux_amd64, linux_arm64 and darwin_arm64.
const ExpectedLockPlatforms = 3

var (
	providerRe = regexp.MustCompile(`^provider\s+"([^"]+)"\s*\{`)
	h1Re       = regexp.MustCompile(`"h1:`)
)

// platformsPerProvider counts h1: hashes per provider block. A lock file
// never names a platform, so they are counted, not read.
func platformsPerProvider(lock string) (map[string]int, []string) {
	counts := map[string]int{}
	var order []string
	current := ""
	for _, line := range pyfmt.SplitLines(lock) {
		if m := providerRe.FindStringSubmatch(line); m != nil {
			current = m[1]
			if _, seen := counts[current]; !seen {
				order = append(order, current)
			}
			counts[current] = 0
		} else if current != "" {
			counts[current] += len(h1Re.FindAllStringIndex(line, -1))
		}
	}
	return counts, order
}

// IncompleteTerraformLock: a lock locked for fewer platforms than run
// Terraform, which fails pre-commit on every pull request.
func IncompleteTerraformLock(s model.RepoSnapshot) []model.Finding {
	if deref(s.TerraformLock) == "" {
		return nil
	}
	counts, _ := platformsPerProvider(*s.TerraformLock)
	var short []string
	for name, n := range counts {
		if n < ExpectedLockPlatforms {
			short = append(short, name)
		}
	}
	if len(short) == 0 {
		return nil
	}
	sort.Strings(short)
	parts := make([]string, len(short))
	for i, name := range short {
		base := name[strings.LastIndex(name, "/")+1:]
		parts[i] = fmt.Sprintf("%s (%d of %d)", base, counts[name], ExpectedLockPlatforms)
	}
	return finding(s, "estate.incomplete_terraform_lock", "high",
		fmt.Sprintf("%d provider(s) locked for too few platforms", len(short)),
		strings.Join(parts, ", ")+". CI runs amd64 and adds the missing hash during `init`, "+
			"which modifies a tracked file and fails pre-commit on every pull "+
			"request. Regenerate with `terraform providers lock "+
			"-platform=linux_amd64 -platform=linux_arm64 -platform=darwin_arm64`.",
		s.URL, nil)
}
