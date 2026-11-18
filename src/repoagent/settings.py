"""Configuration and secret resolution.

`Settings` is this job's own. Secret resolution comes from jay-withers/appkit
(`appkit.keyvault`), shared with career, finances, gym-log and the other agent;
fix a bug there, once. The wrappers below bind it to these settings and cache
it, so a test can clear them.

Every secret is read from an environment variable first, then `.env`, and only
then from Key Vault. That ordering is what makes a local run work with no
Azure involved, and it is why Terraform manages no Container Apps Key Vault
reference: a revision carrying one hard-fails if the secret is absent, whereas
this resolves at runtime and reports what is missing.

The name mapping is mechanical: `secret("RESEND-API-KEY")` reads
`$RESEND_API_KEY`, then `.env`, then the Key Vault secret named
`RESEND-API-KEY`. Key Vault forbids underscores in names, environment variables
conventionally forbid hyphens, so one of the two has to be rewritten.
"""

from __future__ import annotations

from functools import cache, lru_cache
from typing import Any

from appkit import keyvault

# Re-exported: the tests (and conftest's cache clearing) reach them here.
from appkit.keyvault import KEY_VAULT_SCOPE as KEY_VAULT_SCOPE
from appkit.keyvault import StaticTokenCredential as StaticTokenCredential
from appkit.keyvault import dotenv as dotenv
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

    # DeepSeek's API is OpenAI-compatible, so the triage step is one POST over
    # the existing fetch helpers rather than an SDK. Overridable so tests point
    # at a mock host, and so the same code reaches any compatible endpoint.
    deepseek_api_url: str = "https://api.deepseek.com"
    # `deepseek-flash` rather than `deepseek-v4-pro`: triage is ordering and
    # summarising a list someone else established, not a reasoning problem, and
    # the pro model's thinking tokens bill as output.
    #
    # Named explicitly rather than as `deepseek-chat`, which this was set to
    # first. That name is absent from `GET /models` and from the pricing page,
    # but still resolves — to `deepseek-flash`, as the response's own `model`
    # field reports. An undocumented alias can be withdrawn without notice, and
    # this job would find out on a Monday.
    deepseek_model: str = "deepseek-flash"

    # Repositories carrying any of these topics are skipped entirely — not
    # fetched, not checked, not sent to the model.
    #
    # A topic rather than a list of repository names, because `github-repos`
    # already applies topics from its catalogue, so the exemption is declared
    # where the rest of a repository's configuration is. `no-scan` is the
    # explicit marker; `tutorial` is semantic — teaching material genuinely
    # should not be held to infrastructure standards.
    #
    # Comma-separated rather than a list, because this is injected as an
    # environment variable and pydantic-settings wants JSON for a sequence.
    ignore_topics: str = "no-scan,tutorial"

    # The repository whose Terraform declares every repository in the estate,
    # and the file inside it. Empty switches the "unmanaged repository" check
    # off entirely rather than flagging everything at once.
    catalogue_repo: str = "jay-withers/github-repos"
    catalogue_path: str = "terraform/terraform.tfvars"

    # Blob container holding the scan's history. Empty switches history off
    # entirely — every finding then reports as new, which is the right default
    # locally and what keeps `make run` working with no storage account.
    #
    # Not a secret: it is a URL, and reaching it still needs the managed
    # identity's RBAC grant on the container.
    state_container_url: str = ""

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

    @property
    def ignored_topics(self) -> frozenset[str]:
        """`ignore_topics` as a set, blanks and casing handled."""
        return frozenset(t.strip().lower() for t in self.ignore_topics.split(",") if t.strip())


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()


@cache
def secret(name: str) -> str:
    """Resolve a secret by its hyphenated Key Vault name, raising if it is absent."""
    return keyvault.resolve(name, key_vault_uri=settings().key_vault_uri, credential=credential)


@cache
def optional_secret(name: str) -> str | None:
    """Resolve a secret that is allowed not to exist, returning None if it does not.

    An authentication or network error still raises: "the credential is broken"
    must not look like "not configured".
    """
    return keyvault.resolve_optional(
        name, key_vault_uri=settings().key_vault_uri, credential=credential
    )


@lru_cache(maxsize=1)
def credential() -> Any:
    """The credential for Key Vault and the state blob (`appkit.keyvault.make_credential`)."""
    cfg = settings()
    return keyvault.make_credential(
        keyvault_token=cfg.azure_keyvault_token, client_id=cfg.azure_client_id
    )
