"""Application Insights, configured at start-up and flushed before exit.

Copied from jay-withers/market-agent apps/marketagent/src/marketagent/telemetry.py.
Re-copy rather than diverge. Only the logger and service names differ, and the
FastAPI exclusion is gone because there is no HTTP server here.

Terraform injects `APPLICATIONINSIGHTS_CONNECTION_STRING` from the shared
platform's Application Insights, but nothing reads it unless this module runs:
Container Apps has no codeless agent, so the variable on its own populates
precisely nothing. An absent or empty string disables telemetry entirely, which
is what keeps the tests and a local `repoagent render` offline and free of the
SDK's start-up cost.

Two things are deliberately narrower than the distro's defaults, both because
the shared workspace runs on `daily_quota_gb = 0.15` — a cap this agent now
shares with every other project on the platform:

- **Only the `repoagent` logger is exported.** The distro attaches its handler
  to the root logger, which would ship every third-party line to Application
  Insights *as well as* to Log Analytics, where container stdout already lands.
- **Performance counters and live metrics are off.** Both are periodic
  emissions from a job that runs for under a minute once a week.
"""

from __future__ import annotations

import logging
import os

from .settings import settings

logger = logging.getLogger(__name__)

_configured = False


def configure(role: str) -> bool:
    """Set up tracing, metrics and log export. Returns whether it was enabled.

    `role` becomes the cloud role name, so a scheduled scan and a hand-run one
    are distinguishable in the portal.
    """
    global _configured

    if _configured:
        return True

    connection_string = settings().applicationinsights_connection_string
    if not connection_string:
        return False

    # Imported here rather than at module scope so the checks, the models and
    # the tests need neither the SDK nor a connection string — the same reason
    # settings.py defers its Azure imports.
    from azure.monitor.opentelemetry import configure_azure_monitor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource

    configure_azure_monitor(
        connection_string=connection_string,
        logger_name="repoagent",
        enable_live_metrics=False,
        enable_performance_counters=False,
        resource=Resource.create(
            {
                "service.name": f"repoagent-{role}",
                # The immutable image tag, so a trace and the digest it produced
                # name the same build.
                "service.version": os.environ.get("IMAGE_TAG", "unknown"),
                "deployment.environment": settings().environment,
            }
        ),
    )

    # What turns the GitHub and Resend calls into dependency spans.
    HTTPXClientInstrumentor().instrument()

    _configured = True
    logger.info("application insights enabled for %s", role)
    return True


def flush(timeout_millis: int = 10_000) -> None:
    """Push buffered telemetry before a short-lived process exits.

    The exporters batch, and this is a process that runs for under a minute and
    then stops. Without this it exits with its spans and logs still in the
    buffer, which looks exactly like telemetry that was never configured.

    Never raises: losing telemetry must not turn a successful run into a failed
    one, nor mask the exception the job is already exiting on.
    """
    if not _configured:
        return

    from opentelemetry import _logs, metrics, trace

    for provider in (
        trace.get_tracer_provider(),
        _logs.get_logger_provider(),
        metrics.get_meter_provider(),
    ):
        force_flush = getattr(provider, "force_flush", None)
        if force_flush is None:
            continue
        try:
            force_flush(timeout_millis)
        except Exception as exc:
            logger.warning("flushing telemetry failed: %s", exc)
