"""Semantics of the execution/action delivery ledger (#5142, ENGINE-K1).

What is asserted here is *meaning*: which outcome each situation produces, which
columns the row is left holding, and which refusals are typed rather than silent.
The concurrency guarantee is asserted separately against a real PostgreSQL server
in `test_execution_store_postgres.py`, because SQLite treats `SELECT ... FOR UPDATE`
as a no-op — a locking assertion here would pass without testing any locking at
all. Keeping the two files apart is the same deliberate split
`test_work_claims.py` makes, and for the same reason: a green SQLite run must not
be mistakable for evidence about locking.

Several tests here are **negative** — they prove a guard exists rather than that a
happy path works:

- `TestStaleRevisionRefused`: a superseded revision cannot overwrite current
  progress, and the refusal writes nothing at all.
- `TestAuthorityBinding`: a wrong tenant, a wrong claim, or a superseded claim
  generation all refuse; a cross-tenant refusal does not even return the row.
- `TestNextCheckPairing`: a non-terminal advance without a next check time is
  refused, because such a row would be invisible to pickup forever.
- `TestUnknownOutcomeStaysUnknown`: an indeterminate observation is never
  upgraded to success.
- `TestOuterStatesUnchanged`: this module adds a vocabulary and does not touch
  `NodeState` or its human/terminal transitions.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration import state as state_module
from src.orchestration.execution_state import (
    TERMINAL_EXECUTION_STATUSES,
    UNRESOLVED_ACTION_STATUSES,
    ActionIntent,
    ActionStatus,
    BlockCode,
    BlockRecord,
    ExecutionIdentity,
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
from src.orchestration.state import NodeState
from src.orchestration.work_claims import OwnerKind
from src.shared.models.base import Base

ORG_A = "org-alpha"
ORG_B = "org-beta"
CLAIM = "claim-5142"
CLAIM_B = "claim-5142-b"
PLAN_VERSION = 3


# ---------------------------------------------------------------------------
# Fixtures — SQLite in memory, same shape as test_work_claims.py
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    # pysqlite's implicit BEGIN swallows SAVEPOINTs, and `prepare_action` /
    # `create_execution` both depend on a savepoint to isolate an IntegrityError
    # from the caller's transaction. Without these two hooks the nested block is a
    # no-op and the duplicate-key tests would assert nothing.
    @event.listens_for(eng.sync_engine, "connect")
    def _disable_pysqlite_implicit_begin(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(eng.sync_engine, "begin")
    def _emit_explicit_begin(connection):
        connection.exec_driver_sql("BEGIN")

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield eng
    await eng.dispose()


@pytest.fixture
def session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def session(session_factory):
    async with session_factory() as s:
        yield s


@pytest.fixture
async def graph(session):
    """A flow and two nodes in each tenant, so the FKs resolve.

    Two tenants because several of the guards here are about *not* crossing between
    them, and a single-tenant fixture cannot exercise that.
    """
    made: dict[str, str] = {}
    for org, key in ((ORG_A, "a"), (ORG_B, "b")):
        flow = OrchestrationFlow(org_id=org, slug=f"flow-{key}", title=f"Flow {key}", state="draft")
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
        for index in (1, 2):
            node = OrchestrationNode(
                org_id=org,
                flow_id=flow.id,
                epic_ref="E1",
                wave_ref="W1",
                node_ref=f"N{index}",
                kind="story",
                title=f"Node {key}{index}",
            )
            session.add(node)
            await session.flush()
            made[f"node_{key}{index}"] = node.id
    await session.flush()
    return made


def _identity(graph, *, node: str = "node_a1", org: str = ORG_A, cycle: int = 1, generation: int = 1, plan: int = PLAN_VERSION) -> ExecutionIdentity:
    return ExecutionIdentity(
        org_id=org,
        node_id=graph[node],
        cycle=cycle,
        accepted_plan_version=plan,
        claim_id=CLAIM_B if org == ORG_B else CLAIM,
        claim_generation=generation,
    )


async def _seed(session, graph, **kwargs):
    """Create one execution and return `(identity, record)`."""
    identity = _identity(graph, **kwargs)
    claim = await session.get(OrchestrationWorkClaim, identity.claim_id)
    claim.generation = identity.claim_generation
    claim.state = ClaimState.HELD.value
    await session.flush()
    flow_key = "flow_b" if identity.org_id == ORG_B else "flow_a"
    outcome = await create_execution(session, identity=identity, flow_id=graph[flow_key])
    assert outcome.kind is OutcomeKind.APPLIED
    return identity, outcome.record


def _soon() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=5)


async def _set_live_generation(session, graph, generation: int, *, org: str = ORG_A) -> None:
    claim_id = graph["claim_b"] if org == ORG_B else graph["claim_a"]
    claim = await session.get(OrchestrationWorkClaim, claim_id)
    claim.generation = generation
    claim.state = ClaimState.HELD.value
    await session.flush()


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class TestCreateExecution:
    async def test_creates_a_runnable_identity_bound_to_its_authority(self, session, graph):
        identity, record = await _seed(session, graph)

        assert record.org_id == ORG_A
        assert record.node_id == graph["node_a1"]
        assert record.cycle == 1
        assert record.phase is ExecutionPhase.ADMITTED
        assert record.status is ExecutionStatus.RUNNABLE
        assert record.revision == 1
        assert record.attempts == 0
        # The authority is stored on the row, not looked up later: the claim and plan
        # that admitted this execution must remain inspectable after both move on.
        assert record.claim_id == CLAIM
        assert record.claim_generation == 1
        assert record.accepted_plan_version == PLAN_VERSION
        # A fresh execution is due immediately, so it is never invisible to pickup on
        # account of a missing check time.
        assert record.next_check_at is not None
        assert record.block is None
        assert record.runnable is True

    async def test_repeat_create_returns_the_same_identity(self, session, graph):
        identity, first = await _seed(session, graph)
        again = await create_execution(session, identity=identity, flow_id=graph["flow_a"])

        assert again.kind is OutcomeKind.APPLIED
        assert again.record.id == first.id
        rows = (await session.execute(select(OrchestrationExecution))).scalars().all()
        assert len(rows) == 1, "a repeat create must not produce a second identity"

    async def test_repeat_create_does_not_reset_progress(self, session, graph):
        """The dangerous version of idempotency: re-create must not rewind the row.

        A restarting worker calls `create_execution` first. If that reset the phase,
        every crash would silently replay delivery from the beginning.
        """
        identity, record = await _seed(session, graph)
        await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.DELIVERING,
                status=ExecutionStatus.RUNNABLE,
                expected_revision=record.revision,
                next_check_at=_soon(),
            ),
        )

        again = await create_execution(session, identity=identity, flow_id=graph["flow_a"])
        assert again.record.phase is ExecutionPhase.DELIVERING
        assert again.record.revision == 2

    async def test_second_cycle_is_a_separate_identity(self, session, graph):
        """Cycle is in the key so a re-run is a new record, not an overwrite.

        Without it, the second attempt at a node would inherit the first's action
        rows and read its completed steps as already done.
        """
        first_identity, first = await _seed(session, graph, cycle=1)
        _, second = await _seed(session, graph, cycle=2)

        assert second.id != first.id
        assert second.cycle == 2
        assert first_identity.cycle == 1

    async def test_same_node_ref_in_another_tenant_is_independent(self, session, graph):
        _, a = await _seed(session, graph, node="node_a1", org=ORG_A)
        _, b = await _seed(session, graph, node="node_b1", org=ORG_B)
        assert a.id != b.id
        assert {a.org_id, b.org_id} == {ORG_A, ORG_B}

    async def test_create_requires_a_flow(self, session, graph):
        with pytest.raises(ExecutionStoreError, match="name the flow"):
            await create_execution(session, identity=_identity(graph), flow_id="  ")

    async def test_adopting_an_execution_re_checks_authority(self, session, graph):
        """Re-create with a superseded generation is refused, not silently adopted.

        Adopting an execution is as consequential as advancing one: a worker whose
        claim was handed away must not resume the work by asking for it again.
        """
        identity, record = await _seed(session, graph, generation=2)
        stale = _identity(graph, generation=1)

        refused = await create_execution(session, identity=stale, flow_id=graph["flow_a"])
        assert refused.kind is OutcomeKind.CONFLICT
        assert refused.reason == "claim_generation_superseded"
        assert refused.record.id == record.id


class TestLoadExecution:
    async def test_absent_is_none_not_a_refusal(self, session, graph):
        """Absent and refused are different answers, and callers act on them differently."""
        assert await load_execution(session, identity=_identity(graph)) is None

    async def test_loads_under_a_lock_when_asked(self, session, graph):
        identity, record = await _seed(session, graph)
        outcome = await load_execution(session, identity=identity, for_update=True)
        assert outcome.kind is OutcomeKind.APPLIED
        assert outcome.record.id == record.id

    async def test_cross_tenant_load_is_refused_without_returning_the_row(self, session, graph):
        identity, _ = await _seed(session, graph)
        intruder = ExecutionIdentity(
            org_id=ORG_B,
            node_id=identity.node_id,
            cycle=identity.cycle,
            accepted_plan_version=PLAN_VERSION,
            claim_id=CLAIM,
            claim_generation=1,
        )
        # Scoped by org_id, so the row is simply not there for the other tenant.
        assert await load_execution(session, identity=intruder) is None


# ---------------------------------------------------------------------------
# Authority
# ---------------------------------------------------------------------------


class TestAuthorityBinding:
    """Every mutating path re-verifies the caller's binding inside the transaction."""

    @pytest.mark.parametrize(
        ("kwargs", "reason"),
        [
            ({"generation": 1}, "claim_generation_superseded"),
            ({"plan": PLAN_VERSION + 1}, "accepted_plan_version_mismatch"),
        ],
    )
    async def test_advance_refuses_a_lapsed_binding(self, session, graph, kwargs, reason):
        identity, record = await _seed(session, graph, generation=2)
        bad = _identity(graph, **{"generation": 2, **kwargs})

        outcome = await advance_execution(
            session,
            identity=bad,
            advance=PhaseAdvance(
                phase=ExecutionPhase.DELIVERING,
                status=ExecutionStatus.RUNNABLE,
                expected_revision=record.revision,
                next_check_at=_soon(),
            ),
        )
        assert outcome.kind is OutcomeKind.CONFLICT
        assert outcome.reason == reason
        # Nothing written: a refused caller must not have moved the row.
        row = await session.get(OrchestrationExecution, record.id)
        assert row.phase == ExecutionPhase.ADMITTED.value
        assert row.revision == 1

    async def test_advance_refuses_a_different_claim(self, session, graph):
        identity, record = await _seed(session, graph)
        other_claim = ExecutionIdentity(
            org_id=ORG_A,
            node_id=identity.node_id,
            cycle=identity.cycle,
            accepted_plan_version=PLAN_VERSION,
            claim_id="claim-somebody-else",
            claim_generation=1,
        )
        outcome = await advance_execution(
            session,
            identity=other_claim,
            advance=PhaseAdvance(
                phase=ExecutionPhase.CONCLUDED,
                status=ExecutionStatus.CONCLUDED,
                expected_revision=record.revision,
            ),
        )
        assert outcome.kind is OutcomeKind.CONFLICT
        assert outcome.reason == "claim_mismatch"

    async def test_create_refuses_a_released_live_claim(self, session, graph):
        identity = _identity(graph)
        claim = await session.get(OrchestrationWorkClaim, identity.claim_id)
        claim.state = ClaimState.RELEASED.value
        await session.flush()

        outcome = await create_execution(session, identity=identity, flow_id=graph["flow_a"])

        assert outcome.kind is OutcomeKind.CONFLICT
        assert outcome.reason == "claim_not_held"
        assert (await session.execute(select(OrchestrationExecution))).scalars().all() == []

    @pytest.mark.parametrize(
        ("owner_kind", "owner_ref_key"),
        [
            (OwnerKind.DIRECT_DISPATCH.value, "flow_a"),
            (OwnerKind.ENGINE_FLOW.value, "flow_b"),
        ],
    )
    async def test_create_refuses_a_claim_not_owned_by_this_engine_flow(self, session, graph, owner_kind, owner_ref_key):
        identity = _identity(graph)
        claim = await session.get(OrchestrationWorkClaim, identity.claim_id)
        claim.owner_kind = owner_kind
        claim.owner_ref = graph[owner_ref_key]
        await session.flush()

        outcome = await create_execution(session, identity=identity, flow_id=graph["flow_a"])

        assert outcome.kind is OutcomeKind.CONFLICT
        assert outcome.reason == "claim_owner_mismatch"
        assert outcome.record is None
        assert (await session.execute(select(OrchestrationExecution))).scalars().all() == []

    async def test_advance_checks_the_live_claim_not_only_the_ledger_snapshot(self, session, graph):
        identity, record = await _seed(session, graph)
        await _set_live_generation(session, graph, 2)

        outcome = await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.CONCLUDED,
                status=ExecutionStatus.CONCLUDED,
                expected_revision=record.revision,
            ),
        )

        assert outcome.kind is OutcomeKind.CONFLICT
        assert outcome.reason == "claim_generation_superseded"
        assert (await session.get(OrchestrationExecution, record.id)).revision == record.revision

    async def test_advance_checks_the_current_accepted_plan(self, session, graph):
        identity, record = await _seed(session, graph)
        current = (
            await session.execute(
                select(OrchestrationAcceptedPlan).where(
                    OrchestrationAcceptedPlan.org_id == ORG_A,
                    OrchestrationAcceptedPlan.flow_id == graph["flow_a"],
                    OrchestrationAcceptedPlan.superseded_at.is_(None),
                )
            )
        ).scalar_one()
        current.superseded_at = datetime.now(UTC)
        session.add(
            OrchestrationAcceptedPlan(
                org_id=ORG_A,
                flow_id=graph["flow_a"],
                version=PLAN_VERSION + 1,
                plan_document={},
                plan_hash="plan-a-v4",
            )
        )
        await session.flush()

        outcome = await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.CONCLUDED,
                status=ExecutionStatus.CONCLUDED,
                expected_revision=record.revision,
            ),
        )

        assert outcome.kind is OutcomeKind.CONFLICT
        assert outcome.reason == "accepted_plan_version_mismatch"

    async def test_create_refuses_cross_tenant_flow_and_node_bindings(self, session, graph):
        outcome = await create_execution(
            session,
            identity=_identity(graph, node="node_b1", org=ORG_A),
            flow_id=graph["flow_b"],
        )

        assert outcome.kind is OutcomeKind.CONFLICT
        assert outcome.reason == "tenant_binding_mismatch"
        assert (await session.execute(select(OrchestrationExecution))).scalars().all() == []

    async def test_a_newer_generation_may_continue_the_execution(self, session, graph):
        """A legitimate handover advances the generation; the new owner must proceed.

        Refusing a *newer* generation would strand every handed-over execution, which
        is the opposite of the recovery this ledger is for. Only an older generation
        is stale.
        """
        identity, record = await _seed(session, graph, generation=1)
        successor = _identity(graph, generation=2)
        await _set_live_generation(session, graph, 2)

        outcome = await advance_execution(
            session,
            identity=successor,
            advance=PhaseAdvance(
                phase=ExecutionPhase.REPAIRING,
                status=ExecutionStatus.RUNNABLE,
                expected_revision=record.revision,
                next_check_at=_soon(),
            ),
        )
        assert outcome.kind is OutcomeKind.APPLIED
        assert outcome.record.phase is ExecutionPhase.REPAIRING
        # The successor's authority is now DURABLE, not just accepted for this one
        # call. Persisting it is what lets the next caller be measured against the
        # generation that actually owns the work; see the displaced-worker test below,
        # which is the failure this assertion's absence allowed.
        assert outcome.record.claim_generation == 2
        row = await session.get(OrchestrationExecution, record.id)
        assert row.claim_generation == 2

    async def test_a_superseded_generation_cannot_write_after_a_handover(self, session, graph):
        """The displaced worker is refused terminally, not invited to retry.

        The property this ledger exists for, and the one that must not be provable
        only in CI: the PostgreSQL version of this test skips wherever `pgserver` has
        no wheel for the running interpreter, so without this case the guard is
        unverified on those environments.

        Distinct from `test_adopting_an_execution_re_checks_authority`, which seeds
        generation 2 and exercises `create_execution` — a path that always persisted
        the generation. What is covered here is advance-at-2 *then* attempt-at-1,
        which is the sequence that was broken: with the generation left unwritten by
        the advance, the row stayed at 1, the displaced worker's generation was not
        *older* than the stored one, no conflict was detected, and the revision fence
        answered STALE. The distinction is the whole point — STALE is retryable by
        this module's contract, so the displaced worker would re-read, retry at the
        current revision, and succeed in writing work it no longer owns. CONFLICT is
        terminal.
        """
        displaced, record = await _seed(session, graph, generation=1)
        successor = _identity(graph, generation=2)
        await _set_live_generation(session, graph, 2)

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
        assert taken.kind is OutcomeKind.APPLIED

        # The displaced worker, still running and still holding generation 1.
        refused = await advance_execution(
            session,
            identity=displaced,
            advance=PhaseAdvance(
                phase=ExecutionPhase.CONCLUDED,
                status=ExecutionStatus.CONCLUDED,
                expected_revision=record.revision,
            ),
        )
        assert refused.kind is OutcomeKind.CONFLICT, "a superseded generation must be refused on authority, not merely on revision"
        assert refused.reason == "claim_generation_superseded"

        # And it cannot get in by doing what a STALE answer would have told it to do:
        # re-read the current revision and try again.
        fresh = await session.get(OrchestrationExecution, record.id)
        retried = await advance_execution(
            session,
            identity=displaced,
            advance=PhaseAdvance(
                phase=ExecutionPhase.CONCLUDED,
                status=ExecutionStatus.CONCLUDED,
                expected_revision=fresh.revision,
            ),
        )
        assert retried.kind is OutcomeKind.CONFLICT
        assert retried.reason == "claim_generation_superseded"

        # The successor's progress survives untouched.
        row = await session.get(OrchestrationExecution, record.id)
        assert row.phase == ExecutionPhase.REPAIRING.value
        assert row.status == ExecutionStatus.RUNNABLE.value

    async def test_cross_tenant_conflict_withholds_the_record(self, session, graph):
        """The one case where the refusal returns no record at all.

        Returning it would leak across the boundary the refusal exists to enforce.
        Reached by constructing the mismatch directly, since the scoped queries make
        it unreachable through the public path.
        """
        from src.orchestration.execution_store import _binding_conflict, _conflict

        identity, record = await _seed(session, graph)
        row = await session.get(OrchestrationExecution, record.id)
        foreign = ExecutionIdentity(
            org_id=ORG_B,
            node_id=identity.node_id,
            cycle=1,
            accepted_plan_version=PLAN_VERSION,
            claim_id=CLAIM,
            claim_generation=1,
        )
        assert _binding_conflict(row, foreign) == "tenant_mismatch"
        outcome = _conflict(row, "tenant_mismatch")
        assert outcome.kind is OutcomeKind.CONFLICT
        assert outcome.record is None

    async def test_prepare_and_observe_also_refuse(self, session, graph):
        identity, _ = await _seed(session, graph, generation=2)
        stale = _identity(graph, generation=1)

        prepared = await prepare_action(session, identity=stale, intent=ActionIntent(operation_key="op-1", kind="open_pull_request"))
        assert prepared.kind is OutcomeKind.CONFLICT

        observed = await record_observation(session, identity=stale, observation=Observation(operation_key="op-1", outcome=ObservedOutcome.SUCCEEDED))
        assert observed.kind is OutcomeKind.CONFLICT
        # And no action row was created by the refused preparation.
        assert (await session.execute(select(OrchestrationAction))).scalars().all() == []

    @pytest.mark.parametrize("operation", ["prepare", "observe", "advance"])
    async def test_operations_on_a_missing_execution_are_typed_refusals(self, session, graph, operation):
        identity = _identity(graph)
        with pytest.raises(ExecutionStoreError, match="No execution exists"):
            if operation == "prepare":
                await prepare_action(session, identity=identity, intent=ActionIntent(operation_key="op", kind="k"))
            elif operation == "observe":
                await record_observation(session, identity=identity, observation=Observation(operation_key="op", outcome=ObservedOutcome.FAILED))
            else:
                await advance_execution(
                    session,
                    identity=identity,
                    advance=PhaseAdvance(phase=ExecutionPhase.PREPARING, status=ExecutionStatus.RUNNABLE, expected_revision=1, next_check_at=_soon()),
                )

    async def test_preparing_an_action_also_advances_the_fence(self, session, graph):
        """A successor's first act may be an action, not an advance.

        The fence has to move on that path too, or the predecessor keeps write access
        until some later advance happens to close it.
        """
        identity, _ = await _seed(session, graph, generation=1)
        await _set_live_generation(session, graph, 3)

        prepared = await prepare_action(
            session,
            identity=_identity(graph, generation=3),
            intent=ActionIntent(operation_key="pr:successor", kind="open_pull_request"),
        )
        assert prepared.kind is OutcomeKind.APPLIED
        assert prepared.record.claim_generation == 3

        refused = await prepare_action(
            session,
            identity=identity,
            intent=ActionIntent(operation_key="pr:displaced", kind="open_pull_request"),
        )
        assert refused.kind is OutcomeKind.CONFLICT
        assert refused.reason == "claim_generation_superseded"

    async def test_the_fence_is_never_lowered(self, session, graph):
        """Monotonic: a lower generation cannot hand the fence back to a superseded run.

        `create_execution` adopts an existing record, and adopting is as consequential
        as advancing — an adopt that *lowered* the generation would restore write
        access to the run that was handed over.
        """
        identity, _ = await _seed(session, graph, generation=1)
        await _set_live_generation(session, graph, 4)
        await advance_execution(
            session,
            identity=_identity(graph, generation=4),
            advance=PhaseAdvance(
                phase=ExecutionPhase.DELIVERING,
                status=ExecutionStatus.RUNNABLE,
                expected_revision=1,
                next_check_at=_soon(),
            ),
        )

        readopted = await create_execution(session, identity=identity, flow_id=graph["flow_a"])
        assert readopted.kind is OutcomeKind.CONFLICT
        assert readopted.reason == "claim_generation_superseded"

        row = await session.get(OrchestrationExecution, readopted.record.id)
        assert row.claim_generation == 4

    async def test_a_foreign_claim_is_refused_without_disclosing_the_binding(self, session, graph):
        """A refusal must not hand back the values that would satisfy it.

        `load_execution` is a plain read whose only correct inputs are `org_id`,
        `node_id` and `cycle` — all non-secret graph identifiers. If the refusal
        carried the record, a caller presenting any made-up claim would harvest the
        real `claim_id`, `claim_generation` and `accepted_plan_version`, which is
        exactly the binding the next write's authority check tests.
        """
        identity, _ = await _seed(session, graph)
        probe = ExecutionIdentity(
            org_id=ORG_A,
            node_id=identity.node_id,
            cycle=identity.cycle,
            accepted_plan_version=PLAN_VERSION + 7,
            claim_id="claim-never-issued-to-me",
            claim_generation=1,
        )

        outcome = await load_execution(session, identity=probe)
        assert outcome.kind is OutcomeKind.CONFLICT
        assert outcome.reason == "claim_mismatch"
        assert outcome.record is None, "a caller that never held the claim learns nothing about it"

        # A lapsed generation under the RIGHT claim still gets the record: that caller
        # already knows these values and needs to see what superseded it.
        _, seeded = await _seed(session, graph, node="node_a2", generation=2)
        stale = _identity(graph, node="node_a2", generation=1)
        lapsed = await load_execution(session, identity=stale)
        assert lapsed.kind is OutcomeKind.CONFLICT
        assert lapsed.reason == "claim_generation_superseded"
        assert lapsed.record is not None
        assert lapsed.record.claim_generation == 2


