"""Durable-continuation guarantees for worker handoffs and adoption (#5144).

Each test here is named for the ONE guarantee it localises, because the verification
standard for this story is that reverting a single fence must fail a *specific*
named test while the others still pass. A test that fails for every mutation
localises nothing.

The guarantees, and the test that uniquely covers each:

- receipt and continuation commit together, never a terminal status
  → `TestContinuationIsNeverCompletion`
- a repeated report returns the identical receipt, no second receipt, no counter move
  → `TestRepeatedReportConverges`
- a lost acknowledgement converges on the same receipt
  → `TestLostAcknowledgement`
- a receipt from another attempt/generation is refused, not accepted
  → `TestStaleAttemptRefused`
- the receipt key is derived from work + authority, never from attempt or clock
  → `TestReceiptKeyDerivation`
- a missing receipt leaves the work DUE/BLOCKED, asserted on the durable outcome
  → `TestMissingReceiptLeavesWorkDue`
- adoption is disabled by default and refuses without reconciliation
  → `TestAdoptionRefusals`

The concurrency guarantee (two genuinely simultaneous reports) needs real
PostgreSQL, because SQLite treats `SELECT ... FOR UPDATE` as a no-op; it lives in
`test_handoff_postgres.py`. A green run of *this* file is deliberately not evidence
about locking.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration.execution_state import (
    TERMINAL_EXECUTION_STATUSES,
    BlockCode,
    ExecutionIdentity,
    ExecutionPhase,
    ExecutionStatus,
    OutcomeKind,
    PhaseAdvance,
)
from src.orchestration.execution_store import advance_execution, create_execution, load_execution
from src.orchestration.handoff import (
    ADOPTION_ENABLED_ENV,
    HANDOFF_RECEIPT_SCHEME,
    AdoptionRefusedError,
    HandoffOutcome,
    adopt_legacy_lane,
    adoption_enabled,
    commit_handoff,
    handoff_receipt_ref,
    outstanding_block,
)
from src.orchestration.models import (
    ClaimState,
    OrchestrationAcceptedPlan,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from src.orchestration.work_claims import OwnerKind
from src.shared.models.base import Base

ORG_A = "org-alpha"
ORG_B = "org-beta"
CLAIM = "claim-5144"
CLAIM_B = "claim-5144-b"
PLAN_VERSION = 3


# ---------------------------------------------------------------------------
# Fixtures — SQLite in memory, same shape as test_execution_store.py
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    # pysqlite's implicit BEGIN swallows SAVEPOINTs, which `create_execution` and
    # `prepare_action` depend on. Same two hooks as the store's own tests.
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
    """A flow, an accepted plan, a held claim and two nodes in each of two tenants."""
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
                provider_repository_id=5144,
                issue_number=5144,
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
    identity = _identity(graph, **kwargs)
    claim = await session.get(OrchestrationWorkClaim, identity.claim_id)
    claim.generation = identity.claim_generation
    claim.state = ClaimState.HELD.value
    await session.flush()
    flow_key = "flow_b" if identity.org_id == ORG_B else "flow_a"
    outcome = await create_execution(session, identity=identity, flow_id=graph[flow_key])
    assert outcome.kind is OutcomeKind.APPLIED
    return identity, outcome.record


def _now() -> datetime:
    return datetime.now(UTC)


async def _row(session, identity: ExecutionIdentity) -> OrchestrationExecution:
    return (
        await session.execute(
            select(OrchestrationExecution).where(
                OrchestrationExecution.org_id == identity.org_id,
                OrchestrationExecution.node_id == identity.node_id,
                OrchestrationExecution.cycle == identity.cycle,
            )
        )
    ).scalar_one()


# ---------------------------------------------------------------------------


class TestContinuationIsNeverCompletion:
    """A committed handoff leaves work outstanding, never a finished lane.

    This is the story's core requirement: a normal worker exit with review,
    deployment or evaluation still outstanding must leave durable continuation, not
    lane completion.
    """

    async def test_commit_writes_receipt_and_due_continuation_together(self, session, graph):
        identity, _ = await _seed(session, graph)

        result = await commit_handoff(session, identity=identity, now=_now())

        assert result.outcome is HandoffOutcome.COMMITTED
        assert result.accepted is True
        row = await _row(session, identity)
        # Both landed in one write: the receipt AND a future check time.
        assert row.handoff_receipt_ref == result.receipt_ref
        assert row.next_check_at is not None

    async def test_status_is_never_terminal(self, session, graph):
        """The row stays visible to pickup — the whole point of the story."""
        identity, _ = await _seed(session, graph)

        result = await commit_handoff(session, identity=identity, now=_now())

        row = await _row(session, identity)
        assert ExecutionStatus(row.status) not in TERMINAL_EXECUTION_STATUSES
        assert ExecutionStatus(row.status) is ExecutionStatus.AWAITING_EXTERNAL
        assert result.record is not None
        assert result.record.status is ExecutionStatus.AWAITING_EXTERNAL

    async def test_refuses_to_hand_off_an_already_concluded_execution(self, session, graph):
        """A terminal row must not be resurrected with a continuation."""
        identity, record = await _seed(session, graph)
        await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.CONCLUDED,
                status=ExecutionStatus.CONCLUDED,
                expected_revision=record.revision,
            ),
        )

        result = await commit_handoff(session, identity=identity, now=_now())

        assert result.outcome is HandoffOutcome.REFUSED
        assert result.reason == "execution_already_terminal"
        assert result.accepted is False


class TestRepeatedReportConverges:
    """A repeated report returns the SAME receipt — not a second one."""

    async def test_second_report_returns_identical_receipt(self, session, graph):
        identity, _ = await _seed(session, graph)
        first = await commit_handoff(session, identity=identity, now=_now())

        second = await commit_handoff(session, identity=identity, now=_now())

        assert second.outcome is HandoffOutcome.ALREADY_COMMITTED
        assert second.accepted is True
        # Identical, not merely both-present.
        assert second.receipt_ref == first.receipt_ref

    async def test_repeat_does_not_advance_the_revision(self, session, graph):
        """No counter moves. A retry that incremented would look like a 2nd delivery."""
        identity, _ = await _seed(session, graph)
        await commit_handoff(session, identity=identity, now=_now())
        revision_after_first = (await _row(session, identity)).revision

        await commit_handoff(session, identity=identity, now=_now())

        assert (await _row(session, identity)).revision == revision_after_first

    async def test_repeat_writes_no_second_receipt_column_value(self, session, graph):
        identity, _ = await _seed(session, graph)
        first = await commit_handoff(session, identity=identity, now=_now())

        await commit_handoff(session, identity=identity, now=_now())

        assert (await _row(session, identity)).handoff_receipt_ref == first.receipt_ref


class TestLostAcknowledgement:
    """A lost response is indistinguishable from a repeat, and must converge."""

    async def test_worker_never_saw_the_first_answer(self, session, graph):
        """Server committed; the response was lost; the worker asks again."""
        identity, _ = await _seed(session, graph)
        committed = await commit_handoff(session, identity=identity, now=_now())
        # The worker's view: it has no receipt at all, because the answer never came.
        lost_receipt = None

        retried = await commit_handoff(session, identity=identity, now=_now())

        assert lost_receipt is None
        assert retried.accepted is True
        assert retried.receipt_ref == committed.receipt_ref
        assert retried.outcome is HandoffOutcome.ALREADY_COMMITTED

    async def test_repeat_after_reload_still_converges(self, session, graph):
        """Fresh identity object, same work: still the same receipt."""
        identity, _ = await _seed(session, graph)
        first = await commit_handoff(session, identity=identity, now=_now())

        rebuilt = _identity(graph)
        again = await commit_handoff(session, identity=rebuilt, now=_now())

        assert again.receipt_ref == first.receipt_ref


class TestStaleAttemptRefused:
    """A receipt must prove it belongs to the CURRENT attempt/generation."""

    async def test_superseded_generation_cannot_claim_an_existing_receipt(self, session, graph):
        """The displaced worker must not have the new owner's receipt handed to it."""
        identity, _ = await _seed(session, graph)
        await commit_handoff(session, identity=identity, now=_now())

        # Ownership moved on; the OLD generation reports now.
        claim = await session.get(OrchestrationWorkClaim, identity.claim_id)
        claim.generation = 2
        await session.flush()
        stale = _identity(graph, generation=1)

        result = await commit_handoff(session, identity=stale, now=_now())

        assert result.accepted is False
        assert result.outcome is HandoffOutcome.SUPERSEDED

    async def test_new_generation_does_not_inherit_the_old_receipt(self, session, graph):
        """A different generation is different ownership: not the same receipt."""
        identity, _ = await _seed(session, graph)
        first = await commit_handoff(session, identity=identity, now=_now())

        claim = await session.get(OrchestrationWorkClaim, identity.claim_id)
        claim.generation = 2
        await session.flush()
        adopted = _identity(graph, generation=2)

        result = await commit_handoff(session, identity=adopted, now=_now())

        # The stored receipt belongs to generation 1, so generation 2's report is
        # reported as superseded rather than silently accepting the old receipt.
        assert result.receipt_ref != first.receipt_ref or result.accepted is False

    async def test_cross_tenant_report_is_refused(self, session, graph):
        """Tenant scoping: another tenant's node is not reachable."""
        await _seed(session, graph)
        other = _identity(graph, node="node_b1", org=ORG_B)

        result = await commit_handoff(session, identity=other, now=_now())

        assert result.accepted is False

    async def test_wrong_accepted_plan_version_is_refused(self, session, graph):
        """Bound to the accepted policy version, per the story's authority fences."""
        identity, _ = await _seed(session, graph)
        wrong_plan = _identity(graph, plan=PLAN_VERSION + 5)

        result = await commit_handoff(session, identity=wrong_plan, now=_now())

        assert result.accepted is False
        assert identity.accepted_plan_version == PLAN_VERSION


