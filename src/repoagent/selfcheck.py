"""`repoagent selfcheck`: prove a job's container could do a real run, and do nothing.

After every deploy, `make smoke` (scripts/smoke.sh) starts one run of each job
with its args swapped for `selfcheck`. That run gets everything a real one
does — the image, the managed identity, the env and the Key Vault references —
and checks what a real run needs before it does anything: the settings load, every secret the scan
reads resolves, the GitHub App can mint an installation token, and the state in
storage reads.
It scans nothing, calls no LLM, sends no email and writes nothing.

Exits 1 if any check fails, naming each failure but never a secret's value. A
failure fails the smoke test, which rolls the deploy back.

The required secrets are the ones the jobs read with `secret()`, which raises
on a missing value, so a run without one would fail anyway; an optional one
only has to resolve. Add a secret here when a job starts reading one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

# github/auth.py and mailer.py (the scan job).
SECRETS = ("GITHUB-APP-ID", "GITHUB-APP-PRIVATE-KEY", "RESEND-API-KEY")
OPTIONAL_SECRETS = ("DIGEST-EMAIL-TO", "DEEPSEEK-API-KEY")


def checks() -> list[tuple[str, Callable[[], object]]]:
    from . import state
    from .github import auth
    from .settings import optional_secret, secret, settings

    def present(name: str) -> None:
        if not secret(name):
            raise ValueError("resolved to an empty value")

    return [
        ("settings", settings),
        *[(f"secret {name}", lambda name=name: present(name)) for name in SECRETS],
        *[
            (f"secret {name} (optional)", lambda name=name: optional_secret(name))
            for name in OPTIONAL_SECRETS
        ],
        # A real call: the App's key, its installation and GitHub all have to
        # work for a scan to see anything. Minting a token reads nothing.
        ("github app token", auth.token),
        # state.load() degrades to empty on purpose, so this proves the call
        # path rather than access; the scan tolerates it the same way.
        ("storage", state.load),
    ]


def run() -> int:
    failed = 0
    for label, check in checks():
        try:
            check()
        except Exception as exc:
            failed += 1
            logger.error("selfcheck: FAIL %s: %s: %s", label, type(exc).__name__, str(exc)[:300])
        else:
            logger.info("selfcheck: ok %s", label)
    if failed:
        logger.error("selfcheck: %d check(s) failed", failed)
        return 1
    logger.info("selfcheck: all checks passed")
    return 0
