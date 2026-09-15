"""Tests for draft registration of a compiled loop proposal (issue #4528).

The story's promise is narrow and load-bearing: an authoring agent may put a plan
*on the graph*, and nothing it does can make that plan run. So the tests here are
organised around the issue's three named bug classes, and each group is written to
fail if the corresponding structural guard is weakened rather than merely if the
happy path breaks.

**"Registration auto-starts execution"** (`TestADraftIsInert`). Asserted three
independent ways, because one assertion is one assertion somebody can delete:

1. The decision row registration writes is **not** of an approval kind, so
   `dispatch_pass` has nothing to root a chain in. Asserted against
   `genesis.APPROVAL_DECISION_KINDS` itself, not against a restated literal.
2. Many ticks in a row move **nothing**. This is the issue's "draft state
   dispatches nothing across many ticks" criterion, run against the real
   `run_tick` and the real `run_dispatch_pass` rather than a stub, because the
   claim is about those two functions' behaviour.
3. The acceptance gate **dominates** the graph: every other node has an
   unsatisfied predecessor while it is unanswered, so nothing behind it is even a
   tick candidate.

And the release: a human answering that gate through the *existing* #4527 seam
(`apply_gate_answer_for_context`, unchanged by this story) writes `GATE_APPROVED`,
after which dispatch does arm. A draft that could never be accepted would satisfy
every inertness test above and be useless, so the accept path is asserted here too
— through the shared adapter, against real `users` / `tenant_memberships` rows and
the real `AccessControl`, so it is the actual authorization that is exercised.

**"Gateless proposal free-runs after accept"** (`TestAutonomyDefault`). A proposal
declaring no gate gets one at every wave boundary; a proposal declaring its own
gates is passed through untouched; the flag turns the behaviour off and nothing
else changes. The boundary assertion is deliberately about *paths*, not node
counts: a gate that exists beside a boundary the work flows straight past is
decoration, so the test asserts there is no edge out of a gated wave that skips
the gate.

**"Compile failure kills the AIDLC run"** is a worker-side property and is tested
worker-side. What belongs here is the half the worker depends on: a rejected
document writes zero rows (so a fail-soft retry has nothing to collide with), and
an identical resubmission is reported as already-registered rather than compiling a
second plan.

Tenant isolation is asserted at the route (`TestRouteAuthorization`): the document's
`org_id` is compared, never substituted, and a caller without `PLAN_DRAFT` is
refused before anything is read.

The session fixture is `test_compile.py`'s, including its two pysqlite hooks —
they are load-bearing, not boilerplate: without them `begin_nested()` becomes the
outermost unit of work and `compile_proposal`'s savepoint is not exercised at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.access_control import AccessControl
from src.admin.config import AdminRole, Permission
from src.orchestration.adapters.github_comments import (
    GateAnswerStatus,
    InputPath,
    apply_gate_answer_for_context,
)
from src.orchestration.compile import (
    ApprovalContext,
    NonApprovalSupersedeError,
    ProposalRejectedError,
    TenantMismatchError,
    compile_proposal,
    plan_hash,
)
from src.orchestration.dispatch_pass import DispatchPassConfig, run_dispatch_pass
from src.orchestration.genesis import APPROVAL_DECISION_KINDS
from src.orchestration.models import (
    DecisionKind,
    NodeKind,
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationFlow,
    OrchestrationNode,
)
from src.orchestration.proposal import LoopProposal, ProposedEdge, ProposedNode
from src.orchestration.registration import (
    ACCEPTANCE_GATE_REF,
    AUTONOMY_FLAG_ENV,
    WAVE_GATE_REF,
    DraftFlowConflictError,
    gate_every_wave_enabled,
    insert_wave_gates,
    register_draft_proposal,
    transform_for_registration,
)
from src.orchestration.repository import OrchestrationRepository
from src.orchestration.state import ActorKind, NodeState
from src.orchestration.tick import run_tick
from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.schemas.auth import TokenContext

ORG_A = "org-alpha"
ORG_B = "org-beta"
FLOW = "delivery-loop"
SPEC_REVISION = "issue-4120-r1"
AGENT_USER_ID = "cognito-sub-aidlc-worker"
INSTALLATION_A = 42

ROUTE = "/orchestration/flows/drafts"


@pytest.fixture(autouse=True)
def autonomy_default_unset(monkeypatch):
    """Every test starts with the flag **unset**, i.e. the shipped default.

    Autouse because the alternative is that a test which sets the variable leaks it
    into whatever runs next, and the leak would silently disable the autonomy
    default in tests that exist to assert it. Unset rather than set-to-true so the
    default under test is the real one an unconfigured environment gets.
    """
    monkeypatch.delenv(AUTONOMY_FLAG_ENV, raising=False)


@pytest.fixture
async def session():
    """In-memory SQLite session with working SAVEPOINTs. See module docstring."""
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
def registrar():
    """The server-resolved context the route builds for a registering agent.

    `actor_kind` is SERVICE, which is what an agent principal really is. It matters
    beyond bookkeeping: `resolve_engine_genesis` refuses a service-actor decision
    outright, so even a decision *kind* mistake could not turn this row into a
    human root.
    """
    return ApprovalContext(
        org_id=ORG_A,
        actor_id=AGENT_USER_ID,
        actor_role=AdminRole.MEMBER.value,
        actor_kind=ActorKind.SERVICE,
        reason="AIDLC authoring completed; registering the compiled loop proposal.",
    )


def address(node_ref: str, *, epic: str = "epic-1", wave: str = "wave-1") -> str:
    return f"{FLOW}/{epic}/{wave}/{node_ref}"


def gateless_proposal(*, org_id: str = ORG_A, **overrides) -> LoopProposal:
    """Two waves, chained, **no gate anywhere** — what an AIDLC author emits.

    This is the document the autonomy default exists for, so it is the default
    fixture: the shipped behaviour is what most tests should be exercising.
    """
    payload = {
        "flow_slug": FLOW,
        "title": "Delivery loop",
        "org_id": org_id,
        "spec_revision": SPEC_REVISION,
        "intent_ref": "4120",
        "nodes": [
            ProposedNode(address=address("story-a"), kind="story", title="Story A", issue_ref="4527"),
            ProposedNode(address=address("story-b"), kind="story", title="Story B", issue_ref="4528"),
            ProposedNode(address=address("eval-w1"), kind="eval", title="Wave 1 eval"),
            ProposedNode(address=address("story-c", wave="wave-2"), kind="story", title="Story C", issue_ref="4529"),
            ProposedNode(address=address("eval-w2", wave="wave-2"), kind="eval", title="Wave 2 eval"),
        ],
        "edges": [
            ProposedEdge(from_address=address("story-a"), to_address=address("eval-w1")),
            ProposedEdge(from_address=address("story-b"), to_address=address("eval-w1")),
            ProposedEdge(from_address=address("eval-w1"), to_address=address("story-c", wave="wave-2")),
            ProposedEdge(from_address=address("story-c", wave="wave-2"), to_address=address("eval-w2", wave="wave-2")),
        ],
    }
    payload.update(overrides)
    return LoopProposal(**payload)


def author_gated_proposal(*, org_id: str = ORG_A) -> LoopProposal:
    """The same two waves, but the author gated wave 1 themselves."""
    return LoopProposal(
        flow_slug=FLOW,
        title="Delivery loop",
        org_id=org_id,
        spec_revision=SPEC_REVISION,
        intent_ref="4120",
        nodes=[
            ProposedNode(address=address("story-a"), kind="story", title="Story A", issue_ref="4527"),
            ProposedNode(address=address("eval-w1"), kind="eval", title="Wave 1 eval"),
            ProposedNode(address=address("my-gate"), kind="gate", title="The author's own gate"),
            ProposedNode(address=address("story-c", wave="wave-2"), kind="story", title="Story C", issue_ref="4529"),
            ProposedNode(address=address("eval-w2", wave="wave-2"), kind="eval", title="Wave 2 eval"),
        ],
        edges=[
            ProposedEdge(from_address=address("story-a"), to_address=address("eval-w1")),
            ProposedEdge(from_address=address("eval-w1"), to_address=address("my-gate")),
            ProposedEdge(from_address=address("my-gate"), to_address=address("story-c", wave="wave-2")),
            ProposedEdge(from_address=address("story-c", wave="wave-2"), to_address=address("eval-w2", wave="wave-2")),
        ],
    )


def invalid_proposal(*, org_id: str = ORG_A) -> LoopProposal:
    """Fails validation several ways: a container as a node, a duplicate, a cycle."""
    return LoopProposal(
        flow_slug=FLOW,
        title="Hostile proposal",
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
    """Zero rows in every table a registration would have written."""
    for model in (OrchestrationFlow, OrchestrationNode, OrchestrationEdge, OrchestrationAcceptedPlan, OrchestrationDecision):
        assert await count_rows(session, model) == 0, f"{model.__tablename__} should have no rows"


async def nodes_by_ref(session: AsyncSession, *, org_id: str = ORG_A) -> dict[str, OrchestrationNode]:
    """The tenant's nodes keyed by `node_ref`.

    Keyed by the last address segment only, which is unambiguous because the
    fixtures below give every node a distinct `node_ref` across waves — the
    uniqueness assert keeps that true, since a collision would silently drop a node
    from every state assertion in this file.
    """
    rows = (await session.execute(sa.select(OrchestrationNode).where(OrchestrationNode.org_id == org_id))).scalars().all()
    by_ref = {row.node_ref: row for row in rows}
    assert len(by_ref) == len(rows), "fixture node_refs must be unique across waves"
    return by_ref


async def seed_org(session: AsyncSession, org_id: str = ORG_A, *, installation_ids: list[str] | None = None) -> None:
    """An org row with one GitHub installation.

    Needed by the dispatch half: `resolve_installation_id` fails closed on an
    unresolvable org, and a test asserting "nothing dispatched" would then pass
    because the org was unwired rather than because the plan was inert — the exact
    false green this fixture exists to prevent.
    """
    session.add(
        Organization(
            id=org_id,
            name=org_id,
            github_installation_ids=installation_ids if installation_ids is not None else [str(INSTALLATION_A)],
        )
    )
    await session.flush()


def dispatch_config() -> DispatchPassConfig:
    """A fully-configured dispatch target, for the same reason as `seed_org`."""
    return DispatchPassConfig(
        queue_url="https://sqs.us-east-1.amazonaws.com/123456789012/adp-dev-agent-submit.fifo",
        repo="aws-e/adp",
    )


def token_context(org_id: str, *, user_id: str = AGENT_USER_ID) -> TokenContext:
    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id="",
        department_id="",
        account_type="service",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def edges_out_of(proposal: LoopProposal, address_prefix: str) -> list[ProposedEdge]:
    return [edge for edge in proposal.edges if edge.from_address.startswith(address_prefix)]


class TestAutonomyDefault:
    """The gateless-proposal default: a gate at every wave boundary."""

    def test_flag_defaults_to_disabled_when_unset(self):
        """The fail-safe reading, inverted by #4575.

        A wave gate the engine cannot yet arm wedges the plan at wave 1, so an
        unread flag must mean "do not insert a gate a human cannot answer" — not
        "ask the human again", which is what it would have meant if the gate were
        answerable. Off is the default until #4575 lands.
        """
        assert gate_every_wave_enabled() is False

    @pytest.mark.parametrize("spelling", ["0", "false", "no", "off", "FALSE", " Off "])
    def test_an_explicit_falsey_spelling_is_disabled(self, monkeypatch, spelling):
        monkeypatch.setenv(AUTONOMY_FLAG_ENV, spelling)
        assert gate_every_wave_enabled() is False

    @pytest.mark.parametrize("spelling", ["1", "true", "yes", "on"])
    def test_an_explicit_truthy_spelling_opts_in(self, monkeypatch, spelling):
        """Turning the training wheels on is now an explicit operator opt-in."""
        monkeypatch.setenv(AUTONOMY_FLAG_ENV, spelling)
        assert gate_every_wave_enabled() is True

    def test_gateless_proposal_gets_a_gate_at_every_wave_boundary(self):
        """The issue's first named unit test.

        Wave 1 is left by an edge, so it gains a gate. Wave 2 is left by nothing,
        so it does not: a gate after the last wave guards no boundary, and the
        acceptance gate already covers "may this plan run at all".
        """
        transformed = insert_wave_gates(gateless_proposal())

        wave_gates = {node.address for node in transformed.nodes if node.address.endswith(f"/{WAVE_GATE_REF}")}
        assert wave_gates == {address(WAVE_GATE_REF)}

    def test_no_path_leaves_a_gated_wave_without_passing_its_gate(self):
        """A boundary gate, not an ornament beside the boundary.

        The property that actually prevents the free-run: after the transform there
        is no edge from wave 1 to wave 2 that skips wave 1's gate.
        """
        transformed = insert_wave_gates(gateless_proposal())
        gate = address(WAVE_GATE_REF)

        crossing = [
            edge
            for edge in transformed.edges
            if edge.from_address.startswith(f"{FLOW}/epic-1/wave-1/")
            and edge.to_address.startswith(f"{FLOW}/epic-1/wave-2/")
            and edge.from_address != gate
        ]
        assert crossing == [], f"these edges cross the boundary without passing the gate: {crossing}"

    def test_the_wave_gate_is_the_downstream_waves_predecessor(self):
        """Stated positively too, so a transform that deleted the crossing edges
        entirely — which would satisfy the test above — still fails here."""
        transformed = insert_wave_gates(gateless_proposal())

        assert ProposedEdge(from_address=address(WAVE_GATE_REF), to_address=address("story-c", wave="wave-2")) in transformed.edges

    def test_declared_gates_pass_through_untouched(self):
        """The issue's second named unit test.

        An author who gated their plan is not overridden by a default whose whole
        purpose is to cover authors who did not. Asserted as full-document equality
        so a transform that "helpfully" reordered or retitled anything fails.
        """
        original = author_gated_proposal()

        assert insert_wave_gates(original) == original

    def test_the_flag_off_leaves_a_gateless_document_untouched(self):
        """The regression criterion: registration disabled behaves as today.

        Only the acceptance gate is added, because that is not the autonomy default
        — it is what makes the draft accept-ready and is not optional.
        """
        original = gateless_proposal()

        with pytest.MonkeyPatch.context() as patch:
            patch.setenv(AUTONOMY_FLAG_ENV, "false")
            transformed, _ = transform_for_registration(original)

        assert [node.address for node in transformed.nodes if node.kind == NodeKind.GATE.value] == [address(ACCEPTANCE_GATE_REF)]

    def test_wave_gates_are_inserted_before_the_acceptance_gate(self, monkeypatch):
        """Order is load-bearing, not stylistic.

        Reversed, the acceptance gate would already be a declared gate and
        `insert_wave_gates` would read the document as author-gated — the autonomy
        default would silently never apply to anything. Asserted through the public
        composition rather than by inspecting call order.

        The wave-gate transform ships OFF (#4575), so this opts in explicitly — the
        property under test is the *ordering* when both gates are inserted.
        """
        monkeypatch.setenv(AUTONOMY_FLAG_ENV, "1")
        transformed, _ = transform_for_registration(gateless_proposal())

        gate_addresses = {node.address for node in transformed.nodes if node.kind == NodeKind.GATE.value}
        assert gate_addresses == {address(ACCEPTANCE_GATE_REF), address(WAVE_GATE_REF)}

    def test_a_single_wave_proposal_gets_no_wave_gate(self):
        """One wave has no internal boundary to guard."""
        single_wave = LoopProposal(
            flow_slug=FLOW,
            title="Delivery loop",
            org_id=ORG_A,
            spec_revision=SPEC_REVISION,
            intent_ref="4120",
            nodes=[
                ProposedNode(address=address("story-a"), kind="story", title="Story A", issue_ref="4527"),
                ProposedNode(address=address("eval-w1"), kind="eval", title="Wave 1 eval"),
            ],
            edges=[ProposedEdge(from_address=address("story-a"), to_address=address("eval-w1"))],
        )

        assert insert_wave_gates(single_wave) == single_wave

    def test_the_transformed_document_still_validates(self):
        """The reason the transform runs *before* the compiler's Gate 1.

        A transform that produced a duplicate address, a dangling edge or a cycle
        must be refused like any other bad document. Trusted code is exactly the
        code nobody re-checks, so this asserts the transform's output survives the
        authoritative validator.
        """
        from src.orchestration.proposal import validate_proposal

        transformed, _ = transform_for_registration(gateless_proposal())

        assert validate_proposal(transformed) == []


class TestRegistrationWritesADraft:
    """What lands on the graph, and what it is recorded as."""

    async def test_the_acceptance_gate_is_born_awaiting_gate(self, session, registrar):
        result, gate_address = await register_draft_proposal(session, gateless_proposal(), registrar)

        assert gate_address == address(ACCEPTANCE_GATE_REF)
        nodes = await nodes_by_ref(session)
        assert nodes[ACCEPTANCE_GATE_REF].state == NodeState.AWAITING_GATE.value
        assert nodes[ACCEPTANCE_GATE_REF].kind == NodeKind.GATE.value
        # Counted from the table rather than from `nodes_by_ref`, which is keyed by
        # `node_ref` and so collapses the two waves' `eval` nodes.
        assert result.nodes_created == await count_rows(session, OrchestrationNode)

    async def test_every_other_node_is_born_pending(self, session, registrar, monkeypatch):
        """Only the acceptance gate gets a non-default initial state.

        A wave gate born `awaiting_gate` would make the flow's outstanding gate
        ambiguous, and `_resolve_gate` refuses ambiguity — the bare `accept`
        command would stop working. Opts the wave-gate transform on (OFF by
        default, #4575) precisely so a wave gate exists to assert this about.
        """
        monkeypatch.setenv(AUTONOMY_FLAG_ENV, "1")
        await register_draft_proposal(session, gateless_proposal(), registrar)

        nodes = await nodes_by_ref(session)
        awaiting = {ref for ref, node in nodes.items() if node.state == NodeState.AWAITING_GATE.value}
        assert awaiting == {ACCEPTANCE_GATE_REF}
        assert nodes[WAVE_GATE_REF].state == NodeState.PENDING.value

    async def test_the_decision_row_is_plan_drafted_by_a_service_actor(self, session, registrar):
        result, _ = await register_draft_proposal(session, gateless_proposal(), registrar)

        rows = (await session.execute(sa.select(OrchestrationDecision))).scalars().all()
        assert [row.kind for row in rows] == [DecisionKind.PLAN_DRAFTED.value]
        assert rows[0].id == result.decision_id
        assert rows[0].actor_id == AGENT_USER_ID
        assert rows[0].actor_kind == ActorKind.SERVICE.value

    async def test_the_plan_is_stored_and_readable_as_version_1(self, session, registrar):
        """The graph UI reads the same store (S12), so "visible immediately" is
        exactly "the accepted-plan row and its nodes exist"."""
        result, _ = await register_draft_proposal(session, gateless_proposal(), registrar)

        plan = (await session.execute(sa.select(OrchestrationAcceptedPlan))).scalar_one()
        assert plan.version == result.plan_version == 1
        assert plan.plan_hash == result.plan_hash
        assert plan.accepted_by_decision_id == result.decision_id

    async def test_the_stored_document_is_the_transformed_one(self, session, registrar, monkeypatch):
        """What a human accepts must be what the engine will run.

        Storing the pre-transform document would leave the accepted plan disagreeing
        with the graph about where the gates are. Opts the wave-gate transform on
        (OFF by default, #4575) so both gate kinds are present to assert on.
        """
        monkeypatch.setenv(AUTONOMY_FLAG_ENV, "1")
        await register_draft_proposal(session, gateless_proposal(), registrar)

        plan = (await session.execute(sa.select(OrchestrationAcceptedPlan))).scalar_one()
        stored = {node["address"] for node in plan.plan_document["nodes"]}
        assert address(ACCEPTANCE_GATE_REF) in stored
        assert address(WAVE_GATE_REF) in stored

    async def test_an_identical_resubmission_registers_nothing_new(self, session, registrar):
        """R-NF2, and the property a fail-soft worker retry depends on."""
        first, _ = await register_draft_proposal(session, gateless_proposal(), registrar)
        second, _ = await register_draft_proposal(session, gateless_proposal(), registrar)

        assert first.already_compiled is False
        assert second.already_compiled is True
        assert second.plan_version == first.plan_version
        assert await count_rows(session, OrchestrationAcceptedPlan) == 1
        assert await count_rows(session, OrchestrationDecision) == 1

    async def test_a_retry_does_not_reset_a_gate_a_human_already_answered(self, session, registrar, access):
        """The `initial_states` override applies only to nodes a call *creates*.

        Otherwise a duplicate webhook delivery after acceptance would put the gate
        back to `awaiting_gate` and re-arm a decision that had already been made.

        This is the exact overlap `_refuse_if_flow_is_live` deliberately permits:
        the flow is approved, so the injected-node case is refused outright (see
        `TestRegistrationCannotExtendAnApprovedFlow`), but an *identical* document
        short-circuits ahead of that refusal because it writes nothing. The two
        properties compose here — the retry is allowed through, and `initial_states`
        is why being allowed through is harmless.
        """
        await seed_principal(session, org_id=ORG_A, role=AdminRole.ORG_ADMIN.value)
        _, gate_address = await register_draft_proposal(session, gateless_proposal(), registrar)
        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]

        outcome = await apply_gate_answer_for_context(
            session,
            context=token_context(ORG_A, user_id=HUMAN_USER_ID),
            node_id=gate.id,
            approve=True,
            reason="Reviewed the wave map.",
            access=access,
            input_path=InputPath.GITHUB_COMMENT,
        )
        assert outcome.status is GateAnswerStatus.APPLIED

        # The identical document — the retry a fail-soft caller actually makes.
        # Permitted (it writes nothing), and it must not touch the answered gate.
        await register_draft_proposal(session, gateless_proposal(), registrar)

        await session.refresh(gate)
        assert gate.state == NodeState.PASSED.value
        assert gate_address == address(ACCEPTANCE_GATE_REF)


class TestADraftIsInert:
    """The issue's first bug class: registration must not start execution."""

    def test_the_draft_decision_kind_is_not_an_approval_kind(self):
        """Guard 1, asserted against the real frozenset rather than a literal.

        `dispatch_pass` roots a chain in the latest decision *of an approval kind*.
        A kind absent from that set cannot arm execution however many ticks run, so
        adding `PLAN_DRAFTED` to it would be the single-line change that breaks the
        whole story — and it fails here.
        """
        assert DecisionKind.PLAN_DRAFTED.value not in APPROVAL_DECISION_KINDS

    async def test_no_node_ever_becomes_ready_across_many_ticks(self, session, registrar):
        """The issue's named criterion: a draft dispatches nothing across many ticks.

        Ten ticks, because "inert" is a claim about the steady state, not about the
        first sweep — a bug that released one node per tick would pass a
        single-tick test.
        """
        await seed_org(session)
        await register_draft_proposal(session, gateless_proposal(), registrar)

        for _ in range(10):
            report = await run_tick(session)
            assert report.success
            assert report.transitions_effected == 0

        states = {node.state for node in (await nodes_by_ref(session)).values()}
        assert states == {NodeState.PENDING.value, NodeState.AWAITING_GATE.value}

    async def test_dispatch_publishes_nothing_across_many_ticks(self, session, registrar):
        """Tick and dispatch are separate passes, so inertness is asserted of both.

        The org and the dispatch config are both fully wired, so a green result here
        cannot come from an unconfigured environment.
        """
        await seed_org(session)
        await register_draft_proposal(session, gateless_proposal(), registrar)

        for _ in range(10):
            await run_tick(session)
            report = await run_dispatch_pass(session, dispatch_config())
            assert report.pending == []
            assert report.dispatched == 0

    async def test_the_acceptance_gate_blocks_every_root(self, session, registrar):
        """Guard 2, as a graph property: the gate dominates.

        Every node that had no predecessor now has the gate as one, so there is no
        node whose predecessors can all be `passed` while the gate is unanswered.
        """
        transformed, gate_address = transform_for_registration(gateless_proposal())

        has_incoming = {edge.to_address for edge in transformed.edges}
        assert [node.address for node in transformed.nodes if node.address not in has_incoming] == [gate_address]

    async def test_the_gate_cannot_be_walked_out_of_by_a_service_actor(self, session, registrar):
        """`state.py` marks the edges out of `awaiting_gate` human-only.

        Asserted through `transition()` itself rather than by reading the table, so
        the property survives a refactor of how the table is spelled.
        """
        from src.orchestration.state import transition

        for target in (NodeState.PASSED, NodeState.REJECTED_AT_GATE):
            outcome = transition(
                NodeState.AWAITING_GATE,
                target,
                actor_kind=ActorKind.SERVICE,
                reason="the engine trying to answer its own gate",
            )
            assert outcome.allowed is False


class TestAcceptanceReleasesTheDraft:
    """A draft that could never be accepted would pass every test above."""

    async def test_the_flow_has_exactly_one_outstanding_gate(self, session, registrar):
        """What makes the bare `@agent-engine accept` command unambiguous.

        `_resolve_gate` with no `gate_ref` resolves "the flow's single node in
        `awaiting_gate`" and refuses when there are two, so this is the property the
        closing comment's one-command promise rests on.
        """
        from src.orchestration.engine_commands import _resolve_gate

        result, _ = await register_draft_proposal(session, gateless_proposal(), registrar)

        gate = await _resolve_gate(session, org_id=ORG_A, flow_id=result.flow_id, gate_ref=None)
        assert gate is not None
        assert gate.node_ref == ACCEPTANCE_GATE_REF

    async def test_a_human_answer_writes_an_approval_kind_decision(self, session, registrar, access):
        """The release: `GATE_APPROVED` is in `APPROVAL_DECISION_KINDS`.

        Through the #4527 adapter this story did not modify, against real
        `users`/`tenant_memberships` rows and the real `AccessControl` — a stubbed
        permission check would assert that something was called, not that an
        authorized human is what unlocks the plan.
        """
        await seed_principal(session, org_id=ORG_A, role=AdminRole.ORG_ADMIN.value)
        await register_draft_proposal(session, gateless_proposal(), registrar)
        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]

        outcome = await apply_gate_answer_for_context(
            session,
            context=token_context(ORG_A, user_id=HUMAN_USER_ID),
            node_id=gate.id,
            approve=True,
            reason="Reviewed the wave map; start it.",
            access=access,
            input_path=InputPath.GITHUB_COMMENT,
        )

        assert outcome.status is GateAnswerStatus.APPLIED
        await session.refresh(gate)
        assert gate.state == NodeState.PASSED.value

        kinds = {row.kind for row in (await session.execute(sa.select(OrchestrationDecision))).scalars().all()}
        assert kinds & APPROVAL_DECISION_KINDS == {DecisionKind.GATE_APPROVED.value}

    async def test_the_first_wave_moves_only_after_acceptance(self, session, registrar, access, monkeypatch):
        """End to end, in one test: inert, then a human acts, then work is ready.

        This is the smoke test's shape as a unit test — the same sequence the issue
        asks an operator to witness on a toy intent in dev. Opts the wave-gate
        transform on (OFF by default, #4575) so wave 2's blocker is the wave gate,
        which is what the final assertion is about.
        """
        monkeypatch.setenv(AUTONOMY_FLAG_ENV, "1")
        await seed_org(session)
        await seed_principal(session, org_id=ORG_A, role=AdminRole.ORG_ADMIN.value)
        await register_draft_proposal(session, gateless_proposal(), registrar)

        await run_tick(session)
        assert (await nodes_by_ref(session))["story-a"].state == NodeState.PENDING.value

        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]
        await apply_gate_answer_for_context(
            session,
            context=token_context(ORG_A, user_id=HUMAN_USER_ID),
            node_id=gate.id,
            approve=True,
            reason="accept",
            access=access,
            input_path=InputPath.GITHUB_COMMENT,
        )

        report = await run_tick(session)
        assert report.success
        nodes = await nodes_by_ref(session)
        assert nodes["story-a"].state == NodeState.READY.value
        # Wave 2 stays put: its predecessor is wave 1's gate, which nobody answered.
        assert nodes["story-c"].state == NodeState.PENDING.value


class TestACollidingNodeIssueRefCannotHijackACommand:
    """A `PLAN_DRAFT`-authored `issue_ref` must not win engine-command resolution.

    The escalation this closes: `_resolve_target` maps an `@agent-engine` comment
    to a flow, and a node's `issue_ref` is how a comment on a story/gate reaches
    its parent flow. But `ProposedNode.issue_ref` is author-chosen free text with no
    ownership check, and authoring a draft node is open to any `PLAN_DRAFT` holder
    (every MEMBER). So a MEMBER could register a *fresh* flow — bypassing the
    approved-flow guards, which only cover superseding an existing flow — carrying a
    node whose `issue_ref` collides with a victim flow's intent issue. If the node
    match won resolution, a human's `@agent-engine accept` on their own intent issue
    would be routed to the attacker's flow and root the approval under the human's
    identity: the exact "human-rooted genesis" inversion the bridge exists to
    prevent.

    The fix makes both lookups (node parent, flow `intent_ref`) have to agree on a
    single flow, so a cross-flow collision is refused rather than resolved to
    either side.
    """

    def _attacker_proposal(self, *, issue_ref: str) -> LoopProposal:
        """A second, freshly-slugged draft whose one story node steals `issue_ref`."""
        slug = "attacker-loop"
        return LoopProposal(
            flow_slug=slug,
            title="Attacker loop",
            org_id=ORG_A,
            spec_revision="issue-9999-r1",
            intent_ref="9999",
            nodes=[
                ProposedNode(address=f"{slug}/epic-1/wave-1/evil", kind="story", title="Evil", issue_ref=issue_ref),
                ProposedNode(address=f"{slug}/epic-1/wave-1/eval-w1", kind="eval", title="Wave 1 eval"),
            ],
            edges=[ProposedEdge(from_address=f"{slug}/epic-1/wave-1/evil", to_address=f"{slug}/epic-1/wave-1/eval-w1")],
        )

    async def test_intent_issue_with_a_colliding_foreign_node_is_refused(self, session, registrar):
        """The hijack, blocked. `4120` is the victim flow's `intent_ref`; the attacker
        plants a node carrying `issue_ref=4120` in a *different* flow. A comment on
        4120 now names two distinct flows, so it is refused rather than routed to the
        attacker's flow."""
        from src.orchestration.engine_commands import _resolve_target

        victim, _ = await register_draft_proposal(session, gateless_proposal(), registrar)  # intent_ref 4120
        await register_draft_proposal(session, self._attacker_proposal(issue_ref="4120"), registrar)

        # Registration allows the collision (no ownership check on issue_ref) — which
        # is exactly why the resolver has to be the one that refuses.
        assert await count_rows(session, OrchestrationFlow) == 2

        resolved = await _resolve_target(session, org_id=ORG_A, issue_number=4120)
        assert resolved is None, "a foreign node colliding with the victim's intent issue must refuse, not resolve"

        # And specifically not the attacker's flow.
        if resolved is not None:  # pragma: no cover - guarded by the assert above
            assert resolved[0] != victim.flow_id

    async def test_the_intent_issue_still_resolves_to_its_own_flow_without_a_collision(self, session, registrar):
        """Positive control: the legitimate path is untouched.

        With no colliding foreign node, a comment on the intent issue resolves to
        that flow, flow-scoped (no single node)."""
        from src.orchestration.engine_commands import _resolve_target

        victim, _ = await register_draft_proposal(session, gateless_proposal(), registrar)  # intent_ref 4120

        resolved = await _resolve_target(session, org_id=ORG_A, issue_number=4120)
        assert resolved == (victim.flow_id, None)

    async def test_a_comment_on_a_story_node_still_resolves_to_that_node(self, session, registrar):
        """Positive control: a comment on a story's own issue reaches its flow.

        `4527` is `story-a`'s `issue_ref` in the victim flow and is no other flow's
        `intent_ref`, so the node match stands and is returned."""
        from src.orchestration.engine_commands import _resolve_target

        victim, _ = await register_draft_proposal(session, gateless_proposal(), registrar)

        resolved = await _resolve_target(session, org_id=ORG_A, issue_number=4527)
        assert resolved is not None
        flow_id, node = resolved
        assert flow_id == victim.flow_id
        assert node is not None and node.issue_ref in ("4527", "#4527")

    async def test_the_same_issue_addressing_the_same_flow_two_ways_is_not_ambiguous(self, session, registrar):
        """A node whose `issue_ref` equals its *own* flow's `intent_ref` is self-
        consistent — both lookups name one flow, so it resolves rather than refusing."""
        from src.orchestration.engine_commands import _resolve_target

        # A single-flow proposal whose story node reuses the flow's own intent_ref.
        proposal = gateless_proposal().model_copy(update={"intent_ref": "4527"})
        victim, _ = await register_draft_proposal(session, proposal, registrar)

        resolved = await _resolve_target(session, org_id=ORG_A, issue_number=4527)
        assert resolved is not None
        assert resolved[0] == victim.flow_id


class TestRefusalWritesNothing:
    """A rejected registration must leave nothing for a fail-soft retry to hit."""

    async def test_an_invalid_document_is_refused(self, session, registrar):
        with pytest.raises(ProposalRejectedError):
            await register_draft_proposal(session, invalid_proposal(), registrar)

    async def test_an_invalid_document_writes_zero_rows(self, session, registrar):
        with pytest.raises(ProposalRejectedError):
            await register_draft_proposal(session, invalid_proposal(), registrar)
        await assert_graph_is_empty(session)

    async def test_a_document_declaring_another_tenant_is_refused_not_rehomed(self, session, registrar):
        """Tenant isolation: compared against the resolved org, never substituted."""
        with pytest.raises(TenantMismatchError):
            await register_draft_proposal(session, gateless_proposal(org_id=ORG_B), registrar)
        await assert_graph_is_empty(session)


class TestRegistrationCannotExtendAnApprovedFlow:
    """The escalation found in review (PR #4558), and the guard that closes it.

    Both node-level inertness guards are *bypassed* — not broken — by registering
    into a flow that already carries a human approval:

    - `_latest_approval_decision_id` is **flow**-scoped, so the human's existing
      approval row roots a node appended to that flow later, and the registrant
      never writes an approval kind at all.
    - `initial_states` applies only to addresses a compile creates, so an already
      existing acceptance-gate address keeps its `passed` state.

    The reproduction dispatched an attacker-chosen issue as a real agent run
    attributed to the human approver. So the tests here are written against the
    *dispatch outcome*, not just against the refusal: the property that matters is
    "nothing runs", and a refusal is only how it is achieved.
    """

    async def approved_flow(self, session, registrar, access) -> str:
        """A flow whose gate a real human answered — i.e. a live, approved plan."""
        await seed_org(session)
        await seed_principal(session, org_id=ORG_A, role=AdminRole.ORG_ADMIN.value)
        result, _ = await register_draft_proposal(session, gateless_proposal(), registrar)

        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]
        outcome = await apply_gate_answer_for_context(
            session,
            context=token_context(ORG_A, user_id=HUMAN_USER_ID),
            node_id=gate.id,
            approve=True,
            reason="Reviewed and accepted.",
            access=access,
            input_path=InputPath.GITHUB_COMMENT,
        )
        assert outcome.status is GateAnswerStatus.APPLIED
        return result.flow_id

    def injected_proposal(self) -> LoopProposal:
        """A new node smuggled into the same flow_slug, carrying its own issue.

        `issue_ref` is what a dispatch would deliver work against, so it is the
        payload of the escalation: if this node ever dispatched, an attacker-chosen
        issue would run as an agent under the approver's identity.
        """
        return LoopProposal(
            flow_slug=FLOW,
            title="Delivery loop",
            org_id=ORG_A,
            spec_revision=SPEC_REVISION,
            nodes=[
                ProposedNode(address=address("injected", wave="wave-9"), kind="story", title="Attacker work", issue_ref="9999"),
                ProposedNode(address=address("eval-w9", wave="wave-9"), kind="eval", title="Wave 9 eval"),
            ],
            edges=[ProposedEdge(from_address=address("injected", wave="wave-9"), to_address=address("eval-w9", wave="wave-9"))],
        )

    async def test_registering_into_an_approved_flow_is_refused(self, session, registrar, access):
        await self.approved_flow(session, registrar, access)

        with pytest.raises(DraftFlowConflictError):
            await register_draft_proposal(session, self.injected_proposal(), registrar)

    async def test_the_injected_node_is_never_created(self, session, registrar, access):
        """Refused before anything is created, so there is no row to clean up."""
        await self.approved_flow(session, registrar, access)
        before = await count_rows(session, OrchestrationNode)

        with pytest.raises(DraftFlowConflictError):
            await register_draft_proposal(session, self.injected_proposal(), registrar)

        assert await count_rows(session, OrchestrationNode) == before
        assert "injected" not in await nodes_by_ref(session)

    async def test_nothing_is_dispatched_after_an_attempted_injection(self, session, registrar, access):
        """The reproduction, inverted into a regression test.

        Real `run_tick` and real `run_dispatch_pass`, with the org and dispatch
        target fully wired so a green result cannot come from an unconfigured
        environment. The review's harness saw `dispatched=1` here.

        Wave 1 legitimately dispatches — a human accepted that plan — so the
        assertion is specifically that no dispatch carries the injected issue.
        """
        await self.approved_flow(session, registrar, access)

        with pytest.raises(DraftFlowConflictError):
            await register_draft_proposal(session, self.injected_proposal(), registrar)

        # `run_dispatch_pass` commits the node to `running` and returns the envelopes
        # it would send as `PendingPublish` rows; `publish_pending` is the separate
        # phase that actually sends them. Reading `report.pending` therefore inspects
        # the dispatch *decision* — the thing under test — with no SQS client at all.
        issues: set[str] = set()
        for _ in range(10):
            await run_tick(session)
            report = await run_dispatch_pass(session, dispatch_config())
            for item in report.pending:
                issues.add(str((item.envelope.get("source_ref") or {}).get("issue")))

        # Asserted first, and not as a courtesy: "no dispatch carried issue 9999" is
        # also trivially true of a harness that dispatches nothing, so without this
        # line an unconfigured queue, a broken genesis resolution or a tick that
        # stopped advancing would all read as the guard working. The accepted wave-1
        # stories must really be flowing for the negative below to mean anything.
        assert issues, "harness dispatched nothing at all, so the assertion below proves nothing"

        assert "9999" not in issues, f"the injected node dispatched: {issues}"

    async def test_a_flow_with_an_approval_is_refused_even_with_no_plan_in_force(self, session, registrar):
        """The two conditions are independent, so each is tested alone.

        A flow can carry an approval decision without an in-force accepted plan
        (a gate approved on a flow whose plan row was superseded, say). The
        decision table is what dispatch reads, so that alone must refuse.
        """
        await seed_org(session)
        result, _ = await register_draft_proposal(session, gateless_proposal(), registrar)

        repo = OrchestrationRepository(session)
        await repo.append_decision(
            org_id=ORG_A,
            flow_id=result.flow_id,
            kind=DecisionKind.GATE_APPROVED.value,
            actor_id=HUMAN_USER_ID,
            actor_role=AdminRole.ORG_ADMIN.value,
            actor_kind=ActorKind.HUMAN.value,
        )
        await session.execute(sa.delete(OrchestrationAcceptedPlan))

        with pytest.raises(DraftFlowConflictError):
            await register_draft_proposal(session, self.injected_proposal(), registrar)

    async def test_a_differing_document_never_supersedes_the_plan_of_record(self, session, registrar):
        """The second finding: plan-of-record rewrite, reproduced then closed.

        `compile_proposal` calls `record_accepted_plan` unconditionally, which
        supersedes the in-force row — and `PLAN_DRAFT` is held by every ordinary
        member. The review reproduced v1 -> v2 ("attacker rewrote the plan").
        Registration being create-only removes the path: the supersede branch is
        unreachable because a draft only ever lands version 1 of a flow that had
        nothing in force.
        """
        await register_draft_proposal(session, gateless_proposal(), registrar)
        original = (await session.execute(sa.select(OrchestrationAcceptedPlan))).scalar_one()

        rewrite = gateless_proposal().model_copy(update={"title": "Attacker rewrote the plan"})
        with pytest.raises(DraftFlowConflictError):
            await register_draft_proposal(session, rewrite, registrar)

        plans = (await session.execute(sa.select(OrchestrationAcceptedPlan))).scalars().all()
        assert len(plans) == 1, "a draft registration created a second plan version"
        assert plans[0].version == original.version == 1
        assert plans[0].superseded_at is None, "the in-force plan was superseded by a draft registration"
        assert plans[0].plan_document["title"] == "Delivery loop"

    async def test_the_identical_document_is_still_a_permitted_retry(self, session, registrar):
        """The one overlap the guard must allow, or fail-soft retries break.

        The worker retries by construction, so refusing an identical resubmission
        would turn a dropped connection into a permanent failure. It writes nothing,
        so permitting it grants no authority.
        """
        first, _ = await register_draft_proposal(session, gateless_proposal(), registrar)
        second, _ = await register_draft_proposal(session, gateless_proposal(), registrar)

        assert second.already_compiled is True
        assert second.plan_version == first.plan_version
        assert await count_rows(session, OrchestrationAcceptedPlan) == 1

    async def test_the_identical_document_is_a_permitted_retry_even_after_acceptance(self, session, registrar, access):
        """The ordering the two conditions must be evaluated in, pinned by a test.

        My own first version of the guard checked the approval-decision condition
        *before* the idempotency comparison, which meant that the moment a human
        accepted the plan, the worker's ordinary retry started getting a 409 — a
        dropped connection turning into a permanent registration failure for a
        request that would have written nothing.

        The retry has to short-circuit ahead of the refusals precisely because
        `compile_proposal` returns `already_compiled` and writes no node, edge,
        decision or plan row: there is no authority to be gained by permitting it,
        approved flow or not. This test exists so a future reordering that looks
        tidier (both refusals first, retry last) fails loudly here instead of
        quietly breaking fail-soft in production.
        """
        await self.approved_flow(session, registrar, access)
        before_nodes = await count_rows(session, OrchestrationNode)
        before_decisions = await count_rows(session, OrchestrationDecision)

        retry, _ = await register_draft_proposal(session, gateless_proposal(), registrar)

        assert retry.already_compiled is True
        # Nothing written, which is *why* it is safe to permit.
        assert await count_rows(session, OrchestrationNode) == before_nodes
        assert await count_rows(session, OrchestrationDecision) == before_decisions
        assert await count_rows(session, OrchestrationAcceptedPlan) == 1

        # And the human's answer is untouched — the retry did not re-arm the gate.
        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]
        assert gate.state == NodeState.PASSED.value

    async def test_the_guard_hashes_the_same_document_compile_stores(self, session, registrar):
        """The subtle half of the retry allowance, pinned directly.

        The guard's permitted-retry branch compares its `document_hash` against the
        `plan_hash` on the stored plan row. Those two only agree if the guard hashes
        the *transformed* document, because that is what `compile_proposal` receives
        and stores. My first version hashed the document as authored, which silently
        broke every fail-soft retry: the hashes never matched, so the retry fell
        through to the refusals and came back 409 forever.

        Asserted against the stored row rather than by re-deriving the expected
        value, so it fails if either side of the agreement moves.
        """
        authored = gateless_proposal()
        await register_draft_proposal(session, authored, registrar)

        stored = (await session.execute(sa.select(OrchestrationAcceptedPlan))).scalar_one()
        transformed, _ = transform_for_registration(authored)

        assert stored.plan_hash == plan_hash(transformed), "the stored plan is not the transformed document"
        assert stored.plan_hash != plan_hash(authored), (
            "authored and transformed hash identically, so this test cannot detect the bug it exists for (the transforms must add an acceptance gate)"
        )

    async def test_a_new_flow_slug_is_the_supported_way_forward(self, session, registrar):
        """The refusal must leave a legitimate author a route, or it just blocks work.

        A second delivery loop registers cleanly under its own slug, which is what
        the error message tells the caller to do.
        """
        await register_draft_proposal(session, gateless_proposal(), registrar)

        other = gateless_proposal().model_copy(update={"flow_slug": "delivery-loop-2"})
        other = other.model_copy(
            update={
                "nodes": [node.model_copy(update={"address": node.address.replace(f"{FLOW}/", "delivery-loop-2/")}) for node in other.nodes],
                "edges": [
                    edge.model_copy(
                        update={
                            "from_address": edge.from_address.replace(f"{FLOW}/", "delivery-loop-2/"),
                            "to_address": edge.to_address.replace(f"{FLOW}/", "delivery-loop-2/"),
                        }
                    )
                    for edge in other.edges
                ],
            }
        )

        result, _ = await register_draft_proposal(session, other, registrar)
        assert result.already_compiled is False
        assert await count_rows(session, OrchestrationFlow) == 2

    async def test_the_route_maps_the_conflict_to_409(self, session, app_with_router):
        """A 409, not a 422: the document is fine, the target is not available."""
        client = client_for(app_with_router, permitted=True)
        payload = gateless_proposal().model_dump(mode="json")
        assert client.post(ROUTE, json=payload).status_code == 201

        rewrite = {**payload, "title": "Attacker rewrote the plan"}
        response = client.post(ROUTE, json=rewrite)

        assert response.status_code == 409, response.text
        assert await count_rows(session, OrchestrationAcceptedPlan) == 1


