"""Bind a worker's delivery handoff to a durable continuation receipt (#5144).

## The defect this exists to close

A worker process can exit 0 while delivery is unfinished, and before this module
nothing durable recorded that. The mechanism is precise rather than theoretical:

* ``lib/status_gateway_client.record_status()`` returns ``None`` — it discards the
  gateway's parsed response entirely.
* ``lib/invocation_status.py`` calls it inside ``try/except StatusGatewayError`` and
  logs ``"Failed to update invocation status via gateway (non-fatal)"``, then
  continues.

That fail-soft is **correct for advisory status**: a lost dashboard transition must
never abort a run in flight. It is **wrong for an authoritative handoff**, where a
swallowed failure is exactly how a run comes to look complete while review, deploy
or evaluation are still outstanding. So the two concerns are separated rather than
merged — the advisory path keeps its fail-soft contract byte for byte, and the
authoritative handoff lives here with a strict success/readback result.

## Why the server resolves the work, and the worker names none of it

The worker sends no tenant, node, cycle, plan version or claim generation. It
cannot: every hosted worker assumes the same platform role, so a self-declared
identifier reduces to "whatever the caller typed" and any worker could commit a
handoff against any run whose identifiers it could guess. The caller is identified
from its run credential and TokenReview-bound pod, and every authority fence is
then read from protected state the worker cannot write.

This is the same property ``pr_binding_routes`` and ``registration_routes`` rely
on, stated once more because it is the reason this module takes an
:class:`ExecutionIdentity` resolved by its caller rather than any request field.

## Receipt and continuation commit together, or not at all

:func:`commit_handoff` performs **one** write through
``execution_store.advance_execution``, which already carries a
``handoff_receipt_ref`` alongside the phase, status and next-check time. That is
deliberate reuse rather than new storage: the column exists in migration 052 and
the store's own docstring names this issue as its consumer, so no schema change is
required and no second transaction can interleave.

Splitting them would produce exactly the two failure shapes the story forbids:

* receipt without continuation — delivery reads as finished while work remains;
* continuation without receipt — the proof is gone and a repeat cannot converge.

The continuation is always non-terminal. This module never writes
``CONCLUDED``/``SUPERSEDED``: a handoff means *another party still owes something*,
so the row keeps a ``next_check_at`` and stays visible to pickup. A worker's green
exit can no longer express lane completion.

## Idempotency, and what the receipt is keyed by

The receipt reference is derived from the **work and its authority fences** — never
from the attempt number, a timestamp or a fresh UUID. So a repeated report or a
lost response converges on the identical receipt instead of minting a second one or
advancing a counter. This follows ``execution_runner.OperationIdentity``'s rule for
the same reason it does: a key derived per attempt turns every retry into a second
apparent delivery.

The generation *is* part of the key, and that is not a contradiction. A legitimate
handover advances the generation, and the new owner's handoff is genuinely
different work-ownership — so it earns its own receipt, while a repeat by the same
owner at the same generation gets the same string back.

## Readback proves the receipt belongs to the current attempt

:func:`commit_handoff` returns the stored receipt read from the row it just wrote,
and the authority fences are compared **at the point of use** inside the store's
locked write rather than once on the way in. That ordering is the whole point: this
effort has repeatedly produced fences that read a value before slow work and
trusted it after. ``advance_execution`` takes the row lock, calls
``_binding_conflict`` and ``_adopt_generation`` under it, and refuses a superseded
generation there — so a receipt from a stale attempt is refused rather than
accepted, and this module reports that refusal instead of a success.

A receipt whose stored value disagrees with the one this attempt would mint is
reported as :data:`HandoffOutcome.SUPERSEDED`, not silently overwritten.

## Adoption is explicit, refusable, and never automatic

:func:`adopt_legacy_lane` transfers a lane the older mechanism owns. It does not
migrate current work, grants no new database privilege, and is **disabled by
default**. It delegates to ``work_claims.force_handover``, reusing that primitive's
four existing guards (a recorded decision, a positive ``exited`` verdict where both
``live`` and ``unverifiable`` block, reconciled effects, reconciled credentials)
rather than building a second transfer path beside it.

If safe transfer cannot be verified, the caller persists a **typed block** through
the store's existing ``BlockRecord`` vocabulary — ``OWNERSHIP_LOST`` or
``AUTHORITY_UNVERIFIABLE`` — rather than declaring delivery complete. A resident
coordinator and the engine may never own the same lane at once, which is why
adoption goes through ``force_handover``'s release-then-reclaim shape instead of
splicing a replacement owner into a held row.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from .execution_state import (
    BlockCode,
    BlockRecord,
    ExecutionIdentity,
    ExecutionPhase,
    ExecutionRecord,
    ExecutionStatus,
    OutcomeKind,
    PhaseAdvance,
)
from .execution_store import advance_execution, load_execution

logger = logging.getLogger(__name__)

__all__ = [
    "ADOPTION_ENABLED_ENV",
    "HANDOFF_RECEIPT_SCHEME",
    "AdoptionRefusedError",
    "HandoffOutcome",
    "HandoffResult",
    "adopt_legacy_lane",
    "adoption_enabled",
    "commit_handoff",
    "handoff_receipt_ref",
    "outstanding_block",
]

# The scheme prefix every receipt carries. Present so a stored value can be
# recognised as a handoff receipt without parsing it, and so a value written by
# some other writer into the same column cannot be mistaken for one.
HANDOFF_RECEIPT_SCHEME = "handoff"

# Read per call, never at import: adoption must stay disabled until the accepting
# server and the new worker cohort are both deployed and verified compatibly.
ADOPTION_ENABLED_ENV = "ADP_ENGINE_ADOPTION_ENABLED"

# How long a committed continuation waits before the runner looks again. A handoff
# is followed by *someone else's* work (review, deploy, evaluation), so this is a
# revisit interval and not a retry backoff; the runner may reschedule it later.
_CONTINUATION_SECONDS = 300

# `OrchestrationExecution.handoff_receipt_ref` is String(255). A receipt that
# silently truncated would compare unequal to itself on readback, which would turn
# every repeat into a superseded refusal.
_MAX_RECEIPT_CHARS = 255


class HandoffOutcome(StrEnum):
    """What happened to a handoff report.

    Distinct values rather than a bool because the caller's obligations differ:
    ``COMMITTED``/``ALREADY_COMMITTED`` may report success, and everything else may
    **not** — the worker must leave delivery unfinished instead.
    """

    COMMITTED = "committed"  # Receipt and continuation are newly durable
    ALREADY_COMMITTED = "already_committed"  # The identical receipt was already stored
    SUPERSEDED = "superseded"  # Another attempt/generation owns this work now
    STALE = "stale"  # The row moved mid-flight; re-read and retry
    REFUSED = "refused"  # Authority could not be verified; fail closed


# The only outcomes a worker may treat as an accepted handoff. Named as a set so a
# caller cannot accidentally admit a new outcome by writing `!= SUPERSEDED`.
_ACCEPTED: frozenset[HandoffOutcome] = frozenset({HandoffOutcome.COMMITTED, HandoffOutcome.ALREADY_COMMITTED})


@dataclass(frozen=True)
class HandoffResult:
    """The strict result a handoff report returns.

    Deliberately unlike ``record_status()``'s ``None``. A caller cannot mistake
    this for success: :attr:`accepted` is false unless the receipt is genuinely
    durable, and :attr:`receipt_ref` is the value read back from the committed row
    rather than the one the caller hoped to write.
    """

    outcome: HandoffOutcome
    receipt_ref: str | None = None
    record: ExecutionRecord | None = None
    reason: str | None = None

    @property
    def accepted(self) -> bool:
        """True only when the receipt is durable and belongs to this attempt."""
        return self.outcome in _ACCEPTED and bool(self.receipt_ref)


class AdoptionRefusedError(RuntimeError):
    """A legacy lane could not be adopted safely.

    Carries a stable ``code`` so the caller can persist a typed block rather than
    inferring one from message text.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def handoff_receipt_ref(identity: ExecutionIdentity, execution_id: str) -> str:
    """The receipt reference this work and this ownership generation mint.

    Derived from the work plus every authority fence, and **never** from the
    attempt count, wall clock or a random value — that is what makes a repeated
    report return the same string instead of a second receipt.

    Raises:
        ValueError: the reference would exceed the column bound. Raised rather
            than truncated: a truncated receipt compares unequal to itself on
            readback, which would report every repeat as superseded.
    """
    ref = (
        f"{HANDOFF_RECEIPT_SCHEME}:execution={execution_id}:cycle={identity.cycle}:"
        f"plan={identity.accepted_plan_version}:claim={identity.claim_id}:"
        f"generation={identity.claim_generation}"
    )
    if len(ref) > _MAX_RECEIPT_CHARS:
        raise ValueError("handoff receipt reference exceeds the 255-character ledger column limit")
    return ref


