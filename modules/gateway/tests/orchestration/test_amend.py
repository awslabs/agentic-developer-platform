"""Tests for plan amendment as a first-class, attributed engine operation.

Issue #4200. Amendment is a **second write path into promotion state** — the exact
state agents must not reach — so the guarantees under test are the ones that keep
it from being a softer door than the gate it sits beside:

  - **AC-28**: amending creates version 2, marks version 1 superseded, and version
    1 **remains queryable** with its original node set.
  - **State preservation**: a node present in both versions with an unchanged
    definition keeps its state and run history; an already-`passed` node is never
    reset by an amendment that does not mention it. Re-planning a wave must not
    throw away work already accepted in it.
  - **Attribution**: one decision row per amendment, kind `plan_amended`, carrying
    actor identity, the role held **at amendment time**, and the human/service
    discriminator.
  - **AC-17 (adversarial)**: `_ORG_SCOPED_PERMISSIONS` is asserted for
    **equality**, so a future permission added without registering it fails here.
  - **AC-29 (adversarial)**: an amendment failing `validate_proposal` writes
    nothing — amendment cannot smuggle in a plan the original path would refuse.
  - **Atomicity**: a failure mid-amendment leaves version 1 current and no orphans.
  - **Idempotency (R-NF2)**: the same amendment twice yields one new version.

The session fixture is `test_compile.py`'s, including its two pysqlite hooks —
they are load-bearing, not boilerplate (see that file's fixture docstring: without
them `begin_nested()` becomes the outermost unit of work and the atomicity tests
pass for the wrong reason).
"""