class TestGateArming:
    """Both author-declared and inserted gates become answerable (#4575)."""

    async def drive(self, session, rounds: int = 10) -> None:
        for _ in range(rounds):
            await run_tick(session)
            await run_dispatch_pass(session, dispatch_config())

    async def test_author_gate_is_presented_after_all_predecessors_pass(self, session, registrar):
        await seed_org(session)
        await compile_proposal(session, author_gated_proposal(), registrar)

        nodes = await nodes_by_ref(session)
        for ref in ("story-a", "eval-w1"):
            nodes[ref].state = NodeState.PASSED.value
        await session.flush()
        await self.drive(session)

        nodes = await nodes_by_ref(session)
        assert nodes["my-gate"].kind == NodeKind.GATE.value
        assert nodes["my-gate"].state == NodeState.AWAITING_GATE.value
        assert nodes["my-gate"].attempts == 0
        assert nodes["story-c"].state == NodeState.PENDING.value

    async def test_the_acceptance_gate_is_answerable_because_it_is_born_armed(self, session, registrar):
        """The half that must keep working: the story's actual promise.

        Whatever the engine does about wave gates, a registered draft must present
        exactly one answerable gate, or `@agent-engine accept` has nothing to answer
        and the one-command promise in the closing comment is false.
        """
        await seed_org(session)
        await register_draft_proposal(session, gateless_proposal(), registrar)
        await self.drive(session)

        nodes = await nodes_by_ref(session)
        awaiting = [ref for ref, node in nodes.items() if node.state == NodeState.AWAITING_GATE.value]
        assert awaiting == [ACCEPTANCE_GATE_REF], f"expected exactly one answerable gate, got {awaiting}"