def outstanding_block(code: BlockCode, *, owner: str, required_input: str, detail: str | None = None) -> BlockRecord:
    """A typed block for a handoff that could not be verified.

    Exists so the refusal paths persist a block naming a resolvable condition and
    an owner, instead of the caller inventing free text or — far worse — treating
    an unverifiable handoff as delivery.
    """
    return BlockRecord(code=code, owner=owner, required_input=required_input, detail=detail)


async def commit_handoff(
    session: AsyncSession,
    *,
    identity: ExecutionIdentity,
    phase: ExecutionPhase = ExecutionPhase.AWAITING_REVIEW,
    now: datetime,
    next_check_at: datetime | None = None,
    progress_note: str | None = None,
) -> HandoffResult:
    """Atomically commit an idempotent handoff receipt plus a due continuation.

    The server resolves nothing from the caller's assertions: `identity` must
    already have been derived from protected state (the run credential's tenant and
    the execution record's node/cycle/plan/claim), which is why this function takes
    no worker-supplied field at all.

    Commits nothing and makes no external call — the caller owns the transaction
    boundary, matching every other function in the execution store.

    The continuation is always non-terminal, so a committed handoff can never read
    as lane completion. A superseded generation is refused here rather than written,
    and the refusal is reported instead of a success.

    Returns:
        :class:`HandoffResult`. Only ``accepted`` results license the caller to
        report an accepted handoff.
    """
    current = await load_execution(session, identity=identity)
    if current is None:
        # No row at all: there is nothing to hand off, and inventing one here would
        # let a caller create work that no policy admitted.
        return HandoffResult(outcome=HandoffOutcome.REFUSED, reason="unknown_execution")
    if current.kind is OutcomeKind.CONFLICT:
        # Authority mismatch. `record` may be withheld for a tenant mismatch, which
        # is the store's disclosure rule and is preserved rather than worked around.
        return HandoffResult(outcome=HandoffOutcome.SUPERSEDED, record=current.record, reason=current.reason)
    record = current.record
    if record is None:
        return HandoffResult(outcome=HandoffOutcome.REFUSED, reason=current.reason or "unreadable_execution")

    expected = handoff_receipt_ref(identity, record.id)

    if record.handoff_receipt_ref:
        # A receipt is already stored. Converge only when it is *this* work and this
        # ownership generation's receipt; a different one means the row belongs to
        # another attempt now, and returning it as success is precisely the
        # stale-attempt acceptance the story forbids.
        if record.handoff_receipt_ref == expected:
            return HandoffResult(
                outcome=HandoffOutcome.ALREADY_COMMITTED,
                receipt_ref=record.handoff_receipt_ref,
                record=record,
                reason="handoff_already_committed",
            )
        logger.info(
            "handoff: refusing report on execution %s — stored receipt belongs to another attempt",
            record.id,
        )
        return HandoffResult(
            outcome=HandoffOutcome.SUPERSEDED,
            record=record,
            reason="handoff_receipt_superseded",
        )

    if record.status in {ExecutionStatus.CONCLUDED, ExecutionStatus.SUPERSEDED}:
        # Terminal already. Writing a continuation onto it would resurrect work the
        # engine has finished with, and a handoff receipt is meaningless there.
        return HandoffResult(outcome=HandoffOutcome.REFUSED, record=record, reason="execution_already_terminal")

    outcome = await advance_execution(
        session,
        identity=identity,
        advance=PhaseAdvance(
            phase=phase,
            # AWAITING_EXTERNAL, never CONCLUDED: the handoff says another party
            # owes the next step, so the row must stay due and visible to pickup.
            status=ExecutionStatus.AWAITING_EXTERNAL,
            expected_revision=record.revision,
            next_check_at=next_check_at or now + timedelta(seconds=_CONTINUATION_SECONDS),
            progress_note=progress_note,
        ),
        # The receipt rides the same write as the phase/status/next-check move, so
        # the two cannot diverge. This is the atomicity the story requires.
        handoff_receipt_ref=expected,
    )

    if outcome.kind is OutcomeKind.STALE:
        # The row moved between the read and the write. Nothing was written; the
        # caller must re-read. Reported as STALE rather than retried here, because
        # whatever moved the row may have changed what should happen next.
        return HandoffResult(outcome=HandoffOutcome.STALE, record=outcome.record, reason=outcome.reason)
    if outcome.kind is OutcomeKind.CONFLICT:
        return HandoffResult(outcome=HandoffOutcome.SUPERSEDED, record=outcome.record, reason=outcome.reason)
    if outcome.kind is not OutcomeKind.APPLIED:
        # BLOCKED is not reachable here (no block is passed) but must not fall
        # through to success if it ever becomes so.
        return HandoffResult(outcome=HandoffOutcome.REFUSED, record=outcome.record, reason=outcome.reason)

    committed = outcome.record
    stored = committed.handoff_receipt_ref if committed else None
    if stored != expected:
        # Readback disagreement. Reported as a refusal rather than trusted, because
        # a receipt this attempt cannot confirm is not evidence of a handoff.
        logger.warning("handoff: readback mismatch on execution %s", record.id)
        return HandoffResult(outcome=HandoffOutcome.REFUSED, record=committed, reason="handoff_readback_mismatch")

    logger.info("handoff: committed receipt and due continuation for execution %s", record.id)
    return HandoffResult(outcome=HandoffOutcome.COMMITTED, receipt_ref=stored, record=committed)


