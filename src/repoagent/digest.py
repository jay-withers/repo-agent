"""Turning a scan into an email.

**Every figure here is computed, never written by a model.** The triage step in
`llm.py` contributes exactly two fields — `summary` and `themes` — and is told
the tables are already above its text and must not be restated. Every count,
every repository name and every finding on this page comes from the checks.

That rule exists because market-agent's summary job learned it the hard way:
given a reconciliation count and no trades table, the model accurately reported
from what it had been given that nothing had happened on a day three trades
executed. The defence is not a better prompt, it is never letting the model near
a number that gets reported.

Model output is escaped on the way into the HTML, along with everything else
GitHub supplies. A repository description containing a `<` was always able to
break the layout; a model writing one is merely likelier.
"""

from __future__ import annotations

from html import escape

from .models import ScanResult, TriageUsage

# Kept deliberately plain. This is read in a mail client, where anything clever
# renders differently in each one, and the content is a table and some links.
_STYLE = (
    "font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; "
    "font-size: 14px; line-height: 1.5;"
)


def subject(result: ScanResult) -> str:
    """The subject line, built from stored counts.

    Leads with what changed rather than with the agent's name: an inbox shows
    perhaps forty characters, and "repo-agent" in all of them is wasted.
    """
    repos = len(result.repos)
    findings = len(result.findings)
    if findings == 0:
        return f"repo-agent — {repos} repos, nothing to flag"
    subject = f"repo-agent — {findings} finding{'s' if findings != 1 else ''} across {repos} repos"
    # The count of *new* things is what decides whether this gets opened, and an
    # inbox shows perhaps forty characters, so it goes in only when non-zero.
    new = sum(1 for f in result.findings if f.is_new)
    return f"{subject} ({new} new)" if new else subject


def render_text(result: ScanResult) -> str:
    """The plain-text body. Also what `repoagent render` prints."""
    lines = [
        f"Scanned {len(result.repos)} repositories.",
        "",
    ]

    # Below the count, above the findings: the model's paragraph is context for
    # the list, so it reads before it. It is rendered only if it exists — an
    # absent summary means triage was off or failed, which is not worth a line
    # saying so.
    if result.summary:
        lines.append(result.summary)
        lines.append("")
    if result.themes:
        lines.append(f"Themes: {'; '.join(result.themes)}")
        lines.append("")

    if result.findings:
        new = sum(1 for f in result.findings if f.is_new)
        headline = f"{len(result.findings)} finding(s)"
        if new:
            headline += f", {new} new since last run"
        lines.append(f"{headline}:")
        lines.append("")
        for finding in result.findings:
            age = f" ({finding.age_days}d)" if finding.age_days is not None else ""
            marker = " [NEW]" if finding.is_new else ""
            lines.append(f"  [{finding.severity}] {finding.repo}: {finding.title}{age}{marker}")
            lines.append(f"      {finding.detail}")
            if finding.evidence_url:
                lines.append(f"      {finding.evidence_url}")
        lines.append("")
    else:
        lines.append("No findings.")
        lines.append("")

    # Resolved before the repository list, because a fixed thing is the most
    # encouraging line in the email and it belongs where it will be read.
    if result.resolved:
        lines.append(f"Resolved since last run ({len(result.resolved)}):")
        for repo, title in result.resolved:
            lines.append(f"  {repo}: {title}")
        lines.append("")

    if result.suppressed_count:
        lines.append(
            f"{result.suppressed_count} finding(s) suppressed. `repoagent state` lists them."
        )
        lines.append("")

    # Named, not just counted: an exemption nobody can see is one nobody
    # revisits, and a topic added by mistake would otherwise silently drop a
    # repository out of the digest for ever.
    if result.ignored:
        listed = ", ".join(f"{name} ({topic})" for name, topic in result.ignored)
        lines.append(f"Ignored by topic: {listed}")
        lines.append("")

    lines.append("Repositories:")
    for repo in sorted(result.repos, key=lambda r: r.full_name):
        flags = []
        if repo.archived:
            flags.append("archived")
        if not repo.description:
            flags.append("no description")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        lines.append(f"  {repo.full_name}{suffix}")

    lines.append("")
    # Which build produced this. With the job's Terraform in one repository and
    # the image in another, "what actually ran" is otherwise cross-repo
    # archaeology.
    lines.append(f"repo-agent {result.image_tag}")
    if result.usage:
        lines.append(triage_footer(result.usage))
    return "\n".join(lines)


