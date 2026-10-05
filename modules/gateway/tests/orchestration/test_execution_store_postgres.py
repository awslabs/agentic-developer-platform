"""Real-PostgreSQL concurrency tests for the execution/action ledger (#5142).

The story requires PostgreSQL concurrency tests rather than SQLite-only locking
assertions, and here that is load-bearing rather than pedantic. Everything that
makes this store safe under genuine concurrency is PostgreSQL behavior SQLite
either lacks or fakes:

- `SELECT ... FOR UPDATE` is a **no-op** on SQLite. The row lock that makes the
  second writer wait and then observe the winner's committed state does not exist
  there, so `test_execution_store.py` proves the *semantics* and deliberately
  proves nothing about locking.
- The unique indexes on `(org_id, node_id, cycle)` and
  `(org_id, execution_id, operation_key)` are the correctness backstop, and
  `IntegrityError` from a concurrent insert is the path `create_execution` and
  `prepare_action` convert into convergence. That path is unreachable without two
  genuinely concurrent writers.
- SQLite's single-writer model hides the exact interleaving that produces the
  duplicate: two transactions that both read "no execution" before either inserts.

Why it matters for this issue rather than in the abstract: duplication here is the
*expected* case, not an exotic one. A worker dies mid-delivery, SQS redelivers, a
tick restarts, and two processes reach for the same node's cycle at once. If that
produced two executions, each would carry its own action ledger and neither would
see the other's completed steps — so the recovery this issue exists to enable would
instead open a second pull request. Convergence under concurrency is what keeps the
fix from causing the double-effect it was built to prevent.

`asyncio.gather` over separate sessions, not threads: the sessions are genuinely
concurrent at the database while the failure mode stays reproducible instead of
depending on OS thread scheduling. Same reasoning `test_work_claims_postgres.py`
and `test_pr_bindings_postgres.py` give.

Skips (never silently passes) when no PostgreSQL server is available — `pgserver`
publishes wheels for Python <= 3.12 only, which CI's Test job uses. A skip here
means **"not tested"**, and it is reported as such rather than as a pass.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.orchestration.execution_state import (
    ActionIntent,
    ActionStatus,
    BlockCode,
    BlockRecord,
    ExecutionIdentity,
    ExecutionOutcome,
    ExecutionPhase,
    ExecutionStatus,
    ExecutionStoreError,
    Observation,
    ObservedOutcome,
    OutcomeKind,
    PhaseAdvance,
)
from src.orchestration.execution_store import (
    advance_execution,
    create_execution,
    load_execution,
    prepare_action,
    record_observation,
)
from src.orchestration.models import (
    ClaimState,
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from src.orchestration.work_claims import OwnerKind

# Re-exported through tests/migrations/conftest.py, but this file lives in
# tests/orchestration/, so the fixtures are imported explicitly. `pg_server` is
# session-scoped, so a run that also touches the migration tests shares one server.
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401

pytestmark = pytest.mark.integration

ORG_A = "org-alpha"
ORG_B = "org-beta"
CLAIM = "claim-5142"
CLAIM_B = "claim-5142-b"
PLAN_VERSION = 3


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def pg_engine(pg_url):  # noqa: F811 - pg_url is a fixture, not a shadowed import
    """An async engine on a fresh PostgreSQL database with only the needed tables.

    Built straight from the ORM models rather than by running the Alembic chain:
    this file tests runtime concurrency, so creating only the tables under test (and
    their FK parents) keeps it independent of unrelated migrations. Migration
    correctness — including that the DDL matches these models — is
    `tests/migrations/test_052_orchestration_executions.py` instead.
    """
    engine = create_async_engine(to_async_url(pg_url), echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(OrchestrationFlow.__table__.create)
        await conn.run_sync(OrchestrationAcceptedPlan.__table__.create)
        await conn.run_sync(OrchestrationNode.__table__.create)
        await conn.run_sync(OrchestrationWorkClaim.__table__.create)
        await conn.run_sync(OrchestrationExecution.__table__.create)
        await conn.run_sync(OrchestrationAction.__table__.create)
    yield engine
    await engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine):
    return async_sessionmaker(pg_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def graph(pg_session_factory):
    """A committed flow and node per tenant, so every concurrent session sees them."""
    made: dict[str, str] = {}
    async with pg_session_factory() as session:
        for org, key in ((ORG_A, "a"), (ORG_B, "b")):
            flow = OrchestrationFlow(execution_paused=False, org_id=org, slug=f"flow-{key}", title=f"Flow {key}", state="draft")
            session.add(flow)
            await session.flush()
            made[f"flow_{key}"] = flow.id
            session.add(
                OrchestrationAcceptedPlan(
                    org_id=org,
                    flow_id=flow.id,
                    version=PLAN_VERSION,
                    plan_document={},
                    plan_hash=f"plan-{key}",
                )
            )
            claim_id = CLAIM if org == ORG_A else CLAIM_B
            session.add(
                OrchestrationWorkClaim(
                    id=claim_id,
                    org_id=org,
                    provider_repository_id=5142,
                    issue_number=5142,
                    owner_kind=OwnerKind.ENGINE_FLOW.value,
                    owner_ref=flow.id,
                    state=ClaimState.HELD.value,
                    generation=1,
                )
            )
            made[f"claim_{key}"] = claim_id
            node = OrchestrationNode(
                org_id=org,
                flow_id=flow.id,
                epic_ref="E1",
                wave_ref="W1",
                node_ref="N1",
                kind="story",
                title=f"Node {key}",
            )
            session.add(node)
            await session.flush()
            made[f"node_{key}"] = node.id
        await session.commit()
    return made


def _identity(graph, *, node: str = "node_a", org: str = ORG_A, cycle: int = 1, generation: int = 1, plan: int = PLAN_VERSION) -> ExecutionIdentity:
    return ExecutionIdentity(
        org_id=org,
        node_id=graph[node],
        cycle=cycle,
        accepted_plan_version=plan,
        claim_id=CLAIM_B if org == ORG_B else CLAIM,
        claim_generation=generation,
    )


def _soon():
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) + timedelta(minutes=5)


async def _create_attempt(session_factory, identity, flow_id):
    """One full create attempt in its own committed transaction.

    Committing inside the attempt is what makes the race real: an uncommitted winner
    would be invisible to the loser however the locking behaves. Returns the outcome,
    or the `ExecutionStoreError` if the attempt lost the insert race and the winner
    had not yet committed — both are legitimate results for a loser, and the tests
    assert on the *combination* rather than on which arm fired.
    """
    async with session_factory() as session:
        try:
            outcome = await create_execution(session, identity=identity, flow_id=flow_id)
            await session.commit()
            return outcome
        except ExecutionStoreError as exc:
            await session.rollback()
            return exc


async def _prepare_attempt(session_factory, identity, intent):
    async with session_factory() as session:
        try:
            outcome = await prepare_action(session, identity=identity, intent=intent)
            await session.commit()
            return outcome
        except ExecutionStoreError as exc:
            await session.rollback()
            return exc


async def _observe_attempt(session_factory, identity, observation):
    async with session_factory() as session:
        outcome = await record_observation(session, identity=identity, observation=observation)
        await session.commit()
        return outcome


async def _seed(pg_session_factory, graph, **kwargs):
    identity = _identity(graph, **kwargs)
    flow_key = "flow_b" if identity.org_id == ORG_B else "flow_a"
    async with pg_session_factory() as session:
        claim = await session.get(OrchestrationWorkClaim, identity.claim_id)
        claim.generation = identity.claim_generation
        claim.state = ClaimState.HELD.value
        await session.flush()
        outcome = await create_execution(session, identity=identity, flow_id=graph[flow_key])
        await session.commit()
    return identity, outcome.record


# ---------------------------------------------------------------------------
# One durable identity
# ---------------------------------------------------------------------------


class TestConcurrentCreate:
    """Concurrent creation produces exactly one durable identity."""

    async def test_two_simultaneous_creates_produce_one_execution(self, pg_session_factory, graph):
        """The headline guarantee.

        Both callers wanting the same identity is agreement, not conflict — so both
        legitimately succeed, but against *one* row. If this ever produced two rows,
        the ledger would be advisory and the recovery path would double-act.
        """
        identity = _identity(graph)
        results = await asyncio.gather(
            _create_attempt(pg_session_factory, identity, graph["flow_a"]),
            _create_attempt(pg_session_factory, identity, graph["flow_a"]),
        )

        async with pg_session_factory() as session:
            rows = (await session.execute(select(OrchestrationExecution))).scalars().all()
        assert len(rows) == 1, f"expected exactly one execution, got {len(rows)}: {results}"

        applied = [r for r in results if getattr(r, "kind", None) is OutcomeKind.APPLIED]
        lost = [r for r in results if isinstance(r, ExecutionStoreError)]
        # Every attempt either adopted the single identity or reported the race as a
        # typed refusal. Nothing may report success against a second row, and nothing
        # may fail for an untyped reason.
        assert len(applied) + len(lost) == 2, f"unexpected outcomes: {results}"
        assert applied, "at least the winner must succeed"
        for outcome in applied:
            assert outcome.record.id == rows[0].id
        for error in lost:
            assert error.code == "create_race_lost"

    @pytest.mark.parametrize("fan_out", [5, 12])
    async def test_many_simultaneous_creates_produce_one_execution(self, pg_session_factory, graph, fan_out):
        """Scaled up, because a two-way race can pass by luck.

        A retry storm or a redelivered queue message produces many concurrent starts
        on one node; the invariant is the same at any width.
        """
        identity = _identity(graph)
        results = await asyncio.gather(*[_create_attempt(pg_session_factory, identity, graph["flow_a"]) for _ in range(fan_out)])

        async with pg_session_factory() as session:
            count = (await session.execute(text("SELECT count(*) FROM orchestration_executions"))).scalar_one()
        assert count == 1, f"expected one execution out of {fan_out} attempts, got {count}: {results}"

        ids = {r.record.id for r in results if getattr(r, "kind", None) is OutcomeKind.APPLIED}
        assert len(ids) <= 1, "every successful caller must hold the same identity"

    async def test_concurrent_creates_on_different_cycles_are_independent(self, pg_session_factory, graph):
        """Cycle is in the key, so a re-run of the same node is its own identity."""
        results = await asyncio.gather(
            _create_attempt(pg_session_factory, _identity(graph, cycle=1), graph["flow_a"]),
            _create_attempt(pg_session_factory, _identity(graph, cycle=2), graph["flow_a"]),
        )
        assert all(getattr(r, "kind", None) is OutcomeKind.APPLIED for r in results), results
        async with pg_session_factory() as session:
            count = (await session.execute(text("SELECT count(*) FROM orchestration_executions"))).scalar_one()
        assert count == 2


class TestConcurrentActionPreparation:
    """Concurrent preparation of the same operation key produces one action."""

    async def test_two_simultaneous_preparations_produce_one_action(self, pg_session_factory, graph):
        """The double-effect guard under real concurrency.

        Two rows here would mean two pull requests for one step — precisely the
        failure the operation key exists to prevent.
        """
        identity, _ = await _seed(pg_session_factory, graph)
        intent = ActionIntent(operation_key="pr:node-a:cycle-1", kind="open_pull_request")

        results = await asyncio.gather(
            _prepare_attempt(pg_session_factory, identity, intent),
            _prepare_attempt(pg_session_factory, identity, intent),
        )

        async with pg_session_factory() as session:
            rows = (await session.execute(select(OrchestrationAction))).scalars().all()
        assert len(rows) == 1, f"expected exactly one action, got {len(rows)}: {results}"

        applied = [r for r in results if getattr(r, "kind", None) is OutcomeKind.APPLIED]
        lost = [r for r in results if isinstance(r, ExecutionStoreError)]
        assert len(applied) + len(lost) == 2, f"unexpected outcomes: {results}"
        for outcome in applied:
            assert outcome.action.id == rows[0].id
        for error in lost:
            assert error.code == "action_race_lost"

    @pytest.mark.parametrize("fan_out", [5, 12])
    async def test_many_simultaneous_preparations_produce_one_action(self, pg_session_factory, graph, fan_out):
        identity, _ = await _seed(pg_session_factory, graph)
        intent = ActionIntent(operation_key="pr:storm", kind="open_pull_request")
        results = await asyncio.gather(*[_prepare_attempt(pg_session_factory, identity, intent) for _ in range(fan_out)])

        async with pg_session_factory() as session:
            count = (await session.execute(text("SELECT count(*) FROM orchestration_actions"))).scalar_one()
        assert count == 1, f"expected one action out of {fan_out} attempts, got {count}: {results}"

    async def test_a_lost_action_race_leaves_the_callers_transaction_usable(self, pg_session_factory, graph, monkeypatch):
        """The savepoint's purpose, against a real unique violation.

        `prepare_action` isolates the `IntegrityError` in a SAVEPOINT so the caller's
        transaction survives. Without that, one lost race would poison an entire
        pass — discarding work that had nothing to do with the contended key.
        """
        from src.orchestration import execution_store

        identity, _ = await _seed(pg_session_factory, graph)
        contended = ActionIntent(operation_key="pr:contended", kind="open_pull_request")

        # A committed winner for the contended key.
        async with pg_session_factory() as session:
            await prepare_action(session, identity=identity, intent=contended)
            await session.commit()

        async with pg_session_factory() as session:
            unrelated = await prepare_action(session, identity=identity, intent=ActionIntent(operation_key="pr:unrelated", kind="open_pull_request"))
            original = execution_store._action_for_key

            async def blind_to_the_winner(session, row, operation_key, *, for_update=False):
                # Simulates the interleaving where the loser read before the winner
                # committed, which is the only way to reach the IntegrityError path.
                if operation_key == "pr:contended":
                    return None
                return await original(session, row, operation_key, for_update=for_update)

            monkeypatch.setattr(execution_store, "_action_for_key", blind_to_the_winner)
            with pytest.raises(ExecutionStoreError, match="concurrently prepared"):
                await prepare_action(session, identity=identity, intent=contended)

            # The caller's own pending work is intact and the session still usable.
            assert await session.get(OrchestrationAction, unrelated.action.id) is not None
            await session.commit()

        async with pg_session_factory() as session:
            assert await session.get(OrchestrationAction, unrelated.action.id) is not None
            count = (await session.execute(text("SELECT count(*) FROM orchestration_actions"))).scalar_one()
            assert count == 2

    async def test_a_lost_create_race_leaves_the_callers_transaction_usable(self, pg_session_factory, graph, monkeypatch):
        """Same guarantee on the create path, against a real unique violation."""
        from src.orchestration import execution_store

        contended = _identity(graph, node="node_a", cycle=1)
        async with pg_session_factory() as session:
            await create_execution(session, identity=contended, flow_id=graph["flow_a"])
            await session.commit()

        async with pg_session_factory() as session:
            other = await create_execution(session, identity=_identity(graph, cycle=9), flow_id=graph["flow_a"])
            original = execution_store._locked_execution

            async def blind_to_the_winner(session, identity):
                if identity.cycle == 1:
                    return None
                return await original(session, identity)

            monkeypatch.setattr(execution_store, "_locked_execution", blind_to_the_winner)
            with pytest.raises(ExecutionStoreError, match="concurrently created"):
                await create_execution(session, identity=contended, flow_id=graph["flow_a"])

            assert await session.get(OrchestrationExecution, other.record.id) is not None
            await session.commit()

        async with pg_session_factory() as session:
            assert await session.get(OrchestrationExecution, other.record.id) is not None


# ---------------------------------------------------------------------------
# Compare-and-set under concurrency
# ---------------------------------------------------------------------------


class TestConcurrentAdvance:
    """A stale revision cannot overwrite current progress."""

    async def test_two_simultaneous_advances_apply_exactly_one(self, pg_session_factory, graph):
        """Both read revision 1; only one may write.

        This is the lost-update failure in its purest form. Without the CAS fence the
        second write would land silently, erasing the first's progress from the very
        record whose purpose is to survive process loss.
        """
        identity, record = await _seed(pg_session_factory, graph)

        async def attempt(note, phase):
            async with pg_session_factory() as session:
                outcome = await advance_execution(
                    session,
                    identity=identity,
                    advance=PhaseAdvance(
                        phase=phase,
                        status=ExecutionStatus.RUNNABLE,
                        expected_revision=record.revision,
                        next_check_at=_soon(),
                        progress_note=note,
                    ),
                )
                await session.commit()
                return outcome

        results = await asyncio.gather(
            attempt("first", ExecutionPhase.DELIVERING),
            attempt("second", ExecutionPhase.SUBMITTING),
        )

        applied = [r for r in results if r.kind is OutcomeKind.APPLIED]
        stale = [r for r in results if r.kind is OutcomeKind.STALE]
        assert len(applied) == 1, f"expected exactly one applied advance, got {results}"
        assert len(stale) == 1, f"expected exactly one stale refusal, got {results}"
        assert stale[0].reason == "stale_revision"

        async with pg_session_factory() as session:
            row = await session.get(OrchestrationExecution, record.id)
        # Advanced by exactly one, and holding the winner's note. A revision of 3
        # would mean both writes landed.
        assert row.revision == record.revision + 1
        assert row.progress_note == applied[0].record.progress_note

    @pytest.mark.parametrize("fan_out", [5, 12])
    async def test_many_simultaneous_advances_apply_exactly_one(self, pg_session_factory, graph, fan_out):
        identity, record = await _seed(pg_session_factory, graph)

        async def attempt(index):
            async with pg_session_factory() as session:
                outcome = await advance_execution(
                    session,
                    identity=identity,
                    advance=PhaseAdvance(
                        phase=ExecutionPhase.DELIVERING,
                        status=ExecutionStatus.RUNNABLE,
                        expected_revision=record.revision,
                        next_check_at=_soon(),
                        progress_note=f"lane-{index}",
                    ),
                )
                await session.commit()
                return outcome

        results = await asyncio.gather(*[attempt(index) for index in range(fan_out)])
        assert len([r for r in results if r.kind is OutcomeKind.APPLIED]) == 1
        assert len([r for r in results if r.kind is OutcomeKind.STALE]) == fan_out - 1

        async with pg_session_factory() as session:
            row = await session.get(OrchestrationExecution, record.id)
        assert row.revision == record.revision + 1

    async def test_a_stale_advance_cannot_overwrite_a_block(self, pg_session_factory, graph):
        """The consequence that matters operationally.

        A worker that blocked on a human gate must not have that block erased by a
        slower sibling still holding the pre-block revision — the work would resume
        without the approval the gate exists to require.
        """
        identity, record = await _seed(pg_session_factory, graph)

        async with pg_session_factory() as session:
            blocked = await advance_execution(
                session,
                identity=identity,
                advance=PhaseAdvance(
                    phase=ExecutionPhase.SETTLING,
                    status=ExecutionStatus.RUNNABLE,
                    expected_revision=record.revision,
                    next_check_at=_soon(),
                ),
                block=BlockRecord(
                    code=BlockCode.HUMAN_GATE_REQUIRED,
                    owner="platform-operator",
                    required_input="approve the wave gate",
                    remaining_gates=("gate:security-review",),
                ),
            )
            await session.commit()
        assert blocked.kind is OutcomeKind.BLOCKED

        async with pg_session_factory() as session:
            late = await advance_execution(
                session,
                identity=identity,
                advance=PhaseAdvance(
                    phase=ExecutionPhase.CONCLUDED,
                    status=ExecutionStatus.CONCLUDED,
                    expected_revision=record.revision,
                ),
            )
            await session.commit()

        assert late.kind is OutcomeKind.STALE
        async with pg_session_factory() as session:
            row = await session.get(OrchestrationExecution, record.id)
        assert row.status == ExecutionStatus.BLOCKED.value
        assert row.block_code == BlockCode.HUMAN_GATE_REQUIRED.value

    async def test_the_row_lock_serializes_overlapping_writers(self, pg_session_factory, graph):
        """`FOR UPDATE` makes the second writer wait, not interleave.

        Asserted by holding the lock in one open transaction and timing out a second
        attempt: if the lock were absent (as it is on SQLite), the second would
        return immediately. This is the assertion that cannot be made anywhere but
        against a real server.
        """
        identity, record = await _seed(pg_session_factory, graph)

        holder = pg_session_factory()
        await holder.__aenter__()
        try:
            await advance_execution(
                holder,
                identity=identity,
                advance=PhaseAdvance(
                    phase=ExecutionPhase.DELIVERING,
                    status=ExecutionStatus.RUNNABLE,
                    expected_revision=record.revision,
                    next_check_at=_soon(),
                ),
            )
            # Deliberately NOT committed: the lock is held for the block below.

            async def blocked_writer():
                async with pg_session_factory() as session:
                    return await advance_execution(
                        session,
                        identity=identity,
                        advance=PhaseAdvance(
                            phase=ExecutionPhase.SUBMITTING,
                            status=ExecutionStatus.RUNNABLE,
                            expected_revision=record.revision,
                            next_check_at=_soon(),
                        ),
                    )

            with pytest.raises(TimeoutError):
                await asyncio.wait_for(blocked_writer(), timeout=2.0)
        finally:
            await holder.rollback()
            await holder.__aexit__(None, None, None)

        # And once the lock is released the row is back to its pre-advance state,
        # because the holder rolled back.
        async with pg_session_factory() as session:
            row = await session.get(OrchestrationExecution, record.id)
        assert row.revision == record.revision


class TestAtomicityUnderRealTransactions:
    """Intent and continuation commit together, or roll back together."""

    async def test_intent_and_next_check_survive_together(self, pg_session_factory, graph):
        identity, record = await _seed(pg_session_factory, graph)
        async with pg_session_factory() as session:
            await advance_execution(
                session,
                identity=identity,
                advance=PhaseAdvance(
                    phase=ExecutionPhase.SUBMITTING,
                    status=ExecutionStatus.AWAITING_EXTERNAL,
                    expected_revision=record.revision,
                    next_check_at=_soon(),
                ),
                intent=ActionIntent(operation_key="pr:atomic", kind="open_pull_request"),
            )
            await session.commit()

        async with pg_session_factory() as session:
            row = await session.get(OrchestrationExecution, record.id)
            actions = (await session.execute(select(OrchestrationAction))).scalars().all()
        assert row.next_check_at is not None
        assert row.pending_action_key == "pr:atomic"
        assert len(actions) == 1

    async def test_a_failed_transaction_rolls_back_intent_and_next_check_together(self, pg_session_factory, graph):
        """Neither half may survive a failed transaction.

        A surviving intent with no scheduled follow-up is an effect nobody will look
        at; a surviving check time with no intent wakes a runner for work that was
        never recorded. Both are durable inconsistencies with no repair path, which
        is why they must commit as one unit.
        """
        identity, record = await _seed(pg_session_factory, graph)

        async with pg_session_factory() as session:
            outcome = await advance_execution(
                session,
                identity=identity,
                advance=PhaseAdvance(
                    phase=ExecutionPhase.SUBMITTING,
                    status=ExecutionStatus.AWAITING_EXTERNAL,
                    expected_revision=record.revision,
                    next_check_at=_soon(),
                ),
                intent=ActionIntent(operation_key="pr:rolled-back", kind="open_pull_request"),
            )
            assert outcome.kind is OutcomeKind.APPLIED
            # Whatever else the caller was doing fails.
            await session.rollback()

        async with pg_session_factory() as session:
            row = await session.get(OrchestrationExecution, record.id)
            actions = (await session.execute(select(OrchestrationAction))).scalars().all()
        assert row.revision == record.revision, "the advance did not survive"
        assert row.phase == ExecutionPhase.ADMITTED.value
        assert row.pending_action_key is None
        assert actions == [], "the intent did not survive either"

    async def test_an_observation_is_not_visible_until_it_commits(self, pg_session_factory, graph):
        """The store commits nothing itself, proven across real connections."""
        identity, _ = await _seed(pg_session_factory, graph)
        async with pg_session_factory() as session:
            await prepare_action(session, identity=identity, intent=ActionIntent(operation_key="pr:pending", kind="open_pull_request"))
            await session.commit()

        async with pg_session_factory() as writer:
            await record_observation(
                writer,
                identity=identity,
                observation=Observation(operation_key="pr:pending", outcome=ObservedOutcome.SUCCEEDED, receipt_ref="pr/42"),
            )
            # A separate connection must still see the unobserved action.
            async with pg_session_factory() as reader:
                row = (await reader.execute(select(OrchestrationAction).where(OrchestrationAction.operation_key == "pr:pending"))).scalar_one()
                assert row.status == ActionStatus.PREPARED.value
                assert row.receipt_ref is None
            await writer.commit()

        async with pg_session_factory() as reader:
            row = (await reader.execute(select(OrchestrationAction).where(OrchestrationAction.operation_key == "pr:pending"))).scalar_one()
        assert row.status == ActionStatus.SUCCEEDED.value


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


class TestCrossTenantKeysCannotBind:
    """A key from one tenant resolves to nothing in another."""

    async def test_the_same_node_ref_in_two_tenants_yields_two_executions(self, pg_session_factory, graph):
        await _seed(pg_session_factory, graph, node="node_a", org=ORG_A)
        await _seed(pg_session_factory, graph, node="node_b", org=ORG_B)
        async with pg_session_factory() as session:
            rows = (await session.execute(select(OrchestrationExecution))).scalars().all()
        assert len(rows) == 2
        assert {r.org_id for r in rows} == {ORG_A, ORG_B}

    async def test_a_foreign_tenant_cannot_read_or_advance_an_execution(self, pg_session_factory, graph):
        """Uniqueness is tenant-scoped and every query filters `org_id`, so the other
        tenant's id is not a handle on this row — it resolves to nothing at all."""
        identity, record = await _seed(pg_session_factory, graph, org=ORG_A)
        intruder = ExecutionIdentity(
            org_id=ORG_B,
            node_id=identity.node_id,
            cycle=identity.cycle,
            accepted_plan_version=PLAN_VERSION,
            claim_id=CLAIM,
            claim_generation=1,
        )

        async with pg_session_factory() as session:
            with pytest.raises(ExecutionStoreError, match="No execution exists"):
                await advance_execution(
                    session,
                    identity=intruder,
                    advance=PhaseAdvance(
                        phase=ExecutionPhase.CONCLUDED,
                        status=ExecutionStatus.CONCLUDED,
                        expected_revision=record.revision,
                    ),
                )
            with pytest.raises(ExecutionStoreError, match="No execution exists"):
                await prepare_action(session, identity=intruder, intent=ActionIntent(operation_key="pr:forged", kind="open_pull_request"))

        async with pg_session_factory() as session:
            row = await session.get(OrchestrationExecution, record.id)
            actions = (await session.execute(select(OrchestrationAction))).scalars().all()
        assert row.revision == record.revision
        assert row.status == ExecutionStatus.RUNNABLE.value
        assert actions == []

    async def test_a_foreign_tenant_cannot_observe_an_action(self, pg_session_factory, graph):
        identity, _ = await _seed(pg_session_factory, graph, org=ORG_A)
        async with pg_session_factory() as session:
            await prepare_action(session, identity=identity, intent=ActionIntent(operation_key="pr:mine", kind="open_pull_request"))
            await session.commit()

        intruder = ExecutionIdentity(
            org_id=ORG_B,
            node_id=identity.node_id,
            cycle=identity.cycle,
            accepted_plan_version=PLAN_VERSION,
            claim_id=CLAIM,
            claim_generation=1,
        )
        async with pg_session_factory() as session:
            with pytest.raises(ExecutionStoreError, match="No execution exists"):
                await record_observation(
                    session, identity=intruder, observation=Observation(operation_key="pr:mine", outcome=ObservedOutcome.SUCCEEDED)
                )

        async with pg_session_factory() as session:
            row = (await session.execute(select(OrchestrationAction))).scalar_one()
        assert row.status == ActionStatus.PREPARED.value, "an unobserved action must stay unobserved"

    async def test_the_same_operation_key_in_two_tenants_does_not_collide(self, pg_session_factory, graph):
        """`org_id` leads the unique index, so idempotency is per tenant.

        A globally-unique key would let one tenant's operation suppress another's —
        a cross-tenant denial of service dressed up as idempotency.
        """
        a_identity, _ = await _seed(pg_session_factory, graph, node="node_a", org=ORG_A)
        b_identity, _ = await _seed(pg_session_factory, graph, node="node_b", org=ORG_B)
        intent = ActionIntent(operation_key="pr:shared-key", kind="open_pull_request")

        async with pg_session_factory() as session:
            a = await prepare_action(session, identity=a_identity, intent=intent)
            b = await prepare_action(session, identity=b_identity, intent=intent)
            await session.commit()

        assert a.action.id != b.action.id
        assert b.reason is None, "another tenant's key is not a duplicate"
        async with pg_session_factory() as session:
            count = (await session.execute(text("SELECT count(*) FROM orchestration_actions"))).scalar_one()
        assert count == 2

    @pytest.mark.parametrize(
        ("flow_key", "node_key"),
        [("flow_b", "node_a"), ("flow_a", "node_b")],
    )
    async def test_composite_foreign_keys_reject_cross_tenant_graph_bindings(self, pg_session_factory, graph, flow_key, node_key):
        """Single-column ids are valid; only the tenant-paired FK rejects this."""
        async with pg_session_factory() as session:
            session.add(
                OrchestrationExecution(
                    org_id=ORG_A,
                    flow_id=graph[flow_key],
                    node_id=graph[node_key],
                    cycle=99,
                    phase=ExecutionPhase.ADMITTED.value,
                    status=ExecutionStatus.RUNNABLE.value,
                    revision=1,
                    accepted_plan_version=PLAN_VERSION,
                    claim_id=CLAIM,
                    claim_generation=1,
                    attempts=0,
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()

    async def test_composite_foreign_key_rejects_cross_tenant_action_binding(self, pg_session_factory, graph):
        _, execution = await _seed(pg_session_factory, graph, org=ORG_A)
        async with pg_session_factory() as session:
            session.add(
                OrchestrationAction(
                    org_id=ORG_B,
                    execution_id=execution.id,
                    operation_key="forged",
                    kind="open_pull_request",
                    status=ActionStatus.PREPARED.value,
                    attempt=0,
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()


class TestConcurrentAuthorityChange:
    async def test_a_superseded_generation_cannot_write_after_a_handover(self, pg_session_factory, graph):
        """The recovery case, end to end under real transactions.

        A handover advances the claim generation (#5127). The displaced worker — which
        may still be running and mid-call — must not be able to record progress
        against work it no longer owns.
        """
        identity, record = await _seed(pg_session_factory, graph, generation=1)

        # The successor takes over and advances the execution.
        successor = _identity(graph, generation=2)
        async with pg_session_factory() as session:
            claim = await session.get(OrchestrationWorkClaim, successor.claim_id)
            claim.generation = successor.claim_generation
            await session.flush()
            taken = await advance_execution(
                session,
                identity=successor,
                advance=PhaseAdvance(
                    phase=ExecutionPhase.REPAIRING,
                    status=ExecutionStatus.RUNNABLE,
                    expected_revision=record.revision,
                    next_check_at=_soon(),
                ),
            )
            await session.commit()
        assert taken.kind is OutcomeKind.APPLIED

        # The displaced worker, still holding generation 1 and its old revision.
        async with pg_session_factory() as session:
            refused = await advance_execution(
                session,
                identity=identity,
                advance=PhaseAdvance(
                    phase=ExecutionPhase.CONCLUDED,
                    status=ExecutionStatus.CONCLUDED,
                    expected_revision=record.revision,
                ),
            )
            await session.commit()

        # Refused on authority, not merely on revision: it must not be able to
        # succeed by re-reading and retrying with a current revision either.
        assert refused.kind is OutcomeKind.CONFLICT
        assert refused.reason == "claim_generation_superseded"

        async with pg_session_factory() as session:
            fresh = await session.get(OrchestrationExecution, record.id)
            retried = await advance_execution(
                session,
                identity=identity,
                advance=PhaseAdvance(
                    phase=ExecutionPhase.CONCLUDED,
                    status=ExecutionStatus.CONCLUDED,
                    expected_revision=fresh.revision,
                ),
            )
        assert retried.kind is OutcomeKind.CONFLICT

        async with pg_session_factory() as session:
            row = await session.get(OrchestrationExecution, record.id)
        assert row.phase == ExecutionPhase.REPAIRING.value, "the successor's write stands"
        assert row.status == ExecutionStatus.RUNNABLE.value
        # The successor's generation, durably recorded by its applied advance. This is
        # what makes the two refusals above terminal CONFLICTs rather than retryable
        # STALE answers: the fence can only measure a caller against the generation
        # that actually owns the work if that generation is written down.
        assert row.claim_generation == 2, "an applied advance records the generation it wrote under"

    async def test_a_new_current_plan_refuses_the_old_execution_identity(self, pg_session_factory, graph):
        identity, record = await _seed(pg_session_factory, graph)
        async with pg_session_factory() as session:
            current = (
                await session.execute(
                    select(OrchestrationAcceptedPlan).where(
                        OrchestrationAcceptedPlan.org_id == ORG_A,
                        OrchestrationAcceptedPlan.flow_id == graph["flow_a"],
                        OrchestrationAcceptedPlan.superseded_at.is_(None),
                    )
                )
            ).scalar_one()
            current.superseded_at = _soon()
            session.add(
                OrchestrationAcceptedPlan(
                    org_id=ORG_A,
                    flow_id=graph["flow_a"],
                    version=PLAN_VERSION + 1,
                    plan_document={},
                    plan_hash="plan-a-v4",
                )
            )
            await session.commit()

        async with pg_session_factory() as session:
            refused = await advance_execution(
                session,
                identity=identity,
                advance=PhaseAdvance(
                    phase=ExecutionPhase.CONCLUDED,
                    status=ExecutionStatus.CONCLUDED,
                    expected_revision=record.revision,
                ),
            )
        assert refused.kind is OutcomeKind.CONFLICT
        assert refused.reason == "accepted_plan_version_mismatch"


class TestConcurrentObservationSettlement:
    async def test_opposite_terminal_observations_cannot_overwrite_the_winner(self, pg_session_factory, graph):
        identity, _ = await _seed(pg_session_factory, graph)
        async with pg_session_factory() as session:
            await prepare_action(session, identity=identity, intent=ActionIntent(operation_key="settle", kind="k"))
            await session.commit()

        results = await asyncio.gather(
            _observe_attempt(
                pg_session_factory,
                identity,
                Observation(operation_key="settle", outcome=ObservedOutcome.SUCCEEDED, receipt_ref="receipt/success"),
            ),
            _observe_attempt(
                pg_session_factory,
                identity,
                Observation(operation_key="settle", outcome=ObservedOutcome.FAILED, receipt_ref="receipt/failure"),
            ),
        )

        assert {result.kind for result in results} == {OutcomeKind.APPLIED, OutcomeKind.CONFLICT}
        conflict = next(result for result in results if result.kind is OutcomeKind.CONFLICT)
        assert conflict.reason == "action_already_settled"
        async with pg_session_factory() as session:
            row = (await session.execute(select(OrchestrationAction).where(OrchestrationAction.operation_key == "settle"))).scalar_one()
        assert (row.status, row.receipt_ref) in {
            (ActionStatus.SUCCEEDED.value, "receipt/success"),
            (ActionStatus.FAILED.value, "receipt/failure"),
        }


class TestTheReadModelTakesNoLocks:
    """`load_execution(for_update=False)` is documented as lock-free. Enforce it here.

    The read model (#5145) and operator diagnostics render executions that are being
    written right now. If a plain read took `FOR UPDATE` — directly or inside the
    authority check it delegates to — then opening a dashboard would wait on whatever
    worker happens to hold the row, and a slow reader would in turn make the next
    writer wait. Both suites' semantic assertions pass either way, because `FOR UPDATE`
    is a no-op on SQLite: only a real server shows the block, which is why this lives
    here.
    """

    async def test_a_plain_read_does_not_block_on_a_live_writer(self, pg_session_factory, graph):
        identity, record = await _seed(pg_session_factory, graph)

        # A writer holding its transaction open, exactly as a worker does between
        # recording an intent and committing it.
        holder = pg_session_factory()
        await holder.begin()
        try:
            advanced = await advance_execution(
                holder,
                identity=identity,
                advance=PhaseAdvance(
                    phase=ExecutionPhase.DELIVERING,
                    status=ExecutionStatus.RUNNABLE,
                    expected_revision=record.revision,
                    next_check_at=_soon(),
                ),
            )
            assert advanced.kind is OutcomeKind.APPLIED

            async def read() -> ExecutionOutcome | None:
                async with pg_session_factory() as session:
                    return await load_execution(session, identity=identity, for_update=False)

            # Fails by timing out rather than by assertion if any lock is taken.
            outcome = await asyncio.wait_for(read(), timeout=10)
        finally:
            await holder.rollback()
            await holder.close()

        # It reads the last committed state, not the open writer's uncommitted phase.
        assert outcome is not None
        assert outcome.kind is OutcomeKind.APPLIED
        assert outcome.record.phase is ExecutionPhase.ADMITTED

    async def test_a_plain_read_does_not_block_the_next_writer(self, pg_session_factory, graph):
        """The converse: a reader's transaction must not make a writer wait either."""
        identity, record = await _seed(pg_session_factory, graph)

        reader = pg_session_factory()
        await reader.begin()
        try:
            assert await load_execution(reader, identity=identity, for_update=False) is not None

            async def write() -> ExecutionOutcome:
                async with pg_session_factory() as session:
                    outcome = await advance_execution(
                        session,
                        identity=identity,
                        advance=PhaseAdvance(
                            phase=ExecutionPhase.DELIVERING,
                            status=ExecutionStatus.RUNNABLE,
                            expected_revision=record.revision,
                            next_check_at=_soon(),
                        ),
                    )
                    await session.commit()
                    return outcome

            outcome = await asyncio.wait_for(write(), timeout=10)
        finally:
            await reader.rollback()
            await reader.close()

        assert outcome.kind is OutcomeKind.APPLIED

    async def test_a_refused_plain_read_still_takes_no_locks(self, pg_session_factory, graph):
        """The refusal path reads the row to decide disclosure; it must not lock it."""
        identity, record = await _seed(pg_session_factory, graph)
        stale = _identity(graph, plan=PLAN_VERSION + 1)

        holder = pg_session_factory()
        await holder.begin()
        try:
            await advance_execution(
                holder,
                identity=identity,
                advance=PhaseAdvance(
                    phase=ExecutionPhase.DELIVERING,
                    status=ExecutionStatus.RUNNABLE,
                    expected_revision=record.revision,
                    next_check_at=_soon(),
                ),
            )

            async def read() -> ExecutionOutcome | None:
                async with pg_session_factory() as session:
                    return await load_execution(session, identity=stale, for_update=False)

            refused = await asyncio.wait_for(read(), timeout=10)
        finally:
            await holder.rollback()
            await holder.close()

        assert refused is not None
        assert refused.kind is OutcomeKind.CONFLICT
        assert refused.reason == "accepted_plan_version_mismatch"
