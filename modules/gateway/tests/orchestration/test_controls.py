"""Gate approval, rejection and loop-resume controls (Issue #4213).

The story's guarantee is that a gate decision becomes **evidence**: an authorized
human approves or rejects, and the engine records who decided what, in which role,
permanently. So the tests here fall into three groups, and each one covers a
failure mode that is silent if it is not asserted.

**The decision is attributed** (AC-5 / AC-6). Approving writes an append-only row
carrying the acting identity, the role held at decision time, and
``actor_kind="human"``. Reject targets ``rejected_at_gate`` — **not** the
nonexistent ``rejected``, which ``NodeState`` raises ``ValueError`` on and which a
frontend once emitted with no backend writer.

**The authority cannot be bypassed** (AC-9, adversarial). Three separate doors are
checked, because each is a different bug:

- a caller **without** ``PLAN_APPROVE`` gets 403;
- a caller from **another org** gets **404** for a valid gate id — never 403,
  which would confirm the id exists and let a caller enumerate another tenant's
  flows by status code;
- a request that tries to **assert ``actor_kind`` in its body** is rejected. The
  issue's AC-9 says such a field must be "ignored"; ``extra="forbid"`` is
  deliberately *stronger* than ignoring it, and the test asserts the 422 plus the
  fact that no row was written. An ignored field looks accepted to the caller, and
  a service caller who believes it claimed human actor kind has been told
  something false.

**The record cannot be rewritten.** An UPDATE against a decision row raises
``AppendOnlyViolationError``. Without that, gate attribution is rewritable and the
EPIC's central guarantee becomes unverifiable.

Plus **AC-17**, the load-bearing adversarial one: ``_ORG_SCOPED_PERMISSIONS`` is
asserted for **equality**. A permission absent from that set is evaluated
*globally*, which for approval authority means cross-org gate approval. Equality
means the next permission added without a deliberate scoping decision fails CI.

And **R-O3f**: the per-run pause/steer/abort seam answers 501. A control that
appears to pause a run without doing so is worse than no control, because an
operator who believes a run is paused stops watching it.

Session and app fixtures mirror `test_read_api.py`, including its two pysqlite
hooks.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.access_control import _ORG_SCOPED_PERMISSIONS
from src.admin.config import ROLE_PERMISSIONS, AdminRole, Permission
from src.orchestration.models import (
    AppendOnlyViolationError,
    DecisionKind,
    NodeKind,
    OrchestrationDecision,
    OrchestrationFlow,
    OrchestrationNode,
)
from src.orchestration.repository import OrchestrationRepository
from src.orchestration.state import ActorKind, NodeState
from src.shared.models.base import Base
from src.shared.schemas.auth import TokenContext

ORG_A = "org-alpha"
ORG_B = "org-beta"
FLOW_SLUG = "delivery-loop"
USER_ID = "cognito-sub-approver"
ACTOR_ROLE = "org_admin"


def approve_route(gate_id: str) -> str:
    return f"/orchestration/gates/{gate_id}/approve"


def reject_route(gate_id: str) -> str:
    return f"/orchestration/gates/{gate_id}/reject"


def resume_route(node_id: str) -> str:
    return f"/orchestration/nodes/{node_id}/resume"


def decisions_route(flow_id: str) -> str:
    return f"/orchestration/flows/{flow_id}/decisions"


@pytest.fixture
async def session():
    """In-memory SQLite session with working SAVEPOINTs. See test_read_api.py."""
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


def _token_context(org_id: str, *, user_id: str = USER_ID) -> TokenContext:
    """An authenticated caller in `org_id`.

    `org_id` is an authenticated Cognito claim, never writable by a request
    header, which is what lets the route use it as the tenant directly — and is
    why `actor_kind` derived from this context cannot be forged by a caller.
    """
    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id="",
        department_id="",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def app_with_router(session):
    """A minimal app carrying only the controls router.

    Deliberately not `create_app()`: that pulls the whole middleware stack and
    would make an authz assertion here depend on all of it.
    """
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from src.auth.dependencies import get_current_user
    from src.orchestration.controls import router as controls_router
    from src.shared.database import get_db
    from src.shared.exceptions import BedrockGatewayError

    app = FastAPI()
    app.include_router(controls_router)

    @app.exception_handler(BedrockGatewayError)
    async def _gateway_error_handler(_request: Request, exc: BedrockGatewayError):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.error, "message": exc.message})

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: _token_context(ORG_A)
    return app


def client_for(app, *, permitted: bool = True, org_id: str = ORG_A):
    """A TestClient whose access control is stubbed to permit or deny.

    Denial is expressed as `PLAN_APPROVE` because that is the permission the
    write routes gate on — asserting a different permission's denial here would
    test a check the route does not make.
    """
    from unittest.mock import AsyncMock, MagicMock

    from fastapi.testclient import TestClient

    from src.admin.access_control import AccessControl
    from src.admin.exceptions import AccessDeniedError
    from src.auth.dependencies import get_current_user
    from src.orchestration.controls import get_access_control

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
    access.get_user_role = AsyncMock(return_value=(AdminRole(ACTOR_ROLE), org_id, None))

    app.dependency_overrides[get_access_control] = lambda: access
    app.dependency_overrides[get_current_user] = lambda: _token_context(org_id)
    return TestClient(app, raise_server_exceptions=False)


async def seed_flow(
    session: AsyncSession,
    *,
    org_id: str = ORG_A,
    slug: str = FLOW_SLUG,
) -> OrchestrationFlow:
    repo = OrchestrationRepository(session)
    return await repo.create_flow(org_id=org_id, slug=slug, title="Delivery loop", intent_ref="4120")


async def seed_node(
    session: AsyncSession,
    flow: OrchestrationFlow,
    *,
    node_ref: str = "gate-1",
    kind: str = NodeKind.GATE.value,
    state: str = NodeState.AWAITING_GATE.value,
    attempts: int = 0,
    org_id: str = ORG_A,
) -> OrchestrationNode:
    """One node, defaulting to a gate that is awaiting an answer."""
    repo = OrchestrationRepository(session)
    node = await repo.add_node(
        org_id=org_id,
        flow_id=flow.id,
        epic_ref="epic-1",
        wave_ref="wave-1",
        node_ref=node_ref,
        kind=kind,
        title=f"Node {node_ref}",
    )
    node.state = state
    node.attempts = attempts
    await session.flush()
    return node


async def state_of(session: AsyncSession, node_id: str) -> str:
    """Re-read a node's state straight from the database.

    A bare `session.get` would serve the instance already in the identity map,
    whose `state` predates the route's UPDATE — so every assertion here would read
    the seeded value and pass regardless of what the route did. `expire_all()` is
    deliberately NOT used instead: it also expires the `flow` the test still holds,
    and the next `flow.id` access then triggers a lazy load that raises under
    asyncio. A scalar SELECT touches neither problem.
    """
    return (await session.execute(select(OrchestrationNode.state).where(OrchestrationNode.id == node_id))).scalar_one()


async def attempts_of(session: AsyncSession, node_id: str) -> int:
    """Re-read `attempts` from the database. Same reasoning as `state_of`."""
    return (await session.execute(select(OrchestrationNode.attempts).where(OrchestrationNode.id == node_id))).scalar_one()


async def decisions_for(session: AsyncSession, flow_id: str, *, org_id: str = ORG_A) -> list[OrchestrationDecision]:
    return await OrchestrationRepository(session).list_decisions(org_id=org_id, flow_id=flow_id)


class TestAC5Approve:
    """AC-5: an authorized approver moves a gate to `passed`, attributed."""

    @pytest.mark.asyncio
    async def test_approve_moves_gate_to_passed(self, session, app_with_router):
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)

        response = client_for(app_with_router).post(approve_route(gate.id), json={"reason": "looks right"})

        assert response.status_code == 200, response.text
        assert response.json()["state"] == NodeState.PASSED.value
        assert await state_of(session, gate.id) == NodeState.PASSED.value

    @pytest.mark.asyncio
    async def test_approve_writes_an_attributed_decision_row(self, session, app_with_router):
        """The row is the point of the story — identity, role, and the human
        discriminator, all three, on an append-only row."""
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)

        client_for(app_with_router).post(approve_route(gate.id), json={"reason": "ship it"})

        rows = await decisions_for(session, flow.id)
        approvals = [row for row in rows if row.kind == DecisionKind.GATE_APPROVED.value]
        assert len(approvals) == 1

        decision = approvals[0]
        assert decision.actor_id == USER_ID
        assert decision.actor_role == ACTOR_ROLE
        # The discriminator that makes "was this approved by a human?" answerable.
        assert decision.actor_kind == ActorKind.HUMAN.value
        assert decision.node_id == gate.id
        assert decision.from_state == NodeState.AWAITING_GATE.value
        assert decision.to_state == NodeState.PASSED.value
        assert "ship it" in decision.reason

    @pytest.mark.asyncio
    async def test_approving_a_node_not_at_a_gate_is_refused_and_recorded(self, session, app_with_router):
        """`running -> passed` is legal for a human in `state.py` (that is how a
        green evaluation promotes a node with no gate). A *gate answer* is a
        different act, so the control must narrow it — otherwise pressing approve
        marks work that is still in flight as passed with no gate ever raised."""
        flow = await seed_flow(session)
        node = await seed_node(session, flow, state=NodeState.RUNNING.value)

        response = client_for(app_with_router).post(approve_route(node.id), json={})

        assert response.status_code == 409
        assert await state_of(session, node.id) == NodeState.RUNNING.value
        kinds = [row.kind for row in await decisions_for(session, flow.id)]
        assert DecisionKind.TRANSITION_REJECTED.value in kinds


class TestAC6Reject:
    """AC-6: reject lands on `rejected_at_gate` and records a reason."""

    @pytest.mark.asyncio
    async def test_reject_moves_gate_to_rejected_at_gate(self, session, app_with_router):
        """`rejected_at_gate`, never `rejected`. The latter is a phantom state
        with no backend writer that `NodeState` raises ValueError on."""
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)

        response = client_for(app_with_router).post(reject_route(gate.id), json={"reason": "needs tests"})

        assert response.status_code == 200, response.text
        assert response.json()["state"] == NodeState.REJECTED_AT_GATE.value
        assert await state_of(session, gate.id) == NodeState.REJECTED_AT_GATE.value

    @pytest.mark.asyncio
    async def test_reject_records_the_reason(self, session, app_with_router):
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)

        client_for(app_with_router).post(reject_route(gate.id), json={"reason": "needs tests"})

        rejections = [row for row in await decisions_for(session, flow.id) if row.kind == DecisionKind.GATE_REJECTED.value]
        assert len(rejections) == 1
        assert "needs tests" in rejections[0].reason
        assert rejections[0].actor_kind == ActorKind.HUMAN.value

    @pytest.mark.asyncio
    async def test_rejected_is_never_written_as_a_state(self, session, app_with_router):
        """Stated directly: no surface may persist the phantom literal."""
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)

        client_for(app_with_router).post(reject_route(gate.id), json={})

        assert await state_of(session, gate.id) != "rejected"
        to_states = {row.to_state for row in await decisions_for(session, flow.id)}
        assert "rejected" not in to_states


class TestAC9Resume:
    """AC-9: resume recovers `failed` and — human-only — `halted`."""

    @pytest.mark.asyncio
    async def test_resume_moves_failed_to_ready(self, session, app_with_router):
        flow = await seed_flow(session)
        node = await seed_node(session, flow, kind=NodeKind.STORY.value, state=NodeState.FAILED.value)

        response = client_for(app_with_router).post(resume_route(node.id), json={"reason": "transient"})

        assert response.status_code == 200, response.text
        assert response.json()["state"] == NodeState.READY.value
        assert await state_of(session, node.id) == NodeState.READY.value

    @pytest.mark.asyncio
    async def test_resume_moves_halted_to_ready_as_a_human(self, session, app_with_router):
        """`halted -> ready` is the human-only override (R-Q9c). It succeeds here
        because `actor_kind` is derived from the authenticated session."""
        flow = await seed_flow(session)
        node = await seed_node(session, flow, kind=NodeKind.STORY.value, state=NodeState.HALTED.value, attempts=3)

        response = client_for(app_with_router).post(resume_route(node.id), json={"reason": "root cause fixed"})

        assert response.status_code == 200, response.text
        assert response.json()["actor_kind"] == ActorKind.HUMAN.value
        assert await state_of(session, node.id) == NodeState.READY.value

    @pytest.mark.asyncio
    async def test_clearing_a_halt_is_recorded_as_an_override(self, session, app_with_router):
        """A halt override bypasses the defect-cycle bound that stopped spend, so
        it must be findable as such rather than inferred from `from_state`."""
        flow = await seed_flow(session)
        node = await seed_node(session, flow, kind=NodeKind.STORY.value, state=NodeState.HALTED.value, attempts=3)

        client_for(app_with_router).post(resume_route(node.id), json={})

        overrides = [row for row in await decisions_for(session, flow.id) if row.kind == DecisionKind.HALT_OVERRIDDEN.value]
        assert len(overrides) == 1
        assert overrides[0].actor_kind == ActorKind.HUMAN.value
        assert overrides[0].from_state == NodeState.HALTED.value
        assert overrides[0].to_state == NodeState.READY.value

    @pytest.mark.asyncio
    async def test_resume_does_not_consume_the_defect_cycle_bound(self, session, app_with_router):
        """`attempts` is incremented by dispatch, when the node actually runs.
        Bumping it on resume would spend the cycle bound on work that has not
        run — and for a node resumed from `halted`, immediately re-halt it."""
        flow = await seed_flow(session)
        node = await seed_node(session, flow, kind=NodeKind.STORY.value, state=NodeState.FAILED.value, attempts=2)

        client_for(app_with_router).post(resume_route(node.id), json={})

        assert await attempts_of(session, node.id) == 2

    @pytest.mark.asyncio
    async def test_a_body_asserting_actor_kind_is_rejected(self, session, app_with_router):
        """AC-9 adversarial: a service caller must not be able to claim human
        actor kind. `extra="forbid"` makes the attempt a 422 rather than a
        silently-dropped field — and nothing moves, and nothing is recorded."""
        flow = await seed_flow(session)
        node = await seed_node(session, flow, kind=NodeKind.STORY.value, state=NodeState.HALTED.value)

        response = client_for(app_with_router).post(
            resume_route(node.id),
            json={"reason": "sneaky", "actor_kind": ActorKind.SERVICE.value},
        )

        assert response.status_code == 422
        assert await state_of(session, node.id) == NodeState.HALTED.value
        assert await decisions_for(session, flow.id) == []

    @pytest.mark.asyncio
    async def test_a_gate_body_asserting_actor_kind_is_rejected(self, session, app_with_router):
        """The same forbidding applies to the approve path."""
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)

        response = client_for(app_with_router).post(
            approve_route(gate.id),
            json={"actor_kind": ActorKind.SERVICE.value},
        )

        assert response.status_code == 422
        assert await state_of(session, gate.id) == NodeState.AWAITING_GATE.value

    @pytest.mark.asyncio
    async def test_resuming_a_node_in_a_live_state_is_refused(self, session, app_with_router):
        """`pending -> ready` is legal for the engine when predecessors are
        satisfied, so without a narrowing guard a "resume" button could take it
        and claim a node was recovered when nothing was wrong."""
        flow = await seed_flow(session)
        node = await seed_node(session, flow, kind=NodeKind.STORY.value, state=NodeState.PENDING.value)

        response = client_for(app_with_router).post(resume_route(node.id), json={})

        assert response.status_code == 409
        assert await state_of(session, node.id) == NodeState.PENDING.value
        kinds = [row.kind for row in await decisions_for(session, flow.id)]
        assert DecisionKind.TRANSITION_REJECTED.value in kinds


class TestAuthorization:
    """Adversarial: neither door opens without the permission."""

    @pytest.mark.asyncio
    async def test_approve_without_the_permission_is_403(self, session, app_with_router):
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)

        response = client_for(app_with_router, permitted=False).post(approve_route(gate.id), json={})

        assert response.status_code == 403
        assert await state_of(session, gate.id) == NodeState.AWAITING_GATE.value

    @pytest.mark.asyncio
    async def test_an_unauthorized_attempt_is_recorded(self, session, app_with_router):
        """R-N2b: recorded rejections are the primary detector for off-plan
        activity, so an in-org but unauthorized attempt leaves evidence."""
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)

        client_for(app_with_router, permitted=False).post(approve_route(gate.id), json={})

        kinds = [row.kind for row in await decisions_for(session, flow.id)]
        assert DecisionKind.TRANSITION_REJECTED.value in kinds

    @pytest.mark.asyncio
    async def test_resume_without_the_permission_is_403(self, session, app_with_router):
        flow = await seed_flow(session)
        node = await seed_node(session, flow, kind=NodeKind.STORY.value, state=NodeState.HALTED.value)

        response = client_for(app_with_router, permitted=False).post(resume_route(node.id), json={})

        assert response.status_code == 403
        assert await state_of(session, node.id) == NodeState.HALTED.value


class TestTenantIsolation:
    """A cross-org id is 404 — never 403, which would confirm it exists."""

    @pytest.mark.asyncio
    async def test_another_orgs_gate_is_404_not_403(self, session, app_with_router):
        """The headline isolation guarantee. The gate id is real and awaiting an
        answer; it just belongs to another tenant. A 403 would let an outsider
        enumerate another org's flows by status code."""
        foreign_flow = await seed_flow(session, org_id=ORG_B, slug="other-loop")
        foreign_gate = await seed_node(session, foreign_flow, org_id=ORG_B)

        response = client_for(app_with_router, org_id=ORG_A).post(approve_route(foreign_gate.id), json={})

        assert response.status_code == 404
        assert await state_of(session, foreign_gate.id) == NodeState.AWAITING_GATE.value

    @pytest.mark.asyncio
    async def test_another_orgs_gate_and_a_missing_gate_are_indistinguishable(self, session, app_with_router):
        """Same status code, so the two cases cannot be told apart."""
        foreign_flow = await seed_flow(session, org_id=ORG_B, slug="other-loop")
        foreign_gate = await seed_node(session, foreign_flow, org_id=ORG_B)
        client = client_for(app_with_router, org_id=ORG_A)

        cross_org = client.post(approve_route(foreign_gate.id), json={})
        nonexistent = client.post(approve_route("11111111-2222-3333-4444-555555555555"), json={})

        assert cross_org.status_code == nonexistent.status_code == 404

    @pytest.mark.asyncio
    async def test_another_orgs_node_cannot_be_resumed(self, session, app_with_router):
        foreign_flow = await seed_flow(session, org_id=ORG_B, slug="other-loop")
        foreign_node = await seed_node(session, foreign_flow, kind=NodeKind.STORY.value, state=NodeState.HALTED.value, org_id=ORG_B)

        response = client_for(app_with_router, org_id=ORG_A).post(resume_route(foreign_node.id), json={})

        assert response.status_code == 404
        assert await state_of(session, foreign_node.id) == NodeState.HALTED.value

    @pytest.mark.asyncio
    async def test_another_orgs_decisions_are_404(self, session, app_with_router):
        foreign_flow = await seed_flow(session, org_id=ORG_B, slug="other-loop")

        response = client_for(app_with_router, org_id=ORG_A).get(decisions_route(foreign_flow.id))

        assert response.status_code == 404


