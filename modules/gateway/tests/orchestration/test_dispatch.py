"""Tests for engine dispatch (issue #4204).

Covers the issue's Validation section: idempotency (R-NF2 — the same ready node
dispatched twice produces ONE run), rejection-and-recording for a red predecessor
(AC-14) and a gate skip (AC-15), tenant isolation, and the source-level no-bypass
assertion that mirrors `test_tick.py`'s.

AC-30's genesis refusals live in `test_genesis.py`; what is tested here is the
consequence — a refused genesis means `dispatch_node` is never reached, and a
rejected transition means no run exists.

"Two concurrent dispatches" is modelled as **two attempts that both observed the
same prior state**, which is exactly what overlapping ticks produce, rather than
as two OS threads whose interleaving would be non-deterministic and whose failures
would be flaky rather than informative. Same reasoning as `test_tick.py`.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration import dispatch as dispatch_module
from src.orchestration.dispatch import (
    DispatchStatus,
    dispatch_node,
    graph_address,
)
from src.orchestration.genesis import EngineGenesis, resolve_engine_genesis
from src.orchestration.models import (
    DecisionKind,
    OrchestrationDecision,
    OrchestrationFlow,
    OrchestrationNode,
)
from src.orchestration.state import ActorKind, NodeState
from src.shared.models.base import Base

ORG_A = "org-alpha"
ORG_B = "org-beta"
APPROVER = "cognito-sub-alice"


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
    kind: str = "story",
    org_id: str | None = None,
) -> OrchestrationNode:
    node = OrchestrationNode(
        org_id=org_id or flow.org_id,
        flow_id=flow.id,
        epic_ref="4191",
        wave_ref="wave-4",
        node_ref=node_ref,
        kind=kind,
        title=f"Node {node_ref}",
        state=state.value if isinstance(state, NodeState) else state,
    )
    session.add(node)
    await session.flush()
    return node


async def _genesis_for(session: AsyncSession, flow: OrchestrationFlow, *, org_id: str | None = None) -> EngineGenesis:
    """A real resolved genesis — built by resolving a real approval row.

    Deliberately NOT constructed directly: the tests exercise the same object the
    production path produces, so a change to the resolver cannot leave these
    tests passing against a shape that no longer exists.
    """
    decision = OrchestrationDecision(
        org_id=org_id or flow.org_id,
        flow_id=flow.id,
        kind=DecisionKind.GATE_APPROVED.value,
        actor_id=APPROVER,
        actor_role="org_admin",
        actor_kind=ActorKind.HUMAN.value,
        reason="approved at the wave gate",
    )
    session.add(decision)
    await session.flush()
    return await resolve_engine_genesis(session, org_id=org_id or flow.org_id, decision_id=decision.id)


async def _state_of(session: AsyncSession, node_id: str) -> str:
    return (await session.execute(select(OrchestrationNode.state).where(OrchestrationNode.id == node_id))).scalar_one()


async def _decisions(session: AsyncSession) -> list[OrchestrationDecision]:
    return list((await session.execute(select(OrchestrationDecision))).scalars().all())


class TestSuccessfulDispatch:
    """A ready node moves to running and a run is recorded against its address."""

    async def test_ready_node_is_dispatched(self, session):
        flow = await _make_flow(session)
        node = await _make_node(session, flow)
        genesis = await _genesis_for(session, flow)

        outcome = await dispatch_node(session, node, genesis)

        assert outcome.status is DispatchStatus.DISPATCHED
        assert outcome.dispatched is True
        assert await _state_of(session, node.id) == NodeState.RUNNING.value

    async def test_run_carries_the_graph_address(self, session):
        flow = await _make_flow(session)
        node = await _make_node(session, flow)
        genesis = await _genesis_for(session, flow)

        outcome = await dispatch_node(session, node, genesis)

        assert outcome.run is not None
        assert outcome.run.graph_address == "flow-1/4191/wave-4/s7"

    async def test_address_matches_the_cost_rollups_composition(self, session):
        """The ledger join key must agree with `cost.get_flow_cost`'s spelling.

        A second spelling here would make dispatched runs invisible to the cost
        rollup, and invisible-but-real is the failure mode the cost story exists
        to prevent.
        """
        flow = await _make_flow(session)
        node = await _make_node(session, flow)

        assert graph_address(node, flow_slug=flow.slug) == f"{flow.slug}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}"

    async def test_run_attributes_the_authorising_decision(self, session):
        """Audit trail: "who authorised this run?" answers with a row id."""
        flow = await _make_flow(session)
        node = await _make_node(session, flow)
        genesis = await _genesis_for(session, flow)

        outcome = await dispatch_node(session, node, genesis)

        assert outcome.run.root_decision_id == genesis.decision_id
        assert outcome.run.root_human_id == APPROVER

    async def test_dispatch_increments_the_attempt_counter(self, session):
        flow = await _make_flow(session)
        node = await _make_node(session, flow)
        genesis = await _genesis_for(session, flow)

        await dispatch_node(session, node, genesis)

        attempts = (await session.execute(select(OrchestrationNode.attempts).where(OrchestrationNode.id == node.id))).scalar_one()
        assert attempts == 1

    async def test_dispatch_by_node_id_works_the_same(self, session):
        """The node is re-resolved either way, so an id is as good as an instance."""
        flow = await _make_flow(session)
        node = await _make_node(session, flow)
        genesis = await _genesis_for(session, flow)

        outcome = await dispatch_node(session, node.id, genesis)

        assert outcome.status is DispatchStatus.DISPATCHED


class TestIdempotency:
    """R-NF2: the same ready node dispatched twice produces exactly ONE run."""

    async def test_dispatching_the_same_ready_node_twice_produces_one_run(self, session):
        flow = await _make_flow(session)
        node = await _make_node(session, flow)
        genesis = await _genesis_for(session, flow)

        first = await dispatch_node(session, node, genesis)
        second = await dispatch_node(session, node, genesis)

        assert first.status is DispatchStatus.DISPATCHED
        assert first.run is not None
        # The second attempt creates NO run — that is the whole guarantee.
        assert second.status is DispatchStatus.REJECTED
        assert second.run is None

    async def test_two_attempts_on_the_same_observed_state_yield_one_dispatch(self, session):
        """The concurrency shape: both attempts observed `ready`.

        This is what overlapping ticks actually produce. The conditional UPDATE
        means exactly one matches a row; the loser must create no run.
        """
        flow = await _make_flow(session)
        node = await _make_node(session, flow)
        genesis = await _genesis_for(session, flow)
        assert node.state == NodeState.READY.value

        first = await dispatch_node(session, node, genesis)

        # The second attempt presents the STALE observation — `ready` — which is
        # what a concurrent tick that read before the first write would present.
        # The observation is passed as an argument rather than by mutating an ORM
        # instance: assigning `state` on a persistent object is a WRITE that
        # autoflush would push back to the row, resetting it to `ready` and
        # quietly turning this into a test of nothing.
        second = await dispatch_module._dispatch_transition(
            session,
            node_id=node.id,
            org_id=ORG_A,
            flow_id=flow.id,
            observed_state=NodeState.READY.value,
            reason="concurrent attempt on a stale observation",
        )

        assert first.status is DispatchStatus.DISPATCHED
        rows, allowed, _ = second
        assert allowed is True, "transition() legitimately allows ready -> running"
        assert rows == 0, "but the conditional UPDATE must match no row — no second run"

    async def test_a_running_node_is_not_dispatched_again(self, session):
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.RUNNING)
        genesis = await _genesis_for(session, flow)

        outcome = await dispatch_node(session, node, genesis)

        assert outcome.status is DispatchStatus.REJECTED
        assert outcome.run is None

    async def test_losing_the_race_after_a_legal_read_reports_already_running(self, session, monkeypatch):
        """The genuine interleaving: `ready` when read, moved before the write.

        This is the one case the seam cannot be driven into from the outside —
        it needs the row to change between `dispatch_node`'s SELECT and its
        UPDATE, which a single-threaded test cannot produce naturally. The seam is
        stubbed to report what it reports in that situation (allowed, 0 rows) so
        the assertion is about `dispatch_node`'s *handling*: a lost race is
        ALREADY_RUNNING with no run, distinct from a REJECTED authority failure,
        because one is normal overlap to ignore and the other is a deviation to
        surface.
        """

        async def _lost_race(*_args, **_kwargs):
            return 0, True, None

        monkeypatch.setattr(dispatch_module, "_dispatch_transition", _lost_race)

        flow = await _make_flow(session)
        node = await _make_node(session, flow)
        genesis = await _genesis_for(session, flow)

        outcome = await dispatch_node(session, node, genesis)

        assert outcome.status is DispatchStatus.ALREADY_RUNNING
        assert outcome.run is None, "a lost race must create no second run (R-NF2)"
        assert outcome.reason


class TestIllegalDispatchIsRejectedAndRecorded:
    """AC-14 / AC-15: rejected by `transition()` AND the attempt recorded."""

    async def test_gate_skip_is_rejected(self, session):
        """AC-15: a SERVICE actor advancing out of `awaiting_gate` IS the gate skip."""
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.AWAITING_GATE, kind="gate")
        genesis = await _genesis_for(session, flow)

        outcome = await dispatch_node(session, node, genesis)

        assert outcome.status is DispatchStatus.REJECTED
        assert await _state_of(session, node.id) == NodeState.AWAITING_GATE.value

    async def test_gate_skip_attempt_is_recorded_as_a_decision(self, session):
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.AWAITING_GATE, kind="gate")
        genesis = await _genesis_for(session, flow)

        await dispatch_node(session, node, genesis)

        rejections = [d for d in await _decisions(session) if d.kind == DecisionKind.TRANSITION_REJECTED.value]
        assert len(rejections) == 1
        assert rejections[0].node_id == node.id
        assert rejections[0].rejection_reason, "a recorded rejection must say why — it is the deviation evidence"

    @pytest.mark.parametrize(
        "state",
        [NodeState.PENDING, NodeState.FAILED, NodeState.HALTED, NodeState.PASSED, NodeState.REJECTED_AT_GATE, NodeState.SUPERSEDED],
    )
    async def test_a_node_not_in_ready_is_never_dispatched(self, session, state):
        """AC-14 lands here: a red predecessor means the tick never released the
        node, so it is not in `ready` and dispatch must refuse it."""
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=state)
        genesis = await _genesis_for(session, flow)

        outcome = await dispatch_node(session, node, genesis)

        assert outcome.status is DispatchStatus.REJECTED
        assert outcome.run is None
        assert await _state_of(session, node.id) == state.value

    async def test_halted_node_is_not_resurrected_by_dispatch(self, session):
        """R-Q9c: the engine must never self-clear a halt, including via dispatch."""
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED)
        genesis = await _genesis_for(session, flow)

        outcome = await dispatch_node(session, node, genesis)

        assert outcome.status is DispatchStatus.REJECTED
        assert await _state_of(session, node.id) == NodeState.HALTED.value

    async def test_every_rejection_is_recorded_with_the_service_actor_kind(self, session):
        """The dispatch acts as SERVICE even though its authority is human.

        Recording HUMAN would claim a person pressed a button they did not press.
        """
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.PENDING)
        genesis = await _genesis_for(session, flow)

        await dispatch_node(session, node, genesis)

        rejections = [d for d in await _decisions(session) if d.kind == DecisionKind.TRANSITION_REJECTED.value]
        assert rejections[0].actor_kind == ActorKind.SERVICE.value


class TestTenantIsolation:
    async def test_a_node_from_another_org_is_not_found(self, session):
        flow_a = await _make_flow(session)
        flow_b = await _make_flow(session, org_id=ORG_B, slug="flow-b")
        node_b = await _make_node(session, flow_b, org_id=ORG_B)
        genesis_a = await _genesis_for(session, flow_a)

        outcome = await dispatch_node(session, node_b, genesis_a)

        assert outcome.status is DispatchStatus.NOT_FOUND
        assert outcome.run is None
        assert await _state_of(session, node_b.id) == NodeState.READY.value

    async def test_a_caller_supplied_org_on_the_instance_does_not_override_genesis(self, session):
        """The node's org is re-resolved, not read off the passed instance.

        A caller-supplied ORM object's `org_id` is a claim. Mutating it must not
        move the tenant the dispatch resolves in.
        """
        flow_a = await _make_flow(session)
        flow_b = await _make_flow(session, org_id=ORG_B, slug="flow-b")
        node_b = await _make_node(session, flow_b, org_id=ORG_B)
        genesis_a = await _genesis_for(session, flow_a)

        # Lie about the node's tenant on the in-memory instance.
        node_b.org_id = ORG_A

        outcome = await dispatch_node(session, node_b, genesis_a)

        assert outcome.status is DispatchStatus.NOT_FOUND, "the DB row's org, not the instance's, must decide"

    async def test_a_node_whose_flow_is_missing_is_refused(self, session):
        """Unaddressable work is not dispatched — nothing could attribute its cost."""
        flow = await _make_flow(session)
        node = await _make_node(session, flow)
        genesis = await _genesis_for(session, flow)

        # Point the node at a flow that does not exist.
        node.flow_id = "no-such-flow"
        await session.execute(OrchestrationNode.__table__.update().where(OrchestrationNode.id == node.id).values(flow_id="no-such-flow"))
        await session.flush()

        outcome = await dispatch_node(session, node.id, genesis)

        assert outcome.status is DispatchStatus.NOT_FOUND


class TestOutcomeInvariants:
    """The dataclass refuses to represent an impossible result."""

    def test_a_dispatched_outcome_must_carry_a_run(self):
        with pytest.raises(ValueError, match="must carry the run"):
            dispatch_module.DispatchOutcome(status=DispatchStatus.DISPATCHED, node_id="n1")

    @pytest.mark.parametrize(
        "status",
        [DispatchStatus.REJECTED, DispatchStatus.ALREADY_RUNNING, DispatchStatus.NOT_FOUND],
    )
    def test_a_non_dispatched_outcome_must_not_carry_a_run(self, status):
        run = dispatch_module.DispatchedRun(
            node_id="n1",
            org_id=ORG_A,
            flow_id="f1",
            graph_address="a/b/c/d",
            root_decision_id="d1",
            root_human_id=APPROVER,
        )
        with pytest.raises(ValueError, match="must not carry a run"):
            dispatch_module.DispatchOutcome(status=status, node_id="n1", run=run)


class TestNoBypassOfTheTransitionGuard:
    """Source-level, mirroring `test_tick.py`: one write seam, always guarded."""

    @staticmethod
    def _dispatch_ast() -> ast.Module:
        return ast.parse(Path(inspect.getfile(dispatch_module)).read_text())

    @staticmethod
    def _called_names(node: ast.AST) -> set[str]:
        names = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                func = sub.func
                if isinstance(func, ast.Name):
                    names.add(func.id)
                elif isinstance(func, ast.Attribute):
                    names.add(func.attr)
        return names

    def test_every_function_that_updates_state_also_calls_transition(self):
        """A second write path must fail the build, not silently bypass the guard."""
        tree = self._dispatch_ast()
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            called = self._called_names(node)
            if "update" in called and "transition" not in called:
                offenders.append(node.name)
        assert offenders == [], (
            f"these functions write node state without consulting transition(): {offenders}. "
            "Every state change must go through the single guarded seam."
        )

    @staticmethod
    def _docstring_nodes(tree: ast.Module) -> set[int]:
        """The `id()` of every Constant node that IS a docstring.

        Identified by position, not by value: `ast.get_docstring` returns text
        already dedented by `inspect.cleandoc`, so comparing a raw string literal
        against it never matches for any indented multi-line docstring — which
        would silently exclude nothing and make the caller's filter a no-op.
        """
        ids = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                ids.add(id(first.value))
        return ids

    def test_no_raw_sql_update_string_anywhere_in_the_module(self):
        """Raw SQL would bypass both the ORM and the AST check above."""
        tree = self._dispatch_ast()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "text":
                pytest.fail("dispatch.py uses sqlalchemy text() — raw SQL can bypass the transition guard")

        # Docstrings legitimately quote the conditional UPDATE to explain it.
        docstring_ids = self._docstring_nodes(tree)
        for literal in ast.walk(tree):
            if isinstance(literal, ast.Constant) and isinstance(literal.value, str) and id(literal) not in docstring_ids:
                assert "UPDATE " not in literal.value.upper(), f"raw UPDATE statement in a string literal: {literal.value!r}"

    def test_transition_is_imported_from_the_single_declared_module(self):
        """R-N2a: one vocabulary, one guard. No second copy, no local redefinition."""
        tree = self._dispatch_ast()
        # `level` matters: for `from .state import ...` the module is "state" and
        # level is 1. Ignoring the level would let an absolute `from state import
        # transition` — a different, shadowing module — pass this check.
        sources = {
            (node.level, node.module)
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and any(a.name == "transition" for a in node.names)
        }
        assert sources == {(1, "state")}, f"transition() must come from .state; got {sources}"

    def test_dispatch_never_writes_a_decision_with_human_actor_kind(self):
        """The engine must not record itself as a human in any decision row."""
        source = Path(inspect.getfile(dispatch_module)).read_text()
        assert "ActorKind.HUMAN" not in source, (
            "dispatch.py names ActorKind.HUMAN — the engine dispatches as SERVICE, even though its authority traces to a human approval"
        )
