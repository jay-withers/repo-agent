"""The mailer's refusals."""

from __future__ import annotations

from repoagent import mailer
from repoagent import settings as settings_module
from tests.helpers import json_client


def test_absent_recipient_is_skipped_not_failed() -> None:
    assert mailer.send("s", "<p>h</p>", "t").status == "skipped"


def test_non_ascii_recipient_is_refused_before_the_request(monkeypatch) -> None:
    """Resend rejects these with a 422 that names the address, not the character.

    market-agent lost a day's summary to a recipient stored with curly quotes
    pasted from somewhere that autocorrects — invisible in `az keyvault secret
    show` output. Catching it here makes the reason legible.
    """
    # Escapes rather than literal characters, so the ones under test are
    # visible in the source instead of looking like ordinary quotes.
    curly = "\u2018someone@example.com\u2019"
    monkeypatch.setenv("DIGEST_EMAIL_TO", curly)
    settings_module.optional_secret.cache_clear()

    result = mailer.send("s", "<p>h</p>", "t", client=json_client({"id": "x"}))

    assert result.status == "failed"
    assert "ASCII" in (result.error or "")


def test_a_refused_recipient_is_described_not_echoed(monkeypatch, caplog) -> None:
    """The address lives in Key Vault to stay out of anything shared.

    The log and the result both reach the shared Log Analytics workspace, so
    they name which recipient and which character, never the address itself.
    """
    monkeypatch.setenv("DIGEST_EMAIL_TO", "ok@example.com, \u2018someone@example.com\u2019")
    settings_module.optional_secret.cache_clear()

    result = mailer.send("s", "<p>h</p>", "t", client=json_client({"id": "x"}))

    assert result.error == "recipient is not plain ASCII: recipient 2 of 2 contains U+2018"
    assert "someone@example.com" not in caplog.text
    assert "U+2018" in caplog.text


def test_a_mail_failure_is_reported_not_raised(monkeypatch) -> None:
    monkeypatch.setenv("DIGEST_EMAIL_TO", "someone@example.com")
    settings_module.optional_secret.cache_clear()

    result = mailer.send("s", "<p>h</p>", "t", client=json_client({"message": "nope"}, status=500))

    assert result.status == "failed"
