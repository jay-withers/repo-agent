"""The checks: pure functions from one repository to its findings.

Every check here is `(RepoSnapshot) -> list[Finding]` and does no I/O, so it is
tested by constructing a snapshot directly with no HTTP anywhere near the test.
Keep them that way — the moment a check fetches something, it stops being
testable from a literal and starts needing a transport.

**These establish facts. The model does not.** `llm.py` reorders and summarises
what comes out of here, and is never the thing that decides whether a repository
has a Renovate config. That division is the whole reason a model is allowed near
the digest at all: every claim in the email traces to a function in this package
that anyone can read.
"""

from __future__ import annotations

from collections.abc import Callable

from ..models import Finding, RepoSnapshot
from . import container, estate, hygiene, renovate, security, terraform, workflows

Check = Callable[[RepoSnapshot], list[Finding]]

# Order is the tie-break for findings of equal severity, so Renovate first:
# a dependency bot that has quietly stopped working is the thing this agent
# exists to catch, and everything in `hygiene` is cosmetic beside it.
ALL: tuple[Check, ...] = (
    renovate.missing_config,
    renovate.onboarding_unmerged,
    # Above `stalled_prs`: a repository that has never had an update at all
    # outranks one whose updates have stopped moving.
    renovate.never_opened_a_pr,
    # Above `stalled_prs`, which skips the pull requests this reports: a plan
    # waiting on approval is an action only a human can take, and until it is
    # taken nothing in that change is deployed.
    terraform.awaiting_approval,
    renovate.stalled_prs,
    renovate.default_config_only,
    # Estate before hygiene: a repository outside the catalogue has nothing
    # enforcing anything on it, which outranks every cosmetic finding below.
    estate.unmanaged,
    # Above the lock check for the same reason `unmanaged` is above everything:
    # a required check nothing reports blocks every pull request in the
    # repository, which outranks anything wrong inside one of them.
    estate.unreportable_required_check,
    # The mirror of the above, and below it: a check nothing requires lets a
    # bad pull request through, which is worse than nothing merging but not
    # as bad as nothing merging at all.
    estate.unenforced_check,
    estate.incomplete_terraform_lock,
    estate.not_shared_preset,
    # A vulnerability CodeQL has already found is a known defect in running
    # code, which outranks a workflow that merely could be exploited.
    security.open_code_scanning_alerts,
    # A privileged token running untrusted code is the worst thing a workflow
    # file can say, so it leads the workflow checks.
    workflows.pull_request_target_checkout,
    workflows.unpinned_actions,
    workflows.retired_runners,
    workflows.unscoped_token,
    container.unpinned_base_image,
    container.runs_as_root,
    hygiene.no_readme,
    hygiene.no_license,
    hygiene.no_description,
    hygiene.no_ci,
    hygiene.stale,
    # Last: housekeeping inside a repository that is otherwise fine.
    hygiene.abandoned_prs,
    hygiene.stale_branches,
)


def run_all(snapshot: RepoSnapshot) -> list[Finding]:
    """Every finding for one repository.

    Archived repositories are skipped wholesale rather than per check: they are
    read-only by definition, so every finding would be true, unactionable and
    permanent — which is the recipe for a digest nobody opens.
    """
    if snapshot.archived:
        return []
    return [finding for check in ALL for finding in check(snapshot)]
