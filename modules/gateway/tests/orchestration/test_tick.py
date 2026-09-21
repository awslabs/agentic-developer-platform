"""Tests for the engine tick (issue #4203).

Covers every case in the issue's Validation section: readiness, blocking with the
blocking predecessor retrievable, terminal nodes never moved, the adversarial
two-concurrent-ticks case (AC-12), the source-level no-bypass assertion, no
silent degradation on a DB error (R-NF3), bounded/paginated reads (R-NF7), and
idempotency.

The concurrency tests are the reason this file exists, so they are written to
fail for the right reason. "Two concurrent ticks" is modelled as **two attempts
that both observed the same prior state** — which is precisely what overlapping
invocations produce — rather than as two OS threads, whose interleaving would be
non-deterministic and whose failures would be flaky rather than informative.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration import tick as tick_module
from src.orchestration.models import (
    DecisionKind,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationFlow,
    OrchestrationNode,
)
from src.orchestration.state import NodeState
from src.orchestration.tick import (
    SATISFIED_STATES,
    TickReport,
    apply_guarded_transition,
    run_tick,
)
from src.shared.models.base import Base

ORG_A = "org-alpha"
ORG_B = "org-beta"


@pytest.fixture
async def engine():
    """In-memory SQLite engine shared across sessions.

    `StaticPool` keeps every session on the same in-memory database, which is
    what lets the concurrency tests open a second session and see the first
    one's rows. The two pysqlite hooks follow `test_compile.py`'s fixture: without
    them the driver manages transactions implicitly and a second session's view of
    an uncommitted write is not what the deployed asyncpg path would show.
    """
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
    node_ref: str,
    state: NodeState | str = NodeState.PENDING,
    org_id: str | None = None,
    kind: str = "story",
) -> OrchestrationNode:
    node = OrchestrationNode(
        org_id=org_id or flow.org_id,
        flow_id=flow.id,
        epic_ref="4191",
        wave_ref="wave-3",
        node_ref=node_ref,
        kind=kind,
        title=f"Node {node_ref}",
        state=state.value if isinstance(state, NodeState) else state,
    )
    session.add(node)
    await session.flush()
    return node


async def _make_edge(session: AsyncSession, flow: OrchestrationFlow, *, frm: OrchestrationNode, to: OrchestrationNode) -> None:
    session.add(OrchestrationEdge(org_id=flow.org_id, flow_id=flow.id, from_node_id=frm.id, to_node_id=to.id))
    await session.flush()


async def _state_of(session: AsyncSession, node_id: str) -> str:
    return (await session.execute(select(OrchestrationNode.state).where(OrchestrationNode.id == node_id))).scalar_one()


class TestReadiness:
    """A pending node moves exactly when its predecessors are satisfied."""

    async def test_all_predecessors_satisfied_becomes_ready(self, session):
        flow = await _make_flow(session)
        upstream = await _make_node(session, flow, node_ref="a", state=NodeState.PASSED)
        target = await _make_node(session, flow, node_ref="b")
        await _make_edge(session, flow, frm=upstream, to=target)

        report = await run_tick(session)

        assert await _state_of(session, target.id) == NodeState.READY.value
        assert report.transitions_effected == 1
        assert report.success

    async def test_node_with_no_predecessors_becomes_ready(self, session):
        # A root node has nothing blocking it, so the very first tick must be able
        # to release it — otherwise no graph ever starts moving.
        flow = await _make_flow(session)
        root = await _make_node(session, flow, node_ref="root")

        report = await run_tick(session)

        assert await _state_of(session, root.id) == NodeState.READY.value
        assert report.transitions_effected == 1

    async def test_unsatisfied_predecessor_stays_pending_and_blocker_is_retrievable(self, session):
        flow = await _make_flow(session)
        blocker = await _make_node(session, flow, node_ref="a", state=NodeState.RUNNING)
        target = await _make_node(session, flow, node_ref="b")
        await _make_edge(session, flow, frm=blocker, to=target)

        report = await run_tick(session)

        assert await _state_of(session, target.id) == NodeState.PENDING.value
        assert report.transitions_effected == 0
        # R-O2b: "what would make this ready?" must be answerable.
        assert report.blocked[target.id] == [blocker.id]

    async def test_one_unsatisfied_predecessor_among_several_blocks(self, session):
        flow = await _make_flow(session)
        done = await _make_node(session, flow, node_ref="a", state=NodeState.PASSED)
        pending_pred = await _make_node(session, flow, node_ref="b", state=NodeState.AWAITING_GATE)
        target = await _make_node(session, flow, node_ref="c")
        await _make_edge(session, flow, frm=done, to=target)
        await _make_edge(session, flow, frm=pending_pred, to=target)

        report = await run_tick(session)

        assert await _state_of(session, target.id) == NodeState.PENDING.value
        # Only the genuinely blocking predecessor is named.
        assert report.blocked[target.id] == [pending_pred.id]

    @pytest.mark.parametrize(
        "blocking_state",
        [
            NodeState.REJECTED_AT_GATE,  # state.py: successors stay pending
            NodeState.FAILED,
            NodeState.HALTED,
            NodeState.SUPERSEDED,
        ],
    )
    async def test_terminal_but_unsuccessful_predecessor_does_not_release(self, session, blocking_state):
        # "Terminal" is not "satisfied". Treating these as satisfied would release
        # work whose dependency never succeeded.
        flow = await _make_flow(session)
        blocker = await _make_node(session, flow, node_ref="a", state=blocking_state)
        target = await _make_node(session, flow, node_ref="b")
        await _make_edge(session, flow, frm=blocker, to=target)

        await run_tick(session)

        assert await _state_of(session, target.id) == NodeState.PENDING.value

    async def test_only_passed_counts_as_satisfied(self):
        assert SATISFIED_STATES == frozenset({NodeState.PASSED})

    async def test_undeclared_predecessor_state_blocks_rather_than_releasing(self, session):
        # A node carrying a literal outside the vocabulary (the `rejected` /
        # `skipped` phantoms of R-N2c) must never be read as done.
        flow = await _make_flow(session)
        blocker = await _make_node(session, flow, node_ref="a", state="skipped")
        target = await _make_node(session, flow, node_ref="b")
        await _make_edge(session, flow, frm=blocker, to=target)

        report = await run_tick(session)

        assert await _state_of(session, target.id) == NodeState.PENDING.value
        assert report.blocked[target.id] == [blocker.id]


class TestTerminalNodesUntouched:
    """The tick moves pending nodes and nothing else."""

    @pytest.mark.parametrize(
        "state",
        [
            NodeState.READY,
            NodeState.RUNNING,
            NodeState.AWAITING_GATE,
            NodeState.PASSED,
            NodeState.REJECTED_AT_GATE,
            NodeState.FAILED,
            NodeState.HALTED,
            NodeState.SUPERSEDED,
        ],
    )
    async def test_non_pending_node_is_never_transitioned(self, session, state):
        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="x", state=state)

        report = await run_tick(session)

        assert await _state_of(session, node.id) == state.value
        assert report.transitions_effected == 0
        assert report.nodes_examined == 0

    async def test_halted_node_is_not_resurrected_by_the_engine(self, session):
        # R-Q9c: only a human clears a halt. The engine must not be able to walk a
        # halted node back into the loop, even indirectly.
        flow = await _make_flow(session)
        halted = await _make_node(session, flow, node_ref="h", state=NodeState.HALTED)

        await run_tick(session)

        assert await _state_of(session, halted.id) == NodeState.HALTED.value


class TestConcurrencySafety:
    """AC-12 / R-O4e: overlapping ticks must not double-transition a node."""

    async def test_two_concurrent_ticks_exactly_one_transition_second_affects_zero_rows(self, session):
        """The adversarial case: both ticks observed `pending`, one must lose.

        Modelled as two guarded writes that each carry the state they observed,
        which is exactly the situation two overlapping invocations create. The
        loser must affect **0** rows rather than overwriting.
        """
        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="contended")

        first_rows, first_allowed = await apply_guarded_transition(
            session,
            node_id=node.id,
            org_id=ORG_A,
            flow_id=flow.id,
            observed_state=NodeState.PENDING.value,
            to_state=NodeState.READY,
            reason="tick 1",
        )
        second_rows, second_allowed = await apply_guarded_transition(
            session,
            node_id=node.id,
            org_id=ORG_A,
            flow_id=flow.id,
            # The stale observation the losing tick is holding.
            observed_state=NodeState.PENDING.value,
            to_state=NodeState.READY,
            reason="tick 2",
        )

        assert (first_allowed, second_allowed) == (True, True)
        assert first_rows == 1, "the winning tick must move exactly one row"
        assert second_rows == 0, "the losing tick must affect 0 rows, not overwrite"
        assert await _state_of(session, node.id) == NodeState.READY.value

    async def test_losing_tick_is_counted_as_a_lost_race_not_a_transition(self, session, monkeypatch):
        """A full tick whose candidate was moved underneath it no-ops visibly."""
        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="contended")

        # Another tick advances the node after we observed it as pending.
        real_fetch = tick_module._fetch_candidate_page
        seen: list[int] = []

        async def fetch_then_steal(sess, *, after_id, limit):
            page = await real_fetch(sess, after_id=after_id, limit=limit)
            if page and not seen:
                seen.append(1)
                # Simulate the competing tick winning the row first.
                await apply_guarded_transition(
                    sess,
                    node_id=node.id,
                    org_id=ORG_A,
                    flow_id=flow.id,
                    observed_state=NodeState.PENDING.value,
                    to_state=NodeState.READY,
                    reason="competing tick",
                )
            return page

        monkeypatch.setattr(tick_module, "_fetch_candidate_page", fetch_then_steal)

        report = await run_tick(session)

        assert report.lost_races == 1
        assert report.transitions_effected == 0
        assert report.success, "losing a race is normal operation, not a failure"
        assert await _state_of(session, node.id) == NodeState.READY.value

    async def test_two_sequential_ticks_produce_no_second_transition(self, session):
        """Idempotency: nothing changed in between, so nothing should move twice."""
        flow = await _make_flow(session)
        upstream = await _make_node(session, flow, node_ref="a", state=NodeState.PASSED)
        target = await _make_node(session, flow, node_ref="b")
        await _make_edge(session, flow, frm=upstream, to=target)

        first = await run_tick(session)
        second = await run_tick(session)

        assert first.transitions_effected == 1
        assert second.transitions_effected == 0
        assert second.nodes_examined == 0, "the node is no longer pending, so it is not even a candidate"
        assert await _state_of(session, target.id) == NodeState.READY.value

    async def test_guarded_write_on_a_stale_observation_of_a_moved_node_is_rejected(self, session):
        """A stale observation of a *different* state is refused by the authority
        guard, not merely missed by the WHERE clause."""
        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="x", state=NodeState.PASSED)

        rows, allowed = await apply_guarded_transition(
            session,
            node_id=node.id,
            org_id=ORG_A,
            flow_id=flow.id,
            observed_state=NodeState.PASSED.value,
            to_state=NodeState.READY,
            reason="stale",
        )

        # PASSED -> READY is not a service-actor edge at all.
        assert allowed is False
        assert rows == 0
        assert await _state_of(session, node.id) == NodeState.PASSED.value


class TestRejectionsRecorded:
    """R-N2b: a rejected transition is persisted, not dropped."""

    async def test_rejected_transition_writes_a_decision_row(self, session):
        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="g", state=NodeState.AWAITING_GATE)

        rows, allowed = await apply_guarded_transition(
            session,
            node_id=node.id,
            org_id=ORG_A,
            flow_id=flow.id,
            observed_state=NodeState.AWAITING_GATE.value,
            # AC-15: a service actor walking a node out of a gate IS the gate skip.
            to_state=NodeState.PASSED,
            reason="engine tried to self-approve a gate",
        )

        assert (rows, allowed) == (0, False)

        decisions = (await session.execute(select(OrchestrationDecision))).scalars().all()
        assert len(decisions) == 1
        assert decisions[0].kind == DecisionKind.TRANSITION_REJECTED.value
        assert decisions[0].actor_kind == "service"
        assert decisions[0].node_id == node.id
        assert decisions[0].rejection_reason  # non-empty, so the UI can render why
        assert await _state_of(session, node.id) == NodeState.AWAITING_GATE.value

    async def test_a_rejection_during_a_tick_is_counted_not_miscounted_as_a_lost_race(self, session, monkeypatch):
        """A refused edge must reach the `transitions_rejected` metric (R-NF8).

        `pending -> ready` is always legal for the engine, so this branch is only
        reachable if the guard ever refuses — which is exactly the case that must
        not be silently filed as a lost race. Forcing a refusal proves the
        counter is wired to the right branch.
        """
        flow = await _make_flow(session)
        node = await _make_node(session, flow, node_ref="n")

        real_transition = tick_module.transition

        def refuse(from_state, to_state, *, actor_kind, reason):
            result = real_transition(from_state, to_state, actor_kind=actor_kind, reason=reason)
            return type(result)(
                allowed=False,
                from_state=result.from_state,
                to_state=result.to_state,
                actor_kind=result.actor_kind,
                reason=result.reason,
                rejection_reason="refused by test",
            )

        monkeypatch.setattr(tick_module, "transition", refuse)

        report = await run_tick(session)

        assert report.transitions_rejected == 1
        assert report.transitions_effected == 0
        assert report.lost_races == 0, "a refusal is a recorded deviation, not an overlap"
        assert await _state_of(session, node.id) == NodeState.PENDING.value


class TestNoBypass:
    """Source-level: every state write in tick.py goes through transition().

    An AST assertion rather than a string search, so it survives reformatting and
    cannot be defeated by a comment. It is the guard that makes the invariant
    hold against a *future* refactor, which is the case a behavioural test cannot
    reach.
    """

    @staticmethod
    def _tick_ast() -> ast.Module:
        return ast.parse(Path(inspect.getfile(tick_module)).read_text())

    @staticmethod
    def _called_names(node: ast.AST) -> set[str]:
        names: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                func = sub.func
                if isinstance(func, ast.Name):
                    names.add(func.id)
                elif isinstance(func, ast.Attribute):
                    names.add(func.attr)
        return names

    def test_every_function_that_updates_state_also_calls_transition(self):
        tree = self._tick_ast()
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            called = self._called_names(node)
            if "update" in called and "transition" not in called:
                offenders.append(node.name)

        assert not offenders, f"these functions write state without consulting transition(): {offenders}"

    def test_no_raw_sql_update_string_anywhere_in_the_module(self):
        tree = self._tick_ast()

        # No `text(...)` construction at all — that is the one route that would
        # let a raw UPDATE past both the ORM and the AST check above.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "text":
                pytest.fail("tick.py must not build raw SQL via text(); state writes go through transition()")

        # Docstrings legitimately discuss the conditional update, so match the
        # shape of an actual statement (`UPDATE <table> SET`) rather than the
        # word, and skip docstrings outright.
        docstrings = {
            ast.get_docstring(node) for node in ast.walk(tree) if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        }
        sql_update = re.compile(r"\bupdate\s+\w+\s+set\b", re.IGNORECASE)

        for literal in ast.walk(tree):
            if isinstance(literal, ast.Constant) and isinstance(literal.value, str) and literal.value not in docstrings:
                assert not sql_update.search(literal.value), f"no raw UPDATE statement in a string literal: {literal.value!r}"

    def test_transition_is_imported_from_the_single_declared_module(self):
        # R-N2a: the vocabulary and the guard are declared once. A local copy here
        # would be a requirement violation, not a style choice.
        tree = self._tick_ast()
        sources = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and any(a.name == "transition" for a in node.names)}
        assert sources == {"state"}, f"transition() must come from .state, not {sources}"


class TestNoSilentDegradation:
    """R-NF3: a failure surfaces — non-success, error log, error metric."""

    async def test_db_error_mid_tick_returns_non_success_and_logs(self, session, monkeypatch, caplog):
        flow = await _make_flow(session)
        await _make_node(session, flow, node_ref="a")
        await _make_node(session, flow, node_ref="b")

        async def boom(*_args, **_kwargs):
            raise RuntimeError("connection reset mid-tick")

        monkeypatch.setattr(tick_module, "_predecessor_states", boom)

        with caplog.at_level("ERROR"):
            report = await run_tick(session)

        assert report.success is False, "a failed tick must NOT report success"
        assert report.errors == 2
        assert report.transitions_effected == 0
        assert any(rec.levelname == "ERROR" for rec in caplog.records)

    async def test_one_failing_node_does_not_stop_the_others(self, session, monkeypatch):
        # Containment: a single bad node must not stall every other flow, but the
        # tick still reports failure.
        flow = await _make_flow(session)
        good = await _make_node(session, flow, node_ref="aaa-good")
        bad = await _make_node(session, flow, node_ref="zzz-bad")

        real = tick_module._predecessor_states

        async def fail_for_one(sess, *, org_id, node_id):
            if node_id == bad.id:
                raise RuntimeError("boom")
            return await real(sess, org_id=org_id, node_id=node_id)

        monkeypatch.setattr(tick_module, "_predecessor_states", fail_for_one)

        report = await run_tick(session)

        assert report.errors == 1
        assert report.success is False
        assert report.transitions_effected == 1
        assert await _state_of(session, good.id) == NodeState.READY.value

    async def test_page_fetch_failure_is_an_error_not_an_empty_tick(self, session, monkeypatch, caplog):
        # The nastiest silent-stall shape: a read failure that looks like "no work
        # to do". It must report an error, not success-with-zero.
        async def boom(*_args, **_kwargs):
            raise RuntimeError("read timeout")

        monkeypatch.setattr(tick_module, "_fetch_candidate_page", boom)

        with caplog.at_level("ERROR"):
            report = await run_tick(session)

        assert report.success is False
        assert report.errors == 1
        assert report.nodes_examined == 0

    async def test_report_success_is_false_whenever_errors_are_recorded(self):
        report = TickReport()
        assert report.success
        report.record(ORG_A, "errors")
        assert report.success is False


class TestBoundedReads:
    """R-NF7: reads are paginated and hard-bounded."""

    async def test_more_candidates_than_the_page_size_are_paginated(self, session, monkeypatch):
        monkeypatch.setattr(tick_module, "_PAGE_SIZE", 3)

        flow = await _make_flow(session)
        for index in range(7):
            await _make_node(session, flow, node_ref=f"n{index:02d}")

        report = await run_tick(session)

        assert report.nodes_examined == 7
        assert report.transitions_effected == 7
        assert report.pages_read == 3, "7 rows at a page size of 3 must take 3 pages"
        assert report.truncated is False

    async def test_backstop_caps_the_scan_and_says_so(self, session, monkeypatch):
        monkeypatch.setattr(tick_module, "_PAGE_SIZE", 2)
        monkeypatch.setattr(tick_module, "_ITEM_BACKSTOP", 4)

        flow = await _make_flow(session)
        for index in range(9):
            await _make_node(session, flow, node_ref=f"n{index:02d}")

        report = await run_tick(session)

        assert report.nodes_examined == 4, "the backstop must bound the scan"
        # Truncation is never silent — that is what distinguishes "out of budget"
        # from "nothing left to do".
        assert report.truncated is True

    async def test_pagination_does_not_revisit_or_skip_rows(self, session, monkeypatch):
        # Keyset pagination must stay correct while the rows it walks are being
        # updated underneath it.
        monkeypatch.setattr(tick_module, "_PAGE_SIZE", 2)

        flow = await _make_flow(session)
        nodes = [await _make_node(session, flow, node_ref=f"n{index:02d}") for index in range(5)]

        report = await run_tick(session)

        assert report.nodes_examined == 5
        for node in nodes:
            assert await _state_of(session, node.id) == NodeState.READY.value


class TestTenantIsolation:
    """The tick spans orgs but never mixes them."""

    async def test_counts_are_broken_out_per_org(self, session):
        flow_a = await _make_flow(session, org_id=ORG_A, slug="flow-a")
        flow_b = await _make_flow(session, org_id=ORG_B, slug="flow-b")
        await _make_node(session, flow_a, node_ref="a1")
        await _make_node(session, flow_b, node_ref="b1")
        await _make_node(session, flow_b, node_ref="b2")

        report = await run_tick(session)

        assert report.per_org[ORG_A]["transitions_effected"] == 1
        assert report.per_org[ORG_B]["transitions_effected"] == 2
        # No org's total is another org's total.
        assert report.per_org[ORG_A]["nodes_examined"] == 1
        assert report.per_org[ORG_B]["nodes_examined"] == 2

    async def test_a_cross_tenant_edge_cannot_block_a_node(self, session):
        """An edge row naming another org's node must not influence readiness.

        If it could, one tenant's stalled work would silently hold another
        tenant's graph — a cross-tenant denial of service via a single row.
        """
        flow_a = await _make_flow(session, org_id=ORG_A, slug="flow-a")
        target = await _make_node(session, flow_a, node_ref="target")
        foreign = await _make_node(session, flow_a, node_ref="foreign", state=NodeState.RUNNING, org_id=ORG_B)
        # An edge belonging to ORG_B pointing at ORG_A's node.
        session.add(OrchestrationEdge(org_id=ORG_B, flow_id=flow_a.id, from_node_id=foreign.id, to_node_id=target.id))
        await session.flush()

        report = await run_tick(session)

        assert await _state_of(session, target.id) == NodeState.READY.value
        assert target.id not in report.blocked
        assert report.per_org[ORG_A]["transitions_effected"] == 1


class TestTickReportTokenSurvivesLambdaLogging:
    """Check 2 of evaluation #4240 greps CloudWatch for the `tick_report` token.

    That check is the only evidence the schedule actually FIRED rather than merely
    having been deployed, so the token must land under the logging setup Lambda
    really presents — not the one pytest presents.

    The distinction is what broke in dev: `awslambdaric` installs a root handler
    before importing the handler module and leaves the root level at WARNING, so
    the original `if not logging.getLogger().handlers: basicConfig(level=INFO)`
    guard was skipped precisely where it was needed, and every `tick_report` line
    was suppressed while the tick itself ran correctly and emitted metrics. A test
    using `caplog.at_level(...)` cannot catch this, because forcing the level is
    exactly the bug being masked.
    """

    def test_token_is_emitted_at_info_under_a_preconfigured_root_logger(self, monkeypatch):
        """Reproduces the Lambda container: root handler present, root at WARNING."""
        import importlib
        import io
        import logging as _logging

        root = _logging.getLogger()
        original_handlers = root.handlers[:]
        original_level = root.level
        try:
            stream = io.StringIO()
            root.handlers = [_logging.StreamHandler(stream)]
            root.setLevel(_logging.WARNING)  # awslambdaric's default

            # Re-import so module-level logging setup runs against that state.
            import src.orchestration.tick_handler as handler_module

            handler_module = importlib.reload(handler_module)

            report = _TickReportStub()
            monkeypatch.setattr(handler_module, "_run_with_cleanup", lambda: report, raising=True)
            monkeypatch.setattr(handler_module.asyncio, "run", lambda coro: report, raising=True)
            monkeypatch.setattr(handler_module, "_emit_metrics", lambda _r: None, raising=True)

            handler_module.handler({}, None)

            assert handler_module.TICK_REPORT_TOKEN in stream.getvalue()
        finally:
            root.handlers = original_handlers
            root.setLevel(original_level)
            import src.orchestration.tick_handler as handler_module

            importlib.reload(handler_module)


class _TickReportStub:
    """Minimal stand-in with the attributes `handler()` reads for its summary."""

    success = True
    nodes_examined = 0
    transitions_effected = 0
    transitions_rejected = 0
    errors = 0
    lost_races = 0
    pages_read = 1
    truncated = False
    per_org: dict = {}
    blocked: dict = {}
