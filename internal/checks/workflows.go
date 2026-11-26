package checks

import (
	"fmt"
	"maps"
	"regexp"
	"slices"
	"sort"
	"strings"

	"github.com/jay-withers/repo-agent/internal/model"
	"github.com/jay-withers/repo-agent/internal/pyfmt"
)

var (
	usesRe          = regexp.MustCompile(`^\s*-?\s*uses:\s*['"]?([A-Za-z0-9._-]+/[A-Za-z0-9._/-]+)@([^\s'"#]+)`)
	shaRe           = regexp.MustCompile(`^[0-9a-f]{40}$`)
	runsOnRe        = regexp.MustCompile(`^\s*runs-on:\s*\[?\s*['"]?([A-Za-z0-9._-]+)`)
	topPermissionRe = regexp.MustCompile(`^permissions:`)
	prHeadRe        = regexp.MustCompile(`github\.event\.pull_request\.head\.`)
	prTargetRe      = regexp.MustCompile(`\bpull_request_target\b`)
)

// retiredRunnerImages are images GitHub has retired or announced it will.
var retiredRunnerImages = map[string]bool{
	"ubuntu-18.04": true, "ubuntu-20.04": true, "macos-11": true, "macos-12": true,
	"windows-2016": true, "windows-2019": true,
}

// firstPartyOwners still move their tags, but under a different trust
// model, so they are reported lower rather than ignored.
var firstPartyOwners = map[string]bool{"actions": true, "github": true}

func workflowsURL(s model.RepoSnapshot) string {
	return fmt.Sprintf("%s/tree/%s/.github/workflows", s.URL, s.DefaultBranch)
}

func sortedKeys(m map[string]bool) []string {
	return slices.Sorted(maps.Keys(m))
}

// UnpinnedActions: actions referenced by a tag or branch, not a commit SHA.
func UnpinnedActions(s model.RepoSnapshot) []model.Finding {
	thirdParty := map[string]map[string]bool{}
	firstParty := map[string]bool{}
	for _, w := range s.Workflows {
		for _, line := range pyfmt.SplitLines(w.Text) {
			m := usesRe.FindStringSubmatch(line)
			if m == nil || shaRe.MatchString(m[2]) {
				continue
			}
			action, ref := m[1], m[2]
			owner, _, _ := strings.Cut(action, "/")
			if firstPartyOwners[owner] {
				firstParty[action+"@"+ref] = true
			} else {
				if thirdParty[w.Name] == nil {
					thirdParty[w.Name] = map[string]bool{}
				}
				thirdParty[w.Name][action+"@"+ref] = true
			}
		}
	}
	var out []model.Finding
	if len(thirdParty) > 0 {
		total := 0
		all := map[string]bool{}
		for _, refs := range thirdParty {
			total += len(refs)
			maps.Copy(all, refs)
		}
		examples := sortedKeys(all)
		examples = examples[:min(3, len(examples))]
		others := ""
		if total > len(examples) {
			others = " and others"
		}
		out = append(out, finding(s, "workflows.unpinned_actions", "high",
			fmt.Sprintf("%d third-party action(s) pinned to a mutable tag", total),
			strings.Join(examples, ", ")+others+". A tag can be "+
				"repointed by its owner at any time, and the job runs the new code "+
				"with whatever secrets it holds. Pin to a commit SHA with the tag "+
				"as a trailing comment.", workflowsURL(s), nil)...)
	}
	if len(firstParty) > 0 {
		refs := sortedKeys(firstParty)
		out = append(out, finding(s, "workflows.unpinned_first_party_actions", "low",
			fmt.Sprintf("%d GitHub-owned action(s) pinned to a tag", len(firstParty)),
			strings.Join(refs[:min(3, len(refs))], ", ")+". Lower risk than a third-party "+
				"action, since the tag is moved by GitHub itself, but still a moving "+
				"reference.", workflowsURL(s), nil)...)
	}
	return out
}

// RetiredRunners asks for runner images GitHub has retired or is retiring.
func RetiredRunners(s model.RepoSnapshot) []model.Finding {
	found := map[string]map[string]bool{}
	for _, w := range s.Workflows {
		for _, line := range pyfmt.SplitLines(w.Text) {
			if m := runsOnRe.FindStringSubmatch(line); m != nil && retiredRunnerImages[m[1]] {
				if found[m[1]] == nil {
					found[m[1]] = map[string]bool{}
				}
				found[m[1]][w.Name] = true
			}
		}
	}
	if len(found) == 0 {
		return nil
	}
	images := slices.Sorted(maps.Keys(found))
	parts := make([]string, len(images))
	for i, image := range images {
		parts[i] = fmt.Sprintf("%s (%s)", image, strings.Join(sortedKeys(found[image]), ", "))
	}
	return finding(s, "workflows.retired_runners", "medium", "workflow uses a retired runner image",
		strings.Join(parts, ", ")+". A retired image stops being provisioned on a date GitHub "+
			"sets, and every workflow asking for it fails that morning.", workflowsURL(s), nil)
}

// UnscopedToken: workflows leaving GITHUB_TOKEN at the repository default.
// Scoped at the top of the file, or on every job, both count; a workflow
// triggered only by workflow_call is its caller's business.
func UnscopedToken(s model.RepoSnapshot) []model.Finding {
	var unscoped []string
	for _, w := range s.Workflows {
		triggers := Triggers(w.Text)
		if len(triggers) == 1 && triggers["workflow_call"] {
			continue
		}
		top := false
		for _, line := range pyfmt.SplitLines(w.Text) {
			if topPermissionRe.MatchString(line) {
				top = true
				break
			}
		}
		if top {
			continue
		}
		jobs := Jobs(w.Text)
		allScoped := len(jobs) > 0
		for _, j := range jobs {
			allScoped = allScoped && j.Scoped
		}
		if allScoped {
			continue
		}
		unscoped = append(unscoped, w.Name)
	}
	if len(unscoped) == 0 {
		return nil
	}
	sort.Strings(unscoped)
	return finding(s, "workflows.unscoped_token", "medium",
		fmt.Sprintf("%d workflow(s) with an unscoped GITHUB_TOKEN", len(unscoped)),
		strings.Join(unscoped, ", ")+" set no `permissions:`, so the token takes "+
			"the repository default, which can be write access to everything. Add a "+
			"top-level `permissions: contents: read` and widen per job where needed.",
		workflowsURL(s), nil)
}

// PullRequestTargetCheckout: pull_request_target paired with a reference to
// the PR's own head, the classic "pwn request".
func PullRequestTargetCheckout(s model.RepoSnapshot) []model.Finding {
	var risky []string
	for _, w := range s.Workflows {
		if prTargetRe.MatchString(w.Text) && prHeadRe.MatchString(w.Text) {
			risky = append(risky, w.Name)
		}
	}
	if len(risky) == 0 {
		return nil
	}
	sort.Strings(risky)
	return finding(s, "workflows.pull_request_target_checkout", "high",
		"pull_request_target workflow handles the pull request's own code",
		strings.Join(risky, ", ")+" run on `pull_request_target` and reference "+
			"`github.event.pull_request.head`. That job holds the base repository's "+
			"secrets and token while working on code anyone can submit. Use "+
			"`pull_request` for anything that builds or runs the change.",
		workflowsURL(s), nil)
}
