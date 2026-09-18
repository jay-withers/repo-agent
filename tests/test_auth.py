"""GitHub App authentication.

The JWT assertions matter more than they look: GitHub rejects a token whose
`exp` is more than ten minutes out or whose `iat` is in the future by its clock,
and both failures arrive as an unexplained 401.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest

from repoagent.github import auth
from tests.helpers import route_client, sequence_client


def _decode(token: str, key: str) -> dict:
    """Verify the signature and return the claims.

    `verify_exp` is off because these tests sign at a fixed past timestamp in
    order to assert the claim arithmetic exactly. Whether the token has expired
    by the wall clock is not what is under test — the relationship between
    `iat`, `exp` and GitHub's limits is.
    """
    from cryptography.hazmat.primitives import serialization

    private = serialization.load_pem_private_key(key.encode(), password=None)
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return jwt.decode(
        token,
        public_pem,
        algorithms=["RS256"],
        options={"verify_aud": False, "verify_exp": False},
    )


def test_app_jwt_is_signed_rs256_and_within_githubs_bounds(rsa_private_key: str) -> None:
    now = 1_700_000_000
    token = auth.app_jwt(now=now)

    assert jwt.get_unverified_header(token)["alg"] == "RS256"

    claims = _decode(token, rsa_private_key)
    assert claims["iss"] == "123456"
    # Backdated, or GitHub rejects it against its own clock.
    assert claims["iat"] == now - auth.JWT_BACKDATE_SECONDS
    # Inside GitHub's ten-minute ceiling, measured from the backdated iat.
    assert claims["exp"] - claims["iat"] <= 600


def test_installation_id_reads_the_single_installation() -> None:
    client = route_client(
        {"/app/installations": httpx.Response(200, json=[{"id": 42, "account": {"login": "x"}}])}
    )
    assert auth.installation_id("jwt", client=client) == 42


def test_installation_id_refuses_when_the_app_is_installed_nowhere() -> None:
    client = route_client({"/app/installations": httpx.Response(200, json=[])})
    with pytest.raises(RuntimeError, match="no installations"):
        auth.installation_id("jwt", client=client)


def test_token_is_minted_once_and_reused_within_its_life() -> None:
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    calls: list[httpx.Request] = []
    client = route_client(
        {
            "/app/installations/7/access_tokens": httpx.Response(
                201, json={"token": "ghs_first", "expires_at": expires}
            ),
            "/app/installations": httpx.Response(200, json=[{"id": 7}]),
        },
        calls=calls,
    )

    assert auth.token(client=client) == "ghs_first"
    assert auth.token(client=client) == "ghs_first"

    # Two requests for the first mint (list installations, then exchange), and
    # nothing at all for the second call.
    assert len(calls) == 2


def test_token_is_reminted_once_the_cached_one_is_near_expiry() -> None:
    soon = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
    later = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    client = sequence_client(
        [
            httpx.Response(200, json=[{"id": 7}]),
            httpx.Response(201, json={"token": "ghs_first", "expires_at": soon}),
            httpx.Response(200, json=[{"id": 7}]),
            httpx.Response(201, json={"token": "ghs_second", "expires_at": later}),
        ]
    )

    assert auth.token(client=client) == "ghs_first"
    # The cached token expires inside the refresh margin, so it is not reused.
    assert auth.token(client=client) == "ghs_second"


def test_unparseable_expiry_does_not_fail_the_run() -> None:
    client = route_client(
        {
            "/app/installations/7/access_tokens": httpx.Response(
                201, json={"token": "ghs_x", "expires_at": "not a timestamp"}
            ),
            "/app/installations": httpx.Response(200, json=[{"id": 7}]),
        }
    )
    # A token we cannot date is still a usable token.
    assert auth.token(client=client) == "ghs_x"
