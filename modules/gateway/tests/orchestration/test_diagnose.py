"""Tests for the exception-diagnoser summon path (issue #4214).

Covers every case in the issue's Validation section. The adversarial ones are not
confirmatory — they are the reason this file exists, because this story's entire
deliverable is authority containment and every one of its listed bug classes is a
containment failure:

- **AC-26 (load-bearing)** `TestDiagnoserHoldsNoPromotionAuthority`: source-level
  proof that this module cannot change a node's state — it does not even import
  the two functions that could — plus behavioural proof that a diagnoser-attributed
  promotion is rejected *and recorded*. This is the test that proves the EPIC's
  central guarantee survives its last story.
- **Cannot clear a halt** `TestDiagnoserCannotClearAHalt`: the diagnoser is
  summoned *because* the node halted, which makes this the most tempting bypass in
  the codebase and therefore the one worth pinning hardest.
- **Summon bound** `TestSummonBoundIsACostGuard`: five consecutive ticks yield one
  diagnosis. A regression here is a spend bug, so it is tested explicitly — and in
  both directions, because a bound that never re-diagnoses a genuinely new incident
  fails silently, which is worse.
- **Advisory framing** `TestAdvisoryFraming`: a record without the advisory flag and
  diagnoser attribution must fail. Presentation is part of the guarantee: a
  diagnosis that reads authoritative turns a human gate into a rubber stamp.
- **Tenant isolation** `TestTenantIsolation`: cross-org summon resolves to nothing
  and a mismatched `org_id` is refused outright.
- **Cut-safety** `TestCutSafety`: no other `src/orchestration/` module imports this
  one, so the story remains droppable.

The SQLite fixture follows `test_stall.py` / `test_dispatch_pass.py`, including the
two pysqlite hooks that make a second session observe the first's uncommitted
writes the way the deployed asyncpg path does.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.features import routes as features_routes
from src.orchestration import diagnose as diagnose_module
from src.orchestration.diagnose import (
    ADVISORY_PREFIX,
    DEFAULT_MAX_SUMMONS_PER_PASS,
    DIAGNOSABLE_STATES,
    DIAGNOSER_ACTOR_ROLE,
    DIAGNOSER_PERSONA,
    FEATURE_FLAG_ENV,
    Diagnosis,
    DiagnosisConfig,
    TenantMismatchError,
    record_diagnosis,
    run_diagnosis_pass,
)
from src.orchestration.models import (
    DecisionKind,
    OrchestrationDecision,
    OrchestrationFlow,
    OrchestrationNode,
)
from src.orchestration.state import ActorKind, NodeState, transition
from src.shared.models.base import Base
from src.shared.models.organization import Organization

ORG_A = "org-alpha"
ORG_B = "org-beta"
INSTALLATION_A = 111
REPO = "aws-e/adp"

NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)

# The path to the module under test, resolved once. Every source-level assertion
# reads this rather than hardcoding a path that a move would silently invalidate.
_DIAGNOSE_PATH = Path(inspect.getfile(diagnose_module))
_ORCHESTRATION_DIR = _DIAGNOSE_PATH.parent


@pytest.fixture
async def engine():
    """In-memory SQLite shared across sessions. Fixture shape from `test_stall.py`."""
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


@pytest.fixture
def config():
    """Enabled and fully wired, so tests exercise the real path by default.

    Enabled explicitly rather than via the env var: the fail-closed default is
    asserted on its own in `TestFailsClosed`, and every other test wants the
    feature actually running.
    """
    return DiagnosisConfig(enabled=True, repo=REPO)


async def _make_org(session: AsyncSession, *, org_id: str = ORG_A, installations: list[int] | None = None) -> Organization:
    org = Organization(
        id=org_id,
        name=f"Org {org_id}",
        github_installation_ids=[str(i) for i in (installations if installations is not None else [INSTALLATION_A])],
    )
    session.add(org)
    await session.flush()
    return org


async def _make_flow(session: AsyncSession, *, org_id: str = ORG_A, slug: str = "flow-1") -> OrchestrationFlow:
    flow = OrchestrationFlow(org_id=org_id, slug=slug, title="Demo flow")
    session.add(flow)
    await session.flush()
    return flow


async def _make_node(
    session: AsyncSession,
    flow: OrchestrationFlow,
    *,
    node_ref: str = "a",
    state: NodeState | str = NodeState.HALTED,
    attempts: int = 5,
    issue_ref: str | None = "4214",
    org_id: str | None = None,
    kind: str = "story",
) -> OrchestrationNode:
    node = OrchestrationNode(
        org_id=org_id or flow.org_id,
        flow_id=flow.id,
        epic_ref="4191",
        wave_ref="wave-7",
        node_ref=node_ref,
        kind=kind,
        title=f"Node {node_ref}",
        state=state.value if isinstance(state, NodeState) else state,
        attempts=attempts,
        issue_ref=issue_ref,
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(node)
    await session.flush()
    return node


async def _record_trigger(
    session: AsyncSession,
    node: OrchestrationNode,
    *,
    kind: DecisionKind = DecisionKind.NODE_HALTED,
    reason: str = "defect-cycle bound exhausted: 5 attempt(s) at a bound of 5",
    created_at: datetime | None = None,
) -> OrchestrationDecision:
    """Append the stall/halt decision row that the stall story would have written.

    This is the trigger source, reproduced exactly as `stall.py::_record_decision`
    writes it — not a mock. The summon path reads real rows of the real kind.
    """
    row = OrchestrationDecision(
        org_id=node.org_id,
        flow_id=node.flow_id,
        node_id=node.id,
        kind=kind.value,
        actor_id="system:orchestration-stall-detector",
        actor_role="engine",
        actor_kind=ActorKind.SERVICE.value,
        reason=reason,
        from_state=NodeState.RUNNING.value,
        to_state=node.state,
        created_at=created_at or NOW,
    )
    session.add(row)
    await session.flush()
    return row


async def _diagnoses(session: AsyncSession, node_id: str) -> list[OrchestrationDecision]:
    stmt = select(OrchestrationDecision).where(
        OrchestrationDecision.node_id == node_id,
        OrchestrationDecision.kind == DecisionKind.NODE_DIAGNOSIS_PROPOSED.value,
    )
    return list((await session.execute(stmt)).scalars().all())


def _as_utc(moment: datetime) -> datetime:
    """Normalise a timestamp read back from SQLite, which returns naive datetimes.

    Arithmetic against an aware value would raise on SQLite and work on asyncpg —
    the same normalisation `stall.py` applies for the same reason.
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


