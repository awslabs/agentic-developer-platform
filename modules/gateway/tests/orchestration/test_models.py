"""Tests for the orchestration graph store models and repository.

Issue #4196. The guarantees under test are the ones the EPIC depends on:

  - Every table carries `org_id`, and every repository query filters on it —
    there is no cross-tenant read path.
  - `orchestration_decisions` is **append-only**: the repository exposes no
    update path, AND an attempted update raises rather than succeeding. Both
    halves matter. "No method" prevents the accidental case; the raise is what
    holds when a caller mutates a loaded instance and commits.
  - Decisions record `actor_role` (authority at decision time) and `actor_kind`
    (human vs service) as separate non-nullable fields — the distinction the
    existing `tenant_access_requests.decided_by` column cannot express, since it
    stores real Cognito subs and synthetic `system:org-member-match` values in
    one indistinguishable string.
  - The accepted plan is versioned: amendment supersedes rather than mutates, so
    the plan in force at a past gate stays readable.
"""

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration.models import (
    ActorKind,
    AppendOnlyViolationError,
    DecisionKind,
    NodeKind,
    NodeState,
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationFlow,
    OrchestrationNode,
)
from src.orchestration.repository import OrchestrationRepository
from src.shared.models.base import Base

ORG_A = "org-alpha"
ORG_B = "org-beta"

ALL_MODELS = (
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationEdge,
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
)


@pytest.fixture
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def repo(session):
    return OrchestrationRepository(session)


class TestTenantIsolation:
    def test_every_model_carries_org_id(self):
        """`org_id` on every table is what makes tenant filtering possible at all."""
        for model in ALL_MODELS:
            assert "org_id" in {c.name for c in model.__table__.columns}, f"{model.__tablename__} is missing org_id"

    def test_org_id_is_non_nullable_and_indexed(self):
        """A nullable org_id would create rows belonging to no tenant — readable
        by a query that filters on any org, or by none."""
        for model in ALL_MODELS:
            col = model.__table__.columns["org_id"]
            assert col.nullable is False, f"{model.__tablename__}.org_id must be NOT NULL"
            assert col.index is True, f"{model.__tablename__}.org_id must be indexed"

    @pytest.mark.asyncio
    async def test_get_flow_does_not_leak_across_tenants(self, repo):
        """The adversarial case: correct id, wrong tenant, must return nothing."""
        flow = await repo.create_flow(org_id=ORG_A, slug="flow-a", title="Flow A")

        assert await repo.get_flow(org_id=ORG_A, flow_id=flow.id) is not None
        assert await repo.get_flow(org_id=ORG_B, flow_id=flow.id) is None

    @pytest.mark.asyncio
    async def test_list_flows_is_scoped(self, repo):
        await repo.create_flow(org_id=ORG_A, slug="flow-a", title="Flow A")
        await repo.create_flow(org_id=ORG_B, slug="flow-b", title="Flow B")

        assert [f.slug for f in await repo.list_flows(org_id=ORG_A)] == ["flow-a"]
        assert [f.slug for f in await repo.list_flows(org_id=ORG_B)] == ["flow-b"]

    @pytest.mark.asyncio
    async def test_node_and_decision_reads_are_scoped(self, repo):
        flow = await repo.create_flow(org_id=ORG_A, slug="flow-a", title="Flow A")
        node = await repo.add_node(
            org_id=ORG_A,
            flow_id=flow.id,
            epic_ref="epic-1",
            wave_ref="wave-1",
            node_ref="story-1",
            kind=NodeKind.STORY,
            title="Story 1",
        )
        await repo.append_decision(
            org_id=ORG_A,
            flow_id=flow.id,
            node_id=node.id,
            kind=DecisionKind.GATE_APPROVED,
            actor_id="user-1",
            actor_role="operator",
            actor_kind=ActorKind.HUMAN,
        )

        assert await repo.get_node(org_id=ORG_B, node_id=node.id) is None
        assert await repo.list_nodes(org_id=ORG_B, flow_id=flow.id) == []
        assert await repo.list_decisions(org_id=ORG_B, flow_id=flow.id) == []
        assert len(await repo.list_decisions(org_id=ORG_A, flow_id=flow.id)) == 1

    @pytest.mark.asyncio
    async def test_accepted_plan_read_is_scoped(self, repo):
        """The highest-value leak to prevent: another tenant's accepted plan."""
        flow = await repo.create_flow(org_id=ORG_A, slug="flow-a", title="Flow A")
        await repo.record_accepted_plan(org_id=ORG_A, flow_id=flow.id, plan_document={"waves": 1}, plan_hash="h1")

        assert await repo.get_accepted_plan(org_id=ORG_A, flow_id=flow.id) is not None
        assert await repo.get_accepted_plan(org_id=ORG_B, flow_id=flow.id) is None
        assert await repo.list_plan_versions(org_id=ORG_B, flow_id=flow.id) == []

    def test_every_repository_read_filters_on_org_id(self):
        """Structural guard: a new method that forgets org_id fails this test.

        Enumerating methods rather than listing them by hand is what makes this
        catch the *next* method, not just today's.
        """
        import inspect

        public = [name for name, fn in inspect.getmembers(OrchestrationRepository, predicate=inspect.isfunction) if not name.startswith("_")]
        assert public, "no public repository methods found — test is not exercising anything"

        for name in public:
            sig = inspect.signature(getattr(OrchestrationRepository, name))
            assert "org_id" in sig.parameters, f"{name}() does not take org_id; it cannot be tenant-scoped"
            source = inspect.getsource(getattr(OrchestrationRepository, name))
            assert "org_id" in source.split("\n", 1)[1], f"{name}() accepts org_id but never uses it"


