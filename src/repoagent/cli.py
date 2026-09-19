"""One entrypoint — `repoagent scan|render|state|suppress|unsuppress`.

`scan` and `render` are the workload; the other three are operator commands for
the state document, and exist because a suppression nobody can add, list or
remove is a feature that does not work. They run against the real blob, so they
need `STATE_CONTAINER_URL` and a credential with `Storage Blob Data Contributor`
on the container — which whoever applied the Terraform already has.

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
    sub.add_parser("state", help="print what the scan remembers between runs")

    hush = sub.add_parser("suppress", help="stop reporting one finding, with a reason")
    hush.add_argument("finding_id", help="the id shown by `repoagent state`")
    # Required, not optional. A suppression without a reason is indistinguishable
    # in six months from a bug, and the whole point is to record the decision.
    hush.add_argument("--reason", required=True, help="why this is acceptable")
    hush.add_argument(
        "--until",
        metavar="YYYY-MM-DD",
        help="expire the suppression on this date; omit for indefinitely",
    )

    speak = sub.add_parser("unsuppress", help="start reporting a finding again")
    speak.add_argument("finding_id", help="the id shown by `repoagent state`")

    args = parser.parse_args(argv)

    _configure_logging()
    telemetry.configure(args.command)
    signal.signal(signal.SIGTERM, _terminate)

    logger = logging.getLogger("repoagent.cli")

    try:
        # Imported lazily so that `--help` and a failed argument parse cost
        # nothing, and so an import error in the job surfaces against the
        # command that needed it.
        if args.command in {"state", "suppress", "unsuppress"}:
            return _state_command(args)

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


def _state_command(args: argparse.Namespace) -> int:
    """The operator commands, which read and write the state document directly.

    Unlike the scan, these **report failure**. A `save` that silently did nothing
    would leave someone believing they had suppressed a finding, and they would
    only find out next Monday.
    """
    from dataclasses import replace

    from . import state as state_module

    logger = logging.getLogger("repoagent.state")
    store = state_module.load()

    if args.command == "state":
        print(store.to_json())
        return 0

    if args.command == "unsuppress":
        if args.finding_id not in store.suppressed:
            logger.error("%s is not suppressed", args.finding_id)
            return 1
        remaining = {k: v for k, v in store.suppressed.items() if k != args.finding_id}
        updated = replace(store, suppressed=remaining)
    else:
        from datetime import UTC, date, datetime

        if args.until:
            try:
                date.fromisoformat(args.until)
            except ValueError:
                logger.error("--until %r is not a YYYY-MM-DD date", args.until)
                return 1
        # Warn rather than refuse on an unknown id: a finding that is currently
        # resolved may come back, and pre-suppressing one is legitimate.
        if args.finding_id not in store.findings:
            logger.warning("%s is not a finding this run knows about", args.finding_id)
        updated = replace(
            store,
            suppressed={
                **store.suppressed,
                args.finding_id: state_module.Suppression(
                    reason=args.reason,
                    until=args.until,
                    added=datetime.now(UTC).date().isoformat(),
                ),
            },
        )

    if not state_module.save(updated):
        logger.error("could not write the state document; nothing was changed")
        return 1
    logger.info("state updated: %s %sd", args.finding_id, args.command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
