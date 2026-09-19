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
class PullRequest:
    """One open pull request, as much of it as the checks need."""

    number: int
    title: str
    author: str
    created_at: datetime | None
    url: str = ""
    draft: bool = False

    @property
    def is_renovate(self) -> bool:
        """Whether Renovate opened this.

        Matched on the author login rather than the title, because the title is
        template-driven and a human can write anything. GitHub reports an App's
        login with a `[bot]` suffix on REST and without it on GraphQL, so both
        forms have to be accepted.
        """
        return self.author.lower().removesuffix("[bot]") in {"renovate", "renovate-bot"}


@dataclass(frozen=True)
class Workflow:
    """One GitHub Actions workflow file, with its contents.

    The text is carried because the interesting questions are about what is
    *in* it — whether actions are pinned to a commit, which runner it asks for —
    and a filename answers none of them. It never reaches the triage prompt;
    `llm._user_prompt` sends names only.
    """

    name: str
    text: str = ""


@dataclass(frozen=True)
class RepoSnapshot:
    """Everything the checks are allowed to see about one repository.

    The point of this type is that checks are pure functions of it: they do no
    I/O, so they are tested by constructing one of these directly, with no HTTP
    anywhere near the test.

    The file fields hold text rather than a path or a flag, because both the
    checks and the triage prompt need the content — "has a Renovate config" and
    "has a Renovate config that only extends config:base" are different
    findings. They are truncated at fetch time; see `github/client.py`.
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
    private: bool = False

    # Populated by the GraphQL detail query. Everything below is optional so a
    # snapshot built from the REST list alone stays valid — which is what the
    # `render` path falls back to when the detail query fails.
    renovate_config: str | None = None
    renovate_config_path: str | None = None
    readme: str | None = None
    dockerfile: str | None = None
    workflows: tuple[Workflow, ...] = ()
    # `.terraform.lock.hcl` from the repository root, where there is one.
    terraform_lock: str | None = None
    # Whether `github-repos` declares this repository. None when the
    # catalogue could not be read, which must not read as "unmanaged".
    in_catalogue: bool | None = None
    open_prs: tuple[PullRequest, ...] = ()
    last_release: str | None = None
    last_release_at: datetime | None = None

    @property
    def renovate_prs(self) -> tuple[PullRequest, ...]:
        return tuple(pr for pr in self.open_prs if pr.is_renovate)

    @property
    def workflow_names(self) -> tuple[str, ...]:
        return tuple(w.name for w in self.workflows)


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

    # Filled in by `state.reconcile`, not by the checks. Deliberately separate
    # from `age_days`: that is how long the *fact* has been true, this is how
    # long we have known about it, and conflating them would claim a missing
    # LICENCE appeared on the day the agent first ran.
    first_seen: str | None = None
    is_new: bool = False

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
class TriageUsage:
    """What one triage call consumed, and what is left to pay for the next.

    `cost_usd` is an **estimate** computed from a price table in `llm.py`;
    `balance_usd` is whatever DeepSeek's own balance endpoint reported, which is
    authoritative and needs no table. Both are here because they answer
    different questions — "was this run expensive" and "will next Monday's run
    happen at all".
    """

    model: str
    cache_hit_tokens: int
    cache_miss_tokens: int
    completion_tokens: int
    # Whether DeepSeek's peak multiplier applied, which doubles every rate.
    peak: bool = False
    cost_usd: float | None = None
    # A string, exactly as the API returned it, rather than a float: it is a
    # money value being displayed and never arithmetic, and parsing it would only
    # create a way to be wrong.
    balance_usd: str | None = None

    @property
    def prompt_tokens(self) -> int:
        return self.cache_hit_tokens + self.cache_miss_tokens


@dataclass(frozen=True)
class ScanResult:
    """One run's output, before it becomes an email."""

    repos: tuple[RepoSnapshot, ...] = ()
    findings: tuple[Finding, ...] = field(default_factory=tuple)
    image_tag: str = "unknown"

    # Written by the model, and the only two fields here that are. Empty when
    # triage is switched off or failed, which the digest renders as simply not
    # having a commentary section rather than as an error.
    summary: str = ""
    themes: tuple[str, ...] = ()

    # None when triage did not run. Computed from the API's own usage figures,
    # never written by the model — the same rule as every other number here.
    usage: TriageUsage | None = None

    # Findings a previous run saw that no check produced this time. Carried as
    # `(repo, title)` because by definition there is no longer a Finding to
    # point at.
    resolved: tuple[tuple[str, str], ...] = ()
    # How many findings a suppression held back, so the digest can say so
    # without listing them. A suppression nobody can see is one nobody revisits.
    suppressed_count: int = 0
