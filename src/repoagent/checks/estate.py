"""Checks about a repository's place in the estate, not its contents.

These are the ones that pay for themselves here, because this estate is
*declared* somewhere: `jay-withers/github-repos` holds a catalogue of every
repository and applies branch protection and required checks from it. A
repository missing from that file is not merely undocumented — nothing is
enforcing anything on it.

Checking presence of settings that `github-repos` already declares would mostly
re-report its own Terraform. The useful inversion is coverage: what exists on
GitHub that the catalogue does not know about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import Finding, RepoSnapshot

# Renovate policy is centralised in one preset repository. A config extending it
# inherits every later change; one that does not has silently opted out.
SHARED_PRESET_MARKER = "jay-withers/renovate"


def unmanaged(snapshot: RepoSnapshot) -> list[Finding]:
    """On GitHub, but absent from the catalogue that configures everything.

    `in_catalogue` is a tri-state on purpose. None means the catalogue could not
    be read, and must never render as "every repository is unmanaged" — which is
    what a plain boolean would produce the first time the file moved.
    """
    if snapshot.in_catalogue is not False:
        return []
    return [
        Finding(
            repo=snapshot.full_name,
            check="estate.unmanaged",
            severity="high",
            title="not in the github-repos catalogue",
            detail=(
                "No entry in github-repos' terraform.tfvars, so nothing applies branch "
                "protection, required status checks, description or topics to it. It is "
                "outside the estate's controls rather than configured differently."
            ),
            evidence_url="https://github.com/jay-withers/github-repos",
        )
    ]


def not_shared_preset(snapshot: RepoSnapshot) -> list[Finding]:
    """A Renovate config that does not extend the estate's shared preset.

    Distinct from `renovate.default_config_only`: this one *has* made choices,
    just locally rather than inheriting the central ones. That is defensible for
    a repository with genuinely unusual needs, which is why it is low severity —
    the finding is the divergence, not a verdict on it.
    """
    config = snapshot.renovate_config
    if config is None or SHARED_PRESET_MARKER in config:
        return []
    return [
        Finding(
            repo=snapshot.full_name,
            check="estate.not_shared_preset",
            severity="low",
            title="Renovate config does not extend the shared preset",
            detail=(
                f"{snapshot.renovate_config_path} does not extend "
                f"github>{SHARED_PRESET_MARKER}, so changes to the estate's central "
                "Renovate policy never reach this repository."
            ),
            evidence_url=(
                f"{snapshot.url}/blob/{snapshot.default_branch}/{snapshot.renovate_config_path}"
            ),
        )
    ]


# Workflow files in this estate are written with two-space indentation, so a job
# id sits at two spaces and its keys at four. Parsed with regexes rather than a
# YAML dependency, for the same reason `parse_catalogue` is not an HCL parser.
_JOB_ID = re.compile(r"^  ([A-Za-z0-9_.-]+):\s*$")
_JOB_NAME = re.compile(r"^    name:\s*(.+?)\s*$")
_JOB_USES = re.compile(r"^    uses:\s*\S+")
# `strategy:` sits at four spaces and `matrix:` under it at six. A matrix job's
# context carries its leg — `plan (dev)` — which cannot be reconstructed from
# here, so both checks below leave those jobs alone.
_JOB_MATRIX = re.compile(r"^      matrix:\s*$")


@dataclass(frozen=True)
class _Job:
    """One job, as much of it as a status check context depends on."""

    id: str
    name: str | None = None
    # Whether the job calls a reusable workflow, which changes the shape of the
    # context entirely: one per job in *that* workflow, namespaced `<id> / <job>`.
    calls: bool = False
    matrix: bool = False

    @property
    def context(self) -> str:
        """What this job reports, for a job that reports one name."""
        return self.name or self.id


def _jobs(text: str) -> list[_Job]:
    """Every job in one workflow file, in the order it is declared."""
    found: list[_Job] = []
    in_jobs = False
    job_id: str | None = None
    name: str | None = None
    calls = False
    matrix = False

    def close() -> None:
        """Record the job just finished, now that its keys have been seen."""
        if job_id is not None:
            found.append(_Job(id=job_id, name=name, calls=calls, matrix=matrix))

    for line in text.splitlines():
        if not in_jobs:
            in_jobs = line.rstrip() == "jobs:"
            continue
        match = _JOB_ID.match(line)
        if match:
            close()
            job_id, name, calls, matrix = match.group(1), None, False, False
            continue
        if job_id is None:
            continue
        if _JOB_USES.match(line):
            calls = True
        elif _JOB_MATRIX.match(line):
            matrix = True
        elif named := _JOB_NAME.match(line):
            name = named.group(1).strip("\"'")
    close()
    return found


def _producible_contexts(text: str) -> tuple[set[str], set[str]]:
    """The contexts one workflow file can report: direct names, and caller ids.

    Two sets because they are checked differently. A normal job reports its
    `name`, or its id where it has none. A job that calls a reusable workflow
    reports one context per job *in that workflow*, namespaced
    `<caller job id> / <reusable job name>` — and the second half lives in
    another repository, so only the caller id can be checked from here.
    """
    jobs = _jobs(text)
    return (
        {job.context for job in jobs if not job.calls},
        {job.id for job in jobs if job.calls},
    )


# A workflow only gates a pull request if it is triggered by one, and only gates
# *every* pull request if that trigger carries no path filter. Both halves
# matter: a required check that does not report leaves a pull request pending
# for ever, which is the failure `unreportable_required_check` exists for.
_ON = re.compile(r"""^["']?on["']?:\s*(.*?)\s*$""")
_EVENT = re.compile(r"""^  ["']?([A-Za-z_]+)["']?:\s*$""")
_PATH_FILTER = re.compile(r"^    paths(-ignore)?:")


def _gates_every_pull_request(text: str) -> bool:
    """Whether this workflow runs on every pull request, unconditionally.

    False for a workflow that is not triggered by `pull_request` at all — a
    release or deploy workflow can never be a required check — and false for one
    whose trigger is path filtered, because a pull request touching none of
    those paths never runs it, and a required check that never runs never
    reports. That is exactly why this repository's own `ci-container-build` is
    deliberately not required.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _ON.match(line)
        if not match:
            continue

        # `on: pull_request` or `on: [push, pull_request]`, all on one line.
        inline = match.group(1).split("#", 1)[0].strip()
        if inline:
            return "pull_request" in re.split(r"[\s,\[\]]+", inline)

        # The block form. Events sit at two spaces, their keys at four.
        triggered = False
        in_pull_request = False
        for rest in lines[index + 1 :]:
            if not rest.strip():
                continue
            if not rest.startswith("  "):
                break
            if event := _EVENT.match(rest):
                in_pull_request = event.group(1) == "pull_request"
                triggered = triggered or in_pull_request
            elif in_pull_request and _PATH_FILTER.match(rest):
                return False
        return triggered
    return False


def unreportable_required_check(snapshot: RepoSnapshot) -> list[Finding]:
    """A required status check that no workflow in the repository can report.

    The worst failure mode branch protection has, and a silent one: an absent
    check never fails a pull request, it leaves it *pending*. Nothing is red,
    nothing is merged, and the only way anything lands is a ruleset bypass —
    which is how azure-landingzone required `terraform / Terraform` for weeks
    while its workflows reported `ci-terraform`, and why its Renovate backlog
    reached six.

    Deliberately conservative. For a namespaced context only the caller job id
    is checked, because the job name after the slash is defined in whichever
    repository owns the reusable workflow; this reports a context whose caller
    does not exist at all, not one whose far half has been renamed.
    """
    required = snapshot.required_checks
    # None means the catalogue could not be read, or does not declare this
    # repository — the same tri-state as `in_catalogue`, and the same reason.
    if not required or not snapshot.workflows:
        return []

    direct: set[str] = set()
    callers: set[str] = set()
    for workflow in snapshot.workflows:
        workflow_direct, workflow_callers = _producible_contexts(workflow.text)
        direct |= workflow_direct
        callers |= workflow_callers

    missing = [
        context
        for context in required
        if context not in direct
        and not (" / " in context and context.split(" / ", 1)[0] in callers)
    ]
    if not missing:
        return []

    listed = ", ".join(f"`{context}`" for context in missing)
    return [
        Finding(
            repo=snapshot.full_name,
            check="estate.unreportable_required_check",
            severity="high",
            title=f"{len(missing)} required status check(s) nothing reports",
            detail=(
                f"{listed} required by the github-repos catalogue, and no workflow in "
                "this repository produces that context. A required check that never "
                "reports leaves every pull request pending rather than failing it, so "
                "nothing merges except by bypassing the ruleset."
            ),
            evidence_url=f"{snapshot.url}/tree/{snapshot.default_branch}/.github/workflows",
        )
    ]


def unenforced_check(snapshot: RepoSnapshot) -> list[Finding]:
    """A workflow that gates every pull request, which nothing requires.

    The mirror of `unreportable_required_check`, and the half that actually
    happens: a repository is created from a template with the full set of
    workflows, and its catalogue entry is written with one or two contexts, or
    none at all. The CI is there, it runs, it goes red — and the pull request
    merges anyway, because nothing in the ruleset is waiting on it. With
    `autoApprove` and platform auto-merge on Renovate pull requests, no human
    ever sees the red tick.

    Only jobs that report on **every** pull request are counted, which is what
    makes this safe to act on: a required check that does not always report is
    worse than no required check at all, so path-filtered workflows and matrix
    jobs are left out rather than recommended. For a job calling a reusable
    workflow the caller id is matched as a prefix, for the same reason the
    other direction only checks the caller id — the job names after the slash
    live in another repository.
    """
    required = snapshot.required_checks
    # None means the catalogue could not be read, or does not declare this
    # repository at all. Neither is a claim about its ruleset, and
    # `estate.unmanaged` already reports the second. An *empty* tuple is a
    # claim: declared, and requiring nothing — which is the case this exists for.
    if required is None or not snapshot.workflows:
        return []

    unenforced: list[tuple[str, str]] = []
    for wf in snapshot.workflows:
        if not _gates_every_pull_request(wf.text):
            continue
        for job in _jobs(wf.text):
            if job.matrix:
                continue
            if job.calls:
                prefix = f"{job.id} / "
                if not any(c == job.id or c.startswith(prefix) for c in required):
                    # The far half is defined in the reusable workflow's own
                    # repository, so it is left unwritten rather than guessed.
                    unenforced.append((f"{job.id} / \u2026", wf.name))
            elif job.context not in required:
                unenforced.append((job.context, wf.name))

    if not unenforced:
        return []

    listed = ", ".join(f"`{context}` ({name})" for context, name in unenforced)
    return [
        Finding(
            repo=snapshot.full_name,
            check="estate.unenforced_check",
            severity="medium",
            title=f"{len(unenforced)} pull request check(s) nothing requires",
            detail=(
                f"{listed} not in this repository's `required_status_checks` in the "
                "github-repos catalogue, so a red run does not block a merge — and a "
                "Renovate pull request, auto-approved and auto-merged, never has a human "
                "to notice. Each runs on every pull request, so each can be required "
                "without leaving one pending. Read the exact context off `gh pr checks` "
                "rather than inferring it."
            ),
            evidence_url="https://github.com/jay-withers/github-repos",
        )
    ]


# Every platform that runs Terraform across this estate: CI is amd64, the dev
# containers and laptops are arm64. CLAUDE.md's remedy locks three —
# linux_amd64, linux_arm64, darwin_arm64 — so three is what a complete lock has.
EXPECTED_LOCK_PLATFORMS = 3

# A `provider "..." {` block header. Each has its own hash list, and a lock can
# be complete for one provider and not another if it was regenerated after a new
# one was added.
_PROVIDER = re.compile(r'^provider\s+"([^"]+)"\s*\{')

# `h1:` is the hash of an *extracted provider package*, so there is exactly one
# per platform locked. `zh:` entries are the registry's zip hashes and are
# present regardless, which is why counting those would prove nothing.
_H1 = re.compile(r'"h1:')


def _platforms_per_provider(lock: str) -> dict[str, int]:
    """How many platforms each provider block is locked for.

    Counted rather than read: **a lock file never names a platform.** An earlier
    version of this check searched the text for `linux_amd64`, which appears in
    no lock file ever written, so it reported every repository in the estate —
    including this one, whose lock is correct — as broken.
    """
    counts: dict[str, int] = {}
    current: str | None = None

    for line in lock.splitlines():
        header = _PROVIDER.match(line)
        if header:
            current = header.group(1)
            counts[current] = 0
        elif current:
            # Occurrences, not presence: real lock files put one hash per line,
            # but nothing requires it and a hand-formatted file would undercount.
            counts[current] += len(_H1.findall(line))
    return counts


def incomplete_terraform_lock(snapshot: RepoSnapshot) -> list[Finding]:
    """A `.terraform.lock.hcl` locked for fewer platforms than run Terraform.

    The failure is specific and this estate has already had it: a lock generated
    on an arm64 laptop carries no amd64 hashes, CI adds one during `init`, and
    the now-modified tracked file fails pre-commit on every pull request until
    someone regenerates it.
    """
    lock = snapshot.terraform_lock
    if not lock:
        return []

    counts = _platforms_per_provider(lock)
    short = {name: n for name, n in counts.items() if n < EXPECTED_LOCK_PLATFORMS}
    if not short:
        return []

    described = ", ".join(
        f"{name.rsplit('/', 1)[-1]} ({n} of {EXPECTED_LOCK_PLATFORMS})"
        for name, n in sorted(short.items())
    )
    return [
        Finding(
            repo=snapshot.full_name,
            check="estate.incomplete_terraform_lock",
            severity="high",
            title=f"{len(short)} provider(s) locked for too few platforms",
            detail=(
                f"{described}. CI runs amd64 and adds the missing hash during `init`, "
                "which modifies a tracked file and fails pre-commit on every pull "
                "request. Regenerate with `terraform providers lock "
                "-platform=linux_amd64 -platform=linux_arm64 -platform=darwin_arm64`."
            ),
            evidence_url=snapshot.url,
        )
    ]