import inspect

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.access_control import _ORG_SCOPED_PERMISSIONS
from src.admin.config import ROLE_PERMISSIONS, AdminRole, Permission
from src.orchestration.amend import (
    AmendmentContext,
    FlowNotFoundError,
    amend_plan,
)
from src.orchestration.compile import (
    ApprovalContext,
    ProposalRejectedError,
    TenantMismatchError,
    compile_proposal,
)
from src.orchestration.models import (
    DecisionKind,
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationEdge,
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
def approval():
    """Server-resolved context for the ORIGINAL compile that amendment supersedes."""
    return ApprovalContext(
        org_id=ORG_A,
        actor_id="cognito-sub-123",
        actor_role="org_admin",
        actor_kind=ActorKind.HUMAN,
        reason="Original plan accepted.",
    )


@pytest.fixture
def amender():
    """Server-resolved amendment context. `org_id` here is authoritative."""
    return AmendmentContext(
        org_id=ORG_A,
        actor_id="cognito-sub-operator",
        actor_role="org_admin",
        actor_kind=ActorKind.HUMAN,
        reason="Wave 1 turned out to be two waves.",
    )


def address(node_ref: str, *, epic: str = "epic-1", wave: str = "wave-1") -> str:
    return f"{FLOW}/{epic}/{wave}/{node_ref}"


def valid_proposal(*, org_id: str = ORG_A, **overrides) -> LoopProposal:
    """The original well-formed plan: two waves, chained."""
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


def amended_proposal(*, org_id: str = ORG_A, **overrides) -> LoopProposal:
    """The amendment: `story-b` is DROPPED, `story-c` is ADDED.

    `story-a` and the wave-1 eval keep their addresses and definitions verbatim, so
    they must keep their state and history. This shape exercises all three
    outcomes — dropped, added, unchanged — in one document.
    """
    payload = {
        "flow_slug": FLOW,
        "title": "Demo flow",
        "org_id": org_id,
        "spec_revision": SPEC_REVISION,
        "intent_ref": "4120",
        "nodes": [
            ProposedNode(address=address("story-a"), kind="story", title="Story A", issue_ref="4196"),
            ProposedNode(address=address("story-c"), kind="story", title="Story C (unplanned work)"),
            ProposedNode(address=address("eval"), kind="eval", title="Wave 1 eval"),
            ProposedNode(address=address("gate", wave="wave-2"), kind="gate", title="Human gate"),
        ],
        "edges": [
            ProposedEdge(from_address=address("story-a"), to_address=address("eval")),
            ProposedEdge(from_address=address("story-c"), to_address=address("eval")),
            ProposedEdge(from_address=address("eval"), to_address=address("gate", wave="wave-2")),
        ],
    }
    payload.update(overrides)
    return LoopProposal(**payload)


def invalid_amendment(org_id: str = ORG_A) -> LoopProposal:
    """A document that fails validation four ways: a container smuggled in as a
    node, a duplicate address, a cycle, and a wave with no eval."""
    return LoopProposal(
        flow_slug=FLOW,
        title="Hostile amendment",
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


def _token_context(org_id: str, *, user_id: str = "cognito-sub-operator"):
    """An authenticated caller in `org_id`.

    `org_id` on a `TokenContext` is authenticated-only — it comes from the Cognito
    claim and is never writable by a request header — which is why the route may
    use it as the tenant without further checking.
    """
    from datetime import UTC, datetime, timedelta

    from src.shared.schemas.auth import TokenContext

    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id="",
        department_id="",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


async def count_rows(session: AsyncSession, model) -> int:
    return (await session.execute(sa.select(sa.func.count()).select_from(model.__table__))).scalar_one()


async def nodes_by_address(session: AsyncSession, flow_slug: str = FLOW) -> dict[str, OrchestrationNode]:
    repo = OrchestrationRepository(session)
    flows = await repo.list_flows(org_id=ORG_A)
    flow = next(f for f in flows if f.slug == flow_slug)
    nodes = await repo.list_nodes(org_id=ORG_A, flow_id=flow.id)
    return {f"{flow.slug}/{n.epic_ref}/{n.wave_ref}/{n.node_ref}": n for n in nodes}


async def compile_original(session: AsyncSession, approval: ApprovalContext):
    """Establish version 1, the plan every amendment test supersedes."""
    return await compile_proposal(session, valid_proposal(), approval)


class TestAC28HappyPath:
    """AC-28: version 2 is created, version 1 is superseded AND still queryable."""

    @pytest.mark.asyncio
    async def test_amendment_creates_plan_version_2(self, session, approval, amender):
        await compile_original(session, approval)
        result = await amend_plan(session, (await compile_original(session, approval)).flow_id, amended_proposal(), amender)

        assert result.plan_version == 2
        assert result.superseded_version == 1

    @pytest.mark.asyncio
    async def test_version_1_is_marked_superseded(self, session, approval, amender):
        original = await compile_original(session, approval)
        await amend_plan(session, original.flow_id, amended_proposal(), amender)

        repo = OrchestrationRepository(session)
        versions = {p.version: p for p in await repo.list_plan_versions(org_id=ORG_A, flow_id=original.flow_id)}

        assert versions[1].superseded_at is not None, "version 1 must be marked superseded"
        assert versions[2].superseded_at is None, "version 2 must be the one in force"

    @pytest.mark.asyncio
    async def test_in_force_plan_is_version_2(self, session, approval, amender):
        original = await compile_original(session, approval)
        await amend_plan(session, original.flow_id, amended_proposal(), amender)

        repo = OrchestrationRepository(session)
        in_force = await repo.get_accepted_plan(org_id=ORG_A, flow_id=original.flow_id)

        assert in_force is not None and in_force.version == 2

    @pytest.mark.asyncio
    async def test_version_1_remains_queryable_with_its_original_node_set(self, session, approval, amender):
        """The whole point of superseding rather than mutating.

        If amendment overwrote the prior version, "what was approved at that gate"
        would become unanswerable — the first blast radius the issue names. The
        stored document must still list `story-b`, which the amendment dropped.
        """
        original = await compile_original(session, approval)
        await amend_plan(session, original.flow_id, amended_proposal(), amender)

        repo = OrchestrationRepository(session)
        versions = {p.version: p for p in await repo.list_plan_versions(org_id=ORG_A, flow_id=original.flow_id)}

        v1_addresses = {node["address"] for node in versions[1].plan_document["nodes"]}
        v2_addresses = {node["address"] for node in versions[2].plan_document["nodes"]}

        assert address("story-b") in v1_addresses, "version 1 must still record the node the amendment dropped"
        assert address("story-b") not in v2_addresses
        assert address("story-c") in v2_addresses
        assert address("story-c") not in v1_addresses, "version 1 must not have acquired the amendment's new node"

    @pytest.mark.asyncio
    async def test_both_plan_versions_coexist(self, session, approval, amender):
        original = await compile_original(session, approval)
        await amend_plan(session, original.flow_id, amended_proposal(), amender)

        assert await count_rows(session, OrchestrationAcceptedPlan) == 2

    @pytest.mark.asyncio
    async def test_amendment_reports_what_it_changed(self, session, approval, amender):
        original = await compile_original(session, approval)
        result = await amend_plan(session, original.flow_id, amended_proposal(), amender)

        assert result.nodes_created == 1, "story-c is new"
        assert result.nodes_superseded == 1, "story-b was dropped"
        assert result.already_amended is False


class TestNodeStateOutcomes:
    """Dropped -> superseded, added -> pending, unchanged -> untouched."""

    @pytest.mark.asyncio
    async def test_dropped_node_becomes_superseded(self, session, approval, amender):
        original = await compile_original(session, approval)
        await amend_plan(session, original.flow_id, amended_proposal(), amender)

        nodes = await nodes_by_address(session)
        assert nodes[address("story-b")].state == NodeState.SUPERSEDED.value

    @pytest.mark.asyncio
    async def test_added_node_becomes_pending(self, session, approval, amender):
        original = await compile_original(session, approval)
        await amend_plan(session, original.flow_id, amended_proposal(), amender)

        nodes = await nodes_by_address(session)
        assert nodes[address("story-c")].state == NodeState.PENDING.value

    @pytest.mark.asyncio
    async def test_unchanged_node_keeps_its_state_and_identity(self, session, approval, amender):
        """Same address + same definition => the SAME ROW, not a replacement.

        Identity is asserted via the node id, because a new row with the same
        address would silently reset attempts and orphan the run history even
        though the state value happened to match.
        """
        original = await compile_original(session, approval)
        before = await nodes_by_address(session)
        story_a_id = before[address("story-a")].id

        # Advance story-a so "kept its state" is a meaningful claim rather than
        # both sides coincidentally being `pending`.
        before[address("story-a")].state = NodeState.RUNNING.value
        await session.flush()

        await amend_plan(session, original.flow_id, amended_proposal(), amender)

        after = await nodes_by_address(session)
        assert after[address("story-a")].id == story_a_id, "the node row must be reused, not replaced"
        assert after[address("story-a")].state == NodeState.RUNNING.value, "an unchanged node must keep its state"

    @pytest.mark.asyncio
    async def test_already_passed_node_is_not_reset_by_an_amendment(self, session, approval, amender):
        """Re-planning must not throw away work already accepted (semantics 4).

        A `passed` node the amendment still lists is left exactly as it was — not
        reset to `pending`, not superseded.
        """
        original = await compile_original(session, approval)
        nodes = await nodes_by_address(session)
        nodes[address("story-a")].state = NodeState.PASSED.value
        nodes[address("story-a")].attempts = 3
        await session.flush()

        await amend_plan(session, original.flow_id, amended_proposal(), amender)

        after = await nodes_by_address(session)
        assert after[address("story-a")].state == NodeState.PASSED.value, "a passed node must not be reset"
        assert after[address("story-a")].attempts == 3, "run history must survive the amendment"

    @pytest.mark.asyncio
    async def test_passed_node_dropped_by_a_human_amendment_is_superseded(self, session, approval, amender):
        """`passed -> superseded` is legal for a HUMAN actor (explicit re-plan)."""
        original = await compile_original(session, approval)
        nodes = await nodes_by_address(session)
        nodes[address("story-b")].state = NodeState.PASSED.value
        await session.flush()

        await amend_plan(session, original.flow_id, amended_proposal(), amender)

        after = await nodes_by_address(session)
        assert after[address("story-b")].state == NodeState.SUPERSEDED.value

    @pytest.mark.asyncio
    async def test_service_actor_cannot_supersede_passed_work(self, session, approval):
        """The vocabulary marks `passed -> superseded` human-only, and amendment
        must honour it: the engine cannot discard accepted work by re-planning.

        This is why superseding goes through `transition()` rather than assigning
        `node.state` directly — the guard is the enforcement, not this module.
        """
        original = await compile_original(session, approval)
        nodes = await nodes_by_address(session)
        nodes[address("story-b")].state = NodeState.PASSED.value
        await session.flush()

        service_amender = AmendmentContext(
            org_id=ORG_A,
            actor_id="engine",
            actor_role="service",
            actor_kind=ActorKind.SERVICE,
            reason="automated re-plan",
        )

        with pytest.raises(ProposalRejectedError, match="cannot supersede"):
            await amend_plan(session, original.flow_id, amended_proposal(), service_amender)

        # The refusal must leave version 1 in force — a partial amendment is the
        # "nodes from two plan versions" blast radius.
        repo = OrchestrationRepository(session)
        in_force = await repo.get_accepted_plan(org_id=ORG_A, flow_id=original.flow_id)
        assert in_force is not None and in_force.version == 1
        after = await nodes_by_address(session)
        assert after[address("story-b")].state == NodeState.PASSED.value


class TestDecisionRecord:
    """One attributed, append-only decision row per amendment."""

    @pytest.mark.asyncio
    async def test_exactly_one_amend_decision_is_written(self, session, approval, amender):
        original = await compile_original(session, approval)
        await amend_plan(session, original.flow_id, amended_proposal(), amender)

        repo = OrchestrationRepository(session)
        decisions = await repo.list_decisions(org_id=ORG_A, flow_id=original.flow_id)
        amend_decisions = [d for d in decisions if d.kind == DecisionKind.PLAN_AMENDED.value]

        assert len(amend_decisions) == 1

    @pytest.mark.asyncio
    async def test_decision_carries_full_attribution(self, session, approval, amender):
        """Actor identity, the role held at amendment time, and the human/service
        discriminator as its own column — the three fields that make "was this
        approved by a human?" answerable after the fact."""
        original = await compile_original(session, approval)
        await amend_plan(session, original.flow_id, amended_proposal(), amender)

        repo = OrchestrationRepository(session)
        decision = next(d for d in await repo.list_decisions(org_id=ORG_A, flow_id=original.flow_id) if d.kind == DecisionKind.PLAN_AMENDED.value)

        assert decision.actor_id == "cognito-sub-operator"
        assert decision.actor_role == "org_admin"
        assert decision.actor_kind == ActorKind.HUMAN.value
        assert decision.org_id == ORG_A

    @pytest.mark.asyncio
    async def test_decision_records_the_superseded_and_created_versions(self, session, approval, amender):
        original = await compile_original(session, approval)
        await amend_plan(session, original.flow_id, amended_proposal(), amender)

        repo = OrchestrationRepository(session)
        decision = next(d for d in await repo.list_decisions(org_id=ORG_A, flow_id=original.flow_id) if d.kind == DecisionKind.PLAN_AMENDED.value)

        assert "v1 -> v2" in decision.reason
        assert "Wave 1 turned out to be two waves." in decision.reason, "the operator's own justification must survive verbatim"

    @pytest.mark.asyncio
    async def test_new_plan_version_points_at_its_decision(self, session, approval, amender):
        original = await compile_original(session, approval)
        result = await amend_plan(session, original.flow_id, amended_proposal(), amender)

        repo = OrchestrationRepository(session)
        in_force = await repo.get_accepted_plan(org_id=ORG_A, flow_id=original.flow_id)

        assert in_force.accepted_by_decision_id == result.decision_id

    @pytest.mark.asyncio
    async def test_amend_decision_kind_is_distinct_from_plan_accepted(self, session, approval, amender):
        """Amendment must be distinguishable from original acceptance in the audit
        trail — a shared kind would make "was this the plan we started with?"
        unanswerable."""
        original = await compile_original(session, approval)
        await amend_plan(session, original.flow_id, amended_proposal(), amender)

        repo = OrchestrationRepository(session)
        kinds = [d.kind for d in await repo.list_decisions(org_id=ORG_A, flow_id=original.flow_id)]

        assert DecisionKind.PLAN_ACCEPTED.value in kinds
        assert DecisionKind.PLAN_AMENDED.value in kinds


class TestAC17OrgScopedPermissionRegistration:
    """AC-17 (adversarial): the frozenset is asserted for EQUALITY.

    Shaped after `tests/budget/test_routes.py:344-360`. A permission missing from
    `_ORG_SCOPED_PERMISSIONS` lets a principal with an empty `org_id` skip the
    membership-deny AND short-circuit the `target_org_id` check (which requires a
    truthy `allowed_org_id`), passing the scope check entirely. Equality means the
    next permission added without registering it **fails this test** instead of
    silently opening that hole.
    """

    def test_org_scoped_permissions_equals_the_expected_frozenset(self):
        expected = frozenset(
            {
                Permission.ORG_READ,
                Permission.ORG_UPDATE,
                Permission.ORG_CREATE,
                Permission.ORG_DELETE,
                Permission.BUDGET_READ,
                Permission.BUDGET_UPDATE,
                Permission.RATELIMIT_READ,
                Permission.RATELIMIT_UPDATE,
                Permission.USAGE_READ,
                Permission.LOGS_READ,
                Permission.LOGS_EXPORT,
                Permission.USER_READ,
                Permission.USER_MANAGE,
                Permission.METRICS_READ,
                Permission.AGENT_REGISTER,
                Permission.PLAN_APPROVE,
            }
        )

        unregistered = sorted(p.value for p in expected - _ORG_SCOPED_PERMISSIONS)
        newly_registered = sorted(p.value for p in _ORG_SCOPED_PERMISSIONS - expected)

        assert _ORG_SCOPED_PERMISSIONS == expected, (
            "_ORG_SCOPED_PERMISSIONS changed.\n"
            f"  unregistered (a principal with an empty org_id can bypass the scope check): {unregistered}\n"
            f"  newly registered (add to this test's expected set): {newly_registered}"
        )

    def test_plan_approve_is_org_scoped(self):
        """Stated directly, so the reason survives even if the set above is edited."""
        assert Permission.PLAN_APPROVE in _ORG_SCOPED_PERMISSIONS

    def test_plan_approve_is_not_granted_to_unprivileged_roles(self):
        """Amendment is at least as privileged as gate approval, never less."""
        assert Permission.PLAN_APPROVE in ROLE_PERMISSIONS[AdminRole.PLATFORM_ADMIN]
        assert Permission.PLAN_APPROVE in ROLE_PERMISSIONS[AdminRole.ORG_ADMIN]
        assert Permission.PLAN_APPROVE not in ROLE_PERMISSIONS[AdminRole.DEPT_ADMIN]
        assert Permission.PLAN_APPROVE not in ROLE_PERMISSIONS[AdminRole.MEMBER]

    def test_frontend_permission_table_mirrors_the_backend_for_plan_approve(self):
        """The frontend `ROLE_PERMISSIONS` is hand-mirrored and has drifted before.

        A missing entry hides operator UI from someone the API would allow; a
        spurious one shows UI the API will 403. Checked as source text because the
        table is TypeScript.
        """
        from pathlib import Path

        auth_ts = (Path(__file__).resolve().parents[2] / "frontend/src/services/auth.ts").read_text()
        types_ts = (Path(__file__).resolve().parents[2] / "frontend/src/types/index.ts").read_text()

        assert "PLAN_APPROVE = 'plan:approve'" in types_ts, "frontend Permission enum is missing PLAN_APPROVE"
        assert auth_ts.count("Permission.PLAN_APPROVE") == 2, (
            "frontend ROLE_PERMISSIONS must grant PLAN_APPROVE to exactly platform_admin and org_admin, mirroring src/admin/config.py"
        )


class TestAC29ValidationParity:
    """AC-29 (adversarial): an amendment cannot be less validated than an original.

    If the amended path had its own validator, it would eventually accept a plan
    the original path refuses — the softer-door failure in a different disguise.
    """

    @pytest.mark.asyncio
    async def test_invalid_amendment_is_rejected(self, session, approval, amender):
        original = await compile_original(session, approval)

        with pytest.raises(ProposalRejectedError):
            await amend_plan(session, original.flow_id, invalid_amendment(), amender)

    @pytest.mark.asyncio
    async def test_invalid_amendment_writes_zero_new_rows(self, session, approval, amender):
        original = await compile_original(session, approval)
        plans_before = await count_rows(session, OrchestrationAcceptedPlan)
        nodes_before = await count_rows(session, OrchestrationNode)
        decisions_before = await count_rows(session, OrchestrationDecision)

        with pytest.raises(ProposalRejectedError):
            await amend_plan(session, original.flow_id, invalid_amendment(), amender)

        assert await count_rows(session, OrchestrationAcceptedPlan) == plans_before
        assert await count_rows(session, OrchestrationNode) == nodes_before
        assert await count_rows(session, OrchestrationDecision) == decisions_before

    @pytest.mark.asyncio
    async def test_invalid_amendment_leaves_version_1_in_force(self, session, approval, amender):
        original = await compile_original(session, approval)

        with pytest.raises(ProposalRejectedError):
            await amend_plan(session, original.flow_id, invalid_amendment(), amender)

        repo = OrchestrationRepository(session)
        in_force = await repo.get_accepted_plan(org_id=ORG_A, flow_id=original.flow_id)
        assert in_force is not None and in_force.version == 1

    @pytest.mark.asyncio
    async def test_rejection_reports_the_violations(self, session, approval, amender):
        """The author needs to know what was wrong, in one pass."""
        original = await compile_original(session, approval)

        with pytest.raises(ProposalRejectedError) as excinfo:
            await amend_plan(session, original.flow_id, invalid_amendment(), amender)

        rules = {violation.rule for violation in excinfo.value.violations}
        assert {"container_as_node", "duplicate_address"} <= rules

    @pytest.mark.asyncio
    async def test_amendment_uses_the_same_validator_as_compile(self):
        """Structural, not behavioural: `amend.py` imports `validate_proposal`
        from the proposal module rather than defining rules of its own. A second
        copy of the rules is free to drift, and the drift would be invisible until
        an amendment was accepted that an original submission would have refused.
        """
        from src.orchestration import amend as amend_module
        from src.orchestration.proposal import validate_proposal

        assert amend_module.validate_proposal is validate_proposal

        source = inspect.getsource(amend_module)
        assert "def validate_" not in source, "amend.py must not define its own validation rules"


class TestTenantIsolation:
    """A cross-org `flow_id` is 404, not 403 — no existence disclosure."""

    @pytest.mark.asyncio
    async def test_flow_from_another_org_is_not_found(self, session, approval, amender):
        """The flow exists, but not in the amender's tenant.

        `FlowNotFoundError` (404), never a permission error (403): a 403 would
        confirm the id is real and let a caller enumerate other tenants' flows by
        reading status codes.
        """
        original = await compile_original(session, approval)

        other_org_amender = AmendmentContext(
            org_id=ORG_B,
            actor_id="cognito-sub-intruder",
            actor_role="org_admin",
            reason="not my flow",
        )

        with pytest.raises(FlowNotFoundError):
            await amend_plan(session, original.flow_id, amended_proposal(org_id=ORG_B), other_org_amender)

    @pytest.mark.asyncio
    async def test_nonexistent_flow_is_not_found(self, session, amender):
        with pytest.raises(FlowNotFoundError):
            await amend_plan(session, "no-such-flow-id", amended_proposal(), amender)

    @pytest.mark.asyncio
    async def test_cross_org_attempt_writes_nothing(self, session, approval, amender):
        original = await compile_original(session, approval)
        plans_before = await count_rows(session, OrchestrationAcceptedPlan)

        other_org_amender = AmendmentContext(org_id=ORG_B, actor_id="intruder", actor_role="org_admin")

        with pytest.raises(FlowNotFoundError):
            await amend_plan(session, original.flow_id, amended_proposal(org_id=ORG_B), other_org_amender)

        assert await count_rows(session, OrchestrationAcceptedPlan) == plans_before
        nodes = await nodes_by_address(session)
        assert nodes[address("story-b")].state == NodeState.PENDING.value, "the other tenant's node must be untouched"

    @pytest.mark.asyncio
    async def test_document_declaring_another_tenant_is_rejected_not_rehomed(self, session, approval, amender):
        """Re-homing would be the worse failure: another tenant's plan quietly
        becoming this tenant's state, with this operator's name on the decision."""
        original = await compile_original(session, approval)

        with pytest.raises(TenantMismatchError):
            await amend_plan(session, original.flow_id, amended_proposal(org_id=ORG_B), amender)

    @pytest.mark.asyncio
    async def test_amendment_targeting_a_different_flow_is_rejected(self, session, approval, amender):
        """An amendment whose addresses name another flow would file its nodes
        under this flow while claiming to belong elsewhere."""
        original = await compile_original(session, approval)
        elsewhere = LoopProposal(
            flow_slug="other-flow",
            title="Other flow",
            org_id=ORG_A,
            spec_revision=SPEC_REVISION,
            nodes=[
                ProposedNode(address="other-flow/epic-1/wave-1/story-x", kind="story", title="X"),
                ProposedNode(address="other-flow/epic-1/wave-1/eval", kind="eval", title="Eval"),
            ],
        )

        with pytest.raises(ProposalRejectedError, match="must target the flow it amends"):
            await amend_plan(session, original.flow_id, elsewhere, amender)


class TestAtomicity:
    """A failure mid-amendment leaves version 1 current and no orphan nodes."""

    @pytest.mark.asyncio
    async def test_injected_failure_rolls_back_the_whole_amendment(self, session, approval, amender, monkeypatch):
        """Fail at the last write — the accepted-plan row — so the nodes and the
        decision are already inserted when it blows up. That is the case that
        would leave "nodes from two plan versions, no coherent state" if the
        savepoint were not doing its job.
        """
        original = await compile_original(session, approval)
        nodes_before = await count_rows(session, OrchestrationNode)

        async def boom(*_args, **_kwargs):
            raise RuntimeError("injected failure mid-amendment")

        monkeypatch.setattr(OrchestrationRepository, "record_accepted_plan", boom)

        with pytest.raises(RuntimeError, match="injected failure"):
            await amend_plan(session, original.flow_id, amended_proposal(), amender)

        repo = OrchestrationRepository(session)
        in_force = await repo.get_accepted_plan(org_id=ORG_A, flow_id=original.flow_id)

        assert in_force is not None and in_force.version == 1, "version 1 must still be current"
        assert await count_rows(session, OrchestrationAcceptedPlan) == 1
        assert await count_rows(session, OrchestrationNode) == nodes_before, "no orphan nodes from the failed amendment"

    @pytest.mark.asyncio
    async def test_rollback_restores_dropped_node_state(self, session, approval, amender, monkeypatch):
        """The supersede happens before the failure point, so this proves the
        in-Python state mutation is unwound too — not just the inserts."""
        original = await compile_original(session, approval)

        async def boom(*_args, **_kwargs):
            raise RuntimeError("injected failure mid-amendment")

        monkeypatch.setattr(OrchestrationRepository, "record_accepted_plan", boom)

        with pytest.raises(RuntimeError):
            await amend_plan(session, original.flow_id, amended_proposal(), amender)

        session.expire_all()
        nodes = await nodes_by_address(session)
        assert nodes[address("story-b")].state == NodeState.PENDING.value, "the superseded node must be restored by the rollback"
        assert address("story-c") not in nodes, "the amendment's new node must not survive"

    @pytest.mark.asyncio
    async def test_no_decision_row_survives_a_failed_amendment(self, session, approval, amender, monkeypatch):
        original = await compile_original(session, approval)
        decisions_before = await count_rows(session, OrchestrationDecision)

        async def boom(*_args, **_kwargs):
            raise RuntimeError("injected failure mid-amendment")

        monkeypatch.setattr(OrchestrationRepository, "record_accepted_plan", boom)

        with pytest.raises(RuntimeError):
            await amend_plan(session, original.flow_id, amended_proposal(), amender)

        assert await count_rows(session, OrchestrationDecision) == decisions_before, (
            "a decision attributing an amendment that never landed would be a false audit record"
        )

    @pytest.mark.asyncio
    async def test_amend_plan_does_not_commit(self, session, approval, amender):
        """The caller owns the transaction, so an amendment can land together with
        whatever else the request writes. Same convention as compile.py."""
        original = await compile_original(session, approval)
        await amend_plan(session, original.flow_id, amended_proposal(), amender)

        await session.rollback()

        assert await count_rows(session, OrchestrationAcceptedPlan) == 0, "amend_plan must not have committed"


class TestIdempotency:
    """R-NF2: submitting the same amendment twice yields ONE new version."""

    @pytest.mark.asyncio
    async def test_same_amendment_twice_creates_one_new_version(self, session, approval, amender):
        original = await compile_original(session, approval)

        first = await amend_plan(session, original.flow_id, amended_proposal(), amender)
        second = await amend_plan(session, original.flow_id, amended_proposal(), amender)

        assert first.plan_version == 2
        assert second.plan_version == 2, "a resubmission must not allocate version 3"
        assert await count_rows(session, OrchestrationAcceptedPlan) == 2

    @pytest.mark.asyncio
    async def test_resubmission_is_flagged_as_already_amended(self, session, approval, amender):
        """A caller reporting "N nodes superseded" must not present a retry as a
        fresh amendment."""
        original = await compile_original(session, approval)
        await amend_plan(session, original.flow_id, amended_proposal(), amender)
        second = await amend_plan(session, original.flow_id, amended_proposal(), amender)

        assert second.already_amended is True
        assert second.nodes_created == 0
        assert second.nodes_superseded == 0

    @pytest.mark.asyncio
    async def test_resubmission_writes_no_second_decision(self, session, approval, amender):
        original = await compile_original(session, approval)
        await amend_plan(session, original.flow_id, amended_proposal(), amender)
        await amend_plan(session, original.flow_id, amended_proposal(), amender)

        repo = OrchestrationRepository(session)
        amend_decisions = [d for d in await repo.list_decisions(org_id=ORG_A, flow_id=original.flow_id) if d.kind == DecisionKind.PLAN_AMENDED.value]

        assert len(amend_decisions) == 1, "a retry must not double-attribute"

    @pytest.mark.asyncio
    async def test_resubmission_does_not_re_supersede_nodes(self, session, approval, amender):
        """`SUPERSEDED` has no outgoing edges, so a naive re-pass would attempt an
        illegal transition — and a recorded rejection is the primary deviation
        detector (R-N2b), so a false positive there is not harmless."""
        original = await compile_original(session, approval)
        await amend_plan(session, original.flow_id, amended_proposal(), amender)
        await amend_plan(session, original.flow_id, amended_proposal(), amender)

        repo = OrchestrationRepository(session)
        rejections = [
            d for d in await repo.list_decisions(org_id=ORG_A, flow_id=original.flow_id) if d.kind == DecisionKind.TRANSITION_REJECTED.value
        ]

        assert rejections == [], "an idempotent re-pass must not record spurious transition rejections"

    @pytest.mark.asyncio
    async def test_a_genuinely_different_amendment_creates_version_3(self, session, approval, amender):
        """Idempotency must key on the document, not on "an amendment happened"."""
        original = await compile_original(session, approval)
        await amend_plan(session, original.flow_id, amended_proposal(), amender)

        third = amended_proposal()
        third.nodes.append(ProposedNode(address=address("story-d"), kind="story", title="Story D"))

        result = await amend_plan(session, original.flow_id, third, amender)

        assert result.plan_version == 3
        assert result.superseded_version == 2


class TestSequentialAmendments:
    """History accumulates: every version stays readable."""

    @pytest.mark.asyncio
    async def test_three_versions_all_remain_queryable(self, session, approval, amender):
        original = await compile_original(session, approval)
        await amend_plan(session, original.flow_id, amended_proposal(), amender)

        third = amended_proposal()
        third.nodes.append(ProposedNode(address=address("story-d"), kind="story", title="Story D"))
        await amend_plan(session, original.flow_id, third, amender)

        repo = OrchestrationRepository(session)
        versions = await repo.list_plan_versions(org_id=ORG_A, flow_id=original.flow_id)

        assert [p.version for p in versions] == [1, 2, 3]
        assert [p.superseded_at is None for p in versions] == [False, False, True], "only the newest version is in force"

    @pytest.mark.asyncio
    async def test_each_version_keeps_its_own_document(self, session, approval, amender):
        original = await compile_original(session, approval)
        await amend_plan(session, original.flow_id, amended_proposal(), amender)

        third = amended_proposal()
        third.nodes.append(ProposedNode(address=address("story-d"), kind="story", title="Story D"))
        await amend_plan(session, original.flow_id, third, amender)

        repo = OrchestrationRepository(session)
        versions = {p.version: p for p in await repo.list_plan_versions(org_id=ORG_A, flow_id=original.flow_id)}

        assert address("story-d") not in {n["address"] for n in versions[1].plan_document["nodes"]}
        assert address("story-d") not in {n["address"] for n in versions[2].plan_document["nodes"]}
        assert address("story-d") in {n["address"] for n in versions[3].plan_document["nodes"]}


class TestEdgesAndFirstAmendment:
    """Edge upsert reuse, and amending a flow with no plan in force."""

    @pytest.mark.asyncio
    async def test_new_edges_are_created_and_existing_ones_reused(self, session, approval, amender):
        original = await compile_original(session, approval)
        edges_before = await count_rows(session, OrchestrationEdge)

        result = await amend_plan(session, original.flow_id, amended_proposal(), amender)

        # story-c -> eval is new; story-a -> eval and eval -> gate already exist.
        assert result.edges_created == 1
        assert await count_rows(session, OrchestrationEdge) == edges_before + 1

    @pytest.mark.asyncio
    async def test_amending_a_flow_with_no_plan_in_force(self, session, amender):
        """A flow can exist without an accepted plan. Amending it creates version 1
        and reports no superseded version — rendered as "none" rather than "v0",
        which would name a version that never existed.
        """
        repo = OrchestrationRepository(session)
        flow = await repo.create_flow(org_id=ORG_A, slug=FLOW, title="Demo flow")

        result = await amend_plan(session, flow.id, amended_proposal(), amender)

        assert result.plan_version == 1
        assert result.superseded_version is None
        assert result.nodes_superseded == 0

        decision = next(d for d in await repo.list_decisions(org_id=ORG_A, flow_id=flow.id) if d.kind == DecisionKind.PLAN_AMENDED.value)
        assert "none -> v1" in decision.reason


class TestRouteAuthorization:
    """Adversarial: the HTTP surface. 403 writes zero rows; cross-org is 404.

    These are route-level rather than `amend_plan`-level because the permission
    check and the org resolution both live at the boundary — that is where a
    caller actually arrives, and testing only the service layer would leave the
    door itself unexercised.
    """

    @pytest.fixture
    def app_with_router(self, session):
        """A minimal app carrying only the orchestration router.

        Deliberately not the full `create_app()`: that pulls the whole middleware
        stack (Cognito, budget, rate-limit) and would make an authz assertion here
        depend on all of it. The router and its dependencies are the unit.
        """
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse

        from src.auth.dependencies import get_current_user
        from src.orchestration.routes import router as orchestration_router
        from src.shared.database import get_db
        from src.shared.exceptions import BedrockGatewayError

        app = FastAPI()
        app.include_router(orchestration_router)

        # `AccessDeniedError` carries status_code=403 and is translated to a 403
        # response by the app-level BedrockGatewayError handler in `create_app()`
        # (app.py:213) — the same mechanism every other gated route relies on.
        # Registered here because this minimal app skips create_app(); without it a
        # denied caller surfaces as 500 and the authz assertions would be testing
        # the harness rather than the route.
        @app.exception_handler(BedrockGatewayError)
        async def _gateway_error_handler(_request: Request, exc: BedrockGatewayError):
            return JSONResponse(status_code=exc.status_code, content={"error": exc.error, "message": exc.message})

        async def override_db():
            yield session

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = lambda: _token_context(ORG_A)
        return app

    @staticmethod
    def _client(app, *, permitted: bool, role: str = "org_admin"):
        from unittest.mock import AsyncMock, MagicMock

        from fastapi.testclient import TestClient

        from src.admin.access_control import AccessControl
        from src.admin.config import AdminRole
        from src.admin.exceptions import AccessDeniedError
        from src.orchestration.routes import get_access_control

        access = MagicMock(spec=AccessControl)
        if permitted:
            access.check_permission = AsyncMock(return_value=True)
        else:
            access.check_permission = AsyncMock(
                side_effect=AccessDeniedError(
                    message="Permission 'plan:approve' is required for this operation",
                    required_permission=Permission.PLAN_APPROVE.value,
                    user_role="member",
                )
            )
        access.get_user_role = AsyncMock(return_value=(AdminRole(role), ORG_A, None))

        app.dependency_overrides[get_access_control] = lambda: access
        return TestClient(app, raise_server_exceptions=False)

    @pytest.mark.asyncio
    async def test_caller_without_the_permission_gets_403(self, session, approval, app_with_router):
        original = await compile_original(session, approval)
        client = self._client(app_with_router, permitted=False)

        response = client.post(
            f"/orchestration/flows/{original.flow_id}/amendments",
            json=amended_proposal().model_dump(mode="json"),
        )

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_denied_caller_writes_zero_rows(self, session, approval, app_with_router):
        """The load-bearing half: a refused caller must not have amended anything."""
        original = await compile_original(session, approval)
        plans_before = await count_rows(session, OrchestrationAcceptedPlan)
        decisions_before = await count_rows(session, OrchestrationDecision)
        nodes_before = await count_rows(session, OrchestrationNode)

        client = self._client(app_with_router, permitted=False)
        client.post(
            f"/orchestration/flows/{original.flow_id}/amendments",
            json=amended_proposal().model_dump(mode="json"),
        )

        assert await count_rows(session, OrchestrationAcceptedPlan) == plans_before
        assert await count_rows(session, OrchestrationDecision) == decisions_before
        assert await count_rows(session, OrchestrationNode) == nodes_before

        nodes = await nodes_by_address(session)
        assert nodes[address("story-b")].state == NodeState.PENDING.value, "no node may have been superseded by a denied caller"

    @pytest.mark.asyncio
    async def test_caller_from_another_org_gets_404_not_403(self, session, approval, app_with_router):
        """404, never 403 — a 403 would confirm the flow_id exists elsewhere and
        let a caller enumerate other tenants' flows by status code."""
        original = await compile_original(session, approval)

        from src.auth.dependencies import get_current_user

        app_with_router.dependency_overrides[get_current_user] = lambda: _token_context(ORG_B)
        client = self._client(app_with_router, permitted=True)

        response = client.post(
            f"/orchestration/flows/{original.flow_id}/amendments",
            json=amended_proposal(org_id=ORG_B).model_dump(mode="json"),
        )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_permitted_caller_amends_successfully(self, session, approval, app_with_router):
        original = await compile_original(session, approval)
        client = self._client(app_with_router, permitted=True)

        response = client.post(
            f"/orchestration/flows/{original.flow_id}/amendments",
            json=amended_proposal().model_dump(mode="json"),
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["plan_version"] == 2
        assert body["superseded_version"] == 1
        assert body["nodes_superseded"] == 1

    @pytest.mark.asyncio
    async def test_invalid_document_is_422_with_violations(self, session, approval, app_with_router):
        original = await compile_original(session, approval)
        client = self._client(app_with_router, permitted=True)

        response = client.post(
            f"/orchestration/flows/{original.flow_id}/amendments",
            json=invalid_amendment().model_dump(mode="json"),
        )

        assert response.status_code == 422
        rules = {violation["rule"] for violation in response.json()["detail"]["violations"]}
        assert "container_as_node" in rules

    @pytest.mark.asyncio
    async def test_superseded_version_is_still_readable_over_http(self, session, approval, app_with_router):
        """The smoke test's second half: after amending, version 1 still returns
        its original node set. This is the guarantee amendment rests on."""
        original = await compile_original(session, approval)
        client = self._client(app_with_router, permitted=True)

        client.post(
            f"/orchestration/flows/{original.flow_id}/amendments",
            json=amended_proposal().model_dump(mode="json"),
        )

        response = client.get(f"/orchestration/flows/{original.flow_id}/plans?version=1")

        assert response.status_code == 200, response.text
        versions = response.json()
        assert len(versions) == 1
        assert versions[0]["version"] == 1
        assert versions[0]["superseded_at"] is not None
        assert address("story-b") in {node["address"] for node in versions[0]["plan_document"]["nodes"]}

    @pytest.mark.asyncio
    async def test_plans_read_is_gated_on_the_same_permission(self, session, approval, app_with_router):
        original = await compile_original(session, approval)
        client = self._client(app_with_router, permitted=False)

        response = client.get(f"/orchestration/flows/{original.flow_id}/plans")

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_decision_records_the_role_resolved_at_the_boundary(self, session, approval, app_with_router):
        """The route snapshots the role from the same resolver the permission check
        used, so attribution reflects the authority actually granted."""
        original = await compile_original(session, approval)
        client = self._client(app_with_router, permitted=True, role="platform_admin")

        client.post(
            f"/orchestration/flows/{original.flow_id}/amendments",
            json=amended_proposal().model_dump(mode="json"),
        )

        repo = OrchestrationRepository(session)
        decision = next(d for d in await repo.list_decisions(org_id=ORG_A, flow_id=original.flow_id) if d.kind == DecisionKind.PLAN_AMENDED.value)
        assert decision.actor_role == "platform_admin"


class TestAmendmentContext:
    """The context is server-resolved; conversion to the compile path is faithful."""

    def test_actor_kind_defaults_to_human(self):
        """Amending a plan is a human act; a service actor must say so explicitly."""
        actor = AmendmentContext(org_id=ORG_A, actor_id="u", actor_role="org_admin")
        assert actor.actor_kind is ActorKind.HUMAN

    def test_to_approval_preserves_every_attribution_field(self):
        actor = AmendmentContext(
            org_id=ORG_A,
            actor_id="u-1",
            actor_role="platform_admin",
            actor_kind=ActorKind.SERVICE,
            reason="because",
        )
        approval = actor.to_approval()

        assert (approval.org_id, approval.actor_id, approval.actor_role) == (ORG_A, "u-1", "platform_admin")
        assert approval.actor_kind is ActorKind.SERVICE
        assert approval.reason == "because"

    def test_context_is_frozen(self):
        """Server-resolved context must not be mutable after the route builds it."""
        actor = AmendmentContext(org_id=ORG_A, actor_id="u", actor_role="org_admin")
        with pytest.raises(Exception):
            actor.org_id = ORG_B  # type: ignore[misc]
