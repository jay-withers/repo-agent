"""`checks.terraform`, against snapshots built directly."""

from __future__ import annotations

from repoagent.checks import renovate, terraform
from repoagent.github import parse
from tests.factories import human_pr, renovate_pr, snapshot
from tests.test_detail import _detail

PENDING = terraform.APPROVAL_PENDING_LABEL


def test_no_pending_label_is_silent() -> None:
    prs = (renovate_pr(labels=("dependencies",)), human_pr())
    assert terraform.awaiting_approval(snapshot(open_prs=prs)) == []


def test_pending_plans_are_one_finding_naming_each_pr() -> None:
    prs = (
        renovate_pr(
            days_old=9, number=12, title="chore(deps): update azurerm to v5", labels=(PENDING,)
        ),
        human_pr(number=7, title="feat: add a vault", labels=("enhancement", PENDING)),
        human_pr(number=8, title="docs: tidy"),
    )

    findings = terraform.awaiting_approval(snapshot(open_prs=prs))

    assert len(findings) == 1
    finding = findings[0]
    assert finding.check == "terraform.awaiting_approval"
    assert finding.title == "2 Terraform plan(s) awaiting approval"
    assert "#12" in finding.detail and "#7" in finding.detail and "#8" not in finding.detail
    assert "terraform-approved" in finding.detail
    assert finding.age_days == 9
    assert f"label%3A{PENDING}" in finding.evidence_url


def test_a_truncated_page_says_at_least() -> None:
    prs = (human_pr(labels=(PENDING,)),)
    findings = terraform.awaiting_approval(snapshot(open_prs=prs, open_pr_total=40))
    assert findings[0].title.startswith("at least 1 ")


def test_a_renovate_pr_awaiting_approval_is_not_also_stalled() -> None:
    # Old enough that stalled_prs would otherwise fire on it.
    pr = renovate_pr(days_old=40, labels=(PENDING,))

    assert renovate.stalled_prs(snapshot(open_prs=(pr,))) == []
    assert len(terraform.awaiting_approval(snapshot(open_prs=(pr,)))) == 1


def test_labels_are_parsed_from_the_detail_query() -> None:
    merged = parse.merge_detail(
        snapshot(),
        _detail(
            pullRequests={
                "totalCount": 1,
                "nodes": [
                    {
                        "number": 3,
                        "title": "chore(deps): update azurerm",
                        "url": "u",
                        "isDraft": False,
                        "createdAt": "2026-10-01T00:00:00Z",
                        "author": {"login": "renovate"},
                        "labels": {"nodes": [{"name": "dependencies"}, {"name": PENDING}]},
                    }
                ],
            }
        ),
    )

    assert merged.open_prs[0].labels == ("dependencies", PENDING)


def test_a_pr_without_labels_parses_to_no_labels() -> None:
    raw = {"number": 1, "title": "t", "author": {"login": "x"}}
    assert parse.pull_request(raw).labels == ()