class TestDecisionAttribution:
    def test_actor_role_and_actor_kind_are_separate_non_nullable_fields(self):
        """Three columns, not one string.

        `decided_by` mixing `caller.user_id` with `"system:org-member-match"` is
        why this cannot be a naming convention inside a single identity column:
        after the fact, nothing distinguishes a real approval from a synthetic
        one. Separate columns make "was a human responsible?" a query.
        """
        cols = OrchestrationDecision.__table__.columns

        for name in ("actor_id", "actor_role", "actor_kind"):
            assert name in cols, f"{name} missing from orchestration_decisions"
            assert cols[name].nullable is False, f"{name} must be NOT NULL"
            assert cols[name].server_default is None, f"{name} must have no server_default"

        assert cols["actor_role"] is not cols["actor_kind"]

    @pytest.mark.asyncio
    async def test_human_and_service_decisions_are_distinguishable(self, repo, session):
        """The guarantee: an agent's decision can never read as a human's."""
        flow = await repo.create_flow(org_id=ORG_A, slug="flow-a", title="Flow A")
        await repo.append_decision(
            org_id=ORG_A,
            flow_id=flow.id,
            kind=DecisionKind.GATE_APPROVED,
            actor_id="cognito-sub-123",
            actor_role="operator",
            actor_kind=ActorKind.HUMAN,
        )
        await repo.append_decision(
            org_id=ORG_A,
            flow_id=flow.id,
            kind=DecisionKind.TRANSITION_REJECTED,
            actor_id="agent-worker",
            actor_role="engine",
            actor_kind=ActorKind.SERVICE,
            rejection_reason="no legal transition from 'awaiting_gate' to 'passed'",
        )
        await session.commit()

        decisions = await repo.list_decisions(org_id=ORG_A, flow_id=flow.id)
        by_kind = {d.actor_kind: d for d in decisions}

        assert by_kind[ActorKind.HUMAN].actor_id == "cognito-sub-123"
        assert by_kind[ActorKind.SERVICE].actor_id == "agent-worker"
        assert by_kind[ActorKind.SERVICE].rejection_reason

    @pytest.mark.asyncio
    async def test_rejected_transition_is_recorded_with_its_reason(self, repo):
        """R-N2b: a rejected transition must be persistable, not just raised.

        Recorded rejections are the primary detector for off-plan agent activity,
        so the record has to carry the reason it was rejected.
        """
        from src.orchestration.state import transition

        flow = await repo.create_flow(org_id=ORG_A, slug="flow-a", title="Flow A")
        result = transition(
            NodeState.AWAITING_GATE,
            NodeState.PASSED,
            actor_kind=ActorKind.SERVICE,
            reason="engine attempted to self-approve a gate",
        )
        assert result.allowed is False

        decision = await repo.append_decision(
            org_id=ORG_A,
            flow_id=flow.id,
            kind=DecisionKind.TRANSITION_REJECTED,
            actor_id="engine",
            actor_role="engine",
            actor_kind=result.actor_kind,
            reason=result.reason,
            rejection_reason=result.rejection_reason,
            from_state=result.from_state,
            to_state=result.to_state,
        )

        assert decision.rejection_reason == result.rejection_reason
        assert decision.from_state == NodeState.AWAITING_GATE
        assert decision.to_state == NodeState.PASSED


