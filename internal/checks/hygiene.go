package checks

import (
	"fmt"
	"sort"
	"strings"

	"github.com/jay-withers/repo-agent/internal/model"
	"github.com/jay-withers/repo-agent/internal/pyfmt"
)

// Hygiene thresholds; see docs/checks.md.
const (
	StalePushDays      = 180
	AbandonedPRDays    = 30
	AbandonedDraftDays = 90
	StaleBranchDays    = 90
	maxNamed           = 10
)

var botBranchPrefixes = []string{"renovate/", "dependabot/"}

// NoReadme: nothing at README.md.
func NoReadme(s model.RepoSnapshot) []model.Finding {
	if s.HasReadme {
		return nil
	}
	return finding(s, "hygiene.no_readme", "medium", "no README",
		"Nothing at README.md, so the repository's front page is a file listing.", s.URL, nil)
}

// NoLicense: a public repository with no licence GitHub can detect.
func NoLicense(s model.RepoSnapshot) []model.Finding {
	if s.HasLicense || s.Private {
		return nil
	}
	return finding(s, "hygiene.no_license", "medium", "public repository with no licence",
		"GitHub detects no licence, which in most jurisdictions means nobody "+
			"may legally reuse the code regardless of it being public.", s.URL, nil)
}

// NoDescription: no one-line description.
func NoDescription(s model.RepoSnapshot) []model.Finding {
	if deref(s.Description) != "" {
		return nil
	}
	return finding(s, "hygiene.no_description", "low", "no description",
		"No description set, so the repository is unidentifiable in a list.", s.URL, nil)
}

// NoCI: no workflows. High where Renovate is configured, since its updates
// then merge on faith.
func NoCI(s model.RepoSnapshot) []model.Finding {
	if len(s.Workflows) > 0 {
		return nil
	}
	severity := "low"
	rest := "Nothing runs on push or pull request."
	if s.RenovateConfig != nil {
		severity = "high"
		rest = "Renovate is configured here, so dependency updates arrive with " +
			"nothing testing them."
	}
	return finding(s, "hygiene.no_ci", severity, "no CI workflows",
		"No .github/workflows/*.yml. "+rest, s.URL, nil)
}

// Stale: nothing pushed in six months.
func Stale(s model.RepoSnapshot) []model.Finding {
	age := model.AgeDays(s.PushedAt)
	if age == nil || *age < StalePushDays {
		return nil
	}
	return finding(s, "hygiene.stale", "low", "no pushes in six months",
		fmt.Sprintf("Last push was %d days ago. If it is finished, archiving it removes "+
			"it from every check here; if it is not, it needs attention.", *age), s.URL, age)
}

// AbandonedPRs: human pull requests idle for weeks. Renovate's are
// stalled_prs' business.
func AbandonedPRs(s model.RepoSnapshot) []model.Finding {
	type aged struct {
		age int
		pr  model.PullRequest
	}
	var abandoned []aged
	for _, pr := range s.OpenPRs {
		if pr.IsRenovate() {
			continue
		}
		at := pr.UpdatedAt
		if at == nil {
			at = pr.CreatedAt
		}
		age := model.AgeDays(at)
		threshold := AbandonedPRDays
		if pr.Draft {
			threshold = AbandonedDraftDays
		}
		if age != nil && *age >= threshold {
			abandoned = append(abandoned, aged{*age, pr})
		}
	}
	if len(abandoned) == 0 {
		return nil
	}
	sort.SliceStable(abandoned, func(i, j int) bool { return abandoned[i].age > abandoned[j].age })
	oldest := abandoned[0].age
	var named []string
	for _, a := range abandoned[:min(maxNamed, len(abandoned))] {
		draft := ""
		if a.pr.Draft {
			draft = " (draft)"
		}
		named = append(named, fmt.Sprintf("#%d %s%s by %s, idle %dd", a.pr.Number, pyfmt.Repr(a.pr.Title), draft, a.pr.Author, a.age))
	}
	prefix := ""
	if s.OpenPRTotal != nil && *s.OpenPRTotal > len(s.OpenPRs) {
		prefix = "at least "
	}
	return finding(s, "hygiene.abandoned_prs", "low",
		fmt.Sprintf("%s%d abandoned pull request(s)", prefix, len(abandoned)),
		strings.Join(named, ", ")+". Merge, close or mark as draft; an open pull request nobody is "+
			"working on goes on conflicting with everything that does merge.",
		s.URL+"/pulls", &oldest)
}

// StaleBranches: old branch heads with no pull request open from them.
// Silent where branches could not be read.
func StaleBranches(s model.RepoSnapshot) []model.Finding {
	if s.Branches == nil {
		return nil
	}
	type aged struct {
		age  int
		name string
	}
	var found []aged
	for _, b := range *s.Branches {
		if b.Name == s.DefaultBranch || b.HasOpenPR {
			continue
		}
		bot := false
		for _, p := range botBranchPrefixes {
			if strings.HasPrefix(b.Name, p) {
				bot = true
			}
		}
		if bot {
			continue
		}
		if age := model.AgeDays(b.CommittedAt); age != nil && *age >= StaleBranchDays {
			found = append(found, aged{*age, b.Name})
		}
	}
	if len(found) == 0 {
		return nil
	}
	// Python's sort(reverse=True) on (age, name): age descending, then name
	// descending.
	sort.SliceStable(found, func(i, j int) bool {
		if found[i].age != found[j].age {
			return found[i].age > found[j].age
		}
		return found[i].name > found[j].name
	})
	prefix := ""
	if s.BranchTotal != nil && *s.BranchTotal > len(*s.Branches) {
		prefix = "at least "
	}
	var named []string
	for _, f := range found[:min(maxNamed, len(found))] {
		named = append(named, fmt.Sprintf("%s (%dd)", f.name, f.age))
	}
	more := ""
	if n := len(found) - maxNamed; n > 0 {
		more = fmt.Sprintf(" and %d more", n)
	}
	return finding(s, "hygiene.stale_branches", "low",
		fmt.Sprintf("%s%d stale branch(es) with no open pull request", prefix, len(found)),
		strings.Join(named, ", ")+more+". Delete them if merged, "+
			"or open a pull request if the work is still wanted. Enabling "+
			"`delete_branch_on_merge` in github-repos stops the merged ones recurring.",
		s.URL+"/branches/stale", model.Ptr(found[0].age))
}