# ---------------------------------------------------------------------------
# Idempotent actions
# ---------------------------------------------------------------------------


class TestPrepareAction:
    async def test_records_intent_before_the_effect(self, session, graph):
        identity, record = await _seed(session, graph)
        outcome = await prepare_action(
            session,
            identity=identity,
            intent=ActionIntent(
                operation_key="pr:node-a1:cycle-1",
                kind="open_pull_request",
                artifact_ref="s3://adp-artifacts/node-a1/cycle-1/patch.diff",
                detail={"base": "main"},
            ),
        )

        assert outcome.kind is OutcomeKind.APPLIED
        action = outcome.action
        assert action.status is ActionStatus.PREPARED
        assert action.status in UNRESOLVED_ACTION_STATUSES
        assert action.resolved is False
        assert action.attempt == record.attempts
        assert action.artifact_ref.startswith("s3://")
        assert action.receipt_ref is None
        assert action.observed_at is None
        assert action.detail == {"base": "main"}

    async def test_duplicate_operation_key_returns_the_original_record(self, session, graph):
        """The retry-after-crash guarantee. One key, one action, original status.

        A second row would mean a second pull request, which is exactly the
        double-effect this ledger exists to prevent.
        """
        identity, _ = await _seed(session, graph)
        intent = ActionIntent(operation_key="pr:once", kind="open_pull_request", artifact_ref="ref/first")
        first = await prepare_action(session, identity=identity, intent=intent)
        await record_observation(
            session,
            identity=identity,
            observation=Observation(operation_key="pr:once", outcome=ObservedOutcome.SUCCEEDED, receipt_ref="pr/42"),
        )

        repeat = await prepare_action(
            session,
            identity=identity,
            # A retrying caller may present different incidental detail; it must not
            # overwrite what the first attempt recorded.
            intent=ActionIntent(operation_key="pr:once", kind="open_pull_request", artifact_ref="ref/second"),
        )

        assert repeat.kind is OutcomeKind.APPLIED
        assert repeat.reason == "action_already_prepared"
        assert repeat.action.id == first.action.id
        # The original status and receipt come back, which is what tells the retrying
        # caller the step already took effect.
        assert repeat.action.status is ActionStatus.SUCCEEDED
        assert repeat.action.receipt_ref == "pr/42"
        assert repeat.action.artifact_ref == "ref/first"

        rows = (await session.execute(select(OrchestrationAction))).scalars().all()
        assert len(rows) == 1

    async def test_the_same_key_in_a_different_cycle_is_a_different_action(self, session, graph):
        """Uniqueness is per execution, so a re-run is not blocked by its predecessor."""
        first_identity, _ = await _seed(session, graph, cycle=1)
        second_identity, _ = await _seed(session, graph, cycle=2)
        intent = ActionIntent(operation_key="pr:same-key", kind="open_pull_request")

        a = await prepare_action(session, identity=first_identity, intent=intent)
        b = await prepare_action(session, identity=second_identity, intent=intent)

        assert a.action.id != b.action.id
        assert b.reason is None, "a different execution is not a duplicate"


