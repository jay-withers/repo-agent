"""What the scan remembers between runs.

One JSON document in a blob, read whole at the start of a run and written whole
at the end. That is the entire access pattern, which is why this is a blob and
not a table: there is no query to serve, and a weekly job with `parallelism = 1`
has no concurrent writer to arbitrate.

It answers two questions the findings alone cannot:

- **What is new?** A digest that reprints the same forty lines every Monday
  teaches you to ignore it by about week three.
- **What have I already decided to live with?** A suppression is a recorded
  decision, with a reason and optionally an expiry, so "we know, it's fine" stops
  being re-litigated weekly.

**Absent state is not an error.** No container configured, a first run, a
deleted blob, a transient failure — all of them degrade to "everything looks
new" and the scan continues. The findings are the product; the deltas are
commentary on them, exactly like the triage step.

`Finding.id` is what ties a finding across runs: a hash of `repo:check`, derived
rather than stored so that two scans of an unchanged repository agree. Nothing
here works without that property.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any

from .models import Finding
from .settings import credential, settings

logger = logging.getLogger(__name__)

# The document's own name inside the container.
BLOB_NAME = "state.json"

# Bumped when the shape changes incompatibly. A document from the future is left
# alone rather than overwritten, because the alternative is a newer deployment
# silently discarding what it could not read.
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class KnownFinding:
    """A finding a previous run saw, and when it first saw it.

    `repo`, `check` and `title` are stored alongside the date purely so a
    *resolved* finding can be named in the digest — by the time it is resolved
    the check no longer produces it, so there is nothing else left to describe
    it with.
    """

    first_seen: str
    repo: str = ""
    check: str = ""
    title: str = ""


@dataclass(frozen=True)
class Suppression:
    """A decision to stop reporting something, and why."""

    reason: str
    # ISO date, or None for indefinitely. An expiry is worth offering because
    # most "we'll live with it" decisions are really "not this quarter".
    until: str | None = None
    added: str = ""

    def active(self, today: date) -> bool:
        if self.until is None:
            return True
        try:
            return date.fromisoformat(self.until) >= today
        except ValueError:
            # An unparseable expiry expires. A typo must not suppress a finding
            # for ever, which is the failure mode that is hard to notice.
            logger.warning("suppression expiry %r is not a date, ignoring it", self.until)
            return False


@dataclass(frozen=True)
class State:
    """Everything carried from one run to the next."""

    findings: dict[str, KnownFinding] = field(default_factory=dict)
    suppressed: dict[str, Suppression] = field(default_factory=dict)
    last_run: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": SCHEMA_VERSION,
                "last_run": self.last_run,
                "findings": {k: asdict(v) for k, v in sorted(self.findings.items())},
                "suppressed": {k: asdict(v) for k, v in sorted(self.suppressed.items())},
            },
            indent=2,
            sort_keys=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> State:
        """Parse a state document, tolerating anything but invalid JSON.

        Every field is optional and defaulted: a document written by an older
        version, or edited by hand, should cost at most the fields it is missing.
        """
        payload: dict[str, Any] = json.loads(raw)
        version = payload.get("version", SCHEMA_VERSION)
        if version > SCHEMA_VERSION:
            raise ValueError(
                f"state document is version {version}, this build understands "
                f"{SCHEMA_VERSION}. Refusing to read it rather than overwriting it."
            )
        return cls(
            findings={
                key: KnownFinding(
                    **{k: v for k, v in value.items() if k in KnownFinding.__annotations__}
                )
                for key, value in (payload.get("findings") or {}).items()
                if isinstance(value, dict) and value.get("first_seen")
            },
            suppressed={
                key: Suppression(
                    **{k: v for k, v in value.items() if k in Suppression.__annotations__}
                )
                for key, value in (payload.get("suppressed") or {}).items()
                if isinstance(value, dict)
            },
            last_run=payload.get("last_run"),
        )


@dataclass(frozen=True)
class Reconciled:
    """One run's findings, placed against what the last run saw."""

    findings: tuple[Finding, ...] = ()
    resolved: tuple[KnownFinding, ...] = ()
    suppressed: tuple[Finding, ...] = ()
    state: State = field(default_factory=State)

    @property
    def new(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.is_new)


