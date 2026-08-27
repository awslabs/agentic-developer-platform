"""Tests for the authoritative in-transaction compile of a loop proposal.

Issue #4199. The guarantees under test are the ones that make "agents never write
engine tables" structural rather than aspirational:

  - **AC-29**: `compile_proposal` re-validates. Calling it directly with an
    invalid document — bypassing the advisory CLI entirely, exactly as a hostile
    or careless caller would — raises and writes **nothing**. This is the test
    that proves the CLI is not the control.
  - **Atomicity**: a failure part-way through leaves no rows. Without this, a
    failure between the node insert and the accepted-plan insert would leave nodes
    on the graph for a plan nobody approved.
  - **Idempotency (R-NF2)**: compiling the same proposal twice does not
    double-insert.
  - **Tenant isolation**: a document declaring a different `org_id` than the
    approver's resolved org is rejected, not silently re-homed.
  - Compiled nodes all start in `NodeState.pending`.

The session fixture mirrors `test_models.py`'s: in-memory SQLite over
`Base.metadata.create_all`. `compile_proposal` never commits — the caller owns the
transaction — so these tests assert against the session's own view, which is what
the gate-approval route will see.
"""

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration.compile import (
    ApprovalContext,
    ProposalRejectedError,
    TenantMismatchError,
    compile_proposal,
    plan_hash,
)
from src.orchestration.models import (
    DecisionKind,
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationFlow,
    OrchestrationNode,
)
from src.orchestration.proposal import LoopProposal, ProposedEdge, ProposedNode
from src.orchestration.repository import OrchestrationRepository
from src.orchestration.state import ActorKind, NodeState
from src.shared.models.base import Base

ORG_A = "org-alpha"
ORG_B = "org-beta"
FLOW = "demo-flow"
SPEC_REVISION = "issue-4120-r1"