def triage_footer(usage: TriageUsage) -> str:
    """What the commentary above cost, and whether there is money for the next one.

    The cost is prefixed `~` because it comes from a price table maintained by
    hand in `llm.py`; the balance is not, because it comes from DeepSeek. Showing
    both is the point — the estimate can drift, the balance cannot.
    """
    parts = [
        f"triage {usage.model}",
        f"{usage.prompt_tokens:,} in ({usage.cache_hit_tokens:,} cached) "
        f"/ {usage.completion_tokens:,} out",
    ]
    if usage.cost_usd is not None:
        parts.append(f"~${usage.cost_usd:.4f}{' peak' if usage.peak else ''}")
    if usage.balance_usd is not None:
        parts.append(f"${usage.balance_usd} left")
    return " · ".join(parts)


def render_html(result: ScanResult) -> str:
    """The HTML body. Same content as the text version, in a table."""
    rows = []
    for repo in sorted(result.repos, key=lambda r: r.full_name):
        flags = []
        if repo.archived:
            flags.append("archived")
        if not repo.description:
            flags.append("no description")
        rows.append(
            f"<tr><td style='padding:4px 12px 4px 0'>"
            f"<a href='{escape(repo.url, quote=True)}'>{escape(repo.full_name)}</a></td>"
            f"<td style='padding:4px 0; color:#666'>{escape(', '.join(flags))}</td></tr>"
        )

    # Italic and grey, so it reads as commentary rather than as a finding. The
    # distinction matters: everything else in this email is machine-established
    # and this paragraph is not.
    summary_html = ""
    if result.summary:
        summary_html = (
            f"<p style='color:#333; font-style:italic; "
            f"border-left:3px solid #ddd; padding-left:12px; margin:16px 0'>"
            f"{escape(result.summary)}</p>"
        )
    if result.themes:
        chips = " ".join(
            f"<span style='background:#f0f0f0; border-radius:3px; padding:2px 6px; "
            f"font-size:12px; color:#444'>{escape(theme)}</span>"
            for theme in result.themes
        )
        summary_html += f"<p style='margin:12px 0'>{chips}</p>"

    resolved_html = ""
    if result.resolved:
        done = "".join(
            f"<li>{escape(repo)}: {escape(title)}</li>" for repo, title in result.resolved
        )
        resolved_html = f"<h2 style='font-size:16px'>Resolved since last run</h2><ul>{done}</ul>"

    ignored_html = ""
    if result.ignored:
        listed = ", ".join(f"{escape(n)} ({escape(t)})" for n, t in result.ignored)
        ignored_html = f"<p style='color:#888; font-size:12px'>Ignored by topic: {listed}</p>"

    suppressed_html = ""
    if result.suppressed_count:
        suppressed_html = (
            f"<p style='color:#888; font-size:12px'>{result.suppressed_count} finding(s) "
            f"suppressed — <code>repoagent state</code> lists them.</p>"
        )

    findings_html = ""
    if result.findings:
        items = "".join(
            f"<li><strong>{escape(f.repo)}</strong>: {escape(f.title)}"
            + (
                " <span style='background:#e8f4ff; color:#06c; border-radius:3px; "
                "padding:1px 5px; font-size:11px'>new</span>"
                if f.is_new
                else ""
            )
            + (
                f" <span style='color:#666'>({f.age_days}d)</span>"
                if f.age_days is not None
                else ""
            )
            + f"<br><span style='color:#444'>{escape(f.detail)}</span></li>"
            for f in result.findings
        )
        new = sum(1 for f in result.findings if f.is_new)
        heading = "Findings" + (f" — {new} new since last run" if new else "")
        findings_html = f"<h2 style='font-size:16px'>{escape(heading)}</h2><ul>{items}</ul>"

    return (
        f'<div style="{_STYLE}">'
        f"<p>Scanned <strong>{len(result.repos)}</strong> repositories.</p>"
        f"{summary_html}"
        f"{findings_html}"
        f"{resolved_html}"
        f"{suppressed_html}"
        f"{ignored_html}"
        f"<h2 style='font-size:16px'>Repositories</h2>"
        f"<table style='border-collapse:collapse'>{''.join(rows)}</table>"
        f"<p style='color:#888; font-size:12px'>repo-agent {escape(result.image_tag)}"
        + (f"<br>{escape(triage_footer(result.usage))}" if result.usage else "")
        + "</p>"
        "</div>"
    )
