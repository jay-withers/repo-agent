"""The GraphQL detail query and the merge that folds it into a snapshot."""

from __future__ import annotations

import json

import httpx

from repoagent.github import client as github_client
from repoagent.github import parse
from tests.factories import snapshot
from tests.helpers import mock_client

# Most specific first: `/app/installations` is a prefix of the token URL, so the
# order here is what decides which one a substring match wins.
_AUTH_ROUTES = {
    "/app/installations/1/access_tokens": httpx.Response(
        201, json={"token": "ghs_test", "expires_at": "2099-01-01T00:00:00Z"}
    ),
    "/app/installations": httpx.Response(200, json=[{"id": 1}]),
}


def _detail(**overrides: object) -> dict:
    base: dict = {
        "nameWithOwner": "jay-withers/widget",
        "defaultBranchRef": {"name": "main"},
        "renovate0": {"text": '{"extends": ["config:recommended"]}'},
        "readme": {"text": "# widget"},
        "dockerfile": None,
        "workflows": {
            "entries": [
                {"name": "ci.yml", "object": {"text": "on: [push]"}},
                {"name": "README.md", "object": {"text": "not a workflow"}},
            ]
        },
        "pullRequests": {"totalCount": 0, "nodes": []},
        "closedPullRequests": {"totalCount": 0, "nodes": []},
        "releases": {"nodes": [{"tagName": "v1.2.3", "publishedAt": "2026-09-01T00:00:00Z"}]},
    }
    return {**base, **overrides}


def test_merge_folds_detail_into_the_snapshot() -> None:
    merged = parse.merge_detail(snapshot(renovate_config=None, workflows=()), _detail())

    assert merged.renovate_config == '{"extends": ["config:recommended"]}'
    assert merged.renovate_config_path == "renovate.json"
    assert merged.last_release == "v1.2.3"


def test_merge_keeps_only_workflow_files() -> None:
    """`.github/workflows` legitimately holds files Actions ignores."""
    merged = parse.merge_detail(snapshot(), _detail())

    assert merged.workflow_names == ("ci.yml",)
    assert merged.workflows[0].text == "on: [push]"


def test_merge_without_detail_returns_the_snapshot_unchanged() -> None:
    """A repository the batch could not resolve still reaches the checks."""
    original = snapshot()

    assert parse.merge_detail(original, None) is original


def test_renovate_config_precedence_follows_renovates_own() -> None:
    """A repository with two configs is read by Renovate the same way."""
    detail = _detail(renovate0=None, renovate2={"text": "{}"})
    merged = parse.merge_detail(snapshot(), detail)

    assert merged.renovate_config_path == ".github/renovate.json"


def test_a_binary_blob_reads_as_absent() -> None:
    merged = parse.merge_detail(snapshot(), _detail(readme={"text": None}))

    assert merged.readme is None
    assert merged.has_readme is False


def test_a_pull_request_with_a_deleted_author_does_not_break_the_run() -> None:
    detail = _detail(
        pullRequests={
            "totalCount": 1,
            "nodes": [
                {"number": 1, "title": "x", "author": None, "createdAt": "2026-01-01T00:00:00Z"}
            ],
        }
    )
    merged = parse.merge_detail(snapshot(), detail)

    assert merged.open_prs[0].author == "unknown"


def test_detail_query_batches_repositories() -> None:
    """Node-limit scoring is on the whole document, so batches stay bounded."""
    calls: list[httpx.Request] = []
    names = [f"jay-withers/repo{i}" for i in range(25)]

    def handler(request: httpx.Request) -> httpx.Response:
        for fragment, response in _AUTH_ROUTES.items():
            if fragment in str(request.url):
                return response
        calls.append(request)
        body = json.loads(request.content)
        aliases = {k: _detail() for k in body["variables"] if k.startswith("o")}
        return httpx.Response(200, json={"data": {f"r{i}": _detail() for i in range(len(aliases))}})

    details = github_client.repo_details(names, client=mock_client(handler))

    # 25 repositories at a batch size of 10 is three requests, not one.
    assert len(calls) == 3
    assert len(details) == 25


def test_repository_names_are_sent_as_variables_not_interpolated() -> None:
    """A repository called `") { ... }` is a valid GitHub name."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        for fragment, response in _AUTH_ROUTES.items():
            if fragment in str(request.url):
                return response
        calls.append(request)
        return httpx.Response(200, json={"data": {"r0": _detail()}})

    github_client.repo_details(['jay-withers/") { evil }'], client=mock_client(handler))

    body = json.loads(calls[0].content)
    assert body["variables"]["n0"] == '") { evil }'
    assert "evil" not in body["query"]


def test_a_null_alias_is_skipped_rather_than_failing_the_batch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        for fragment, response in _AUTH_ROUTES.items():
            if fragment in str(request.url):
                return response
        return httpx.Response(200, json={"data": {"r0": None, "r1": _detail()}})

    details = github_client.repo_details(
        ["jay-withers/gone", "jay-withers/widget"], client=mock_client(handler)
    )

    assert list(details) == ["jay-withers/widget"]


def test_a_merged_renovate_pr_proves_renovate_has_run() -> None:
    merged = parse.merge_detail(
        snapshot(renovate_pr_ever=None),
        _detail(
            closedPullRequests={
                "totalCount": 2,
                "nodes": [{"author": {"login": "jay-withers"}}, {"author": {"login": "renovate"}}],
            }
        ),
    )

    assert merged.renovate_pr_ever is True


def test_an_open_renovate_pr_proves_it_too() -> None:
    """The history page is not the only evidence — what is open counts."""
    merged = parse.merge_detail(
        snapshot(renovate_pr_ever=None),
        _detail(
            pullRequests={
                "totalCount": 1,
                "nodes": [
                    {
                        "number": 1,
                        "title": "chore(deps): update httpx",
                        "url": "u",
                        "isDraft": False,
                        "createdAt": "2026-09-01T00:00:00Z",
                        "author": {"login": "renovate[bot]"},
                    }
                ],
            }
        ),
    )

    assert merged.renovate_pr_ever is True


def test_a_complete_history_with_no_renovate_pr_proves_the_negative() -> None:
    merged = parse.merge_detail(
        snapshot(renovate_pr_ever=None),
        _detail(
            closedPullRequests={
                "totalCount": 1,
                "nodes": [{"author": {"login": "jay-withers"}}],
            }
        ),
    )

    assert merged.renovate_pr_ever is False


def test_a_history_longer_than_the_page_answers_nothing() -> None:
    """Absence is only evidence when the page holds everything there is.

    A busy repository fills this page with human pull requests, and calling
    that "Renovate has never run" would report the estate's most active
    repositories as its deadest.
    """
    merged = parse.merge_detail(
        snapshot(renovate_pr_ever=None),
        _detail(
            closedPullRequests={
                "totalCount": 400,
                "nodes": [{"author": {"login": "jay-withers"}}],
            }
        ),
    )

    assert merged.renovate_pr_ever is None


def test_a_failed_detail_query_leaves_the_question_unanswered() -> None:
    """The `render` fallback path must not invent a high-severity finding."""
    assert parse.merge_detail(snapshot(renovate_pr_ever=None), None).renovate_pr_ever is None


def test_the_detail_query_asks_for_closed_pull_request_history() -> None:
    """Pinned: without this the check can only ever answer None."""
    assert "closedPullRequests" in github_client._REPO_FRAGMENT
    assert "states: [MERGED, CLOSED]" in github_client._REPO_FRAGMENT
