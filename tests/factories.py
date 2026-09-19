"""Builders for the frozen domain types.

Separate from `helpers.py` deliberately: that file is copied verbatim from
market-agent and is re-copied rather than diverged, so anything specific to this
project's models belongs here instead.

Every builder defaults to a *healthy* repository, so a test names only the thing
it is testing. The alternative — defaulting to empty and having each test fill in
five unrelated fields — makes it impossible to see at a glance what a test is
actually about.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from repoagent.models import PullRequest, RepoSnapshot, Workflow

HEALTHY_RENOVATE = '{"extends": ["github>jay-withers/renovate"], "packageRules": []}'

# A workflow with nothing wrong with it: a supported runner, and every action
# pinned to a commit with the tag as a comment — this estate's own convention.
HEALTHY_WORKFLOW = """\
name: CI
on: [pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
      - uses: jay-withers/workflows/ci.yml@2d4b1e0f3a5c7d9e1f2a3b4c5d6e7f8091a2b3c4 # v1.2.0
"""

# One `h1:` per platform locked, which is what a real lock file carries — the
# platform *names* appear nowhere in one.
HEALTHY_TF_LOCK = """\
provider "registry.terraform.io/hashicorp/azurerm" {
  version = "5.6.0"
  hashes = [
    "h1:0GEze9b+Z5XuA1H9v+NfgQ7yO9XfxV4YaCvWAJ3Ul/k=",
    "h1:41VFAA3JqAsqQovfhif8aRkmgv6KuuppxSLe34mK7j8=",
    "h1:KcCIg3phnZW7/clpgZ6hDnhco6CNh/Hc00nQQZTIBjQ=",
    "zh:0f4c7b924708dcdf58b7077988549a07dabe6800cef7666a00a0d0ba27aa49d4",
  ]
}
"""


def workflow(name: str = "ci.yml", text: str = HEALTHY_WORKFLOW) -> Workflow:
    return Workflow(name=name, text=text)


def snapshot(**overrides: object) -> RepoSnapshot:
    """A repository with nothing wrong with it."""
    defaults: dict[str, object] = {
        "name": "widget",
        "full_name": "jay-withers/widget",
        "description": "A widget",
        "default_branch": "main",
        "archived": False,
        "pushed_at": datetime.now(UTC) - timedelta(days=3),
        "topics": (),
        "has_license": True,
        "has_readme": True,
        "url": "https://github.com/jay-withers/widget",
        "private": False,
        "renovate_config": HEALTHY_RENOVATE,
        "renovate_config_path": "renovate.json",
        "readme": "# widget\n",
        "dockerfile": None,
        "workflows": (workflow(),),
        "terraform_lock": None,
        "in_catalogue": True,
        # What HEALTHY_WORKFLOW's one job reports, so the default repository
        # satisfies its own required checks.
        "required_checks": ("test",),
        "open_prs": (),
        "last_release": "v1.0.0",
        "last_release_at": datetime.now(UTC) - timedelta(days=10),
    }
    return RepoSnapshot(**{**defaults, **overrides})  # type: ignore[arg-type]


def renovate_pr(days_old: int = 1, **overrides: object) -> PullRequest:
    """An open Renovate pull request, `days_old` days old."""
    defaults: dict[str, object] = {
        "number": 42,
        "title": "chore(deps): update dependency httpx to v0.28.1",
        "author": "renovate[bot]",
        "created_at": datetime.now(UTC) - timedelta(days=days_old),
        "url": "https://github.com/jay-withers/widget/pull/42",
        "draft": False,
    }
    return PullRequest(**{**defaults, **overrides})  # type: ignore[arg-type]