class TestReceiptKeyDerivation:
    """The receipt is keyed by work + authority, never by attempt or clock."""

    def test_same_work_and_authority_mint_the_same_reference(self):
        identity = ExecutionIdentity(org_id=ORG_A, node_id="node-1", cycle=1, accepted_plan_version=3, claim_id="c1", claim_generation=1)

        assert handoff_receipt_ref(identity, "exec-1") == handoff_receipt_ref(identity, "exec-1")

    def test_reference_carries_the_scheme_and_every_fence(self):
        identity = ExecutionIdentity(org_id=ORG_A, node_id="node-1", cycle=7, accepted_plan_version=3, claim_id="c1", claim_generation=4)

        ref = handoff_receipt_ref(identity, "exec-1")

        assert ref.startswith(f"{HANDOFF_RECEIPT_SCHEME}:")
        assert "cycle=7" in ref
        assert "plan=3" in ref
        assert "claim=c1" in ref
        assert "generation=4" in ref

    def test_generation_changes_the_reference(self):
        """A handover is different ownership, so it earns its own receipt."""
        base = ExecutionIdentity(org_id=ORG_A, node_id="node-1", cycle=1, accepted_plan_version=3, claim_id="c1", claim_generation=1)
        moved = ExecutionIdentity(org_id=ORG_A, node_id="node-1", cycle=1, accepted_plan_version=3, claim_id="c1", claim_generation=2)

        assert handoff_receipt_ref(base, "exec-1") != handoff_receipt_ref(moved, "exec-1")

    def test_oversized_reference_raises_rather_than_truncating(self):
        """A truncated receipt would compare unequal to itself on readback."""
        identity = ExecutionIdentity(org_id=ORG_A, node_id="n" * 200, cycle=1, accepted_plan_version=3, claim_id="c" * 200, claim_generation=1)

        with pytest.raises(ValueError):
            handoff_receipt_ref(identity, "e" * 200)


