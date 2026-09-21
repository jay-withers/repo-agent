"""The checks, exercised with no HTTP anywhere near them.

Every test here constructs a snapshot and calls a function. That is the whole
point of the checks being pure — the fixtures are literals, the failures are
readable, and nothing needs a transport.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from repoagent import checks
from repoagent.checks import hygiene, renovate
from tests.factories import renovate_pr, snapshot


def _checks(findings: list) -> set[str]:
    return {f.check for f in findings}


def _days_ago(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


# --- renovate ---------------------------------------------------------------


def test_healthy_repository_produces_no_findings() -> None:
    assert checks.run_all(snapshot()) == []


def test_missing_renovate_config_is_high_severity() -> None:
    findings = renovate.missing_config(snapshot(renovate_config=None))

    assert len(findings) == 1
    assert findings[0].check == "renovate.missing_config"
    assert findings[0].severity == "high"


def test_onboarding_pr_open_means_renovate_does_nothing() -> None:
    """The clearest possible "configured but not working" signal."""
    pr = renovate_pr(days_old=90, title="Configure Renovate")
    findings = renovate.onboarding_unmerged(snapshot(open_prs=(pr,)))

    assert len(findings) == 1
    assert findings[0].severity == "high"
    assert findings[0].age_days == 90


def test_onboarding_pr_is_not_also_counted_as_a_stalled_update() -> None:
    """One PR must not produce two findings under two names."""
    pr = renovate_pr(days_old=90, title="Configure Renovate")

    assert renovate.stalled_prs(snapshot(open_prs=(pr,))) == []


def test_a_recent_renovate_pr_is_not_stalled() -> None:
    assert renovate.stalled_prs(snapshot(open_prs=(renovate_pr(days_old=2),))) == []


def test_an_old_renovate_pr_is_stalled() -> None:
    findings = renovate.stalled_prs(snapshot(open_prs=(renovate_pr(days_old=40),)))

    assert len(findings) == 1
    assert findings[0].check == "renovate.stalled_prs"
    assert findings[0].age_days == 40


def test_a_backlog_opened_hours_ago_is_not_a_finding() -> None:
    """The estate opens a week of updates in one Monday burst.

    The shared preset schedules PRs `before 6am on monday`, and this check used
    to run at 07:00 that same morning — so a healthy repository was always
    momentarily backlogged at the one moment it looked, and market-agent was
    reported every week while every PR in it auto-merged the same day.
    """
    prs = tuple(renovate_pr(days_old=0, number=n) for n in range(1, 7))

    assert renovate.stalled_prs(snapshot(open_prs=prs)) == []


def test_last_mondays_batch_still_open_on_sunday_is_a_finding() -> None:
    """The failure the check exists for, at the age the scan actually sees it.

    Renovate opens the batch on Monday and the scan runs the following Sunday,
    so a week's updates that never merged are six days old when counted. This
    pins that `BACKLOG_MIN_AGE_DAYS` is low enough to catch them in the week
    they failed rather than the week after.
    """
    prs = tuple(renovate_pr(days_old=6, number=n) for n in range(1, 7))
    findings = renovate.stalled_prs(snapshot(open_prs=prs))

    assert len(findings) == 1
    assert "6 open Renovate PRs" in findings[0].detail
    assert findings[0].severity == "medium"


def test_a_small_number_of_old_prs_is_not_a_backlog() -> None:
    """Under the count, only the 21-day stale arm can fire."""
    prs = tuple(renovate_pr(days_old=6, number=n) for n in range(1, 4))

    assert renovate.stalled_prs(snapshot(open_prs=prs)) == []


def test_stalled_and_backlogged_together_escalates_to_high() -> None:
    prs = tuple(renovate_pr(days_old=40, number=n) for n in range(1, 7))

    assert renovate.stalled_prs(snapshot(open_prs=prs))[0].severity == "high"


def test_a_repository_renovate_has_never_delivered_to_is_a_finding() -> None:
    """The gap every other Renovate check leaves open.

    Config present, no onboarding PR, nothing open, shared preset extended —
    all four other checks pass on a repository that has never received one
    dependency update.
    """
    repo = snapshot(renovate_pr_ever=False, created_at=_days_ago(30))
    findings = renovate.never_opened_a_pr(repo)

    assert len(findings) == 1
    assert findings[0].check == "renovate.never_opened_a_pr"
    assert findings[0].severity == "high"
    assert findings[0].age_days == 30
    # Every other Renovate check is silent on it, which is why this one exists.
    assert _checks(checks.run_all(repo)) == {"renovate.never_opened_a_pr"}


def test_a_new_repository_has_not_missed_its_first_window_yet() -> None:
    """The preset opens PRs one window a week, so under a week proves nothing."""
    repo = snapshot(renovate_pr_ever=False, created_at=_days_ago(3))

    assert renovate.never_opened_a_pr(repo) == []


def test_an_unanswerable_pr_history_never_reads_as_never_ran() -> None:
    """None is "could not tell" — the `in_catalogue` rule.

    The closed-PR history is one page deep, so a busy repository can fill it
    with human pull requests. Reporting that as "Renovate has never run" would
    name the estate's most active repositories as its deadest.
    """
    assert renovate.never_opened_a_pr(snapshot(renovate_pr_ever=None)) == []


def test_a_repository_with_no_config_is_left_to_the_config_check() -> None:
    """One fault must not produce two findings under two names."""
    repo = snapshot(renovate_config=None, renovate_pr_ever=False, created_at=_days_ago(30))

    assert renovate.never_opened_a_pr(repo) == []
    assert _checks(checks.run_all(repo)) >= {"renovate.missing_config"}
    assert "renovate.never_opened_a_pr" not in _checks(checks.run_all(repo))


def test_a_humans_pull_request_is_not_renovates() -> None:
    human = renovate_pr(days_old=200, author="jay-withers")

    assert renovate.stalled_prs(snapshot(open_prs=(human,))) == []


def test_renovate_is_matched_with_and_without_the_bot_suffix() -> None:
    """GraphQL and REST report an App's login differently."""
    assert renovate_pr(author="renovate[bot]").is_renovate
    assert renovate_pr(author="renovate").is_renovate
    assert not renovate_pr(author="dependabot[bot]").is_renovate