class TestOnlyAnApprovalRewritesThePlanOfRecord:
    """The guard in the primitive, and why the one in `register_draft_proposal` is not enough.

    Everything in `TestRegistrationCannotExtendAnApprovedFlow` above goes through
    `register_draft_proposal`, whose pre-flight refuses an approved flow. That
    closes the escalation *for that caller*. It does not close the class, because
    the refusal lives in the caller rather than in the code that writes the rows:
    reached directly, `compile_proposal` with a non-approval `decision_kind` still
    superseded the in-force plan **and** got the injected node dispatched, because
    `_latest_approval_decision_id` is flow-scoped and roots any node appended to a
    flow a human ever approved.

    That reproduced on the remediation commit (`e4e5d329`) — the same two findings
    from review PR #4558, through a route the guard did not cover. So the invariant
    now lives in `compile_proposal`: **a non-approval compile may not supersede an
    in-force plan.** These tests call the primitive directly, on purpose. A test
    that only ever went through `register_draft_proposal` is exactly the blind spot
    that let both findings ship green twice.
    """

    async def approved_flow(self, session, registrar, access) -> str:
        await seed_org(session)
        await seed_principal(session, org_id=ORG_A, role=AdminRole.ORG_ADMIN.value)
        result, _ = await register_draft_proposal(session, gateless_proposal(), registrar)
        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]
        outcome = await apply_gate_answer_for_context(
            session,
            context=token_context(ORG_A, user_id=HUMAN_USER_ID),
            node_id=gate.id,
            approve=True,
            reason="Reviewed and accepted.",
            access=access,
            input_path=InputPath.GITHUB_COMMENT,
        )
        assert outcome.status is GateAnswerStatus.APPLIED
        return result.flow_id

    def injected_proposal(self) -> LoopProposal:
        return LoopProposal(
            flow_slug=FLOW,
            title="Delivery loop",
            org_id=ORG_A,
            spec_revision=SPEC_REVISION,
            nodes=[
                ProposedNode(address=address("injected", wave="wave-9"), kind="story", title="Attacker work", issue_ref="9999"),
                ProposedNode(address=address("eval-w9", wave="wave-9"), kind="eval", title="Wave 9 eval"),
            ],
            edges=[ProposedEdge(from_address=address("injected", wave="wave-9"), to_address=address("eval-w9", wave="wave-9"))],
        )

    async def test_a_draft_compile_cannot_supersede_an_in_force_plan(self, session, registrar, access):
        """The primitive refuses, so no caller can reach the supersede branch."""
        await self.approved_flow(session, registrar, access)

        with pytest.raises(NonApprovalSupersedeError):
            await compile_proposal(session, self.injected_proposal(), registrar, decision_kind=DecisionKind.PLAN_DRAFTED)

    async def test_the_plan_of_record_survives_a_direct_draft_compile(self, session, registrar, access):
        """v1 stays in force and no v2 appears — the HIGH-2 reproduction, inverted."""
        await self.approved_flow(session, registrar, access)

        with pytest.raises(NonApprovalSupersedeError):
            await compile_proposal(session, self.injected_proposal(), registrar, decision_kind=DecisionKind.PLAN_DRAFTED)

        plans = (await session.execute(sa.select(OrchestrationAcceptedPlan))).scalars().all()
        assert len(plans) == 1, "a non-approval compile created a second plan version"
        assert plans[0].version == 1
        assert plans[0].superseded_at is None, "the in-force plan was superseded by a non-approval compile"

    async def test_nothing_is_dispatched_after_a_direct_draft_compile(self, session, registrar, access):
        """The HIGH-1 reproduction, inverted, against the primitive.

        With real `run_tick` and `run_dispatch_pass`. On `e4e5d329` this harness saw
        the injected issue dispatched, rooted by the human's approval.
        """
        await self.approved_flow(session, registrar, access)

        with pytest.raises(NonApprovalSupersedeError):
            await compile_proposal(session, self.injected_proposal(), registrar, decision_kind=DecisionKind.PLAN_DRAFTED)

        issues: set[str] = set()
        for _ in range(10):
            await run_tick(session)
            report = await run_dispatch_pass(session, dispatch_config())
            for item in report.pending:
                issues.add(str((item.envelope.get("source_ref") or {}).get("issue")))

        # Same reason as the sibling test above: "9999 did not dispatch" is trivially
        # true of a harness that dispatches nothing, so the accepted wave-1 stories
        # must really be flowing for the negative to carry any weight.
        assert issues, "harness dispatched nothing at all, so the assertion below proves nothing"
        assert "9999" not in issues, f"the injected node dispatched: {issues}"
        assert "injected" not in await nodes_by_ref(session)

    async def test_a_flow_with_an_in_force_plan_and_a_passed_node_is_refused(self, session, registrar, access):
        """The specific regression the re-review asked for (its item 5).

        A flow carrying **both** an in-force plan and a node a human already drove to
        `passed` — the state every previous test omitted, since they were all
        greenfield. Asserts no supersession and no dispatch.
        """
        await self.approved_flow(session, registrar, access)

        gate = (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF]
        assert gate.state == NodeState.PASSED.value, "fixture must leave a passed node behind"
        assert await count_rows(session, OrchestrationAcceptedPlan) == 1

        with pytest.raises(NonApprovalSupersedeError):
            await compile_proposal(session, self.injected_proposal(), registrar, decision_kind=DecisionKind.PLAN_DRAFTED)

        plans = (await session.execute(sa.select(OrchestrationAcceptedPlan))).scalars().all()
        assert len(plans) == 1 and plans[0].superseded_at is None
        assert (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF].state == NodeState.PASSED.value

    async def test_an_approval_kind_may_still_supersede(self, session, registrar, access):
        """The guard must not break amendment, which is normal operation.

        Scoped to non-approval kinds only: an approval-kind compile supersedes as it
        always has. Without this, the fix would read as "drafts are safe" while
        having quietly broken the plan-amendment path.
        """
        await self.approved_flow(session, registrar, access)

        amended = gateless_proposal().model_copy(update={"title": "Amended by a human approver"})
        result = await compile_proposal(session, amended, registrar, decision_kind=DecisionKind.PLAN_AMENDED)

        assert result.plan_version == 2
        plans = (await session.execute(sa.select(OrchestrationAcceptedPlan).order_by(OrchestrationAcceptedPlan.version))).scalars().all()
        assert [plan.version for plan in plans] == [1, 2]
        assert plans[0].superseded_at is not None, "the prior version should have been superseded"
        assert plans[1].superseded_at is None

    async def test_a_draft_compile_into_a_clean_flow_still_works(self, session, registrar):
        """The guard is scoped to a flow with a plan in force, and nothing wider.

        Registration's own happy path is a non-approval compile into a flow with
        nothing in force. If the guard caught that too, the whole story would be
        broken rather than secured.
        """
        await seed_org(session)
        result, _ = await register_draft_proposal(session, gateless_proposal(), registrar)

        assert result.already_compiled is False
        assert result.plan_version == 1
        assert (await nodes_by_ref(session))[ACCEPTANCE_GATE_REF].state == NodeState.AWAITING_GATE.value