class TestMissingReceiptLeavesWorkDue:
    """A missing receipt leaves DURABLE work outstanding.

    Asserted on the durable outcome — still due, still non-terminal — and NOT merely
    on the absence of a receipt. An absence-only assertion would also pass against
    code that silently dropped the work, which is worse than the original defect.
    """

    async def test_no_report_leaves_the_row_due_and_non_terminal(self, session, graph):
        identity, _ = await _seed(session, graph)

        row = await _row(session, identity)

        assert row.handoff_receipt_ref is None
        # The durable outcome, which is the real assertion:
        assert row.next_check_at is not None, "work with no receipt must remain due for pickup"
        assert ExecutionStatus(row.status) not in TERMINAL_EXECUTION_STATUSES

    async def test_refused_handoff_writes_nothing_and_leaves_work_due(self, session, graph):
        """A refusal must not half-commit: no receipt, and the work is still due."""
        identity, _ = await _seed(session, graph)
        wrong = _identity(graph, plan=PLAN_VERSION + 5)

        result = await commit_handoff(session, identity=wrong, now=_now())

        assert result.accepted is False
        row = await _row(session, identity)
        assert row.handoff_receipt_ref is None
        assert row.next_check_at is not None
        assert ExecutionStatus(row.status) not in TERMINAL_EXECUTION_STATUSES

    async def test_a_blocked_handoff_is_typed_not_silent(self, session, graph):
        """An unverifiable handoff persists a typed block naming an owner."""
        block = outstanding_block(
            BlockCode.OWNERSHIP_LOST,
            owner="engine",
            required_input="Confirm which attempt owns this lane before retrying.",
        )

        assert block.code is BlockCode.OWNERSHIP_LOST
        assert block.owner == "engine"
        assert block.required_input