def test_the_untouched_onboarding_config_is_flagged_quietly() -> None:
    findings = renovate.default_config_only(
        snapshot(renovate_config='{"extends": ["config:recommended"]}')
    )

    assert len(findings) == 1
    assert findings[0].severity == "low"


def test_extending_a_shared_preset_is_a_decision() -> None:
    """The estate centralises policy in jay-withers/renovate. That is the point.

    An earlier version of this check flagged all thirteen repositories for
    having "no policy" when every one of them extends the shared preset.
    """
    config = '{"extends": ["github>jay-withers/renovate"]}'

    assert renovate.default_config_only(snapshot(renovate_config=config)) == []


def test_a_stock_preset_plus_real_configuration_is_a_decision() -> None:
    config = '{"extends": ["config:recommended"], "customManagers": [{"customType": "regex"}]}'

    assert renovate.default_config_only(snapshot(renovate_config=config)) == []


def test_the_schema_hint_alone_does_not_count_as_configuration() -> None:
    config = '{"$schema": "https://docs.renovatebot.com/renovate-schema.json", \
"extends": ["config:recommended"]}'

    assert len(renovate.default_config_only(snapshot(renovate_config=config))) == 1


def test_a_json5_config_is_not_parsed_as_json() -> None:
    """A JSON5 config is valid to Renovate and fatal to json.loads.

    The fallback errs towards finding a key: a false negative costs one
    low-severity line, a false positive is the bug this check already had once.
    """
    config = "{\n  // grouped weekly\n  packageRules: [],\n}"

    assert renovate.default_config_only(snapshot(renovate_config=config)) == []


def test_a_single_extends_string_is_handled_like_a_list() -> None:
    """Renovate accepts `"extends": "config:recommended"` as well as a list."""
    findings = renovate.default_config_only(
        snapshot(renovate_config='{"extends": "config:recommended"}')
    )

    assert len(findings) == 1


# --- hygiene ----------------------------------------------------------------


def test_missing_licence_is_only_reported_for_public_repositories() -> None:
    assert hygiene.no_license(snapshot(has_license=False, private=False))
    assert hygiene.no_license(snapshot(has_license=False, private=True)) == []


def test_no_ci_is_high_when_renovate_is_configured() -> None:
    """Updates merged on faith are worse than no updates at all."""
    with_renovate = hygiene.no_ci(snapshot(workflows=()))
    without = hygiene.no_ci(snapshot(workflows=(), renovate_config=None))

    assert with_renovate[0].severity == "high"
    assert without[0].severity == "low"


def test_a_repository_pushed_recently_is_not_stale() -> None:
    assert hygiene.stale(snapshot(pushed_at=datetime.now(UTC) - timedelta(days=30))) == []


def test_a_repository_untouched_for_a_year_is_stale() -> None:
    findings = hygiene.stale(snapshot(pushed_at=datetime.now(UTC) - timedelta(days=365)))

    assert len(findings) == 1
    assert findings[0].age_days == 365


def test_a_missing_timestamp_costs_the_finding_not_the_run() -> None:
    assert hygiene.stale(snapshot(pushed_at=None)) == []


# --- run_all ----------------------------------------------------------------


def test_archived_repositories_are_skipped_entirely() -> None:
    """Every finding would be true, unactionable and permanent."""
    derelict = snapshot(
        archived=True,
        renovate_config=None,
        has_readme=False,
        has_license=False,
        description=None,
        workflows=(),
    )

    assert checks.run_all(derelict) == []


def test_a_neglected_repository_produces_several_findings() -> None:
    findings = checks.run_all(
        snapshot(
            renovate_config=None,
            renovate_config_path=None,
            has_readme=False,
            has_license=False,
            description=None,
            workflows=(),
        )
    )

    assert _checks(findings) == {
        "renovate.missing_config",
        "hygiene.no_readme",
        "hygiene.no_license",
        "hygiene.no_description",
        "hygiene.no_ci",
    }


def test_finding_ids_are_stable_across_runs() -> None:
    """De-duplication later depends on this, and history will depend on it more."""
    first = checks.run_all(snapshot(renovate_config=None))
    second = checks.run_all(snapshot(renovate_config=None))

    assert [f.id for f in first] == [f.id for f in second]
