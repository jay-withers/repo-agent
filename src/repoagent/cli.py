"""One entrypoint, two commands — `repoagent scan|render`.

Both workloads share one image and differ only by the container's `args`. Note
Terraform deliberately sets **no** `command`: the Dockerfile's `ENTRYPOINT` names
this console script, and duplicating that name in Terraform creates a second
source of truth that is not versioned with the code defining it. market-agent
took a full outage from exactly that in September 2026, when an `ignore_changes`
command went stale against a renamed script and every workload crash-looped on
`executable file not found`.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from . import telemetry
from .settings import settings


def _configure_logging() -> None:
    """Plain logging to stdout.

    The Container Apps environment already ships stdout to Log Analytics, where
    `daily_quota_gb = 0.15` is shared with every other project on the platform.
    `telemetry.configure()` then exports this same `repoagent` logger to
    Application Insights when a connection string is present, so a line logged
    here is paid for twice — keep it that way round rather than logging more
    because one of the two destinations happens to be quiet.
    """
    logging.basicConfig(
        level=getattr(logging, settings().log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stdout,
    )
    # Chatty at INFO and say nothing useful about the run.
    for noisy in (
        "httpx",
        "httpcore",
        "azure.core.pipeline.policies.http_logging_policy",
        "azure.identity",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _terminate(signum: int, _frame: object) -> None:
    """Turn SIGTERM into an exception so `finally` blocks run.

    Container Apps sends SIGTERM before SIGKILL when a job reaches
    `replica_timeout_in_seconds` or is scaled down. Without this the default
    disposition kills the process outright, and buffered telemetry is lost with
    it — `telemetry.flush()` never runs.
    """
    raise SystemExit(f"terminated by signal {signum}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="repoagent")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scan", help="scan every repository and email the digest")
    sub.add_parser("render", help="scan and print the digest, sending nothing")

    args = parser.parse_args(argv)

    _configure_logging()
    telemetry.configure(args.command)
    signal.signal(signal.SIGTERM, _terminate)

    logger = logging.getLogger("repoagent.cli")

    try:
        # Imported lazily so that `--help` and a failed argument parse cost
        # nothing, and so an import error in the job surfaces against the
        # command that needed it.
        from .jobs import scan

        if args.command == "scan":
            scan.run(send_email=True)
        else:
            result = scan.run(send_email=False)
            from . import digest

            print(digest.render_text(result))
    except (Exception, SystemExit) as exc:
        # Both, because SystemExit is what _terminate raises and it does not
        # derive from Exception. A non-zero return is what marks the job
        # execution failed, which is what the platform's job-failure alert
        # watches for.
        logger.exception("repoagent %s failed: %s", args.command, exc)
        return 1
    finally:
        telemetry.flush()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