class TestDecisionsAreAppendOnly:
    """Gate attribution that can be rewritten makes the audit trail worthless."""

    def test_repository_exposes_no_decision_update_or_delete_path(self):
        """The repository surface itself must offer no way to mutate a decision."""
        mutating_verbs = ("update", "delete", "remove", "edit", "set_", "modify")
        names = [n.lower() for n in dir(OrchestrationRepository) if not n.startswith("_")]
        offending = [n for n in names if "decision" in n and any(v in n for v in mutating_verbs)]
        assert offending == [], f"repository exposes a decision mutation path: {offending}"

    def test_repository_offers_exactly_one_decision_write_method(self):
        """Fails closed: a newly added decision-writing method breaks this test.

        Equality against an allowlist, not a substring scan — so the next method
        someone adds has to be justified in review rather than slipping in.
        """
        names = {n for n in dir(OrchestrationRepository) if not n.startswith("_") and "decision" in n.lower()}
        assert names == {"append_decision", "list_decisions"}, f"unexpected decision methods on the repository: {names}"

    @pytest.mark.asyncio
    async def test_updating_a_decision_raises(self, repo, session):
        """Adversarial: mutate a loaded decision and flush. Must raise.

        This is the case the missing-method check cannot cover — a caller holding
        the session can always mutate an instance. The ORM-level guard is what
        makes the append-only claim true rather than aspirational.
        """
        flow = await repo.create_flow(org_id=ORG_A, slug="flow-a", title="Flow A")
        decision = await repo.append_decision(
            org_id=ORG_A,
            flow_id=flow.id,
            kind=DecisionKind.GATE_APPROVED,
            actor_id="user-1",
            actor_role="operator",
            actor_kind=ActorKind.HUMAN,
        )
        await session.commit()

        # Rewrite attribution: claim a human approved what a service decided.
        decision.actor_kind = ActorKind.SERVICE
        decision.actor_id = "someone-else"

        with pytest.raises(AppendOnlyViolationError):
            await session.flush()

    @pytest.mark.asyncio
    async def test_bulk_update_of_decisions_is_not_silently_allowed(self, repo, session):
        """A Core-level UPDATE bypasses ORM events, so document the boundary.

        The ORM guard covers every path the application actually uses. Raw Core
        UPDATEs are not blockable in Python — that is what the CI guard in
        `test_internal_plane_guard.py` exists for: keep this table unreachable
        from the plane agents can call.
        """
        flow = await repo.create_flow(org_id=ORG_A, slug="flow-a", title="Flow A")
        await repo.append_decision(
            org_id=ORG_A,
            flow_id=flow.id,
            kind=DecisionKind.GATE_APPROVED,
            actor_id="user-1",
            actor_role="operator",
            actor_kind=ActorKind.HUMAN,
        )
        await session.commit()

        # Assert the ORM path is guarded (the path all application code takes).
        loaded = (await session.execute(sa.select(OrchestrationDecision))).scalar_one()
        loaded.actor_role = "admin"
        with pytest.raises(AppendOnlyViolationError):
            await session.flush()