@pytest.fixture
async def session():
    """In-memory SQLite session with **working SAVEPOINTs**.

    The two `event.listens_for` hooks are not boilerplate and must not be dropped.
    `compile_proposal` wraps its inserts in `session.begin_nested()`, and by
    default pysqlite does **not** emit `BEGIN` — it manages transactions
    implicitly. With no enclosing transaction the SAVEPOINT becomes the outermost
    unit of work, so `RELEASE SAVEPOINT` effectively commits and a later
    `session.rollback()` has nothing left to undo. Atomicity tests would then pass
    for the wrong reason (rows genuinely gone from the savepoint's own rollback)
    while `test_compile_does_not_commit` failed, which is exactly what happened
    when this fixture was first written without the hooks.

    This is a **test-driver artifact, not a production defect**: asyncpg nests
    savepoints correctly, so the deployed path already behaves as the tests
    assert. The recipe below is SQLAlchemy's documented workaround for pysqlite's
    transactional DDL/DML handling — disable the driver's implicit transaction,
    then emit `BEGIN` explicitly.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _disable_pysqlite_implicit_begin(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _emit_explicit_begin(connection):
        connection.exec_driver_sql("BEGIN")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def approval():
    """The server-resolved approval context. `org_id` here is authoritative."""
    return ApprovalContext(
        org_id=ORG_A,
        actor_id="cognito-sub-123",
        actor_role="org_admin",
        actor_kind=ActorKind.HUMAN,
        reason="Reviewed the plan; wave structure looks right.",
    )


def address(node_ref: str, *, epic: str = "epic-1", wave: str = "wave-1") -> str:
    return f"{FLOW}/{epic}/{wave}/{node_ref}"


def valid_proposal(*, org_id: str = ORG_A, **overrides) -> LoopProposal:
    """A well-formed two-wave proposal: 3 nodes + eval per wave, chained."""
    payload = {
        "flow_slug": FLOW,
        "title": "Demo flow",
        "org_id": org_id,
        "spec_revision": SPEC_REVISION,
        "intent_ref": "4120",
        "nodes": [
            ProposedNode(address=address("story-a"), kind="story", title="Story A", issue_ref="4196"),
            ProposedNode(address=address("story-b"), kind="story", title="Story B"),
            ProposedNode(address=address("eval"), kind="eval", title="Wave 1 eval"),
            ProposedNode(address=address("gate", wave="wave-2"), kind="gate", title="Human gate"),
        ],
        "edges": [
            ProposedEdge(from_address=address("story-a"), to_address=address("eval")),
            ProposedEdge(from_address=address("story-b"), to_address=address("eval")),
            ProposedEdge(from_address=address("eval"), to_address=address("gate", wave="wave-2")),
        ],
    }
    payload.update(overrides)
    return LoopProposal(**payload)


def invalid_proposal(org_id: str = ORG_A) -> LoopProposal:
    """A document that fails validation four ways: a container smuggled in as a
    node, a duplicate address, a cycle, and a wave with no eval."""
    return LoopProposal(
        flow_slug=FLOW,
        title="Hostile flow",
        org_id=org_id,
        spec_revision=SPEC_REVISION,
        nodes=[
            ProposedNode(address=address("story-a"), kind="story", title="A"),
            ProposedNode(address=address("story-a"), kind="story", title="A again"),
            ProposedNode(address=address("sneaky-wave"), kind="wave", title="A container as a node"),
        ],
        edges=[
            ProposedEdge(from_address=address("story-a"), to_address=address("sneaky-wave")),
            ProposedEdge(from_address=address("sneaky-wave"), to_address=address("story-a")),
        ],
    )


async def count_rows(session: AsyncSession, model) -> int:
    return (await session.execute(sa.select(sa.func.count()).select_from(model.__table__))).scalar_one()


async def assert_graph_is_empty(session: AsyncSession) -> None:
    """Zero rows in every table a compile would have written.

    Asserted table by table rather than as a total so a failure names which table
    was left dirty.
    """
    for model in (OrchestrationNode, OrchestrationEdge, OrchestrationAcceptedPlan, OrchestrationDecision):
        assert await count_rows(session, model) == 0, f"{model.__tablename__} should have no rows"


class TestAC29AuthoritativeValidation:
    """AC-29: the authoritative check cannot be bypassed by skipping the CLI."""

    @pytest.mark.asyncio
    async def test_invalid_document_raises_when_the_cli_is_bypassed(self, session, approval):
        """The load-bearing test. A caller that never ran the advisory validator
        submits a malformed document straight to the compiler. It must be refused
        here, because this is the only code path that creates nodes."""
        with pytest.raises(ProposalRejectedError):
            await compile_proposal(session, invalid_proposal(), approval)

    @pytest.mark.asyncio
    async def test_invalid_document_writes_zero_rows(self, session, approval):
        """Refusal must leave no trace. A partially-compiled hostile plan would
        put nodes on the graph for a plan nobody approved."""
        with pytest.raises(ProposalRejectedError):
            await compile_proposal(session, invalid_proposal(), approval)
        await assert_graph_is_empty(session)

    @pytest.mark.asyncio
    async def test_no_flow_row_is_created_for_a_refused_proposal(self, session, approval):
        """The flow is resolved inside the savepoint, so even it must not survive
        — otherwise a hostile submission could litter the tenant with flows."""
        with pytest.raises(ProposalRejectedError):
            await compile_proposal(session, invalid_proposal(), approval)
        assert await count_rows(session, OrchestrationFlow) == 0

    @pytest.mark.asyncio
    async def test_raised_error_carries_every_violation(self, session, approval):
        """The caller reports back to the author, so it needs all of them — not
        just the first rule that tripped."""
        with pytest.raises(ProposalRejectedError) as caught:
            await compile_proposal(session, invalid_proposal(), approval)
        found = {violation.rule for violation in caught.value.violations}
        assert {"duplicate_address", "container_as_node", "cycle", "wave_eval_cardinality"} <= found

    @pytest.mark.asyncio
    async def test_a_container_kind_alone_is_enough_to_refuse(self, session, approval):
        """Narrow adversarial case: an otherwise perfect plan with one container
        node. The smuggled container must be refused on its own merits."""
        proposal = valid_proposal(
            nodes=[
                ProposedNode(address=address("story-a"), kind="story", title="A"),
                ProposedNode(address=address("eval"), kind="eval", title="Eval"),
                ProposedNode(address=address("wave-as-node"), kind="wave", title="Container"),
            ],
            edges=[],
        )
        with pytest.raises(ProposalRejectedError) as caught:
            await compile_proposal(session, proposal, approval)
        assert "container_as_node" in {v.rule for v in caught.value.violations}
        await assert_graph_is_empty(session)

    @pytest.mark.asyncio
    async def test_the_session_stays_usable_after_a_refusal(self, session, approval):
        """Refusal happens before any savepoint opens, so the caller's transaction
        must be unharmed — a gate-approval route has its own writes to make."""
        with pytest.raises(ProposalRejectedError):
            await compile_proposal(session, invalid_proposal(), approval)
        result = await compile_proposal(session, valid_proposal(), approval)
        assert result.nodes_created == 4


class TestTenantIsolation:
    """The org comes from server-resolved context, never from the document."""

    @pytest.mark.asyncio
    async def test_mismatched_org_is_rejected(self, session, approval):
        """Adversarial: a document claiming another tenant. Rejected, never
        silently re-homed to the approver's org."""
        with pytest.raises(TenantMismatchError):
            await compile_proposal(session, valid_proposal(org_id=ORG_B), approval)

    @pytest.mark.asyncio
    async def test_mismatched_org_writes_zero_rows(self, session, approval):
        with pytest.raises(TenantMismatchError):
            await compile_proposal(session, valid_proposal(org_id=ORG_B), approval)
        await assert_graph_is_empty(session)
        assert await count_rows(session, OrchestrationFlow) == 0

    @pytest.mark.asyncio
    async def test_tenant_mismatch_is_catchable_as_a_rejection(self, session, approval):
        """A caller that only cares "was this refused?" must catch it with the
        base class."""
        with pytest.raises(ProposalRejectedError):
            await compile_proposal(session, valid_proposal(org_id=ORG_B), approval)

    @pytest.mark.asyncio
    async def test_rows_land_in_the_approvers_org_not_the_documents(self, session, approval):
        """The document's declared org is only ever compared. When they agree,
        the org written is still the resolved one."""
        result = await compile_proposal(session, valid_proposal(), approval)
        nodes = await OrchestrationRepository(session).list_nodes(org_id=ORG_A, flow_id=result.flow_id)
        assert nodes and all(node.org_id == ORG_A for node in nodes)

    @pytest.mark.asyncio
    async def test_two_tenants_may_each_have_a_flow_with_the_same_slug(self, session):
        """Flow lookup is scoped to the tenant, so identical slugs in different
        orgs are different flows — not a collision that merges their graphs."""
        first = await compile_proposal(session, valid_proposal(org_id=ORG_A), ApprovalContext(org_id=ORG_A, actor_id="a", actor_role="org_admin"))
        second = await compile_proposal(session, valid_proposal(org_id=ORG_B), ApprovalContext(org_id=ORG_B, actor_id="b", actor_role="org_admin"))
        assert first.flow_id != second.flow_id