class TestRouteAuthorization:
    """`POST /orchestration/flows/drafts` — the agent-reachable ingress."""

    async def test_registration_returns_201_with_the_accept_command(self, session, app_with_router):
        client = client_for(app_with_router, permitted=True)

        response = client.post(ROUTE, json=gateless_proposal().model_dump(mode="json"))

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["already_registered"] is False
        assert body["acceptance_gate_address"] == address(ACCEPTANCE_GATE_REF)
        # Composed server-side so the worker's closing comment cannot drift from
        # what `engine_commands.py` parses.
        assert body["accept_command"] == "@agent-engine accept"

    async def test_the_route_gates_on_plan_draft_not_plan_approve(self, session, app_with_router):
        """The permission an agent principal can actually hold.

        Asserted on the recorded call rather than by reading the source: gating on
        `PLAN_APPROVE` here would mean an authoring agent needs approval authority
        to register a draft, which is the self-approval the EPIC forbids.
        """
        client = client_for(app_with_router, permitted=True)

        client.post(ROUTE, json=gateless_proposal().model_dump(mode="json"))

        access = app_with_router.dependency_overrides[get_access_control_dep()]()
        called = access.check_permission.await_args
        assert called.args[1] is Permission.PLAN_DRAFT
        assert called.kwargs["target_org_id"] == ORG_A

    async def test_a_caller_without_the_permission_gets_403_and_writes_nothing(self, session, app_with_router):
        client = client_for(app_with_router, permitted=False)

        response = client.post(ROUTE, json=gateless_proposal().model_dump(mode="json"))

        assert response.status_code == 403, response.text
        await assert_graph_is_empty(session)

    async def test_a_document_declaring_another_tenant_gets_422(self, session, app_with_router):
        client = client_for(app_with_router, permitted=True)

        response = client.post(ROUTE, json=gateless_proposal(org_id=ORG_B).model_dump(mode="json"))

        assert response.status_code == 422, response.text
        await assert_graph_is_empty(session)

    async def test_an_invalid_document_gets_422_with_per_violation_detail(self, session, app_with_router):
        client = client_for(app_with_router, permitted=True)

        response = client.post(ROUTE, json=invalid_proposal().model_dump(mode="json"))

        assert response.status_code == 422, response.text
        assert response.json()["detail"]["violations"], "the author needs to know which rules failed"
        await assert_graph_is_empty(session)

    async def test_an_identical_resubmission_gets_200_not_201(self, session, app_with_router):
        """A fail-soft retry must not be reported as a second plan."""
        client = client_for(app_with_router, permitted=True)
        payload = gateless_proposal().model_dump(mode="json")

        assert client.post(ROUTE, json=payload).status_code == 201
        second = client.post(ROUTE, json=payload)

        assert second.status_code == 200, second.text
        assert second.json()["already_registered"] is True

    async def test_the_actor_is_recorded_as_a_service_actor(self, session, app_with_router):
        """Never HUMAN. `resolve_engine_genesis` refuses a service-actor decision,
        so this is a second reason the row registration writes cannot root a
        dispatch even if its kind were changed."""
        client = client_for(app_with_router, permitted=True)

        client.post(ROUTE, json=gateless_proposal().model_dump(mode="json"))

        row = (await session.execute(sa.select(OrchestrationDecision))).scalar_one()
        assert row.actor_kind == ActorKind.SERVICE.value
        assert row.actor_id == AGENT_USER_ID


