"""The two GitHub-specific deviations from market-agent's fetch.py."""

from __future__ import annotations

import httpx
import pytest

from repoagent import fetch
from tests.helpers import sequence_client


def test_403_is_retried_because_github_rate_limits_with_it() -> None:
    """The reason this module diverges from the original at all.

    GitHub signals a secondary rate limit with 403, not 429. Treating it as a
    permissions error means giving up on a request that would have succeeded a
    second later.
    """
    client = sequence_client(
        [
            httpx.Response(403, json={"message": "secondary rate limit"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    assert fetch.get_json("https://api.github.com/x", client=client) == {"ok": True}


def test_retry_after_is_honoured_over_the_default_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(fetch.time, "sleep", lambda s: slept.append(s))

    client = sequence_client(
        [
            httpx.Response(403, headers={"retry-after": "7"}, json={}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    fetch.get_json("https://api.github.com/x", client=client)

    assert slept == [7.0]


def test_a_wild_retry_after_is_capped() -> None:
    # A server asking for an hour inside a job with a timeout is better
    # reported as a failure than slept through.
    assert fetch._retry_delay(httpx.Response(403, headers={"retry-after": "9999"}), 1) == 60.0


def test_404_is_not_retried() -> None:
    client = sequence_client([httpx.Response(404, json={})])
    with pytest.raises(fetch.FetchError, match="404"):
        fetch.get_json("https://api.github.com/missing", client=client)


def test_graphql_errors_in_a_200_body_are_raised_not_returned() -> None:
    """GraphQL reports failure in the body, with a 200 status.

    A caller checking only the status reads a failed query as an empty result,
    which downstream looks exactly like a repository with nothing in it.
    """
    client = sequence_client(
        [
            httpx.Response(200, json={"errors": [{"message": "Bad credentials"}]}),
            httpx.Response(200, json={"errors": [{"message": "Bad credentials"}]}),
            httpx.Response(200, json={"errors": [{"message": "Bad credentials"}]}),
        ]
    )
    with pytest.raises(fetch.FetchError, match="Bad credentials"):
        fetch.graphql("https://api.github.com/graphql", query="{ viewer { login } }", client=client)


def test_graphql_returns_data_on_success() -> None:
    client = sequence_client([httpx.Response(200, json={"data": {"viewer": {"login": "jay"}}})])
    result = fetch.graphql(
        "https://api.github.com/graphql", query="{ viewer { login } }", client=client
    )
    assert result == {"viewer": {"login": "jay"}}