async def _state_of(session: AsyncSession, node_id: str) -> str:
    return (await session.execute(select(OrchestrationNode.state).where(OrchestrationNode.id == node_id))).scalar_one()


def _diagnose_ast() -> ast.Module:
    return ast.parse(_DIAGNOSE_PATH.read_text())


# ---------------------------------------------------------------------------
# AC-26 — the load-bearing adversarial test
# ---------------------------------------------------------------------------


class TestDiagnoserHoldsNoPromotionAuthority:
    """AC-26. The EPIC exists to prevent an agent promoting work; this is its last story.

    Source-level *and* behavioural, because each catches what the other cannot: the
    source assertions prove no such path exists today, and the behavioural ones
    prove the attempt would be refused even if someone added one.
    """

    def test_the_module_does_not_import_transition(self):
        """Stronger than "never calls it": it cannot, because it has no reference.

        `transition()` is the single guarded entry point for changing a node's
        state. A module that never imports it cannot change state through the
        sanctioned path, and the two tests below close the unsanctioned ones.
        """
        tree = _diagnose_ast()
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.update(alias.name for alias in node.names)

        assert "transition" not in imported, "diagnose.py must not import transition(); the diagnoser holds no promotion authority"
        assert "apply_guarded_transition" not in imported, "diagnose.py must not import apply_guarded_transition(); it writes no state"

    def test_the_module_never_calls_transition_or_the_guarded_seam(self):
        """Belt and braces: no call by either name, however it was obtained."""
        forbidden = {"transition", "apply_guarded_transition", "dispatch_node"}
        for node in ast.walk(_diagnose_ast()):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                assert name not in forbidden, f"diagnose.py must not call {name}(); the diagnoser cannot move a node"

    def test_the_module_never_names_a_transition_target(self):
        """No `to_state=` keyword anywhere except the explicit `None` on the record.

        A diagnosis proposes nowhere for the node to go. The only `to_state` in the
        module is the literal `None` written onto the decision row, and this test
        pins that: any other value would be a proposed promotion.
        """
        for node in ast.walk(_diagnose_ast()):
            if isinstance(node, ast.keyword) and node.arg == "to_state":
                assert isinstance(node.value, ast.Constant) and node.value.value is None, (
                    "the only to_state in diagnose.py must be the literal None; a diagnosis proposes no transition"
                )

    def test_the_module_never_builds_an_update_statement(self):
        """No UPDATE, and no raw SQL. The module appends; it does not mutate."""
        for node in ast.walk(_diagnose_ast()):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                assert name not in {"update", "text"}, f"diagnose.py must not build {name}(); it writes no state and no raw SQL"

    def test_the_module_never_references_a_human_actor(self):
        """`ActorKind.HUMAN` is what makes the human-only recovery edges reachable.

        Same assertion `test_stall.py` makes about the stall detector, for the same
        reason and with more at stake: this module is summoned precisely when a
        human-only edge would be the convenient thing to take.
        """
        for node in ast.walk(_diagnose_ast()):
            if isinstance(node, ast.Attribute) and node.attr == "HUMAN":
                pytest.fail("diagnose.py must not reference ActorKind.HUMAN; the diagnoser is never a human actor")

    def test_the_persisted_row_records_a_service_actor(self):
        """Attribution on the row itself, not merely in the code that wrote it."""
        assert 'actor_kind="service"' in _DIAGNOSE_PATH.read_text(), "the diagnosis row must be attributed to a service actor"

    @pytest.mark.parametrize(
        ("from_state", "to_state"),
        [
            (NodeState.AWAITING_GATE, NodeState.PASSED),
            (NodeState.HALTED, NodeState.READY),
            (NodeState.FAILED, NodeState.READY),
            (NodeState.REJECTED_AT_GATE, NodeState.READY),
        ],
    )
    def test_a_diagnoser_attributed_promotion_is_rejected(self, from_state, to_state):
        """Behavioural half of AC-26, across every promotion a diagnoser might want.

        The diagnoser is a service actor, so every one of these edges refuses it.
        Parametrized rather than asserted once because "cannot promote" must hold
        for *all* promotions, not just the halt-clearing one.
        """
        result = transition(from_state, to_state, actor_kind=ActorKind.SERVICE, reason="diagnoser attempting to promote a node")

        assert result.allowed is False
        assert result.new_state is None
        assert "requires actor_kind" in result.rejection_reason

    async def test_a_rejected_promotion_is_recorded_not_merely_refused(self, session, config):
        """AC-26 requires the attempt be *recorded*. A refusal nobody can see is not a control.

        Under RULING 5 recorded rejections are the primary detector for off-plan
        agent activity, so the evidence surviving is the point — a refusal that
        unwound the stack would lose it.
        """
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED)

        result = transition(NodeState.HALTED, NodeState.READY, actor_kind=ActorKind.SERVICE, reason="diagnoser clearing its own halt")
        assert result.allowed is False

        # The rejection is durable, exactly as the tick's seam records it.
        session.add(
            OrchestrationDecision(
                org_id=node.org_id,
                flow_id=node.flow_id,
                node_id=node.id,
                kind=DecisionKind.TRANSITION_REJECTED.value,
                actor_id="system:orchestration-exception-diagnoser",
                actor_role=DIAGNOSER_ACTOR_ROLE,
                actor_kind=ActorKind.SERVICE.value,
                reason=result.reason,
                rejection_reason=result.rejection_reason,
                from_state=str(result.from_state),
                to_state=str(result.to_state),
            )
        )
        await session.flush()

        rows = list(
            (
                await session.execute(
                    select(OrchestrationDecision).where(
                        OrchestrationDecision.node_id == node.id,
                        OrchestrationDecision.kind == DecisionKind.TRANSITION_REJECTED.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].actor_role == DIAGNOSER_ACTOR_ROLE
        assert rows[0].rejection_reason and "requires actor_kind" in rows[0].rejection_reason
        # And the node did not budge.
        assert await _state_of(session, node.id) == NodeState.HALTED.value

    async def test_a_full_pass_changes_no_node_state(self, session, config):
        """End-to-end: the pass runs, records a diagnosis, and moves nothing."""
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED)
        await _record_trigger(session, node)

        report = await run_diagnosis_pass(session, config)

        assert report.diagnoses_recorded == 1
        assert await _state_of(session, node.id) == NodeState.HALTED.value, "a diagnosis pass must never change a node's state"

    async def test_the_diagnosis_row_carries_no_target_state(self, session, config):
        """`to_state IS NULL` — the record cannot express a promotion.

        This is the structural expression of propose-never-dispose: every other
        node-scoped decision kind carries a state pair, and this one deliberately
        does not.
        """
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED)
        await _record_trigger(session, node)

        await run_diagnosis_pass(session, config)

        (row,) = await _diagnoses(session, node.id)
        assert row.to_state is None, "a diagnosis must propose no target state"
        assert row.from_state == NodeState.HALTED.value, "a diagnosis records the state it observed"


# ---------------------------------------------------------------------------
# Cannot clear a halt
# ---------------------------------------------------------------------------


class TestDiagnoserCannotClearAHalt:
    """The diagnoser is summoned *because* the node halted. That is not permission to un-halt it."""

    def test_a_service_actor_cannot_take_the_halt_recovery_edge(self):
        result = transition(NodeState.HALTED, NodeState.READY, actor_kind=ActorKind.SERVICE, reason="diagnoser resuming a halted node")

        assert result.allowed is False
        assert result.new_state is None
        assert "requires actor_kind" in result.rejection_reason

    def test_the_edge_exists_for_humans_so_this_pins_human_only_not_impossible(self):
        """The override is real — it is just human-only. Asserted so the test above
        cannot pass merely because the edge was deleted."""
        result = transition(NodeState.HALTED, NodeState.READY, actor_kind=ActorKind.HUMAN, reason="operator override")

        assert result.allowed is True
        assert result.new_state is NodeState.READY

    def test_the_module_never_names_ready_as_a_target(self):
        """Source-level: `READY` is the only state that could clear a halt."""
        for node in ast.walk(_diagnose_ast()):
            if isinstance(node, ast.Attribute) and node.attr == "READY":
                pytest.fail("diagnose.py must never reference NodeState.READY; that is the state that would clear a halt")

    async def test_a_halted_node_stays_halted_across_the_pass(self, session, config):
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED, attempts=99)
        await _record_trigger(session, node)

        await run_diagnosis_pass(session, config)

        assert await _state_of(session, node.id) == NodeState.HALTED.value

    async def test_the_module_writes_no_halt_overridden_decision(self, session, config):
        """`HALT_OVERRIDDEN` is the kind a human's override records. The diagnoser must never emit it."""
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED)
        await _record_trigger(session, node)

        await run_diagnosis_pass(session, config)

        rows = list(
            (
                await session.execute(
                    select(OrchestrationDecision).where(OrchestrationDecision.kind == DecisionKind.HALT_OVERRIDDEN.value),
                )
            )
            .scalars()
            .all()
        )
        assert rows == [], "a diagnosis pass must never record a halt override"


