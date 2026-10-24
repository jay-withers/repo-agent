"""Infrastructure changes waiting on a human.

Every planned Terraform change needs approval before it can merge: the shared
`terraform-deploy.yml` fails a pull request's plan until the PR carries
`terraform-approved`, because `cd-terraform` applies on merge and Renovate
auto-merges anything green. While a plan waits, that workflow keeps
`terraform-approval-required` on the pull request, and removes it once the plan
is approved or no longer changes anything. This module only reads the label.

The approval is deliberately not automatic, so the digest is where it surfaces:
nothing else announces a red required check on a bot's pull request.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..models import Finding, RepoSnapshot

# Must match `pending-label` in jay-withers/workflows' terraform-deploy.yml.
APPROVAL_PENDING_LABEL = "terraform-approval-required"

MAX_NAMED = 10


def awaiting_approval(snapshot: RepoSnapshot) -> list[Finding]:
    """Open pull requests whose Terraform plan is waiting to be approved.

    One finding per repository, naming each pull request, for the same reason
    `renovate.stalled_prs` is one finding: the action is "go and review the
    plans in this repository". Renovate's pull requests carrying the label are
    excluded from `stalled_prs`, so none is reported twice.
    """
    waiting = [pr for pr in snapshot.open_prs if APPROVAL_PENDING_LABEL in pr.labels]
    if not waiting:
        return []

    ages = [age for age in (_age_days(pr.created_at) for pr in waiting) if age is not None]
    oldest = max(ages) if ages else None
    named = ", ".join(f"#{pr.number} {pr.title!r}" for pr in waiting[:MAX_NAMED])
    # The page is the oldest open pull requests, so one beyond it could carry
    # the label too.
    truncated = snapshot.open_pr_total is not None and snapshot.open_pr_total > len(
        snapshot.open_prs
    )
    return [
        Finding(
            repo=snapshot.full_name,
            check="terraform.awaiting_approval",
            severity="medium",
            title=(
                f"{'at least ' if truncated else ''}{len(waiting)} Terraform plan(s) "
                "awaiting approval"
            ),
            detail=(
                f"{named}. Review the plan comment on each, then add the `terraform-approved` "
                "label and re-run the failed jobs; merging applies it. Until then the change "
                "is not deployed, and Renovate cannot merge it."
            ),
            evidence_url=(
                f"{snapshot.url}/pulls?q=is%3Apr+is%3Aopen+label%3A{APPROVAL_PENDING_LABEL}"
            ),
            age_days=oldest,
        )
    ]


def _age_days(moment: datetime | None) -> int | None:
    """Whole days between `moment` and now, or None where there is no timestamp."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0, (datetime.now(UTC) - moment).days)
