"""Real-PostgreSQL races for the scheduled execution runner (#5143)."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.budget.enforcement_settings import BudgetAccountingGap, BudgetEnforcementSetting
from src.orchestration.execution_policy import Action
from src.orchestration.execution_runner import (
    DecisionKind,
    EffectOutcome,
    EffectRequest,
    EffectResult,
    HandlerDecision,
    HandlerObservation,
    ObservationKind,
    RunnerConfig,
    RunnerContext,
    run_execution_runner,
)
from src.orchestration.execution_state import ActionIntent, ActionStatus, ExecutionIdentity, ExecutionPhase
from src.orchestration.execution_store import create_execution
from src.orchestration.models import (
    ClaimState,
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401

pytestmark = pytest.mark.integration

ORG = "org-runner-pg"
CLAIM = "claim-runner-pg"
NOW = datetime(2026, 9, 18, 13, 0, tzinfo=UTC)


class Clock:
    def now(self) -> datetime:
        return NOW

    def monotonic(self) -> float:
        return 0


class RacingHandler:
    def __init__(self, parties: int = 2) -> None:
        self.parties = parties
        self.observers = 0
        self.ready = asyncio.Event()
        self.perform_count = 0

    async def observe(self, context: RunnerContext) -> HandlerObservation:
        self.observers += 1
        if self.observers >= self.parties:
            self.ready.set()
        # Widen the race window when a second tick is expected, but never let the
        # barrier itself decide the outcome. `skip_locked` may legitimately leave the
        # other tick with nothing to observe, in which case this rendezvous can never
        # complete; waiting for the runner's own I/O timeout instead would turn that
        # into a spurious `UNCERTAIN` observation and test the timeout path rather than
        # the CAS fence under test.
        with suppress(TimeoutError):
            await asyncio.wait_for(self.ready.wait(), timeout=1)
        return HandlerObservation(ObservationKind.READY)

    def decide(self, context: RunnerContext, observation: HandlerObservation) -> HandlerDecision:
        return HandlerDecision(
            DecisionKind.EFFECT,
            phase=ExecutionPhase.SUBMITTING,
            effect=EffectRequest(ActionIntent("provider:concurrent-effect", "synthetic_provider_effect"), Action.DEVELOP),
        )

    async def perform(self, context: RunnerContext, effect: EffectRequest) -> EffectResult:
        self.perform_count += 1
        return EffectResult(EffectOutcome.SUCCEEDED, receipt_ref="provider/one-receipt")


@pytest.fixture
async def pg_engine(pg_url):  # noqa: F811
    engine = create_async_engine(to_async_url(pg_url), echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(BudgetEnforcementSetting.__table__.create)
        await connection.run_sync(BudgetAccountingGap.__table__.create)
        await connection.run_sync(OrchestrationFlow.__table__.create)
        await connection.run_sync(OrchestrationAcceptedPlan.__table__.create)
        await connection.run_sync(OrchestrationNode.__table__.create)
        await connection.run_sync(OrchestrationWorkClaim.__table__.create)
        await connection.run_sync(OrchestrationExecution.__table__.create)
        await connection.run_sync(OrchestrationAction.__table__.create)
    yield engine
    await engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine):
    return async_sessionmaker(pg_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def execution(pg_session_factory):
    async with pg_session_factory() as session:
        flow = OrchestrationFlow(execution_paused=False, org_id=ORG, slug="runner-pg", title="Runner PostgreSQL")
        session.add(flow)
        await session.flush()
        node = OrchestrationNode(
            org_id=ORG,
            flow_id=flow.id,
            epic_ref="E1",
            wave_ref="W1",
            node_ref="N1",
            kind="story",
            state="running",
            title="PostgreSQL runner node",
        )
        session.add(node)
        session.add(
            OrchestrationAcceptedPlan(
                org_id=ORG,
                flow_id=flow.id,
                version=1,
                plan_document={},
                plan_hash="b" * 64,
            )
        )
        session.add(
            OrchestrationWorkClaim(
                id=CLAIM,
                org_id=ORG,
                provider_repository_id=5143,
                issue_number=5143,
                owner_kind="engine_flow",
                owner_ref=flow.id,
                state=ClaimState.HELD.value,
                generation=1,
                active_run_id="runner-pg",
            )
        )
        await session.flush()
        outcome = await create_execution(
            session,
            identity=ExecutionIdentity(ORG, node.id, 1, 1, CLAIM, 1),
            flow_id=flow.id,
            next_check_at=NOW,
        )
        await session.commit()
        return outcome.record


async def _allow(_factory, _record, _effect, _now):
    return None


async def test_concurrent_ticks_admit_exactly_one_external_effect(pg_session_factory, execution):
    """Two real transactions may observe, but CAS licenses only one effect."""
    handler = RacingHandler()
    config = RunnerConfig(
        enabled=True,
        max_actions=1,
        io_timeout_seconds=5,
        time_budget_seconds=10,
        retry_seconds=30,
        max_attempts=3,
    )
    reports = await asyncio.gather(
        run_execution_runner(
            pg_session_factory,
            handlers={ExecutionPhase.ADMITTED: handler},
            config=config,
            clock=Clock(),
            authority_verifier=_allow,
        ),
        run_execution_runner(
            pg_session_factory,
            handlers={ExecutionPhase.ADMITTED: handler},
            config=config,
            clock=Clock(),
            authority_verifier=_allow,
        ),
    )

    assert handler.perform_count == 1
    assert sum(report.effects_succeeded for report in reports) == 1
    # Exactly one tick may license the effect. The loser is allowed to reach the row
    # and be refused by the revision fence (`stale`), or to never see it at all
    # because `skip_locked` handed it to the winner — both are correct admission
    # control, and which one happens depends on lock timing. Asserting one specific
    # interleaving makes this test fail ~25% of runs while the actual invariant
    # (one effect, one action, one receipt) holds, so assert the invariant: no more
    # than one reservation, and nothing beyond a refusal for whoever lost.
    assert sum(report.reserved for report in reports) == 1
    assert sum(report.stale for report in reports) <= 1
    assert sum(report.effects_failed + report.effects_uncertain + report.errors for report in reports) == 0
    async with pg_session_factory() as session:
        assert (await session.scalar(select(func.count()).select_from(OrchestrationAction))) == 1
        action = (await session.execute(select(OrchestrationAction))).scalar_one()
        assert action.status == ActionStatus.SUCCEEDED.value
        assert action.receipt_ref == "provider/one-receipt"


@pytest.mark.parametrize(
    ("crash_point", "expected_performs", "expected_actions"),
    [
        ("before_observe", 0, 0),
        ("after_intent", 0, 1),
        ("after_effect", 1, 1),
        ("before_receipt", 1, 1),
    ],
)
async def test_process_death_points_leave_truthful_durable_state(
    pg_session_factory,
    execution,
    crash_point,
    expected_performs,
    expected_actions,
):
    """The crash window is asserted on PostgreSQL, where row locks are real."""
    handler = RacingHandler(parties=1)

    async def crash(name, _context):
        if name == crash_point:
            raise RuntimeError(f"simulated process death at {name}")

    report = await run_execution_runner(
        pg_session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=RunnerConfig(
            enabled=True,
            max_actions=1,
            io_timeout_seconds=5,
            time_budget_seconds=10,
            retry_seconds=30,
            max_attempts=3,
        ),
        clock=Clock(),
        authority_verifier=_allow,
        checkpoint=crash,
    )
    assert report.errors == 1
    assert handler.perform_count == expected_performs
    async with pg_session_factory() as session:
        actions = (await session.execute(select(OrchestrationAction))).scalars().all()
        assert len(actions) == expected_actions
        if actions:
            # Until the receipt transaction commits, no success is fabricated.
            assert actions[0].status == ActionStatus.PREPARED.value
        row = (await session.execute(select(OrchestrationExecution))).scalar_one()
        assert row.status in {"runnable", "awaiting_external"}
        assert row.next_check_at is not None


async def test_due_query_uses_positive_statuses_and_oldest_due_order(pg_session_factory, execution):
    """The query shape keeps the status/time due index usable as history grows."""
    async with pg_session_factory() as session:
        # Model the steady state: concluded history dominates a small due set.
        rows = []
        for index in range(20_000):
            runnable = index % 50 == 0
            rows.append(
                {
                    "id": f"bulk-{index:05d}",
                    "org_id": ORG,
                    "flow_id": execution.flow_id,
                    "node_id": execution.node_id,
                    "cycle": index + 2,
                    "phase": "admitted" if runnable else "concluded",
                    "status": "runnable" if runnable else "concluded",
                    "revision": 1,
                    "accepted_plan_version": 1,
                    "claim_id": CLAIM,
                    "claim_generation": 1,
                    "attempts": 0,
                    "next_check_at": NOW - timedelta(seconds=index) if runnable else None,
                }
            )
        await session.execute(insert(OrchestrationExecution), rows)
        await session.commit()
        await session.execute(text("ANALYZE orchestration_executions"))
        explained = await session.execute(
            text(
                "EXPLAIN (COSTS OFF) "
                "SELECT id FROM orchestration_executions "
                "WHERE org_id = :org_id "
                "AND status IN ('runnable', 'awaiting_external', 'blocked') "
                "AND next_check_at <= :due "
                "ORDER BY next_check_at, id LIMIT 10"
            ),
            {"org_id": ORG, "due": NOW + timedelta(seconds=1)},
        )
        plan = "\n".join(str(row[0]) for row in explained)
        assert "ix_orchestration_executions_due" in plan, plan
        assert "next_check_at" in plan, plan


async def test_pause_wins_race_after_observation_without_spending_attempt(pg_session_factory, execution):
    """A pause committed between the runner read and reservation wins in SQL."""
    from sqlalchemy import update

    async def pause_before_reservation(factory, record, effect, now):
        async with factory() as session:
            await session.execute(update(OrchestrationFlow).where(OrchestrationFlow.id == record.flow_id).values(execution_paused=True))
            await session.commit()
        return None

    handler = RacingHandler(parties=1)
    report = await run_execution_runner(
        pg_session_factory,
        handlers={ExecutionPhase.ADMITTED: handler},
        config=RunnerConfig(enabled=True, io_timeout_seconds=5),
        clock=Clock(),
        authority_verifier=pause_before_reservation,
    )
    assert report.reserved == 0 and handler.perform_count == 0 and report.errors == 0
    async with pg_session_factory() as session:
        row = await session.get(OrchestrationExecution, execution.id)
        assert row.attempts == 0 and row.pending_action_key is None
        assert await session.scalar(select(func.count()).select_from(OrchestrationAction)) == 0
