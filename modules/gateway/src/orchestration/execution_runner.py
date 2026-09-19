"""Bounded recovery runner for durable orchestration executions (#5143).

The runner owns scheduling and crash recovery, while phase adapters own provider
semantics.  The ordering is deliberate: observe an unresolved action, decide,
persist the next intent and wake-up, commit, then perform provider I/O.  No
network call is made while a ledger row is locked.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.shared.logging import get_logger

from .execution_policy import Action
from .execution_state import (
    TERMINAL_EXECUTION_STATUSES,
    ActionIntent,
    ActionStatus,
    BlockCode,
    BlockRecord,
    ExecutionIdentity,
    ExecutionOutcome,
    ExecutionPhase,
    ExecutionRecord,
    ExecutionStatus,
    Observation,
    ObservedOutcome,
    OutcomeKind,
    PhaseAdvance,
)
from .execution_store import advance_execution, load_execution, record_observation
from .models import ClaimState, OrchestrationAction, OrchestrationExecution, OrchestrationWorkClaim
from .notify import Notification, NotificationError, notify
from .policy_admission import load_in_force_policy
from .work_claims import OwnerKind

logger = get_logger(__name__)

FEATURE_FLAG_ENV = "FEATURE_ORCHESTRATION_ENGINE_ENABLED"


class ObservationKind(StrEnum):
    """Provider facts a phase adapter can report without implying a decision."""

    READY = "ready"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    BLOCKED = "blocked"


class DecisionKind(StrEnum):
    """Pure outcomes from ``ExecutionHandler.decide``."""

    WAIT = "wait"
    EFFECT = "effect"
    ADVANCE = "advance"
    BLOCK = "block"
    CONCLUDE = "conclude"
    ALREADY_DONE = "already_done"


class EffectOutcome(StrEnum):
    """What an adapter knows after attempting an external effect."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class HandlerObservation:
    kind: ObservationKind
    operation_key: str | None = None
    receipt_ref: str | None = None
    retry_at: datetime | None = None
    detail: str | None = None
    block: BlockRecord | None = None

    def __post_init__(self) -> None:
        if self.kind is ObservationKind.BLOCKED and self.block is None:
            raise ValueError("a blocked observation must carry a BlockRecord")


@dataclass(frozen=True)
class EffectRequest:
    """An idempotently named provider effect, persisted before it is attempted."""

    intent: ActionIntent
    action: Action
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("effect timeout_seconds must be positive")


@dataclass(frozen=True)
class HandlerDecision:
    kind: DecisionKind
    phase: ExecutionPhase | None = None
    status: ExecutionStatus | None = None
    next_check_at: datetime | None = None
    effect: EffectRequest | None = None
    block: BlockRecord | None = None
    progress_note: str | None = None
    settlement: TransactionMutation | None = None

    def __post_init__(self) -> None:
        if self.kind is DecisionKind.EFFECT and self.effect is None:
            raise ValueError("an effect decision must carry an EffectRequest")
        if self.kind is DecisionKind.BLOCK and self.block is None:
            raise ValueError("a block decision must carry a BlockRecord")
        if self.kind is not DecisionKind.EFFECT and self.effect is not None:
            raise ValueError("only an effect decision may carry an EffectRequest")
        if self.kind is not DecisionKind.BLOCK and self.status is ExecutionStatus.BLOCKED:
            raise ValueError("a blocked status must be expressed as a block decision with a BlockRecord")
        if self.kind in {DecisionKind.WAIT, DecisionKind.EFFECT, DecisionKind.ADVANCE} and self.status in TERMINAL_EXECUTION_STATUSES:
            raise ValueError("terminal work must use conclude or already_done")


@dataclass(frozen=True)
class EffectResult:
    outcome: EffectOutcome
    receipt_ref: str | None = None
    retry_at: datetime | None = None
    detail: str | None = None
    settlement: TransactionMutation | None = None


@dataclass(frozen=True)
class RunnerContext:
    identity: ExecutionIdentity
    execution: ExecutionRecord
    now: datetime


TransactionMutation = Callable[[AsyncSession, RunnerContext], Awaitable[None]]


@dataclass(frozen=True)
class OperationIdentity:
    """Stable effect identity including every authority fence and provider binding."""

    execution_id: str
    cycle: int
    accepted_plan_version: int
    claim_id: str
    claim_generation: int
    effect_kind: str
    provider_bindings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.effect_kind.strip() or not self.execution_id.strip() or not self.claim_id.strip():
            raise ValueError("operation identity requires effect, execution, and claim identifiers")
        if any(not binding.strip() for binding in self.provider_bindings):
            raise ValueError("provider operation bindings must be non-empty")

    @classmethod
    def from_context(cls, context: RunnerContext, effect_kind: str, *provider_bindings: str) -> OperationIdentity:
        return cls(
            execution_id=context.execution.id,
            cycle=context.execution.cycle,
            accepted_plan_version=context.execution.accepted_plan_version,
            claim_id=context.execution.claim_id,
            claim_generation=context.execution.claim_generation,
            effect_kind=effect_kind,
            provider_bindings=tuple(provider_bindings),
        )

    @property
    def key(self) -> str:
        authority = (
            f"{self.effect_kind}:execution={self.execution_id}:cycle={self.cycle}:"
            f"plan={self.accepted_plan_version}:claim={self.claim_id}:generation={self.claim_generation}"
        )
        if not self.provider_bindings:
            if len(authority) > 255:
                raise ValueError("operation authority fields exceed the 255-character ledger key limit")
            return authority
        # Length-prefix each binding rather than joining on a bare ":".  A plain join
        # is not injective: ("title=a", "body=b:title=c") and ("title=a:body=b",
        # "title=c") flatten to the same text, so two genuinely different effects would
        # share one operation key and the second would adopt the first's prepared
        # action instead of being carried out.  The length prefix is self-delimiting,
        # so the bindings can always be recovered and distinct tuples stay distinct.
        # It also makes the digest namespace below unspoofable: a prefixed binding
        # always begins with digits and "=", never with "bindings_sha256=".
        provider_text = ":".join(f"{len(binding)}={binding}" for binding in self.provider_bindings)
        candidate = f"{authority}:{provider_text}"
        if len(candidate) <= 255:
            return candidate
        digest = hashlib.sha256(provider_text.encode()).hexdigest()
        candidate = f"{authority}:bindings_sha256={digest}"
        if len(candidate) > 255:
            raise ValueError("operation authority fields exceed the 255-character ledger key limit")
        return candidate


