// Package model holds the shapes the scan produces: what the checks may see
// about a repository, and what they report.
//
// The Python original's tri-states (None meaning "could not tell") are
// pointers here, and a nil pointer must stay silent in every check, exactly
// as None did. A nil *[]T is "not read"; a non-nil pointer to an empty slice
// is "read, and there were none".
package model

import (
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"time"
)

// Now is the clock every age is measured against. A variable so tests can
// freeze it; nothing else assigns it.
var Now = func() time.Time { return time.Now().UTC() }

// PullRequest is one open pull request, as much of it as the checks need.
type PullRequest struct {
	Number    int
	Title     string
	Author    string
	CreatedAt *time.Time
	URL       string
	Draft     bool
	// Last activity of any kind: what "abandoned" is measured by.
	UpdatedAt *time.Time
	// GraphQL's mergeStateStatus: BLOCKED, DIRTY, BEHIND, CLEAN... Empty
	// where unknown.
	MergeState string
}

// IsRenovate reports whether Renovate opened this, by author login. GitHub
// reports an App's login with a `[bot]` suffix on REST and without it on
// GraphQL, so both forms are accepted.
func (p PullRequest) IsRenovate() bool { return IsRenovateLogin(p.Author) }

// IsRenovateLogin is IsRenovate's rule for a bare login.
func IsRenovateLogin(login string) bool {
	l := strings.TrimSuffix(strings.ToLower(login), "[bot]")
	return l == "renovate" || l == "renovate-bot"
}

// Branch is one branch, as much of it as hygiene.stale_branches needs.
type Branch struct {
	Name        string
	CommittedAt *time.Time
	HasOpenPR   bool
}

// Workflow is one GitHub Actions workflow file with its contents. The text
// never reaches the triage prompt; only the name does.
type Workflow struct {
	Name string
	Text string
}

// CodeScanningAlert is one open code scanning alert.
type CodeScanningAlert struct {
	Number    int
	Rule      string
	Severity  string
	CreatedAt *time.Time
	URL       string
	Tool      string
}

// Issue is one open issue, as much of it as checks/delivery needs.
type Issue struct {
	Number    int
	Title     string
	Labels    []string
	CreatedAt *time.Time
	URL       string
}

// RepoSnapshot is everything the checks are allowed to see about one
// repository. Checks are pure functions of it.
type RepoSnapshot struct {
	Name          string
	FullName      string
	Description   *string
	DefaultBranch string
	Archived      bool
	PushedAt      *time.Time
	CreatedAt     *time.Time
	Topics        []string
	HasLicense    bool
	HasReadme     bool
	URL           string
	Private       bool

	// From the GraphQL detail query; absent when it failed.
	RenovateConfig     *string
	RenovateConfigPath *string
	Readme             *string
	Dockerfile         *string
	Workflows          []Workflow
	TerraformLock      *string
	// Whether github-repos declares this repository. Nil when the catalogue
	// could not be read, which must not read as "unmanaged".
	InCatalogue *bool
	// The contexts the catalogue requires. Nil when unread or undeclared; an
	// empty slice is a claim (declared, requiring nothing).
	RequiredChecks       *[]string
	CodeScanningEnforced *bool
	OpenPRs              []PullRequest
	OpenPRTotal          *int
	Branches             *[]Branch
	BranchTotal          *int
	RenovatePREver       *bool
	LastRelease          *string
	LastReleaseAt        *time.Time
	CodeScanningAlerts   *[]CodeScanningAlert
	OpenIssues           *[]Issue
}

// RenovatePRs are the open pull requests Renovate opened.
func (r RepoSnapshot) RenovatePRs() []PullRequest {
	var prs []PullRequest
	for _, pr := range r.OpenPRs {
		if pr.IsRenovate() {
			prs = append(prs, pr)
		}
	}
	return prs
}

// WorkflowNames are the workflow file names, in order.
func (r RepoSnapshot) WorkflowNames() []string {
	names := make([]string, len(r.Workflows))
	for i, w := range r.Workflows {
		names[i] = w.Name
	}
	return names
}

// Finding is one thing worth a human's attention, about one repository.
type Finding struct {
	Repo        string
	Check       string
	Severity    string
	Title       string
	Detail      string
	EvidenceURL string
	// Days since the underlying fact became true, where there is a timestamp.
	AgeDays *int

	// Filled in by state.Reconcile, not by the checks.
	FirstSeen string
	IsNew     bool
}

// ID is the finding's identity across runs: the first twelve hex characters
// of sha256("repo:check"). Load-bearing: change it and every finding in the
// estate reads as new, and every suppression stops matching.
func (f Finding) ID() string {
	sum := sha256.Sum256([]byte(f.Repo + ":" + f.Check))
	return hex.EncodeToString(sum[:])[:12]
}

// TriageUsage is what one triage call consumed. CostUSD is an estimate from
// a hand-kept price table; BalanceUSD is DeepSeek's own figure, verbatim.
type TriageUsage struct {
	Model            string
	CacheHitTokens   int
	CacheMissTokens  int
	CompletionTokens int
	Peak             bool
	CostUSD          *float64
	BalanceUSD       *string
}

// PromptTokens is hit plus miss.
func (u TriageUsage) PromptTokens() int { return u.CacheHitTokens + u.CacheMissTokens }

// Suggestion is the model's opinion. Not a finding: never counted, never in
// the subject line, never written to state.
type Suggestion struct {
	Text string
	// Empty for a suggestion about the estate as a whole.
	Repo string
}

// TriageOutcome is everything the triage step produced.
type TriageOutcome struct {
	Findings    []Finding
	Summary     string
	Themes      []string
	Usage       *TriageUsage
	Suggestions []Suggestion
}

// Resolved is a finding a previous run saw that no check produced this time.
type Resolved struct {
	Repo  string
	Title string
}

// Ignored is a repository skipped by topic, and the topic that matched.
type Ignored struct {
	FullName string
	Topic    string
}

// ScanResult is one run's output, before it becomes an email.
type ScanResult struct {
	Repos    []RepoSnapshot
	Findings []Finding
	ImageTag string
	// Written by the model, and the only two fields here that are.
	Summary         string
	Themes          []string
	Usage           *TriageUsage
	Resolved        []Resolved
	Suggestions     []Suggestion
	Ignored         []Ignored
	SuppressedCount int
}

// Ptr returns a pointer to v, for the optional fields.
func Ptr[T any](v T) *T { return &v }

// AgeDays is whole days between t and Now, floored at zero, or nil without
// a timestamp. Python's timedelta.days floors, so a moment a few hours in
// the future is -1 there and 0 here after the floor at zero, the same answer.
func AgeDays(t *time.Time) *int {
	if t == nil {
		return nil
	}
	d := FloorDays(Now().Sub(*t))
	if d < 0 {
		d = 0
	}
	return &d
}

// FloorDays is timedelta.days: whole days, rounded towards minus infinity.
func FloorDays(d time.Duration) int {
	day := 24 * time.Hour
	n := int(d / day)
	if d%day < 0 {
		n--
	}
	return n
}
