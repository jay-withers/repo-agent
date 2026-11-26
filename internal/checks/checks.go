// Package checks holds the pure functions from one repository to its
// findings. Every check is func(model.RepoSnapshot) []model.Finding and does
// no I/O, so it is tested by building a snapshot, with no HTTP anywhere near.
//
// **These establish facts. The model does not.** The triage step reorders
// and summarises what comes out of here, and never decides whether a
// repository has a Renovate config.
//
// Every title and detail is pinned by testdata/golden (written by the Python
// implementation before the port). PR and issue titles are quoted with
// pyfmt.Repr, Python's repr(), which is what the digest has always shown.
package checks

import "github.com/jay-withers/repo-agent/internal/model"

// Check is one check.
type Check func(model.RepoSnapshot) []model.Finding

// All is every check in order. Order is the tie-break for findings of equal
// severity, so it runs from "nothing is running" to housekeeping.
var All = []Check{
	// A failed deploy means a release is not running, which outranks
	// anything about how updates arrive.
	DeployFailed,
	// A main that no longer builds fails every PR's container-build, so it
	// is the cause of whatever BlockedPRs finds below it.
	BuildFailed,
	MissingConfig,
	OnboardingUnmerged,
	// Above StalledPRs: never having had an update outranks updates that
	// have stopped moving.
	NeverOpenedAPR,
	// Above StalledPRs, which skips these: a PR that cannot merge holds back
	// every update it carries, CVE fixes included.
	BlockedPRs,
	StalledPRs,
	// Merged but not yet applied: a human's action, not a defect.
	TerraformPending,
	DeployPending,
	DefaultConfigOnly,
	// Estate before hygiene: outside the catalogue, nothing enforces anything.
	Unmanaged,
	// A required check nothing reports blocks every PR, which outranks
	// anything wrong inside one of them; its mirror, a check nothing
	// requires, lets a bad PR through, which is worse than nothing merging
	// but not as bad as nothing merging at all.
	UnreportableRequiredCheck,
	UnenforcedCheck,
	IncompleteTerraformLock,
	NotSharedPreset,
	// A vulnerability already found outranks a workflow that could be
	// exploited.
	OpenCodeScanningAlerts,
	// A privileged token running untrusted code is the worst thing a
	// workflow file can say.
	PullRequestTargetCheckout,
	UnpinnedActions,
	RetiredRunners,
	UnscopedToken,
	UnpinnedBaseImage,
	RunsAsRoot,
	NoReadme,
	NoLicense,
	NoDescription,
	NoCI,
	Stale,
	// Last: housekeeping inside a repository that is otherwise fine.
	AbandonedPRs,
	StaleBranches,
}

// RunAll is every finding for one repository. Archived repositories are
// skipped wholesale: every finding would be true, unactionable and permanent.
func RunAll(s model.RepoSnapshot) []model.Finding {
	if s.Archived {
		return nil
	}
	var out []model.Finding
	for _, check := range All {
		out = append(out, check(s)...)
	}
	return out
}

func finding(s model.RepoSnapshot, check, severity, title, detail, url string, age *int) []model.Finding {
	return []model.Finding{{
		Repo: s.FullName, Check: check, Severity: severity,
		Title: title, Detail: detail, EvidenceURL: url, AgeDays: age,
	}}
}

func deref(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

// pyStr is Python's f"{x}" of an optional string: None prints as "None".
func pyStr(p *string) string {
	if p == nil {
		return "None"
	}
	return *p
}