def reconcile(state: State, findings: list[Finding], today: date | None = None) -> Reconciled:
    """Pure: annotate findings against `state` and produce the state to store.

    No I/O, so this is tested from literals — the same rule as the checks. Three
    things come out of it: the findings to report (annotated with when each was
    first seen), the ones that have gone since last run, and the ones a
    suppression is holding back.
    """
    moment = today or datetime.now(UTC).date()
    stamp = moment.isoformat()

    reported: list[Finding] = []
    held: list[Finding] = []
    carried: dict[str, KnownFinding] = {}

    for finding in findings:
        known = state.findings.get(finding.id)
        first_seen = known.first_seen if known else stamp
        annotated = replace(finding, first_seen=first_seen, is_new=known is None)

        carried[finding.id] = KnownFinding(
            first_seen=first_seen,
            repo=finding.repo,
            check=finding.check,
            title=finding.title,
        )

        rule = state.suppressed.get(finding.id)
        if rule is not None and rule.active(moment):
            held.append(annotated)
        else:
            reported.append(annotated)

    # Anything the previous run knew about that no check produced this time.
    # Suppressed findings are still *found*, so they are not resolved.
    resolved = tuple(
        known for finding_id, known in sorted(state.findings.items()) if finding_id not in carried
    )

    # Expired and orphaned suppressions are dropped rather than carried for ever,
    # so the document does not accumulate rules for findings that no longer exist.
    kept = {
        key: rule
        for key, rule in state.suppressed.items()
        if rule.active(moment) and key in carried
    }

    return Reconciled(
        findings=tuple(reported),
        resolved=resolved,
        suppressed=tuple(held),
        state=State(findings=carried, suppressed=kept, last_run=datetime.now(UTC).isoformat()),
    )


def load() -> State:
    """Read the state document, or return an empty one.

    Never raises. Every reason for failing to read — nothing configured, first
    run ever, a deleted blob, a permissions problem, a transient error — means
    the same thing to the caller: treat everything as new.
    """
    blob = _blob()
    if blob is None:
        return State()

    from azure.core.exceptions import ResourceNotFoundError

    try:
        raw = blob.download_blob().readall()
    except ResourceNotFoundError:
        logger.info("no state document yet; every finding will report as new")
        return State()
    except Exception as exc:
        logger.warning("could not read state, treating every finding as new: %s", exc)
        return State()

    try:
        state = State.from_json(raw.decode("utf-8"))
    except (ValueError, TypeError) as exc:
        logger.warning("state document is unreadable, ignoring it: %s", exc)
        return State()

    logger.info(
        "state: %d known finding(s), %d suppression(s), last run %s",
        len(state.findings),
        len(state.suppressed),
        state.last_run or "never",
    )
    return state


def save(state: State) -> bool:
    """Write the state document, reporting whether it landed.

    Never raises, for the same reason `load` does not: failing to remember this
    run costs next week's deltas, and must not cost this week's digest — which
    by the time this is called has already been built.
    """
    blob = _blob()
    if blob is None:
        return False
    try:
        blob.upload_blob(state.to_json().encode("utf-8"), overwrite=True)
    except Exception as exc:
        logger.warning("could not write state; next run will see everything as new: %s", exc)
        return False
    return True


def _blob() -> Any:
    """A client for the state document, or None when storage is not configured.

    Imported lazily so that the models, the checks and their tests never need
    the Azure SDK present — the same reason `settings.py` defers its imports.
    """
    url = settings().state_container_url
    if not url:
        return None

    from azure.storage.blob import BlobClient

    return BlobClient.from_blob_url(f"{url.rstrip('/')}/{BLOB_NAME}", credential=credential())
