"""The walking skeleton, end to end against a mock transport."""

from __future__ import annotations

import httpx

from repoagent import digest
from repoagent.jobs import scan
from repoagent.models import ScanResult
from tests.helpers import route_client

_REPOS = {
    "total_count": 2,
    "repositories": [
        {
            "name": "market-agent",
            "full_name": "jay-withers/market-agent",
            "description": "AI paper trading",
            "default_branch": "main",
            "archived": False,
            "pushed_at": "2026-09-18T08:00:00Z",
            "topics": ["azure", "python"],
            "license": {"key": "mit"},
            "html_url": "https://github.com/jay-withers/market-agent",
        },
        {
            "name": "git-demo",
            "full_name": "jay-withers/git-demo",
            "description": None,
            "default_branch": "main",
            "archived": False,
            "pushed_at": "2026-08-31T10:00:00Z",
            "topics": [],
            "license": None,
            "html_url": "https://github.com/jay-withers/git-demo",
        },
    ],
}

_AUTH_ROUTES = {
    "/app/installations/1/access_tokens": httpx.Response(
        201, json={"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"}
    ),
    "/app/installations": httpx.Response(200, json=[{"id": 1}]),
}


def test_render_reads_every_repo_and_sends_nothing() -> None:
    calls: list[httpx.Request] = []
    client = route_client(
        {**_AUTH_ROUTES, "/installation/repositories": httpx.Response(200, json=_REPOS)},
        calls=calls,
    )

    result = scan.run(send_email=False, http=client)

    assert [r.name for r in result.repos] == ["market-agent", "git-demo"]
    assert not any("resend" in str(c.url) for c in calls)


def test_scan_skips_the_email_when_no_recipient_is_configured() -> None:
    """The default, and it must not read as a failure.

    conftest deletes DIGEST_EMAIL_TO and KEY_VAULT_URI, so this is the path a
    developer gets with no configuration at all.
    """
    calls: list[httpx.Request] = []
    client = route_client(
        {**_AUTH_ROUTES, "/installation/repositories": httpx.Response(200, json=_REPOS)},
        calls=calls,
    )

    result = scan.run(send_email=True, http=client)

    assert len(result.repos) == 2
    assert not any("resend" in str(c.url) for c in calls)


def test_scan_sends_when_a_recipient_is_configured(monkeypatch) -> None:
    monkeypatch.setenv("DIGEST_EMAIL_TO", "someone@example.com")
    from repoagent import settings as settings_module

    settings_module.optional_secret.cache_clear()

    calls: list[httpx.Request] = []
    client = route_client(
        {
            **_AUTH_ROUTES,
            "/installation/repositories": httpx.Response(200, json=_REPOS),
            "api.resend.com": httpx.Response(200, json={"id": "mail_1"}),
        },
        calls=calls,
    )

    scan.run(send_email=True, http=client)

    sent = [c for c in calls if "resend" in str(c.url)]
    assert len(sent) == 1


def test_digest_reports_counts_from_the_data_not_from_prose() -> None:
    result = scan.run(
        send_email=False,
        http=route_client(
            {**_AUTH_ROUTES, "/installation/repositories": httpx.Response(200, json=_REPOS)}
        ),
    )

    assert "Scanned 2 repositories" in digest.render_text(result)
    # The subject's figures are both derived from the result, never written by a
    # model — which is the property this test exists to pin.
    # With no state configured every finding is new, so the subject says so —
    # and both figures are still derived from the result, never written by a
    # model, which is the property this test exists to pin.
    new = sum(1 for f in result.findings if f.is_new)
    assert digest.subject(result) == (
        f"repo-agent — {len(result.findings)} findings across {len(result.repos)} repos ({new} new)"
    )
    # The repository with no description is flagged as such in the listing.
    assert "no description" in digest.render_text(result)


def test_digest_says_nothing_to_flag_when_there_are_no_findings() -> None:
    """The empty case, built directly rather than scanned.

    A repository clean enough to produce no findings at all is hard to fake over
    HTTP and trivial to construct, which is the point of the checks being pure.
    """
    result = ScanResult(repos=(), findings=(), image_tag="v0.1.0")

    assert digest.subject(result) == "repo-agent — 0 repos, nothing to flag"
    assert "No findings." in digest.render_text(result)