# --- Fixtures and helpers used by the route and acceptance tests --------------

HUMAN_USER_ID = "cognito-sub-operator"


def get_access_control_dep():
    from src.orchestration.draft_routes import get_access_control

    return get_access_control


async def seed_principal(session: AsyncSession, *, org_id: str, role: str) -> str:
    """A real user plus the membership row that carries their role.

    `tenant_memberships` is the authority for authority (`_resolve_membership_role`),
    so a permission check against real rows is the only one that proves an
    unauthorized caller is actually refused.
    """
    user = User(
        id=f"user-{HUMAN_USER_ID}",
        org_id=org_id,
        team_id=f"team-{org_id}",
        email="operator@example.test",
        cognito_sub=HUMAN_USER_ID,
    )
    session.add(user)
    session.add(TenantMembership(user_id=user.id, tenant_id=org_id, role=role, is_active=True))
    await session.flush()
    return user.id


@pytest.fixture
def access(session):
    """The real access control, over the session's real rows."""
    return AccessControl(db=session)


@pytest.fixture
def app_with_router(session):
    """A minimal app carrying only the draft router.

    Deliberately not `create_app()`: that pulls the whole middleware stack and
    would make an authz assertion here depend on all of it. Same shape as
    `test_flow_create.py`.
    """
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from src.auth.dependencies import get_current_user
    from src.orchestration.draft_routes import router as draft_router
    from src.shared.database import get_db
    from src.shared.exceptions import BedrockGatewayError

    app = FastAPI()
    app.include_router(draft_router)

    # `AccessDeniedError` carries status_code=403 and is translated by the
    # app-level handler in `create_app()`. Registered here because this minimal app
    # skips it; without it a denied caller surfaces as 500 and the authz assertions
    # would be testing the harness rather than the route.
    @app.exception_handler(BedrockGatewayError)
    async def _gateway_error_handler(_request: Request, exc: BedrockGatewayError):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.error, "message": exc.message})

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: token_context(ORG_A)
    return app


def client_for(app, *, permitted: bool, role: str = AdminRole.MEMBER.value):
    """A TestClient whose access control is stubbed to permit or deny.

    Denial is expressed as `PLAN_DRAFT` because that is the permission this route
    gates on — asserting a different permission's denial would test a check the
    route does not make. `role` defaults to MEMBER, which is what a
    registry-resolved agent principal actually resolves to.
    """
    from unittest.mock import AsyncMock, MagicMock

    from fastapi.testclient import TestClient

    from src.admin.exceptions import AccessDeniedError

    access = MagicMock(spec=AccessControl)
    if permitted:
        access.check_permission = AsyncMock(return_value=True)
    else:
        access.check_permission = AsyncMock(
            side_effect=AccessDeniedError(
                message=f"Permission '{Permission.PLAN_DRAFT.value}' is required for this operation",
                required_permission=Permission.PLAN_DRAFT.value,
                user_role=role,
            )
        )
    access.get_user_role = AsyncMock(return_value=(AdminRole(role), ORG_A, None))

    app.dependency_overrides[get_access_control_dep()] = lambda: access
    return TestClient(app, raise_server_exceptions=False)