def adoption_enabled() -> bool:
    """Whether adopting a legacy lane is permitted in this deployment.

    Defaults to **false**, and is read per call rather than at import so a test and
    a rollback do not depend on module import order.

    Staged deployment is the reason this flag exists: the accepting server must be
    deployed before the new worker and **both** revisions verified compatibly, so
    enabling adoption is a later milestone that this code deliberately cannot
    declare for itself.
    """
    return os.environ.get(ADOPTION_ENABLED_ENV, "false").lower() == "true"


async def adopt_legacy_lane(
    session: AsyncSession,
    *,
    org_id: str,
    claim_id: str,
    decision_id: str,
    resolver,
    effects_reconciled: bool,
    credentials_reconciled: bool,
    accepted_plan_version: int,
    now: datetime | None = None,
):
    """Transfer one exited, reconciled legacy lane to the engine. Never automatic.

    Delegates the transfer itself to ``work_claims.force_handover`` rather than
    reimplementing it, so this path inherits that primitive's guards instead of
    adding a second, weaker set beside them: a recorded decision, a positive
    ``exited`` liveness verdict (both ``live`` and ``unverifiable`` block), and
    explicit reconciliation of outstanding effects and the prior owner's
    credentials.

    ``force_handover`` leaves the claim ``RELEASED`` at ``generation + 1`` with no
    active run, so the adopter reclaims through the ordinary admission path. That
    release-then-reclaim shape is what keeps a resident coordinator and the engine
    from ever holding one lane simultaneously — a splice that installed a new owner
    directly into a held row could not offer that guarantee.

    Grants no new database privilege and migrates no current work.

    Raises:
        AdoptionRefusedError: adoption is disabled, no policy is accepted, or the
            transfer was refused. The caller persists a typed block; it must not
            treat a refusal as delivery completion.
    """
    from .work_claims import Disposition, WorkClaimError, force_handover

    if not adoption_enabled():
        raise AdoptionRefusedError(
            "adoption_disabled",
            "Legacy-lane adoption is disabled in this deployment; the accepting server and worker "
            "revisions must be deployed and verified compatibly first.",
        )
    if accepted_plan_version < 1:
        # Policy-bound only. Adopting a lane with no accepted policy would place
        # work under engine ownership that no policy ever admitted.
        raise AdoptionRefusedError(
            "policy_not_accepted",
            "Adoption requires an accepted policy version; this lane has none.",
        )
    if not effects_reconciled or not credentials_reconciled:
        # Checked here as well as inside `force_handover` so the refusal names which
        # reconciliation is outstanding, which is what an operator has to act on.
        raise AdoptionRefusedError(
            "reconciliation_outstanding",
            "Adoption requires reconciliation of the prior owner's outstanding effects and credentials.",
        )

    try:
        receipt = await force_handover(
            session,
            org_id=org_id,
            claim_id=claim_id,
            decision_id=decision_id,
            resolver=resolver,
            effects_reconciled=effects_reconciled,
            credentials_reconciled=credentials_reconciled,
            now=now,
        )
    except WorkClaimError as exc:
        raise AdoptionRefusedError(exc.code, exc.message) from None

    if receipt.disposition is not Disposition.ADMITTED:
        # Includes the liveness arms: a prior owner still live, or one whose exit
        # could not be established, both refuse here rather than transferring.
        raise AdoptionRefusedError(
            receipt.reason or "handover_refused",
            f"Legacy lane {claim_id} could not be adopted: {receipt.reason or 'handover refused'}.",
        )
    # `force_handover` records ReleaseReason.HANDOVER itself and leaves the row
    # released at the next generation; the adopter reclaims through normal admission.
    logger.info("handoff: adopted legacy lane %s at generation %s", claim_id, receipt.generation)
    return receipt
