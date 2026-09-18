"""Configuration and secret resolution.

Copied from jay-withers/market-agent apps/marketagent/src/marketagent/settings.py
and trimmed: the database, broker and API fields have no counterpart here. The
secret-resolution machinery below is unchanged — re-copy rather than diverge.

Every secret is read from an environment variable first and only then from Key
Vault. That ordering is what makes `repoagent render` work against a local
`.env` with no Azure involved at all, and it is why Terraform does not manage
Container Apps Key Vault references: a revision carrying one hard-fails if the
secret is absent, whereas this resolves at runtime and simply reports what is
missing.

The name mapping is mechanical: `secret("GITHUB-APP-ID")` reads
`$GITHUB_APP_ID`, falling back to the Key Vault secret named `GITHUB-APP-ID`.
Key Vault forbids underscores in names, environment variables conventionally
forbid hyphens, so one of the two has to be rewritten.
"""

from __future__ import annotations

import os
import time
from functools import cache, lru_cache
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Non-secret configuration. Anything in this class is safe in a log line."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "dev"

    key_vault_uri: str = ""
    # The managed identity's client id. Empty locally, which is the signal to
    # fall back to DefaultAzureCredential.
    azure_client_id: str = ""
    # A pre-fetched Key Vault access token, for a container that has no `az`.
    # Short-lived and Key Vault-scoped, which is why this is preferable to
    # writing API keys into a .env. See StaticTokenCredential.
    azure_keyvault_token: str = ""

    applicationinsights_connection_string: str = ""

    # Overridable so tests can point at a mock host, and so a GitHub Enterprise
    # instance would need configuration rather than a code change.
    github_api_url: str = "https://api.github.com"
    github_graphql_url: str = "https://api.github.com/graphql"

    # Resend refuses a `from` on an unverified domain, so the default is their
    # shared testing sender — which only delivers to the address that owns the
    # Resend account. A real domain replaces it later.
    digest_email_from: str = "repo-agent <onboarding@resend.dev>"

    # The *recipient* is deliberately not here. It lives in Key Vault, read via
    # optional_secret("DIGEST-EMAIL-TO"), for two reasons: this repository is
    # public, so an address in it would be committed permanently; and a value
    # read at runtime can be changed with `az keyvault secret set` and picked up
    # by the next run, with no new revision. Absent means build the digest and
    # send nothing, which is the right default for development.

    log_level: str = Field(default="INFO")


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()


@cache
def secret(name: str) -> str:
    """Resolve a secret by its hyphenated Key Vault name.

    Cached: this process reads each secret once and then exits, so the cache is
    about not paying for the same round trip twice within a run rather than
    about long-lived reuse.
    """
    from_env = os.environ.get(name.replace("-", "_").upper())
    if from_env:
        return from_env

    uri = settings().key_vault_uri
    if not uri:
        raise RuntimeError(
            f"{name} is not set and no KEY_VAULT_URI is configured. "
            f"Set ${name.replace('-', '_').upper()} locally, or "
            f"`az keyvault secret set --name {name}` for a deployed environment."
        )

    # Imported here rather than at module scope so the models, the checks and
    # the tests never need the Azure SDK installed or a credential present.
    from azure.keyvault.secrets import SecretClient

    client = SecretClient(vault_url=uri, credential=credential())
    return client.get_secret(name).value or ""


@cache
def optional_secret(name: str) -> str | None:
    """Resolve a secret that is allowed not to exist, returning None if it does not.

    For values that switch a behaviour on rather than being required to run —
    the digest's recipient is the case this exists for. `secret()` raising is
    right for a credential the job cannot work without, and wrong for a setting
    whose absence means "don't do that bit".

    **Absence is not the same as failure.** A missing env var, no vault
    configured, or a secret that is not in the vault all return None; an
    authentication or network error propagates, because "the credential is
    broken" must not look like "no recipient configured".
    """
    from_env = os.environ.get(name.replace("-", "_").upper())
    if from_env:
        return from_env

    uri = settings().key_vault_uri
    if not uri:
        return None

    from azure.core.exceptions import ResourceNotFoundError
    from azure.keyvault.secrets import SecretClient

    client = SecretClient(vault_url=uri, credential=credential())
    try:
        return client.get_secret(name).value or None
    except ResourceNotFoundError:
        return None


# The scope a Key Vault data-plane token is issued for. `az` calls the same
# thing `--resource https://vault.azure.net`.
KEY_VAULT_SCOPE = "https://vault.azure.net/.default"


class StaticTokenCredential:
    """A credential wrapping one pre-fetched access token.

    Exists for the containerised local loop. The application image carries no
    `az`, so `DefaultAzureCredential` has nothing to fall back to and a
    container cannot reach Key Vault on its own — the alternative was writing
    the GitHub App private key to a `.env` in plaintext. Passing in a token
    instead is strictly better: it expires in about an hour, it is scoped to
    Key Vault alone, and no long-lived secret touches the filesystem.

    Never used in Azure, where the managed identity is available directly.
    """

    def __init__(self, token: str, scope: str = KEY_VAULT_SCOPE) -> None:
        self._token = token
        self._scope = scope
        # az does not report the expiry in a form worth parsing here, and the
        # SDK only uses this to decide whether to refresh — which this
        # credential cannot do. An hour matches the real lifetime; an expired
        # token then fails as a 401 from Key Vault, which is the honest outcome.
        self._expires_on = int(time.time()) + 3600

    def _check(self, scopes: tuple[str, ...]) -> None:
        """Refuse a scope this token was not issued for."""
        if scopes and self._scope not in scopes:
            raise ValueError(f"this token is scoped to {self._scope}, not {', '.join(scopes)}")

    def get_token(self, *scopes: str, **_kwargs: Any) -> Any:
        from azure.core.credentials import AccessToken

        self._check(scopes)
        return AccessToken(self._token, self._expires_on)

    def get_token_info(self, *scopes: str, **_kwargs: Any) -> Any:
        """The newer protocol; some SDK versions call this instead."""
        from azure.core.credentials import AccessTokenInfo

        self._check(scopes)
        return AccessTokenInfo(self._token, self._expires_on)


@lru_cache(maxsize=1)
def credential():
    """The credential for Key Vault.

    Three cases, in priority order:

    1. A pre-fetched Key Vault token from the environment — the containerised
       local loop, where there is no `az` to fall back to.
    2. The user-assigned identity, named explicitly. In Azure this must be
       explicit: `DefaultAzureCredential` would find the same identity
       eventually, but a workload with more than one identity attached picks
       unpredictably, and the failure looks like a permissions problem rather
       than a wrong-identity one.
    3. `DefaultAzureCredential`, which picks up a developer's `az login`.
    """
    cfg = settings()

    if cfg.azure_keyvault_token:
        return StaticTokenCredential(cfg.azure_keyvault_token)

    if cfg.azure_client_id:
        from azure.identity import ManagedIdentityCredential

        return ManagedIdentityCredential(client_id=cfg.azure_client_id)

    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()
