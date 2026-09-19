"""Secret resolution: the environment, then `.env`, then Key Vault.

The `.env` step existed in every document describing this project long before it
existed in the code. `SettingsConfigDict(env_file=".env")` loads that file into
the `Settings` class and never into `os.environ`, so a secret written there was
silently ignored — `make run` only ever worked for people who exported the
variables instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repoagent import settings as settings_module
from repoagent.settings import dotenv, optional_secret, secret


def _write_dotenv(body: str) -> None:
    """Write a `.env` into the test's working directory and re-read it."""
    Path(".env").write_text(body)
    dotenv.cache_clear()
    settings_module.settings.cache_clear()


def test_tests_cannot_see_the_repositorys_own_dotenv() -> None:
    """The isolation `conftest.py` provides, asserted rather than assumed.

    Deleting `KEY_VAULT_URI` never stopped `.env` being read, so a developer with
    one on disk had a different test suite from CI.
    """
    assert dotenv() == {}
    assert settings_module.settings().key_vault_uri == ""


def test_a_secret_in_dotenv_is_read() -> None:
    _write_dotenv("DEEPSEEK_API_KEY=sk-from-file\n")

    assert optional_secret("DEEPSEEK-API-KEY") == "sk-from-file"


def test_the_real_environment_beats_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """So `FOO=bar make run` overrides a checked-out file rather than losing to it."""
    _write_dotenv("DEEPSEEK_API_KEY=sk-from-file\n")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")
    settings_module.optional_secret.cache_clear()

    assert optional_secret("DEEPSEEK-API-KEY") == "sk-from-env"


def test_a_quoted_pem_keeps_its_newlines(
    monkeypatch: pytest.MonkeyPatch, rsa_private_key: str
) -> None:
    """The case that rules out a hand-rolled parser.

    A PEM's newlines only survive `.env` as `\\n` inside quotes, and a
    `split("=")` mangles them into a key that fails to parse with a message about
    the header — the failure `github/auth.py` already warns about.

    Uses the fixture's in-process key rather than a literal: a PEM in a public
    repository is a finding in someone else's scanner even when it unlocks
    nothing, and gitleaks rejects the commit. `conftest` exports that same key,
    so it has to be removed for `.env` to be the only source — which is itself
    the precedence rule working.
    """
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY")
    _write_dotenv(f'GITHUB_APP_PRIVATE_KEY="{rsa_private_key.replace(chr(10), chr(92) + "n")}"\n')
    settings_module.secret.cache_clear()

    assert secret("GITHUB-APP-PRIVATE-KEY") == rsa_private_key


def test_the_hyphen_to_underscore_mapping_applies_to_dotenv_too() -> None:
    """Key Vault forbids underscores, environment variables conventionally forbid hyphens."""
    _write_dotenv("DIGEST_EMAIL_TO=someone@example.com\n")

    assert optional_secret("DIGEST-EMAIL-TO") == "someone@example.com"


def test_no_dotenv_file_is_not_an_error() -> None:
    """The deployed image has none, and reads everything from Key Vault."""
    assert not Path(".env").exists()
    assert dotenv() == {}


def test_a_missing_secret_fails_loudly_rather_than_reaching_a_vault() -> None:
    """What the empty KEY_VAULT_URI buys: no test can make a real Key Vault call."""
    with pytest.raises(RuntimeError, match="no KEY_VAULT_URI is configured"):
        secret("NOT-A-SECRET-ANYONE-SET")


def test_an_absent_optional_secret_is_none_not_an_error() -> None:
    assert optional_secret("NOT-A-SECRET-ANYONE-SET") is None
