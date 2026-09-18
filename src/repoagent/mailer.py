"""The digest email, via Resend.

Copied from jay-withers/market-agent apps/marketagent/src/marketagent/mailer.py
with the secret name and sender changed. Re-copy rather than diverge.

Deliberately thin, and it returns its outcome rather than raising: sending is
the last thing a run does, and a mail provider having a bad minute should not
turn a successful scan into a failed job.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .fetch import FetchError, post_json
from .settings import optional_secret, secret, settings

logger = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"


@dataclass(frozen=True)
class MailResult:
    """The outcome of one send."""

    # sent | failed | skipped.
    status: str
    provider_id: str | None = None
    error: str | None = None


def send(subject: str, html: str, text: str, client: Any = None) -> MailResult:
    """Send the digest, returning what happened rather than raising.

    `skipped` when no recipient is configured, which is the default: a job that
    emails on every run during development is worse than one that has to be
    switched on deliberately.
    """
    cfg = settings()
    # From Key Vault rather than an env var Terraform injects: a personal
    # address in a public repository is permanent, and a runtime lookup means
    # changing the recipient needs no redeploy.
    recipient = optional_secret("DIGEST-EMAIL-TO")
    if not recipient:
        logger.info("no DIGEST-EMAIL-TO configured, not sending")
        return MailResult(status="skipped")

    # A recipient with a non-ASCII character is rejected by Resend with a 422
    # that names the address rather than the character, and curly quotes are
    # invisible in `az keyvault secret show` output. market-agent lost a day's
    # summary to exactly this, so it is worth catching before the request.
    addresses = [address.strip() for address in recipient.split(",") if address.strip()]
    for address in addresses:
        if not address.isascii():
            logger.warning("DIGEST-EMAIL-TO contains a non-ASCII character: %r", address)
            return MailResult(
                status="failed",
                error=f"recipient is not plain ASCII: {address!r}",
            )

    try:
        payload = post_json(
            RESEND_ENDPOINT,
            body={
                "from": cfg.digest_email_from,
                # Resend takes a list even for one recipient.
                "to": addresses,
                "subject": subject,
                "html": html,
                "text": text,
            },
            headers={
                "Authorization": f"Bearer {secret('RESEND-API-KEY')}",
                "Content-Type": "application/json",
            },
            client=client,
        )
    except FetchError as exc:
        logger.warning("digest email failed: %s", exc)
        return MailResult(status="failed", error=str(exc)[:500])

    return MailResult(status="sent", provider_id=(payload or {}).get("id"))
