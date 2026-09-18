"""Turning GitHub's JSON into the frozen types the checks consume.

Separate from `client.py` so that everything downstream of a request is a pure
function of a dictionary, and can be tested from a saved payload.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import RepoSnapshot


def repo_snapshot(raw: dict[str, Any]) -> RepoSnapshot:
    """Build a snapshot from one `/installation/repositories` entry."""
    return RepoSnapshot(
        name=raw["name"],
        full_name=raw["full_name"],
        description=raw.get("description"),
        default_branch=raw.get("default_branch", "main"),
        archived=bool(raw.get("archived", False)),
        pushed_at=_timestamp(raw.get("pushed_at")),
        topics=tuple(raw.get("topics") or ()),
        has_license=raw.get("license") is not None,
        url=raw.get("html_url", ""),
    )


def _timestamp(raw: Any) -> datetime | None:
    """Parse one of GitHub's ISO-8601 timestamps, tolerating absence.

    Returns None rather than raising: a missing or malformed timestamp costs
    one finding its age, and should not cost the whole scan.
    """
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