class ExecutionHandler(Protocol):
    """Published phase-adapter contract used by #5147/#5149/#5151/#5154."""

    async def observe(self, context: RunnerContext) -> HandlerObservation: ...

    def decide(self, context: RunnerContext, observation: HandlerObservation) -> HandlerDecision: ...

    async def perform(self, context: RunnerContext, effect: EffectRequest) -> EffectResult: ...


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


AuthorityVerifier = Callable[
    [async_sessionmaker[AsyncSession], ExecutionRecord, EffectRequest, datetime],
    Awaitable[BlockRecord | None],
]
Notifier = Callable[[Notification], str | Awaitable[str]]
Checkpoint = Callable[[str, RunnerContext], None | Awaitable[None]]


@dataclass(frozen=True)
class RunnerConfig:
    enabled: bool = False
    max_actions: int = 20
    io_timeout_seconds: float = 30.0
    time_budget_seconds: float = 50.0
    retry_seconds: int = 60
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if self.max_actions < 1 or self.io_timeout_seconds <= 0 or self.time_budget_seconds <= 0:
            raise ValueError("runner action, I/O, and time bounds must be positive")
        if self.retry_seconds < 1 or self.max_attempts < 1:
            raise ValueError("runner retry and attempt bounds must be positive")

    @classmethod
    def from_env(cls) -> RunnerConfig:
        def _positive_int(name: str, default: int) -> int:
            try:
                value = int(os.environ.get(name, str(default)))
            except ValueError:
                return default
            return value if value > 0 else default

        def _positive_float(name: str, default: float) -> float:
            try:
                value = float(os.environ.get(name, str(default)))
            except ValueError:
                return default
            return value if value > 0 else default

        return cls(
            # Literal "true" only, matching `features.routes._is_enabled_strict` and the
            # other engine passes (`tracker_projection`, `engine_commands`, `diagnose`).
            # Truthy-string semantics here would let `1` start the runner while the
            # features endpoint — and therefore the UI — still reported the engine as
            # disabled, which is the opposite of the flag's fail-closed contract
            # (`test_feature_flag_parity` requires "1"/"yes"/"enabled" to resolve off).
            enabled=(os.environ.get(FEATURE_FLAG_ENV) or "").strip().lower() == "true",
            max_actions=_positive_int("ORCH_RUNNER_MAX_ACTIONS", 20),
            io_timeout_seconds=_positive_float("ORCH_RUNNER_IO_TIMEOUT_SECONDS", 30.0),
            time_budget_seconds=_positive_float("ORCH_RUNNER_TIME_BUDGET_SECONDS", 50.0),
            retry_seconds=_positive_int("ORCH_RUNNER_RETRY_SECONDS", 60),
            max_attempts=_positive_int("ORCH_RUNNER_MAX_ATTEMPTS", 3),
        )


@dataclass
class RunnerReport:
    enabled: bool = False
    examined: int = 0
    reserved: int = 0
    observed: int = 0
    effects_attempted: int = 0
    effects_succeeded: int = 0
    effects_failed: int = 0
    effects_uncertain: int = 0
    advanced: int = 0
    blocked: int = 0
    stale: int = 0
    conflicts: int = 0
    notifications_sent: int = 0
    notifications_failed: int = 0
    # Deliveries that already reached a recorded terminal state — exhausted retries or
    # an outcome the provider cannot be asked about again. Counted separately from
    # `notifications_failed` because nothing new failed in this pass: the block and its
    # evidence are already durable. Folding them together would make every later tick
    # report failure for one old undeliverable notice, which both hides a genuine new
    # failure and leaves the scheduled tick permanently red with no action that clears it.
    notifications_unresolved: int = 0
    errors: int = 0
    capped: bool = False
    per_org: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.errors == 0 and self.notifications_failed == 0

    def bump(self, org_id: str, key: str) -> None:
        bucket = self.per_org.setdefault(org_id, {})
        bucket[key] = bucket.get(key, 0) + 1


_handlers: dict[ExecutionPhase, ExecutionHandler] = {}


def register_execution_handler(phase: ExecutionPhase, handler: ExecutionHandler) -> None:
    """Register one runtime adapter. Replacing a phase is refused explicitly."""
    if phase in _handlers and _handlers[phase] is not handler:
        raise ValueError(f"an execution handler is already registered for {phase.value}")
    _handlers[phase] = handler


def registered_execution_handlers() -> Mapping[ExecutionPhase, ExecutionHandler]:
    return dict(_handlers)


