"""The scan: authenticate, read every repository, check it, triage, report.

Four stages, and the order of the middle two is the whole design:

1. **Fetch.** The REST installation list, then one batched GraphQL query for the
   detail behind it. A failure of the second degrades the run; a failure of the
   first ends it.
2. **Check.** Pure functions in `checks/` turn each snapshot into findings.
   Every fact the digest reports is established here.
3. **Triage.** `llm.py` asks DeepSeek to order those findings and write a
   paragraph of context. It cannot add, remove or alter a finding.
4. **Report.** `digest.py` renders, `mailer.py` sends.

Stage 3 is the only one that can be skipped — no API key, or a failed call, and
the digest goes out with its findings in check order and no commentary.
"""

from __future__ import annotations

import logging
import os

import httpx

from .. import checks, digest, llm, mailer
from ..github import client as github_client
from ..github import parse
from ..models import Finding, RepoSnapshot, ScanResult

logger = logging.getLogger(__name__)

# The order findings fall in before triage, and the order they stay in if triage
# is off. Severity first, because an unordered list of forty is read top-down and
# abandoned halfway.
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


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
        repos = _snapshots(session)
        findings = _findings(repos)
        ordered, summary, themes = llm.triage(findings, repos, client=session)

        result = ScanResult(
            repos=repos,
            findings=ordered,
            image_tag=os.environ.get("IMAGE_TAG", "unknown"),
            summary=summary,
            themes=themes,
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
        logger.info(
            "scanned %d repositories, %d finding(s), email %s",
            len(repos),
            len(ordered),
            outcome.status,
        )
        return result
    finally:
        if owned:
            session.close()


def _snapshots(session: httpx.Client) -> tuple[RepoSnapshot, ...]:
    """Every repository, with its GraphQL detail folded in where available.

    The detail query is allowed to fail. Its absence costs the Renovate checks
    their input — they report nothing rather than something wrong, since a
    snapshot with `renovate_config=None` because nothing was fetched is
    indistinguishable from one that genuinely has no config.
    """
    raw_repos = github_client.installation_repos(client=session)
    repos = [parse.repo_snapshot(raw) for raw in raw_repos]

    # Archived repositories are skipped by `checks.run_all` anyway, so fetching
    # their detail is tokens and rate limit spent on findings nobody will see.
    wanted = [repo.full_name for repo in repos if not repo.archived]
    # Deliberately broad. Anything this query can raise — a transport error, a
    # GraphQL node-limit rejection, a shape nobody anticipated — should cost the
    # run its detail and not its digest. The REST list alone still reports every
    # repository and every hygiene finding.
    try:
        details = github_client.repo_details(wanted, client=session)
    except Exception as exc:
        logger.warning("detail query failed, continuing without it: %s", exc)
        return tuple(repos)

    return tuple(parse.merge_detail(repo, details.get(repo.full_name)) for repo in repos)


def _findings(repos: tuple[RepoSnapshot, ...]) -> list[Finding]:
    """Run every check against every repository, in a stable order."""
    findings = [finding for repo in repos for finding in checks.run_all(repo)]
    findings.sort(key=lambda f: (_SEVERITY_RANK.get(f.severity, 9), f.repo, f.check))
    return findings
