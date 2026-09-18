"""Authenticating as a GitHub App.

A GitHub App rather than a personal access token, because a PAT expires (a year
at most), belongs to one person, and carries that person's whole account.
An App's installation token is scoped to the repositories the App is installed
on, gets its own 5,000 requests an hour, and needs no diary entry to rotate.

The flow is two steps and worth stating plainly, because every part of it has a
sharp edge:

1. Sign a short-lived **JWT** with the App's RSA private key. This proves "I am
   this App" and can do almost nothing else.
2. Exchange that JWT for an **installation access token** — `ghs_…`, valid one
   hour — which is what actually reads repositories.

Hand-rolled on PyJWT rather than taking `githubkit` or `PyGithub`: this is
about sixty lines, and neither library would spare us the GraphQL query we hand
write anyway.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ..fetch import get_json, post_json
from ..settings import secret, settings

logger = logging.getLogger(__name__)

# GitHub rejects a JWT whose `exp` is more than 10 minutes out. Nine leaves room
# for the backdated `iat` below without crossing that line.
JWT_LIFETIME_SECONDS = 9 * 60

# GitHub rejects a JWT whose `iat` is in the future by its clock. Backdating a
# minute absorbs ordinary clock skew between this container and GitHub, and is
# what their own documentation recommends.
JWT_BACKDATE_SECONDS = 60

# Re-mint this long before the token actually expires, so a scan that started
# just under the wire does not fail partway through.
TOKEN_REFRESH_MARGIN_SECONDS = 120

# Module-level rather than @lru_cache because expiry has to be *checked*, which
# a cache keyed on the arguments cannot do. Reset in tests.
_cached: tuple[str, datetime] | None = None


def app_jwt(now: float | None = None) -> str:
    """Sign a JWT proving we are the App. Valid for under ten minutes."""
    # Imported here so that importing this module needs neither the crypto
    # stack nor a private key — the same reason settings.py defers its Azure
    # imports.
    import jwt

    issued = int(now if now is not None else time.time())
    payload = {
        "iat": issued - JWT_BACKDATE_SECONDS,
        "exp": issued + JWT_LIFETIME_SECONDS,
        "iss": secret("GITHUB-APP-ID"),
    }
    # The key is a PEM. Stored in Key Vault with `az keyvault secret set
    # --file`, because its newlines matter and a shell-quoted `--value` mangles
    # them into a key that fails to parse with a message about the header.
    return jwt.encode(payload, secret("GITHUB-APP-PRIVATE-KEY"), algorithm="RS256")


def installation_id(jwt_token: str, client: httpx.Client | None = None) -> int:
    """Find the single installation this App has.

    Looked up rather than configured: it is derivable from the App's own
    credentials, and one fewer secret is one fewer thing to rotate or get
    wrong. An App installed in exactly one place — which is the case here — has
    exactly one answer.
    """
    installations = get_json(
        f"{settings().github_api_url}/app/installations",
        headers=_app_headers(jwt_token),
        client=client,
    )
    if not installations:
        raise RuntimeError(
            "this GitHub App has no installations. Install it on the account "
            "whose repositories should be scanned."
        )
    if len(installations) > 1:
        # Not an error: take the first, but say so, because the alternative is
        # silently scanning one account's repositories and not another's.
        logger.warning(
            "App has %d installations; using the first (%s)",
            len(installations),
            installations[0].get("account", {}).get("login", "?"),
        )
    return int(installations[0]["id"])


def token(client: httpx.Client | None = None, now: datetime | None = None) -> str:
    """An installation access token, minted on demand and cached until expiry.

    One scan makes a few dozen requests over a few seconds, so in practice this
    mints once. The caching is for correctness under retry rather than for
    speed.
    """
    global _cached

    moment = now or datetime.now(UTC)
    if _cached is not None:
        cached_token, expires_at = _cached
        if expires_at - timedelta(seconds=TOKEN_REFRESH_MARGIN_SECONDS) > moment:
            return cached_token

    jwt_token = app_jwt()
    payload = post_json(
        f"{settings().github_api_url}/app/installations/"
        f"{installation_id(jwt_token, client=client)}/access_tokens",
        body={},
        headers=_app_headers(jwt_token),
        client=client,
    )

    expires_at = _parse_expiry(payload.get("expires_at"), moment)
    _cached = (payload["token"], expires_at)
    logger.info("minted installation token, expires %s", expires_at.isoformat())
    return payload["token"]


def reset_cache() -> None:
    """Drop the cached token. For tests, and for a long-lived caller."""
    global _cached
    _cached = None


def auth_headers(client: httpx.Client | None = None) -> dict[str, str]:
    """Headers for an ordinary API call as the installation."""
    return {
        "Authorization": f"Bearer {token(client=client)}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _app_headers(jwt_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _parse_expiry(raw: Any, fallback_from: datetime) -> datetime:
    """Read GitHub's `expires_at`, falling back to the documented one hour.

    A token whose expiry we cannot read is still a usable token; treating an
    unparseable timestamp as fatal would fail a run over a field we only use to
    decide whether to re-mint.
    """
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("could not parse token expiry %r, assuming one hour", raw)
    return fallback_from + timedelta(hours=1)
