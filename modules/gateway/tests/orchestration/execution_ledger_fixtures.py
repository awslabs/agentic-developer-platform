"""Published fixtures for the execution/action ledger (#5142) consumers.

Two siblings integrate against this store before it has a runtime caller, and both
need something concrete to build on:

- **#5145 (ENGINE-K4, read model)** needs populated DTOs — a record in each
  interesting shape (runnable, awaiting external, blocked, concluded) so a view can
  be built and asserted without a database.
- **#5143 (ENGINE-K2, runner)** needs a synthetic adapter — an in-memory stand-in
  with the real store's signatures and the real store's *refusals*, so the runner's
  logic can be tested without PostgreSQL.

These live under `tests/` because they are test material, not production code, and
`test_execution_store.py` asserts that both stay faithful to the real store. That
assertion is the point: a fixture that drifts from the thing it imitates sends a
sibling issue down a path that cannot work, and the failure surfaces in *their*
branch, where it is much more expensive to diagnose.

The synthetic store deliberately reproduces the refusals rather than only the happy
path. A runner tested against a stand-in that always succeeds is a runner with no
tested handling for `STALE` or `CONFLICT`, which are the cases the whole design
exists for.

Nothing here contains a credential or a transcript. The `*_ref` values are
deliberately shaped like references (an S3 key, a PR node id, a receipt id) so a
consumer copying a fixture does not learn the wrong habit.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from src.orchestration.execution_state import (
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

__all__ = [
    "SyntheticExecutionStore",
    "action_records",
    "execution_records",
    "read_model_fixture",
    "sample_identity",
]

FIXTURE_ORG = "org-fixture"
FIXTURE_FLOW = "flow-fixture-0001"
FIXTURE_NODE = "node-fixture-0001"
FIXTURE_CLAIM = "claim-fixture-0001"
FIXTURE_PLAN_VERSION = 4

# A fixed instant, so a fixture-driven assertion is reproducible. Real writers use
# `datetime.now(UTC)`; a frozen value here keeps a rendered view byte-comparable.
FIXTURE_NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)


def sample_identity(*, cycle: int = 1, generation: int = 1) -> ExecutionIdentity:
    """The authority binding a caller presents on every store call."""
    return ExecutionIdentity(
        org_id=FIXTURE_ORG,
        node_id=FIXTURE_NODE,
        cycle=cycle,
        accepted_plan_version=FIXTURE_PLAN_VERSION,
        claim_id=FIXTURE_CLAIM,
        claim_generation=generation,
    )


def _record(
    *,
    phase: ExecutionPhase,
    status: ExecutionStatus,
    revision: int,
    execution_id: str,
    next_check_at: datetime | None,
    **overrides,
) -> ExecutionRecord:
    base = {
        "id": execution_id,
        "org_id": FIXTURE_ORG,
        "flow_id": FIXTURE_FLOW,
        "node_id": FIXTURE_NODE,
        "cycle": 1,
        "phase": phase,
        "status": status,
        "revision": revision,
        "accepted_plan_version": FIXTURE_PLAN_VERSION,
        "claim_id": FIXTURE_CLAIM,
        "claim_generation": 1,
        "attempts": 0,
        "next_check_at": next_check_at,
        "progressed_at": FIXTURE_NOW,
        "created_at": FIXTURE_NOW,
        "updated_at": FIXTURE_NOW,
    }
    base.update(overrides)
    return ExecutionRecord(**base)


def execution_records() -> dict[str, ExecutionRecord]:
    """One populated record per shape a read model must render.

    Keyed by shape rather than returned as a list so a view test can name the case
    it is asserting, and so a case added later does not silently shift an index.
    """
    return {
        # Freshly admitted: due now, nothing attempted yet.
        "runnable": _record(
            phase=ExecutionPhase.ADMITTED,
            status=ExecutionStatus.RUNNABLE,
            revision=1,
            execution_id="exec-fixture-runnable",
            next_check_at=FIXTURE_NOW,
        ),
        # Mid-delivery, attempts consumed, a deadline in view.
        "delivering": _record(
            phase=ExecutionPhase.DELIVERING,
            status=ExecutionStatus.RUNNABLE,
            revision=4,
            execution_id="exec-fixture-delivering",
            next_check_at=FIXTURE_NOW + timedelta(minutes=2),
            attempts=1,
            deadline_at=FIXTURE_NOW + timedelta(hours=4),
            progress_note="patch prepared; submitting next",
        ),
        # Waiting on a provider. `pending_action_key` is what a recovering process
        # goes and asks about, which is why a read model must surface it.
        "awaiting_external": _record(
            phase=ExecutionPhase.AWAITING_REVIEW,
            status=ExecutionStatus.AWAITING_EXTERNAL,
            revision=7,
            execution_id="exec-fixture-awaiting",
            next_check_at=FIXTURE_NOW + timedelta(minutes=15),
            attempts=1,
            pending_action_key="pr:node-fixture-0001:cycle-1",
            notification_receipt_ref="ses/0190a7c4f1d2",
        ),
        # Blocked on a human gate: the row carries everything needed to route it.
        "blocked_human_gate": _record(
            phase=ExecutionPhase.SETTLING,
            status=ExecutionStatus.BLOCKED,
            revision=9,
            execution_id="exec-fixture-blocked",
            next_check_at=FIXTURE_NOW + timedelta(hours=1),
            attempts=2,
            block=BlockRecord(
                code=BlockCode.HUMAN_GATE_REQUIRED,
                owner="platform-operator",
                required_input="approve the wave gate for node-fixture-0001",
                remaining_gates=("gate:security-review", "gate:cost-approval"),
                # Stamped at the last *real* progress, not at the moment of blocking —
                # that is what makes "stuck since" meaningful to an operator.
                progressed_at=FIXTURE_NOW - timedelta(hours=3),
                detail="gate opened by the engine; awaiting a human decision",
            ),
        ),
        # Attempt bound reached. Recorded, not resolved: recovery is the existing
        # authorized path, and a read model must not present this as retryable.
        "blocked_attempts_exhausted": _record(
            phase=ExecutionPhase.REPAIRING,
            status=ExecutionStatus.BLOCKED,
            revision=12,
            execution_id="exec-fixture-exhausted",
            next_check_at=FIXTURE_NOW + timedelta(hours=6),
            attempts=3,
            block=BlockRecord(
                code=BlockCode.ATTEMPTS_EXHAUSTED,
                owner="platform-operator",
                required_input="authorized recovery decision",
                progressed_at=FIXTURE_NOW - timedelta(hours=8),
            ),
        ),
        # Terminal: no next check time at all, by construction.
        "concluded": _record(
            phase=ExecutionPhase.CONCLUDED,
            status=ExecutionStatus.CONCLUDED,
            revision=14,
            execution_id="exec-fixture-concluded",
            next_check_at=None,
            attempts=1,
            handoff_receipt_ref="issue-comment/5142#c9",
        ),
        # Superseded by a newer cycle or an amendment. Also terminal, but it is not
        # a success, and a view that collapses the two would misreport delivery.
        "superseded": _record(
            phase=ExecutionPhase.CONCLUDED,
            status=ExecutionStatus.SUPERSEDED,
            revision=15,
            execution_id="exec-fixture-superseded",
            next_check_at=None,
            attempts=1,
            progress_note="superseded by cycle 2",
        ),
    }


def action_records() -> dict[str, ActionRecord]:
    """One action per status, including the two that are deliberately unresolved."""

    def _action(key: str, status: ActionStatus, **overrides) -> ActionRecord:
        base = {
            "id": f"action-fixture-{key}",
            "org_id": FIXTURE_ORG,
            "execution_id": "exec-fixture-delivering",
            "operation_key": f"pr:node-fixture-0001:cycle-1:{key}",
            "kind": "open_pull_request",
            "status": status,
            "attempt": 1,
            "artifact_ref": "s3://adp-artifacts/node-fixture-0001/cycle-1/patch.diff",
            "receipt_ref": None,
            "detail": {"base": "main"},
            "created_at": FIXTURE_NOW,
            "observed_at": None,
        }
        base.update(overrides)
        return ActionRecord(**base)

    return {
        "prepared": _action("prepared", ActionStatus.PREPARED),
        "dispatched": _action("dispatched", ActionStatus.DISPATCHED),
        "succeeded": _action(
            "succeeded",
            ActionStatus.SUCCEEDED,
            receipt_ref="pr/PR_kwDOABCD1234",
            observed_at=FIXTURE_NOW,
            detail={"base": "main", "observation": {"probe": "provider-api"}},
        ),
        "failed": _action(
            "failed",
            ActionStatus.FAILED,
            observed_at=FIXTURE_NOW,
            detail={"base": "main", "observation": {"error": "validation_failed"}},
        ),
        # The one a read model must NOT render as either outcome: an observer looked
        # and could not tell. `observed_at` is set (we looked) while the status stays
        # uncertain.
        "unknown": _action(
            "unknown",
            ActionStatus.UNKNOWN,
            observed_at=FIXTURE_NOW,
            detail={"base": "main", "observation": {"probe": "timed out"}},
        ),
    }


def read_model_fixture() -> dict:
    """The flat field set #5145 renders, drawn from the blocked record.

    A dict rather than the dataclass because a read model serializes fields, and the
    thing worth pinning is the field *names* — the failure this catches is a view
    built over a name that was never stored.
    """
    record = execution_records()["blocked_human_gate"]
    return {
        "id": record.id,
        "org_id": record.org_id,
        "flow_id": record.flow_id,
        "node_id": record.node_id,
        "cycle": record.cycle,
        "phase": record.phase,
        "status": record.status,
        "revision": record.revision,
        "accepted_plan_version": record.accepted_plan_version,
        "claim_id": record.claim_id,
        "claim_generation": record.claim_generation,
        "attempts": record.attempts,
        "next_check_at": record.next_check_at,
        "deadline_at": record.deadline_at,
        "progressed_at": record.progressed_at,
        "progress_note": record.progress_note,
        "block": record.block,
        "pending_action_key": record.pending_action_key,
        "notification_receipt_ref": record.notification_receipt_ref,
        "handoff_receipt_ref": record.handoff_receipt_ref,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


class SyntheticExecutionStore:
    """In-memory stand-in with the real store's signatures and refusals (#5143).

    The runner needs to exercise its own control flow — advance, observe, retry,
    give up — without a database. What makes this fixture useful rather than
    misleading is that it reproduces the *refusals*: stale revisions, lapsed
    authority, the next-check pairing, and idempotent action keys. A stand-in that
    always succeeds would leave the runner's handling of `STALE` and `CONFLICT`
    untested, and those are the cases the design exists for.

    The `session` parameter is accepted and ignored, so a caller can pass `None` and
    keep the same call shape as the real store.

    What it does NOT imitate: row locking and genuine concurrency. Those are
    PostgreSQL behaviors; `test_execution_store_postgres.py` covers them.
    """

    def __init__(self) -> None:
        self._executions: dict[tuple[str, str, int], ExecutionRecord] = {}
        self._actions: dict[tuple[str, str], ActionRecord] = {}
        self._now = FIXTURE_NOW

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _key(identity: ExecutionIdentity) -> tuple[str, str, int]:
        return (identity.org_id, identity.node_id, identity.cycle)

    @staticmethod
    def _conflict_reason(record: ExecutionRecord, identity: ExecutionIdentity) -> str | None:
        """Mirrors `execution_store._binding_conflict`, including its asymmetry.

        A *newer* claim generation is not a conflict — a legitimate handover advances
        it and the new owner must be able to continue. Only an older one is stale.
        That asymmetry is only safe because `_adopt_generation` records the newer
        generation, exactly as `execution_store._adopt_generation` does.
        """
        if record.org_id != identity.org_id:
            return "tenant_mismatch"
        if record.claim_id != identity.claim_id:
            return "claim_mismatch"
        if identity.claim_generation < record.claim_generation:
            return "claim_generation_superseded"
        if record.accepted_plan_version != identity.accepted_plan_version:
            return "accepted_plan_version_mismatch"
        return None

    @staticmethod
    def _withholds_record(reason: str) -> bool:
        """Mirrors `execution_store._conflict`: a caller that never held the claim
        learns nothing from the refusal, so it cannot harvest the binding that would
        satisfy the next authority check. A merely lapsed owner still gets the row.
        """
        return reason in ("tenant_mismatch", "claim_mismatch")

    def _adopt_generation(self, identity: ExecutionIdentity, record: ExecutionRecord) -> ExecutionRecord:
        """Raise the stored generation to a newer writer's, mirroring the real store.

        Reproduced here because a stand-in that let a superseded generation keep
        writing would leave a runner's handling of a handover untested against the
        behavior it will actually meet: the real store refuses that caller with
        `CONFLICT`/`claim_generation_superseded` on every later attempt, not merely
        with a one-off `STALE`.
        """
        if identity.claim_generation <= record.claim_generation:
            return record
        adopted = replace(record, claim_generation=identity.claim_generation)
        self._executions[self._key(identity)] = adopted
        return adopted

    def _require(self, identity: ExecutionIdentity) -> ExecutionRecord:
        record = self._executions.get(self._key(identity))
        if record is None:
            raise ExecutionStoreError(
                "unknown_execution",
                f"No execution exists for node {identity.node_id} cycle {identity.cycle}.",
            )
        return record

    # -- the five operations ---------------------------------------------

    async def create_execution(
        self,
        session=None,
        *,
        identity: ExecutionIdentity,
        flow_id: str,
        phase: ExecutionPhase = ExecutionPhase.ADMITTED,
        next_check_at: datetime | None = None,
        deadline_at: datetime | None = None,
    ) -> ExecutionOutcome:
        existing = self._executions.get(self._key(identity))
        if existing is not None:
            reason = self._conflict_reason(existing, identity)
            if reason:
                return ExecutionOutcome(
                    kind=OutcomeKind.CONFLICT,
                    record=None if self._withholds_record(reason) else existing,
                    reason=reason,
                )
            # Progress is never rewound — re-creating must not replay delivery from
            # the start — but the adopting generation is recorded, as the real store
            # does, so an earlier generation cannot write afterwards.
            return ExecutionOutcome(kind=OutcomeKind.APPLIED, record=self._adopt_generation(identity, existing))

        record = _record(
            phase=phase,
            status=ExecutionStatus.RUNNABLE,
            revision=1,
            execution_id=f"exec-synthetic-{uuid.uuid5(uuid.NAMESPACE_URL, str(self._key(identity)))}",
            next_check_at=next_check_at or self._now,
            cycle=identity.cycle,
            org_id=identity.org_id,
            flow_id=flow_id,
            node_id=identity.node_id,
            claim_id=identity.claim_id,
            claim_generation=identity.claim_generation,
            accepted_plan_version=identity.accepted_plan_version,
            deadline_at=deadline_at,
        )
        self._executions[self._key(identity)] = record
        return ExecutionOutcome(kind=OutcomeKind.APPLIED, record=record)

    async def load_execution(
        self,
        session=None,
        *,
        identity: ExecutionIdentity,
        for_update: bool = False,
    ) -> ExecutionOutcome | None:
        record = self._executions.get(self._key(identity))
        if record is None:
            return None
        reason = self._conflict_reason(record, identity)
        if reason:
            return ExecutionOutcome(
                kind=OutcomeKind.CONFLICT,
                record=None if self._withholds_record(reason) else record,
                reason=reason,
            )
        return ExecutionOutcome(kind=OutcomeKind.APPLIED, record=record)

    async def prepare_action(self, session=None, *, identity: ExecutionIdentity, intent: ActionIntent) -> ExecutionOutcome:
        record = self._require(identity)
        reason = self._conflict_reason(record, identity)
        if reason:
            return ExecutionOutcome(
                kind=OutcomeKind.CONFLICT,
                record=None if self._withholds_record(reason) else record,
                reason=reason,
            )
        record = self._adopt_generation(identity, record)

        action_key = (record.id, intent.operation_key)
        existing = self._actions.get(action_key)
        if existing is not None:
            # The original record, with its original status — what tells a retrying
            # caller the step may already have taken effect.
            return ExecutionOutcome(
                kind=OutcomeKind.APPLIED,
                record=record,
                action=existing,
                reason="action_already_prepared",
            )

        action = ActionRecord(
            id=f"action-synthetic-{uuid.uuid5(uuid.NAMESPACE_URL, str(action_key))}",
            org_id=identity.org_id,
            execution_id=record.id,
            operation_key=intent.operation_key,
            kind=intent.kind,
            status=ActionStatus.PREPARED,
            attempt=record.attempts,
            artifact_ref=intent.artifact_ref,
            receipt_ref=None,
            detail=dict(intent.detail or {}),
            created_at=self._now,
            observed_at=None,
        )
        self._actions[action_key] = action
        return ExecutionOutcome(kind=OutcomeKind.APPLIED, record=record, action=action)

    async def record_observation(self, session=None, *, identity: ExecutionIdentity, observation: Observation) -> ExecutionOutcome:
        record = self._require(identity)
        reason = self._conflict_reason(record, identity)
        if reason:
            return ExecutionOutcome(
                kind=OutcomeKind.CONFLICT,
                record=None if self._withholds_record(reason) else record,
                reason=reason,
            )
        record = self._adopt_generation(identity, record)

        action_key = (record.id, observation.operation_key)
        action = self._actions.get(action_key)
        if action is None:
            raise ExecutionStoreError(
                "unknown_action",
                f"No action {observation.operation_key} is recorded on execution {record.id}.",
            )

        status = {
            ObservedOutcome.SUCCEEDED: ActionStatus.SUCCEEDED,
            ObservedOutcome.FAILED: ActionStatus.FAILED,
            # Never upgraded to success. `observed_at` is still stamped: we looked.
            ObservedOutcome.INDETERMINATE: ActionStatus.UNKNOWN,
        }[observation.outcome]
        detail = dict(action.detail)
        if observation.detail:
            detail["observation"] = observation.detail
        settled = replace(
            action,
            status=status,
            observed_at=self._now,
            receipt_ref=observation.receipt_ref or action.receipt_ref,
            detail=detail,
        )
        self._actions[action_key] = settled
        return ExecutionOutcome(kind=OutcomeKind.APPLIED, record=record, action=settled)

    async def advance_execution(
        self,
        session=None,
        *,
        identity: ExecutionIdentity,
        advance: PhaseAdvance,
        intent: ActionIntent | None = None,
        block: BlockRecord | None = None,
        pending_action_key: str | None = None,
        notification_receipt_ref: str | None = None,
        handoff_receipt_ref: str | None = None,
    ) -> ExecutionOutcome:
        record = self._require(identity)
        reason = self._conflict_reason(record, identity)
        if reason:
            return ExecutionOutcome(
                kind=OutcomeKind.CONFLICT,
                record=None if self._withholds_record(reason) else record,
                reason=reason,
            )
        if record.revision != advance.expected_revision:
            # The case a runner must handle: nothing written, current record returned.
            return ExecutionOutcome(kind=OutcomeKind.STALE, record=record, reason="stale_revision")
        record = self._adopt_generation(identity, record)

        status = ExecutionStatus.BLOCKED if block is not None else advance.status
        if status in (ExecutionStatus.CONCLUDED, ExecutionStatus.SUPERSEDED):
            next_check_at = None
        elif advance.next_check_at is None:
            raise ExecutionStoreError(
                "missing_next_check",
                f"A {status.value} execution must carry a next_check_at; without one the work becomes invisible to pickup.",
            )
        else:
            next_check_at = advance.next_check_at

        if pending_action_key is not None:
            resolved_key = pending_action_key or None
        elif status is ExecutionStatus.AWAITING_EXTERNAL and intent is not None:
            resolved_key = intent.operation_key
        elif status is ExecutionStatus.AWAITING_EXTERNAL:
            resolved_key = record.pending_action_key
        else:
            resolved_key = None

        advanced = replace(
            record,
            phase=advance.phase,
            status=status,
            revision=record.revision + 1,
            next_check_at=next_check_at,
            attempts=record.attempts + (1 if advance.consume_attempt else 0),
            deadline_at=advance.deadline_at if advance.deadline_at is not None else record.deadline_at,
            progress_note=advance.progress_note if advance.progress_note is not None else record.progress_note,
            block=block,
            # Not touched when blocking: becoming blocked is not progress.
            progressed_at=record.progressed_at if block is not None else self._now,
            pending_action_key=resolved_key,
            notification_receipt_ref=(notification_receipt_ref or None) if notification_receipt_ref is not None else record.notification_receipt_ref,
            handoff_receipt_ref=(handoff_receipt_ref or None) if handoff_receipt_ref is not None else record.handoff_receipt_ref,
            updated_at=self._now,
        )
        self._executions[self._key(identity)] = advanced

        action = None
        if intent is not None:
            prepared = await self.prepare_action(None, identity=identity, intent=intent)
            action = prepared.action

        return ExecutionOutcome(
            kind=OutcomeKind.BLOCKED if block is not None else OutcomeKind.APPLIED,
            record=advanced,
            action=action,
            reason=block.code.value if block is not None else None,
        )
