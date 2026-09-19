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

from repoagent.models import PullRequest, RepoSnapshot

HEALTHY_RENOVATE = '{"extends": ["config:recommended"], "packageRules": []}'


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
        "workflows": ("ci.yml",),
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
