package checks

// The golden test (internal/scan) runs every check over a fixture built to
// fire each one, against the Python's output. These cover the rules that
// fixture can't show by itself: silence on a tri-state's nil, and the
// branches of the gate logic.

import (
	"strings"
	"testing"
	"time"

	"github.com/jay-withers/repo-agent/internal/model"
)

func snap() model.RepoSnapshot {
	return model.RepoSnapshot{Name: "a", FullName: "o/a", URL: "https://github.com/o/a", DefaultBranch: "main",
		Description: model.Ptr("d"), HasLicense: true, HasReadme: true,
		Workflows: []model.Workflow{{Name: "ci.yml", Text: "on: push\npermissions: {}\njobs:\n  a:\n    runs-on: x\n"}}}
}

func TestATriStatesNilStaysSilent(t *testing.T) {
	s := snap()
	cfg := "{}"
	s.RenovateConfig = &cfg
	for name, check := range map[string]Check{
		"unmanaged":      Unmanaged,
		"alerts":         OpenCodeScanningAlerts,
		"deploy failed":  DeployFailed,
		"stale branches": StaleBranches,
		"never opened":   NeverOpenedAPR,
		"unreportable":   UnreportableRequiredCheck,
		"unenforced":     UnenforcedCheck,
		"terraform":      TerraformPending,
	} {
		if got := check(s); len(got) != 0 {
			t.Errorf("%s fired on nil: %v", name, got)
		}
	}
}

func TestAnEmptyRequiredListIsAClaim(t *testing.T) {
	s := snap()
	s.Workflows = []model.Workflow{{Name: "ci.yml", Text: "on:\n  pull_request:\njobs:\n  test:\n    runs-on: x\n"}}
	s.RequiredChecks = &[]string{}
	if got := UnenforcedCheck(s); len(got) != 1 || !strings.Contains(got[0].Detail, "`test` (ci.yml)") {
		t.Errorf("got %v", got)
	}
	if got := UnreportableRequiredCheck(s); len(got) != 0 {
		t.Error("nothing required, nothing unreportable")
	}
}

func TestArchivedRepositoriesAreSkipped(t *testing.T) {
	s := snap()
	s.Archived = true
	s.HasReadme = false
	if RunAll(s) != nil {
		t.Error("archived")
	}
	s.Archived = false
	if len(RunAll(s)) == 0 {
		t.Error("not archived: no README fires")
	}
}

func TestAGateCoversTheJobsBehindIt(t *testing.T) {
	text := `on: pull_request
jobs:
  build:
    runs-on: x
  scan:
    needs: build
    runs-on: x
  gate:
    name: container-build
    needs:
      - scan
      - missing
    runs-on: x
  deploy:
    uses: o/r/.github/workflows/d.yml@v1
`
	s := snap()
	s.Workflows = []model.Workflow{{Name: "c.yml", Text: text}}
	s.RequiredChecks = &[]string{"container-build", "deploy / Deploy"}
	if got := UnenforcedCheck(s); len(got) != 0 {
		t.Errorf("build and scan are behind the required gate, deploy is required by prefix: %v", got)
	}
	s.RequiredChecks = &[]string{"deploy"}
	got := UnenforcedCheck(s)
	if len(got) != 1 || !strings.Contains(got[0].Detail, "`container-build` (c.yml)") || strings.Contains(got[0].Detail, "deploy / ") {
		t.Errorf("got %v", got)
	}
}

func TestCodeQLIsEnforcedByTheRulesetNotAStatusCheck(t *testing.T) {
	s := snap()
	s.Workflows = []model.Workflow{{Name: "codeql.yml", Text: "on: [pull_request]\njobs:\n  codeql:\n    uses: ./.github/workflows/codeql.yml\n"}}
	s.RequiredChecks = &[]string{}
	if got := UnenforcedCheck(s); len(got) != 1 || !strings.Contains(got[0].Detail, "code_scanning = true") {
		t.Errorf("got %v", got)
	}
	s.CodeScanningEnforced = model.Ptr(true)
	if got := UnenforcedCheck(s); len(got) != 0 {
		t.Errorf("got %v", got)
	}
}

func TestTriggers(t *testing.T) {
	cases := map[string][]string{
		"on: [push, pull_request] # c\n":                  {"push", "pull_request"},
		"'on':\n  push:\n  \"workflow_call\":\nname: x\n": {"push", "workflow_call"},
		"name: no trigger\n":                              {},
	}
	for text, want := range cases {
		got := Triggers(text)
		if len(got) != len(want) {
			t.Errorf("%q: %v", text, got)
		}
		for _, w := range want {
			if !got[w] {
				t.Errorf("%q: missing %s", text, w)
			}
		}
	}
	if gatesEveryPullRequest("name: x\n") {
		t.Error("no trigger gates nothing")
	}
}

func TestJobs(t *testing.T) {
	jobs := Jobs("jobs:\n  a:\n    name: 'Quoted'\n    needs: [b, c]\n  b:\n    name: \"\"\n    permissions: read-all\n")
	if len(jobs) != 2 || jobs[0].Context() != "Quoted" || len(jobs[0].Needs) != 2 || jobs[1].Context() != "b" || !jobs[1].Scoped {
		t.Errorf("got %+v", jobs)
	}
}

func TestOptionalFieldsPrintLikePython(t *testing.T) {
	if pyStr(nil) != "None" || deref(nil) != "" {
		t.Error("pyStr(nil) is None, deref(nil) is empty")
	}
}

func TestAgeDaysFloorsLikeTimedelta(t *testing.T) {
	now := time.Date(2026, 11, 29, 18, 0, 0, 0, time.UTC)
	model.Now = func() time.Time { return now }
	t.Cleanup(func() { model.Now = func() time.Time { return time.Now().UTC() } })
	future := now.Add(5 * time.Hour)
	if *model.AgeDays(&future) != 0 || model.FloorDays(-5*time.Hour) != -1 || model.FloorDays(47*time.Hour) != 1 {
		t.Error("floors toward minus infinity, and an age is never negative")
	}
	s := snap()
	s.CodeScanningAlerts = &[]model.CodeScanningAlert{{Rule: "r", Severity: "low", CreatedAt: &future}}
	if got := OpenCodeScanningAlerts(s); *got[0].AgeDays != -1 {
		t.Errorf("the alert check keeps Python's unfloored days: %v", *got[0].AgeDays)
	}
}
