"""The scan: authenticate, read every repository, check it, triage, report.

Five stages, and the order of the middle ones is the whole design:

1. **Fetch.** The REST installation list, then one batched GraphQL query for the
   detail behind it. A failure of the second degrades the run; a failure of the
   first ends it.
2. **Check.** Pure functions in `checks/` turn each snapshot into findings.
   Every fact the digest reports is established here.
3. **Reconcile.** `state.py` places those findings against what the last run
   saw: which are new, which have gone, which a suppression is holding back.
4. **Triage.** `llm.py` asks DeepSeek to order what remains and write a
   paragraph of context. It cannot add, remove or alter a finding.
5. **Report.** `digest.py` renders, `mailer.py` sends.

Stages 3 and 4 can both be skipped — no storage account, no API key, or a
failure in either, and the digest still goes out with its findings. What is lost
is the deltas and the commentary, never the findings themselves.

**Reconcile runs before triage**, so a suppressed finding is never sent to
DeepSeek: there is no point paying to prioritise something the digest will not
print, and a suppression is a decision the model has no business revisiting.

**State is saved last, after the email.** A run that fails to send must not
record its findings as seen, or the retry reports nothing as new.
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace

import httpx

from .. import checks, digest, llm, mailer, state
from ..github import client as github_client
from ..github import parse
from ..models import Finding, RepoSnapshot, ScanResult
from ..settings import settings

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
        repos, ignored = _snapshots(session)
        found = _findings(repos)

        against = state.load()
        reconciled = state.reconcile(against, found)
        logger.info(
            "%d finding(s): %d new, %d resolved, %d suppressed",
            len(reconciled.findings),
            len(reconciled.new),
            len(reconciled.resolved),
            len(reconciled.suppressed),
        )

        triaged = llm.triage(list(reconciled.findings), repos, client=session)

        result = ScanResult(
            repos=repos,
            findings=triaged.findings,
            image_tag=os.environ.get("IMAGE_TAG", "unknown"),
            summary=triaged.summary,
            themes=triaged.themes,
            usage=triaged.usage,
            # Deliberately not folded into `findings`, and deliberately not
            # passed to `state`: a suggestion is an opinion, and nothing about it
            # should ever be mistaken for something the scanner checked.
            suggestions=triaged.suggestions,
            ignored=ignored,
            resolved=tuple((k.repo, k.title) for k in reconciled.resolved),
            suppressed_count=len(reconciled.suppressed),
        )

        if not send_email:
            # `render` deliberately does not save. Printing the digest locally
            # must not mark everything as seen and rob the next real run of its
            # deltas — running `make run` twice would otherwise empty the "new"
            # section of Monday's email.
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
            len(triaged.findings),
            outcome.status,
        )

        # Last, and only once the digest is out. A run that failed to send must
        # not record its findings as seen, or the retry reports nothing as new.
        if outcome.status != "skipped":
            state.save(reconciled.state)
        return result
    finally:
        if owned:
            session.close()


def _snapshots(
    session: httpx.Client,
) -> tuple[tuple[RepoSnapshot, ...], tuple[tuple[str, str], ...]]:
    """Every repository worth checking, plus the ones deliberately skipped.

    The detail query is allowed to fail. Its absence costs the Renovate checks
    their input — they report nothing rather than something wrong, since a
    snapshot with `renovate_config=None` because nothing was fetched is
    indistinguishable from one that genuinely has no config.
    """
    raw_repos = github_client.installation_repos(client=session)
    everything = [parse.repo_snapshot(raw) for raw in raw_repos]

    # Dropped before anything else, so an exempt repository costs no GraphQL
    # node budget and contributes nothing to the triage prompt.
    exempt = settings().ignored_topics
    ignored: list[tuple[str, str]] = []
    repos: list[RepoSnapshot] = []
    for repo in everything:
        matched = next((t for t in repo.topics if t.lower() in exempt), None)
        if matched:
            ignored.append((repo.full_name, matched))
        else:
            repos.append(repo)
    if ignored:
        logger.info(
            "ignoring %d repositor%s by topic: %s",
            len(ignored),
            "y" if len(ignored) == 1 else "ies",
            ", ".join(f"{name} ({topic})" for name, topic in ignored),
        )

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
        return tuple(repos), tuple(ignored)

    # None when unreadable, which `estate.unmanaged` treats as "do not judge" —
    # a moved catalogue file must not report the whole estate as unmanaged.
    declared = github_client.catalogue(client=session)

    merged = tuple(
        replace(
            parse.merge_detail(repo, details.get(repo.full_name)),
            in_catalogue=None if declared is None else repo.name in declared,
            required_checks=(
                None
                if declared is None or repo.name not in declared
                else tuple(sorted(declared[repo.name]))
            ),
        )
        for repo in repos
    )
    return merged, tuple(ignored)


def _findings(repos: tuple[RepoSnapshot, ...]) -> list[Finding]:
    """Run every check against every repository, in a stable order."""
    findings = [finding for repo in repos for finding in checks.run_all(repo)]
    findings.sort(key=lambda f: (_SEVERITY_RANK.get(f.severity, 9), f.repo, f.check))
    return findings