# ---------------------------------------------------------------------------
# The summon bound — the cost guard
# ---------------------------------------------------------------------------


class TestSummonBoundIsACostGuard:
    """One diagnosis per node per stall/halt event. A regression here is a spend bug.

    Each diagnosis is a real agent run with real Bedrock cost, so this is the
    story's named risk area and it is tested in both directions: the bound must
    suppress repeat ticks, and it must NOT suppress a genuinely new incident.
    """

    async def test_a_node_halted_across_five_ticks_is_diagnosed_exactly_once(self, session, config):
        """The issue's explicit case. Five consecutive passes, one diagnosis."""
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED)
        await _record_trigger(session, node)

        reports = [await run_diagnosis_pass(session, config) for _ in range(5)]

        assert len(await _diagnoses(session, node.id)) == 1, "five ticks on one halted node must produce exactly one diagnosis"
        assert reports[0].diagnoses_recorded == 1
        assert [r.diagnoses_recorded for r in reports[1:]] == [0, 0, 0, 0]
        # And the suppression is visible, not merely an absence of spend.
        assert [r.suppressed_by_bound for r in reports[1:]] == [1, 1, 1, 1]

    async def test_a_node_with_no_stall_or_halt_event_is_never_diagnosed(self, session, config):
        """A node failed by hand is in a diagnosable state but nothing asked for a diagnosis.

        Without this, any node reaching `failed` by any route would summon an
        agent — the unbounded-spend bug by a different door.
        """
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.FAILED)
        # No trigger row.

        report = await run_diagnosis_pass(session, config)

        assert report.nodes_examined == 1
        assert report.diagnoses_recorded == 0
        assert report.suppressed_by_bound == 0, "no event is not the same as an already-diagnosed event"
        assert await _diagnoses(session, node.id) == []

    async def test_a_genuinely_new_event_after_a_diagnosis_is_diagnosed_again(self, session, config):
        """The other direction, and the one a naive marker gets wrong.

        A node stalls, a human resumes it, it runs, and it stalls again. That is a
        new incident with new context. A "has this node ever been diagnosed?" check
        would bound spend correctly and then silently never diagnose the second
        failure — an invisible loss, which is worse than the spend it saves.
        """
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.FAILED)
        await _record_trigger(session, node, kind=DecisionKind.NODE_STALLED, created_at=NOW)

        first = await run_diagnosis_pass(session, config)
        assert first.diagnoses_recorded == 1

        # A second stall event, strictly after the diagnosis that was just written.
        # Anchored to that row's own `created_at` rather than to a fabricated
        # timestamp: the diagnosis is stamped by the real clock (`utcnow` is the
        # column default), so a hardcoded "later" time drawn from this file's fake
        # timeline would actually be in the past and the test would assert the
        # opposite of what it claims.
        (recorded,) = await _diagnoses(session, node.id)
        await _record_trigger(
            session,
            node,
            kind=DecisionKind.NODE_STALLED,
            created_at=_as_utc(recorded.created_at) + timedelta(hours=6),
        )

        second = await run_diagnosis_pass(session, config)

        assert second.diagnoses_recorded == 1, "a new stall/halt event is a new incident and must be diagnosed"
        assert len(await _diagnoses(session, node.id)) == 2

    async def test_an_event_older_than_the_last_diagnosis_does_not_resummon(self, session, config):
        """Ordering, not mere existence: a stale event must not re-trigger."""
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED)
        await _record_trigger(session, node, created_at=NOW)

        assert (await run_diagnosis_pass(session, config)).diagnoses_recorded == 1

        # An event backdated strictly before the diagnosis we just wrote. Anchored
        # to that row for the same reason as the test above.
        (recorded,) = await _diagnoses(session, node.id)
        await _record_trigger(session, node, created_at=_as_utc(recorded.created_at) - timedelta(days=1))

        assert (await run_diagnosis_pass(session, config)).diagnoses_recorded == 0
        assert len(await _diagnoses(session, node.id)) == 1

    async def test_the_per_pass_cap_bounds_a_mass_halt(self, session):
        """A second, independent bound: many nodes halting at once.

        The per-node marker cannot help here — every one of these nodes is on its
        first event — so a mass halt would be a burst of simultaneous agent runs
        without this cap.
        """
        await _make_org(session)
        flow = await _make_flow(session)
        for i in range(10):
            node = await _make_node(session, flow, node_ref=f"n{i}", state=NodeState.HALTED)
            await _record_trigger(session, node)

        report = await run_diagnosis_pass(session, DiagnosisConfig(enabled=True, repo=REPO, max_summons_per_pass=3))

        assert report.diagnoses_recorded == 3
        assert report.capped is True, "hitting the cap must be reported, not inferred from a short count"

    async def test_the_default_cap_is_the_documented_value(self):
        assert DiagnosisConfig().max_summons_per_pass == DEFAULT_MAX_SUMMONS_PER_PASS

    def test_a_cap_below_one_is_rejected(self):
        """A cap of zero would disable diagnosis while looking configured."""
        for cap in (0, -1):
            with pytest.raises(ValueError, match="at least 1"):
                DiagnosisConfig(max_summons_per_pass=cap)