class TestRecordObservation:
    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            (ObservedOutcome.SUCCEEDED, ActionStatus.SUCCEEDED),
            (ObservedOutcome.FAILED, ActionStatus.FAILED),
            (ObservedOutcome.INDETERMINATE, ActionStatus.UNKNOWN),
        ],
    )
    async def test_every_observed_outcome_round_trips(self, session, graph, outcome, expected):
        identity, _ = await _seed(session, graph)
        await prepare_action(session, identity=identity, intent=ActionIntent(operation_key="op", kind="k"))

        result = await record_observation(
            session,
            identity=identity,
            observation=Observation(operation_key="op", outcome=outcome, receipt_ref="receipt/1", detail={"probe": "provider-api"}),
        )
        assert result.kind is OutcomeKind.APPLIED
        assert result.action.status is expected
        assert result.action.receipt_ref == "receipt/1"
        # Nested under its own key: the observer's detail must not overwrite the
        # intent's, since the two are separate assertions by separate parties.
        assert result.action.detail["observation"] == {"probe": "provider-api"}
        # `observed_at` is stamped even for INDETERMINATE — we did look, and the fact
        # that we looked is itself worth keeping.
        assert result.action.observed_at is not None

    async def test_observing_something_never_prepared_is_refused(self, session, graph):
        """No action row may be conjured by its own observation.

        Such a row would have no intent behind it and no attempt number that means
        anything, so the disagreement is surfaced instead of papered over.
        """
        identity, _ = await _seed(session, graph)
        with pytest.raises(ExecutionStoreError, match="observations settle prepared actions only"):
            await record_observation(
                session, identity=identity, observation=Observation(operation_key="never-prepared", outcome=ObservedOutcome.SUCCEEDED)
            )

    async def test_duplicate_terminal_observation_is_idempotent(self, session, graph):
        identity, _ = await _seed(session, graph)
        await prepare_action(session, identity=identity, intent=ActionIntent(operation_key="op", kind="k"))
        observation = Observation(operation_key="op", outcome=ObservedOutcome.SUCCEEDED, receipt_ref="receipt/1", detail="verified")
        first = await record_observation(session, identity=identity, observation=observation)
        observed_at = first.action.observed_at

        duplicate = await record_observation(session, identity=identity, observation=observation)

        assert duplicate.kind is OutcomeKind.APPLIED
        assert duplicate.reason == "observation_already_recorded"
        assert duplicate.action.observed_at == observed_at

    async def test_conflicting_terminal_observation_is_refused_without_rewrite(self, session, graph, caplog):
        identity, _ = await _seed(session, graph)
        await prepare_action(session, identity=identity, intent=ActionIntent(operation_key="op", kind="k"))
        first = await record_observation(
            session,
            identity=identity,
            observation=Observation(operation_key="op", outcome=ObservedOutcome.SUCCEEDED, receipt_ref="receipt/success"),
        )

        conflict = await record_observation(
            session,
            identity=identity,
            observation=Observation(operation_key="op", outcome=ObservedOutcome.FAILED, receipt_ref="receipt/failure"),
        )

        assert conflict.kind is OutcomeKind.CONFLICT
        assert conflict.reason == "action_already_settled"
        assert conflict.action.status is ActionStatus.SUCCEEDED
        assert conflict.action.receipt_ref == "receipt/success"
        assert conflict.action.observed_at == first.action.observed_at
        assert "refusing conflicting observation" in caplog.text


