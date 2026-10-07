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

# A human pull request with no activity for a month has stopped being work in
# progress. Drafts get three: a draft is a declared parking spot, and reporting
# one after four weeks would teach people to stop opening them.
ABANDONED_PR_DAYS = 30
ABANDONED_DRAFT_DAYS = 90

# A branch whose head is three months old, with no pull request open from it, is
# either merged-and-not-deleted or forgotten work. Bot branches are excluded:
# Renovate and Dependabot create and delete their own.
STALE_BRANCH_DAYS = 90
BOT_BRANCH_PREFIXES = ("renovate/", "dependabot/")
# Enough names to act on without turning one finding into a list.
MAX_NAMED = 10


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


def abandoned_prs(snapshot: RepoSnapshot) -> list[Finding]:
    """Human pull requests nobody has touched in weeks.

    Renovate's own pull requests are `renovate.stalled_prs`'s business and are
    excluded here, so the two checks never report the same pull request.
    Measured from the last update rather than creation: a long-running pull
    request someone is still pushing to is not abandoned.
    """
    abandoned = []
    for pr in snapshot.open_prs:
        if pr.is_renovate:
            continue
        age = _age_days(pr.updated_at or pr.created_at)
        threshold = ABANDONED_DRAFT_DAYS if pr.draft else ABANDONED_PR_DAYS
        if age is not None and age >= threshold:
            abandoned.append((age, pr))

    if not abandoned:
        return []
    abandoned.sort(key=lambda item: item[0], reverse=True)
    oldest = abandoned[0][0]
    named = ", ".join(
        f"#{pr.number} {pr.title!r}{' (draft)' if pr.draft else ''} by {pr.author}, idle {age}d"
        for age, pr in abandoned[:MAX_NAMED]
    )
    # The page is the oldest open pull requests first, so anything beyond it is
    # newer and less likely to qualify — but not certainly, so say so.
    truncated = snapshot.open_pr_total is not None and snapshot.open_pr_total > len(
        snapshot.open_prs
    )
    return [
        Finding(
            repo=snapshot.full_name,
            check="hygiene.abandoned_prs",
            severity="low",
            title=f"{'at least ' if truncated else ''}{len(abandoned)} abandoned pull request(s)",
            detail=(
                f"{named}. Merge, close or mark as draft; an open pull request nobody is "
                "working on goes on conflicting with everything that does merge."
            ),
            evidence_url=f"{snapshot.url}/pulls",
            age_days=oldest,
        )
    ]


def stale_branches(snapshot: RepoSnapshot) -> list[Finding]:
    """Branches with old heads and no pull request open from them.

    Silent where branches could not be read (`None`), rather than reporting a
    clean bill on a list nobody fetched.
    """
    if snapshot.branches is None:
        return []

    stale_found = []
    for branch in snapshot.branches:
        if branch.name == snapshot.default_branch or branch.has_open_pr:
            continue
        if branch.name.startswith(BOT_BRANCH_PREFIXES):
            continue
        age = _age_days(branch.committed_at)
        if age is not None and age >= STALE_BRANCH_DAYS:
            stale_found.append((age, branch.name))

    if not stale_found:
        return []
    stale_found.sort(reverse=True)
    truncated = snapshot.branch_total is not None and snapshot.branch_total > len(snapshot.branches)
    named = ", ".join(f"{name} ({age}d)" for age, name in stale_found[:MAX_NAMED])
    more = len(stale_found) - MAX_NAMED
    return [
        Finding(
            repo=snapshot.full_name,
            check="hygiene.stale_branches",
            severity="low",
            title=(
                f"{'at least ' if truncated else ''}{len(stale_found)} stale branch(es) "
                "with no open pull request"
            ),
            detail=(
                f"{named}{f' and {more} more' if more > 0 else ''}. Delete them if merged, "
                "or open a pull request if the work is still wanted. Enabling "
                "`delete_branch_on_merge` in github-repos stops the merged ones recurring."
            ),
            evidence_url=f"{snapshot.url}/branches/stale",
            age_days=stale_found[0][0],
        )
    ]


def _age_days(moment: datetime | None) -> int | None:
    """Whole days between `moment` and now, or None where there is no timestamp."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0, (datetime.now(UTC) - moment).days)