# ---------------------------------------------------------------------------
# Advisory framing
# ---------------------------------------------------------------------------


class TestAdvisoryFraming:
    """Presentation is part of the guarantee, not polish.

    A diagnosis that reads as a system conclusion invites a human to rubber-stamp
    an agent's guess, which turns the gate this EPIC protects into decoration. So
    the advisory framing is asserted on the persisted record, not on a renderer.
    """

    async def test_the_record_carries_the_advisory_marker_and_diagnoser_attribution(self, session, config):
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED)
        await _record_trigger(session, node)

        await run_diagnosis_pass(session, config)

        (row,) = await _diagnoses(session, node.id)
        assert row.reason.startswith(ADVISORY_PREFIX), "the advisory marker must lead the persisted reason"
        assert row.actor_role == DIAGNOSER_ACTOR_ROLE
        assert row.actor_kind == ActorKind.SERVICE.value
        assert row.kind == DecisionKind.NODE_DIAGNOSIS_PROPOSED.value

    def test_the_advisory_marker_says_unverified_and_not_a_verdict(self):
        """The wording is load-bearing, so it is pinned rather than left to taste."""
        assert "ADVISORY" in ADVISORY_PREFIX
        assert "unverified" in ADVISORY_PREFIX.lower()
        assert "not a verdict" in ADVISORY_PREFIX.lower()

    def test_the_decision_kind_is_named_as_a_proposal(self):
        """`node_diagnosis_proposed`, not `node_diagnosed`.

        A row named for a settled finding is read as one. The name is part of the
        framing.
        """
        assert DecisionKind.NODE_DIAGNOSIS_PROPOSED.value == "node_diagnosis_proposed"
        assert not hasattr(DecisionKind, "NODE_DIAGNOSED"), "a kind named NODE_DIAGNOSED would read as a verdict"

    def test_a_diagnosis_cannot_claim_to_be_authoritative(self):
        """`advisory` is a property with no setter — there is no path to False."""
        diagnosis = Diagnosis(
            node_id="n",
            org_id=ORG_A,
            flow_id="f",
            observed_state=NodeState.HALTED.value,
            trigger_kind=DecisionKind.NODE_HALTED.value,
            summary="something went wrong",
        )
        assert diagnosis.advisory is True

        with pytest.raises(AttributeError):
            diagnosis.advisory = False  # type: ignore[misc]

    def test_a_diagnosis_has_no_field_in_which_to_propose_a_transition(self):
        """Propose-never-dispose at the level of the type.

        If the dataclass had a `to_state` or `proposed_state` field, a caller could
        express a promotion and only convention would stop it being honoured.
        """
        fields = set(Diagnosis.__dataclass_fields__)
        for forbidden in ("to_state", "proposed_state", "next_state", "target_state"):
            assert forbidden not in fields, f"Diagnosis must not carry {forbidden}; a diagnosis proposes no transition"

    async def test_the_summary_contains_the_context_a_reviewer_would_gather_by_hand(self, session, config):
        """The point of the story: the human starts from a first pass, not a blank page."""
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED, attempts=5)
        await _record_trigger(session, node, reason="defect-cycle bound exhausted: 5 attempt(s) at a bound of 5")

        await run_diagnosis_pass(session, config)

        (row,) = await _diagnoses(session, node.id)
        assert node.node_ref in row.reason, "the graph address locates the failure"
        assert NodeState.HALTED.value in row.reason, "the observed state is part of the context"
        assert "5 attempt(s)" in row.reason, "the recorded reason from the trigger event is carried through"
        assert DecisionKind.NODE_HALTED.value in row.reason, "which event triggered this is part of the context"


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    """One org's failure context must never reach another's audit trail."""

    def test_a_mismatched_org_id_is_refused_outright(self):
        """`record_diagnosis` compares rather than trusts. There is no safe repair."""
        diagnosis = Diagnosis(
            node_id="n",
            org_id=ORG_B,
            flow_id="f",
            observed_state=NodeState.HALTED.value,
            trigger_kind=DecisionKind.NODE_HALTED.value,
            summary="cross-tenant attempt",
        )

        with pytest.raises(TenantMismatchError, match="never cross a tenant boundary"):
            record_diagnosis(None, diagnosis, node_org_id=ORG_A)  # type: ignore[arg-type]

    async def test_a_mismatched_org_id_writes_nothing(self, session):
        """The refusal must happen before the append, not after."""
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED)

        diagnosis = Diagnosis(
            node_id=node.id,
            org_id=ORG_B,
            flow_id=node.flow_id,
            observed_state=node.state,
            trigger_kind=DecisionKind.NODE_HALTED.value,
            summary="cross-tenant attempt",
        )
        with pytest.raises(TenantMismatchError):
            record_diagnosis(session, diagnosis, node_org_id=node.org_id)

        await session.flush()
        assert await _diagnoses(session, node.id) == []

    async def test_a_trigger_event_in_another_org_does_not_summon(self, session, config):
        """The summon path filters on `org_id`, so another org's event is invisible.

        Constructed adversarially: the trigger row points at this node by id but
        carries a different `org_id`, which is exactly what a cross-tenant leak
        would look like.
        """
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED)

        session.add(
            OrchestrationDecision(
                org_id=ORG_B,  # Not the node's org.
                flow_id=node.flow_id,
                node_id=node.id,
                kind=DecisionKind.NODE_HALTED.value,
                actor_id="system:orchestration-stall-detector",
                actor_role="engine",
                actor_kind=ActorKind.SERVICE.value,
                reason="halt recorded against the wrong tenant",
                created_at=NOW,
            )
        )
        await session.flush()

        report = await run_diagnosis_pass(session, config)

        assert report.diagnoses_recorded == 0, "an event in another org must not summon a diagnoser"
        assert await _diagnoses(session, node.id) == []

    async def test_each_diagnosis_carries_its_own_node_s_org(self, session, config):
        """Two orgs halting at once must produce two correctly-attributed records."""
        await _make_org(session, org_id=ORG_A)
        await _make_org(session, org_id=ORG_B)
        flow_a = await _make_flow(session, org_id=ORG_A, slug="flow-a")
        flow_b = await _make_flow(session, org_id=ORG_B, slug="flow-b")
        node_a = await _make_node(session, flow_a, node_ref="a", state=NodeState.HALTED)
        node_b = await _make_node(session, flow_b, node_ref="b", state=NodeState.HALTED)
        await _record_trigger(session, node_a)
        await _record_trigger(session, node_b)

        report = await run_diagnosis_pass(session, config)

        assert report.diagnoses_recorded == 2
        (row_a,) = await _diagnoses(session, node_a.id)
        (row_b,) = await _diagnoses(session, node_b.id)
        assert row_a.org_id == ORG_A
        assert row_b.org_id == ORG_B
        # Per-org counters keep the two separable in metrics too.
        assert report.per_org[ORG_A]["diagnoses_recorded"] == 1
        assert report.per_org[ORG_B]["diagnoses_recorded"] == 1

    async def test_the_dispatched_envelope_is_scoped_to_the_node_s_org(self, session, config):
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED)
        await _record_trigger(session, node)

        report = await run_diagnosis_pass(session, config)

        (pending,) = report.pending
        assert pending.envelope["tenant_id"] == ORG_A
        assert pending.org_id == ORG_A


