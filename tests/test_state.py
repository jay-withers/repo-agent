"""History: what is new, what has gone, and what we have agreed to ignore.

`reconcile` is pure, so every test here is literals — the same rule as the
checks. The blob I/O around it is tested by its absence: nothing configured must
degrade to "everything is new" rather than failing.
"""

from __future__ import annotations

import json
from datetime import date

from repoagent import state as state_module
from repoagent.models import Finding
from repoagent.state import KnownFinding, State, Suppression, reconcile

TODAY = date(2026, 9, 21)
LAST_WEEK = "2026-09-14"


def _finding(check: str = "renovate.missing_config", repo: str = "jay-withers/a") -> Finding:
    return Finding(
        repo=repo, check=check, severity="high", title="no Renovate configuration", detail="..."
    )


def _seen(finding: Finding, when: str = LAST_WEEK) -> dict[str, KnownFinding]:
    return {
        finding.id: KnownFinding(
            first_seen=when, repo=finding.repo, check=finding.check, title=finding.title
        )
    }


# --- new and known ----------------------------------------------------------


def test_a_finding_never_seen_before_is_new() -> None:
    out = reconcile(State(), [_finding()], today=TODAY)

    assert len(out.new) == 1
    assert out.findings[0].first_seen == "2026-09-21"


def test_a_finding_seen_last_week_is_not_new_and_keeps_its_first_seen() -> None:
    finding = _finding()
    out = reconcile(State(findings=_seen(finding)), [finding], today=TODAY)

    assert out.new == ()
    assert out.findings[0].first_seen == LAST_WEEK


def test_first_seen_is_separate_from_age_days() -> None:
    """One is how long the fact has been true, the other how long we have known.

    Conflating them would claim a missing LICENCE appeared on the day the agent
    first ran.
    """
    finding = Finding(
        repo="r",
        check="hygiene.no_license",
        severity="medium",
        title="t",
        detail="d",
        age_days=None,
    )
    out = reconcile(State(), [finding], today=TODAY)

    assert out.findings[0].age_days is None
    assert out.findings[0].first_seen == "2026-09-21"


def test_identical_findings_across_runs_share_an_id() -> None:
    """The property everything here depends on."""
    assert _finding().id == _finding().id


# --- resolved ---------------------------------------------------------------


def test_a_finding_that_has_gone_is_resolved() -> None:
    gone = _finding(check="hygiene.no_readme")
    out = reconcile(State(findings=_seen(gone)), [], today=TODAY)

    assert len(out.resolved) == 1
    assert out.resolved[0].title == "no Renovate configuration"
    assert out.resolved[0].repo == "jay-withers/a"


def test_a_resolved_finding_is_not_carried_into_the_next_state() -> None:
    gone = _finding()
    out = reconcile(State(findings=_seen(gone)), [], today=TODAY)

    assert out.state.findings == {}


def test_resolved_findings_are_named_from_stored_detail() -> None:
    """By the time it is resolved no check produces it, so nothing else describes it."""
    gone = _finding()
    state = State(
        findings={
            gone.id: KnownFinding(
                first_seen=LAST_WEEK, repo="jay-withers/z", check="c", title="stored title"
            )
        }
    )

    assert reconcile(state, [], today=TODAY).resolved[0].title == "stored title"


# --- suppression ------------------------------------------------------------


def test_a_suppressed_finding_is_held_back() -> None:
    finding = _finding()
    state = State(suppressed={finding.id: Suppression(reason="deliberate")})
    out = reconcile(state, [finding], today=TODAY)

    assert out.findings == ()
    assert len(out.suppressed) == 1


def test_a_suppression_expires() -> None:
    finding = _finding()
    state = State(suppressed={finding.id: Suppression(reason="for now", until="2026-09-20")})
    out = reconcile(state, [finding], today=TODAY)

    assert len(out.findings) == 1
    assert out.suppressed == ()


def test_a_suppression_expiring_today_still_applies() -> None:
    finding = _finding()
    state = State(suppressed={finding.id: Suppression(reason="x", until="2026-09-21")})

    assert len(reconcile(state, [finding], today=TODAY).suppressed) == 1


def test_an_unparseable_expiry_expires_rather_than_lasting_for_ever() -> None:
    """A typo must not silently suppress a finding permanently."""
    finding = _finding()
    state = State(suppressed={finding.id: Suppression(reason="x", until="next tuesday")})

    assert len(reconcile(state, [finding], today=TODAY).findings) == 1


def test_a_suppressed_finding_is_still_found_so_is_not_resolved() -> None:
    finding = _finding()
    state = State(
        findings=_seen(finding), suppressed={finding.id: Suppression(reason="deliberate")}
    )
    out = reconcile(state, [finding], today=TODAY)

    assert out.resolved == ()
    assert finding.id in out.state.findings


def test_expired_and_orphaned_suppressions_are_dropped_from_state() -> None:
    """Otherwise the document accumulates rules for findings that no longer exist."""
    finding = _finding()
    state = State(
        suppressed={
            finding.id: Suppression(reason="keep"),
            "deadbeefcafe": Suppression(reason="orphan, no such finding"),
        }
    )
    out = reconcile(state, [finding], today=TODAY)

    assert list(out.state.suppressed) == [finding.id]


# --- the document -----------------------------------------------------------


def test_the_document_round_trips() -> None:
    finding = _finding()
    original = reconcile(
        State(suppressed={finding.id: Suppression(reason="r", until="2027-01-01")}),
        [finding],
        today=TODAY,
    ).state

    assert State.from_json(original.to_json()) == original


def test_a_document_from_the_future_is_refused_rather_than_overwritten() -> None:
    """A newer deployment's document must not be silently discarded by an older one."""
    raw = json.dumps({"version": state_module.SCHEMA_VERSION + 1, "findings": {}})

    try:
        State.from_json(raw)
    except ValueError as exc:
        assert "Refusing" in str(exc)
    else:
        raise AssertionError("expected a refusal")


def test_a_hand_edited_document_missing_fields_still_loads() -> None:
    raw = json.dumps({"findings": {"abc": {"first_seen": "2026-01-01"}}})
    loaded = State.from_json(raw)

    assert loaded.findings["abc"].first_seen == "2026-01-01"
    assert loaded.findings["abc"].repo == ""


def test_an_entry_with_no_first_seen_is_discarded() -> None:
    loaded = State.from_json(json.dumps({"findings": {"abc": {"repo": "r"}}}))

    assert loaded.findings == {}


# --- degradation ------------------------------------------------------------


def test_no_storage_configured_means_everything_is_new() -> None:
    """`conftest` configures none, which is also the local default."""
    assert state_module.load() == State()


def test_saving_without_storage_reports_that_it_did_not() -> None:
    """The operator commands rely on this being honest."""
    assert state_module.save(State()) is False
