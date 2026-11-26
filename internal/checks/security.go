package checks

import (
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/jay-withers/repo-agent/internal/model"
)

// severityRank ranks security severity and plain severity together.
// Anything unrecognised ranks last (9) rather than being dropped.
var severityRank = map[string]int{
	"critical": 0, "high": 1, "error": 1, "medium": 2, "warning": 2, "low": 3, "note": 3,
}

func rankOf(s string) int {
	if r, ok := severityRank[s]; ok {
		return r
	}
	return 9
}

const (
	rulesNamed = 5
	alertPage  = 100
)

// counter is Python's Counter: counts that keep first-seen order.
type counter struct {
	order  []string
	counts map[string]int
}

func newCounter() *counter { return &counter{counts: map[string]int{}} }

func (c *counter) add(k string) {
	if _, ok := c.counts[k]; !ok {
		c.order = append(c.order, k)
	}
	c.counts[k]++
}

// OpenCodeScanningAlerts rolls every open alert (CodeQL, Trivy) into one
// finding per repository. It names rule ids and counts and **no file
// paths**: findings go to the triage model, and the location of an unfixed
// vulnerability is the part worth keeping out of it.
func OpenCodeScanningAlerts(s model.RepoSnapshot) []model.Finding {
	if s.CodeScanningAlerts == nil || len(*s.CodeScanningAlerts) == 0 {
		return nil
	}
	alerts := *s.CodeScanningAlerts

	worst := 9
	for _, a := range alerts {
		worst = min(worst, rankOf(a.Severity))
	}
	severity := "low"
	switch {
	case worst <= 1:
		severity = "high"
	case worst == 2:
		severity = "medium"
	}

	bySeverity := newCounter()
	byRule := newCounter()
	tools := map[string]bool{}
	var oldest *time.Time
	for _, a := range alerts {
		name := a.Severity
		if name == "" {
			name = "unrated"
		}
		bySeverity.add(name)
		byRule.add(a.Rule)
		tool := a.Tool
		if tool == "" {
			tool = "unknown tool"
		}
		tools[tool] = true
		if a.CreatedAt != nil && (oldest == nil || a.CreatedAt.Before(*oldest)) {
			oldest = a.CreatedAt
		}
	}

	sevOrder := append([]string(nil), bySeverity.order...)
	sort.SliceStable(sevOrder, func(i, j int) bool { return rankOf(sevOrder[i]) < rankOf(sevOrder[j]) })
	var breakdown []string
	for _, name := range sevOrder {
		breakdown = append(breakdown, fmt.Sprintf("%d %s", bySeverity.counts[name], name))
	}

	rules := append([]string(nil), byRule.order...)
	sort.SliceStable(rules, func(i, j int) bool {
		ci, cj := byRule.counts[rules[i]], byRule.counts[rules[j]]
		if ci != cj {
			return ci > cj
		}
		return rules[i] < rules[j]
	})
	var namedParts []string
	for _, rule := range rules[:min(rulesNamed, len(rules))] {
		part := "`" + rule + "`"
		if n := byRule.counts[rule]; n > 1 {
			part += fmt.Sprintf(" (%d)", n)
		}
		namedParts = append(namedParts, part)
	}
	named := strings.Join(namedParts, ", ")
	if len(rules) > rulesNamed {
		named += fmt.Sprintf(" and %d more rule(s)", len(rules)-rulesNamed)
	}

	count := fmt.Sprint(len(alerts))
	if len(alerts) >= alertPage {
		count = "at least " + count
	}
	toolNames := make([]string, 0, len(tools))
	for t := range tools {
		toolNames = append(toolNames, t)
	}
	sort.Strings(toolNames)

	var age *int
	if oldest != nil {
		age = model.Ptr(model.FloorDays(model.Now().Sub(*oldest)))
	}
	return finding(s, "security.open_code_scanning_alerts", severity,
		fmt.Sprintf("%s open %s alert(s)", count, strings.Join(toolNames, " and ")),
		strings.Join(breakdown, ", ")+". Rules: "+named+". Code scanning reports without failing the "+
			"build, so these sit in the Security tab until someone fixes or dismisses them.",
		s.URL+"/security/code-scanning", age)
}
