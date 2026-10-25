"""What the shared delivery workflows are waiting on a human for.

jay-withers/workflows opens an issue for each: `terraform-pending` when
`cd-terraform` planned changes on main that have not been applied (applying is
a manual run), and `deploy-failed` when a deploy or apply on main failed.
Creating either emails the owner once; these checks are the weekly reminder of
whatever is still open. Both read `RepoSnapshot.open_issues`, which needs the
App's Issues permission, and stay silent without it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..models import Finding, Issue, RepoSnapshot

# Must match the labels jay-withers/workflows' terraform-deploy.yml and
# container-apps-deploy.yml put on the issues they open.
TERRAFORM_PENDING_LABEL = "terraform-pending"
DEPLOY_FAILED_LABEL = "deploy-failed"


def deploy_failed(snapshot: RepoSnapshot) -> list[Finding]:
    """Deploys or applies on main that failed and have not succeeded since.

    High: whatever that release carried is not running, and the next success
    is the only thing that closes the issue.
    """
    issues = _labelled(snapshot, DEPLOY_FAILED_LABEL)
    if not issues:
        return []
    return [
        Finding(
            repo=snapshot.full_name,
            check="delivery.deploy_failed",
            severity="high",
            title=f"{len(issues)} failed deploy(s) not yet fixed",
            detail=(
                f"{_named(issues)}. Each issue links the failed run. Fix the cause and re-run "
                "it; the next successful run closes the issue."
            ),
            evidence_url=_label_url(snapshot, DEPLOY_FAILED_LABEL),
            age_days=_oldest(issues),
        )
    ]


def terraform_pending(snapshot: RepoSnapshot) -> list[Finding]:
    """Terraform changes merged to main and waiting to be applied.

    Medium: nothing is broken, but main and the environment disagree until
    someone reviews the plan and runs `cd-terraform` by hand.
    """
    issues = _labelled(snapshot, TERRAFORM_PENDING_LABEL)
    if not issues:
        return []
    return [
        Finding(
            repo=snapshot.full_name,
            check="delivery.terraform_pending",
            severity="medium",
            title="Terraform changes waiting to be applied",
            detail=(
                f"{_named(issues)}. Review the plan the issue links, then run cd-terraform from "
                "the Actions tab to apply; the issue closes itself once nothing is left to apply."
            ),
            evidence_url=_label_url(snapshot, TERRAFORM_PENDING_LABEL),
            age_days=_oldest(issues),
        )
    ]


def _labelled(snapshot: RepoSnapshot, label: str) -> list[Issue]:
    # None (no permission, or the request failed) and () both mean nothing to
    # report; only an issue actually seen produces a finding.
    return [issue for issue in snapshot.open_issues or () if label in issue.labels]


def _named(issues: list[Issue]) -> str:
    return ", ".join(f"#{issue.number} {issue.title!r}" for issue in issues)


def _label_url(snapshot: RepoSnapshot, label: str) -> str:
    return f"{snapshot.url}/issues?q=is%3Aissue+is%3Aopen+label%3A{label}"


def _oldest(issues: list[Issue]) -> int | None:
    ages = [
        max(0, (datetime.now(UTC) - i.created_at).days) for i in issues if i.created_at is not None
    ]
    return max(ages) if ages else None
