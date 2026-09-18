"""Shared HTTP plumbing for the outbound APIs.

Copied from jay-withers/market-agent apps/marketagent/src/marketagent/fetch.py.
Re-copy rather than diverge; fix bugs in both.

Two deliberate differences from that original, both because the only API this
agent talks to is GitHub's:

- `RETRYABLE_STATUS` includes **403**. GitHub signals a secondary rate limit
  with 403 rather than 429, so the original's list silently treats "slow down"
  as "you are not allowed" and gives up. Learned the hard way: four search-API
  calls per repository across eleven repositories tripped it within seconds.
- `graphql()` exists and **is** retried, unlike `post_json`. The reason
  `post_json` refuses to retry is that market-agent's only POST submits an
  order, where a duplicate is a real trade. A GraphQL read has no side effect,
  so the same reasoning does not apply.

`httpx`, not `httpx2`: the latter is present because the Anthropic 1.x SDK
depends on it, but depending on another package's transitive dependency is how
you get broken by an upgrade you didn't make.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Generous, because nothing here is user-facing and a slow answer beats no
# answer, but bounded so a hung connection cannot eat the job's
# `replica_timeout_in_seconds`.
TIMEOUT_SECONDS = 30.0

DEFAULT_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0

# Worth retrying: the server said it was briefly unable, or asked us to slow
# down. A 4xx other than these is our fault and will fail identically next time.
#
# 403 is GitHub's secondary rate limit, which is *not* a permissions error
# however much it looks like one. 520-524 are Cloudflare's own inventions,
# meaning the edge answered but its origin did not.
RETRYABLE_STATUS = frozenset({403, 408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524})


class FetchError(RuntimeError):
    """A request that failed after exhausting its retries."""


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """How long to wait, preferring whatever the server asked for.

    GitHub sends `Retry-After` on a secondary rate limit and
    `x-ratelimit-reset` on a primary one. Guessing when the server has already
    said is how you get banned for longer.
    """
    if response is not None:
        retry_after = response.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            # Bounded: a server asking for an hour is better reported as a
            # failed run than slept through inside a job with a timeout.
            return min(float(retry_after), 60.0)

    # Linear rather than exponential: three attempts a couple of seconds apart
    # covers a blip, and anything longer is better reported as a failure.
    return BACKOFF_SECONDS * attempt


def get_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    client: httpx.Client | None = None,
    attempts: int = DEFAULT_ATTEMPTS,
) -> Any:
    """GET `url` and return the decoded JSON, retrying transient failures.

    `client` is injectable so tests can supply a `MockTransport` and so a
    caller making many requests can reuse one connection pool.
    """
    owned = client is None
    session = client or httpx.Client(timeout=TIMEOUT_SECONDS)
    try:
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            response: httpx.Response | None = None
            try:
                response = session.get(url, headers=headers, params=params)
                if response.status_code in RETRYABLE_STATUS:
                    raise FetchError(f"{response.status_code} from {url}")
                response.raise_for_status()
                return response.json()
            except (httpx.TransportError, FetchError) as exc:
                last = exc
                if attempt == attempts:
                    break
                delay = _retry_delay(response, attempt)
                logger.warning(
                    "%s (attempt %d/%d), retrying in %.0fs", exc, attempt, attempts, delay
                )
                time.sleep(delay)
            except httpx.HTTPStatusError as exc:
                # Not retried: a 401 or a 404 will say the same thing again.
                raise FetchError(f"{exc.response.status_code} from {url}") from exc

        raise FetchError(f"{url} failed after {attempts} attempts: {last}") from last
    finally:
        if owned:
            session.close()


def post_json(
    url: str,
    *,
    body: dict[str, Any],
    headers: dict[str, str] | None = None,
    client: httpx.Client | None = None,
) -> Any:
    """POST `body` as JSON and return the decoded response. **Never retried.**

    Kept un-retried from the original even though this agent's only POST is an
    email: a mail provider that executed before failing to answer would send
    twice, and a duplicate digest is a worse outcome than a missing one — the
    findings are all still true next week.
    """
    owned = client is None
    session = client or httpx.Client(timeout=TIMEOUT_SECONDS)
    try:
        response = session.post(url, json=body, headers=headers)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:200]
        raise FetchError(f"{exc.response.status_code} from {url}: {detail}") from exc
    except httpx.TransportError as exc:
        raise FetchError(f"{url} failed: {exc}") from exc
    finally:
        if owned:
            session.close()


def graphql(
    url: str,
    *,
    query: str,
    variables: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    client: httpx.Client | None = None,
    attempts: int = DEFAULT_ATTEMPTS,
) -> Any:
    """POST a GraphQL query and return its `data`, retrying transient failures.

    Retried despite being a POST because a GraphQL read has no side effect —
    see the module docstring.

    GraphQL reports errors in a 200 body rather than as a status code, so a
    caller checking only the status would read a failure as an empty result.
    """
    owned = client is None
    session = client or httpx.Client(timeout=TIMEOUT_SECONDS)
    body = {"query": query, "variables": variables or {}}
    try:
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            response: httpx.Response | None = None
            try:
                response = session.post(url, json=body, headers=headers)
                if response.status_code in RETRYABLE_STATUS:
                    raise FetchError(f"{response.status_code} from {url}")
                response.raise_for_status()
                payload = response.json()
                if payload.get("errors"):
                    messages = "; ".join(str(e.get("message", e)) for e in payload["errors"][:3])
                    raise FetchError(f"graphql errors from {url}: {messages}")
                return payload.get("data")
            except (httpx.TransportError, FetchError) as exc:
                last = exc
                if attempt == attempts:
                    break
                delay = _retry_delay(response, attempt)
                logger.warning(
                    "%s (attempt %d/%d), retrying in %.0fs", exc, attempt, attempts, delay
                )
                time.sleep(delay)
            except httpx.HTTPStatusError as exc:
                raise FetchError(f"{exc.response.status_code} from {url}") from exc

        raise FetchError(f"{url} failed after {attempts} attempts: {last}") from last
    finally:
        if owned:
            session.close()