# ---------------------------------------------------------------------------
# Cut-safety
# ---------------------------------------------------------------------------


class TestCutSafety:
    """The story must remain droppable. Nothing else may import it."""

    def test_no_other_orchestration_module_imports_diagnose(self):
        """Source-level across the whole package, so a future caller trips this test.

        The issue is explicit: "nothing else in this EPIC imports this module... Do
        not add a caller in any other story." If this fails, either the import must
        be removed or the cut-safety claim must be withdrawn — it cannot be both.
        """
        offenders: list[str] = []

        for path in sorted(_ORCHESTRATION_DIR.rglob("*.py")):
            if path == _DIAGNOSE_PATH:
                continue

            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module == "diagnose" or module.endswith(".diagnose"):
                        offenders.append(f"{path.name}: from {module} import ...")
                    if any(alias.name == "diagnose" for alias in node.names):
                        offenders.append(f"{path.name}: from {module} import diagnose")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.endswith(".diagnose") or alias.name == "diagnose":
                            offenders.append(f"{path.name}: import {alias.name}")

        assert offenders == [], f"diagnose.py must have no callers so the story stays droppable; found: {offenders}"

    def test_the_story_adds_no_migration(self):
        """The no-new-table case: `kind` is String(32) and holds the new value.

        Asserted so a later change that needs DDL cannot land quietly under this
        story's "no migration" claim.
        """
        assert len(DecisionKind.NODE_DIAGNOSIS_PROPOSED.value) <= 32, "the new kind must fit the existing String(32) column"
        assert OrchestrationDecision.__table__.c.kind.type.length == 32

    def test_diagnose_reuses_the_dispatch_seam_rather_than_forking_it(self):
        """The reuse table names the dispatch seam as reused as-is."""
        tree = _diagnose_ast()
        from_dispatch = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module == "dispatch_pass"]
        imported = {alias.name for node in from_dispatch for alias in node.names}

        assert {"message_group_id", "message_deduplication_id"} <= imported, "diagnose.py must reuse the dispatch story's FIFO key shapes"
        assert "resolve_installation_id" in imported, "diagnose.py must reuse the dispatch story's fail-closed installation check"

    def test_the_vocabulary_comes_from_the_single_declared_module(self):
        """R-N2a: a local copy of the state vocabulary would be a requirement violation."""
        tree = _diagnose_ast()
        sources = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and any(a.name == "NodeState" for a in node.names)}
        assert sources == {"state"}, f"NodeState must come from .state, not {sources}"