class TestUnknownOutcomeStaysUnknown:
    """An outcome nobody could confirm is never upgraded to success."""

    async def test_indeterminate_leaves_the_action_unresolved(self, session, graph):
        identity, record = await _seed(session, graph)
        await prepare_action(session, identity=identity, intent=ActionIntent(operation_key="op-unknown", kind="open_pull_request"))
        result = await record_observation(
            session, identity=identity, observation=Observation(operation_key="op-unknown", outcome=ObservedOutcome.INDETERMINATE)
        )

        assert result.action.status is ActionStatus.UNKNOWN
        assert result.action.status in UNRESOLVED_ACTION_STATUSES
        assert result.action.resolved is False, "an unknown outcome must not read as settled"

    async def test_awaiting_external_names_the_step_to_ask_about(self, session, graph):
        """The recovery hook: the row itself says which step to go and check.

        Without `pending_action_key`, a process resuming an `AWAITING_EXTERNAL`
        execution would have to guess, and guessing means either stalling or
        repeating the effect.
        """
        identity, record = await _seed(session, graph)
        outcome = await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.AWAITING_REVIEW,
                status=ExecutionStatus.AWAITING_EXTERNAL,
                expected_revision=record.revision,
                next_check_at=_soon(),
            ),
            intent=ActionIntent(operation_key="await:review:pr-42", kind="await_review"),
        )

        assert outcome.record.status is ExecutionStatus.AWAITING_EXTERNAL
        assert outcome.record.pending_action_key == "await:review:pr-42"
        assert outcome.record.runnable is False
        assert outcome.action.status is ActionStatus.PREPARED

    async def test_leaving_awaiting_external_clears_the_pending_key(self, session, graph):
        identity, record = await _seed(session, graph)
        waiting = await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.AWAITING_REVIEW,
                status=ExecutionStatus.AWAITING_EXTERNAL,
                expected_revision=record.revision,
                next_check_at=_soon(),
            ),
            intent=ActionIntent(operation_key="await:review", kind="await_review"),
        )
        moved = await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.SETTLING,
                status=ExecutionStatus.RUNNABLE,
                expected_revision=waiting.record.revision,
                next_check_at=_soon(),
            ),
        )
        # A leftover key would point at a step that has already been settled.
        assert moved.record.pending_action_key is None


