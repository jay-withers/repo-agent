// Package digest turns a scan into an email.
//
// **Every figure here is computed, never written by a model.** The triage
// step contributes the summary, the themes and the suggestions, and the
// suggestions are labelled as opinion. Everything GitHub or the model
// supplies is escaped on the way into the HTML.
//
// The output is pinned byte for byte by testdata/golden, which the Python
// implementation rendered before the port: a change here changes those files
// in the same PR, on purpose.
package digest

import (
	"fmt"
	"sort"
	"strings"

	"github.com/jay-withers/repo-agent/internal/model"
	"github.com/jay-withers/repo-agent/internal/pyfmt"
)

const style = "font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; " +
	"font-size: 14px; line-height: 1.5;"

var esc = pyfmt.EscapeHTML

func newCount(findings []model.Finding) int {
	n := 0
	for _, f := range findings {
		if f.IsNew {
			n++
		}
	}
	return n
}

// Subject leads with what changed: an inbox shows forty characters.
func Subject(r model.ScanResult) string {
	repos, findings := len(r.Repos), len(r.Findings)
	if findings == 0 {
		return fmt.Sprintf("repo-agent — %d repos, nothing to flag", repos)
	}
	plural := "s"
	if findings == 1 {
		plural = ""
	}
	subject := fmt.Sprintf("repo-agent — %d finding%s across %d repos", findings, plural, repos)
	if n := newCount(r.Findings); n > 0 {
		return fmt.Sprintf("%s (%d new)", subject, n)
	}
	return subject
}

func sortedRepos(r model.ScanResult) []model.RepoSnapshot {
	repos := append([]model.RepoSnapshot(nil), r.Repos...)
	sort.SliceStable(repos, func(i, j int) bool { return repos[i].FullName < repos[j].FullName })
	return repos
}

func flags(repo model.RepoSnapshot) []string {
	var f []string
	if repo.Archived {
		f = append(f, "archived")
	}
	if repo.Description == nil || *repo.Description == "" {
		f = append(f, "no description")
	}
	return f
}

// Text is the plain-text body, and what `repoagent render` prints.
func Text(r model.ScanResult) string {
	lines := []string{fmt.Sprintf("Scanned %d repositories.", len(r.Repos)), ""}
	if r.Summary != "" {
		lines = append(lines, r.Summary, "")
	}
	if len(r.Themes) > 0 {
		lines = append(lines, "Themes: "+strings.Join(r.Themes, "; "), "")
	}
	if len(r.Findings) > 0 {
		headline := fmt.Sprintf("%d finding(s)", len(r.Findings))
		if n := newCount(r.Findings); n > 0 {
			headline += fmt.Sprintf(", %d new since last run", n)
		}
		lines = append(lines, headline+":", "")
		for _, f := range r.Findings {
			age, marker := "", ""
			if f.AgeDays != nil {
				age = fmt.Sprintf(" (%dd)", *f.AgeDays)
			}
			if f.IsNew {
				marker = " [NEW]"
			}
			lines = append(lines, fmt.Sprintf("  [%s] %s: %s%s%s", f.Severity, f.Repo, f.Title, age, marker),
				"      "+f.Detail)
			if f.EvidenceURL != "" {
				lines = append(lines, "      "+f.EvidenceURL)
			}
		}
		lines = append(lines, "")
	} else {
		lines = append(lines, "No findings.", "")
	}
	if len(r.Resolved) > 0 {
		lines = append(lines, fmt.Sprintf("Resolved since last run (%d):", len(r.Resolved)))
		for _, x := range r.Resolved {
			lines = append(lines, fmt.Sprintf("  %s: %s", x.Repo, x.Title))
		}
		lines = append(lines, "")
	}
	if r.SuppressedCount > 0 {
		lines = append(lines, fmt.Sprintf("%d finding(s) suppressed. `repoagent state` lists them.", r.SuppressedCount), "")
	}
	if len(r.Suggestions) > 0 {
		lines = append(lines, "Suggestions (model opinion, not checked):")
		for _, s := range r.Suggestions {
			where := ""
			if s.Repo != "" {
				where = s.Repo + ": "
			}
			lines = append(lines, "  - "+where+s.Text)
		}
		lines = append(lines, "")
	}
	if len(r.Ignored) > 0 {
		parts := make([]string, len(r.Ignored))
		for i, x := range r.Ignored {
			parts[i] = fmt.Sprintf("%s (%s)", x.FullName, x.Topic)
		}
		lines = append(lines, "Ignored by topic: "+strings.Join(parts, ", "), "")
	}
	lines = append(lines, "Repositories:")
	for _, repo := range sortedRepos(r) {
		suffix := ""
		if f := flags(repo); len(f) > 0 {
			suffix = "  [" + strings.Join(f, ", ") + "]"
		}
		lines = append(lines, "  "+repo.FullName+suffix)
	}
	lines = append(lines, "", "repo-agent "+r.ImageTag)
	if r.Usage != nil {
		lines = append(lines, TriageFooter(*r.Usage))
	}
	return strings.Join(lines, "\n")
}

