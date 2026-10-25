"""`checks.security`, against snapshots built directly."""

from __future__ import annotations

import httpx

from repoagent.checks import security
from repoagent.github import client as github_client
from repoagent.github import parse
from repoagent.jobs import scan
from tests.factories import code_alert, snapshot
from tests.helpers import route_client


def test_a_repository_with_no_open_alerts_is_silent() -> None:
    assert security.open_code_scanning_alerts(snapshot()) == []


def test_unknown_alerts_are_silent_not_clean() -> None:
    # None is "could not ask" — no permission, CodeQL never ran, or the request
    # failed — and must produce nothing rather than a claim either way.
    assert security.open_code_scanning_alerts(snapshot(code_scanning_alerts=None)) == []


def test_open_alerts_are_one_finding_per_repository() -> None:
    alerts = (
        code_alert("py/path-injection", "high", days_old=40, number=1),
        code_alert("py/path-injection", "high", days_old=5, number=2),
        code_alert("py/clear-text-logging-sensitive-data", "medium", number=3),
    )

    [finding] = security.open_code_scanning_alerts(snapshot(code_scanning_alerts=alerts))

    assert finding.check == "security.open_code_scanning_alerts"
    assert finding.title == "3 open CodeQL alert(s)"
    assert finding.severity == "high"
    assert "2 high, 1 medium" in finding.detail
    assert "`py/path-injection` (2)" in finding.detail
    assert finding.evidence_url.endswith("/security/code-scanning")
    # Measured from the oldest alert: that is how long the fact has been true.
    assert finding.age_days == 40


def test_trivy_and_codeql_alerts_are_one_finding_naming_both_tools() -> None:
    alerts = (
        code_alert("CVE-2026-1234", "critical", number=1, tool="Trivy"),
        code_alert("py/path-injection", "medium", number=2),
    )

    [finding] = security.open_code_scanning_alerts(snapshot(code_scanning_alerts=alerts))

    assert finding.title == "2 open CodeQL and Trivy alert(s)"
    assert finding.severity == "high"
    assert "`CVE-2026-1234`" in finding.detail


def test_parse_reads_the_tool_name() -> None:
    [alert] = parse.code_scanning_alerts(
        [{"number": 1, "rule": {"id": "CVE-1"}, "tool": {"name": "Trivy"}}]
    )
    assert alert.tool == "Trivy"


def test_severity_follows_the_worst_alert() -> None:
    def severity_of(*levels: str) -> str:
        alerts = tuple(code_alert(severity=s, number=i) for i, s in enumerate(levels))
        return security.open_code_scanning_alerts(snapshot(code_scanning_alerts=alerts))[0].severity

    assert severity_of("low", "critical") == "high"
    assert severity_of("error") == "high"
    assert severity_of("note", "medium") == "medium"
    assert severity_of("warning") == "medium"
    assert severity_of("note", "low") == "low"


def test_the_detail_never_names_a_file() -> None:
    # Findings go to the triage model. The location of an unfixed vulnerability
    # is the part to keep out of that prompt.
    [finding] = security.open_code_scanning_alerts(snapshot(code_scanning_alerts=(code_alert(),)))
    assert ".py" not in finding.detail.replace("py/", "")


def test_a_full_page_says_at_least() -> None:
    alerts = tuple(code_alert(number=i) for i in range(100))
    [finding] = security.open_code_scanning_alerts(snapshot(code_scanning_alerts=alerts))
    assert finding.title == "at least 100 open CodeQL alert(s)"


def test_parse_prefers_security_severity_and_keeps_none_as_none() -> None:
    raw = [
        {
            "number": 4,
            "created_at": "2026-09-01T00:00:00Z",
            "html_url": "https://github.com/o/r/security/code-scanning/4",
            "rule": {
                "id": "py/sql-injection",
                "severity": "error",
                "security_severity_level": "critical",
            },
        },
        {
            "number": 5,
            "rule": {"id": "py/unused-import", "severity": "note", "security_severity_level": None},
        },
    ]

    alerts = parse.code_scanning_alerts(raw)

    assert alerts is not None
    assert [(a.rule, a.severity) for a in alerts] == [
        ("py/sql-injection", "critical"),
        ("py/unused-import", "note"),
    ]
    assert parse.code_scanning_alerts(None) is None
    assert parse.code_scanning_alerts([]) == ()


# --- fetching -------------------------------------------------------------

_TOKEN = "/app/installations/1/access_tokens"


def _auth_routes(permissions: dict[str, str]) -> dict[str, httpx.Response]:
    return {
        _TOKEN: httpx.Response(
            201,
            json={
                "token": "ghs_x",
                "expires_at": "2099-01-01T00:00:00Z",
                "permissions": permissions,
            },
        ),
        "/app/installations": httpx.Response(200, json=[{"id": 1}]),
    }


def test_without_the_permission_no_alert_is_requested() -> None:
    # The alert API would answer 403, which `fetch` retries as a rate limit —
    # seconds per repository spent learning nothing.
    calls: list[httpx.Request] = []
    client = route_client(_auth_routes({"contents": "read"}), calls=calls)

    assert github_client.code_scanning_alerts(["o/a", "o/b"], client=client) == {}
    assert not any("code-scanning" in str(c.url) for c in calls)


def test_alerts_are_read_per_repository_and_a_404_is_unknown() -> None:
    calls: list[httpx.Request] = []
    client = route_client(
        {
            **_auth_routes({"security_events": "read"}),
            "/repos/o/a/code-scanning/alerts": httpx.Response(
                200, json=[{"number": 1, "rule": {"id": "py/x", "severity": "error"}}]
            ),
            # Code scanning has never run here: unknown, not clean.
            "/repos/o/b/code-scanning/alerts": httpx.Response(
                404, json={"message": "no analysis found"}
            ),
        },
        calls=calls,
    )

    alerts = github_client.code_scanning_alerts(["o/a", "o/b"], client=client)

    assert alerts["o/a"] == [{"number": 1, "rule": {"id": "py/x", "severity": "error"}}]
    assert alerts["o/b"] is None
    [first, *_] = [c for c in calls if "code-scanning" in str(c.url)]
    assert first.url.params["state"] == "open"
    # Every tool: Trivy's image findings land beside CodeQL's.
    assert "tool_name" not in first.url.params


def test_the_scan_reports_open_alerts() -> None:
    repos = {
        "total_count": 1,
        "repositories": [
            {
                "name": "widget",
                "full_name": "jay-withers/widget",
                "description": "A widget",
                "default_branch": "main",
                "pushed_at": "2026-09-18T08:00:00Z",
                "license": {"key": "mit"},
                "html_url": "https://github.com/jay-withers/widget",
            }
        ],
    }
    client = route_client(
        {
            **_auth_routes({"security_events": "read"}),
            "/installation/repositories": httpx.Response(200, json=repos),
            "/code-scanning/alerts": httpx.Response(
                200,
                json=[
                    {
                        "number": 9,
                        "rule": {
                            "id": "py/sql-injection",
                            "severity": "error",
                            "security_severity_level": "high",
                        },
                    }
                ],
            ),
        }
    )

    result = scan.run(send_email=False, http=client)

    [finding] = [f for f in result.findings if f.check == "security.open_code_scanning_alerts"]
    assert finding.repo == "jay-withers/widget"
    assert finding.severity == "high"
