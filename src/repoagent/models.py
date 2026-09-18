"""The shapes the scan produces.

Frozen dataclasses rather than Pydantic models: nothing here crosses a wire or
is parsed from untrusted input — `RepoSnapshot` is built from GitHub's JSON by
`github/client.py`, and `Finding` is built by the checks. Pydantic earns its
place where a schema is sent to a model or validated on the way in, which is
the LLM triage step that does not exist yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class RepoSnapshot:
    """Everything the checks are allowed to see about one repository.

    The point of this type is that checks are pure functions of it: they do no
    I/O, so they are tested by constructing one of these directly, with no HTTP
    anywhere near the test.
    """

    name: str
    full_name: str
    description: str | None
    default_branch: str
    archived: bool
    pushed_at: datetime | None
    topics: tuple[str, ...] = ()
    has_license: bool = False
    has_readme: bool = False
    url: str = ""


@dataclass(frozen=True)
class Finding:
    """One thing worth a human's attention, about one repository."""

    repo: str
    check: str
    severity: str
    title: str
    detail: str
    evidence_url: str = ""
    # Days since the underlying fact became true, where GitHub gives a
    # timestamp to derive it from. None when the fact has no age — a missing
    # LICENSE has always been missing.
    age_days: int | None = None

    @property
    def id(self) -> str:
        """A stable identity for this finding, for de-duplication later.

        Deliberately derived rather than stored: two scans of an unchanged
        repository must produce the same id, and anything involving the run's
        own timestamp would not.
        """
        import hashlib

        return hashlib.sha256(f"{self.repo}:{self.check}".encode()).hexdigest()[:12]


@dataclass(frozen=True)
class ScanResult:
    """One run's output, before it becomes an email."""

    repos: tuple[RepoSnapshot, ...] = ()
    findings: tuple[Finding, ...] = field(default_factory=tuple)
    image_tag: str = "unknown"