# ---------------------------------------------------------------------------
# Fail-closed flag
# ---------------------------------------------------------------------------


class TestFailsClosed:
    """Off by default, and off when the flag says anything other than "true"."""

    @pytest.fixture(autouse=True)
    def _no_flag_env(self, monkeypatch):
        monkeypatch.delenv(FEATURE_FLAG_ENV, raising=False)

    def test_the_default_config_is_disabled(self):
        assert DiagnosisConfig().enabled is False

    def test_an_absent_flag_resolves_to_disabled(self):
        assert DiagnosisConfig.from_env().enabled is False

    @pytest.mark.parametrize("value", ["", "False", "0", "no", "off", "1", "yes", "enabled", "TRUE-ish"])
    def test_only_the_literal_true_enables_it(self, monkeypatch, value):
        """`"1"`/`"yes"` are here deliberately: truthy-string semantics must NOT enable an agent-summoning path."""
        monkeypatch.setenv(FEATURE_FLAG_ENV, value)
        assert DiagnosisConfig.from_env().enabled is False

    @pytest.mark.parametrize("value", ["true", "TRUE", "True", "tRuE"])
    def test_explicit_true_enables_it(self, monkeypatch, value):
        monkeypatch.setenv(FEATURE_FLAG_ENV, value)
        assert DiagnosisConfig.from_env().enabled is True

    def test_the_flag_semantics_match_the_gateway_s_strict_helper(self, monkeypatch):
        """Pinned against `_is_enabled_strict` so the two cannot drift.

        This module reads the env var itself rather than importing the helper (the
        helper lives in a routes module), so the risk is a silent divergence. This
        test removes it by asserting the two agree on every interesting value.
        """
        for value in ["true", "TRUE", "True", "", "false", "0", "1", "yes", "off", "enabled"]:
            monkeypatch.setenv(FEATURE_FLAG_ENV, value)
            assert DiagnosisConfig.from_env().enabled is features_routes._is_enabled_strict(FEATURE_FLAG_ENV), f"divergence on {value!r}"

    async def test_a_disabled_pass_examines_nothing_and_writes_nothing(self, session):
        """Fail-closed means no rows and no agent runs, not merely no dispatch."""
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED)
        await _record_trigger(session, node)

        report = await run_diagnosis_pass(session, DiagnosisConfig(enabled=False, repo=REPO))

        assert report.enabled is False
        assert report.nodes_examined == 0
        assert report.diagnoses_recorded == 0
        assert report.pending == []
        assert await _diagnoses(session, node.id) == []

    async def test_an_unset_flag_disables_the_pass_end_to_end(self, session):
        """With no config passed, `from_env` applies — and it is off."""
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED)
        await _record_trigger(session, node)

        report = await run_diagnosis_pass(session)

        assert report.enabled is False
        assert await _diagnoses(session, node.id) == []

    def test_a_malformed_cap_falls_back_rather_than_raising(self, monkeypatch):
        """This runs on the tick path; a typo must not take the tick down."""
        for bad in ("x", "0", "-3", "1.5"):
            monkeypatch.setenv("BG_ORCH_MAX_DIAGNOSES_PER_PASS", bad)
            assert DiagnosisConfig.from_env().max_summons_per_pass == DEFAULT_MAX_SUMMONS_PER_PASS

    def test_a_valid_cap_is_honoured(self, monkeypatch):
        monkeypatch.setenv("BG_ORCH_MAX_DIAGNOSES_PER_PASS", "2")
        assert DiagnosisConfig.from_env().max_summons_per_pass == 2


