"""Reading jobs out of a workflow file's text, shared by `estate` and `workflows`.

Workflow files in this estate are written with two-space indentation, so a job
id sits at two spaces and its keys at four. Parsed with regexes rather than a
YAML dependency, for the same reason `parse_catalogue` is not an HCL parser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_JOB_ID = re.compile(r"^  ([A-Za-z0-9_.-]+):\s*$")
_JOB_NAME = re.compile(r"^    name:\s*(.+?)\s*$")
_JOB_USES = re.compile(r"^    uses:\s*\S+")
_JOB_PERMISSIONS = re.compile(r"^    permissions:")
# `strategy:` sits at four spaces and `matrix:` under it at six. A matrix job's
# context carries its leg — `plan (dev)` — which cannot be reconstructed from
# here, so the estate checks leave those jobs alone.
_JOB_MATRIX = re.compile(r"^      matrix:\s*$")

# `on:` at column 0, with anything inline after it, and one event of the block
# form at two spaces.
_ON = re.compile(r"""^["']?on["']?:\s*(.*?)\s*$""")
_EVENT = re.compile(r"""^  ["']?([A-Za-z_]+)["']?:\s*$""")


@dataclass(frozen=True)
class Job:
    """One job, as much of it as the checks depend on."""

    id: str
    name: str | None = None
    # Whether the job calls a reusable workflow, which changes the shape of the
    # context entirely: one per job in *that* workflow, namespaced `<id> / <job>`.
    calls: bool = False
    matrix: bool = False
    # Whether the job sets its own `permissions:`, which overrides the
    # workflow's for that job's token.
    scoped: bool = False

    @property
    def context(self) -> str:
        """What this job reports, for a job that reports one name."""
        return self.name or self.id


def jobs(text: str) -> list[Job]:
    """Every job in one workflow file, in the order it is declared."""
    found: list[Job] = []
    in_jobs = False
    job_id: str | None = None
    name: str | None = None
    calls = matrix = scoped = False

    def close() -> None:
        """Record the job just finished, now that its keys have been seen."""
        if job_id is not None:
            found.append(Job(id=job_id, name=name, calls=calls, matrix=matrix, scoped=scoped))

    for line in text.splitlines():
        if not in_jobs:
            in_jobs = line.rstrip() == "jobs:"
            continue
        match = _JOB_ID.match(line)
        if match:
            close()
            job_id, name = match.group(1), None
            calls = matrix = scoped = False
            continue
        if job_id is None:
            continue
        if _JOB_USES.match(line):
            calls = True
        elif _JOB_MATRIX.match(line):
            matrix = True
        elif _JOB_PERMISSIONS.match(line):
            scoped = True
        elif named := _JOB_NAME.match(line):
            name = named.group(1).strip("\"'")
    close()
    return found


def triggers(text: str) -> set[str]:
    """The events a workflow is triggered by, in the inline or the block form."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _ON.match(line)
        if not match:
            continue
        inline = match.group(1).split("#", 1)[0].strip()
        if inline:
            return {event for event in re.split(r"[\s,\[\]]+", inline) if event}
        found = set()
        for rest in lines[index + 1 :]:
            if rest.strip() and not rest.startswith(" "):
                break
            if event := _EVENT.match(rest):
                found.add(event.group(1))
        return found
    return set()
