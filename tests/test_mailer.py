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


def test_a_mail_failure_is_reported_not_raised(monkeypatch) -> None:
    monkeypatch.setenv("DIGEST_EMAIL_TO", "someone@example.com")
    settings_module.optional_secret.cache_clear()

    result = mailer.send("s", "<p>h</p>", "t", client=json_client({"message": "nope"}, status=500))

    assert result.status == "failed"
