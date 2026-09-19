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

The receipt reference is derived from the **execution, cycle and authority fences**,
not a timestamp or a fresh UUID. So a repeated report or a
lost response converges on the identical receipt instead of minting a second one or
advancing a counter. This follows ``execution_runner.OperationIdentity``'s rule for
the same reason it does: a key derived per attempt turns every retry into a second
apparent delivery.

The generation *is* part of the key, and that is not a contradiction. A legitimate
handover advances the generation, and the new owner's handoff is genuinely
different work-ownership — so it earns its own receipt, while a repeat by the same
owner at the same generation gets the same string back.

## Attempt, generation and live authority are separate fences

The route resolves the caller's original dispatch through
``resolve_registration_target``. :func:`identity_for_attempt` binds that exact
cycle and rechecks the node's attempt under its row lock. A retry can advance the
attempt without advancing the claim generation, so the generation fence alone
does not prove attempt ownership.

:func:`commit_handoff` returns the stored receipt. ``advance_execution`` compares
plan/claim bindings and generation under its locked write. After the awaited SQL
work, the route refreshes flow and credential authority, compares it with the
original snapshot, and refuses before committing or returning a replayed receipt
if it changed. These checks do not turn the receipt into delivery completion.

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
    TERMINAL_EXECUTION_STATUSES,
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
    "HANDOFF_RECEIPT_CONTRACT_VERSION",
    "HANDOFF_RECEIPT_SCHEME",
    "AdoptionRefusedError",
    "HandoffAction",
    "HandoffOutcome",
    "HandoffReceipt",
    "HandoffResult",
    "OutstandingContinuation",
    "adopt_legacy_lane",
    "adoption_enabled",
    "commit_handoff",
    "current_identity",
    "handoff_action_id",
    "handoff_receipt_ref",
    "identity_for_attempt",
    "handoff_required",
    "missing_receipt_hold",
    "outstanding_block",
    "outstanding_continuation",
    "receipt_for",
]

# The scheme prefix every receipt carries. Present so a stored value can be
# recognised as a handoff receipt without parsing it, and so a value written by
# some other writer into the same column cannot be mistaken for one.
HANDOFF_RECEIPT_SCHEME = "handoff"

# The receipt *contract* version, distinct from `accepted_plan_version` (which is
# the tenant's policy) and from the envelope version. It exists because the worker
# validates field-by-field: a worker that knows version 1 and is answered by a
# server speaking version 2 must refuse rather than silently validate a subset of
# the fields it expected. Bumped only when the field set or its meaning changes.
HANDOFF_RECEIPT_CONTRACT_VERSION = 1

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


class HandoffAction(StrEnum):
    """What the committed continuation says is owed next.

    Named rather than left for the worker to infer from the outcome: ``committed``
    says a write happened, not *what is now owed*. A future action (deployment,
    evaluation) is a new member here, and a member an older worker does not
    recognise is a refusal on its side rather than a guess.
    """

    AWAITING_REVIEW = "awaiting_review"


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
class HandoffReceipt:
    """The complete identity of a committed continuation (#5144).

    Every field is read from protected state after the write, never from the
    caller's request. The worker's job is to check this against the dispatch it was
    given — which it can only do if the server states *all* of it, so the receipt
    reference alone is deliberately not the contract.

    The fields are exactly the authority fences a continuation rests on, plus what
    is owed and when it is next due:

    * ``org_id``/``node_id``/``cycle`` — which tenant's work, and which attempt.
      A worker dispatched to cycle 3 must refuse a receipt naming cycle 4.
    * ``flow_id`` — the lane. The worker compares it because a node id alone does
      not prove the receipt belongs to the flow it was dispatched under.
    * ``accepted_plan_version`` — the policy the work was admitted under.
    * ``claim_generation`` — the ownership generation that produced the receipt. A
      legitimate handover advances it, so a receipt at another generation is
      another owner's.
    * ``action`` — what is owed next, from a closed vocabulary.
    * ``next_check_at`` — the committed due time, proving the continuation is
      genuinely still scheduled rather than terminal.
    * ``contract_version`` — so a worker that validates version 1 refuses a
      version it cannot field-check instead of validating a subset.
    """

    contract_version: int
    receipt_ref: str
    org_id: str
    flow_id: str
    node_id: str
    cycle: int
    accepted_plan_version: int
    claim_id: str
    claim_generation: int
    action: HandoffAction
    action_id: str
    next_check_at: datetime

    def as_response(self) -> dict:
        """The wire form. Explicit rather than ``asdict`` so adding a field to the
        dataclass cannot silently widen the contract the worker validates."""
        return {
            "contract_version": self.contract_version,
            "receipt_ref": self.receipt_ref,
            "org_id": self.org_id,
            "flow_id": self.flow_id,
            "node_id": self.node_id,
            "cycle": self.cycle,
            "accepted_plan_version": self.accepted_plan_version,
            "claim_id": self.claim_id,
            "claim_generation": self.claim_generation,
            "action": self.action.value,
            "action_id": self.action_id,
            "next_check_at": self.next_check_at.isoformat(),
        }


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
    # The full typed identity, present exactly when `accepted` is true. Absent on
    # every refusal, so a worker cannot read authority fences off a refused report.
    receipt: HandoffReceipt | None = None

    @property
    def accepted(self) -> bool:
        """True only when the receipt is durable, typed, and belongs to this attempt.

        The typed :attr:`receipt` is part of the condition, not merely carried
        alongside it. A positive acceptance with no receipt is the one answer the
        worker cannot act on — it would be told "yes" with no identity to check that
        "yes" against, which is the readback defect restated at the server. Every
        path that cannot build one already returns a refusal, so this makes that
        agreement structural rather than a property each call site must remember.
        """
        return self.outcome in _ACCEPTED and bool(self.receipt_ref) and self.receipt is not None


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

    Derived from the execution, cycle and authority fences, not the
    wall clock or a random value — that is what makes a repeated
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


