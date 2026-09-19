"""Triage, and the three defences that keep the model from establishing facts.

The model may reorder the digest and write a paragraph. It may not invent a
finding, delete one, or fail the run. Each of those is a test here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from repoagent import llm
from repoagent import settings as settings_module
from repoagent.models import Finding
from tests.factories import snapshot
from tests.helpers import json_client, mock_client, route_client

_FINDINGS = [
    Finding(
        repo="jay-withers/a",
        check="renovate.missing_config",
        severity="high",
        title="no Renovate configuration",
        detail="...",
    ),
    Finding(
        repo="jay-withers/b",
        check="hygiene.no_readme",
        severity="medium",
        title="no README",
        detail="...",
    ),
    Finding(
        repo="jay-withers/c",
        check="hygiene.no_description",
        severity="low",
        title="no description",
        detail="...",
    ),
]

_REPOS = (
    snapshot(name="a", full_name="jay-withers/a"),
    snapshot(name="b", full_name="jay-withers/b"),
    snapshot(name="c", full_name="jay-withers/c"),
)


@pytest.fixture
def api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    settings_module.optional_secret.cache_clear()


def _reply(body: dict) -> dict:
    """A DeepSeek chat completion carrying `body` as its JSON content."""
    return {
        "choices": [{"message": {"content": json.dumps(body)}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


def test_no_api_key_passes_findings_through_untouched() -> None:
    """What makes `repoagent render` work on a laptop with no DeepSeek account."""
    findings, summary, themes, _ = llm.triage(list(_FINDINGS), _REPOS)

    assert list(findings) == _FINDINGS
    assert summary == ""
    assert themes == ()


def test_no_findings_makes_no_call(api_key: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request to {request.url}")

    assert llm.triage([], _REPOS, client=mock_client(handler)) == ((), "", (), None)


def test_the_model_reorders_the_findings(api_key: None) -> None:
    ids = [f.id for f in _FINDINGS]
    client = json_client(
        _reply(
            {"summary": "Fix a first.", "order": [ids[2], ids[0], ids[1]], "themes": ["renovate"]}
        )
    )

    findings, summary, themes, _ = llm.triage(list(_FINDINGS), _REPOS, client=client)

    assert [f.id for f in findings] == [ids[2], ids[0], ids[1]]
    assert summary == "Fix a first."
    assert themes == ("renovate",)


def test_invented_finding_ids_are_dropped(api_key: None) -> None:
    ids = [f.id for f in _FINDINGS]
    client = json_client(_reply({"summary": "", "order": ["deadbeefcafe", *ids], "themes": []}))

    findings, _, _, _ = llm.triage(list(_FINDINGS), _REPOS, client=client)

    assert [f.id for f in findings] == ids


def test_findings_the_model_omits_are_appended_not_lost(api_key: None) -> None:
    """The set that goes in is always the set that comes out."""
    ids = [f.id for f in _FINDINGS]
    client = json_client(_reply({"summary": "", "order": [ids[1]], "themes": []}))

    findings, _, _, _ = llm.triage(list(_FINDINGS), _REPOS, client=client)

    assert [f.id for f in findings] == [ids[1], ids[0], ids[2]]


def test_a_duplicated_id_is_only_rendered_once(api_key: None) -> None:
    ids = [f.id for f in _FINDINGS]
    client = json_client(
        _reply({"summary": "", "order": [ids[0], ids[0], ids[1], ids[2]], "themes": []})
    )

    findings, _, _, _ = llm.triage(list(_FINDINGS), _REPOS, client=client)

    assert [f.id for f in findings] == ids


def test_a_failed_call_costs_the_commentary_not_the_digest(api_key: None) -> None:
    client = json_client({"error": "insufficient balance"}, status=402)

    findings, summary, _, _ = llm.triage(list(_FINDINGS), _REPOS, client=client)

    assert list(findings) == _FINDINGS
    assert summary == ""


def test_a_reply_that_is_not_json_costs_the_commentary_not_the_digest(api_key: None) -> None:
    client = json_client({"choices": [{"message": {"content": "Sure! Here you go:"}}]})

    findings, summary, _, _ = llm.triage(list(_FINDINGS), _REPOS, client=client)

    assert list(findings) == _FINDINGS
    assert summary == ""


def test_a_reply_missing_the_choices_key_is_survivable(api_key: None) -> None:
    findings, summary, _, _ = llm.triage(list(_FINDINGS), _REPOS, client=json_client({}))

    assert list(findings) == _FINDINGS
    assert summary == ""


def test_the_prompt_carries_only_repositories_that_have_a_finding(api_key: None) -> None:
    """Context for a clean repository is tokens spent to say nothing is wrong."""
    calls: list[httpx.Request] = []
    repos = (*_REPOS, snapshot(name="clean", full_name="jay-withers/clean"))

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_reply({"summary": "", "order": [], "themes": []}))

    llm.triage(list(_FINDINGS), repos, client=mock_client(handler))

    body = calls[0].content.decode()
    assert "jay-withers/a" in body
    assert "jay-withers/clean" not in body


def test_long_files_are_clipped_before_they_reach_the_prompt(api_key: None) -> None:
    """A privacy control as much as a cost one."""
    calls: list[httpx.Request] = []
    repos = (snapshot(full_name="jay-withers/a", readme="x" * 50_000),)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_reply({"summary": "", "order": [], "themes": []}))

    llm.triage([_FINDINGS[0]], repos, client=mock_client(handler))

    body = json.loads(calls[0].content)
    readme = body["messages"][1]["content"]
    assert "x" * 50_000 not in readme
    assert "[truncated]" in readme


def test_the_request_names_the_configured_model(api_key: None) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_reply({"summary": "", "order": [], "themes": []}))

    llm.triage(list(_FINDINGS), _REPOS, client=mock_client(handler))

    body = json.loads(calls[0].content)
    assert body["model"] == "deepseek-flash"
    assert body["response_format"] == {"type": "json_object"}
    assert calls[0].headers["authorization"] == "Bearer sk-test"


# --- cost and balance -------------------------------------------------------


def _usage_reply(hit: int, miss: int, out: int, model: str = "deepseek-flash") -> dict:
    return {
        "model": model,
        "choices": [
            {"message": {"content": json.dumps({"summary": "", "order": [], "themes": []})}}
        ],
        "usage": {
            "prompt_tokens": hit + miss,
            "prompt_cache_hit_tokens": hit,
            "prompt_cache_miss_tokens": miss,
            "completion_tokens": out,
        },
    }


_BALANCE = {
    "is_available": True,
    "balance_infos": [
        {
            "currency": "USD",
            "total_balance": "9.98",
            "granted_balance": "0.00",
            "topped_up_balance": "9.98",
        },
    ],
}


def _priced_client(reply: dict, balance: dict | None = _BALANCE) -> httpx.Client:
    """Answers the completion, then the balance lookup."""
    routes = {"/chat/completions": httpx.Response(200, json=reply)}
    if balance is not None:
        routes["/user/balance"] = httpx.Response(200, json=balance)
    return route_client(routes)


def test_usage_is_reported_from_the_api_not_estimated(api_key: None) -> None:
    _, _, _, usage = llm.triage(
        list(_FINDINGS), _REPOS, client=_priced_client(_usage_reply(hit=1000, miss=2000, out=150))
    )

    assert usage is not None
    assert usage.cache_hit_tokens == 1000
    assert usage.cache_miss_tokens == 2000
    assert usage.prompt_tokens == 3000
    assert usage.completion_tokens == 150


def test_cost_prices_cache_hits_far_below_misses(api_key: None) -> None:
    """Fifty times cheaper, which is why the two are tracked apart."""
    all_hit = llm._usage(
        {"prompt_cache_hit_tokens": 1_000_000, "prompt_cache_miss_tokens": 0},
        "m",
        now=datetime(2026, 9, 19, 12, 0, tzinfo=UTC),
    )
    all_miss = llm._usage(
        {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 1_000_000},
        "m",
        now=datetime(2026, 9, 19, 12, 0, tzinfo=UTC),
    )

    assert all_hit.cost_usd == pytest.approx(0.003)
    assert all_miss.cost_usd == pytest.approx(0.15)


def test_peak_rates_are_double(api_key: None) -> None:
    args = {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 1_000_000}
    # Saturday noon: off-peak.
    off = llm._usage(args, "m", now=datetime(2026, 9, 19 + 0, 12, 0, tzinfo=UTC))
    # Monday 07:00 UTC, which is when the job's own cron fires.
    peak = llm._usage(args, "m", now=datetime(2026, 9, 21, 7, 0, tzinfo=UTC))

    assert not off.peak
    assert peak.peak
    assert peak.cost_usd == pytest.approx(off.cost_usd * 2)


def test_the_scans_own_schedule_falls_in_a_peak_window() -> None:
    """Monday 07:00 UTC sits inside 06:00-10:00. Worth knowing before moving it."""
    assert llm._is_peak(datetime(2026, 9, 21, 7, 0, tzinfo=UTC))


def test_weekends_are_off_peak() -> None:
    assert not llm._is_peak(datetime(2026, 9, 19 + 1, 7, 0, tzinfo=UTC))  # Sunday


def test_missing_cache_fields_are_counted_as_misses(api_key: None) -> None:
    """Over-estimates rather than under-estimates, which is the safe direction."""
    usage = llm._usage({"prompt_tokens": 500}, "m", now=datetime(2026, 9, 19, 12, 0, tzinfo=UTC))

    assert usage.cache_miss_tokens == 500
    assert usage.cache_hit_tokens == 0


def test_the_balance_is_attached_from_the_api(api_key: None) -> None:
    _, _, _, usage = llm.triage(
        list(_FINDINGS), _REPOS, client=_priced_client(_usage_reply(10, 20, 5))
    )

    assert usage is not None
    assert usage.balance_usd == "9.98"


def test_a_failed_balance_lookup_costs_only_the_balance(api_key: None) -> None:
    client = route_client(
        {
            "/chat/completions": httpx.Response(200, json=_usage_reply(10, 20, 5)),
            "/user/balance": httpx.Response(500, json={"error": "nope"}),
        }
    )

    _, _, _, usage = llm.triage(list(_FINDINGS), _REPOS, client=client)

    assert usage is not None
    assert usage.balance_usd is None
    assert usage.cache_miss_tokens == 20


def test_a_non_usd_balance_is_ignored(api_key: None) -> None:
    """The price table is in USD; a CNY figure beside it would mislead."""
    cny = {"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "70.00"}]}

    _, _, _, usage = llm.triage(
        list(_FINDINGS), _REPOS, client=_priced_client(_usage_reply(10, 20, 5), balance=cny)
    )

    assert usage is not None
    assert usage.balance_usd is None


def test_the_model_recorded_is_the_one_the_api_served(api_key: None) -> None:
    """`deepseek-chat` resolves to something else, and a cost on the wrong model misleads."""
    _, _, _, usage = llm.triage(
        list(_FINDINGS),
        _REPOS,
        client=_priced_client(_usage_reply(10, 20, 5, model="deepseek-flash")),
    )

    assert usage is not None
    assert usage.model == "deepseek-flash"
