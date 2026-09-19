"""Repository hygiene: the things a human would notice on the repo's front page.

Low stakes individually, which is why every one of these is `low` or `medium`
and why the triage step exists — thirty hygiene findings must never bury one
repository whose Renovate has silently stopped.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..models import Finding, RepoSnapshot

# Past this, a repository is either finished or abandoned, and the digest cannot
# tell which. Six months is long enough that ordinary quiet periods — a library
# that simply works — do not trip it.
STALE_PUSH_DAYS = 180


def no_readme(snapshot: RepoSnapshot) -> list[Finding]:
    """No README at the repository root."""
    if snapshot.has_readme:
        return []
    return [
        Finding(
            repo=snapshot.full_name,
            check="hygiene.no_readme",
            severity="medium",
            title="no README",
            detail="Nothing at README.md, so the repository's front page is a file listing.",
            evidence_url=snapshot.url,
        )
    ]


def no_license(snapshot: RepoSnapshot) -> list[Finding]:
    """No licence GitHub can detect.

    Only reported for public repositories: an unlicensed private repository is
    the normal case and flagging it is noise.
    """
    if snapshot.has_license or snapshot.private:
        return []
    return [
        Finding(
            repo=snapshot.full_name,
            check="hygiene.no_license",
            severity="medium",
            title="public repository with no licence",
            detail=(
                "GitHub detects no licence, which in most jurisdictions means nobody "
                "may legally reuse the code regardless of it being public."
            ),
            evidence_url=snapshot.url,
        )
    ]


def no_description(snapshot: RepoSnapshot) -> list[Finding]:
    """No one-line description."""
    if snapshot.description:
        return []
    return [
        Finding(
            repo=snapshot.full_name,
            check="hygiene.no_description",
            severity="low",
            title="no description",
            detail="No description set, so the repository is unidentifiable in a list.",
            evidence_url=snapshot.url,
        )
    ]


def no_ci(snapshot: RepoSnapshot) -> list[Finding]:
    """No GitHub Actions workflows.

    Relevant to this estate specifically: Renovate raising PRs into a repository
    with no CI means every update is merged on faith, which is worse than not
    having Renovate at all.
    """
    if snapshot.workflows:
        return []
    severity = "high" if snapshot.renovate_config is not None else "low"
    return [
        Finding(
            repo=snapshot.full_name,
            check="hygiene.no_ci",
            severity=severity,
            title="no CI workflows",
            detail=(
                "No .github/workflows/*.yml. "
                + (
                    "Renovate is configured here, so dependency updates arrive with "
                    "nothing testing them."
                    if snapshot.renovate_config is not None
                    else "Nothing runs on push or pull request."
                )
            ),
            evidence_url=snapshot.url,
        )
    ]


def stale(snapshot: RepoSnapshot) -> list[Finding]:
    """Nothing pushed in a long time."""
    age = _age_days(snapshot.pushed_at)
    if age is None or age < STALE_PUSH_DAYS:
        return []
    return [
        Finding(
            repo=snapshot.full_name,
            check="hygiene.stale",
            severity="low",
            title="no pushes in six months",
            detail=(
                f"Last push was {age} days ago. If it is finished, archiving it removes "
                "it from every check here; if it is not, it needs attention."
            ),
            evidence_url=snapshot.url,
            age_days=age,
        )
    ]


def _age_days(moment: datetime | None) -> int | None:
    """Whole days between `moment` and now, or None where there is no timestamp."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0, (datetime.now(UTC) - moment).days)
