"""What GitHub's own security tooling has already found and nobody has fixed.

CodeQL runs in CI and its job is green whatever it finds — results land in
Security → Code scanning, a tab nobody visits unprompted. So an alert can sit
open for months on a repository whose every check is passing. This is the
prompt.

Only CodeQL, and only open alerts. Dependabot and secret scanning alerts are
still deliberately unread; see CLAUDE.md.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from ..models import Finding, RepoSnapshot

# Security severity where a rule has one, plain severity where it does not, in
# one ranking. Anything unrecognised ranks last rather than being dropped.
_RANK = {
    "critical": 0,
    "high": 1,
    "error": 1,
    "medium": 2,
    "warning": 2,
    "low": 3,
    "note": 3,
}

# How many distinct rules to name before "and N more". The link carries the rest.
_RULES_NAMED = 5

# Matches `client.PAGE_SIZE`: a full page means there may be more.
_PAGE = 100


def open_code_scanning_alerts(snapshot: RepoSnapshot) -> list[Finding]:
    """Open CodeQL alerts, rolled into one finding per repository.

    One finding rather than one per alert, because `Finding.id` is `repo:check`
    and the digest is read by a person: forty rows for forty alerts bury every
    other finding, while one row with a link to the tab says the same thing.

    Severity follows the worst alert. The detail names rule ids and counts but
    **no file paths**: findings are sent to the triage model, and the location of
    an unfixed vulnerability is the one part worth keeping out of a prompt that
    may be trained on.
    """
    alerts = snapshot.code_scanning_alerts
    if not alerts:
        return []

    worst = min(_RANK.get(a.severity, 9) for a in alerts)
    severity = "high" if worst <= 1 else "medium" if worst == 2 else "low"

    by_severity = Counter(a.severity or "unrated" for a in alerts)
    breakdown = ", ".join(
        f"{count} {name}"
        for name, count in sorted(by_severity.items(), key=lambda kv: _RANK.get(kv[0], 9))
    )
    rules = sorted(
        Counter(a.rule for a in alerts).items(),
        key=lambda kv: (-kv[1], kv[0]),
    )
    named = ", ".join(
        f"`{rule}`" + (f" ({n})" if n > 1 else "") for rule, n in rules[:_RULES_NAMED]
    )
    if len(rules) > _RULES_NAMED:
        named += f" and {len(rules) - _RULES_NAMED} more rule(s)"

    count = f"at least {len(alerts)}" if len(alerts) >= _PAGE else str(len(alerts))
    oldest = min((a.created_at for a in alerts if a.created_at), default=None)

    return [
        Finding(
            repo=snapshot.full_name,
            check="security.open_code_scanning_alerts",
            severity=severity,
            title=f"{count} open CodeQL alert(s)",
            detail=(
                f"{breakdown}. Rules: {named}. CodeQL reports without failing the build, "
                "so these sit in the Security tab until someone fixes or dismisses them."
            ),
            evidence_url=f"{snapshot.url}/security/code-scanning",
            age_days=(datetime.now(UTC) - oldest).days if oldest else None,
        )
    ]
