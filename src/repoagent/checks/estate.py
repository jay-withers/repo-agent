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


def _producible_contexts(text: str) -> tuple[set[str], set[str]]:
    """The contexts one workflow file can report: direct names, and caller ids.

    Two sets because they are checked differently. A normal job reports its
    `name`, or its id where it has none. A job that calls a reusable workflow
    reports one context per job *in that workflow*, namespaced
    `<caller job id> / <reusable job name>` — and the second half lives in
    another repository, so only the caller id can be checked from here.
    """
    direct: set[str] = set()
    callers: set[str] = set()
    in_jobs = False
    job_id: str | None = None
    job_name: str | None = None
    calls = False

    def close() -> None:
        """Record the job just finished, now that its keys have been seen."""
        if job_id is None:
            return
        if calls:
            callers.add(job_id)
        else:
            direct.add(job_name or job_id)

    for line in text.splitlines():
        if not in_jobs:
            in_jobs = line.rstrip() == "jobs:"
            continue
        match = _JOB_ID.match(line)
        if match:
            close()
            job_id, job_name, calls = match.group(1), None, False
            continue
        if job_id is None:
            continue
        if _JOB_USES.match(line):
            calls = True
        elif name := _JOB_NAME.match(line):
            job_name = name.group(1).strip("\"'")
    close()
    return direct, callers


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