class TestAtomicity:
    @pytest.mark.asyncio
    async def test_failure_after_nodes_before_accepted_plan_rolls_back(self, session, approval, monkeypatch):
        """Injected failure at exactly the dangerous point. Nodes are already
        inserted; the accepted-plan row is not. Without the savepoint this leaves
        orphan nodes for a plan that was never accepted."""

        async def boom(*args, **kwargs):
            raise RuntimeError("injected failure before accepted-plan insert")

        monkeypatch.setattr(OrchestrationRepository, "record_accepted_plan", boom)

        with pytest.raises(RuntimeError, match="injected failure"):
            await compile_proposal(session, valid_proposal(), approval)

        await assert_graph_is_empty(session)

    @pytest.mark.asyncio
    async def test_failure_leaves_no_orphan_flow(self, session, approval, monkeypatch):
        async def boom(*args, **kwargs):
            raise RuntimeError("injected failure")

        monkeypatch.setattr(OrchestrationRepository, "record_accepted_plan", boom)
        with pytest.raises(RuntimeError):
            await compile_proposal(session, valid_proposal(), approval)
        assert await count_rows(session, OrchestrationFlow) == 0

    @pytest.mark.asyncio
    async def test_failure_during_edge_insert_rolls_back_nodes(self, session, approval, monkeypatch):
        """Edges are inserted after nodes; a failure there must not leave the
        nodes behind either."""

        async def boom(*args, **kwargs):
            raise RuntimeError("injected edge failure")

        monkeypatch.setattr(OrchestrationRepository, "add_edge", boom)
        with pytest.raises(RuntimeError):
            await compile_proposal(session, valid_proposal(), approval)
        await assert_graph_is_empty(session)

    @pytest.mark.asyncio
    async def test_compile_does_not_commit(self, session, approval):
        """The caller owns the transaction: gate approval commits this compile
        together with its own writes, so a commit here would split them."""
        await compile_proposal(session, valid_proposal(), approval)
        assert await count_rows(session, OrchestrationNode) == 4
        await session.rollback()
        assert await count_rows(session, OrchestrationNode) == 0

    @pytest.mark.asyncio
    async def test_pre_existing_work_in_the_transaction_survives_a_refusal(self, session, approval, monkeypatch):
        """The savepoint must roll back only the compile, not writes the caller
        made earlier in the same transaction."""
        repo = OrchestrationRepository(session)
        await repo.create_flow(org_id=ORG_A, slug="unrelated-flow", title="Unrelated")

        async def boom(*args, **kwargs):
            raise RuntimeError("injected failure")

        monkeypatch.setattr(OrchestrationRepository, "record_accepted_plan", boom)
        with pytest.raises(RuntimeError):
            await compile_proposal(session, valid_proposal(), approval)

        assert await count_rows(session, OrchestrationFlow) == 1


