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
# The number to measure this against is the estate's own `prConcurrentLimit`,
# which the shared preset sets to 20 rather than leaving at Renovate's default
# of 10 — five is a quarter of the way to silence, not half.
PR_BACKLOG_COUNT = 5

# A backlog only means anything once the PRs in it have had time to merge.
#
# This is **coupled to the scan's cron**. The shared preset opens every
# repository's PRs `before 6am on monday`, and the scan runs Sunday evening —
# the point of maximum drain, six days later. Two days is therefore generous:
# the batch this check is looking for is a week old by the time it is counted,
# and the guard exists only to discount a manually triggered Renovate run that
# happened to open a batch just before the scan.
#
# Move the cron back towards Monday morning and this number has to rise with
# it, or every healthy repository reads as backlogged — that is what it did at
# `0 7 * * 1`, one hour after the burst. Raise it much above two and the
# opposite failure appears: a week's batch that never merged is only six days
# old on the Sunday it should be caught.
BACKLOG_MIN_AGE_DAYS = 2

# How long a repository gets to receive its first update before never having
# had one is a finding. The shared preset opens PRs in one window a week, so
# anything under a full week is a repository that has not reached its first
# window yet rather than one Renovate has forgotten.
#
# Eight days rather than seven because the scan is itself weekly and runs an
# hour after the window: at eight, a repository created on any day of the week
# has had two windows pass before it is ever named. That is deliberately
# lenient — the cost of waiting one more Monday is nothing, and the cost of
# reporting a repository that was about to work is a check people stop reading.
FIRST_UPDATE_GRACE_DAYS = 8


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
    # Volume alone is not evidence: it has to be volume that outlived a
    # schedule window. Where no PR carries a timestamp neither arm can fire,
    # which errs towards silence — a false positive here is the bug this check
    # has already had once.
    backlog = len(prs) >= PR_BACKLOG_COUNT and oldest is not None and oldest >= BACKLOG_MIN_AGE_DAYS
    if not (stale or backlog):
        return []

    # Both arms now require an age, so the age is always worth stating: it is
    # what tells a reader this is a backlog that has sat rather than one that
    # arrived this morning.
    reason = []
    if backlog:
        reason.append(f"{len(prs)} open Renovate PRs")
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


def never_opened_a_pr(snapshot: RepoSnapshot) -> list[Finding]:
    """A configured repository Renovate has never opened a single PR in.

    This is the gap every other check in this module leaves open. A repository
    created from the template carries a Renovate config, so `missing_config`
    passes; the config is committed directly rather than onboarded, so there is
    no onboarding PR for `onboarding_unmerged` to find; nothing is open, so
    `stalled_prs` sees nothing; and it extends the shared preset, so
    `default_config_only` is satisfied. Every check is green and the repository
    has never received one dependency update.

    Four repositories in this estate were in exactly that state at once — new
    ones, all showing "onboarded" rather than "activated" in Mend's portal, all
    with a full Dependency Dashboard and seven updates parked under "Awaiting
    Schedule". The cause was that the shared preset opens PRs only `before 6am
    on monday`, and none of them had had a Renovate job land inside that
    six-hour window since being created.

    **It only ever fires once per repository.** The moment Renovate opens its
    first PR this goes quiet for good, so it catches a repository that never
    started and not one that stops later — `stalled_prs` is the check for that.
    A repository that has been dead since birth is the case worth a finding,
    because nothing else in the estate will ever mention it.
    """
    if snapshot.renovate_config is None:
        # `missing_config` already reports this, and more usefully.
        return []
    # None is "could not tell", and must never render as "Renovate has never
    # run" — see `RepoSnapshot.renovate_pr_ever`.
    if snapshot.renovate_pr_ever is not False:
        return []

    age = _age_days(snapshot.created_at)
    if age is None or age < FIRST_UPDATE_GRACE_DAYS:
        return []

    return [
        Finding(
            repo=snapshot.full_name,
            check="renovate.never_opened_a_pr",
            severity="high",
            title="Renovate has never opened a pull request here",
            detail=(
                f"{snapshot.renovate_config_path} is present and Renovate has run, but in "
                f"{age} days it has never opened a single update PR. Check the Dependency "
                'Dashboard issue: updates sitting under "Awaiting Schedule" mean no '
                "Renovate job has landed inside the preset's weekly window. "
                '"Create all awaiting schedule PRs at once" clears the backlog; '
                "triggering a run on its own does not, because the run re-evaluates the "
                "schedule and parks them again."
            ),
            evidence_url=snapshot.url,
            age_days=age,
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