# ---------------------------------------------------------------------------
# Compare-and-set
# ---------------------------------------------------------------------------


class TestAdvanceExecution:
    async def test_applies_phase_status_and_next_check_together(self, session, graph):
        identity, record = await _seed(session, graph)
        due = _soon()
        deadline = datetime.now(UTC) + timedelta(hours=2)

        outcome = await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.SUBMITTING,
                status=ExecutionStatus.RUNNABLE,
                expected_revision=record.revision,
                next_check_at=due,
                consume_attempt=True,
                deadline_at=deadline,
                progress_note="patch pushed",
            ),
        )

        assert outcome.kind is OutcomeKind.APPLIED
        assert outcome.record.phase is ExecutionPhase.SUBMITTING
        assert outcome.record.status is ExecutionStatus.RUNNABLE
        assert outcome.record.next_check_at is not None
        assert outcome.record.deadline_at is not None
        assert outcome.record.progress_note == "patch pushed"
        # Revision advances by exactly one; that is what makes the caller's next
        # compare-and-set meaningful.
        assert outcome.record.revision == record.revision + 1
        assert outcome.record.attempts == 1
        assert outcome.record.progressed_at is not None

    async def test_attempts_are_only_consumed_when_asked(self, session, graph):
        identity, record = await _seed(session, graph)
        outcome = await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.PREPARING,
                status=ExecutionStatus.RUNNABLE,
                expected_revision=record.revision,
                next_check_at=_soon(),
            ),
        )
        assert outcome.record.attempts == 0, "a phase move is not an attempt"

    @pytest.mark.parametrize("phase", list(ExecutionPhase))
    async def test_every_declared_phase_serializes(self, session, graph, phase):
        """Each phase in the vocabulary survives a write and a read back.

        Parametrized over the enum itself, so a member added later without a storage
        path fails here rather than in the deployed database.
        """
        identity, record = await _seed(session, graph)
        terminal = phase is ExecutionPhase.CONCLUDED
        outcome = await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=phase,
                status=ExecutionStatus.CONCLUDED if terminal else ExecutionStatus.RUNNABLE,
                expected_revision=record.revision,
                next_check_at=None if terminal else _soon(),
            ),
        )
        assert outcome.record.phase is phase
        reloaded = await load_execution(session, identity=identity)
        assert reloaded.record.phase is phase

    @pytest.mark.parametrize("status", list(ExecutionStatus))
    async def test_every_declared_status_serializes(self, session, graph, status):
        identity, record = await _seed(session, graph)
        block = (
            BlockRecord(code=BlockCode.HUMAN_GATE_REQUIRED, owner="platform-operator", required_input="gate approval")
            if status is ExecutionStatus.BLOCKED
            else None
        )
        outcome = await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.SETTLING,
                status=status,
                expected_revision=record.revision,
                next_check_at=None if status in TERMINAL_EXECUTION_STATUSES else _soon(),
            ),
            block=block,
        )
        assert outcome.record.status is status
        reloaded = await load_execution(session, identity=identity)
        assert reloaded.record.status is status

    @pytest.mark.parametrize("status", sorted(TERMINAL_EXECUTION_STATUSES, key=lambda s: s.value))
    async def test_a_terminal_status_clears_the_next_check_time(self, session, graph, status):
        """A terminal row must not carry a wake-up a runner would trust."""
        identity, record = await _seed(session, graph)
        outcome = await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.CONCLUDED,
                status=status,
                expected_revision=record.revision,
                # Offered deliberately: the store must clear it rather than trust it.
                next_check_at=_soon(),
            ),
        )
        assert outcome.record.next_check_at is None
        assert outcome.record.runnable is False


