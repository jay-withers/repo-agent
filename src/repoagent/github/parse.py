"""Turning GitHub's JSON into the frozen types the checks consume.

Separate from `client.py` so that everything downstream of a request is a pure
function of a dictionary, and can be tested from a saved payload.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from ..models import PullRequest, RepoSnapshot, Workflow


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
        private=bool(raw.get("private", False)),
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


def pull_request(raw: dict[str, Any]) -> PullRequest:
    """Build a pull request from one GraphQL `pullRequests.nodes` entry."""
    # `author` is null for a PR whose account was deleted, which is rare and
    # must not take the run with it.
    author = (raw.get("author") or {}).get("login") or "unknown"
    return PullRequest(
        number=int(raw.get("number", 0)),
        title=raw.get("title", ""),
        author=author,
        created_at=_timestamp(raw.get("createdAt")),
        url=raw.get("url", ""),
        draft=bool(raw.get("isDraft", False)),
    )


def merge_detail(snapshot: RepoSnapshot, detail: dict[str, Any] | None) -> RepoSnapshot:
    """Fold one repository's GraphQL detail into its REST snapshot.

    Returns the snapshot unchanged when there is no detail, so a repository the
    batch could not resolve still reaches the checks with everything the REST
    list knew about it.
    """
    if not detail:
        return snapshot

    config_path, config_text = _renovate_config(detail)
    workflows = detail.get("workflows") or {}
    releases = (detail.get("releases") or {}).get("nodes") or []
    release = releases[0] if releases else {}
    prs = (detail.get("pullRequests") or {}).get("nodes") or []

    return replace(
        snapshot,
        renovate_config=config_text,
        renovate_config_path=config_path,
        readme=_blob_text(detail.get("readme")),
        dockerfile=_blob_text(detail.get("dockerfile")),
        has_readme=_blob_text(detail.get("readme")) is not None,
        workflows=tuple(
            Workflow(name=entry.get("name", ""), text=_blob_text(entry.get("object")) or "")
            for entry in (workflows.get("entries") or [])
            # A Tree lists directories too, and `.github/workflows` legitimately
            # contains non-workflow files that GitHub Actions ignores.
            if entry.get("name", "").endswith((".yml", ".yaml"))
        ),
        # `terraform/` first, which is this estate's layout; the root is the
        # fallback for a repository that is itself a module.
        terraform_lock=_blob_text(detail.get("tflock")) or _blob_text(detail.get("tflock_root")),
        open_prs=tuple(pull_request(raw) for raw in prs),
        last_release=release.get("tagName"),
        last_release_at=_timestamp(release.get("publishedAt")),
    )


def _renovate_config(detail: dict[str, Any]) -> tuple[str | None, str | None]:
    """The first Renovate config present, as `(path, text)`.

    First in `RENOVATE_CONFIG_PATHS` order, which is Renovate's own precedence —
    a repository with two of them is read by Renovate the same way.
    """
    from .client import RENOVATE_CONFIG_PATHS

    for index, path in enumerate(RENOVATE_CONFIG_PATHS):
        text = _blob_text(detail.get(f"renovate{index}"))
        if text is not None:
            return path, text
    return None, None


def _blob_text(node: Any) -> str | None:
    """The text of a GraphQL Blob, or None where the path does not exist.

    A binary blob comes back with `text: null` even though the object exists,
    which is indistinguishable here from absence and is the right answer anyway:
    there is nothing for a check or a prompt to read.

    Truncated here rather than at the prompt, so that the cap applies to what is
    stored and no later caller can accidentally ship a whole README to the model.
    """
    from .client import MAX_FILE_BYTES

    if not isinstance(node, dict):
        return None
    text = node.get("text")
    if not isinstance(text, str):
        return None
    if len(text) <= MAX_FILE_BYTES:
        return text
    return text[:MAX_FILE_BYTES] + "\n… [truncated]"