def test_digest_carries_the_image_tag(monkeypatch) -> None:
    """Which build produced this digest.

    With the job's Terraform in one repository and the image in another,
    "what actually ran" is otherwise cross-repo archaeology.
    """
    monkeypatch.setenv("IMAGE_TAG", "v0.1.0")
    result = scan.run(
        send_email=False,
        http=route_client(
            {**_AUTH_ROUTES, "/installation/repositories": httpx.Response(200, json=_REPOS)}
        ),
    )
    assert "v0.1.0" in digest.render_text(result)
    assert "v0.1.0" in digest.render_html(result)


def test_the_footer_reports_what_triage_cost_and_what_is_left() -> None:
    """Both numbers computed, neither written by the model."""
    from repoagent.models import TriageUsage

    result = ScanResult(
        repos=(),
        image_tag="v0.1.0",
        usage=TriageUsage(
            model="deepseek-flash",
            cache_hit_tokens=1024,
            cache_miss_tokens=1653,
            completion_tokens=186,
            peak=True,
            cost_usd=0.000723,
            balance_usd="9.98",
        ),
    )

    footer = digest.triage_footer(result.usage)

    assert "deepseek-flash" in footer
    # Thousands separators, because a raw 2677 beside a dollar figure reads as money.
    assert "2,677 in (1,024 cached) / 186 out" in footer
    # `~` on the estimate, nothing on the balance: one is a hand-maintained price
    # table, the other is what DeepSeek says.
    assert "~$0.0007 peak" in footer
    assert "$9.98 left" in footer
    assert footer in digest.render_text(result)


def test_no_footer_when_triage_did_not_run() -> None:
    result = ScanResult(repos=(), image_tag="v0.1.0", usage=None)

    assert "triage" not in digest.render_text(result)


def test_render_does_not_save_state(monkeypatch) -> None:
    """Running `make run` twice must not rob Monday's email of its deltas."""
    from repoagent import state

    saved: list[object] = []
    monkeypatch.setattr(state, "save", lambda s: saved.append(s) or True)

    scan.run(
        send_email=False,
        http=route_client(
            {**_AUTH_ROUTES, "/installation/repositories": httpx.Response(200, json=_REPOS)}
        ),
    )

    assert saved == []


def test_a_skipped_email_does_not_save_state(monkeypatch) -> None:
    """A run that reported nothing must not record its findings as seen."""
    from repoagent import state

    saved: list[object] = []
    monkeypatch.setattr(state, "save", lambda s: saved.append(s) or True)

    # No DIGEST_EMAIL_TO, so mailer returns `skipped`.
    scan.run(
        send_email=True,
        http=route_client(
            {**_AUTH_ROUTES, "/installation/repositories": httpx.Response(200, json=_REPOS)}
        ),
    )

    assert saved == []


def test_a_sent_email_saves_state(monkeypatch) -> None:
    monkeypatch.setenv("DIGEST_EMAIL_TO", "someone@example.com")
    from repoagent import settings as settings_module
    from repoagent import state

    settings_module.optional_secret.cache_clear()
    saved: list[object] = []
    monkeypatch.setattr(state, "save", lambda s: saved.append(s) or True)

    scan.run(
        send_email=True,
        http=route_client(
            {
                **_AUTH_ROUTES,
                "/installation/repositories": httpx.Response(200, json=_REPOS),
                "api.resend.com": httpx.Response(200, json={"id": "mail_1"}),
            }
        ),
    )

    assert len(saved) == 1


def test_the_digest_names_what_was_resolved_and_counts_what_is_suppressed() -> None:
    result = ScanResult(
        repos=(),
        image_tag="v0.1.0",
        resolved=(("jay-withers/a", "no README"),),
        suppressed_count=2,
    )
    text = digest.render_text(result)

    assert "Resolved since last run (1):" in text
    assert "jay-withers/a: no README" in text
    assert "2 finding(s) suppressed" in text
