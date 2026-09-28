"""Tests for deviation detection (issue #4204, R-O5f).

**AC-13 is tested in both directions, and the negative direction is not optional.**
A run with no dispatching node must be flagged, and a legitimately dispatched run
must NOT be. Only testing the first would leave a detector that flags everything
passing its own suite while being useless in production — a detector that cries
wolf gets muted, and a muted detector is a false negative with extra steps.

The observation side reads `usage_logs.graph_address`, so these tests write ledger
rows rather than mocking a reconciliation input. Mocking the observation would test
the loop and skip the query, and the query is where the org filter, the NULL
handling and the address composition live.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration.deviation import (
    DISPATCHED_STATES,
    DeviationKind,
    detect_deviations,
    record_deviations,
)
from src.orchestration.models import DecisionKind, OrchestrationDecision, OrchestrationFlow, OrchestrationNode
from src.orchestration.state import ActorKind, NodeState
from src.shared.models.base import Base
from src.shared.models.usage import UsageLog

ORG_A = "org-alpha"
ORG_B = "org-beta"


@pytest.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

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


async def _make_flow(session: AsyncSession, *, org_id: str = ORG_A, slug: str = "flow-1") -> OrchestrationFlow:
    flow = OrchestrationFlow(org_id=org_id, slug=slug, title="Demo flow")
    session.add(flow)
    await session.flush()
    return flow


async def _make_node(
    session: AsyncSession,
    flow: OrchestrationFlow,
    *,
    node_ref: str = "s7",
    state: NodeState | str = NodeState.READY,
    org_id: str | None = None,
) -> OrchestrationNode:
    node = OrchestrationNode(
        org_id=org_id or flow.org_id,
        flow_id=flow.id,
        epic_ref="4191",
        wave_ref="wave-4",
        node_ref=node_ref,
        kind="story",
        title=f"Node {node_ref}",
        state=state.value if isinstance(state, NodeState) else state,
    )
    session.add(node)
    await session.flush()
    return node


async def _log_call(
    session: AsyncSession,
    *,
    org_id: str = ORG_A,
    graph_address: str | None = "flow-1/4191/wave-4/s7",
    count: int = 1,
) -> None:
    """Write `count` ledger rows at one address — one row per model call."""
    for _ in range(count):
        session.add(
            UsageLog(
                org_id=org_id,
                department_id="dept-1",
                team_id="team-1",
                user_id="agent-worker",
                account_type="service",
                model="claude-sonnet-4",
                input_tokens=100,
                output_tokens=50,
                cost_usd=Decimal("0.001"),
                latency_ms=800,
                status_code=200,
                graph_address=graph_address,
            )
        )
    await session.flush()


class TestDispatchedRunsAreNotFlagged:
    """AC-13, negative direction — the half that keeps the detector usable."""

    @pytest.mark.parametrize("state", sorted(DISPATCHED_STATES, key=lambda s: s.value), ids=lambda s: s.value)
    async def test_a_run_against_a_dispatched_node_is_not_a_deviation(self, session, state):
        """Every post-dispatch state is legitimate attribution, not a deviation.

        Parametrized over the whole set rather than spot-checking `running`,
        because a node that has since passed, failed or been halted still WAS
        dispatched, and flagging its historical ledger rows would make every
        completed flow generate permanent false positives.
        """
        flow = await _make_flow(session)
        await _make_node(session, flow, state=state)
        await _log_call(session)

        report = await detect_deviations(session, org_id=ORG_A)

        assert report.deviations == ()
        assert report.has_deviations is False
        assert report.addresses_reconciled == 1

    async def test_clean_reconciliation_is_distinguishable_from_no_activity(self, session):
        """An empty deviation list means two very different things.

        "Everything reconciled" and "nothing was observed" look identical in a
        bare empty list, and conflating them is how a detector that has gone
        blind reads as a clean bill of health.
        """
        flow = await _make_flow(session)
        await _make_node(session, flow, state=NodeState.RUNNING)
        await _log_call(session)

        clean = await detect_deviations(session, org_id=ORG_A)
        silent = await detect_deviations(session, org_id="org-with-nothing")

        assert clean.deviations == () and silent.deviations == ()
        assert clean.addresses_observed == 1
        assert silent.addresses_observed == 0

    async def test_calls_with_no_graph_address_are_ignored(self, session):
        """A NULL address means "not attributable to a node", not "off-graph".

        Every pre-feature row and every non-gateway Bedrock path has one, so
        flagging them would bury the real signal in historical noise.
        """
        await _make_flow(session)
        await _log_call(session, graph_address=None, count=5)

        report = await detect_deviations(session, org_id=ORG_A)

        assert report.deviations == ()
        assert report.addresses_observed == 0


class TestUndispatchedRunsAreFlagged:
    """AC-13, positive direction — off-graph work becomes visible."""

    @pytest.mark.parametrize("state", [NodeState.PENDING, NodeState.READY], ids=["pending", "ready"])
    async def test_a_run_against_a_node_the_engine_never_started_is_flagged(self, session, state):
        """The "agent worked ahead of the plan" shape.

        PENDING and READY are the states that make this test matter: the node
        EXISTS, so an existence-based check would pass it. Only a state-based
        check notices that the engine never dispatched it.
        """
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=state)
        await _log_call(session, count=3)

        report = await detect_deviations(session, org_id=ORG_A)

        assert report.has_deviations is True
        (deviation,) = report.deviations
        assert deviation.kind is DeviationKind.NODE_NOT_DISPATCHED
        assert deviation.node_id == node.id
        assert deviation.observed_node_state == state.value
        assert deviation.call_count == 3

    async def test_a_run_against_an_address_with_no_node_is_flagged(self, session):
        """A fabricated or stale address — a different diagnosis, so a different kind."""
        await _make_flow(session)
        await _log_call(session, graph_address="flow-1/9999/wave-9/ghost")

        report = await detect_deviations(session, org_id=ORG_A)

        (deviation,) = report.deviations
        assert deviation.kind is DeviationKind.NO_SUCH_NODE
        assert deviation.node_id is None
        assert deviation.observed_node_state is None

    async def test_the_two_kinds_are_reported_separately(self, session):
        """One "unknown run" bucket would hide that these need different responses."""
        flow = await _make_flow(session)
        await _make_node(session, flow, node_ref="s7", state=NodeState.PENDING)
        await _log_call(session, graph_address="flow-1/4191/wave-4/s7")
        await _log_call(session, graph_address="flow-1/4191/wave-4/ghost")

        report = await detect_deviations(session, org_id=ORG_A)

        assert {d.kind for d in report.deviations} == {
            DeviationKind.NODE_NOT_DISPATCHED,
            DeviationKind.NO_SUCH_NODE,
        }

    async def test_call_count_is_carried_because_scale_is_diagnostic(self, session):
        """One stray call and a running agent need different urgency."""
        await _make_flow(session)
        await _log_call(session, graph_address="flow-1/4191/wave-4/ghost", count=42)

        report = await detect_deviations(session, org_id=ORG_A)

        assert report.deviations[0].call_count == 42

    async def test_an_undeclared_node_state_counts_as_a_deviation(self, session):
        """Fail-closed: "cannot be shown to have been dispatched" is not a pass.

        Treating an unreadable state as authorisation is the fail-open reading,
        and it would let a corrupted or future-versioned row launder off-graph work.
        """
        flow = await _make_flow(session)
        await _make_node(session, flow, state="some_future_state")
        await _log_call(session)

        report = await detect_deviations(session, org_id=ORG_A)

        (deviation,) = report.deviations
        assert deviation.kind is DeviationKind.NODE_NOT_DISPATCHED
        assert deviation.observed_node_state == "some_future_state"

    async def test_deviation_detail_reads_as_a_sentence(self, session):
        """R-O5f: deviations are rendered, so each carries its own explanation."""
        flow = await _make_flow(session)
        await _make_node(session, flow, state=NodeState.PENDING)
        await _log_call(session, count=2)

        report = await detect_deviations(session, org_id=ORG_A)

        detail = report.deviations[0].detail
        assert "2 model call(s)" in detail
        assert "flow-1/4191/wave-4/s7" in detail
        assert "never dispatched" in detail


class TestDetectionIsReadOnly:
    async def test_detect_writes_no_decision_rows(self, session):
        """Recording is a separate explicit step.

        A detector that wrote on read would append a duplicate row every time
        somebody opened the page.
        """
        flow = await _make_flow(session)
        await _make_node(session, flow, state=NodeState.PENDING)
        await _log_call(session)

        await detect_deviations(session, org_id=ORG_A)

        rows = (await session.execute(select(OrchestrationDecision))).scalars().all()
        assert list(rows) == []

    async def test_detect_does_not_change_node_state(self, session):
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.PENDING)
        await _log_call(session)

        await detect_deviations(session, org_id=ORG_A)

        state = (await session.execute(select(OrchestrationNode.state).where(OrchestrationNode.id == node.id))).scalar_one()
        assert state == NodeState.PENDING.value


class TestTenantIsolation:
    async def test_another_orgs_ledger_rows_are_not_reconciled(self, session):
        flow = await _make_flow(session)
        await _make_node(session, flow, state=NodeState.PENDING)
        await _log_call(session, org_id=ORG_B)

        report = await detect_deviations(session, org_id=ORG_A)

        assert report.addresses_observed == 0
        assert report.deviations == ()

    async def test_another_orgs_dispatched_node_does_not_excuse_a_run(self, session):
        """The dangerous direction: org B's node must not authorise org A's run.

        Both orgs use the same address here. If the node index were not
        org-filtered, B's `running` node would silently reconcile A's off-graph
        activity — a cross-tenant hole that reads as a clean report.
        """
        flow_b = await _make_flow(session, org_id=ORG_B, slug="flow-1")
        await _make_node(session, flow_b, state=NodeState.RUNNING, org_id=ORG_B)
        await _log_call(session, org_id=ORG_A)

        report = await detect_deviations(session, org_id=ORG_A)

        assert report.has_deviations is True
        assert report.deviations[0].kind is DeviationKind.NO_SUCH_NODE

    async def test_report_is_stamped_with_the_org_it_reconciled(self, session):
        await _make_flow(session)
        report = await detect_deviations(session, org_id=ORG_A)
        assert report.org_id == ORG_A


class TestFlowScoping:
    async def test_scoping_to_a_flow_excludes_other_flows(self, session):
        flow_1 = await _make_flow(session, slug="flow-1")
        flow_2 = await _make_flow(session, slug="flow-2")
        await _make_node(session, flow_1, state=NodeState.PENDING)
        await _make_node(session, flow_2, state=NodeState.PENDING)
        await _log_call(session, graph_address="flow-1/4191/wave-4/s7")
        await _log_call(session, graph_address="flow-2/4191/wave-4/s7")

        report = await detect_deviations(session, org_id=ORG_A, flow_slug="flow-1")

        assert len(report.deviations) == 1
        assert report.deviations[0].graph_address.startswith("flow-1/")

    async def test_a_slug_prefix_does_not_match_a_longer_slug(self, session):
        """`flow-1` must not pull in `flow-10` — the trailing `/` is what stops it."""
        flow_10 = await _make_flow(session, slug="flow-10")
        await _make_node(session, flow_10, state=NodeState.PENDING)
        await _log_call(session, graph_address="flow-10/4191/wave-4/s7")

        report = await detect_deviations(session, org_id=ORG_A, flow_slug="flow-1")

        assert report.addresses_observed == 0

    async def test_like_metacharacters_in_a_slug_do_not_widen_the_scope(self, session):
        """An unescaped `%` would match past its own subtree into other flows."""
        await _make_flow(session, slug="flow-1")
        await _log_call(session, graph_address="flow-1/4191/wave-4/s7")

        report = await detect_deviations(session, org_id=ORG_A, flow_slug="%")

        assert report.addresses_observed == 0, "'%' must be a literal slug, not a wildcard"


class TestRecordingDeviations:
    async def test_a_deviation_is_appended_as_a_rejection_row(self, session):
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.PENDING)
        await _log_call(session)
        report = await detect_deviations(session, org_id=ORG_A)

        appended = await record_deviations(session, report)

        assert appended == 1
        (row,) = (await session.execute(select(OrchestrationDecision))).scalars().all()
        assert row.kind == DecisionKind.TRANSITION_REJECTED.value
        assert row.node_id == node.id
        assert row.rejection_reason

    async def test_the_detector_records_itself_as_a_service_actor(self, session):
        """SERVICE describes who DETECTED it, and says nothing about who caused it.

        Who caused the deviation is precisely what is unknown — which is why the
        row exists to be investigated.
        """
        flow = await _make_flow(session)
        await _make_node(session, flow, state=NodeState.PENDING)
        await _log_call(session)
        report = await detect_deviations(session, org_id=ORG_A)

        await record_deviations(session, report)

        (row,) = (await session.execute(select(OrchestrationDecision))).scalars().all()
        assert row.actor_kind == ActorKind.SERVICE.value

    async def test_a_no_such_node_deviation_is_reported_but_not_persisted(self, session):
        """The named gap: `orchestration_decisions.flow_id` is a non-nullable FK.

        A NO_SUCH_NODE deviation has no flow to attach to by definition, so it is
        returned and logged but not written. Asserted rather than left implicit so
        the limitation cannot be mistaken for "nothing was detected" — closing it
        needs a flow-independent table, which is a migration this story does not
        ship.
        """
        await _make_flow(session)
        await _log_call(session, graph_address="flow-1/4191/wave-4/ghost")
        report = await detect_deviations(session, org_id=ORG_A)

        appended = await record_deviations(session, report)

        assert report.has_deviations is True, "still detected and returned"
        assert appended == 0, "but not persistable"
        assert list((await session.execute(select(OrchestrationDecision))).scalars().all()) == []

    async def test_recording_a_clean_report_writes_nothing(self, session):
        flow = await _make_flow(session)
        await _make_node(session, flow, state=NodeState.RUNNING)
        await _log_call(session)
        report = await detect_deviations(session, org_id=ORG_A)

        appended = await record_deviations(session, report)

        assert appended == 0

    async def test_recorded_rows_are_append_only(self, session):
        """The store story's guarantee still holds for deviation rows."""
        from src.orchestration.models import AppendOnlyViolationError

        flow = await _make_flow(session)
        await _make_node(session, flow, state=NodeState.PENDING)
        await _log_call(session)
        report = await detect_deviations(session, org_id=ORG_A)
        await record_deviations(session, report)

        (row,) = (await session.execute(select(OrchestrationDecision))).scalars().all()
        row.rejection_reason = "actually it was fine"
        with pytest.raises(AppendOnlyViolationError):
            await session.flush()