class TestNextCheckPairing:
    @pytest.mark.parametrize(
        "status",
        [ExecutionStatus.RUNNABLE, ExecutionStatus.AWAITING_EXTERNAL, ExecutionStatus.BLOCKED],
    )
    async def test_a_non_terminal_advance_needs_a_next_check_time(self, session, graph, status):
        """Refused, because such a row is work that has moved on and will never be
        picked up again — a permanent stall that reads as progress in every view."""
        identity, record = await _seed(session, graph)
        block = (
            BlockRecord(code=BlockCode.DEPENDENCY_UNSATISFIED, owner="engine", required_input="predecessor completion")
            if status is ExecutionStatus.BLOCKED
            else None
        )
        with pytest.raises(ExecutionStoreError, match="invisible to pickup"):
            await advance_execution(
                session,
                identity=identity,
                advance=PhaseAdvance(
                    phase=ExecutionPhase.DELIVERING,
                    status=status,
                    expected_revision=record.revision,
                    next_check_at=None,
                ),
                block=block,
            )
        row = await session.get(OrchestrationExecution, record.id)
        assert row.revision == 1, "a refused advance writes nothing"


class TestStaleRevisionRefused:
    """A superseded revision cannot overwrite current progress."""

    async def test_stale_advance_is_refused_and_writes_nothing(self, session, graph):
        identity, record = await _seed(session, graph)
        # Someone else moves the row first.
        winner = await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.DELIVERING,
                status=ExecutionStatus.RUNNABLE,
                expected_revision=record.revision,
                next_check_at=_soon(),
                progress_note="winner",
            ),
        )
        assert winner.kind is OutcomeKind.APPLIED

        loser = await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.CONCLUDED,
                status=ExecutionStatus.CONCLUDED,
                expected_revision=record.revision,  # the revision it read before
                progress_note="loser",
            ),
        )

        assert loser.kind is OutcomeKind.STALE
        assert loser.reason == "stale_revision"
        # The current record comes back so the caller can re-read without a second
        # round trip — and it is the *winner's* state, unmodified.
        assert loser.record.revision == winner.record.revision
        assert loser.record.phase is ExecutionPhase.DELIVERING
        assert loser.record.progress_note == "winner"

    async def test_a_stale_advance_does_not_prepare_its_action(self, session, graph):
        """The intent belongs to the advance, so a refused advance leaves no effect
        recorded — otherwise an action would exist that nothing will ever follow up."""
        identity, record = await _seed(session, graph)
        await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.DELIVERING,
                status=ExecutionStatus.RUNNABLE,
                expected_revision=record.revision,
                next_check_at=_soon(),
            ),
        )
        stale = await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.SUBMITTING,
                status=ExecutionStatus.RUNNABLE,
                expected_revision=record.revision,
                next_check_at=_soon(),
            ),
            intent=ActionIntent(operation_key="pr:should-not-exist", kind="open_pull_request"),
        )
        assert stale.kind is OutcomeKind.STALE
        assert stale.action is None
        assert (await session.execute(select(OrchestrationAction))).scalars().all() == []

    async def test_a_revision_from_the_future_is_also_refused(self, session, graph):
        """Not only older revisions: any mismatch means the caller's read is not the
        row, and writing on a number it never observed would defeat the fence."""
        identity, record = await _seed(session, graph)
        outcome = await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.DELIVERING,
                status=ExecutionStatus.RUNNABLE,
                expected_revision=record.revision + 7,
                next_check_at=_soon(),
            ),
        )
        assert outcome.kind is OutcomeKind.STALE


