"""The root `Dockerfile`, read as text.

Only `Dockerfile` at the repository root is fetched (`github/client.py`), which
is this estate's layout. A repository keeping its Dockerfile anywhere else is
silent here rather than reported as clean or as broken.

Most of the estate already passes both checks — Renovate pins base image
digests and the template runs as a numeric uid — so these are regression
guards for a repository that did not start from the template.
"""

from __future__ import annotations

import re

from ..models import Finding, RepoSnapshot

# `FROM [--platform=...] image [AS name]`, case-insensitive as Docker is.
_FROM = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE)
_USER = re.compile(r"^\s*USER\s+(\S+)", re.IGNORECASE)
_ROOT_USERS = frozenset({"root", "0", "0:0", "root:root"})


def unpinned_base_image(snapshot: RepoSnapshot) -> list[Finding]:
    """Base images referenced by a tag rather than a digest.

    A tag is mutable in exactly the way an action tag is: the next build pulls
    whatever the publisher pushed under it. Stage aliases (`FROM base AS
    runtime`), `scratch` and `$ARG`-templated images have nothing to pin and
    are skipped.
    """
    if not snapshot.dockerfile:
        return []

    stages: set[str] = set()
    unpinned: list[str] = []
    for line in snapshot.dockerfile.splitlines():
        match = _FROM.match(line)
        if not match:
            continue
        image, alias = match.group(1), match.group(2)
        if alias:
            stages.add(alias.lower())
        if image.lower() in stages or image == "scratch" or "$" in image:
            continue
        if "@sha256:" not in image:
            unpinned.append(image)

    if not unpinned:
        return []
    return [
        Finding(
            repo=snapshot.full_name,
            check="container.unpinned_base_image",
            severity="medium",
            title=f"{len(unpinned)} base image(s) not pinned to a digest",
            detail=(
                f"{', '.join(unpinned)}. A tag can move under the next build. Pin as "
                "`image:tag@sha256:...` and let Renovate keep the digest current."
            ),
            evidence_url=f"{snapshot.url}/blob/{snapshot.default_branch}/Dockerfile",
        )
    ]


def runs_as_root(snapshot: RepoSnapshot) -> list[Finding]:
    """The image's final stage runs as root.

    Only the final stage matters — it is the one that ships — so a `USER` set in
    a builder stage does not count. No `USER` at all means root.
    """
    if not snapshot.dockerfile:
        return []

    user: str | None = None
    seen_from = False
    for line in snapshot.dockerfile.splitlines():
        if _FROM.match(line):
            seen_from = True
            user = None
        elif match := _USER.match(line):
            user = match.group(1)

    if not seen_from or (user is not None and user.lower() not in _ROOT_USERS):
        return []
    return [
        Finding(
            repo=snapshot.full_name,
            check="container.runs_as_root",
            severity="medium",
            title="container runs as root",
            detail=(
                ("The final stage sets no `USER`" if user is None else f"`USER {user}`")
                + ", so the process runs as root, and a runtime enforcing `runAsNonRoot` "
                "refuses it. Add a numeric `USER`, as this estate's template does."
            ),
            evidence_url=f"{snapshot.url}/blob/{snapshot.default_branch}/Dockerfile",
        )
    ]
