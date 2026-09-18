"""Turning a scan into an email.

**Every figure here is computed, never written by a model.** When the LLM
triage step lands it will add commentary and a priority ordering, and it will be
told the tables are already above its text and must not be restated. That rule
exists because market-agent's summary job learned it the hard way: given a
reconciliation count and no trades table, the model accurately reported from
what it had been given that nothing had happened on a day three trades executed.
The defence is not a better prompt, it is never letting the model near a number
that gets reported.
"""

from __future__ import annotations

from .models import ScanResult

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
    return f"repo-agent — {findings} finding{'s' if findings != 1 else ''} across {repos} repos"


def render_text(result: ScanResult) -> str:
    """The plain-text body. Also what `repoagent render` prints."""
    lines = [
        f"Scanned {len(result.repos)} repositories.",
        "",
    ]

    if result.findings:
        lines.append(f"{len(result.findings)} finding(s):")
        lines.append("")
        for finding in result.findings:
            age = f" ({finding.age_days}d)" if finding.age_days is not None else ""
            lines.append(f"  [{finding.severity}] {finding.repo}: {finding.title}{age}")
            lines.append(f"      {finding.detail}")
            if finding.evidence_url:
                lines.append(f"      {finding.evidence_url}")
        lines.append("")
    else:
        lines.append("No findings.")
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
    return "\n".join(lines)


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
            f"<a href='{repo.url}'>{repo.full_name}</a></td>"
            f"<td style='padding:4px 0; color:#666'>{', '.join(flags)}</td></tr>"
        )

    findings_html = ""
    if result.findings:
        items = "".join(
            f"<li><strong>{f.repo}</strong>: {f.title}"
            + (
                f" <span style='color:#666'>({f.age_days}d)</span>"
                if f.age_days is not None
                else ""
            )
            + f"<br><span style='color:#444'>{f.detail}</span></li>"
            for f in result.findings
        )
        findings_html = f"<h2 style='font-size:16px'>Findings</h2><ul>{items}</ul>"

    return (
        f'<div style="{_STYLE}">'
        f"<p>Scanned <strong>{len(result.repos)}</strong> repositories.</p>"
        f"{findings_html}"
        f"<h2 style='font-size:16px'>Repositories</h2>"
        f"<table style='border-collapse:collapse'>{''.join(rows)}</table>"
        f"<p style='color:#888; font-size:12px'>repo-agent {result.image_tag}</p>"
        f"</div>"
    )