def handoff_action_id(identity: ExecutionIdentity, execution_id: str) -> str:
    """The stable id of the continuation this handoff records.

    Derived from the same fences as the receipt reference, so a repeat converges on
    the same action id rather than appearing to record a second continuation. Kept
    distinct from the receipt reference because the two answer different questions:
    the reference proves *a receipt is stored*, the action id names *which
    continuation step* it committed, and a worker checks both.
    """
    return f"{HANDOFF_RECEIPT_SCHEME}-action:{execution_id}:{identity.cycle}:{identity.claim_generation}"


def _receipt_for_record(
    identity: ExecutionIdentity,
    record: ExecutionRecord,
    *,
    receipt_ref: str,
) -> HandoffReceipt | None:
    """Build the typed receipt from the committed row, or ``None`` if it cannot be.

    Returns ``None`` rather than a partially-populated receipt when the committed
    row lacks a due time: a continuation with no ``next_check_at`` is not a
    continuation, and a receipt asserting one that is not stored would be the
    server telling the worker something its own state does not support.
    """
    if record.next_check_at is None:
        logger.warning("handoff: committed execution %s has no next check; refusing to assert a continuation", record.id)
        return None
    return HandoffReceipt(
        contract_version=HANDOFF_RECEIPT_CONTRACT_VERSION,
        receipt_ref=receipt_ref,
        # Read off the stored record, not the identity, wherever the row carries it:
        # the point of the readback is that the worker sees what is durable.
        org_id=record.org_id,
        flow_id=record.flow_id,
        node_id=record.node_id,
        cycle=record.cycle,
        accepted_plan_version=record.accepted_plan_version,
        claim_id=record.claim_id,
        claim_generation=record.claim_generation,
        action=HandoffAction.AWAITING_REVIEW,
        action_id=handoff_action_id(identity, record.id),
        next_check_at=record.next_check_at,
    )


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
            replay = _receipt_for_record(identity, record, receipt_ref=record.handoff_receipt_ref)
            if replay is None:
                # A stored receipt whose row no longer carries a due continuation
                # cannot be replayed as an acceptance: the receipt's whole claim is
                # that work is still scheduled.
                return HandoffResult(
                    outcome=HandoffOutcome.REFUSED,
                    record=record,
                    reason="handoff_continuation_not_due",
                )
            return HandoffResult(
                outcome=HandoffOutcome.ALREADY_COMMITTED,
                receipt_ref=record.handoff_receipt_ref,
                record=record,
                reason="handoff_already_committed",
                receipt=replay,
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

    receipt = _receipt_for_record(identity, committed, receipt_ref=stored)
    if receipt is None:
        # The write landed but did not leave a due continuation. Reported as a
        # refusal rather than a success: the caller must not treat this as a handoff,
        # and the route's refusal path rolls the provisional write back.
        return HandoffResult(outcome=HandoffOutcome.REFUSED, record=committed, reason="handoff_continuation_not_due")

    logger.info("handoff: committed receipt and due continuation for execution %s", record.id)
    return HandoffResult(outcome=HandoffOutcome.COMMITTED, receipt_ref=stored, record=committed, receipt=receipt)


def handoff_required(dispatch: dict) -> bool:
    """Whether this dispatch must produce a durable handoff receipt (#5144).

    Read from the dispatch record's own marker, exactly as ``results.binding_required``
    reads ``pr_binding_required``, and for the same reason: the contract a run was
    dispatched under is a property of *that run*, so the boundary is deterministic
    rather than a wall-clock or deploy-time inference.

    A dispatch written before this contract existed carries no marker, so it keeps its
    prior behaviour unchanged. That asymmetry is deliberate — "no marker, therefore
    require a receipt" would hold every in-flight legacy run the moment this deploys,
    and "no receipt, therefore complete" would reintroduce the defect for new work.
    """
    return bool(dispatch.get("handoff_required"))


async def current_identity(session: AsyncSession, *, org_id: str, node_id: str) -> ExecutionIdentity | None:
    """The live authority fences for this node's current execution, or ``None``.

    One reader, used by both the worker-facing route and the engine's reconciliation,
    because two resolvers that agree today is how the evidence path and the write path
    come to disagree about which cycle a receipt belongs to.

    Every fence is read from the execution row rather than accepted from a caller. The
    caller passes the result to :func:`commit_handoff`, which re-verifies the
    generation **under its own row lock** — so this is a candidate to act on, not a
    trusted authority decision.

    Returns ``None`` when the node has no execution row. Callers must not create one:
    a handoff for work the engine has no execution for is not something to invent.
    """
    from sqlalchemy import select

    from .models import OrchestrationExecution

    row = (
        await session.execute(
            select(OrchestrationExecution)
            .where(
                OrchestrationExecution.org_id == org_id,
                OrchestrationExecution.node_id == node_id,
            )
            # Newest cycle: a repair cycle is separate work with its own ledger, and a
            # handoff belongs to the cycle currently in flight.
            .order_by(OrchestrationExecution.cycle.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return ExecutionIdentity(
        org_id=row.org_id,
        node_id=row.node_id,
        cycle=row.cycle,
        accepted_plan_version=row.accepted_plan_version,
        claim_id=row.claim_id,
        claim_generation=row.claim_generation,
    )


async def identity_for_attempt(
    session: AsyncSession,
    *,
    org_id: str,
    node_id: str,
    attempt: int,
    lock: bool = False,
) -> ExecutionIdentity | None:
    """The execution identity for a **named** attempt, not merely the newest one.

    This exists because :func:`current_identity` answers a different question than
    the write path needs to ask. It returns the node's newest cycle, which is right
    for reconciliation (the engine wants whatever is in flight now) and wrong for a
    worker commit: a worker may only commit against the cycle *it* was dispatched
    to. If a retry advanced the node between the caller authenticating and this
    read, ``current_identity`` would hand that caller the new cycle's fences and it
    would mint a receipt for work it was never dispatched to do.

    ``lock=True`` additionally takes the node row lock — the same lock
    ``pr_bindings.register_binding`` uses, and for the same reason — and revalidates
    that ``attempt`` is still current under it. That closes the window between
    resolving the target and writing: without the lock, a concurrent retry can
    increment ``attempts`` after the check and the write lands on superseded work.

    Returns ``None`` when there is no execution for that exact attempt, when the
    node is gone, or when the attempt is no longer current. Fail closed in every
    case: the caller must treat ``None`` as "not authorized to commit", never as
    "create one".
    """
    from sqlalchemy import select

    from .models import OrchestrationExecution, OrchestrationNode

    if lock:
        # Locked and re-read rather than trusting the value resolved earlier. A
        # retry that incremented `attempts` in between makes this caller stale, and
        # committing for it would bind a receipt to a cycle it does not own.
        node = (
            await session.execute(
                select(OrchestrationNode)
                .where(OrchestrationNode.id == node_id, OrchestrationNode.org_id == org_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if node is None or node.attempts != attempt:
            logger.info(
                "handoff: refusing attempt %s on node %s — attempt is no longer current",
                attempt,
                node_id,
            )
            return None

    row = (
        await session.execute(
            select(OrchestrationExecution)
            .where(
                OrchestrationExecution.org_id == org_id,
                OrchestrationExecution.node_id == node_id,
                # The attempt IS the cycle: `dispatch_pass` creates one execution per
                # delivery cycle keyed on the already-incremented `node.attempts`.
                # Matched exactly, so a caller cannot reach another cycle's row.
                OrchestrationExecution.cycle == attempt,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return ExecutionIdentity(
        org_id=row.org_id,
        node_id=row.node_id,
        cycle=row.cycle,
        accepted_plan_version=row.accepted_plan_version,
        claim_id=row.claim_id,
        claim_generation=row.claim_generation,
    )


@dataclass(frozen=True)
class OutstandingContinuation:
    """Why a claim may not be released, in the terms a refusal has to name (#5144 F1).

    Deliberately narrow: the execution, its cycle and the receipt at stake. A caller
    refusing a release needs to say *which* continuation it is protecting, and nothing
    more — a full ``ExecutionRecord`` here would invite release paths to start reading
    phase and block state they have no business acting on.
    """

    execution_id: str
    node_id: str
    cycle: int
    receipt_ref: str


async def outstanding_continuation(session: AsyncSession, *, org_id: str, claim_id: str) -> OutstandingContinuation | None:
    """The execution whose committed continuation this claim still backs, or ``None``.

    This is the reader reviewer blocker F1 turns on. A committed handoff records that
    another party owes the next step, and that promise rests on the claim generation
    the receipt was minted under: ``results._delivery_receipt`` attributes the stored
    receipt through :func:`receipt_for`, which returns ``None`` once the fences no
    longer match. So releasing the claim does not merely tidy ownership — it makes the
    receipt unattributable, and the story is then held forever on evidence that exists
    but can no longer be credited to any attempt.

    That is worse than the defect being fixed. The original bug let a worker exit 0
    with work outstanding; this would let a worker do everything right, commit a
    durable continuation, and *still* have its story stall — with a receipt sitting in
    the row proving the work was handed off correctly.

    Deliberately keyed on ``claim_id`` rather than on a node: the release paths know
    which claim they are ending and nothing else, and making them resolve a node first
    would be a second, weaker lookup beside this one.

    A read only, and no lock: callers take the claim row ``FOR UPDATE`` before asking,
    so the claim cannot change under them, and the execution row is consulted for a
    receipt that is already durable. Nothing here releases, advances or repairs
    anything — it answers one question, and the caller decides.

    Returns an :class:`OutstandingContinuation` for a **non-terminal** execution
    carrying a receipt minted under this exact claim and generation. ``None`` when
    there is no such execution, which is the ordinary case for every legacy lane and
    every run that never handed off.
    """
    from sqlalchemy import select

    from .models import OrchestrationExecution

    rows = (
        await session.execute(
            select(OrchestrationExecution)
            .where(
                OrchestrationExecution.org_id == org_id,
                OrchestrationExecution.claim_id == claim_id,
                OrchestrationExecution.handoff_receipt_ref.is_not(None),
                # A concluded or superseded execution owes nothing further, so its
                # claim is free. Filtered in SQL rather than after the fact so a lane
                # with a long history does not read every cycle it ever ran.
                OrchestrationExecution.status.notin_([status.value for status in TERMINAL_EXECUTION_STATUSES]),
            )
            # Newest cycle first: the one in flight is the one whose continuation a
            # release would strand.
            .order_by(OrchestrationExecution.cycle.desc())
            .execution_options(populate_existing=True)
        )
    ).scalars()

    for row in rows:
        identity = ExecutionIdentity(
            org_id=row.org_id,
            node_id=row.node_id,
            cycle=row.cycle,
            accepted_plan_version=row.accepted_plan_version,
            claim_id=row.claim_id,
            claim_generation=row.claim_generation,
        )
        # Attribution, not mere presence — the same rule `receipt_for` applies, reached
        # through the same helper. A stored value that is not the receipt THIS identity
        # mints belongs to other ownership and is not a continuation this claim backs,
        # so releasing does not strand it.
        if row.handoff_receipt_ref == handoff_receipt_ref(identity, row.id):
            return OutstandingContinuation(
                execution_id=row.id,
                node_id=row.node_id,
                cycle=row.cycle,
                receipt_ref=row.handoff_receipt_ref,
            )
    return None


async def receipt_for(session: AsyncSession, *, identity: ExecutionIdentity) -> str | None:
    """The durable handoff receipt for this work, or ``None`` if there is none.

    A read, never a write: the caller uses it to decide whether delivery may be
    treated as finished, and the absence of a receipt must leave work due rather than
    cause anything to be created here.

    Returns ``None`` for an execution whose authority fences no longer match, because
    a receipt that cannot be attributed to the current attempt is not evidence about
    it. Fail closed: unverifiable means absent.
    """
    current = await load_execution(session, identity=identity)
    if current is None or current.kind is OutcomeKind.CONFLICT:
        return None
    record = current.record
    if record is None or not record.handoff_receipt_ref:
        return None
    # Attribution, not mere presence: a stored receipt minted under a different
    # generation or plan version belongs to other ownership.
    return record.handoff_receipt_ref if record.handoff_receipt_ref == handoff_receipt_ref(identity, record.id) else None


def missing_receipt_hold(reason: str = "") -> str:
    """The operator-facing hold text for delivery with no durable receipt.

    Phrased as the actionable condition rather than "waiting": the point of the
    story is that a worker's clean exit is not completion, and an operator reading
    this needs to know what is outstanding and that it is still tracked.
    """
    return (
        "Agent exited without committing a durable continuation receipt, so the remaining "
        "review/deployment/evaluation work is not accounted for. This work stays due rather than "
        "being treated as complete." + (f" ({reason})" if reason else "")
    )


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
