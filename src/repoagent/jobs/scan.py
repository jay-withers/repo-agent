"""The scan: authenticate, read every repository, report.

This is the walking skeleton. It proves the whole path end to end — GitHub App
authentication, the installation's repository list, the digest, the email — with
no checks and no model call yet. Those land on top of this without changing its
shape: `run()` already produces a `ScanResult` with a findings tuple, and the
checks will fill it.
"""

from __future__ import annotations

import logging
import os

import httpx

from .. import digest, mailer
from ..github import client as github_client
from ..github import parse
from ..models import ScanResult

logger = logging.getLogger(__name__)


def run(*, send_email: bool = True, http: httpx.Client | None = None) -> ScanResult:
    """Scan every repository the App is installed on and report.

    `send_email=False` is the `render` subcommand: everything except the send,
    so the digest can be read locally without a Resend key or a recipient.

    `http` is injectable so the whole path can be exercised against a
    MockTransport with no network.
    """
    owned = http is None
    session = http or httpx.Client(timeout=30.0)
    try:
        raw_repos = github_client.installation_repos(client=session)
        repos = tuple(parse.repo_snapshot(raw) for raw in raw_repos)

        result = ScanResult(
            repos=repos,
            findings=(),
            image_tag=os.environ.get("IMAGE_TAG", "unknown"),
        )

        if not send_email:
            return result

        outcome = mailer.send(
            subject=digest.subject(result),
            html=digest.render_html(result),
            text=digest.render_text(result),
            client=session,
        )
        # Logged rather than raised, and the status is part of the log line:
        # `skipped` is the normal outcome of a development run and must not
        # read as a failure.
        logger.info("scanned %d repositories, email %s", len(repos), outcome.status)
        return result
    finally:
        if owned:
            session.close()
