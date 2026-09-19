"""Is Renovate actually working?

"Configured" and "working" are different questions, and this agent exists
because the gap between them is silent. A repository can carry a Renovate config
for a year while the App was never installed on it, or hold eleven open update
PRs nobody has looked at since March. Neither shows up anywhere you would
naturally look.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..models import Finding, RepoSnapshot

# A Renovate PR open longer than this is not "waiting to be reviewed", it is
# forgotten. Three weeks spans a holiday without crying wolf, and is short
# enough that the dependency has not usually been superseded by the time it is
# reported.
STALE_PR_DAYS = 21

# Enough open Renovate PRs to mean the process has stopped rather than slipped.
# Renovate's own default `prConcurrentLimit` is 10, so at five you are halfway to
# the point where it silently stops opening new ones — which is the failure this
# check is really looking for.
PR_BACKLOG_COUNT = 5


def missing_config(snapshot: RepoSnapshot) -> list[Finding]:
    """No Renovate configuration file at any path Renovate reads."""
    if snapshot.renovate_config is not None:
        return []
    return [
        Finding(
            repo=snapshot.full_name,
            check="renovate.missing_config",
            severity="high",
            title="no Renovate configuration",
            detail=(
                "No renovate.json, renovate.json5 or .renovaterc at any path Renovate "
                "reads, so dependencies here are never updated automatically."
            ),
            evidence_url=snapshot.url,
        )
    ]


def onboarding_unmerged(snapshot: RepoSnapshot) -> list[Finding]:
    """Renovate's onboarding PR is still open.

    Renovate opens exactly one of these per repository and will not start real
    update PRs until it is merged. An open onboarding PR is therefore the
    single clearest sign of a repository where Renovate is installed, appears
    active, and is doing nothing at all.
    """
    for pr in snapshot.renovate_prs:
        if "configure renovate" in pr.title.lower():
            return [
                Finding(
                    repo=snapshot.full_name,
                    check="renovate.onboarding_unmerged",
                    severity="high",
                    title="Renovate onboarding PR never merged",
                    detail=(
                        f"PR #{pr.number} is Renovate's onboarding pull request. Until it "
                        "is merged Renovate opens no update PRs, so this repository looks "
                        "configured but receives nothing."
                    ),
                    evidence_url=pr.url,
                    age_days=_age_days(pr.created_at),
                )
            ]
    return []


def stalled_prs(snapshot: RepoSnapshot) -> list[Finding]:
    """Renovate PRs nobody has merged.

    Reported as one finding rather than one per PR: the actionable fact is
    "this repository's updates have stopped moving", and eleven findings saying
    so is eleven lines of the same sentence.
    """
    # Onboarding has its own finding above, and counting it here would report
    # the same PR twice under two names.
    prs = [
        pr
        for pr in snapshot.renovate_prs
        if not pr.draft and "configure renovate" not in pr.title.lower()
    ]
    if not prs:
        return []

    ages = [age for age in (_age_days(pr.created_at) for pr in prs) if age is not None]
    oldest = max(ages) if ages else None

    stale = oldest is not None and oldest >= STALE_PR_DAYS
    backlog = len(prs) >= PR_BACKLOG_COUNT
    if not (stale or backlog):
        return []

    reason = []
    if backlog:
        reason.append(f"{len(prs)} open Renovate PRs")
    if stale:
        reason.append(f"the oldest has been open {oldest} days")

    return [
        Finding(
            repo=snapshot.full_name,
            check="renovate.stalled_prs",
            severity="medium" if not (stale and backlog) else "high",
            title="Renovate updates are not being merged",
            detail=(
                f"{' and '.join(reason)}. Renovate stops opening new PRs once it hits "
                "prConcurrentLimit, so a backlog eventually becomes silence."
            ),
            evidence_url=f"{snapshot.url}/pulls?q=is%3Apr+is%3Aopen+author%3Aapp%2Frenovate",
            age_days=oldest,
        )
    ]


def default_config_only(snapshot: RepoSnapshot) -> list[Finding]:
    """A Renovate config that is the generated default and nothing else.

    The thing being looked for is a repository nobody ever made a decision
    about — Renovate's onboarding config, merged and never touched. It is valid,
    it lints, and it expresses no policy.

    **Extending a shared preset is a decision**, and the most centralised form
    of one: `"extends": ["github>jay-withers/renovate"]` means the policy lives
    in one repository rather than thirteen. Only the stock `config:*` presets
    count as "no decision", because they are what onboarding writes by default.

    An earlier version of this check looked for five specific keys in the raw
    text and consequently flagged every repository in the estate, all of which
    extend a shared preset. Matching on absence is the wrong shape here: the
    question is whether anything was *chosen*, not whether a named key appears.
    """
    config = snapshot.renovate_config
    if config is None:
        return []

    keys, extends = _config_shape(config)
    # Anything beyond the schema hint and the preset list is a choice someone
    # made deliberately.
    if keys - {"$schema", "extends"}:
        return []
    # A preset that is not one of Renovate's own is this estate's policy,
    # centralised. `config:recommended` and friends are what onboarding writes.
    if any(not preset.startswith("config:") for preset in extends):
        return []

    return [
        Finding(
            repo=snapshot.full_name,
            check="renovate.default_config_only",
            severity="low",
            title="Renovate config is the untouched default",
            detail=(
                f"{snapshot.renovate_config_path} extends only Renovate's stock presets "
                "and sets nothing else — no shared preset, no packageRules, no schedule "
                "and no grouping, so every update arrives as its own unscheduled PR."
            ),
            evidence_url=(
                f"{snapshot.url}/blob/{snapshot.default_branch}/{snapshot.renovate_config_path}"
            ),
        )
    ]


def _config_shape(config: str) -> tuple[set[str], list[str]]:
    """The config's top-level keys and its `extends` list.

    Parsed as JSON where it can be, and falling back to a substring scan where
    it cannot: `renovate.json5` permits comments and trailing commas, which
    `json.loads` rejects and Renovate accepts. The fallback deliberately errs
    towards finding a key — a false negative here costs one low-severity line,
    and a false positive is the bug this check already had once.
    """
    import json

    try:
        parsed = json.loads(config)
    except (ValueError, TypeError):
        keys = {key for key in _KNOWN_KEYS if f'"{key}"' in config or f"{key}:" in config}
        return keys, ["unparsed"] if "extends" in keys else []

    if not isinstance(parsed, dict):
        return set(), []
    extends = parsed.get("extends")
    if isinstance(extends, str):
        extends = [extends]
    return set(parsed), [e for e in (extends or []) if isinstance(e, str)]


# Only used by the JSON5 fallback above, so it needs to cover the keys that mean
# "a decision was made" rather than every key Renovate understands.
_KNOWN_KEYS = (
    "extends",
    "packageRules",
    "schedule",
    "automerge",
    "autoApprove",
    "groupName",
    "labels",
    "customManagers",
    "prConcurrentLimit",
    "dependencyDashboard",
    "ignoreDeps",
    "rangeStrategy",
)


def _age_days(moment: datetime | None) -> int | None:
    """Whole days between `moment` and now, or None where there is no timestamp."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0, (datetime.now(UTC) - moment).days)
