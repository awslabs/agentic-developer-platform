"""Per-operation run handoff. A static credential projection is not authority.

The Gateway grants spending authority to one approved worker pod for one attempt of
one job, for a bounded time. This controller is a long-lived Deployment, so it is
not that pod. Mounting a Secret containing a run credential does not make it that
pod either: a projection has no operation, attempt or expiry in it, so possession
proves only that someone once wrote a file.

This module reads the separate document that does carry that binding, so the
trusted service can compare an operation the Gateway resolved against the grant it
was actually handed. A bare selector list -- the previous `operations.json` shape --
is refused here rather than treated as a handoff, because accepting it is exactly
the "Secret reference as admission" failure #5536 must not ship.

The producer of this document belongs to #5535. This module is only the consumer,
and it deliberately cannot mint, extend or infer a grant: every field is read from
the document and then re-checked against Gateway's own answer in `registry.verify`.
Refusal is idle, never a hopeful provider call.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from harness_jobs.identity import OperationRefused

HANDOFF_VERSION = 1

# One handoff per admitted operation in the run, matching the selector ceiling.
MAX_HANDOFF_GRANTS = 32


@dataclass(frozen=True)
class RunHandoff:
    """One approved run's authority over one operation, as delivered to this pod.

    `not_after` is the grant's own expiry, independent of the execution lease. Both
    must be live: a grant that outlived its run is not authority, and neither is a
    live grant over a lapsed lease.
    """

    operation_id: str
    attempt_id: str
    job_id: str
    not_after: datetime

    def live(self, now=None):
        return (now or datetime.now(UTC)) < self.not_after


def _text(value, field, limit=255):
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        raise OperationRefused(f"run handoff {field} unavailable")
    return value


def read_handoff(path) -> dict[str, RunHandoff]:
    """Read the run handoff document, refusing anything without per-operation binding.

    Returns grants by operation ID. Raises `OperationRefused` for a missing,
    oversized, malformed, expired or unbound document -- including the bare list a
    static selector projection contains. The caller's response is to stay idle.
    """
    try:
        with Path(path).open("rb") as source:
            raw = source.read(65537)
    except OSError:
        raise OperationRefused("run handoff unavailable") from None
    if len(raw) > 65536:
        raise OperationRefused("run handoff document too large")
    try:
        document = json.loads(raw)
    except ValueError:
        raise OperationRefused("run handoff unreadable") from None
    if isinstance(document, list):
        # The operation selector's shape. It names operations but binds no attempt,
        # job or expiry, so it cannot establish that this pod was granted anything.
        raise OperationRefused(
            "run handoff required: an operation selector or static credential "
            "projection is not admission"
        )
    if not isinstance(document, dict) or document.get("version") != HANDOFF_VERSION:
        raise OperationRefused("unsupported run handoff version")
    grants = document.get("grants")
    if not isinstance(grants, list) or not 1 <= len(grants) <= MAX_HANDOFF_GRANTS:
        raise OperationRefused("run handoff grants unavailable")
    handoffs: dict[str, RunHandoff] = {}
    for grant in grants:
        if not isinstance(grant, dict) or set(grant) != {
            "operation_id",
            "attempt_id",
            "job_id",
            "not_after",
        }:
            # Exact fields, so an unknown key cannot smuggle a second meaning past
            # a reader that only validated the ones it recognized.
            raise OperationRefused("run handoff grant fields unsupported")
        try:
            not_after = datetime.fromisoformat(_text(grant["not_after"], "expiry", 64))
        except (ValueError, TypeError):
            raise OperationRefused("run handoff expiry unavailable") from None
        if not_after.tzinfo is None:
            raise OperationRefused("run handoff expiry requires an explicit offset")
        handoff = RunHandoff(
            _text(grant["operation_id"], "operation"),
            _text(grant["attempt_id"], "attempt"),
            _text(grant["job_id"], "job"),
            not_after,
        )
        if handoff.operation_id in handoffs:
            raise OperationRefused("duplicate run handoff grant")
        handoffs[handoff.operation_id] = handoff
    return handoffs


def live_handoff(handoffs, operation_id) -> RunHandoff:
    """The live grant for one operation, or a refusal. Never a fabricated grant."""
    handoff = handoffs.get(operation_id)
    if handoff is None:
        raise OperationRefused("no run handoff grants this operation")
    if not handoff.live():
        raise OperationRefused("run handoff expired")
    return handoff


def bound_operation(handoff: RunHandoff, operation) -> None:
    """Refuse unless Gateway's resolved operation is the one this grant authorizes.

    The handoff names the attempt and job; the Gateway independently reports the
    attempt and job behind the live lease. Requiring both to agree is what stops a
    copied projection from authorizing a different attempt -- and it never invents
    either value from the operation ID.
    """
    lease = operation.grant.lease
    if (
        handoff.operation_id != lease.operation_id
        or handoff.attempt_id != lease.attempt_id
        or handoff.job_id != operation.job_id
    ):
        raise OperationRefused("run handoff does not authorize this attempt")
