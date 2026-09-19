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
from . import estate, hygiene, renovate, workflows

Check = Callable[[RepoSnapshot], list[Finding]]

# Order is the tie-break for findings of equal severity, so Renovate first:
# a dependency bot that has quietly stopped working is the thing this agent
# exists to catch, and everything in `hygiene` is cosmetic beside it.
ALL: tuple[Check, ...] = (
    renovate.missing_config,
    renovate.onboarding_unmerged,
    renovate.stalled_prs,
    renovate.default_config_only,
    # Estate before hygiene: a repository outside the catalogue has nothing
    # enforcing anything on it, which outranks every cosmetic finding below.
    estate.unmanaged,
    estate.incomplete_terraform_lock,
    estate.not_shared_preset,
    workflows.unpinned_actions,
    workflows.retired_runners,
    hygiene.no_readme,
    hygiene.no_license,
    hygiene.no_description,
    hygiene.no_ci,
    hygiene.stale,
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
