"""Transactional reads and writes for the execution/action delivery ledger.

Issue #5142 (ENGINE-K1, parent #5122). This module is the only writer of
`orchestration_executions` and `orchestration_actions`. Its vocabulary and typed
inputs/results live in `execution_state.py`; the tables live in `models.py`.

## The problem these transactions solve

A worker's knowledge of "what have I already done" is in process memory. Lose the
process and the knowledge goes with it, so a later process must either stall
(nothing tells it work remains) or repeat (nothing tells it the pull request was
already opened). Every method here exists to make one of those two failures
impossible, and the guarantees are transactional rather than advisory.

## Five operations, and what each guarantees

- `create_execution` — one durable identity per `(org_id, node_id, cycle)`, even
  when two processes start at once. The unique index is the arbiter; a lost insert
  race returns the *winner's* record instead of raising, because both callers
  wanting the same identity is agreement, not conflict.
- `load_execution` — a row-locked read for a caller about to advance it.
- `prepare_action` — idempotent on `operation_key`. A repeat returns the original
  record, which is what makes crash-and-retry safe.
- `record_observation` — settles an action's outcome. An observer that could not
  tell leaves it `UNKNOWN`; nothing here upgrades an unobserved action to success.
- `advance_execution` — compare-and-set on `revision`. Writes the phase, the
  status and the next check time as one unit, optionally together with an action
  intent, or persists a typed block.

## Why no external call happens inside a transaction

Every method takes an `AsyncSession` and commits nothing — the caller owns the
transaction boundary, matching `work_claims.py` and every other orchestration
pass. The methods make no network calls at all. This is a correctness constraint,
not tidiness: `advance_execution` holds a row lock, and an HTTP request inside
that lock would block every other writer on the same execution for as long as the
remote end takes to time out. The intended shape is therefore: record the intent,
commit, act, then record the observation in a second transaction. The action row
that exists between those two commits is precisely what tells a recovering process
that the effect may already have landed.

## Why the authority binding is re-checked on every call

Callers pass an `ExecutionIdentity` naming the tenant, the accepted plan version
and the claim generation they hold. The check happens *inside* the writing
transaction, against the stored row, because the interval between a caller's read
and its write is exactly when a claim generation advances (#5127) or an accepted
plan is superseded (#5128). A check done earlier, or against the caller's own
copy, would pass while the authority it describes has already lapsed.

What this module does NOT do: decide ownership, evaluate policy, or approve a
gate. Those belong to `work_claims.py`, `policy_admission.py` and the existing
`controls.py`/`state.py`. This store only refuses to write for a binding the
stored row does not agree with, and reports the refusal as a typed `CONFLICT`.

## Lost updates, and why `revision` is not optional

Two processes that both read an execution mid-flight and both write would leave
only the second one's version — silently erasing the first's progress on the
record whose whole purpose is to survive process loss. So an advance presents the
revision it read, and a mismatch is answered `STALE` without writing. The store
deliberately offers no "re-apply at the current revision" convenience: whatever
moved the row may have changed what the caller should do, so re-reading is the
caller's job.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.logging import get_logger

from .execution_state import (
    ActionIntent,
    ActionRecord,
    ActionStatus,
    BlockCode,
    BlockRecord,
    ExecutionIdentity,
    ExecutionOutcome,
    ExecutionPhase,
    ExecutionRecord,
    ExecutionStatus,
    ExecutionStoreError,
    Observation,
    ObservedOutcome,
    OutcomeKind,
    PhaseAdvance,
)
from .models import (
    ClaimState,
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from .work_claims import OwnerKind

logger = get_logger(__name__)

__all__ = [
    "advance_execution",
    "create_execution",
    "load_execution",
    "prepare_action",
    "record_observation",
    "supersede_replaced_admission",
]


async def supersede_replaced_admission(
    session: AsyncSession,
    *,
    identity: ExecutionIdentity,
    expected_revision: int,
    successor_id: str,
    successor_revision: int,
    successor_cycle: int,
    issue_number: int,
    failed_settlement_decision_id: str | None = None,
) -> bool:
    """Retire an actionless admission after its positively exited worker was replaced.

    Called by replaced_admission recovery or explicit owner failure settlement
    after reading the old run's protected terminal record. Every SQL binding is
    rechecked under locks; the only write retires that exact historical row.
    The settled successor, claim, attempts, plan and all effects remain unchanged.
    An explicit human failure-settlement receipt also permits a failed successor;
    its old-worker proof and exact predecessor revision must be in that receipt.
    """
    flow_id = await _resolve_flow_id(session, identity)
    if flow_id is None:
        return False
    flow = await session.scalar(
        select(OrchestrationFlow)
        .where(OrchestrationFlow.org_id == identity.org_id, OrchestrationFlow.id == flow_id, OrchestrationFlow.archived_at.is_(None))
        .with_for_update(skip_locked=True)
    )
    if flow is None:
        return False
    if successor_cycle <= identity.cycle:
        return False
    if failed_settlement_decision_id:
        decision = await session.get(OrchestrationDecision, failed_settlement_decision_id)
        node = await session.get(OrchestrationNode, identity.node_id, populate_existing=True)
        try:
            data = json.loads(decision.reason) if decision else {}
            old = next((item for item in data.get("retired_admission_evidence", []) if item.get("cycle") == identity.cycle), {})
            if (
                decision is None
                or decision.kind != "failed_review_settled"
                or decision.actor_kind != "human"
                or decision.org_id != identity.org_id
                or decision.node_id != identity.node_id
                or decision.flow_id != flow_id
                or node is None
                or node.state != "failed"
                or node.attempts != successor_cycle
                or data.get("execution_id") != successor_id
                or old.get("revision") != expected_revision
                or old.get("claim_id") != identity.claim_id
                or old.get("claim_generation") != identity.claim_generation
                or old.get("accepted_plan_version") != identity.accepted_plan_version
                or not old.get("terminal_digest")
            ):
                return False
        except (ValueError, TypeError, AttributeError):
            return False
    claim = await session.scalar(
        select(OrchestrationWorkClaim)
        .where(OrchestrationWorkClaim.org_id == identity.org_id, OrchestrationWorkClaim.id == identity.claim_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        claim is None
        or claim.owner_kind != "engine_flow"
        or claim.owner_ref != flow_id
        or claim.state != "released"
        or claim.release_reason != ("failed" if failed_settlement_decision_id else "completed")
        or claim.active_run_id is not None
        or claim.generation <= identity.claim_generation
        or issue_number != claim.issue_number
    ):
        return False
    successor = await session.scalar(
        select(OrchestrationExecution)
        .where(
            OrchestrationExecution.org_id == identity.org_id,
            OrchestrationExecution.flow_id == flow_id,
            OrchestrationExecution.node_id == identity.node_id,
            OrchestrationExecution.id == successor_id,
            OrchestrationExecution.cycle == successor_cycle,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        successor is None
        or successor.revision != successor_revision
        or successor.status != "concluded"
        or successor.phase != "concluded"
        or successor.pending_action_key is not None
        or successor.claim_id != claim.id
        or successor.claim_generation != claim.generation
    ):
        return False
    row = await _locked_execution(session, identity)
    if (
        row is None
        or _binding_conflict(row, identity)
        or row.claim_generation != identity.claim_generation
        or row.revision != expected_revision
        or row.phase != "admitted"
        or row.status not in {"runnable", "awaiting_external"}
        or row.attempts != 0
        or row.pending_action_key is not None
        or row.handoff_receipt_ref is not None
        or row.notification_receipt_ref is not None
        or (failed_settlement_decision_id and old.get("execution_id") != row.id)
    ):
        return False
    # An absent pending pointer does not establish that no effect was started.
    # Refuse every action, even a terminal one; this narrow recovery handles only
    # the initial admission that never entered the continuation runner.
    action = await session.scalar(
        select(OrchestrationAction.id).where(OrchestrationAction.org_id == identity.org_id, OrchestrationAction.execution_id == row.id).limit(1)
    )
    if action is not None:
        return False
    row.status, row.phase = ExecutionStatus.SUPERSEDED.value, ExecutionPhase.CONCLUDED.value
    row.next_check_at = None
    row.revision += 1
    row.progressed_at = _now()
    row.progress_note = f"Exited admission replaced by settled execution {successor.id}; historical authority and attempts retained."
    await session.flush()
    return True


# How an observer's report maps onto a stored action status. `INDETERMINATE` maps
# to `UNKNOWN` rather than to a guess: an observer that looked and could not tell
# has produced real information ("we looked"), and it is not evidence of either
# outcome.
_OBSERVED_STATUS: dict[ObservedOutcome, ActionStatus] = {
    ObservedOutcome.SUCCEEDED: ActionStatus.SUCCEEDED,
    ObservedOutcome.FAILED: ActionStatus.FAILED,
    ObservedOutcome.INDETERMINATE: ActionStatus.UNKNOWN,
}

# Statuses for which a next check time is meaningless, because nothing will pick
# the execution up again. Enforced rather than trusted to each caller: the pairing
# is what stops a terminal row from carrying a phantom wake-up, and a non-terminal
# row from being invisible to the runner.
_NO_NEXT_CHECK: frozenset[ExecutionStatus] = frozenset({ExecutionStatus.CONCLUDED, ExecutionStatus.SUPERSEDED})


def _now() -> datetime:
    return datetime.now(UTC)


def _as_aware(moment: datetime | None) -> datetime | None:
    """Normalize a stored timestamp to timezone-aware UTC.

    The columns are `DateTime(timezone=True)`, so PostgreSQL returns aware values —
    but SQLite (and any driver that drops the offset) returns naive ones, and
    comparing naive to aware raises `TypeError`. Same reasoning and same treatment
    as `work_claims._as_aware`: every writer here is `_now()`/`utcnow()`, both UTC,
    so a naive read-back is a UTC value that lost its label in transit.
    """
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _encode_gates(gates: tuple[str, ...]) -> str | None:
    """Store the outstanding-gate list as JSON text.

    JSON rather than a comma-joined string because a gate reference is free text
    that may itself contain a comma, and a separator that can appear in the data
    is a parsing bug waiting to happen. `None` for the empty case so "no gates" and
    "gates not recorded" do not both read as `"[]"`.
    """
    return json.dumps(list(gates)) if gates else None


def _decode_gates(raw: str | None) -> tuple[str, ...]:
    """Read the gate list back, tolerating anything that is not valid JSON.

    A malformed value must not make an execution unreadable: this is a diagnostic
    field, and raising here would turn a cosmetic storage problem into a failure to
    load the record an operator is trying to diagnose.
    """
    if not raw:
        return ()
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("execution store: could not decode block_remaining_gates; reporting no gates")
        return ()
    return tuple(str(item) for item in decoded) if isinstance(decoded, list) else ()


def _block_from_row(row: OrchestrationExecution) -> BlockRecord | None:
    """Rebuild the typed block from its columns, or None if the row is not blocked.

    Keyed on `block_code` alone: it is the one column a block cannot lack, so a row
    with a code and (through some earlier bug) no owner still reports as blocked
    rather than silently reading as runnable.
    """
    if not row.block_code:
        return None
    try:
        code = BlockCode(row.block_code)
    except ValueError:
        # An unrecognised code means a newer writer stored a member this build does
        # not know. Fail closed and keep it blocked: treating it as "not blocked"
        # would let an older pod resume work a newer one deliberately stopped.
        logger.warning("execution store: unrecognised block code %r; reporting as authority-unverifiable", row.block_code)
        code = BlockCode.AUTHORITY_UNVERIFIABLE
    return BlockRecord(
        code=code,
        owner=row.block_owner or "unknown",
        required_input=row.block_required_input or "unspecified",
        remaining_gates=_decode_gates(row.block_remaining_gates),
        progressed_at=_as_aware(row.progressed_at),
        detail=row.block_detail,
    )


def _to_record(row: OrchestrationExecution) -> ExecutionRecord:
    """Project a stored row onto the immutable record consumers receive.

    Enum conversion is strict for phase and status: an unrecognised value means the
    database holds something this build cannot reason about, and guessing a default
    would be a decision about live work made by a version mismatch.
    """
    try:
        phase = ExecutionPhase(row.phase)
        status = ExecutionStatus(row.status)
    except ValueError as exc:
        raise ExecutionStoreError(
            "unknown_vocabulary",
            f"Execution {row.id} holds a phase/status this build does not recognise ({row.phase}/{row.status}).",
        ) from exc

    return ExecutionRecord(
        id=row.id,
        org_id=row.org_id,
        flow_id=row.flow_id,
        node_id=row.node_id,
        cycle=row.cycle,
        phase=phase,
        status=status,
        revision=row.revision,
        accepted_plan_version=row.accepted_plan_version,
        claim_id=row.claim_id,
        claim_generation=row.claim_generation,
        attempts=row.attempts,
        next_check_at=_as_aware(row.next_check_at),
        deadline_at=_as_aware(row.deadline_at),
        progressed_at=_as_aware(row.progressed_at),
        progress_note=row.progress_note,
        block=_block_from_row(row),
        pending_action_key=row.pending_action_key,
        notification_receipt_ref=row.notification_receipt_ref,
        handoff_receipt_ref=row.handoff_receipt_ref,
        created_at=_as_aware(row.created_at),
        updated_at=_as_aware(row.updated_at),
    )


def _to_action_record(row: OrchestrationAction) -> ActionRecord:
    try:
        status = ActionStatus(row.status)
    except ValueError as exc:
        raise ExecutionStoreError(
            "unknown_vocabulary",
            f"Action {row.id} holds a status this build does not recognise ({row.status}).",
        ) from exc
    return ActionRecord(
        id=row.id,
        org_id=row.org_id,
        execution_id=row.execution_id,
        operation_key=row.operation_key,
        kind=row.kind,
        status=status,
        attempt=row.attempt,
        artifact_ref=row.artifact_ref,
        receipt_ref=row.receipt_ref,
        detail=dict(row.detail or {}),
        created_at=_as_aware(row.created_at),
        observed_at=_as_aware(row.observed_at),
    )


def _binding_conflict(row: OrchestrationExecution, identity: ExecutionIdentity) -> str | None:
    """Check the caller's authority against the stored row. Returns a reason, or None.

    Called INSIDE the writing transaction on every mutating path. Each arm refuses
    something specific:

    - `tenant_mismatch`: the row belongs to another tenant. Reachable when a caller
      passes an id it obtained elsewhere; the row is never returned to the caller in
      this case, because returning it would leak across the boundary being enforced.
    - `claim_generation_superseded`: the caller holds an older ownership generation
      than the row. Its run has been handed over or superseded (#5127), and letting
      it write would record a superseded run's progress as current.
    - `accepted_plan_version_mismatch`: the caller was authorized under a different
      accepted plan version than the row records (#5128). The plan it was
      authorized against is not the plan this execution runs under.

    A *newer* claim generation than the row's is deliberately not a conflict: a
    legitimate handover advances the generation, and the new owner must be able to
    continue the same execution. Only an older generation is stale. `_adopt_generation`
    is what makes that asymmetry safe — see its docstring.
    """
    if row.org_id != identity.org_id:
        return "tenant_mismatch"
    if row.claim_id != identity.claim_id:
        # Deliberately NOT given the generation's "older is stale, newer is a
        # handover" treatment, because a claim id cannot change for the life of an
        # execution. `OrchestrationWorkClaim` holds one row per
        # `(org_id, provider_repository_id, issue_number)`, reused for that issue's
        # whole lifetime: release and handover mutate its `state`/`generation` and the
        # row survives deliberately (deleting it would reset the generation and make a
        # stale worker look current), so no path inserts a replacement or reassigns
        # `id`. A differing claim id therefore never means "superseded" — it means a
        # different issue's claim or a fabricated id, and refusing is terminal.
        return "claim_mismatch"
    if identity.claim_generation < row.claim_generation:
        return "claim_generation_superseded"
    if row.accepted_plan_version != identity.accepted_plan_version:
        return "accepted_plan_version_mismatch"
    return None


async def _resolve_flow_id(
    session: AsyncSession,
    identity: ExecutionIdentity,
    *,
    expected_flow_id: str | None = None,
) -> str | None:
    """Resolve the node inside the caller's tenant and, optionally, its flow."""
    stmt = select(OrchestrationNode.flow_id).where(
        OrchestrationNode.org_id == identity.org_id,
        OrchestrationNode.id == identity.node_id,
    )
    if expected_flow_id is not None:
        stmt = stmt.where(OrchestrationNode.flow_id == expected_flow_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _live_authority_conflict(
    session: AsyncSession,
    identity: ExecutionIdentity,
    *,
    flow_id: str,
    lock: bool = True,
    released_failure_run_id: str | None = None,
    require_unarchived: bool = False,
    closing_superseded_lane: bool = False,
) -> str | None:
    """Verify the claim and accepted plan that are live in this transaction.

    `lock=True` for a writing path: the flow, claim and accepted-plan rows are taken
    `FOR UPDATE` so the authority cannot lapse between this check and the write it
    guards. That is the whole point of checking inside the transaction.

    `lock=False` for a plain read. The same refusals are still computed from the same
    rows — only the locks are dropped, because a diagnostic or the read model (#5145)
    must not block on live work, and must not make a *reader* the reason a writer
    waits. A reader has nothing to protect between the check and a write it will
    never make, so the lock buys it no correctness.
    """
    flow_stmt = select(OrchestrationFlow.id, OrchestrationFlow.archived_at).where(
        OrchestrationFlow.org_id == identity.org_id, OrchestrationFlow.id == flow_id
    )
    if lock:
        flow_stmt = flow_stmt.with_for_update()
    flow = (await session.execute(flow_stmt)).one_or_none()
    if flow is None:
        return "tenant_binding_mismatch"
    if require_unarchived and flow.archived_at is not None:
        return "flow_archived"

    claim_stmt = select(OrchestrationWorkClaim).where(
        OrchestrationWorkClaim.org_id == identity.org_id,
        OrchestrationWorkClaim.id == identity.claim_id,
    )
    if lock:
        claim_stmt = claim_stmt.with_for_update()
    claim = (await session.execute(claim_stmt.execution_options(populate_existing=True))).scalar_one_or_none()
    if claim is None:
        return "claim_mismatch"
    if claim.owner_kind != OwnerKind.ENGINE_FLOW.value or claim.owner_ref != flow_id:
        return "claim_owner_mismatch"
    released_failure = (
        released_failure_run_id is not None
        and claim.state == ClaimState.RELEASED.value
        # Released by the exiting worker as failed, or by the engine as abandoned
        # once the pod was proven gone: either way this run was the last owner
        # and the failure reconciler may settle it (#7419). A worker that released
        # its claim as completed was equally the last owner; the operations retry
        # settles such a run when its runtime evidence cannot be verified (#7826).
        # The run id on the row is what binds every case — never the reason alone.
        and claim.release_reason in {"failed", "abandoned", "completed"}
        and claim.active_run_id is None
        and claim.claim_event_id == released_failure_run_id
    )
    # Closing a superseded node's lane is not adoption (#7810). The lane's stored
    # claim generation is, by construction, behind wherever the issue's claim has
    # moved on to — a successor address re-acquired it, or the exiting worker
    # released it — and that is exactly why the lane must be closed rather than
    # left open forever. Tenant, flow, claim identity and ownership above still
    # bind; `advance_execution` permits only a conclusion under this flag; and the
    # claim row itself is never written on this path.
    if not closing_superseded_lane:
        if claim.state != ClaimState.HELD.value and not released_failure:
            return "claim_not_held"
        if released_failure_run_id is not None and not released_failure and claim.active_run_id != released_failure_run_id:
            return "claim_run_mismatch"
        if claim.generation != identity.claim_generation:
            return "claim_generation_superseded" if claim.generation > identity.claim_generation else "claim_generation_mismatch"

    plan_stmt = (
        select(OrchestrationAcceptedPlan)
        .where(
            OrchestrationAcceptedPlan.org_id == identity.org_id,
            OrchestrationAcceptedPlan.flow_id == flow_id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        )
        .limit(2)
    )
    if lock:
        plan_stmt = plan_stmt.with_for_update()
    plans = list((await session.execute(plan_stmt.execution_options(populate_existing=True))).scalars().all())
    if len(plans) > 1:
        return "accepted_plan_ambiguous"
    current_version = plans[0].version if plans else 0
    if current_version != identity.accepted_plan_version:
        from .plan_lineage import ancestor_plan

        if not plans or await ancestor_plan(session, plans[0], identity.accepted_plan_version, node_id=identity.node_id) is None:
            # A superseded node's execution was, by definition, authorized under a
            # plan the flow has moved past, and lineage can never preserve a node an
            # amendment retired. The caller that settles such a lane has verified the
            # node is superseded (this module never reads graph state); closing the
            # lane — conclude only, enforced in `advance_execution` — is the one write
            # the old authority may still make. Every other binding above applies (#7800).
            if closing_superseded_lane:
                return None
            return "accepted_plan_version_mismatch"
    return None


def _unbound_conflict(reason: str) -> ExecutionOutcome:
    """Return a refusal when no execution row may safely be disclosed."""
    return ExecutionOutcome(kind=OutcomeKind.CONFLICT, record=None, reason=reason)


async def _live_refusal(session: AsyncSession, identity: ExecutionIdentity, reason: str, *, lock: bool = True) -> ExecutionOutcome:
    """Attach the record only when the caller already knows its stored claim.

    `lock=False` for the plain read path, for the same reason `_live_authority_conflict`
    takes it: a refused *reader* must not lock the row it was refused. The disclosure
    rule is unchanged — the record travels only when the caller already named this
    execution's stored claim.
    """
    row = await (_locked_execution(session, identity) if lock else _unlocked_execution(session, identity))
    if row is not None and row.org_id == identity.org_id and row.claim_id == identity.claim_id:
        return _conflict(row, reason)
    return _unbound_conflict(reason)


def _adopt_generation(row: OrchestrationExecution, identity: ExecutionIdentity) -> None:
    """Raise the row's generation to a newer writer's, so the fence actually fences.

    Called on every mutating path once `_binding_conflict` has passed. Accepting a
    newer generation without *recording* it would make the fence advisory: the row
    would stay at the superseded generation, so `_binding_conflict` — which refuses
    only a generation older than the row's — would keep admitting the displaced
    worker. Concretely, and this was reachable before this write existed: a handover
    advances the claim to generation 2 (#5127), the successor advances the execution,
    the row is still recorded at generation 1, and the displaced generation-1 worker
    then loses only the compare-and-set. Its documented response to `STALE` is to
    re-read and decide again — and on that re-read it carries a current revision and
    a generation the row still considers acceptable, so its write applies. A worker
    that was handed over concludes work its successor owns.

    Monotonic by construction: the guard raises the stored generation and never
    lowers it, because a lower value would hand the fence back to the run that was
    superseded. Recording it is not a claim decision — the claim's own
    lifecycle stays with `work_claims.py`; this only records which generation was
    last admitted to write here, which is the fact the next authority check needs.
    """
    if identity.claim_generation > row.claim_generation:
        logger.info(
            "execution store: execution %s adopted by claim generation %s (was %s); earlier generations can no longer write",
            row.id,
            identity.claim_generation,
            row.claim_generation,
        )
        row.claim_generation = identity.claim_generation


def _conflict(row: OrchestrationExecution, reason: str) -> ExecutionOutcome:
    """Build the typed refusal for an authority mismatch.

    The record is withheld from a caller that never held this execution's claim, and
    returned to one whose own binding merely lapsed.

    - `tenant_mismatch`: withheld, because returning it would leak across the very
      boundary the refusal exists to enforce.
    - `claim_mismatch` / `claim_owner_mismatch`: also withheld. The caller either
      presented a different claim or a claim owned by another dispatch lane, so it
      has no standing to read the row. Returning the record would disclose the
      binding values the next write tests; a refusal must not disclose what would
      satisfy it.
    - `claim_generation_superseded` / `accepted_plan_version_mismatch`: returned.
      Here the caller holds the right claim and its own binding lapsed, so it
      already knows these values and needs the current row to see what superseded
      it without a second round trip.
    """
    withheld = reason in ("tenant_mismatch", "claim_mismatch", "claim_owner_mismatch")
    return ExecutionOutcome(
        kind=OutcomeKind.CONFLICT,
        record=None if withheld else _to_record(row),
        reason=reason,
    )


async def _action_for_key(
    session: AsyncSession,
    row: OrchestrationExecution,
    operation_key: str,
    *,
    for_update: bool = False,
) -> OrchestrationAction | None:
    """Look one action up by its idempotency key, scoped to tenant AND execution.

    `org_id` is in the predicate as well as `execution_id` even though the execution
    was already resolved under the tenant: the filter is what makes a cross-tenant id
    unresolvable rather than merely unlikely, and it matches the unique index column
    order so the lookup uses it.
    """
    stmt = select(OrchestrationAction).where(
        OrchestrationAction.org_id == row.org_id,
        OrchestrationAction.execution_id == row.id,
        OrchestrationAction.operation_key == operation_key,
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one_or_none()


def _execution_for_identity(identity: ExecutionIdentity):
    """The identity predicate both the locked and plain execution reads share."""
    return select(OrchestrationExecution).where(
        OrchestrationExecution.org_id == identity.org_id,
        OrchestrationExecution.node_id == identity.node_id,
        OrchestrationExecution.cycle == identity.cycle,
    )


async def _unlocked_execution(session: AsyncSession, identity: ExecutionIdentity) -> OrchestrationExecution | None:
    """Read an execution for its identity without locking it.

    For diagnostics and the read model (#5145): they render live work and must never
    make a writer wait, so they take the same row with no `FOR UPDATE`.
    """
    return (await session.execute(_execution_for_identity(identity))).scalar_one_or_none()


async def _locked_execution(session: AsyncSession, identity: ExecutionIdentity) -> OrchestrationExecution | None:
    """Read an execution for its identity under a row lock.

    `with_for_update()` without `skip_locked`: a contended row must make the second
    caller *wait* and then observe the winner's committed state. Skipping would read
    "no execution" and create a second identity — the exact duplication this module
    exists to prevent. On SQLite `FOR UPDATE` is a no-op, which is why the unique
    index is the correctness backstop and why the concurrency assertions run against
    real PostgreSQL.
    """
    stmt = _execution_for_identity(identity).with_for_update().execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_execution(
    session: AsyncSession,
    *,
    identity: ExecutionIdentity,
    flow_id: str,
    phase: ExecutionPhase = ExecutionPhase.ADMITTED,
    next_check_at: datetime | None = None,
    deadline_at: datetime | None = None,
) -> ExecutionOutcome:
    """Create the durable identity for one node's delivery cycle, or return the existing one.

    Idempotent by design. Two processes starting the same work at the same moment
    both want the same identity, and that is agreement rather than conflict — so a
    lost insert race returns the winner's record with `APPLIED` rather than raising.
    What must never happen is *two* identities, and the unique index on
    `(org_id, node_id, cycle)` is what guarantees that when the application-level
    read loses the race.

    Commits nothing; the caller owns the transaction boundary.

    Returns:
        `APPLIED` with the execution (created or pre-existing), or `CONFLICT` when a
        row exists whose stored authority disagrees with `identity` — a caller
        holding a superseded claim generation must not adopt live work.
    """
    if not str(flow_id or "").strip():
        raise ExecutionStoreError("invalid_execution", "An execution must name the flow it belongs to.")

    resolved_flow_id = await _resolve_flow_id(session, identity, expected_flow_id=flow_id)
    if resolved_flow_id is None:
        return _unbound_conflict("tenant_binding_mismatch")
    live_conflict = await _live_authority_conflict(session, identity, flow_id=resolved_flow_id, require_unarchived=True)
    if live_conflict:
        return await _live_refusal(session, identity, live_conflict)

    existing = await _locked_execution(session, identity)
    if existing is not None:
        # Re-verify authority before handing it back: adopting an execution is as
        # consequential as advancing one.
        conflict = _binding_conflict(existing, identity)
        if conflict:
            logger.info(
                "execution store: refusing to adopt execution for node=%s cycle=%s — %s",
                identity.node_id,
                identity.cycle,
                conflict,
            )
            return _conflict(existing, conflict)
        # Adopting an execution is as consequential as advancing one, so the
        # generation that adopted it is recorded here too.
        _adopt_generation(existing, identity)
        return ExecutionOutcome(kind=OutcomeKind.APPLIED, record=_to_record(existing))

    now = _now()
    row = OrchestrationExecution(
        org_id=identity.org_id,
        flow_id=flow_id,
        node_id=identity.node_id,
        cycle=identity.cycle,
        phase=phase.value,
        status=ExecutionStatus.RUNNABLE.value,
        revision=1,
        accepted_plan_version=identity.accepted_plan_version,
        claim_id=identity.claim_id,
        claim_generation=identity.claim_generation,
        attempts=0,
        # A fresh execution is due now unless the caller says otherwise, so it is
        # never invisible to pickup on account of a missing check time.
        next_check_at=next_check_at or now,
        deadline_at=deadline_at,
        progressed_at=now,
        created_at=now,
    )
    try:
        # Isolate the insert race from the caller's transaction. Rolling the whole
        # session back here would discard work the same pass already did; leaving it
        # failed would poison the rest of it. Same treatment as `claim_work`.
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        # Lost the insert race. Unlike a claim — where losing means someone else owns
        # the work and we must refuse — losing here means the identity we wanted now
        # exists. Read the winner so both callers proceed against one record.
        logger.info(
            "execution store: lost insert race for node=%s cycle=%s — adopting the committed execution",
            identity.node_id,
            identity.cycle,
        )
        winner = await _locked_execution(session, identity)
        if winner is None:
            # Not visible from this transaction, which on PostgreSQL means the winner
            # has not committed yet. Reporting "no execution" would invite a retry
            # that races again, so this is a typed refusal the caller fails closed on.
            raise ExecutionStoreError(
                "create_race_lost",
                f"Another process concurrently created the execution for node {identity.node_id} cycle {identity.cycle}; retry once it commits.",
            ) from None
        conflict = _binding_conflict(winner, identity)
        if conflict:
            return _conflict(winner, conflict)
        _adopt_generation(winner, identity)
        return ExecutionOutcome(kind=OutcomeKind.APPLIED, record=_to_record(winner))

    logger.info(
        "execution store: created execution %s for node=%s cycle=%s phase=%s",
        row.id,
        identity.node_id,
        identity.cycle,
        phase.value,
    )
    return ExecutionOutcome(kind=OutcomeKind.APPLIED, record=_to_record(row))


async def load_execution(
    session: AsyncSession,
    *,
    identity: ExecutionIdentity,
    for_update: bool = False,
    released_failure_run_id: str | None = None,
    closing_superseded_lane: bool = False,
) -> ExecutionOutcome | None:
    """Read one execution, optionally under a row lock.

    `for_update=True` for a caller about to advance it: the lock makes a concurrent
    writer wait rather than interleave, so the revision the caller reads is still
    current when it writes. The default plain read is for diagnostics and the read
    model (#5145), which must not take locks on live work.

    The failure reconciler may supply released_failure_run_id after authenticating
    that run's terminal failure. Only its unchanged, failed claim generation is
    readable through this exception; ordinary callers still require a held claim.

    Returns:
        `None` when no execution exists for the identity — genuinely absent, which is
        different from refused. Otherwise `APPLIED` with the record, or `CONFLICT`
        when the stored authority disagrees with the caller's.
    """
    flow_id = await _resolve_flow_id(session, identity)
    if flow_id is None:
        return None
    # A plain read takes no locks, here or in the authority check it delegates to:
    # the read model renders live work and must not block on — or block — a writer.
    live_conflict = await _live_authority_conflict(
        session,
        identity,
        flow_id=flow_id,
        lock=for_update,
        released_failure_run_id=released_failure_run_id,
        closing_superseded_lane=closing_superseded_lane,
    )
    if live_conflict:
        return await _live_refusal(session, identity, live_conflict, lock=for_update)

    row = await (_locked_execution(session, identity) if for_update else _unlocked_execution(session, identity))

    if row is None:
        return None

    conflict = _binding_conflict(row, identity)
    if conflict:
        return _conflict(row, conflict)
    return ExecutionOutcome(kind=OutcomeKind.APPLIED, record=_to_record(row))


async def prepare_action(
    session: AsyncSession,
    *,
    identity: ExecutionIdentity,
    intent: ActionIntent,
) -> ExecutionOutcome:
    """Record an action's intent before it is attempted, idempotently.

    This is the write that makes an external effect recoverable. It happens BEFORE
    the call, so a process that dies mid-call still leaves a row saying "this was
    about to happen" — which lets a later process go and *ask* instead of blindly
    retrying. Recording afterwards would leave nothing at all in exactly the window
    that matters.

    Idempotent on `intent.operation_key`: a repeat returns the ORIGINAL record,
    including its status, so a retry after a crash discovers what the first attempt
    already achieved. The unique index on `(org_id, execution_id, operation_key)` is
    what makes that hold when two preparers race.

    Makes no external call and commits nothing.

    Returns:
        `APPLIED` with both the execution and the action (newly prepared, or the
        pre-existing record for a repeated key, with `reason="action_already_prepared"`),
        or `CONFLICT` when the caller's authority does not match the stored execution.
    """
    flow_id = await _resolve_flow_id(session, identity)
    if flow_id is None:
        raise ExecutionStoreError(
            "unknown_execution",
            f"No execution exists for node {identity.node_id} cycle {identity.cycle}; create it before preparing actions.",
        )
    live_conflict = await _live_authority_conflict(session, identity, flow_id=flow_id, require_unarchived=True)
    if live_conflict:
        return await _live_refusal(session, identity, live_conflict)

    row = await _locked_execution(session, identity)
    if row is None:
        raise ExecutionStoreError(
            "unknown_execution",
            f"No execution exists for node {identity.node_id} cycle {identity.cycle}; create it before preparing actions.",
        )

    conflict = _binding_conflict(row, identity)
    if conflict:
        logger.info("execution store: refusing action %s — %s", intent.operation_key, conflict)
        return _conflict(row, conflict)

    # A successor preparing an action is writing under its own generation; record it
    # so the predecessor cannot follow behind. See `_adopt_generation`.
    _adopt_generation(row, identity)

    existing = await _action_for_key(session, row, intent.operation_key)
    if existing is not None and existing.kind != intent.kind:
        # Same key, different operation: the key no longer identifies one effect, so
        # honouring it would let this caller adopt a receipt that belongs to different
        # work. That is indistinguishable from "already done" to the caller, so refuse
        # instead of guessing which effect the row stands for. Reaching this means a
        # key derivation lost injectivity; fail closed and name it.
        conflict = f"operation key {intent.operation_key} is already held by a {existing.kind} action; refusing to reuse it for {intent.kind}"
        logger.warning("execution store: refusing action on execution %s — %s", row.id, conflict)
        return _conflict(row, conflict)

    if existing is not None:
        # The duplicate path, and the whole point of the operation key. Returning the
        # original record — not a fresh one, and not an error — is what lets a
        # retrying caller see that this step may already have taken effect.
        logger.info(
            "execution store: duplicate action %s on execution %s — returning the original record (status=%s)",
            intent.operation_key,
            row.id,
            existing.status,
        )
        return ExecutionOutcome(
            kind=OutcomeKind.APPLIED,
            record=_to_record(row),
            action=_to_action_record(existing),
            reason="action_already_prepared",
        )

    action = OrchestrationAction(
        org_id=identity.org_id,
        execution_id=row.id,
        operation_key=intent.operation_key,
        kind=intent.kind,
        status=ActionStatus.PREPARED.value,
        attempt=row.attempts,
        artifact_ref=intent.artifact_ref,
        detail=dict(intent.detail) if intent.detail else None,
        created_at=_now(),
    )
    try:
        async with session.begin_nested():
            session.add(action)
            await session.flush()
    except IntegrityError:
        # Lost the insert race on the operation key. The winner's row IS the original
        # record for this step, which is exactly what a duplicate caller should get.
        logger.info(
            "execution store: lost action insert race for %s — adopting the committed action",
            intent.operation_key,
        )
        winner = await _action_for_key(session, row, intent.operation_key)
        if winner is None:
            raise ExecutionStoreError(
                "action_race_lost",
                f"Another process concurrently prepared action {intent.operation_key}; retry once it commits.",
            ) from None
        return ExecutionOutcome(
            kind=OutcomeKind.APPLIED,
            record=_to_record(row),
            action=_to_action_record(winner),
            reason="action_already_prepared",
        )

    logger.info(
        "execution store: prepared action %s (%s) on execution %s",
        intent.operation_key,
        intent.kind,
        row.id,
    )
    return ExecutionOutcome(kind=OutcomeKind.APPLIED, record=_to_record(row), action=_to_action_record(action))


async def record_observation(
    session: AsyncSession,
    *,
    identity: ExecutionIdentity,
    observation: Observation,
) -> ExecutionOutcome:
    """Settle what was observed about one prepared action.

    Separate from `prepare_action` because observing is a different act from
    intending and — critically — may be done by a *different* process than the one
    that acted. That is the recovery case this ledger exists for: the original run is
    gone, and whatever picks the work up records what it found.

    An `INDETERMINATE` report stores `ActionStatus.UNKNOWN` and still stamps
    `observed_at` (we did look): the outcome remains uncertain, and the uncertainty
    itself is durable. Nothing here upgrades an unobserved action to success, because
    advancing delivery on evidence nobody saw is the failure mode that makes a ledger
    worse than none.

    Makes no external call and commits nothing.

    Returns:
        `APPLIED` with the updated action, or `CONFLICT` for an authority mismatch.
    """
    flow_id = await _resolve_flow_id(session, identity)
    if flow_id is None:
        raise ExecutionStoreError(
            "unknown_execution",
            f"No execution exists for node {identity.node_id} cycle {identity.cycle}; nothing to observe.",
        )
    live_conflict = await _live_authority_conflict(session, identity, flow_id=flow_id)
    if live_conflict:
        return await _live_refusal(session, identity, live_conflict)

    row = await _locked_execution(session, identity)
    if row is None:
        raise ExecutionStoreError(
            "unknown_execution",
            f"No execution exists for node {identity.node_id} cycle {identity.cycle}; nothing to observe.",
        )

    conflict = _binding_conflict(row, identity)
    if conflict:
        return _conflict(row, conflict)

    _adopt_generation(row, identity)

    action = await _action_for_key(session, row, observation.operation_key, for_update=True)
    if action is None:
        # Observing something never prepared means the caller and the ledger disagree
        # about what was attempted. Refused rather than inserted: an action row
        # conjured by its own observation would have no intent behind it and no
        # attempt number that means anything.
        raise ExecutionStoreError(
            "unknown_action",
            f"No action {observation.operation_key} is recorded on execution {row.id}; observations settle prepared actions only.",
        )

    target_status = _OBSERVED_STATUS[observation.outcome].value
    if action.status in (ActionStatus.SUCCEEDED.value, ActionStatus.FAILED.value):
        stored_detail = (action.detail or {}).get("observation")
        is_duplicate = action.status == target_status and action.receipt_ref == observation.receipt_ref and stored_detail == observation.detail
        if is_duplicate:
            return ExecutionOutcome(
                kind=OutcomeKind.APPLIED,
                record=_to_record(row),
                action=_to_action_record(action),
                reason="observation_already_recorded",
            )
        logger.warning(
            "execution store: refusing conflicting observation for settled action %s on execution %s (stored=%s, reported=%s)",
            observation.operation_key,
            row.id,
            action.status,
            target_status,
        )
        return ExecutionOutcome(
            kind=OutcomeKind.CONFLICT,
            record=_to_record(row),
            action=_to_action_record(action),
            reason="action_already_settled",
        )

    action.status = target_status
    action.observed_at = _now()
    if observation.receipt_ref:
        # A reference, never a transcript. See the column docstrings in models.py.
        action.receipt_ref = observation.receipt_ref
    if observation.detail:
        merged = dict(action.detail or {})
        # Nested under its own key so the observer cannot overwrite the intent's
        # detail — the two are separate assertions by separate parties.
        merged["observation"] = observation.detail
        action.detail = merged
    await session.flush()

    logger.info(
        "execution store: observed action %s on execution %s as %s",
        observation.operation_key,
        row.id,
        action.status,
    )
    return ExecutionOutcome(kind=OutcomeKind.APPLIED, record=_to_record(row), action=_to_action_record(action))


async def settle_released_merge(
    session: AsyncSession,
    *,
    identity: ExecutionIdentity,
    expected_revision: int,
    reviewer_run_id: str,
    released_at: datetime,
    receipt,
    verification: dict,
) -> ExecutionOutcome:
    """Record an observed merge and conclude exactly its released generation.

    The owner recovery service authenticates the successful reviewer, accepted
    review and current provider evidence. This specialized writer permits no
    dispatch, effect intent, claim adoption or attempt consumption. The ordinary
    action writers still require held ownership.
    """
    from dataclasses import asdict
    from hashlib import sha256

    from .merge_controller import MERGE_KIND, MergeReceipt, encode

    receipt = MergeReceipt.model_validate(receipt)
    if (
        receipt.verification_kind != "post_merge_verification"
        or any(getattr(receipt, key) != value for key, value in asdict(identity).items())
        or receipt.eligibility_digest != sha256(encode(verification).encode()).hexdigest()
        or verification.get("review_state") != "approved"
    ):
        raise ExecutionStoreError("invalid_merge_settlement", "An exact, verified external merge is required.")
    current = await load_execution(session, identity=identity, for_update=True, released_failure_run_id=reviewer_run_id)
    if current is None or current.kind is not OutcomeKind.APPLIED:
        return current or _unbound_conflict("unknown_execution")
    row = await _locked_execution(session, identity)
    claim = await session.get(OrchestrationWorkClaim, identity.claim_id, populate_existing=True)
    if (
        (row.phase, row.status)
        not in {
            (ExecutionPhase.MERGE_READY.value, ExecutionStatus.RUNNABLE.value),
            (ExecutionPhase.AWAITING_REVIEW.value, ExecutionStatus.RUNNABLE.value),
            (ExecutionPhase.AWAITING_REVIEW.value, ExecutionStatus.AWAITING_EXTERNAL.value),
        }
        or row.pending_action_key is not None
        or row.claim_generation != identity.claim_generation
        or row.id != receipt.execution_id
        or row.flow_id != receipt.flow_id
        or claim.state != ClaimState.RELEASED.value
        or claim.release_reason != "completed"
        or claim.claim_event_id != reviewer_run_id
        or claim.active_run_id is not None
        or claim.released_at != released_at
        or released_at is None
    ):
        return _conflict(row, "released_merge_assignment_changed")
    if await session.scalar(
        select(OrchestrationAction.id)
        .where(
            OrchestrationAction.org_id == identity.org_id,
            OrchestrationAction.execution_id == row.id,
            OrchestrationAction.kind == MERGE_KIND,
        )
        .limit(1)
    ):
        return _conflict(row, "merge_action_requires_reconciliation")
    advanced = await advance_execution(
        session,
        identity=identity,
        released_failure_run_id=reviewer_run_id,
        advance=PhaseAdvance(
            phase=ExecutionPhase.CONCLUDED,
            status=ExecutionStatus.CONCLUDED,
            expected_revision=expected_revision,
            progress_note="Owner accepted verified existing merge after premature claim release.",
        ),
    )
    if advanced.kind is not OutcomeKind.APPLIED:
        return advanced
    action = OrchestrationAction(
        org_id=identity.org_id,
        execution_id=row.id,
        operation_key=receipt.operation_key,
        kind=MERGE_KIND,
        status=ActionStatus.SUCCEEDED.value,
        attempt=row.attempts,
        receipt_ref=f"github/verified-merge/{receipt.merge_sha}",
        detail={"post_merge_verification": verification, "merge_mutation_performed": False},
        observed_at=receipt.observed_at,
    )
    session.add(action)
    await session.flush()
    return ExecutionOutcome(kind=OutcomeKind.APPLIED, record=advanced.record, action=_to_action_record(action))


async def advance_execution(
    session: AsyncSession,
    *,
    identity: ExecutionIdentity,
    advance: PhaseAdvance,
    intent: ActionIntent | None = None,
    block: BlockRecord | None = None,
    pending_action_key: str | None = None,
    notification_receipt_ref: str | None = None,
    handoff_receipt_ref: str | None = None,
    released_failure_run_id: str | None = None,
    closing_superseded_lane: bool = False,
) -> ExecutionOutcome:
    """Compare-and-set the execution's phase, status and next check time — atomically.

    The one write that moves work forward, and the reason this module is
    transactional. Three things happen together or not at all:

    1. the phase and status move;
    2. the next check time is written (or cleared, for a terminal status);
    3. any accompanying action intent is recorded.

    Splitting them produces a durable inconsistency with no repair path. The specific
    one worth naming: a commit that advanced the phase but not the next check time
    leaves work that has moved on and will never be picked up again — a permanent
    stall that reads as progress in every view. And an action intent written in a
    *separate* transaction from the advance that schedules its follow-up can survive
    while that follow-up rolls back, leaving an effect nobody will ever look at.

    `advance.expected_revision` is the fence. If the row has moved, nothing is
    written and the answer is `STALE`, with the current record attached so the caller
    can re-read without a second round trip.

    Passing `block` persists a typed block and forces the status to `BLOCKED` — the
    two cannot be made to disagree, because a row whose status says runnable while
    carrying a block code would be picked up and immediately re-blocked, forever. A
    block is a *record*, not a recovery path: `ATTEMPTS_EXHAUSTED` and `HUMAN_REFUSED`
    note the condition and route to the existing authorized recovery, and nothing
    here approves or bypasses a human gate.

    Makes no external call and commits nothing.

    Returns:
        `APPLIED` on success; `BLOCKED` when the write persisted a block (it DID
        write — the block is now durable); `STALE` when the revision had moved, with
        nothing written; `CONFLICT` for an authority mismatch.
    """
    # A recorded terminal failure can arrive after claim recovery released the
    # same run. This exception only concludes its ledger; it cannot authorize
    # another action, advance a delivery phase, or consume an attempt.
    if released_failure_run_id is not None and (
        advance.phase != ExecutionPhase.CONCLUDED
        or advance.status != ExecutionStatus.CONCLUDED
        or advance.consume_attempt
        or advance.next_check_at is not None
        or intent is not None
        or block is not None
        or pending_action_key is not None
        or notification_receipt_ref is not None
        or handoff_receipt_ref is not None
    ):
        raise ExecutionStoreError("invalid_failure_settlement", "Released failure authority only permits terminal conclusion.")
    flow_id = await _resolve_flow_id(session, identity)
    if flow_id is None:
        raise ExecutionStoreError(
            "unknown_execution",
            f"No execution exists for node {identity.node_id} cycle {identity.cycle}; create it before advancing.",
        )
    if closing_superseded_lane and (advance.status is not ExecutionStatus.CONCLUDED or intent is not None or block is not None):
        raise ExecutionStoreError("invalid_superseded_settlement", "A superseded node's lane may only be concluded.")
    live_conflict = await _live_authority_conflict(
        session, identity, flow_id=flow_id, released_failure_run_id=released_failure_run_id, closing_superseded_lane=closing_superseded_lane
    )
    if live_conflict:
        return await _live_refusal(session, identity, live_conflict)

    row = await _locked_execution(session, identity)
    if row is None:
        raise ExecutionStoreError(
            "unknown_execution",
            f"No execution exists for node {identity.node_id} cycle {identity.cycle}; create it before advancing.",
        )

    # Authority first: a caller with no standing must not even learn whether its
    # revision would have won.
    conflict = _binding_conflict(row, identity)
    if conflict:
        logger.info("execution store: refusing advance on execution %s — %s", row.id, conflict)
        return _conflict(row, conflict)

    if row.revision != advance.expected_revision:
        # Someone moved this row between the caller's read and this write. Nothing is
        # written. The current record comes back so the caller can decide what to do
        # now — deliberately NOT re-applied at the current revision, because whatever
        # moved the row may have changed what should happen next.
        logger.info(
            "execution store: stale advance on execution %s (caller held revision %s, row is at %s)",
            row.id,
            advance.expected_revision,
            row.revision,
        )
        return ExecutionOutcome(kind=OutcomeKind.STALE, record=_to_record(row), reason="stale_revision")

    # The authority fence, made durable, recorded before any of the write lands.
    # `_binding_conflict` above deliberately admits a NEWER generation, because a
    # legitimate handover advances it and the new owner continues this same execution —
    # so once that owner writes, the row must record the generation it wrote under.
    # Without this the row keeps the generation it was created at, the displaced
    # worker's older generation is no longer *older* than the stored one, no conflict
    # is detected, and the request falls through to the revision fence and is answered
    # STALE. That fails OPEN: STALE is retryable by this module's contract, so the
    # displaced worker re-reads, retries at the current revision and writes progress
    # against work it no longer owns. Placed after the authority check so only a caller
    # with standing can move it, and raised via `_adopt_generation` so it is monotonic
    # rather than a plain assignment — see that helper for why lowering it must be
    # impossible even though today's authority check never admits a lower one.
    if advance.consume_attempt:
        from .flow_execution import flow_is_paused

        # _live_authority_conflict already holds the flow lock. A pause that
        # wins the reservation race must not consume an attempt or create intent.
        if await flow_is_paused(session, org_id=identity.org_id, flow_id=flow_id):
            return ExecutionOutcome(kind=OutcomeKind.CONFLICT, record=_to_record(row), reason="flow_paused")
    _adopt_generation(row, identity)

    status = ExecutionStatus.BLOCKED if block is not None else advance.status
    if status in _NO_NEXT_CHECK:
        # A terminal row carrying a wake-up time would be picked up by a runner that
        # trusts the time, so the pairing is enforced here rather than left to every
        # caller to remember.
        next_check_at = None
    elif advance.next_check_at is None:
        raise ExecutionStoreError(
            "missing_next_check",
            f"A {status.value} execution must carry a next_check_at; without one the work becomes invisible to pickup.",
        )
    else:
        next_check_at = advance.next_check_at

    now = _now()
    if (row.phase, row.status) != (advance.phase.value, status.value):
        # Scheduling polls also change revision/progressed_at. Retain only actual
        # transitions so an exact capture can reject A -> B -> A without treating
        # a harmless next_check_at update as changed release authority.
        from .models import OrchestrationDecision

        session.add(
            OrchestrationDecision(
                org_id=row.org_id,
                flow_id=row.flow_id,
                node_id=row.node_id,
                kind="execution_phase_changed",
                actor_id="system:execution-store",
                actor_role="engine",
                actor_kind="service",
                created_at=now,
                reason=json.dumps(
                    {
                        "execution_id": row.id,
                        "cycle": row.cycle,
                        "previous_revision": row.revision,
                        "revision": row.revision + 1,
                        "from_phase": row.phase,
                        "from_status": row.status,
                        "to_phase": advance.phase.value,
                        "to_status": status.value,
                        "claim_id": row.claim_id,
                        "claim_generation": row.claim_generation,
                    },
                    sort_keys=True,
                ),
            )
        )
    row.phase = advance.phase.value
    row.status = status.value
    row.next_check_at = next_check_at
    # Advanced by exactly one on every applied write; that is what makes the caller's
    # next compare-and-set meaningful.
    row.revision = row.revision + 1
    if advance.consume_attempt:
        # Counted here rather than by callers, who would each count differently and so
        # disagree about when the existing attempt bound is reached.
        row.attempts = row.attempts + 1
    if advance.deadline_at is not None:
        row.deadline_at = advance.deadline_at
    if advance.progress_note is not None:
        row.progress_note = advance.progress_note

    if block is None:
        # Every block column cleared together: a stale owner or required_input left
        # behind would describe a block that no longer exists, which is worse than no
        # information at all.
        row.block_code = None
        row.block_owner = None
        row.block_required_input = None
        row.block_remaining_gates = None
        row.block_detail = None
        # Real forward movement. `progressed_at` is what distinguishes "stuck for a
        # minute" from "stuck since Tuesday" on the block record that may follow.
        row.progressed_at = now
    else:
        row.block_code = block.code.value
        row.block_owner = block.owner
        row.block_required_input = block.required_input
        row.block_remaining_gates = _encode_gates(block.remaining_gates)
        row.block_detail = block.detail
        # `progressed_at` deliberately NOT touched: becoming blocked is not progress,
        # and overwriting it would reset the very clock an operator uses to see how
        # long this has been stuck.

    # `pending_action_key` is what makes AWAITING_EXTERNAL actionable — it names the
    # step a recovering process must go and ask about. Set explicitly, or derived from
    # an accompanying intent when the caller did not name one.
    if pending_action_key is not None:
        row.pending_action_key = pending_action_key or None
    elif status is ExecutionStatus.AWAITING_EXTERNAL and intent is not None:
        row.pending_action_key = intent.operation_key
    elif status is not ExecutionStatus.AWAITING_EXTERNAL:
        # Nothing is being waited on, so a leftover key would point at a step that has
        # already been settled.
        row.pending_action_key = None

    if notification_receipt_ref is not None:
        row.notification_receipt_ref = notification_receipt_ref or None
    if handoff_receipt_ref is not None:
        row.handoff_receipt_ref = handoff_receipt_ref or None

    # Flushed BEFORE the action is prepared, and the record projected here, for two
    # reasons. `prepare_action` opens a SAVEPOINT; a rollback of that savepoint would
    # otherwise also undo an UPDATE flushed inside it and expire the attributes we are
    # about to read. Flushing first puts the advance outside the savepoint's reach —
    # still in the caller's transaction, so the atomicity guarantee is unchanged: a
    # failure anywhere still rolls the whole thing back together.
    await session.flush()
    record = _to_record(row)

    action_record: ActionRecord | None = None
    if intent is not None:
        # In the SAME transaction as the advance. This is the store-both-atomically
        # requirement: the intent and the continuation that will follow it up either
        # both survive or neither does.
        prepared = await prepare_action(session, identity=identity, intent=intent)
        if prepared.kind is not OutcomeKind.APPLIED:
            # Unreachable through the ordinary paths — authority was checked above and
            # the execution exists — but if it ever happens the advance must not
            # silently proceed without the action it was scheduled around.
            raise ExecutionStoreError(
                "action_not_prepared",
                f"Could not prepare action {intent.operation_key} alongside the advance ({prepared.reason}).",
            )
        action_record = prepared.action

    logger.info(
        "execution store: advanced execution %s to phase=%s status=%s revision=%s%s",
        record.id,
        record.phase.value,
        record.status.value,
        record.revision,
        f" block={block.code.value}" if block is not None else "",
    )
    return ExecutionOutcome(
        kind=OutcomeKind.BLOCKED if block is not None else OutcomeKind.APPLIED,
        record=record,
        action=action_record,
        reason=block.code.value if block is not None else None,
    )