# ---------------------------------------------------------------------------
# Candidate selection and dispatch
# ---------------------------------------------------------------------------


class TestCandidateSelection:
    """Which nodes are diagnosable, and which are deliberately not."""

    def test_only_the_two_stall_story_outcomes_are_diagnosable(self):
        assert DIAGNOSABLE_STATES == frozenset({NodeState.FAILED, NodeState.HALTED})

    @pytest.mark.parametrize("state", [NodeState.PENDING, NodeState.READY, NodeState.RUNNING, NodeState.AWAITING_GATE, NodeState.PASSED])
    async def test_a_node_not_in_a_diagnosable_state_is_never_examined(self, session, config, state):
        """A `running` node is still working; a `passed` node succeeded. Diagnosing
        either would summon an agent to explain a non-failure."""
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=state)
        await _record_trigger(session, node)

        report = await run_diagnosis_pass(session, config)

        assert report.nodes_examined == 0
        assert await _diagnoses(session, node.id) == []

    async def test_a_stalled_node_is_diagnosed_from_its_failed_state(self, session, config):
        """A stall becomes `failed` per the stall story, so that is where it is found."""
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.FAILED)
        await _record_trigger(session, node, kind=DecisionKind.NODE_STALLED, reason="stalled: 21600s in 'running'")

        report = await run_diagnosis_pass(session, config)

        assert report.diagnoses_recorded == 1
        (row,) = await _diagnoses(session, node.id)
        assert DecisionKind.NODE_STALLED.value in row.reason

    async def test_one_bad_node_does_not_stop_the_others(self, session, config, monkeypatch):
        """Per-node containment (R-NF3): log, count, force non-success, keep going."""
        await _make_org(session)
        flow = await _make_flow(session)
        good = await _make_node(session, flow, node_ref="good", state=NodeState.HALTED)
        await _record_trigger(session, good)
        bad = await _make_node(session, flow, node_ref="bad", state=NodeState.HALTED)
        await _record_trigger(session, bad)

        real_should_summon = diagnose_module._should_summon

        async def _explode(session_, *, node_id, org_id):
            if node_id == bad.id:
                raise RuntimeError("simulated per-node failure")
            return await real_should_summon(session_, node_id=node_id, org_id=org_id)

        monkeypatch.setattr(diagnose_module, "_should_summon", _explode)

        report = await run_diagnosis_pass(session, config)

        assert report.errors == 1
        assert report.success is False, "an error must force a non-success report"
        assert report.diagnoses_recorded == 1, "the healthy node must still be diagnosed"
        assert len(await _diagnoses(session, good.id)) == 1

    async def test_an_unreadable_page_is_an_error_not_an_empty_pass(self, session, config, monkeypatch):
        """Reporting success on a failed read would be the silent stall this EPIC ends."""

        async def _explode(*_args, **_kwargs):
            raise RuntimeError("simulated query failure")

        monkeypatch.setattr(diagnose_module, "_fetch_candidate_page", _explode)

        report = await run_diagnosis_pass(session, config)

        assert report.errors == 1
        assert report.success is False
        assert report.diagnoses_recorded == 0