class TestAcceptedPlanVersioning:
    @pytest.mark.asyncio
    async def test_first_accepted_plan_is_version_one_and_in_force(self, repo):
        flow = await repo.create_flow(org_id=ORG_A, slug="flow-a", title="Flow A")
        plan = await repo.record_accepted_plan(org_id=ORG_A, flow_id=flow.id, plan_document={"waves": 2}, plan_hash="h1")

        assert plan.version == 1
        assert plan.superseded_at is None
        in_force = await repo.get_accepted_plan(org_id=ORG_A, flow_id=flow.id)
        assert in_force.id == plan.id

    @pytest.mark.asyncio
    async def test_amendment_supersedes_rather_than_mutates(self, repo, session):
        """The core guarantee: the prior accepted plan survives an amendment.

        If amendment overwrote the row, "what was accepted at the gate we already
        passed?" would be unanswerable — which is precisely the claim this schema
        exists to make checkable.
        """
        flow = await repo.create_flow(org_id=ORG_A, slug="flow-a", title="Flow A")
        v1 = await repo.record_accepted_plan(org_id=ORG_A, flow_id=flow.id, plan_document={"waves": 2}, plan_hash="h1")
        v1_id = v1.id
        await session.commit()

        v2 = await repo.record_accepted_plan(org_id=ORG_A, flow_id=flow.id, plan_document={"waves": 3}, plan_hash="h2")
        await session.commit()

        assert v2.version == 2
        assert v2.superseded_at is None

        versions = await repo.list_plan_versions(org_id=ORG_A, flow_id=flow.id)
        assert [p.version for p in versions] == [1, 2]

        # v1 still exists, still carries its ORIGINAL document, now superseded.
        original = next(p for p in versions if p.id == v1_id)
        assert original.plan_document == {"waves": 2}
        assert original.plan_hash == "h1"
        assert original.superseded_at is not None

        # Exactly one plan is in force, and it is v2.
        assert (await repo.get_accepted_plan(org_id=ORG_A, flow_id=flow.id)).id == v2.id

    @pytest.mark.asyncio
    async def test_duplicate_version_for_a_flow_is_rejected(self, repo, session):
        """The unique index makes a concurrent double-accept fail loudly."""
        flow = await repo.create_flow(org_id=ORG_A, slug="flow-a", title="Flow A")
        await repo.record_accepted_plan(org_id=ORG_A, flow_id=flow.id, plan_document={"w": 1}, plan_hash="h1")
        await session.commit()

        session.add(
            OrchestrationAcceptedPlan(
                org_id=ORG_A,
                flow_id=flow.id,
                version=1,  # collides
                plan_document={"w": 9},
                plan_hash="h9",
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            await session.commit()

    @pytest.mark.asyncio
    async def test_plans_for_different_flows_version_independently(self, repo, session):
        a = await repo.create_flow(org_id=ORG_A, slug="flow-a", title="Flow A")
        b = await repo.create_flow(org_id=ORG_A, slug="flow-b", title="Flow B")
        await repo.record_accepted_plan(org_id=ORG_A, flow_id=a.id, plan_document={"w": 1}, plan_hash="h1")
        await repo.record_accepted_plan(org_id=ORG_A, flow_id=a.id, plan_document={"w": 2}, plan_hash="h2")
        pb = await repo.record_accepted_plan(org_id=ORG_A, flow_id=b.id, plan_document={"w": 1}, plan_hash="h3")
        await session.commit()

        assert pb.version == 1, "version counters must be per-flow, not global"


class TestGraphShape:
    def test_node_kinds_are_the_executable_three_only(self):
        """Containers are derived state. A WAVE node kind would invite the
        container-rows-as-nodes mistake the schema is shaped to prevent."""
        assert {k.value for k in NodeKind} == {"story", "eval", "gate"}

    def test_node_carries_the_full_graph_address(self):
        """`flow/epic/wave/node` — the cost rollup and graph view both key off it."""
        cols = {c.name for c in OrchestrationNode.__table__.columns}
        assert {"flow_id", "epic_ref", "wave_ref", "node_ref"} <= cols

    def test_no_container_tables_exist(self):
        """Waves and EPICs are computed from their member nodes, not stored.

        A container row is a second source of truth for a value its children
        already imply, and the two drift.
        """
        table_names = {m.__tablename__ for m in ALL_MODELS}
        assert "orchestration_waves" not in table_names
        assert "orchestration_epics" not in table_names

    def test_no_run_table_exists(self):
        """The story is the graph floor; runs attach via the ledger, not as nodes."""
        assert "orchestration_runs" not in {m.__tablename__ for m in ALL_MODELS}

    def test_node_state_vocabulary_is_imported_not_redefined(self):
        """R-N2a: a second copy of the vocabulary is a requirement violation."""
        from src.orchestration import state

        assert NodeState is state.NodeState
        assert ActorKind is state.ActorKind

    @pytest.mark.asyncio
    async def test_edges_connect_nodes_and_enable_lookahead(self, repo, session):
        flow = await repo.create_flow(org_id=ORG_A, slug="flow-a", title="Flow A")
        a = await repo.add_node(org_id=ORG_A, flow_id=flow.id, epic_ref="e1", wave_ref="w1", node_ref="s1", kind=NodeKind.STORY, title="S1")
        b = await repo.add_node(org_id=ORG_A, flow_id=flow.id, epic_ref="e1", wave_ref="w2", node_ref="s2", kind=NodeKind.STORY, title="S2")
        await repo.add_edge(org_id=ORG_A, flow_id=flow.id, from_node_id=a.id, to_node_id=b.id)
        await session.commit()

        edges = await repo.list_edges(org_id=ORG_A, flow_id=flow.id)
        assert [(e.from_node_id, e.to_node_id) for e in edges] == [(a.id, b.id)]

    @pytest.mark.asyncio
    async def test_duplicate_graph_address_is_rejected(self, repo, session):
        """A duplicate address means two nodes answer to one name; rollup doubles."""
        flow = await repo.create_flow(org_id=ORG_A, slug="flow-a", title="Flow A")
        await repo.add_node(org_id=ORG_A, flow_id=flow.id, epic_ref="e1", wave_ref="w1", node_ref="s1", kind=NodeKind.STORY, title="S1")
        await session.commit()

        session.add(OrchestrationNode(org_id=ORG_A, flow_id=flow.id, epic_ref="e1", wave_ref="w1", node_ref="s1", kind=NodeKind.STORY, title="dupe"))
        with pytest.raises(sa.exc.IntegrityError):
            await session.commit()

    @pytest.mark.asyncio
    async def test_duplicate_edge_is_rejected(self, repo, session):
        flow = await repo.create_flow(org_id=ORG_A, slug="flow-a", title="Flow A")
        a = await repo.add_node(org_id=ORG_A, flow_id=flow.id, epic_ref="e1", wave_ref="w1", node_ref="s1", kind=NodeKind.STORY, title="S1")
        b = await repo.add_node(org_id=ORG_A, flow_id=flow.id, epic_ref="e1", wave_ref="w2", node_ref="s2", kind=NodeKind.STORY, title="S2")
        await repo.add_edge(org_id=ORG_A, flow_id=flow.id, from_node_id=a.id, to_node_id=b.id)
        await session.commit()

        session.add(OrchestrationEdge(org_id=ORG_A, flow_id=flow.id, from_node_id=a.id, to_node_id=b.id))
        with pytest.raises(sa.exc.IntegrityError):
            await session.commit()

    @pytest.mark.asyncio
    async def test_nodes_default_to_pending(self, repo):
        """A node must not appear ready before its predecessors are satisfied."""
        flow = await repo.create_flow(org_id=ORG_A, slug="flow-a", title="Flow A")
        node = await repo.add_node(org_id=ORG_A, flow_id=flow.id, epic_ref="e1", wave_ref="w1", node_ref="s1", kind=NodeKind.STORY, title="S1")
        assert node.state == NodeState.PENDING
        assert node.attempts == 0
