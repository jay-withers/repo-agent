"""GitHub Actions hygiene, read out of the workflow files themselves.

Two things this estate already decided and should be held to: actions are
**pinned by commit SHA with the tag as a comment**, and runners are ones GitHub
still supports. Both are invisible until something breaks — a moved tag is a
supply-chain compromise nobody notices, and a retired runner fails every
workflow on the day it is withdrawn.
"""

from __future__ import annotations

import re

from ..models import Finding, RepoSnapshot

# `uses: owner/repo@ref` or `uses: owner/repo/path@ref`. Local (`./...`) and
# Docker (`docker://...`) references have no ref to pin and are skipped.
_USES = re.compile(r"^\s*-?\s*uses:\s*['\"]?([A-Za-z0-9._-]+/[A-Za-z0-9._/-]+)@([^\s'\"#]+)")

# A 40-character hex string. Anything else — `v4`, `main`, `v4.1.1` — is a
# moving reference whose owner can repoint it at different code.
_SHA = re.compile(r"^[0-9a-f]{40}$")

# `runs-on: <label>`, including the `runs-on: [self-hosted, linux]` list form's
# first element, which is enough to spot a retired image.
_RUNS_ON = re.compile(r"^\s*runs-on:\s*\[?\s*['\"]?([A-Za-z0-9._-]+)")

# Images GitHub has retired or announced the retirement of. Kept as an explicit
# list rather than a "latest" rule, because `ubuntu-latest` is fine and pinning
# to a supported version is also fine — the finding is specifically about images
# that are going away.
RETIRED_RUNNERS = frozenset(
    {
        "ubuntu-18.04",
        "ubuntu-20.04",
        "macos-11",
        "macos-12",
        "windows-2016",
        "windows-2019",
    }
)

# Actions published by GitHub itself under these owners still move their tags,
# but the trust model is different and pinning them is a judgement call rather
# than a defect. Reported at a lower severity instead of ignored.
FIRST_PARTY_OWNERS = frozenset({"actions", "github"})


def unpinned_actions(snapshot: RepoSnapshot) -> list[Finding]:
    """Actions referenced by a tag or branch rather than a commit SHA.

    A tag is mutable. Whoever owns the action can repoint `v4` at anything, and
    every workflow that trusts it runs the new code with whatever secrets the job
    holds. This estate's own convention is a SHA with the tag in a comment — see
    the reusable workflows in `jay-withers/workflows`.
    """
    third_party: dict[str, set[str]] = {}
    first_party: set[str] = set()

    for workflow in snapshot.workflows:
        for line in workflow.text.splitlines():
            match = _USES.match(line)
            if not match:
                continue
            action, ref = match.group(1), match.group(2)
            if _SHA.match(ref):
                continue
            owner = action.split("/", 1)[0]
            if owner in FIRST_PARTY_OWNERS:
                first_party.add(f"{action}@{ref}")
            else:
                third_party.setdefault(workflow.name, set()).add(f"{action}@{ref}")

    if not third_party and not first_party:
        return []

    findings = []
    if third_party:
        total = sum(len(v) for v in third_party.values())
        examples = sorted({a for refs in third_party.values() for a in refs})[:3]
        findings.append(
            Finding(
                repo=snapshot.full_name,
                check="workflows.unpinned_actions",
                severity="high",
                title=f"{total} third-party action(s) pinned to a mutable tag",
                detail=(
                    f"{', '.join(examples)}"
                    f"{' and others' if total > len(examples) else ''}. A tag can be "
                    "repointed by its owner at any time, and the job runs the new code "
                    "with whatever secrets it holds. Pin to a commit SHA with the tag "
                    "as a trailing comment."
                ),
                evidence_url=f"{snapshot.url}/tree/{snapshot.default_branch}/.github/workflows",
            )
        )
    if first_party:
        findings.append(
            Finding(
                repo=snapshot.full_name,
                check="workflows.unpinned_first_party_actions",
                severity="low",
                title=f"{len(first_party)} GitHub-owned action(s) pinned to a tag",
                detail=(
                    f"{', '.join(sorted(first_party)[:3])}. Lower risk than a third-party "
                    "action, since the tag is moved by GitHub itself, but still a moving "
                    "reference."
                ),
                evidence_url=f"{snapshot.url}/tree/{snapshot.default_branch}/.github/workflows",
            )
        )
    return findings


def retired_runners(snapshot: RepoSnapshot) -> list[Finding]:
    """Workflows asking for a runner image GitHub has retired or is retiring."""
    found: dict[str, set[str]] = {}
    for workflow in snapshot.workflows:
        for line in workflow.text.splitlines():
            match = _RUNS_ON.match(line)
            if match and match.group(1) in RETIRED_RUNNERS:
                found.setdefault(match.group(1), set()).add(workflow.name)

    if not found:
        return []

    described = ", ".join(
        f"{image} ({', '.join(sorted(files))})" for image, files in sorted(found.items())
    )
    return [
        Finding(
            repo=snapshot.full_name,
            check="workflows.retired_runners",
            severity="medium",
            title="workflow uses a retired runner image",
            detail=(
                f"{described}. A retired image stops being provisioned on a date GitHub "
                "sets, and every workflow asking for it fails that morning."
            ),
            evidence_url=f"{snapshot.url}/tree/{snapshot.default_branch}/.github/workflows",
        )
    ]
