"""Test setup: no network, no Azure, no real credentials.

Copied in shape from jay-withers/market-agent apps/marketagent/tests/conftest.py.
The important parts are unchanged and worth restating:

- Every secret is set as an environment variable, so `secret()` resolves from
  the environment and never reaches for a vault.
- **Tests run in an empty temporary directory**, so the developer's own `.env`
  cannot reach them. Both `Settings` and `secret()` resolve `.env` relative to
  the working directory, and deleting an environment variable does *not* stop
  either from reading the file — which quietly defeated the guarantee below for
  anyone who had one.
- **`KEY_VAULT_URI` is set empty rather than deleted**, so a secret this file
  forgot fails loudly rather than falling through to a real Key Vault call. An
  explicit empty value beats a `.env`; an absent one does not.
- All four `lru_cache`s are cleared around every test, because `secret()`,
  `settings()` and `dotenv()` would otherwise leak one test's values into the
  next.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repoagent import settings as settings_module
from repoagent.github import auth


@pytest.fixture(scope="session")
def rsa_private_key() -> str:
    """A throwaway RSA key, generated in process.

    Never a committed PEM, even a throwaway one: this repository is public, and
    a private key in its history is a finding in someone else's scanner even
    when it unlocks nothing.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture(autouse=True)
def fake_secrets(monkeypatch: pytest.MonkeyPatch, rsa_private_key: str, tmp_path: Path) -> None:
    # First, before anything reads a setting: somewhere with no `.env` in it.
    monkeypatch.chdir(tmp_path)

    monkeypatch.setenv("GITHUB_APP_ID", "123456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", rsa_private_key)
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.delenv("DIGEST_EMAIL_TO", raising=False)
    # Absent by default, so triage is off unless a test switches it on. A test
    # that accidentally enabled it would reach api.deepseek.com for real.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("KEY_VAULT_URI", "")
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("IMAGE_TAG", raising=False)

    _clear_caches()
    yield
    _clear_caches()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries are tested for behaviour, not for wall-clock patience."""
    monkeypatch.setattr("repoagent.fetch.time.sleep", lambda _seconds: None)


def _clear_caches() -> None:
    settings_module.dotenv.cache_clear()
    settings_module.settings.cache_clear()
    settings_module.secret.cache_clear()
    settings_module.optional_secret.cache_clear()
    settings_module.credential.cache_clear()
    auth.reset_cache()