class TestCompileSuccess:
    @pytest.mark.asyncio
    async def test_all_nodes_and_edges_are_created(self, session, approval):
        result = await compile_proposal(session, valid_proposal(), approval)
        assert result.nodes_created == 4
        assert result.edges_created == 3
        assert await count_rows(session, OrchestrationNode) == 4
        assert await count_rows(session, OrchestrationEdge) == 3

    @pytest.mark.asyncio
    async def test_compiled_nodes_all_start_pending(self, session, approval):
        """The engine's tick advances `pending -> ready`; a node compiled into any
        other state would skip that gate."""
        result = await compile_proposal(session, valid_proposal(), approval)
        nodes = await OrchestrationRepository(session).list_nodes(org_id=ORG_A, flow_id=result.flow_id)
        assert nodes
        assert all(node.state == NodeState.PENDING.value for node in nodes)

    @pytest.mark.asyncio
    async def test_address_components_are_stored_denormalised(self, session, approval):
        """Containers are not rows, so the four address segments live on the node."""
        result = await compile_proposal(session, valid_proposal(), approval)
        nodes = {n.node_ref: n for n in await OrchestrationRepository(session).list_nodes(org_id=ORG_A, flow_id=result.flow_id)}
        story = nodes["story-a"]
        assert (story.epic_ref, story.wave_ref, story.node_ref) == ("epic-1", "wave-1", "story-a")
        assert nodes["gate"].wave_ref == "wave-2"

    @pytest.mark.asyncio
    async def test_node_kinds_and_issue_refs_are_carried_through(self, session, approval):
        result = await compile_proposal(session, valid_proposal(), approval)
        nodes = {n.node_ref: n for n in await OrchestrationRepository(session).list_nodes(org_id=ORG_A, flow_id=result.flow_id)}
        assert nodes["story-a"].kind == "story"
        assert nodes["story-a"].issue_ref == "4196"
        assert nodes["eval"].kind == "eval"
        assert nodes["gate"].kind == "gate"
        # Optional on the document, so it must stay NULL rather than becoming "".
        assert nodes["story-b"].issue_ref is None

    @pytest.mark.asyncio
    async def test_edges_resolve_to_the_created_node_ids(self, session, approval):
        """Edges are authored by address; compile must translate them to ids."""
        result = await compile_proposal(session, valid_proposal(), approval)
        edges = await OrchestrationRepository(session).list_edges(org_id=ORG_A, flow_id=result.flow_id)
        pairs = {(edge.from_node_id, edge.to_node_id) for edge in edges}
        assert (result.node_ids[address("story-a")], result.node_ids[address("eval")]) in pairs
        assert (result.node_ids[address("eval")], result.node_ids[address("gate", wave="wave-2")]) in pairs

    @pytest.mark.asyncio
    async def test_accepted_plan_stores_the_document_verbatim(self, session, approval):
        """Acceptance must not depend on an external artifact staying unedited."""
        proposal = valid_proposal()
        result = await compile_proposal(session, proposal, approval)
        plan = await OrchestrationRepository(session).get_accepted_plan(org_id=ORG_A, flow_id=result.flow_id)
        assert plan is not None
        assert LoopProposal.model_validate(plan.plan_document) == proposal
        assert plan.plan_hash == plan_hash(proposal)

    @pytest.mark.asyncio
    async def test_spec_revision_is_recoverable_from_the_stored_document(self, session, approval):
        """No dedicated column: the document carries it, and the document is
        stored verbatim. A column would be a second copy of one value."""
        result = await compile_proposal(session, valid_proposal(), approval)
        plan = await OrchestrationRepository(session).get_accepted_plan(org_id=ORG_A, flow_id=result.flow_id)
        assert plan.plan_document["spec_revision"] == SPEC_REVISION

    @pytest.mark.asyncio
    async def test_decision_is_recorded_with_full_attribution(self, session, approval):
        result = await compile_proposal(session, valid_proposal(), approval)
        decisions = await OrchestrationRepository(session).list_decisions(org_id=ORG_A, flow_id=result.flow_id)
        assert len(decisions) == 1
        decision = decisions[0]
        assert decision.kind == DecisionKind.PLAN_ACCEPTED.value
        assert decision.actor_id == "cognito-sub-123"
        assert decision.actor_role == "org_admin"
        assert decision.actor_kind == ActorKind.HUMAN.value
        assert decision.reason == approval.reason

    @pytest.mark.asyncio
    async def test_accepted_plan_points_at_its_decision(self, session, approval):
        """`accepted_by_decision_id` is nullable only because one row must be
        inserted first — it is not optional information."""
        result = await compile_proposal(session, valid_proposal(), approval)
        plan = await OrchestrationRepository(session).get_accepted_plan(org_id=ORG_A, flow_id=result.flow_id)
        assert plan.accepted_by_decision_id == result.decision_id

    @pytest.mark.asyncio
    async def test_first_plan_is_version_one(self, session, approval):
        result = await compile_proposal(session, valid_proposal(), approval)
        assert result.plan_version == 1

    @pytest.mark.asyncio
    async def test_flow_metadata_comes_from_the_proposal(self, session, approval):
        result = await compile_proposal(session, valid_proposal(), approval)
        flow = await OrchestrationRepository(session).get_flow(org_id=ORG_A, flow_id=result.flow_id)
        assert (flow.slug, flow.title, flow.intent_ref) == (FLOW, "Demo flow", "4120")

    @pytest.mark.asyncio
    async def test_service_actor_must_declare_itself(self, session):
        """Approving is a human act by default; a service actor is recorded as
        such rather than passing as a human."""
        approval = ApprovalContext(org_id=ORG_A, actor_id="engine", actor_role="service", actor_kind=ActorKind.SERVICE)
        result = await compile_proposal(session, valid_proposal(), approval)
        decisions = await OrchestrationRepository(session).list_decisions(org_id=ORG_A, flow_id=result.flow_id)
        assert decisions[0].actor_kind == ActorKind.SERVICE.value