class TestAppendOnly:
    """Adversarial: gate attribution cannot be rewritten."""

    @pytest.mark.asyncio
    async def test_updating_a_decision_row_raises(self, session, app_with_router):
        """The repository exposes no update method, but "no method" only stops the
        accidental case. A caller holding the session can mutate a loaded instance,
        so the guarantee has to hold at the flush boundary."""
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)
        client_for(app_with_router).post(approve_route(gate.id), json={"reason": "original"})

        rows = await decisions_for(session, flow.id)
        decision = next(row for row in rows if row.kind == DecisionKind.GATE_APPROVED.value)

        decision.actor_id = "somebody-else"
        with pytest.raises(AppendOnlyViolationError):
            await session.flush()

    @pytest.mark.asyncio
    async def test_a_bulk_update_against_decisions_also_raises(self, session, app_with_router):
        """The ORM-level hook fires on the mapped path. A bulk `update()` with
        synchronize_session is the other way a caller might try."""
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)
        client_for(app_with_router).post(approve_route(gate.id), json={})

        rows = await decisions_for(session, flow.id)
        decision = next(row for row in rows if row.kind == DecisionKind.GATE_APPROVED.value)

        with pytest.raises(AppendOnlyViolationError):
            await session.execute(
                update(OrchestrationDecision)
                .where(OrchestrationDecision.id == decision.id)
                .values(actor_kind=ActorKind.SERVICE.value)
                .execution_options(synchronize_session="fetch")
            )
            await session.flush()