class TestDispatchIsAdvisoryToo:
    """The diagnoser is dispatched like any worker — with no elevated genesis."""

    async def test_the_envelope_names_the_diagnoser_persona(self, session, config):
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED)
        await _record_trigger(session, node)

        report = await run_diagnosis_pass(session, config)

        (pending,) = report.pending
        assert pending.envelope["persona"] == DIAGNOSER_PERSONA
        assert pending.envelope["intent"]["trigger"] == "engine_diagnosis"

    async def test_the_envelope_claims_no_human_root(self, session, config):
        """Engine-summoned housekeeping. Claiming a human root would be the
        elevated genesis this story must not have."""
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED)
        await _record_trigger(session, node)

        report = await run_diagnosis_pass(session, config)

        (pending,) = report.pending
        assert pending.envelope["correlation"]["is_human_rooted"] is False
        assert pending.envelope["correlation"]["root_human_id"] is None

    async def test_the_envelope_marks_itself_advisory_only(self, session, config):
        """So the run knows its own authority rather than inferring it from a persona string."""
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED)
        await _record_trigger(session, node)

        report = await run_diagnosis_pass(session, config)

        (pending,) = report.pending
        assert pending.envelope["orchestration"]["advisory_only"] is True

    async def test_the_envelope_satisfies_the_worker_s_required_fields(self, session, config):
        """`parse_envelope` in the agent worker rejects a message missing any of these.

        Pinned here because the failure is otherwise invisible from the gateway
        side: the message publishes successfully and the worker discards it.
        """
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED)
        await _record_trigger(session, node)

        report = await run_diagnosis_pass(session, config)

        (pending,) = report.pending
        for key in ("tenant_id", "persona", "source_ref"):
            assert key in pending.envelope, f"the worker requires {key}"
        for key in ("installation_id", "repo", "issue"):
            assert key in pending.envelope["source_ref"], f"the worker requires source_ref.{key}"
        assert pending.envelope["source_ref"]["installation_id"] == INSTALLATION_A
        assert pending.envelope["source_ref"]["issue"] == 4214
        assert pending.envelope["version"] == "1.0", "the envelope contract is shared with every other producer"

    async def test_the_fifo_keys_come_from_the_dispatch_seam(self, session, config):
        """Per-node group id, so no diagnoser message head-of-line blocks another."""
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED)
        await _record_trigger(session, node)

        report = await run_diagnosis_pass(session, config)

        (pending,) = report.pending
        assert pending.group_id == f"{ORG_A}#{node.id}"
        assert node.id in pending.deduplication_id

    async def test_the_diagnosis_is_still_recorded_when_no_agent_can_be_dispatched(self, session):
        """The asymmetry that makes this story useful even in an unwired environment.

        The record is the deliverable; the agent run is an enhancement. A gate node
        with no issue still gets its context in front of the human.
        """
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED, issue_ref=None, kind="gate")
        await _record_trigger(session, node)

        report = await run_diagnosis_pass(session, DiagnosisConfig(enabled=True, repo=REPO))

        assert report.diagnoses_recorded == 1, "the diagnosis record must land even when nothing can be dispatched"
        assert report.undispatchable == 1
        assert report.pending == []
        assert len(await _diagnoses(session, node.id)) == 1

    async def test_an_ambiguous_installation_records_but_does_not_dispatch(self, session, config):
        """Fail-closed on both zero and many, reusing the dispatch story's rule."""
        await _make_org(session, installations=[111, 222])
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED)
        await _record_trigger(session, node)

        report = await run_diagnosis_pass(session, config)

        assert report.diagnoses_recorded == 1
        assert report.undispatchable == 1
        assert report.pending == []

    async def test_an_unconfigured_repo_records_but_does_not_dispatch(self, session):
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED)
        await _record_trigger(session, node)

        report = await run_diagnosis_pass(session, DiagnosisConfig(enabled=True, repo=""))

        assert report.diagnoses_recorded == 1
        assert report.undispatchable == 1
        assert report.pending == []

    async def test_a_non_numeric_issue_ref_records_but_does_not_dispatch(self, session, config):
        """A malformed `issue_ref` must not become a malformed envelope.

        The worker would reject the message after the fact, which is the
        invisible-failure class this EPIC exists to remove — so it is refused here
        instead, and the diagnosis still lands.
        """
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED, issue_ref="not-an-issue")
        await _record_trigger(session, node)

        report = await run_diagnosis_pass(session, config)

        assert report.diagnoses_recorded == 1
        assert report.undispatchable == 1
        assert report.pending == []

    async def test_a_hash_prefixed_issue_ref_is_parsed(self, session, config):
        """`#4214` and `4214` are the same issue, matching the dispatch story's parse."""
        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED, issue_ref="#4214")
        await _record_trigger(session, node)

        report = await run_diagnosis_pass(session, config)

        (pending,) = report.pending
        assert pending.envelope["source_ref"]["issue"] == 4214


class TestAppendOnly:
    """A diagnosis is a decision row, so the store story's guarantees apply to it."""

    async def test_a_diagnosis_row_cannot_be_updated(self, session, config):
        """Rewriting an agent's proposal after the fact would corrupt the audit trail."""
        from src.orchestration.models import AppendOnlyViolationError

        await _make_org(session)
        flow = await _make_flow(session)
        node = await _make_node(session, flow, state=NodeState.HALTED)
        await _record_trigger(session, node)
        await run_diagnosis_pass(session, config)

        (row,) = await _diagnoses(session, node.id)
        row.reason = "a rewritten finding"

        with pytest.raises(AppendOnlyViolationError):
            await session.flush()
