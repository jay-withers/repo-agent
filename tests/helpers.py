"""MockTransport builders.

Copied from jay-withers/market-agent apps/marketagent/tests/helpers.py.
Re-copy rather than diverge.

`tests/` is a package, so these import as `tests.helpers` — a bare
`from helpers import ...` does not resolve.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx


def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    """A client whose every request is answered by `handler`."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def json_client(
    payload: Any,
    status: int = 200,
    capture: list[httpx.Request] | None = None,
) -> httpx.Client:
    """A client answering every request with the same JSON body."""

    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        return httpx.Response(status, json=payload)

    return mock_client(handler)


def sequence_client(
    responses: list[httpx.Response],
    calls: list[httpx.Request] | None = None,
) -> httpx.Client:
    """A client answering each request with the next response in turn.

    Raises rather than repeating the last response when the list runs out: a
    test that makes more requests than it declared has found something, and
    silently serving a stale body would hide it.
    """
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        if not remaining:
            raise AssertionError(f"unexpected extra request to {request.url}")
        return remaining.pop(0)

    return mock_client(handler)


def route_client(
    routes: dict[str, httpx.Response],
    calls: list[httpx.Request] | None = None,
) -> httpx.Client:
    """A client answering by URL substring match.

    Matching on a substring rather than the full URL keeps a test from
    restating query strings it does not care about.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        for fragment, response in routes.items():
            if fragment in str(request.url):
                return response
        raise AssertionError(f"no route matches {request.url}")

    return mock_client(handler)