class TestAC17OrgScopedPermissionRegistration:
    """AC-17 (adversarial, load-bearing): the frozenset is asserted for EQUALITY.

    A permission absent from `_ORG_SCOPED_PERMISSIONS` is evaluated **globally**:
    a principal with an empty `org_id` skips the membership-deny AND
    short-circuits the `target_org_id` check (which requires a truthy
    `allowed_org_id`), passing the scope check entirely. For approval authority
    that means a member of one org can approve another org's gates.

    Equality — not `in` — so that adding any future permission without a
    deliberate scoping decision fails CI. Deliberately duplicated from
    `test_amend.py`: this story's control depends on the property, and a test that
    lives only next to the amendment route would let a future edit there quietly
    remove the guarantee this route rests on.
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
                # Issue #4528: draft registration is org-scoped, and load-bearingly
                # so — it is the only promotion-adjacent permission AdminRole.MEMBER
                # holds, so it is the one where an empty-org bypass would actually be
                # reachable by an ordinary principal.
                Permission.PLAN_DRAFT,
            }
        )

        unregistered = sorted(p.value for p in expected - _ORG_SCOPED_PERMISSIONS)
        newly_registered = sorted(p.value for p in _ORG_SCOPED_PERMISSIONS - expected)

        assert _ORG_SCOPED_PERMISSIONS == expected, (
            "_ORG_SCOPED_PERMISSIONS changed.\n"
            f"  unregistered (a principal with an empty org_id can bypass the scope check): {unregistered}\n"
            f"  newly registered (add to this test's expected set): {newly_registered}"
        )

    def test_the_gate_approval_permission_is_org_scoped(self):
        """Stated directly, so the reason survives an edit to the set above."""
        assert Permission.PLAN_APPROVE in _ORG_SCOPED_PERMISSIONS

    def test_gate_approval_is_not_granted_to_unprivileged_roles(self):
        """The manual post-deploy grant step targets these roles and no others."""
        assert Permission.PLAN_APPROVE in ROLE_PERMISSIONS[AdminRole.PLATFORM_ADMIN]
        assert Permission.PLAN_APPROVE in ROLE_PERMISSIONS[AdminRole.ORG_ADMIN]
        assert Permission.PLAN_APPROVE not in ROLE_PERMISSIONS[AdminRole.DEPT_ADMIN]
        assert Permission.PLAN_APPROVE not in ROLE_PERMISSIONS[AdminRole.MEMBER]


class TestFrontendPermissionMirrorParity:
    """X4: the backend enum and BOTH frontend mirrors must agree.

    Two different files, and the drift is real rather than hypothetical —
    `AGENT_REGISTER` was missing from the enum mirror while the backend had it.
    `Permission` is only *imported* into `auth.ts`, so editing the role map alone
    grants a permission the enum does not contain: the control then either fails
    to compile or, worse, is silently absent from the UI.

    Asserted as source text because the tables are TypeScript, and a runtime
    Python test cannot see them.
    """

    @staticmethod
    def _mirror_sources() -> tuple[str, str]:
        from pathlib import Path

        root = Path(__file__).resolve().parents[2] / "frontend" / "src"
        return (root / "types" / "index.ts").read_text(), (root / "services" / "auth.ts").read_text()

    def test_every_backend_permission_exists_in_the_frontend_enum(self):
        """The full-enum parity assertion (not just this story's permission).

        This is the test that catches X4's existing drift and prevents the next
        one: a backend permission with no mirror member is a control the UI cannot
        reference at all.
        """
        types_ts, _ = self._mirror_sources()

        missing = sorted(p.name for p in Permission if f"{p.name} = '{p.value}'" not in types_ts)

        assert not missing, (
            f"frontend/src/types/index.ts is missing these backend Permission members: {missing}. Both mirrors must be updated in the same PR."
        )

    def test_the_gate_approval_permission_is_in_both_mirrors(self):
        """The enum member AND the role map, which are different files."""
        types_ts, auth_ts = self._mirror_sources()

        assert f"PLAN_APPROVE = '{Permission.PLAN_APPROVE.value}'" in types_ts
        # Exactly the two roles the backend grants it to.
        assert auth_ts.count("Permission.PLAN_APPROVE") == 2


class TestRO3fDeclaredSeam:
    """R-O3f: the per-run seam is declared and explicitly not implemented.

    Asserted rather than left to a docstring because the failure mode is the
    dangerous direction: a control that *appears* to pause a run and does not
    means an operator stops watching a run that is still burning budget.
    """

    @pytest.mark.parametrize("action", ["pause", "steer", "abort"])
    def test_run_controls_return_not_implemented(self, app_with_router, action):
        response = client_for(app_with_router).post(f"/orchestration/runs/run-abc/{action}")

        assert response.status_code == 501
        assert "not implemented" in response.json()["detail"].lower()


class TestDecisionsReadApi:
    """The read path that makes attribution checkable — the story's smoke test."""

    @pytest.mark.asyncio
    async def test_decisions_expose_identity_role_and_actor_kind(self, session, app_with_router):
        """What the smoke test greps for: a `gate_approve`-class row with
        `actor_kind == "human"` and the approver's SSO identity present."""
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)
        client = client_for(app_with_router)
        client.post(approve_route(gate.id), json={"reason": "approved"})

        response = client.get(decisions_route(flow.id))

        assert response.status_code == 200, response.text
        human_approvals = [
            row for row in response.json() if row["kind"] == DecisionKind.GATE_APPROVED.value and row["actor_kind"] == ActorKind.HUMAN.value
        ]
        assert len(human_approvals) >= 1
        assert human_approvals[0]["actor_id"] == USER_ID
        assert human_approvals[0]["actor_role"] == ACTOR_ROLE
