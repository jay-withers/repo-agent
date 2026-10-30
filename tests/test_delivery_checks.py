"""`checks.delivery` and `renovate.blocked_prs`, against snapshots built directly."""

from __future__ import annotations

import httpx

from repoagent.checks import delivery, renovate
from repoagent.github import client as github_client
from repoagent.github import parse
from tests.factories import issue, renovate_pr, snapshot
from tests.helpers import route_client
from tests.test_detail import _detail
from tests.test_security_checks import _auth_routes

FAILED = delivery.DEPLOY_FAILED_LABEL
PENDING = delivery.TERRAFORM_PENDING_LABEL
HELD = delivery.DEPLOY_PENDING_LABEL


def test_unknown_issues_are_silent() -> None:
    # None is "could not read" (no Issues permission, or the request failed).
    assert delivery.deploy_failed(snapshot(open_issues=None)) == []
    assert delivery.terraform_pending(snapshot(open_issues=None)) == []
    assert delivery.deploy_pending(snapshot(open_issues=None)) == []


def test_unrelated_issues_are_silent() -> None:
    issues = (issue(labels=("bug",)), issue(number=6, labels=()))
    assert delivery.deploy_failed(snapshot(open_issues=issues)) == []
    assert delivery.terraform_pending(snapshot(open_issues=issues)) == []
    assert delivery.deploy_pending(snapshot(open_issues=issues)) == []


def test_a_failed_deploy_is_high_and_named() -> None:
    issues = (issue(number=12, title="Deploy to dev failed", labels=(FAILED,), days_old=4),)

    [finding] = delivery.deploy_failed(snapshot(open_issues=issues))

    assert finding.check == "delivery.deploy_failed"
    assert finding.severity == "high"
    assert "#12 'Deploy to dev failed'" in finding.detail
    assert finding.age_days == 4
    assert f"label%3A{FAILED}" in finding.evidence_url


def test_pending_terraform_is_medium_and_named() -> None:
    issues = (
        issue(
            number=3, title="Terraform changes pending apply to dev (terraform)", labels=(PENDING,)
        ),
    )

    [finding] = delivery.terraform_pending(snapshot(open_issues=issues))

    assert finding.check == "delivery.terraform_pending"
    assert finding.severity == "medium"
    assert "#3" in finding.detail and "cd-terraform" in finding.detail


def test_a_held_deploy_is_medium_and_named() -> None:
    issues = (issue(number=9, title="Deploy to dev pending", labels=(HELD,), days_old=2),)

    [finding] = delivery.deploy_pending(snapshot(open_issues=issues))

    assert finding.check == "delivery.deploy_pending"
    assert finding.severity == "medium"
    assert "#9 'Deploy to dev pending'" in finding.detail and "cd-publish" in finding.detail
    assert finding.age_days == 2
    assert f"label%3A{HELD}" in finding.evidence_url


def test_issues_parse_drops_pull_requests_and_keeps_none() -> None:
    raw = [
        {
            "number": 1,
            "title": "Deploy to dev failed",
            "labels": [{"name": FAILED}],
            "created_at": "2026-10-20T00:00:00Z",
            "html_url": "u",
        },
        {"number": 2, "title": "a PR", "labels": [], "pull_request": {"url": "x"}},
    ]
    [only] = parse.issues(raw)
    assert only.number == 1 and only.labels == (FAILED,)
    assert parse.issues(None) is None


def test_without_the_issues_permission_no_issue_is_requested() -> None:
    calls: list[httpx.Request] = []
    client = route_client(_auth_routes({"contents": "read"}), calls=calls)

    assert github_client.open_issues(["o/a"], client=client) == {}
    assert not any("/issues" in str(c.url) for c in calls if "/app/" not in str(c.url))


def test_issues_are_read_per_repository_and_a_failure_is_unknown() -> None:
    client = route_client(
        {
            **_auth_routes({"issues": "read"}),
            "/repos/o/a/issues": httpx.Response(200, json=[{"number": 1, "title": "t"}]),
            "/repos/o/b/issues": httpx.Response(404, json={"message": "nope"}),
        }
    )

    issues = github_client.open_issues(["o/a", "o/b"], client=client)

    assert issues["o/a"] == [{"number": 1, "title": "t"}]
    assert issues["o/b"] is None


def test_a_renovate_pr_blocked_for_a_week_is_named() -> None:
    prs = (
        renovate_pr(
            days_old=30,
            number=23,
            title="chore(deps): Lock file maintenance",
            merge_state="BLOCKED",
        ),
        renovate_pr(days_old=10, number=13, title="chore(deps): update x", merge_state="DIRTY"),
    )

    [finding] = renovate.blocked_prs(snapshot(open_prs=prs))

    assert finding.check == "renovate.blocked_prs"
    assert finding.severity == "high"
    assert finding.title == "2 Renovate PR(s) cannot merge"
    assert "#23 'chore(deps): Lock file maintenance' (checks failing, open 30d)" in finding.detail
    assert "#13 'chore(deps): update x' (conflicts, open 10d)" in finding.detail
    assert finding.age_days == 30


def test_a_freshly_rebased_blocked_pr_is_not_reported() -> None:
    # BLOCKED while its checks run: a day old is not stuck.
    prs = (renovate_pr(days_old=1, merge_state="BLOCKED"),)
    assert renovate.blocked_prs(snapshot(open_prs=prs)) == []


def test_a_blocked_pr_is_not_also_stalled() -> None:
    pr = renovate_pr(days_old=40, merge_state="BLOCKED")

    assert renovate.stalled_prs(snapshot(open_prs=(pr,))) == []
    assert len(renovate.blocked_prs(snapshot(open_prs=(pr,)))) == 1


def test_merge_state_is_parsed_from_the_detail_query() -> None:
    merged = parse.merge_detail(
        snapshot(),
        _detail(
            pullRequests={
                "totalCount": 1,
                "nodes": [
                    {
                        "number": 3,
                        "title": "t",
                        "url": "u",
                        "isDraft": False,
                        "createdAt": "2026-10-01T00:00:00Z",
                        "author": {"login": "renovate"},
                        "mergeStateStatus": "BLOCKED",
                    }
                ],
            }
        ),
    )
    assert merged.open_prs[0].merge_state == "BLOCKED"