class TestAtomicIntentAndContinuation:
    """The store-both-atomically requirement: intent and next check, or neither."""

    async def test_intent_and_next_check_are_written_in_one_transaction(self, session, graph):
        identity, record = await _seed(session, graph)
        outcome = await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.SUBMITTING,
                status=ExecutionStatus.AWAITING_EXTERNAL,
                expected_revision=record.revision,
                next_check_at=_soon(),
            ),
            intent=ActionIntent(operation_key="pr:open", kind="open_pull_request", artifact_ref="s3://bucket/patch"),
        )
        await session.commit()

        async with AsyncSession(session.bind, expire_on_commit=False) as fresh:
            row = await fresh.get(OrchestrationExecution, record.id)
            actions = (await fresh.execute(select(OrchestrationAction))).scalars().all()
        assert row.next_check_at is not None
        assert row.pending_action_key == "pr:open"
        assert len(actions) == 1
        assert actions[0].operation_key == "pr:open"
        assert outcome.action.operation_key == "pr:open"

    async def test_a_failed_transaction_rolls_back_intent_and_next_check_together(self, session, graph):
        """The guarantee that makes the pairing meaningful.

        The caller's transaction fails *after* a successful advance. Neither the new
        check time nor the action intent may survive: a surviving intent with no
        follow-up is an effect nobody will ever look at, and a surviving check time
        with no intent schedules a wake-up for work that was never recorded.
        """
        identity, record = await _seed(session, graph)
        await session.commit()

        async with AsyncSession(session.bind, expire_on_commit=False) as doomed:
            outcome = await advance_execution(
                session=doomed,
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
            # Whatever the caller was doing alongside this fails.
            await doomed.rollback()

        async with AsyncSession(session.bind, expire_on_commit=False) as fresh:
            row = await fresh.get(OrchestrationExecution, record.id)
            actions = (await fresh.execute(select(OrchestrationAction))).scalars().all()
        assert row.revision == 1, "the advance did not survive"
        assert row.phase == ExecutionPhase.ADMITTED.value
        assert row.pending_action_key is None
        assert actions == [], "the intent did not survive either"

    async def test_the_store_commits_nothing_on_its_own(self, session, graph):
        """The caller owns the transaction boundary — asserted, not assumed.

        If any method committed, the rollback test above would pass for the wrong
        reason and the atomicity guarantee would be unenforceable.
        """
        identity = _identity(graph)
        # The graph fixture's own transaction is settled first: StaticPool hands both
        # sessions the same connection, so a second one cannot open a transaction
        # while the first still holds one. Nothing about the store is under test here.
        await session.commit()

        async with AsyncSession(session.bind, expire_on_commit=False) as uncommitted:
            await create_execution(session=uncommitted, identity=identity, flow_id=graph["flow_a"])
            await prepare_action(session=uncommitted, identity=identity, intent=ActionIntent(operation_key="op", kind="k"))
            await uncommitted.rollback()

        async with AsyncSession(session.bind, expire_on_commit=False) as fresh:
            assert (await fresh.execute(select(OrchestrationExecution))).scalars().all() == []
            assert (await fresh.execute(select(OrchestrationAction))).scalars().all() == []


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


class TestBlockRecords:
    @pytest.mark.parametrize("code", list(BlockCode))
    async def test_every_declared_block_code_serializes(self, session, graph, code):
        """Each block code round-trips with its routing information intact.

        Parametrized over the enum so a member added later without storage support
        fails here. The columns carry code, owner, required input and remaining gates
        because an operator must be able to route the block from this row alone,
        without logs that expire.
        """
        identity, record = await _seed(session, graph)
        block = BlockRecord(
            code=code,
            owner="platform-operator",
            required_input="approve the deployment gate",
            remaining_gates=("gate:security-review", "gate:cost, and budget"),
            detail="waiting since the last pass",
        )

        outcome = await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.SETTLING,
                status=ExecutionStatus.RUNNABLE,  # overridden by the block
                expected_revision=record.revision,
                next_check_at=_soon(),
            ),
            block=block,
        )

        assert outcome.kind is OutcomeKind.BLOCKED
        assert outcome.reason == code.value
        # A block forces the status: a row that said runnable while carrying a block
        # code would be picked up and immediately re-blocked, forever.
        assert outcome.record.status is ExecutionStatus.BLOCKED

        stored = outcome.record.block
        assert stored.code is code
        assert stored.owner == "platform-operator"
        assert stored.required_input == "approve the deployment gate"
        # JSON, not a comma-join: a gate reference may itself contain a comma.
        assert stored.remaining_gates == ("gate:security-review", "gate:cost, and budget")
        assert stored.progressed_at is not None
        assert stored.detail == "waiting since the last pass"

        reloaded = await load_execution(session, identity=identity)
        assert reloaded.record.block.code is code
        assert reloaded.record.block.remaining_gates == block.remaining_gates

    async def test_blocking_does_not_reset_the_progress_clock(self, session, graph):
        """Becoming blocked is not progress.

        Overwriting `progressed_at` here would reset the very clock an operator uses
        to see how long this has been stuck.
        """
        identity, record = await _seed(session, graph)
        moved = await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.DELIVERING,
                status=ExecutionStatus.RUNNABLE,
                expected_revision=record.revision,
                next_check_at=_soon(),
            ),
        )
        progressed = moved.record.progressed_at

        blocked = await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.DELIVERING,
                status=ExecutionStatus.RUNNABLE,
                expected_revision=moved.record.revision,
                next_check_at=_soon(),
            ),
            block=BlockRecord(code=BlockCode.PROVIDER_UNAVAILABLE, owner="platform-operator", required_input="provider recovery"),
        )
        assert blocked.record.block.progressed_at == progressed

    async def test_clearing_a_block_clears_every_block_column(self, session, graph):
        """A stale owner or required_input left behind would describe a block that no
        longer exists, which is worse than no information at all."""
        identity, record = await _seed(session, graph)
        blocked = await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.DELIVERING,
                status=ExecutionStatus.RUNNABLE,
                expected_revision=record.revision,
                next_check_at=_soon(),
            ),
            block=BlockRecord(
                code=BlockCode.HUMAN_INPUT_REQUIRED,
                owner="requester",
                required_input="clarify the acceptance criteria",
                remaining_gates=("gate:design",),
                detail="asked in the issue thread",
            ),
        )
        unblocked = await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.DELIVERING,
                status=ExecutionStatus.RUNNABLE,
                expected_revision=blocked.record.revision,
                next_check_at=_soon(),
            ),
        )

        assert unblocked.record.block is None
        row = await session.get(OrchestrationExecution, record.id)
        assert row.block_code is None
        assert row.block_owner is None
        assert row.block_required_input is None
        assert row.block_remaining_gates is None
        assert row.block_detail is None

    @pytest.mark.parametrize("code", [BlockCode.ATTEMPTS_EXHAUSTED, BlockCode.HUMAN_REFUSED])
    async def test_exhaustion_and_refusal_are_recorded_not_resolved(self, session, graph, code):
        """These two route to the existing authorized recovery path.

        The store records the condition and stops. It does not retry, escalate, or
        approve anything — nothing here may manufacture a recovery a human owns.
        """
        identity, record = await _seed(session, graph)
        outcome = await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.SETTLING,
                status=ExecutionStatus.RUNNABLE,
                expected_revision=record.revision,
                next_check_at=_soon(),
            ),
            block=BlockRecord(code=code, owner="platform-operator", required_input="authorized recovery decision"),
        )
        assert outcome.record.status is ExecutionStatus.BLOCKED
        assert outcome.record.block.code is code
        # Not concluded, not superseded: the execution is still live and owned.
        assert outcome.record.status not in TERMINAL_EXECUTION_STATUSES
        assert outcome.record.attempts == record.attempts, "recording a block is not an attempt"

    async def test_an_unrecognised_stored_block_code_fails_closed(self, session, graph):
        """A newer writer's block code must not read as "not blocked".

        Treating it as unblocked would let an older pod resume work a newer one
        deliberately stopped, so the row stays blocked under a conservative code.
        """
        identity, record = await _seed(session, graph)
        row = await session.get(OrchestrationExecution, record.id)
        row.block_code = "a_code_from_the_future"
        row.status = ExecutionStatus.BLOCKED.value
        await session.flush()

        reloaded = await load_execution(session, identity=identity)
        assert reloaded.record.block is not None
        assert reloaded.record.block.code is BlockCode.AUTHORITY_UNVERIFIABLE
        assert reloaded.record.runnable is False


class TestMalformedStorage:
    async def test_unparseable_gate_list_reads_as_no_gates(self, session, graph):
        """A diagnostic field must not make the record unloadable.

        Raising here would turn a cosmetic storage problem into a failure to load the
        very row an operator is trying to diagnose.
        """
        identity, record = await _seed(session, graph)
        row = await session.get(OrchestrationExecution, record.id)
        row.block_code = BlockCode.DEPENDENCY_UNSATISFIED.value
        row.block_owner = "engine"
        row.block_required_input = "predecessor completion"
        row.block_remaining_gates = "{not json at all"
        await session.flush()

        reloaded = await load_execution(session, identity=identity)
        assert reloaded.record.block.remaining_gates == ()

    async def test_no_gates_is_stored_as_null_not_an_empty_list(self, session, graph):
        """So "no gates" and "gates not recorded" do not both read as `"[]"`."""
        identity, record = await _seed(session, graph)
        await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.SETTLING,
                status=ExecutionStatus.RUNNABLE,
                expected_revision=record.revision,
                next_check_at=_soon(),
            ),
            block=BlockRecord(code=BlockCode.BUDGET_EXHAUSTED, owner="platform-operator", required_input="budget increase"),
        )
        row = await session.get(OrchestrationExecution, record.id)
        assert row.block_remaining_gates is None

    @pytest.mark.parametrize(("column", "bad"), [("phase", "phase_from_the_future"), ("status", "status_from_the_future")])
    async def test_an_unrecognised_phase_or_status_is_a_typed_refusal(self, session, graph, column, bad):
        """Guessing a default would be a decision about live work made by a version
        mismatch, so the store refuses to project the row at all."""
        identity, record = await _seed(session, graph)
        row = await session.get(OrchestrationExecution, record.id)
        setattr(row, column, bad)
        await session.flush()

        with pytest.raises(ExecutionStoreError, match="does not recognise"):
            await load_execution(session, identity=identity)

    async def test_an_unrecognised_action_status_is_a_typed_refusal(self, session, graph):
        identity, _ = await _seed(session, graph)
        prepared = await prepare_action(session, identity=identity, intent=ActionIntent(operation_key="op", kind="k"))
        action = await session.get(OrchestrationAction, prepared.action.id)
        action.status = "status_from_the_future"
        await session.flush()

        with pytest.raises(ExecutionStoreError, match="does not recognise"):
            await prepare_action(session, identity=identity, intent=ActionIntent(operation_key="op", kind="k"))