def _identity(record: ExecutionRecord) -> ExecutionIdentity:
    return ExecutionIdentity(
        org_id=record.org_id,
        node_id=record.node_id,
        cycle=record.cycle,
        accepted_plan_version=record.accepted_plan_version,
        claim_id=record.claim_id,
        claim_generation=record.claim_generation,
    )


def _next(now: datetime, config: RunnerConfig, explicit: datetime | None = None) -> datetime:
    return explicit or now + timedelta(seconds=config.retry_seconds)


async def _call_checkpoint(checkpoint: Checkpoint | None, name: str, context: RunnerContext) -> None:
    if checkpoint is None:
        return
    result = checkpoint(name, context)
    if inspect.isawaitable(result):
        await result


async def _load_due(
    factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    limit: int,
    phases: frozenset[ExecutionPhase],
) -> list[ExecutionRecord]:
    """Claim a bounded, fair snapshot with PostgreSQL row locks.

    The due index begins with ``org_id``.  Resolve the oldest-due tenants first,
    then use an equality probe per tenant so ``status`` and ``next_check_at``
    remain usable index conditions.  ``ORDER BY next_check_at, id`` is both fair
    and deterministic; ordering by id alone would make the due index unusable.
    """
    positive_statuses = sorted(status.value for status in set(ExecutionStatus) - set(TERMINAL_EXECUTION_STATUSES))
    phase_values = sorted(phase.value for phase in phases)
    async with factory() as session:
        oldest_due = func.min(OrchestrationExecution.next_check_at).label("oldest_due")
        org_rows = await session.execute(
            select(OrchestrationExecution.org_id, oldest_due)
            .where(
                OrchestrationExecution.status.in_(positive_statuses),
                OrchestrationExecution.phase.in_(phase_values),
                OrchestrationExecution.accepted_plan_version > 0,
                OrchestrationExecution.next_check_at.is_not(None),
                OrchestrationExecution.next_check_at <= now,
            )
            .group_by(OrchestrationExecution.org_id)
            .order_by(oldest_due, OrchestrationExecution.org_id)
            .limit(limit)
        )
        org_ids = [row.org_id for row in org_rows]
        per_org = max(1, (limit + max(1, len(org_ids)) - 1) // max(1, len(org_ids)))
        rows: list[OrchestrationExecution] = []
        for org_id in org_ids:
            tenant_rows = (
                await session.execute(
                    select(OrchestrationExecution)
                    .where(
                        OrchestrationExecution.org_id == org_id,
                        OrchestrationExecution.status.in_(positive_statuses),
                        OrchestrationExecution.phase.in_(phase_values),
                        OrchestrationExecution.accepted_plan_version > 0,
                        OrchestrationExecution.next_check_at.is_not(None),
                        OrchestrationExecution.next_check_at <= now,
                    )
                    .order_by(OrchestrationExecution.next_check_at, OrchestrationExecution.id)
                    .limit(per_org)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
            rows.extend(tenant_rows)
        rows.sort(
            key=lambda row: (
                (row.next_check_at if row.next_check_at and row.next_check_at.tzinfo else row.next_check_at.replace(tzinfo=UTC))
                if row.next_check_at
                else now,
                row.id,
            )
        )
        rows = rows[:limit]

        records: list[ExecutionRecord] = []
        for row in rows:
            identity = ExecutionIdentity(
                org_id=row.org_id,
                node_id=row.node_id,
                cycle=row.cycle,
                accepted_plan_version=row.accepted_plan_version,
                claim_id=row.claim_id,
                claim_generation=row.claim_generation,
            )
            outcome = await load_execution(session, identity=identity, for_update=True)
            if outcome is not None and outcome.kind is OutcomeKind.APPLIED and outcome.record is not None:
                records.append(outcome.record)
        await session.commit()
        return records


async def verify_live_authority(
    factory: async_sessionmaker[AsyncSession],
    record: ExecutionRecord,
    effect: EffectRequest,
    now: datetime | None = None,
) -> BlockRecord | None:
    """Fail closed on a withdrawn plan or work claim immediately before an effect."""
    async with factory() as session:
        admission = await load_in_force_policy(session, org_id=record.org_id, flow_id=record.flow_id)
        if admission.refusal is not None or admission.policy is None or admission.plan_version != record.accepted_plan_version:
            return BlockRecord(
                code=BlockCode.AUTHORITY_UNVERIFIABLE,
                owner="plan-owner",
                required_input="restore or re-accept the execution policy for this delivery cycle",
                progressed_at=record.progressed_at,
                detail="current accepted-plan authority does not match the execution ledger",
            )
        expires_at = admission.policy.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= (now or datetime.now(UTC)):
            return BlockRecord(
                code=BlockCode.AUTHORITY_UNVERIFIABLE,
                owner="plan-owner",
                required_input="accept a current, unexpired execution policy",
                progressed_at=record.progressed_at,
                detail="the accepted execution policy expired before the effect",
            )
        if not admission.policy.permits(effect.action):
            code = BlockCode.HUMAN_GATE_REQUIRED if effect.action in admission.policy.human_gates else BlockCode.AUTHORITY_UNVERIFIABLE
            return BlockRecord(
                code=code,
                owner="plan-owner",
                required_input=f"authorize or release the {effect.action.value} action",
                progressed_at=record.progressed_at,
                detail="the in-force policy does not permit this effect autonomously",
            )
        if record.attempts >= admission.policy.limits.max_attempts_per_node:
            return BlockRecord(
                code=BlockCode.ATTEMPTS_EXHAUSTED,
                owner="plan-owner",
                required_input="authorized recovery after the accepted per-node attempt limit",
                progressed_at=record.progressed_at,
                detail="the in-force policy attempt allowance is exhausted",
            )

        claim = (
            await session.execute(
                select(OrchestrationWorkClaim).where(
                    OrchestrationWorkClaim.org_id == record.org_id,
                    OrchestrationWorkClaim.id == record.claim_id,
                )
            )
        ).scalar_one_or_none()
        if (
            claim is None
            or claim.owner_kind != OwnerKind.ENGINE_FLOW.value
            or claim.owner_ref != record.flow_id
            or claim.state != ClaimState.HELD.value
            or claim.generation != record.claim_generation
        ):
            return BlockRecord(
                code=BlockCode.OWNERSHIP_LOST,
                owner="orchestration-owner",
                required_input="reconcile the current work claim before another external effect",
                progressed_at=record.progressed_at,
                detail="the execution claim is absent, owned by another lane, released, or on another generation",
            )
    return None


def _pending_key_for(record: ExecutionRecord, status: ExecutionStatus, intent: ActionIntent | None) -> str | None:
    """The pending-action pointer this advance should leave on the row.

    A new intent names the step being waited on. A BLOCKED advance preserves whatever
    was already pending, since a block pauses the execution without settling the
    provider call underneath it. Everything else returns None and lets the store clear
    the column: the step it named is finished or abandoned.
    """
    if intent is not None and status is ExecutionStatus.AWAITING_EXTERNAL:
        return intent.operation_key
    if status is ExecutionStatus.BLOCKED:
        return record.pending_action_key
    return None


async def _advance(
    factory: async_sessionmaker[AsyncSession],
    *,
    record: ExecutionRecord,
    phase: ExecutionPhase,
    status: ExecutionStatus,
    next_check_at: datetime | None,
    intent: ActionIntent | None = None,
    block: BlockRecord | None = None,
    consume_attempt: bool = False,
    progress_note: str | None = None,
    notification_receipt_ref: str | None = None,
    settlement: TransactionMutation | None = None,
    now: datetime | None = None,
) -> ExecutionOutcome:
    async with factory() as session:
        outcome = await advance_execution(
            session,
            identity=_identity(record),
            advance=PhaseAdvance(
                phase=phase,
                status=status,
                expected_revision=record.revision,
                next_check_at=next_check_at,
                consume_attempt=consume_attempt,
                deadline_at=record.deadline_at,
                progress_note=progress_note,
            ),
            intent=intent,
            block=block,
            # Becoming BLOCKED pauses work; it does not settle it. Any unsettled
            # provider action must keep its pointer, because that key is the only
            # thing that sends a recovering process to ask the provider what
            # happened (`_process_one` gates all reconciliation on it). Passing
            # None here would let the store clear it as "nothing is being waited
            # on", stranding a `prepared`/`unknown` action that no later pass ever
            # observes — the exact outcome the ledger exists to prevent. Terminal
            # and forward-moving statuses still clear it: there the step really is
            # settled.
            pending_action_key=_pending_key_for(record, status, intent),
            notification_receipt_ref=notification_receipt_ref,
        )
        if settlement is not None and outcome.record is not None and outcome.kind in {OutcomeKind.APPLIED, OutcomeKind.BLOCKED}:
            await settlement(
                session,
                RunnerContext(
                    identity=_identity(outcome.record),
                    execution=outcome.record,
                    now=now or datetime.now(UTC),
                ),
            )
        await session.commit()
        return outcome


async def _record(
    factory: async_sessionmaker[AsyncSession],
    *,
    record: ExecutionRecord,
    operation_key: str,
    outcome: EffectOutcome,
    receipt_ref: str | None,
    detail: str | None,
) -> ExecutionOutcome:
    observed = {
        EffectOutcome.SUCCEEDED: ObservedOutcome.SUCCEEDED,
        EffectOutcome.FAILED: ObservedOutcome.FAILED,
        EffectOutcome.UNCERTAIN: ObservedOutcome.INDETERMINATE,
    }[outcome]
    async with factory() as session:
        result = await record_observation(
            session,
            identity=_identity(record),
            observation=Observation(
                operation_key=operation_key,
                outcome=observed,
                receipt_ref=receipt_ref,
                detail=detail,
            ),
        )
        await session.commit()
        return result


async def _record_and_advance(
    factory: async_sessionmaker[AsyncSession],
    *,
    record: ExecutionRecord,
    operation_key: str,
    effect_result: EffectResult,
    phase: ExecutionPhase,
    status: ExecutionStatus,
    next_check_at: datetime | None,
    progress_note: str | None,
    now: datetime,
) -> ExecutionOutcome:
    """Commit the provider receipt, phase move and dependency settlement together."""
    observed = {
        EffectOutcome.SUCCEEDED: ObservedOutcome.SUCCEEDED,
        EffectOutcome.FAILED: ObservedOutcome.FAILED,
        EffectOutcome.UNCERTAIN: ObservedOutcome.INDETERMINATE,
    }[effect_result.outcome]
    async with factory() as session:
        observation = await record_observation(
            session,
            identity=_identity(record),
            observation=Observation(
                operation_key=operation_key,
                outcome=observed,
                receipt_ref=effect_result.receipt_ref,
                detail=effect_result.detail,
            ),
        )
        if observation.kind is not OutcomeKind.APPLIED or observation.record is None:
            await session.rollback()
            return observation

        advanced = await advance_execution(
            session,
            identity=_identity(observation.record),
            advance=PhaseAdvance(
                phase=phase,
                status=status,
                expected_revision=observation.record.revision,
                next_check_at=next_check_at,
                deadline_at=observation.record.deadline_at,
                progress_note=progress_note,
            ),
        )
        if advanced.kind is not OutcomeKind.APPLIED or advanced.record is None:
            await session.rollback()
            return advanced
        if effect_result.settlement is not None:
            await effect_result.settlement(
                session,
                RunnerContext(identity=_identity(advanced.record), execution=advanced.record, now=now),
            )
        await session.commit()
        return advanced


def _account(report: RunnerReport, org_id: str, outcome: ExecutionOutcome) -> bool:
    if outcome.kind is OutcomeKind.STALE:
        report.stale += 1
        report.bump(org_id, "stale")
        return False
    if outcome.kind is OutcomeKind.CONFLICT:
        report.conflicts += 1
        report.bump(org_id, "conflicts")
        return False
    return outcome.record is not None


async def _persist_block(
    factory: async_sessionmaker[AsyncSession],
    *,
    record: ExecutionRecord,
    block: BlockRecord,
    now: datetime,
    config: RunnerConfig,
    progress_note: str | None = None,
    notification_receipt_ref: str | None = None,
) -> None:
    """Record the current block and move the wake-up forward.

    Every path that returns early from `_notify_block` — delivered, undeliverable or
    uncertain — still has to come through here, and the write is unconditional rather
    than skipped when the stored block already matches. Two separate things depend on
    it.

    The block itself must be durable. Clearing an execution NULLs every block column,
    so a block that cleared and recurred is not on the row even though an action row
    proves a notice was once attempted for that code. Skipping the write there leaves a
    live block reported as blocked by the tick while the row reads `runnable` with no
    code, owner or required_input.

    The wake-up must move. `next_check_at` is only ever advanced by a write, and
    `_load_due` orders by it ascending, so a blocked row whose wake-up is never
    refreshed keeps its original timestamp and sorts ahead of everything forever. At
    the action cap that starves all later work — including other tenants', since the
    due scan resolves oldest-due orgs first. A row is only examined *because* it was
    already due, so on this path the refresh is always owed.
    """
    await _advance(
        factory,
        record=record,
        phase=record.phase,
        status=ExecutionStatus.BLOCKED,
        next_check_at=_next(now, config),
        block=block,
        progress_note=progress_note,
        notification_receipt_ref=notification_receipt_ref,
    )


async def _notify_block(
    factory: async_sessionmaker[AsyncSession],
    *,
    record: ExecutionRecord,
    block: BlockRecord,
    now: datetime,
    config: RunnerConfig,
    notifier: Notifier,
    report: RunnerReport,
) -> None:
    async with factory() as session:
        actions = (
            await session.execute(
                select(OrchestrationAction)
                .where(
                    OrchestrationAction.org_id == record.org_id,
                    OrchestrationAction.execution_id == record.id,
                    OrchestrationAction.kind == "notify_execution_block",
                )
                .order_by(OrchestrationAction.created_at.desc(), OrchestrationAction.id.desc())
            )
        ).scalars()
        matching = [action for action in actions if (action.detail or {}).get("block_code") == block.code.value]
        previous = matching[0] if matching else None

    failed_deliveries = sum(action.status == ActionStatus.FAILED.value for action in matching)
    if failed_deliveries >= config.max_attempts:
        note = f"block notification attempts exhausted ({failed_deliveries}/{config.max_attempts})"
        await _persist_block(factory, record=record, block=block, now=now, config=config, progress_note=note)
        # Already recorded as exhausted; the block stays durable and due. Nothing new
        # failed here, so this must not re-fail the tick on every subsequent pass.
        report.notifications_unresolved += 1
        report.bump(record.org_id, "notifications_unresolved")
        return

    if previous is not None and previous.status == ActionStatus.SUCCEEDED.value:
        # A delivered notice is not evidence that the block on the row is the block
        # being recorded now, and it is not a wake-up either. This path carries the
        # normal steady state of a blocked execution, so it must persist like the
        # others rather than returning without a write.
        await _persist_block(
            factory,
            record=record,
            block=block,
            now=now,
            config=config,
            progress_note=record.progress_note,
            notification_receipt_ref=previous.receipt_ref or None,
        )
        return
    if previous is not None and previous.status in {
        ActionStatus.PREPARED.value,
        ActionStatus.DISPATCHED.value,
        ActionStatus.UNKNOWN.value,
    }:
        # SNS has no lookup by client-side operation key. An uncertain delivery
        # therefore remains uncertain; resending would be a duplicate effect. The
        # uncertainty is already durable on the action row, so it is reported as
        # unresolved rather than as a fresh failure of this pass.
        #
        # Suppressing the *send* must not suppress the *block*, though. The action row
        # proves a notification was attempted for this code, not that the block this
        # call was asked to record is durable: the block may have cleared since (which
        # NULLs every block column) and then recurred, possibly with different
        # required_input. Without this write the execution would be blocked in the
        # tick's accounting while the row still read `runnable` with no block code,
        # no owner and no wake-up — the one state an operator cannot act on.
        await _persist_block(factory, record=record, block=block, now=now, config=config, progress_note=record.progress_note)
        report.notifications_unresolved += 1
        report.bump(record.org_id, "notifications_unresolved")
        return

    operation_key = f"notify:block:{block.code.value}:{record.cycle}:revision-{record.revision}"
    intent = ActionIntent(operation_key=operation_key, kind="notify_execution_block", detail={"block_code": block.code.value})
    prepared = await _advance(
        factory,
        record=record,
        phase=record.phase,
        status=ExecutionStatus.BLOCKED,
        next_check_at=_next(now, config),
        intent=intent,
        block=block,
        progress_note=record.progress_note,
    )
    if not _account(report, record.org_id, prepared) or prepared.record is None:
        return

    message = Notification(
        org_id=record.org_id,
        flow_id=record.flow_id,
        node_id=record.node_id,
        event="execution_blocked",
        summary=f"Execution blocked: {block.code.value}",
        detail={"owner": block.owner, "required_input": block.required_input, "cycle": record.cycle},
    )
    try:
        if inspect.iscoroutinefunction(notifier):
            receipt = await asyncio.wait_for(notifier(message), timeout=config.io_timeout_seconds)
        else:
            delivered = await asyncio.wait_for(asyncio.to_thread(notifier, message), timeout=config.io_timeout_seconds)
            receipt = await asyncio.wait_for(delivered, timeout=config.io_timeout_seconds) if inspect.isawaitable(delivered) else delivered
    except NotificationError as exc:
        await _record(
            factory,
            record=prepared.record,
            operation_key=operation_key,
            outcome=EffectOutcome.FAILED,
            receipt_ref=None,
            detail=str(exc),
        )
        report.notifications_failed += 1
        report.bump(record.org_id, "notifications_failed")
        return
    except TimeoutError as exc:
        await _record(
            factory,
            record=prepared.record,
            operation_key=operation_key,
            outcome=EffectOutcome.UNCERTAIN,
            receipt_ref=None,
            detail=str(exc) or "notification delivery timed out",
        )
        report.notifications_failed += 1
        report.bump(record.org_id, "notifications_failed")
        return

    observed = await _record(
        factory,
        record=prepared.record,
        operation_key=operation_key,
        outcome=EffectOutcome.SUCCEEDED,
        receipt_ref=str(receipt),
        detail="block notification accepted",
    )
    if observed.record is not None:
        await _advance(
            factory,
            record=observed.record,
            phase=observed.record.phase,
            status=ExecutionStatus.BLOCKED,
            next_check_at=_next(now, config),
            block=block,
            notification_receipt_ref=str(receipt),
        )
    report.notifications_sent += 1
    report.bump(record.org_id, "notifications_sent")


def _deadline_lapsed(record: ExecutionRecord, now: datetime) -> bool:
    """Whether this execution may still be *carried out*, judged against a live clock.

    Asked at more than one point in a pass, because arbitrary real time passes in
    between: `observe` is bounded only by `io_timeout_seconds` and the intent commit
    is a further round-trip. A single early reading would report a deadline that has
    since lapsed as still future.
    """
    return record.deadline_at is not None and record.deadline_at <= now


def _deadline_block(record: ExecutionRecord) -> BlockRecord:
    """One typed block for a lapsed deadline, wherever it is detected.

    Both deadline fences route to the same code and owner: the condition an operator
    must resolve is identical whether it was caught before observation or in the gap
    before the provider call, and two different explanations for one condition would
    misroute recovery.
    """
    return BlockRecord(
        code=BlockCode.ATTEMPTS_EXHAUSTED,
        owner="platform-operator",
        required_input="authorized recovery decision after the execution deadline",
        progressed_at=record.progressed_at,
    )


async def _process_one(
    factory: async_sessionmaker[AsyncSession],
    *,
    initial: ExecutionRecord,
    handler: ExecutionHandler,
    config: RunnerConfig,
    clock: Clock,
    authority_verifier: AuthorityVerifier,
    notifier: Notifier,
    report: RunnerReport,
    checkpoint: Checkpoint | None,
) -> None:
    now = clock.now()
    context = RunnerContext(identity=_identity(initial), execution=initial, now=now)
    await _call_checkpoint(checkpoint, "before_observe", context)

    try:
        observation = await asyncio.wait_for(handler.observe(context), timeout=config.io_timeout_seconds)
    except TimeoutError:
        observation = HandlerObservation(
            kind=ObservationKind.UNCERTAIN,
            operation_key=initial.pending_action_key,
            retry_at=_next(now, config),
            detail="observation timed out",
        )
    report.observed += 1
    report.bump(initial.org_id, "observed")

    # A pending effect is reconciled before any retry decision. This write remains
    # allowed even if effect authority was withdrawn: it records truth, not a new
    # side effect.
    if initial.pending_action_key and observation.kind in {
        ObservationKind.SUCCEEDED,
        ObservationKind.FAILED,
        ObservationKind.UNCERTAIN,
    }:
        observed = await _record(
            factory,
            record=initial,
            operation_key=observation.operation_key or initial.pending_action_key,
            outcome={
                ObservationKind.SUCCEEDED: EffectOutcome.SUCCEEDED,
                ObservationKind.FAILED: EffectOutcome.FAILED,
                ObservationKind.UNCERTAIN: EffectOutcome.UNCERTAIN,
            }[observation.kind],
            receipt_ref=observation.receipt_ref,
            detail=observation.detail,
        )
        if not _account(report, initial.org_id, observed) or observed.record is None:
            return
        initial = observed.record
        context = RunnerContext(identity=_identity(initial), execution=initial, now=now)

    decision = handler.decide(context, observation)

    # A lapsed deadline stops work that would still be *carried out* — it must not
    # overwrite a decision that only records what already happened. Concluding
    # observed work and a handler's own typed block are truth, not new effort:
    # replacing either would strand a finished execution as permanently blocked and
    # would substitute a generic owner for the party who can actually clear it.
    #
    # Judged against a live reading rather than the loop-top capture: `observe` above
    # is bounded only by `io_timeout_seconds`, so a deadline can lapse inside it and
    # the stale timestamp would report it as still future.
    if _deadline_lapsed(initial, clock.now()) and decision.kind in {
        DecisionKind.WAIT,
        DecisionKind.EFFECT,
        DecisionKind.ADVANCE,
    }:
        decision = HandlerDecision(kind=DecisionKind.BLOCK, block=_deadline_block(initial))

    if decision.kind is DecisionKind.BLOCK:
        await _notify_block(
            factory,
            record=initial,
            block=decision.block,  # type: ignore[arg-type]
            now=now,
            config=config,
            notifier=notifier,
            report=report,
        )
        report.blocked += 1
        report.bump(initial.org_id, "blocked")
        return

    if decision.kind in {DecisionKind.CONCLUDE, DecisionKind.ALREADY_DONE}:
        outcome = await _advance(
            factory,
            record=initial,
            phase=ExecutionPhase.CONCLUDED,
            status=ExecutionStatus.CONCLUDED,
            next_check_at=None,
            progress_note=decision.progress_note,
            settlement=decision.settlement,
            now=now,
        )
        if _account(report, initial.org_id, outcome):
            report.advanced += 1
        return

    if decision.kind in {DecisionKind.WAIT, DecisionKind.ADVANCE}:
        status = decision.status or (ExecutionStatus.AWAITING_EXTERNAL if decision.kind is DecisionKind.WAIT else ExecutionStatus.RUNNABLE)
        outcome = await _advance(
            factory,
            record=initial,
            phase=decision.phase or initial.phase,
            status=status,
            next_check_at=_next(now, config, decision.next_check_at),
            progress_note=decision.progress_note,
            settlement=decision.settlement,
            now=now,
        )
        if _account(report, initial.org_id, outcome):
            report.advanced += 1
        return

    # Never issue another effect while the previous outcome is unknown. An
    # adapter cannot accidentally turn uncertainty into an implicit retry.
    if initial.pending_action_key and observation.kind is ObservationKind.UNCERTAIN:
        outcome = await _advance(
            factory,
            record=initial,
            phase=initial.phase,
            status=ExecutionStatus.AWAITING_EXTERNAL,
            next_check_at=_next(now, config, observation.retry_at),
            progress_note="external outcome remains uncertain; observation required before retry",
        )
        _account(report, initial.org_id, outcome)
        report.effects_uncertain += 1
        return

    effect = decision.effect
    assert effect is not None
    # Authority is time-limited, so the expiry fence is only meaningful when it is
    # asked at the moment permission is being claimed. `now` was captured before
    # `observe`, which is bounded only by `io_timeout_seconds` — comparing against it
    # answers "was I authorized when this pass started?" instead of "am I authorized
    # to act?". Only the authorization boundaries read the clock afresh: `now` also
    # drives wake-up scheduling, `progressed_at` and block persistence, and moving
    # those would change scheduling and durability semantics rather than fix a fence.
    authority_block = await authority_verifier(factory, initial, effect, clock.now())
    if authority_block is not None:
        await _notify_block(
            factory,
            record=initial,
            block=authority_block,
            now=now,
            config=config,
            notifier=notifier,
            report=report,
        )
        report.blocked += 1
        report.bump(initial.org_id, "blocked")
        return

    if initial.attempts >= config.max_attempts:
        await _notify_block(
            factory,
            record=initial,
            block=BlockRecord(
                code=BlockCode.ATTEMPTS_EXHAUSTED,
                owner="platform-operator",
                required_input="authorized recovery decision",
                progressed_at=initial.progressed_at,
            ),
            now=now,
            config=config,
            notifier=notifier,
            report=report,
        )
        report.blocked += 1
        report.bump(initial.org_id, "blocked")
        return

    prepared = await _advance(
        factory,
        record=initial,
        phase=initial.phase,
        status=ExecutionStatus.AWAITING_EXTERNAL,
        next_check_at=_next(now, config, decision.next_check_at),
        intent=effect.intent,
        consume_attempt=True,
        progress_note=decision.progress_note,
    )
    if not _account(report, initial.org_id, prepared) or prepared.record is None:
        return
    report.reserved += 1
    await _call_checkpoint(checkpoint, "after_intent", RunnerContext(_identity(prepared.record), prepared.record, now))

    # The second check is intentionally after the intent commit and immediately
    # before the provider call. Withdrawal in that gap blocks the effect while
    # leaving a durable intent for reconciliation.
    #
    # The allowance is re-checked against the attempts spent BEFORE this reservation,
    # because the attempt just consumed is the one being licensed right now. Counting
    # it as already spent would retire the final granted attempt without ever making
    # the call: an accepted allowance of N would perform only N-1 effects, N=1 would
    # perform none at all, and the reserved intent would be stranded as a permanently
    # `prepared` action that no observation ever settles. Every other fence — policy,
    # expiry, permission and the work claim — is still re-read live from the database,
    # and the expiry is compared against a live clock reading rather than the loop-top
    # capture, so authority withdrawn by the passage of time is caught here too.
    authority_block = await authority_verifier(factory, replace(prepared.record, attempts=initial.attempts), effect, clock.now())
    if authority_block is not None:
        await _notify_block(
            factory,
            record=prepared.record,
            block=authority_block,
            now=now,
            config=config,
            notifier=notifier,
            report=report,
        )
        report.blocked += 1
        report.bump(initial.org_id, "blocked")
        return

    # The execution's own deadline is the last fence before an irreversible external
    # effect. It is a different clock from the accepted policy's `expires_at` above,
    # which `authority_verifier` fences and which never consults `deadline_at` — so
    # checking authority here does not cover a lapsed deadline. The pre-observation
    # check cannot cover this window either: `observe` and the intent commit both
    # spend real time after it, and a lapsed deadline withdraws permission to *carry
    # out* the work exactly as an expired policy does.
    #
    # The committed intent is deliberately left durable rather than discarded: it is
    # the only record that sends a later pass to ask the provider what happened.
    if _deadline_lapsed(prepared.record, clock.now()):
        await _notify_block(
            factory,
            record=prepared.record,
            block=_deadline_block(prepared.record),
            now=now,
            config=config,
            notifier=notifier,
            report=report,
        )
        report.blocked += 1
        report.bump(initial.org_id, "blocked")
        return

    report.effects_attempted += 1
    report.bump(initial.org_id, "effects_attempted")
    timeout = min(effect.timeout_seconds or config.io_timeout_seconds, config.io_timeout_seconds)
    effect_context = RunnerContext(_identity(prepared.record), prepared.record, now)
    try:
        effect_result = await asyncio.wait_for(handler.perform(effect_context, effect), timeout=timeout)
    except TimeoutError:
        effect_result = EffectResult(EffectOutcome.UNCERTAIN, retry_at=_next(now, config), detail="effect timed out")
    await _call_checkpoint(checkpoint, "after_effect", RunnerContext(_identity(prepared.record), prepared.record, now))
    await _call_checkpoint(checkpoint, "before_receipt", RunnerContext(_identity(prepared.record), prepared.record, now))

    if effect_result.outcome is EffectOutcome.SUCCEEDED:
        status = decision.status or ExecutionStatus.RUNNABLE
        due = None if status in TERMINAL_EXECUTION_STATUSES else now
        phase = decision.phase or initial.phase
    elif effect_result.outcome is EffectOutcome.FAILED:
        status = ExecutionStatus.RUNNABLE
        due = _next(now, config, effect_result.retry_at)
        phase = initial.phase
    else:
        status = ExecutionStatus.AWAITING_EXTERNAL
        due = _next(now, config, effect_result.retry_at)
        phase = initial.phase

    advanced = await _record_and_advance(
        factory,
        record=prepared.record,
        operation_key=effect.intent.operation_key,
        effect_result=effect_result,
        phase=phase,
        status=status,
        next_check_at=due,
        progress_note=effect_result.detail or decision.progress_note,
        now=now,
    )
    if not _account(report, initial.org_id, advanced):
        return
    if effect_result.outcome is EffectOutcome.SUCCEEDED:
        report.effects_succeeded += 1
    elif effect_result.outcome is EffectOutcome.FAILED:
        report.effects_failed += 1
    else:
        report.effects_uncertain += 1
    report.advanced += 1


async def run_execution_runner(
    factory: async_sessionmaker[AsyncSession],
    *,
    handlers: Mapping[ExecutionPhase, ExecutionHandler] | None = None,
    config: RunnerConfig | None = None,
    clock: Clock | None = None,
    authority_verifier: AuthorityVerifier = verify_live_authority,
    notifier: Notifier = notify,
    checkpoint: Checkpoint | None = None,
) -> RunnerReport:
    """Run one bounded recovery pass. Every unfinished action remains due or blocked."""
    config = config or RunnerConfig.from_env()
    report = RunnerReport(enabled=config.enabled)
    if not config.enabled:
        return report

    handlers = registered_execution_handlers() if handlers is None else handlers
    clock = clock or SystemClock()
    if not handlers:
        return report
    started = clock.monotonic()
    candidates = await _load_due(
        factory,
        now=clock.now(),
        limit=config.max_actions + 1,
        phases=frozenset(handlers),
    )
    if len(candidates) > config.max_actions:
        report.capped = True
        candidates = candidates[: config.max_actions]

    for candidate in candidates:
        if clock.monotonic() - started >= config.time_budget_seconds:
            report.capped = True
            break
        handler = handlers.get(candidate.phase)
        if handler is None:
            # Real phase handlers land in named downstream stories. Absence does
            # not fabricate progress or erase the existing due time.
            continue
        report.examined += 1
        report.bump(candidate.org_id, "examined")
        try:
            await _process_one(
                factory,
                initial=candidate,
                handler=handler,
                config=config,
                clock=clock,
                authority_verifier=authority_verifier,
                notifier=notifier,
                report=report,
                checkpoint=checkpoint,
            )
        except Exception:
            report.errors += 1
            report.bump(candidate.org_id, "errors")
            logger.exception("execution runner: failed execution %s; its durable due time remains", candidate.id)
    return report


__all__ = [
    "DecisionKind",
    "Clock",
    "EffectOutcome",
    "EffectRequest",
    "EffectResult",
    "ExecutionHandler",
    "HandlerDecision",
    "HandlerObservation",
    "ObservationKind",
    "OperationIdentity",
    "RunnerConfig",
    "RunnerContext",
    "RunnerReport",
    "SystemClock",
    "TransactionMutation",
    "register_execution_handler",
    "registered_execution_handlers",
    "run_execution_runner",
    "verify_live_authority",
]
