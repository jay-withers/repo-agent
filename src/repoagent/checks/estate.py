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