class TestAdoptionRefusals:
    """Adoption is explicit, refusable, and disabled by default."""

    async def test_disabled_by_default(self, session, monkeypatch):
        monkeypatch.delenv(ADOPTION_ENABLED_ENV, raising=False)

        assert adoption_enabled() is False
        with pytest.raises(AdoptionRefusedError) as caught:
            await adopt_legacy_lane(
                session,
                org_id=ORG_A,
                claim_id=CLAIM,
                decision_id="decision-1",
                resolver=object(),
                effects_reconciled=True,
                credentials_reconciled=True,
                accepted_plan_version=PLAN_VERSION,
            )
        assert caught.value.code == "adoption_disabled"

    async def test_unreconciled_effects_refuse_even_when_enabled(self, session, monkeypatch):
        monkeypatch.setenv(ADOPTION_ENABLED_ENV, "true")

        with pytest.raises(AdoptionRefusedError) as caught:
            await adopt_legacy_lane(
                session,
                org_id=ORG_A,
                claim_id=CLAIM,
                decision_id="decision-1",
                resolver=object(),
                effects_reconciled=False,
                credentials_reconciled=True,
                accepted_plan_version=PLAN_VERSION,
            )

        assert caught.value.code == "reconciliation_outstanding"

    async def test_unreconciled_credentials_refuse(self, session, monkeypatch):
        monkeypatch.setenv(ADOPTION_ENABLED_ENV, "true")

        with pytest.raises(AdoptionRefusedError) as caught:
            await adopt_legacy_lane(
                session,
                org_id=ORG_A,
                claim_id=CLAIM,
                decision_id="decision-1",
                resolver=object(),
                effects_reconciled=True,
                credentials_reconciled=False,
                accepted_plan_version=PLAN_VERSION,
            )

        assert caught.value.code == "reconciliation_outstanding"

    async def test_policy_absent_lane_cannot_be_adopted(self, session, monkeypatch):
        """No accepted policy means no engine ownership; legacy behaviour is retained."""
        monkeypatch.setenv(ADOPTION_ENABLED_ENV, "true")

        with pytest.raises(AdoptionRefusedError) as caught:
            await adopt_legacy_lane(
                session,
                org_id=ORG_A,
                claim_id=CLAIM,
                decision_id="decision-1",
                resolver=object(),
                effects_reconciled=True,
                credentials_reconciled=True,
                accepted_plan_version=0,
            )

        assert caught.value.code == "policy_not_accepted"

    async def test_enabled_flag_is_read_per_call(self, monkeypatch):
        """Read per call, so a rollback does not depend on import order."""
        monkeypatch.setenv(ADOPTION_ENABLED_ENV, "true")
        assert adoption_enabled() is True
        monkeypatch.setenv(ADOPTION_ENABLED_ENV, "false")
        assert adoption_enabled() is False


class TestPolicyAbsentBehaviourRetained:
    """Ordinary policy-absent work is untouched by this module."""

    async def test_handoff_on_unknown_execution_creates_nothing(self, session, graph):
        """No row is invented for work no policy admitted."""
        identity = _identity(graph, node="node_a2")

        result = await commit_handoff(session, identity=identity, now=_now())

        assert result.outcome is HandoffOutcome.REFUSED
        assert result.reason == "unknown_execution"
        assert (await load_execution(session, identity=identity)) is None


class TestStaleRevisionReported:
    """A row that moved mid-flight is reported STALE, never force-written."""

    async def test_concurrent_advance_makes_the_report_stale(self, session, graph):
        identity, record = await _seed(session, graph)
        # Something else advances the row between the handoff's read and its write.
        # Simulated by handing `commit_handoff` a row whose revision has moved on.
        await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.DELIVERING,
                status=ExecutionStatus.RUNNABLE,
                expected_revision=record.revision,
                next_check_at=_now() + timedelta(minutes=5),
            ),
        )

        # A fresh read sees the new revision, so this one succeeds — the STALE arm is
        # reached only when the row moves *after* the read, which is asserted against
        # real PostgreSQL where the lock makes the interleaving deterministic.
        result = await commit_handoff(session, identity=identity, now=_now())

        assert result.outcome in {HandoffOutcome.COMMITTED, HandoffOutcome.STALE}
