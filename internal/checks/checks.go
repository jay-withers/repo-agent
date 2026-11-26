// Package checks holds the pure functions from one repository to its
// findings. Every check is func(model.RepoSnapshot) []model.Finding and does
// no I/O, so it is tested by building a snapshot, with no HTTP anywhere near.
//
// **These establish facts. The model does not.** The triage step reorders
// and summarises what comes out of here, and never decides whether a
// repository has a Renovate config.
//
// Every title and detail is the Python original's, character for character:
// the digest must read the same the day the Go binary takes over. Where the
// Python quoted a PR title with !r, this uses pyfmt.Repr.
package checks

import "github.com/jay-withers/repo-agent/internal/model"

// Check is one check.
type Check func(model.RepoSnapshot) []model.Finding

// All is every check in order. Order is the tie-break for findings of equal
// severity; see the Python checks/__init__.py for why each sits where it does.
var All = []Check{
	DeployFailed,
	BuildFailed,
	MissingConfig,
	OnboardingUnmerged,
	NeverOpenedAPR,
	BlockedPRs,
	StalledPRs,
	TerraformPending,
	DeployPending,
	DefaultConfigOnly,
	Unmanaged,
	UnreportableRequiredCheck,
	UnenforcedCheck,
	IncompleteTerraformLock,
	NotSharedPreset,
	OpenCodeScanningAlerts,
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
