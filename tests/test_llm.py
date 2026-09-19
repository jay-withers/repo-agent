"""Triage, and the three defences that keep the model from establishing facts.

The model may reorder the digest and write a paragraph. It may not invent a
finding, delete one, or fail the run. Each of those is a test here.
"""

from __future__ import annotations

import json

import httpx
import pytest

from repoagent import llm
from repoagent import settings as settings_module
from repoagent.models import Finding
from tests.factories import snapshot
from tests.helpers import json_client, mock_client

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
    findings, summary, themes = llm.triage(list(_FINDINGS), _REPOS)

    assert list(findings) == _FINDINGS
    assert summary == ""
    assert themes == ()


def test_no_findings_makes_no_call(api_key: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request to {request.url}")

    assert llm.triage([], _REPOS, client=mock_client(handler)) == ((), "", ())


def test_the_model_reorders_the_findings(api_key: None) -> None:
    ids = [f.id for f in _FINDINGS]
    client = json_client(
        _reply(
            {"summary": "Fix a first.", "order": [ids[2], ids[0], ids[1]], "themes": ["renovate"]}
        )
    )

    findings, summary, themes = llm.triage(list(_FINDINGS), _REPOS, client=client)

    assert [f.id for f in findings] == [ids[2], ids[0], ids[1]]
    assert summary == "Fix a first."
    assert themes == ("renovate",)


def test_invented_finding_ids_are_dropped(api_key: None) -> None:
    ids = [f.id for f in _FINDINGS]
    client = json_client(_reply({"summary": "", "order": ["deadbeefcafe", *ids], "themes": []}))

    findings, _, _ = llm.triage(list(_FINDINGS), _REPOS, client=client)

    assert [f.id for f in findings] == ids


def test_findings_the_model_omits_are_appended_not_lost(api_key: None) -> None:
    """The set that goes in is always the set that comes out."""
    ids = [f.id for f in _FINDINGS]
    client = json_client(_reply({"summary": "", "order": [ids[1]], "themes": []}))

    findings, _, _ = llm.triage(list(_FINDINGS), _REPOS, client=client)

    assert [f.id for f in findings] == [ids[1], ids[0], ids[2]]


def test_a_duplicated_id_is_only_rendered_once(api_key: None) -> None:
    ids = [f.id for f in _FINDINGS]
    client = json_client(
        _reply({"summary": "", "order": [ids[0], ids[0], ids[1], ids[2]], "themes": []})
    )

    findings, _, _ = llm.triage(list(_FINDINGS), _REPOS, client=client)

    assert [f.id for f in findings] == ids


def test_a_failed_call_costs_the_commentary_not_the_digest(api_key: None) -> None:
    client = json_client({"error": "insufficient balance"}, status=402)

    findings, summary, _ = llm.triage(list(_FINDINGS), _REPOS, client=client)

    assert list(findings) == _FINDINGS
    assert summary == ""


def test_a_reply_that_is_not_json_costs_the_commentary_not_the_digest(api_key: None) -> None:
    client = json_client({"choices": [{"message": {"content": "Sure! Here you go:"}}]})

    findings, summary, _ = llm.triage(list(_FINDINGS), _REPOS, client=client)

    assert list(findings) == _FINDINGS
    assert summary == ""


def test_a_reply_missing_the_choices_key_is_survivable(api_key: None) -> None:
    findings, summary, _ = llm.triage(list(_FINDINGS), _REPOS, client=json_client({}))

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
    assert body["model"] == "deepseek-chat"
    assert body["response_format"] == {"type": "json_object"}
    assert calls[0].headers["authorization"] == "Bearer sk-test"
