package checks

import (
	"regexp"
	"strings"

	"github.com/jay-withers/repo-agent/internal/pyfmt"
)

// Workflow files in this estate use two-space indentation, so a job id sits
// at two spaces and its keys at four. Regexes rather than a YAML parser, for
// the same reason the catalogue is not parsed as HCL.
var (
	jobIDRe     = regexp.MustCompile(`^  ([A-Za-z0-9_.-]+):\s*$`)
	jobNameRe   = regexp.MustCompile(`^    name:\s*(.+?)\s*$`)
	jobUsesRe   = regexp.MustCompile(`^    uses:\s*(\S+)`)
	jobNeedsRe  = regexp.MustCompile(`^    needs:\s*(.*?)\s*$`)
	needsItemRe = regexp.MustCompile(`^      - \s*([A-Za-z0-9_.-]+)\s*$`)
	jobPermsRe  = regexp.MustCompile(`^    permissions:`)
	jobMatrixRe = regexp.MustCompile(`^      matrix:\s*$`)
	onRe        = regexp.MustCompile(`^["']?on["']?:\s*(.*?)\s*$`)
	eventRe     = regexp.MustCompile(`^  ["']?([A-Za-z_]+)["']?:\s*$`)
	listSplitRe = regexp.MustCompile(`[\s,\[\]]+`)
)

// Job is one job, as much of it as the checks depend on.
type Job struct {
	ID   string
	Name *string
	// The reusable workflow it calls, if any: its contexts are then
	// `<id> / <job>`, the second half defined in another repository.
	Uses   *string
	Needs  []string
	Matrix bool
	// Whether the job sets its own `permissions:`.
	Scoped bool
}

// Calls reports whether the job calls a reusable workflow.
func (j Job) Calls() bool { return j.Uses != nil }

// Context is what a job reporting one name reports.
func (j Job) Context() string {
	if j.Name != nil && *j.Name != "" {
		return *j.Name
	}
	return j.ID
}

func splitList(s string) []string {
	var out []string
	for _, part := range listSplitRe.Split(s, -1) {
		if part != "" {
			out = append(out, part)
		}
	}
	return out
}

func beforeHash(s string) string {
	before, _, _ := strings.Cut(s, "#")
	return strings.TrimSpace(before)
}

// Jobs is every job in one workflow file, in declaration order.
func Jobs(text string) []Job {
	var found []Job
	inJobs := false
	var job *Job
	inNeeds := false
	closeJob := func() {
		if job != nil {
			found = append(found, *job)
		}
	}
	for _, line := range pyfmt.SplitLines(text) {
		if !inJobs {
			inJobs = strings.TrimRight(line, " \t\r\n\v\f") == "jobs:"
			continue
		}
		if m := jobIDRe.FindStringSubmatch(line); m != nil {
			closeJob()
			job = &Job{ID: m[1], Needs: []string{}}
			inNeeds = false
			continue
		}
		if job == nil {
			continue
		}
		if inNeeds {
			if m := needsItemRe.FindStringSubmatch(line); m != nil {
				job.Needs = append(job.Needs, m[1])
				continue
			}
			inNeeds = false
		}
		if m := jobUsesRe.FindStringSubmatch(line); m != nil {
			uses := m[1]
			job.Uses = &uses
		} else if m := jobNeedsRe.FindStringSubmatch(line); m != nil {
			inline := beforeHash(m[1])
			job.Needs = splitList(inline)
			if job.Needs == nil {
				job.Needs = []string{}
			}
			inNeeds = inline == ""
		} else if jobMatrixRe.MatchString(line) {
			job.Matrix = true
		} else if jobPermsRe.MatchString(line) {
			job.Scoped = true
		} else if m := jobNameRe.FindStringSubmatch(line); m != nil {
			name := strings.Trim(m[1], `"'`)
			job.Name = &name
		}
	}
	closeJob()
	return found
}

// Triggers is the set of events a workflow runs on, inline or block form.
func Triggers(text string) map[string]bool {
	lines := pyfmt.SplitLines(text)
	for i, line := range lines {
		m := onRe.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		found := map[string]bool{}
		if inline := beforeHash(m[1]); inline != "" {
			for _, e := range splitList(inline) {
				found[e] = true
			}
			return found
		}
		for _, rest := range lines[i+1:] {
			if strings.TrimSpace(rest) != "" && !strings.HasPrefix(rest, " ") {
				break
			}
			if e := eventRe.FindStringSubmatch(rest); e != nil {
				found[e[1]] = true
			}
		}
		return found
	}
	return map[string]bool{}
}