class TestIdempotency:
    """R-NF2: a retried approval converges rather than duplicating or erroring."""

    @pytest.mark.asyncio
    async def test_compiling_the_same_proposal_twice_does_not_double_insert(self, session, approval):
        proposal = valid_proposal()
        await compile_proposal(session, proposal, approval)
        await compile_proposal(session, proposal, approval)

        assert await count_rows(session, OrchestrationNode) == 4
        assert await count_rows(session, OrchestrationEdge) == 3
        assert await count_rows(session, OrchestrationAcceptedPlan) == 1
        assert await count_rows(session, OrchestrationDecision) == 1
        assert await count_rows(session, OrchestrationFlow) == 1

    @pytest.mark.asyncio
    async def test_second_compile_reports_already_compiled(self, session, approval):
        """A caller reporting "N nodes created" must not claim to have created a
        graph it merely found."""
        proposal = valid_proposal()
        first = await compile_proposal(session, proposal, approval)
        second = await compile_proposal(session, proposal, approval)

        assert first.already_compiled is False
        assert second.already_compiled is True
        assert second.nodes_created == 0
        assert second.edges_created == 0
        assert second.plan_version == first.plan_version
        assert second.flow_id == first.flow_id

    @pytest.mark.asyncio
    async def test_already_compiled_result_still_addresses_the_nodes(self, session, approval):
        """A retry's caller needs the node ids just as much as a first compile's."""
        proposal = valid_proposal()
        first = await compile_proposal(session, proposal, approval)
        second = await compile_proposal(session, proposal, approval)
        assert second.node_ids == first.node_ids

    @pytest.mark.asyncio
    async def test_an_amended_plan_writes_a_new_version(self, session, approval):
        """A genuinely different document is an amendment, not a retry: it must
        supersede rather than be mistaken for the plan already in force."""
        await compile_proposal(session, valid_proposal(), approval)

        amended = valid_proposal(
            nodes=[
                ProposedNode(address=address("story-a"), kind="story", title="Story A", issue_ref="4196"),
                ProposedNode(address=address("story-b"), kind="story", title="Story B"),
                ProposedNode(address=address("story-c"), kind="story", title="Story C — added"),
                ProposedNode(address=address("eval"), kind="eval", title="Wave 1 eval"),
                ProposedNode(address=address("gate", wave="wave-2"), kind="gate", title="Human gate"),
            ],
            edges=[
                ProposedEdge(from_address=address("story-a"), to_address=address("eval")),
                ProposedEdge(from_address=address("story-b"), to_address=address("eval")),
                ProposedEdge(from_address=address("story-c"), to_address=address("eval")),
                ProposedEdge(from_address=address("eval"), to_address=address("gate", wave="wave-2")),
            ],
        )
        result = await compile_proposal(session, amended, approval)

        assert result.plan_version == 2
        assert result.already_compiled is False
        # Only the genuinely new node and edge are inserted; the rest are reused.
        assert result.nodes_created == 1
        assert result.edges_created == 1
        assert await count_rows(session, OrchestrationNode) == 5

    @pytest.mark.asyncio
    async def test_amendment_supersedes_the_prior_version(self, session, approval):
        """The plan in force at a past gate must stay readable."""
        await compile_proposal(session, valid_proposal(), approval)
        amended = valid_proposal(title="Demo flow (amended)")
        await compile_proposal(session, amended, approval)

        repo = OrchestrationRepository(session)
        versions = await repo.list_plan_versions(org_id=ORG_A, flow_id=(await repo.list_flows(org_id=ORG_A))[0].id)
        assert len(versions) == 2
        assert versions[0].superseded_at is not None
        assert versions[1].superseded_at is None

    @pytest.mark.asyncio
    async def test_reordered_but_identical_document_is_still_a_retry(self, session, approval):
        """The hash is canonicalised, so idempotency does not depend on JSON key
        ordering — a re-serialised resubmission is the same plan."""
        proposal = valid_proposal()
        await compile_proposal(session, proposal, approval)

        reordered = LoopProposal.model_validate(dict(reversed(list(proposal.model_dump(mode="json").items()))))
        result = await compile_proposal(session, reordered, approval)
        assert result.already_compiled is True


class TestPlanHash:
    def test_identical_documents_hash_identically(self):
        assert plan_hash(valid_proposal()) == plan_hash(valid_proposal())

    def test_key_order_does_not_change_the_hash(self):
        proposal = valid_proposal()
        reordered = LoopProposal.model_validate(dict(reversed(list(proposal.model_dump(mode="json").items()))))
        assert plan_hash(reordered) == plan_hash(proposal)

    def test_a_changed_node_changes_the_hash(self):
        assert plan_hash(valid_proposal()) != plan_hash(valid_proposal(title="Different"))

    def test_a_changed_declared_org_changes_the_hash(self):
        """The hash answers "is this the same document?", and a document differing
        only in declared tenant is not the same document."""
        assert plan_hash(valid_proposal(org_id=ORG_A)) != plan_hash(valid_proposal(org_id=ORG_B))

    def test_hash_is_a_sha256_hex_digest(self):
        digest = plan_hash(valid_proposal())
        assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