// TriageFooter is what the commentary cost (an estimate, so `~`) and the
// balance left (DeepSeek's own figure).
func TriageFooter(u model.TriageUsage) string {
	parts := []string{
		"triage " + u.Model,
		fmt.Sprintf("%s in (%s cached) / %s out", pyfmt.Thousands(u.PromptTokens()),
			pyfmt.Thousands(u.CacheHitTokens), pyfmt.Thousands(u.CompletionTokens)),
	}
	if u.CostUSD != nil {
		peak := ""
		if u.Peak {
			peak = " peak"
		}
		parts = append(parts, fmt.Sprintf("~$%.4f%s", *u.CostUSD, peak))
	}
	if u.BalanceUSD != nil {
		parts = append(parts, "$"+*u.BalanceUSD+" left")
	}
	return strings.Join(parts, " · ")
}

// HTML is the same content as Text, in a table.
func HTML(r model.ScanResult) string {
	var rows strings.Builder
	for _, repo := range sortedRepos(r) {
		fmt.Fprintf(&rows, "<tr><td style='padding:4px 12px 4px 0'><a href='%s'>%s</a></td>"+
			"<td style='padding:4px 0; color:#666'>%s</td></tr>",
			esc(repo.URL), esc(repo.FullName), esc(strings.Join(flags(repo), ", ")))
	}

	summary := ""
	if r.Summary != "" {
		summary = "<p style='color:#333; font-style:italic; " +
			"border-left:3px solid #ddd; padding-left:12px; margin:16px 0'>" + esc(r.Summary) + "</p>"
	}
	if len(r.Themes) > 0 {
		chips := make([]string, len(r.Themes))
		for i, t := range r.Themes {
			chips[i] = "<span style='background:#f0f0f0; border-radius:3px; padding:2px 6px; " +
				"font-size:12px; color:#444'>" + esc(t) + "</span>"
		}
		summary += "<p style='margin:12px 0'>" + strings.Join(chips, " ") + "</p>"
	}

	resolved := ""
	if len(r.Resolved) > 0 {
		var done strings.Builder
		for _, x := range r.Resolved {
			done.WriteString("<li>" + esc(x.Repo) + ": " + esc(x.Title) + "</li>")
		}
		resolved = "<h2 style='font-size:16px'>Resolved since last run</h2><ul>" + done.String() + "</ul>"
	}

	suggestions := ""
	if len(r.Suggestions) > 0 {
		var items strings.Builder
		for _, s := range r.Suggestions {
			where := ""
			if s.Repo != "" {
				where = esc(s.Repo) + ": "
			}
			items.WriteString("<li>" + where + esc(s.Text) + "</li>")
		}
		suggestions = "<h2 style='font-size:16px; color:#666'>Suggestions</h2>" +
			"<p style='color:#888; font-size:12px; margin-top:-8px'>" +
			"Model opinion. Not checked, and not counted above.</p>" +
			"<ul style='color:#555'>" + items.String() + "</ul>"
	}

	ignored := ""
	if len(r.Ignored) > 0 {
		parts := make([]string, len(r.Ignored))
		for i, x := range r.Ignored {
			parts[i] = esc(x.FullName) + " (" + esc(x.Topic) + ")"
		}
		ignored = "<p style='color:#888; font-size:12px'>Ignored by topic: " + strings.Join(parts, ", ") + "</p>"
	}

	suppressed := ""
	if r.SuppressedCount > 0 {
		suppressed = fmt.Sprintf("<p style='color:#888; font-size:12px'>%d finding(s) "+
			"suppressed — <code>repoagent state</code> lists them.</p>", r.SuppressedCount)
	}

	findings := ""
	if len(r.Findings) > 0 {
		var items strings.Builder
		for _, f := range r.Findings {
			items.WriteString("<li><strong>" + esc(f.Repo) + "</strong>: " + esc(f.Title))
			if f.IsNew {
				items.WriteString(" <span style='background:#e8f4ff; color:#06c; border-radius:3px; " +
					"padding:1px 5px; font-size:11px'>new</span>")
			}
			if f.AgeDays != nil {
				fmt.Fprintf(&items, " <span style='color:#666'>(%dd)</span>", *f.AgeDays)
			}
			items.WriteString("<br><span style='color:#444'>" + esc(f.Detail) + "</span></li>")
		}
		heading := "Findings"
		if n := newCount(r.Findings); n > 0 {
			heading += fmt.Sprintf(" — %d new since last run", n)
		}
		findings = "<h2 style='font-size:16px'>" + esc(heading) + "</h2><ul>" + items.String() + "</ul>"
	}

	footer := ""
	if r.Usage != nil {
		footer = "<br>" + esc(TriageFooter(*r.Usage))
	}
	return `<div style="` + style + `">` +
		fmt.Sprintf("<p>Scanned <strong>%d</strong> repositories.</p>", len(r.Repos)) +
		summary + findings + resolved + suggestions + suppressed + ignored +
		"<h2 style='font-size:16px'>Repositories</h2>" +
		"<table style='border-collapse:collapse'>" + rows.String() + "</table>" +
		"<p style='color:#888; font-size:12px'>repo-agent " + esc(r.ImageTag) + footer + "</p>" +
		"</div>"
}