# ---------------------------------------------------------------------------
# Storage hygiene and scope
# ---------------------------------------------------------------------------


class TestReferencesOnly:
    async def test_receipt_and_artifact_columns_hold_references(self, session, graph):
        """A shape assertion, not a content filter.

        These rows are read by operators, so a credential or a complete transcript
        here would be a disclosure with no revocation. The store's contract is that
        callers pass references; this pins the column widths that make a transcript
        physically implausible and documents the intent for reviewers.
        """
        artifact = OrchestrationAction.__table__.c.artifact_ref
        receipt = OrchestrationAction.__table__.c.receipt_ref
        assert artifact.type.length == 512
        assert receipt.type.length == 512
        for name in ("notification_receipt_ref", "handoff_receipt_ref", "pending_action_key"):
            assert OrchestrationExecution.__table__.c[name].type.length == 255

    async def test_receipt_references_round_trip_on_the_execution(self, session, graph):
        identity, record = await _seed(session, graph)
        outcome = await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.CONCLUDED,
                status=ExecutionStatus.CONCLUDED,
                expected_revision=record.revision,
            ),
            notification_receipt_ref="ses/0123456789abcdef",
            handoff_receipt_ref="issue-comment/5142#c1",
        )
        assert outcome.record.notification_receipt_ref == "ses/0123456789abcdef"
        assert outcome.record.handoff_receipt_ref == "issue-comment/5142#c1"


class TestOuterStatesUnchanged:
    """This issue adds a vocabulary; it must not disturb the existing one.

    The execution phases describe *delivery* inside one node. `NodeState` describes
    the node's place in the flow, including the human gate and the terminal states,
    and those remain the outer authority. A duplicated or altered `NodeState` would
    give the platform two disagreeing sources of truth about whether a human has
    approved something.
    """

    def test_node_state_members_are_untouched(self):
        assert [s.value for s in NodeState] == [
            "pending",
            "ready",
            "running",
            "awaiting_merge",
            "awaiting_gate",
            "passed",
            "rejected_at_gate",
            "failed",
            "halted",
            "superseded",
        ]

    def test_terminal_node_states_are_untouched(self):
        assert state_module.TERMINAL_STATES == frozenset(
            {NodeState.PASSED, NodeState.REJECTED_AT_GATE, NodeState.FAILED, NodeState.HALTED, NodeState.SUPERSEDED}
        )

    def test_the_human_only_gate_transitions_are_untouched(self):
        """The gate decision remains human-only, and this module adds no path to it."""
        gate = state_module.LEGAL_TRANSITIONS[NodeState.AWAITING_GATE]
        assert gate[NodeState.PASSED] == frozenset({state_module.ActorKind.HUMAN})
        assert gate[NodeState.REJECTED_AT_GATE] == frozenset({state_module.ActorKind.HUMAN})

    def test_the_two_vocabularies_are_declared_once_each(self):
        """R-N2a: `execution_state.py` declares the execution vocabulary and imports
        `NodeState` rather than redeclaring it."""
        from src.orchestration import execution_state

        assert execution_state.NodeState is NodeState
        assert not hasattr(execution_state, "TERMINAL_STATES")
        assert not hasattr(state_module, "ExecutionPhase")
        assert not hasattr(state_module, "ExecutionStatus")

    def test_the_store_touches_no_node_row(self, session, graph):
        """Scope boundary: graph rows are verified but never transitioned here."""
        source = (__import__("pathlib").Path(__file__).parents[2] / "src/orchestration/execution_store.py").read_text()
        assert "NodeState" not in source
        assert "node.state" not in source


class TestNoNetworkInsideTransactions:
    def test_the_store_imports_no_client_library(self):
        """A network call under a row lock would block every other writer on that
        execution for as long as the remote end takes to time out."""
        source = (__import__("pathlib").Path(__file__).parents[2] / "src/orchestration/execution_store.py").read_text()
        for forbidden in ("import boto3", "import httpx", "import requests", "aiohttp", "GithubAdapter"):
            assert forbidden not in source, f"{forbidden} must not be reachable from a transactional store"

    def test_no_method_commits(self):
        source = (__import__("pathlib").Path(__file__).parents[2] / "src/orchestration/execution_store.py").read_text()
        assert "session.commit()" not in source, "the caller owns the transaction boundary"


class TestPublishedFixtures:
    """The DTO fixtures #5145 reads and the adapter fixture #5143 runs against.

    Kept under test so a change to the store's contract breaks here rather than in a
    sibling issue's branch.
    """

    async def test_read_model_fixture_matches_what_the_store_produces(self, session, graph):
        from tests.orchestration.execution_ledger_fixtures import read_model_fixture

        fixture = read_model_fixture()
        identity, record = await _seed(session, graph)
        # Every field the fixture publishes exists on a real record, with the same
        # type — the failure this catches is #5145 building a view over a field name
        # that was never stored.
        for name, value in fixture.items():
            assert hasattr(record, name), f"read-model fixture exposes unknown field {name}"
            if value is not None and getattr(record, name) is not None:
                assert isinstance(getattr(record, name), type(value)) or isinstance(value, type(getattr(record, name)))

    async def test_adapter_fixture_mirrors_the_real_store_surface(self, session, graph):
        from tests.orchestration.execution_ledger_fixtures import SyntheticExecutionStore

        synthetic = SyntheticExecutionStore()
        identity = _identity(graph)
        real = await create_execution(session, identity=identity, flow_id=graph["flow_a"])
        fake = await synthetic.create_execution(None, identity=identity, flow_id=graph["flow_a"])

        assert fake.kind is real.kind
        assert fake.record.phase is real.record.phase
        assert fake.record.status is real.record.status
        assert fake.record.revision == real.record.revision

        # And the idempotency the runner depends on holds in the synthetic too.
        intent = ActionIntent(operation_key="pr:one", kind="open_pull_request")
        first = await synthetic.prepare_action(None, identity=identity, intent=intent)
        again = await synthetic.prepare_action(None, identity=identity, intent=intent)
        assert again.action.id == first.action.id
        assert again.reason == "action_already_prepared"


def test_json_gate_encoding_is_reversible():
    """Round-trip of the gate encoding on its own, including the comma case."""
    from src.orchestration.execution_store import _decode_gates, _encode_gates

    gates = ("gate:a", "gate:b, with a comma", "gate:c")
    assert _decode_gates(_encode_gates(gates)) == gates
    assert _encode_gates(()) is None
    assert _decode_gates(None) == ()
    # A JSON value that is valid but not a list is not a gate list.
    assert _decode_gates(json.dumps({"gate": "a"})) == ()


def test_uuid_ids_are_assigned_without_a_caller_supplying_them():
    """Ids come from the model default, so no caller can collide them."""
    for column in (OrchestrationExecution.__table__.c.id, OrchestrationAction.__table__.c.id):
        assert column.default is not None
        value = column.default.arg(None) if callable(column.default.arg) else column.default.arg
        uuid.UUID(str(value))
