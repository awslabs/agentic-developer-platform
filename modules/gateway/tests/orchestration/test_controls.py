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

And **R-O3f**: the per-run pause/steer/abort seam refuses any verb the deployment
has not implemented, with a 501 rather than a silent success. A control that
appears to pause a run without doing so is worse than no control, because an
operator who believes a run is paused stops watching it. As of #3965 all four
verbs are implemented, so the 501 case here is driven by a stubbed service — the
rung still has to exist for the verb added next.

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
# A syntactically valid command id, so the #3960 control-body schema validates and
# the request reaches the verb gate. A malformed one is a legitimate 400 and would
# mask whichever refusal the seam tests mean to assert.
COMMAND_ID = "3f8c1d64-1c1e-4a5f-9b2a-77c0d3a1b2e5"
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


@pytest.mark.parametrize("case,status", [("permission", 403), ("service", 403), ("spoof", 422)])
async def test_continuation_recovery_requires_human_approval(app_with_router, monkeypatch, case, status):
    from unittest.mock import AsyncMock

    from src.agentauth.bootstrap import BootstrapRefusedError

    mutate = AsyncMock()
    monkeypatch.setattr("src.orchestration.controls.resume_continuation", mutate)
    monkeypatch.setattr("src.agentauth.human_control.authorize_human_session", AsyncMock(side_effect=BootstrapRefusedError("human required")))
    body = {"expected_attempt": 1, "expected_plan_version": 3, "expected_run_id": "run", "reason": "Resume the existing review."}
    if case == "spoof":
        body["actor_kind"] = "human"
    with client_for(app_with_router, permitted=case != "permission") as client:
        response = client.post("/orchestration/nodes/node/resume-continuation", json=body)
    assert response.status_code == status
    mutate.assert_not_awaited()


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
                Permission.ACTIVITY_READ_ALL,
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

    Issue #3960 moved these routes onto the shared
    ``activity/control_service.py`` gate, so the *reason* a verb is refused now
    depends on how far the caller gets: an unknown or unauthorized run is refused
    at the authorization step (404, existence-hiding) before the verb is ever
    considered, and only a caller authorized for a real live run reaches the verb
    gate and its 501. Both are refusals, which is what R-O3f requires; the tests
    below assert each one at its own layer rather than expecting a blanket 501,
    because a route that answered 501 *before* authorizing would be confirming
    that another tenant's run exists.

    The full status-ordering matrix lives in
    ``tests/activity/test_control_proxy.py``, which drives this same service
    through both adapters. These cover the seam specifically.
    """

    @staticmethod
    def _with_control_service(app, service):
        from src.orchestration.controls import get_run_control_service

        app.dependency_overrides[get_run_control_service] = lambda: service
        return app

    @staticmethod
    def _body(action: str) -> dict:
        """A minimally valid command body. Steer needs an instruction."""
        body: dict = {"command_id": COMMAND_ID}
        if action == "steer":
            body["instruction"] = "prefer the smaller refactor"
        return body

    @pytest.mark.parametrize("action", ["pause", "resume", "steer", "abort"])
    def test_unknown_run_is_refused_without_confirming_it_exists(self, app_with_router, action):
        """No run row → 404, and deliberately not 501.

        This is the case the pre-#3960 test exercised (it posted a made-up id),
        and the answer changed on purpose. 501 here would be a verb-existence
        oracle: it would tell an unauthenticated-for-this-run caller that the
        gate got past lookup, which is exactly what the identical-404 rule for
        unknown / cross-tenant / non-owner exists to prevent.

        A *valid* body is sent so the request reaches the authorization gate.
        Since review finding F1 these routes validate bodies like the activity
        adapter does, so a bodyless POST is now a legitimate 400 — which would
        mask the 404 this test exists to assert.
        """
        # Model an absent row explicitly; this unit test must not read the
        # developer's real DynamoDB table or depend on an active AWS session.
        from unittest.mock import MagicMock

        from src.activity.control_service import ControlService
        from src.orchestration.controls import get_run_control_service

        table = MagicMock()
        table.query.return_value = {"Items": []}
        app_with_router.dependency_overrides[get_run_control_service] = lambda: ControlService(table=table)
        response = client_for(app_with_router).post(f"/orchestration/runs/run-abc/{action}", json=self._body(action))

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    @pytest.mark.parametrize("action", ["pause", "resume", "steer", "abort"])
    def test_a_malformed_body_is_400_before_the_run_is_looked_up(self, app_with_router, action):
        """F1: body validation reaches this adapter, and it reaches it first.

        Two properties in one assertion. That the 400 happens at all is the
        finding — these routes declared no body parameter, so the size cap, the
        `extra="forbid"` override rejection and the UUID check ran only on the
        activity adapter. That it happens *before* lookup is why 400 outranking
        404 is not an oracle: the answer is identical for a run that exists, one
        that belongs to another tenant, and one that does not exist, so it
        distinguishes nothing about the run. It is the same ordering W1-05 pins
        on the activity adapter (400 outranks even 501).
        """
        response = client_for(app_with_router).post(f"/orchestration/runs/run-abc/{action}", json={"command_id": "not-a-uuid"})

        assert response.status_code == 400

    @pytest.mark.parametrize("field", ["actor", "target", "token", "control_address"])
    def test_override_attempts_are_rejected_loudly_on_this_adapter_too(self, app_with_router, field):
        """The fields a caller sends to try to override attribution or destination.

        Dropping them silently returns success to the attempt, so the caller
        believes the override took effect (AC-S7). Asserted here specifically
        because this is the adapter where the check was missing.
        """
        response = client_for(app_with_router).post(
            "/orchestration/runs/run-abc/pause",
            json={"command_id": COMMAND_ID, field: "injected"},
        )

        assert response.status_code == 400

    @pytest.mark.parametrize("action", ["pause", "resume", "steer", "abort"])
    def test_an_authorized_live_run_reaches_the_verb_gate_and_is_refused(self, app_with_router, action):
        """The seam's real assertion: authorized, live, still not implemented.

        Uses a stub service that authorizes successfully so the request reaches
        the verb gate — the only way to prove the 501 is the *verb* being
        unsupported rather than a lookup failing earlier and returning a refusal
        that happens to look like one.
        """
        from unittest.mock import MagicMock

        from src.activity.control_service import ControlError, ControlService

        service = MagicMock(spec=ControlService)
        service.authorize_command.side_effect = ControlError(501, f"control action '{action}' is not implemented")

        app = self._with_control_service(app_with_router, service)
        body = {"command_id": COMMAND_ID}
        if action == "steer":
            body["instruction"] = "prefer the smaller refactor"
        response = client_for(app).post(f"/orchestration/runs/run-abc/{action}", json=body)

        assert response.status_code == 501
        assert "not implemented" in response.json()["detail"].lower()
        service.authorize_command.assert_called_once()

    def test_only_implemented_verbs_are_advertised_as_supported(self):
        """All four verbs now have transport; steer got its queue in #3965."""
        from src.activity.control_service import SUPPORTED_ACTIONS

        assert SUPPORTED_ACTIONS == frozenset({"pause", "resume", "steer", "abort"})

    @pytest.mark.parametrize("action", ["pause", "resume", "steer", "abort"])
    def test_all_four_verbs_are_routed(self, app_with_router, action):
        """A missing route would 405/404 at the router, not reach the gate.

        Parametrized over all four because ``resume`` was absent from the
        original seam test, and an unrouted verb is indistinguishable from a
        refused one if only the status code is checked.
        """
        routes = {(r.path, m) for r in app_with_router.routes for m in getattr(r, "methods", set())}

        assert (f"/orchestration/runs/{{run_id}}/{action}", "POST") in routes


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


class TestRevisionBoundAcceptance:
    """#5331: an answer may be bound to the plan revision the human reviewed.

    The hole this closes is narrow and easy to misjudge, so it is worth stating
    exactly. A client that reads the plan, checks the hash it expected and *then*
    approves has a window between those two HTTP calls. If the plan is amended in
    that window the approval is still accepted, and the decision row then claims a
    human approved a document they never saw. No client can close that window —
    only a comparison inside the same transaction as the state change can, which is
    why the precondition lives on the server and these tests live here.

    `expected_plan_hash` is OPTIONAL and its absence must remain byte-for-byte the
    old behaviour, because every existing caller (the dashboard, the GitHub comment
    path) omits it. That is asserted too: a regression there is a silent outage of
    gate approval, not a missing feature.
    """

    @staticmethod
    async def _plan(session, flow, *, document=None, plan_hash="a" * 64):
        return await OrchestrationRepository(session).record_accepted_plan(
            org_id=ORG_A,
            flow_id=flow.id,
            plan_document=document or {"flow_slug": FLOW_SLUG, "nodes": []},
            plan_hash=plan_hash,
        )

    @pytest.mark.asyncio
    async def test_the_revision_in_force_is_approved(self, session, app_with_router):
        """The happy path: the hash matches, so the gate moves exactly as it would
        have without a precondition."""
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)
        plan = await self._plan(session, flow)

        response = client_for(app_with_router).post(
            approve_route(gate.id),
            json={"reason": "reviewed version 1", "expected_plan_hash": plan.plan_hash},
        )

        assert response.status_code == 200, response.text
        assert await state_of(session, gate.id) == NodeState.PASSED.value

    @pytest.mark.asyncio
    async def test_a_superseded_revision_is_refused_and_the_gate_does_not_move(self, session, app_with_router):
        """The defect this exists for. An operator reviewed version 1, the plan was
        amended to version 2, and their approval must NOT be recorded against the
        document they did not read."""
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)
        reviewed = await self._plan(session, flow, plan_hash="a" * 64)
        # An amendment supersedes `reviewed` and becomes the plan in force.
        await self._plan(session, flow, plan_hash="b" * 64)

        response = client_for(app_with_router).post(
            approve_route(gate.id),
            json={"expected_plan_hash": reviewed.plan_hash},
        )

        assert response.status_code == 409, response.text
        # The claim that matters is not the status code — it is that nothing moved.
        assert await state_of(session, gate.id) == NodeState.AWAITING_GATE.value
        approvals = [row for row in await decisions_for(session, flow.id) if row.kind == DecisionKind.GATE_APPROVED.value]
        assert approvals == []

    @pytest.mark.asyncio
    async def test_a_stale_answer_is_recorded_as_a_refusal(self, session, app_with_router):
        """A refused approval is evidence. Someone tried to approve a plan that had
        already moved, and a reader of the decision log needs to see that rather
        than infer it from an absence."""
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)
        await self._plan(session, flow, plan_hash="a" * 64)

        client_for(app_with_router).post(approve_route(gate.id), json={"expected_plan_hash": "c" * 64})

        refusals = [row for row in await decisions_for(session, flow.id) if row.kind == DecisionKind.TRANSITION_REJECTED.value]
        assert len(refusals) == 1
        assert "not the revision in force" in refusals[0].rejection_reason
        assert refusals[0].actor_id == USER_ID

    @pytest.mark.asyncio
    async def test_the_refusal_does_not_echo_the_revision_in_force(self, session, app_with_router):
        """The message says the expectation was wrong, not what the right answer
        is. A caller re-reads the plan to learn that; a refusal is not a read API,
        and letting it become one invites a client that approves whatever it is
        told is current — which is the review step this story exists to enforce."""
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)
        in_force = "d" * 64
        await self._plan(session, flow, plan_hash=in_force)

        response = client_for(app_with_router).post(approve_route(gate.id), json={"expected_plan_hash": "c" * 64})

        assert response.status_code == 409
        assert in_force not in response.text

    @pytest.mark.asyncio
    async def test_a_flow_with_no_plan_in_force_refuses_a_bound_answer(self, session, app_with_router):
        """Fail closed. Nothing is in force, so no revision can match — the one
        thing this must not do is treat "nothing to compare" as "comparison
        passed"."""
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)

        response = client_for(app_with_router).post(approve_route(gate.id), json={"expected_plan_hash": "a" * 64})

        assert response.status_code == 409, response.text
        assert await state_of(session, gate.id) == NodeState.AWAITING_GATE.value

    @pytest.mark.asyncio
    async def test_an_empty_expected_hash_is_refused_at_the_edge(self, session, app_with_router):
        """`--expect-plan-hash "$(...)"` whose substitution produced nothing must
        not approve. 422 from the model, before the adapter is reached."""
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)
        await self._plan(session, flow)

        response = client_for(app_with_router).post(approve_route(gate.id), json={"expected_plan_hash": ""})

        assert response.status_code == 422, response.text
        assert await state_of(session, gate.id) == NodeState.AWAITING_GATE.value

    @pytest.mark.asyncio
    async def test_a_whitespace_expected_hash_fails_closed_in_the_adapter(self, session):
        """The adapter has a second caller (the GitHub comment path) that does not
        go through the request model, so it must fail closed on its own. A
        precondition enforced only by the door you came in is not a precondition.

        Driven at the adapter directly, because that is the seam the other caller
        uses."""
        from unittest.mock import AsyncMock, MagicMock

        from src.admin.access_control import AccessControl
        from src.orchestration.adapters.github_comments import (
            GateAnswerStatus,
            InputPath,
            apply_gate_answer_for_context,
        )

        flow = await seed_flow(session)
        gate = await seed_node(session, flow)
        await self._plan(session, flow)
        access = MagicMock(spec=AccessControl)
        access.check_permission = AsyncMock(return_value=True)
        access.get_user_role = AsyncMock(return_value=(AdminRole(ACTOR_ROLE), ORG_A, None))

        outcome = await apply_gate_answer_for_context(
            session,
            context=_token_context(ORG_A),
            node_id=gate.id,
            approve=True,
            reason=None,
            access=access,
            input_path=InputPath.DASHBOARD,
            expected_plan_hash="   ",
        )

        assert outcome.status is GateAnswerStatus.REFUSED_STALE_PLAN
        assert await state_of(session, gate.id) == NodeState.AWAITING_GATE.value

    @pytest.mark.asyncio
    async def test_omitting_the_precondition_preserves_the_prior_behaviour(self, session, app_with_router):
        """Every existing caller omits this field. A flow with a plan in force and
        an unbound answer must approve exactly as it did before — this field is
        additive or it is a gate-approval outage."""
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)
        await self._plan(session, flow)

        response = client_for(app_with_router).post(approve_route(gate.id), json={"reason": "unbound, as today"})

        assert response.status_code == 200, response.text
        assert await state_of(session, gate.id) == NodeState.PASSED.value

    @pytest.mark.asyncio
    async def test_a_stale_rejection_is_also_refused(self, session, app_with_router):
        """Rejecting a revision you did not read is the same misattribution as
        approving one, so the precondition is honoured on both verbs."""
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)
        await self._plan(session, flow, plan_hash="a" * 64)

        response = client_for(app_with_router).post(reject_route(gate.id), json={"expected_plan_hash": "c" * 64})

        assert response.status_code == 409, response.text
        assert await state_of(session, gate.id) == NodeState.AWAITING_GATE.value

    @pytest.mark.asyncio
    async def test_an_unauthorized_caller_cannot_probe_which_revision_is_in_force(self, session, app_with_router):
        """Ordering, asserted. The permission check runs BEFORE the plan read, so a
        caller without approval authority gets the same 403 whatever hash they
        send and cannot use this field to discover a tenant's plan state."""
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)
        in_force = "e" * 64
        await self._plan(session, flow, plan_hash=in_force)
        client = client_for(app_with_router, permitted=False)

        matching = client.post(approve_route(gate.id), json={"expected_plan_hash": in_force})
        stale = client.post(approve_route(gate.id), json={"expected_plan_hash": "c" * 64})

        assert matching.status_code == 403, matching.text
        assert stale.status_code == 403, stale.text
        assert in_force not in matching.text

    @pytest.mark.asyncio
    async def test_another_tenants_plan_hash_cannot_satisfy_the_precondition(self, session, app_with_router):
        """The plan is read under the answering caller's resolved org, so a hash
        that is in force in another tenant is simply not in force here."""
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)
        await self._plan(session, flow, plan_hash="a" * 64)
        other = await seed_flow(session, org_id=ORG_B, slug="other-loop")
        foreign = "f" * 64
        await OrchestrationRepository(session).record_accepted_plan(
            org_id=ORG_B,
            flow_id=other.id,
            plan_document={"flow_slug": "other-loop", "nodes": []},
            plan_hash=foreign,
        )

        response = client_for(app_with_router).post(approve_route(gate.id), json={"expected_plan_hash": foreign})

        assert response.status_code == 409, response.text
        assert await state_of(session, gate.id) == NodeState.AWAITING_GATE.value

    @pytest.mark.asyncio
    async def test_a_retry_after_a_lost_response_does_not_approve_twice(self, session, app_with_router):
        """The retry case the CLI actually hits. The first bound approval succeeds
        and its response is lost; the retry sends the same hash. It returns the
        original success and decision id without writing a second approval.
        """
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)
        plan = await self._plan(session, flow)
        client = client_for(app_with_router)

        first = client.post(approve_route(gate.id), json={"expected_plan_hash": plan.plan_hash})
        retry = client.post(approve_route(gate.id), json={"expected_plan_hash": plan.plan_hash})

        assert first.status_code == 200, first.text
        assert retry.status_code == 200, retry.text
        assert retry.json() == first.json()
        approvals = [row for row in await decisions_for(session, flow.id) if row.kind == DecisionKind.GATE_APPROVED.value]
        assert len(approvals) == 1

    @pytest.mark.asyncio
    async def test_a_different_actor_cannot_claim_the_original_bound_result(self, session, app_with_router):
        """Only the original actor gets a replay; another human gets a conflict."""
        from src.auth.dependencies import get_current_user

        flow = await seed_flow(session)
        gate = await seed_node(session, flow)
        plan = await self._plan(session, flow)

        first = client_for(app_with_router).post(approve_route(gate.id), json={"expected_plan_hash": plan.plan_hash})
        other = client_for(app_with_router, org_id=ORG_A)
        app_with_router.dependency_overrides[get_current_user] = lambda: _token_context(ORG_A, user_id="another-human")
        retry = other.post(approve_route(gate.id), json={"expected_plan_hash": plan.plan_hash})

        assert first.status_code == 200, first.text
        assert retry.status_code == 409, retry.text

    @pytest.mark.asyncio
    async def test_an_opposite_verb_cannot_claim_the_original_bound_result(self, session, app_with_router):
        """Approve and reject are different decisions even for the same actor and plan."""
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)
        plan = await self._plan(session, flow)
        client = client_for(app_with_router)

        first = client.post(approve_route(gate.id), json={"expected_plan_hash": plan.plan_hash})
        opposite = client.post(reject_route(gate.id), json={"expected_plan_hash": plan.plan_hash})

        assert first.status_code == 200, first.text
        assert opposite.status_code == 409, opposite.text

    @pytest.mark.asyncio
    async def test_a_decision_for_an_earlier_plan_is_not_replayed_for_a_later_plan(self, session, app_with_router):
        """The replay key includes the exact plan, not just actor, verb and gate.

        Amendments preserve the state of unchanged nodes. Therefore the same gate
        can still be passed after plan B replaces plan A, and a retry carrying B's
        hash must not be handed the decision that approved A.
        """
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)
        plan_a = await self._plan(session, flow, plan_hash="a" * 64)
        client = client_for(app_with_router)

        first = client.post(approve_route(gate.id), json={"expected_plan_hash": plan_a.plan_hash})
        plan_b = await self._plan(session, flow, plan_hash="b" * 64)
        wrong_revision = client.post(approve_route(gate.id), json={"expected_plan_hash": plan_b.plan_hash})

        assert first.status_code == 200, first.text
        assert wrong_revision.status_code == 409, wrong_revision.text
        approvals = [row for row in await decisions_for(session, flow.id) if row.kind == DecisionKind.GATE_APPROVED.value]
        assert len(approvals) == 1

    @pytest.mark.asyncio
    async def test_an_unbound_answer_is_not_reclassified_as_a_bound_retry(self, session, app_with_router):
        """A later bound request is a retry only when the original was bound too."""
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)
        plan = await self._plan(session, flow)
        client = client_for(app_with_router)

        first = client.post(approve_route(gate.id), json={"reason": "ordinary dashboard approval"})
        bound = client.post(approve_route(gate.id), json={"expected_plan_hash": plan.plan_hash})

        assert first.status_code == 200, first.text
        assert bound.status_code == 409, bound.text

    @pytest.mark.asyncio
    async def test_operator_text_cannot_forge_a_bound_retry_marker(self, session, app_with_router):
        """User-controlled reason text cannot turn an unbound answer into a replay."""
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)
        plan = await self._plan(session, flow)
        client = client_for(app_with_router)

        first = client.post(
            approve_route(gate.id),
            json={"reason": f"[plan-hash={plan.plan_hash}] pretend this was bound"},
        )
        bound = client.post(approve_route(gate.id), json={"expected_plan_hash": plan.plan_hash})

        assert first.status_code == 200, first.text
        assert bound.status_code == 409, bound.text

    @pytest.mark.asyncio
    async def test_knowing_the_right_revision_does_not_confer_authority_to_accept_it(self, session, app_with_router):
        """The self-approval boundary, restated for the bound path.

        An authoring agent knows its own plan's hash better than anyone — it just
        registered it. So the one thing the precondition must NOT become is a
        capability: presenting the correct revision has to remain insufficient
        without `PLAN_APPROVE`. A registry-resolved agent principal lands on
        `AdminRole.MEMBER`, which holds `PLAN_DRAFT` and not `PLAN_APPROVE`, and
        the acceptance route gates on the latter.

        Asserted as "the exactly-correct hash still gets 403, and the gate does not
        move", because a precondition evaluated before authority — or one that
        short-circuited a match into an approval — would pass every other test in
        this class while handing an agent the ability to accept its own draft.
        """
        flow = await seed_flow(session)
        gate = await seed_node(session, flow)
        plan = await self._plan(session, flow)

        response = client_for(app_with_router, permitted=False).post(
            approve_route(gate.id),
            json={"expected_plan_hash": plan.plan_hash},
        )

        assert response.status_code == 403, response.text
        assert await state_of(session, gate.id) == NodeState.AWAITING_GATE.value
        approvals = [row for row in await decisions_for(session, flow.id) if row.kind == DecisionKind.GATE_APPROVED.value]
        assert approvals == []


class TestLegacyLaneAdoptionThroughResume:
    """Resume is the production caller of `handoff.adopt_legacy_lane` (#5144).

    The reviewer's fourth blocker was that `adopt_legacy_lane` and
    `outstanding_block` were exported but unreachable: nothing in production called
    either, so the guards they carry protected nothing and the "legacy adoption"
    requirement was satisfied on paper only. These tests are written against the
    **route**, not the helper, because that is the whole claim — a test that called
    `adopt_legacy_lane` directly would pass just as well with the integration
    deleted, which is exactly the state the blocker described.

    Why resume is the site: a story whose worker exited without committing a
    continuation receipt is left in `awaiting_merge` by #5144's hold, and
    `_RESUMABLE_STATES` already routes that state here. It is also the only moment
    a *human* is present to attest that a prior owner's effects and credentials
    were reconciled — evidence no scheduled pass can produce, and which must never
    be defaulted true.

    Everything is asserted against durable claim state as well as the response,
    because "it answered 409" is compatible with a half-finished transfer.
    """

    @staticmethod
    def _resolver(app, rows: dict[str, dict] | None = None, *, fault: bool = False):
        """Install the liveness stub the route's dependency resolves to.

        Overriding the dependency rather than patching boto3 is what
        `get_run_binding_resolver` exists for. The stub is shared with
        `test_handoff_adoption.py` deliberately: the verdicts being driven here are
        the prior owner's, and two divergent fakes would let the two suites
        disagree about what `unverifiable` means.
        """
        from src.orchestration.controls import get_run_binding_resolver
        from tests.orchestration.test_handoff_adoption import FakeLivenessResolver

        resolver = FakeLivenessResolver(rows, fault=fault)
        app.dependency_overrides[get_run_binding_resolver] = lambda: resolver
        return resolver

    @staticmethod
    async def _held_story(session, *, run_id: str, issue: int = 5144):
        """A story held in `awaiting_merge` whose lane a legacy owner still holds.

        Built through the real claim primitives (`claim_work` + `bind_run`) rather
        than by inserting a row, so the lane the route finds is shaped exactly like
        one a pre-engine direct dispatch actually left behind. An accepted plan and
        a `PLAN_ACCEPTED` decision are seeded because adoption is policy-bound: the
        route resolves both server-side and refuses without them.
        """
        from decimal import Decimal

        from src.orchestration.execution_policy import Action, ExecutionPolicy, PolicyLimits, stamp_policy
        from src.orchestration.models import OrchestrationAcceptedPlan
        from src.orchestration.work_claims import ClaimBinding, ClaimOwner, OwnerKind, bind_run, claim_work

        flow = await seed_flow(session, slug=f"legacy-{issue}")
        policy = stamp_policy(
            ExecutionPolicy(
                org_id=ORG_A,
                repository_ids=["acme/work"],
                allowed_actions=[Action.DEVELOP],
                expires_at=datetime(2099, 1, 1, tzinfo=UTC),
                limits=PolicyLimits(max_wall_clock_seconds=86400, max_spend_usd=Decimal("100"), max_attempts_per_node=10, max_concurrent_actions=5),
            ),
            principal_id=USER_ID,
            org_id=ORG_A,
        )
        session.add(
            OrchestrationAcceptedPlan(
                org_id=ORG_A,
                flow_id=flow.id,
                version=3,
                plan_document={"execution_policy": policy.model_dump(mode="json")},
                plan_hash=f"plan-{issue}",
            )
        )
        node = await seed_node(
            session,
            flow,
            node_ref=f"story-{issue}",
            kind=NodeKind.STORY.value,
            state=NodeState.AWAITING_MERGE.value,
        )
        node.issue_ref = f"#{issue}"
        await OrchestrationRepository(session).append_decision(
            org_id=ORG_A,
            flow_id=flow.id,
            kind=DecisionKind.PLAN_ACCEPTED.value,
            actor_id=USER_ID,
            actor_role=ACTOR_ROLE,
            actor_kind=ActorKind.HUMAN.value,
        )
        receipt = await claim_work(
            session,
            binding=ClaimBinding(org_id=ORG_A, provider_repository_id=987_654_321, issue_number=issue),
            owner=ClaimOwner(OwnerKind.DIRECT_DISPATCH, "resident-coordinator"),
            event_id=f"legacy-event-{issue}",
        )
        await bind_run(session, org_id=ORG_A, claim_id=receipt.claim_id, generation=receipt.generation, run_id=run_id)
        await session.flush()
        return flow, node, receipt

    @staticmethod
    def _blocks(rows):
        """The #5144 typed blocks among a flow's decisions, parsed.

        Asserted through the structured `rejection_reason` rather than prose,
        because the block code is what routes an operator to a runbook — and
        because it must stay the same shape `dispatch_pass._record_admission_refusal`
        writes, so one query finds refusals from both sides.
        """
        import json

        parsed = []
        for row in rows:
            if row.kind != DecisionKind.TRANSITION_REJECTED.value or not row.rejection_reason:
                continue
            try:
                payload = json.loads(row.rejection_reason)
            except ValueError:
                continue
            if payload.get("issue") == "5144":
                parsed.append((row, payload))
        return parsed

    @pytest.fixture(autouse=True)
    def _adoption_on(self, monkeypatch):
        """Both flags on, so a refusal below is a real guard rather than a flag.

        Their defaults are asserted separately (`test_handoff.py` for adoption, and
        `test_adoption_stays_disabled_by_default` here for the route), because with
        the flags left off every test in this class would pass for the wrong
        reason — the route would return early and never reach the transfer at all.
        """
        from src.orchestration.handoff import ADOPTION_ENABLED_ENV

        monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
        monkeypatch.setenv(ADOPTION_ENABLED_ENV, "true")
        from unittest.mock import AsyncMock

        monkeypatch.setenv("BG_ORCH_DISPATCH_REPO", "acme/work")
        monkeypatch.setattr("src.orchestration.dispatch_pass.resolve_installation_id", AsyncMock(return_value=42))
        monkeypatch.setattr("src.orchestration.work_admission.resolve_repository_id", AsyncMock(return_value=987_654_321))

    @pytest.mark.asyncio
    async def test_same_tenant_issue_in_another_repository_is_not_adopted(self, session, app_with_router):
        from src.orchestration.models import ClaimState, OrchestrationWorkClaim
        from src.orchestration.work_claims import ClaimBinding, ClaimOwner, OwnerKind, bind_run, claim_work
        from tests.orchestration.test_handoff_adoption import _exited_row

        other = await claim_work(
            session,
            binding=ClaimBinding(org_id=ORG_A, provider_repository_id=111, issue_number=5144),
            owner=ClaimOwner(OwnerKind.DIRECT_DISPATCH, "other-repository"),
            event_id="other-event",
        )
        await bind_run(session, org_id=ORG_A, claim_id=other.claim_id, generation=other.generation, run_id="other-run")
        _, node, own = await self._held_story(session, run_id="own-run")
        self._resolver(app_with_router, {"own-run": _exited_row(), "other-run": _exited_row()})
        response = client_for(app_with_router).post(resume_route(node.id), json={"reconciled": True})
        assert response.status_code == 200, response.text
        row = await session.get(OrchestrationWorkClaim, other.claim_id)
        assert (row.state, row.generation, row.active_run_id) == (ClaimState.HELD.value, other.generation, "other-run")
        assert (await session.get(OrchestrationWorkClaim, own.claim_id)).generation == own.generation + 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", ["missing_repo", "missing_installation", "credentials_unavailable", "provider_unavailable"])
    async def test_missing_repository_identity_blocks_adoption(self, session, app_with_router, monkeypatch, failure):
        from src.orchestration.models import ClaimState, OrchestrationWorkClaim

        _, node, own = await self._held_story(session, run_id="own-run")
        from unittest.mock import AsyncMock

        from httpx import ConnectError

        if failure == "missing_repo":
            monkeypatch.delenv("BG_ORCH_DISPATCH_REPO")
        elif failure == "missing_installation":
            monkeypatch.setattr("src.orchestration.dispatch_pass.resolve_installation_id", AsyncMock(return_value=None))
        else:
            error = ValueError("credentials unavailable") if failure == "credentials_unavailable" else ConnectError("provider unavailable")
            monkeypatch.setattr("src.orchestration.work_admission.resolve_repository_id", AsyncMock(side_effect=error))
        response = client_for(app_with_router).post(resume_route(node.id), json={"reconciled": True})
        assert response.status_code == 409, response.text
        assert (await session.get(OrchestrationWorkClaim, own.claim_id)).state == ClaimState.HELD.value
        assert self._blocks(await decisions_for(session, node.flow_id))[-1][1]["block_code"] == "authority_unverifiable"

    @pytest.mark.asyncio
    async def test_an_exited_reconciled_legacy_lane_is_adopted_once(self, session, app_with_router):
        """The issue's validation bullet, first half: adopt an exited lane, once.

        `force_handover` leaves the claim RELEASED at the next generation with no
        active run, so the adopter reclaims through ordinary admission instead of
        being spliced into a held row. Asserting the generation advanced *and* the
        run was cleared is what distinguishes a real transfer from a route that
        merely returned 200.
        """
        from src.orchestration.models import ClaimState, OrchestrationWorkClaim
        from src.orchestration.work_claims import ReleaseReason
        from tests.orchestration.test_handoff_adoption import _exited_row

        run = "legacy-run-1"
        _, node, receipt = await self._held_story(session, run_id=run)
        self._resolver(app_with_router, {run: _exited_row()})

        response = client_for(app_with_router).post(resume_route(node.id), json={"reconciled": True})

        assert response.status_code == 200, response.text
        assert await state_of(session, node.id) == NodeState.READY.value
        claim = await session.get(OrchestrationWorkClaim, receipt.claim_id)
        assert claim.state == ClaimState.RELEASED.value
        assert claim.generation == receipt.generation + 1
        assert claim.active_run_id is None
        assert claim.release_reason == ReleaseReason.HANDOVER.value

    @pytest.mark.asyncio
    async def test_a_second_resume_does_not_transfer_the_lane_again(self, session, app_with_router):
        """ "Once" is the load-bearing word. A repeat must not advance again.

        Without this, a double-click would walk the generation forward on every
        press, and each advance permanently invalidates tokens issued under the
        previous one — so an idempotence bug here is a way to break a *working*
        owner, not just to waste a write.
        """
        from src.orchestration.models import OrchestrationWorkClaim
        from tests.orchestration.test_handoff_adoption import _exited_row

        run = "legacy-run-1"
        _, node, receipt = await self._held_story(session, run_id=run)
        self._resolver(app_with_router, {run: _exited_row()})
        client = client_for(app_with_router)
        assert client.post(resume_route(node.id), json={"reconciled": True}).status_code == 200
        generation_after_adoption = (await session.get(OrchestrationWorkClaim, receipt.claim_id)).generation

        # The node is `ready` now, so this resume is refused by the narrowing guard
        # — and the point is that the lane is untouched on the way to that refusal.
        second = client.post(resume_route(node.id), json={"reconciled": True})

        assert second.status_code == 409
        claim = await session.get(OrchestrationWorkClaim, receipt.claim_id)
        assert claim.generation == generation_after_adoption

    @pytest.mark.asyncio
    async def test_a_live_prior_owner_blocks_the_transfer_and_the_resume(self, session, app_with_router):
        """The issue's validation bullet, second half — and the reason F4 matters.

        A live owner must block, and the story must *not* be reported resumed. If
        the resume proceeded anyway, the engine would begin work on a story a
        running legacy worker still owns: two writers on one branch, which is the
        double-effect #5144 exists to prevent, reintroduced by its own recovery
        path.
        """
        from src.orchestration.execution_state import BlockCode
        from src.orchestration.models import ClaimState, OrchestrationWorkClaim
        from tests.orchestration.test_handoff_adoption import _live_row

        run = "legacy-run-1"
        flow, node, receipt = await self._held_story(session, run_id=run)
        self._resolver(app_with_router, {run: _live_row()})

        response = client_for(app_with_router).post(resume_route(node.id), json={"reconciled": True})

        assert response.status_code == 409
        # Nothing promoted: the hold stands.
        assert await state_of(session, node.id) == NodeState.AWAITING_MERGE.value
        claim = await session.get(OrchestrationWorkClaim, receipt.claim_id)
        assert (claim.state, claim.generation, claim.active_run_id) == (ClaimState.HELD.value, receipt.generation, run)
        blocks = self._blocks(await decisions_for(session, flow.id))
        assert len(blocks) == 1
        assert blocks[0][1]["block_code"] == BlockCode.OWNERSHIP_LOST.value
        assert blocks[0][1]["owner"]
        assert blocks[0][1]["required_input"]
        # The node went nowhere, so a recorded destination would read as a resume
        # that happened and was undone.
        assert blocks[0][0].to_state is None
        assert blocks[0][0].actor_kind == ActorKind.HUMAN.value

    @pytest.mark.asyncio
    async def test_an_unverifiable_prior_owner_blocks_the_transfer(self, session, app_with_router):
        """Loss of contact is not evidence of an exit.

        The case a weaker implementation gets wrong, because a partitioned-but-
        working worker and an exited one look identical from here. It must refuse
        exactly as `live` does, and this is the test that fails if the route ever
        treats a missing or stale signal as permission.
        """
        from src.orchestration.models import ClaimState, OrchestrationWorkClaim
        from tests.orchestration.test_handoff_adoption import _unverifiable_row

        run = "legacy-run-1"
        flow, node, receipt = await self._held_story(session, run_id=run)
        self._resolver(app_with_router, {run: _unverifiable_row()})

        response = client_for(app_with_router).post(resume_route(node.id), json={"reconciled": True})

        assert response.status_code == 409
        assert await state_of(session, node.id) == NodeState.AWAITING_MERGE.value
        assert (await session.get(OrchestrationWorkClaim, receipt.claim_id)).state == ClaimState.HELD.value
        assert len(self._blocks(await decisions_for(session, flow.id))) == 1

    @pytest.mark.asyncio
    async def test_an_unattested_resume_refuses_rather_than_assuming_reconciliation(self, session, app_with_router):
        """`reconciled` defaults to False, and the default must refuse.

        A database fence cannot revoke a GitHub installation token already issued,
        so the attestation is the only evidence that exists. If it defaulted true —
        or if the route passed `True` regardless — every resume would silently carry
        the authority that makes a transfer legal, and `force_handover`'s guards 3
        and 4 would be unreachable. The lane is exited here, so the *only* thing
        refusing is the missing attestation.
        """
        from src.orchestration.models import ClaimState, OrchestrationWorkClaim
        from tests.orchestration.test_handoff_adoption import _exited_row

        run = "legacy-run-1"
        _, node, receipt = await self._held_story(session, run_id=run)
        self._resolver(app_with_router, {run: _exited_row()})

        response = client_for(app_with_router).post(resume_route(node.id), json={})

        assert response.status_code == 409
        assert await state_of(session, node.id) == NodeState.AWAITING_MERGE.value
        assert (await session.get(OrchestrationWorkClaim, receipt.claim_id)).state == ClaimState.HELD.value

    @pytest.mark.asyncio
    async def test_adoption_stays_disabled_by_default(self, session, app_with_router, monkeypatch):
        """Staged deployment: the flag is off until both revisions are verified.

        With adoption disabled the route must neither transfer the lane nor block
        on it — the legacy lane is simply not this engine's business yet, and a
        resume of an ordinary held story has to keep working. A flag that blocked
        instead of standing aside would make every legacy story unresumable the
        moment this code shipped.
        """
        from src.orchestration.handoff import ADOPTION_ENABLED_ENV
        from src.orchestration.models import ClaimState, OrchestrationWorkClaim
        from tests.orchestration.test_handoff_adoption import _exited_row

        monkeypatch.setenv(ADOPTION_ENABLED_ENV, "false")
        run = "legacy-run-1"
        _, node, receipt = await self._held_story(session, run_id=run)
        self._resolver(app_with_router, {run: _exited_row()})

        response = client_for(app_with_router).post(resume_route(node.id), json={"reconciled": True})

        assert response.status_code == 200, response.text
        assert await state_of(session, node.id) == NodeState.READY.value
        claim = await session.get(OrchestrationWorkClaim, receipt.claim_id)
        assert (claim.state, claim.generation) == (ClaimState.HELD.value, receipt.generation)

    @pytest.mark.asyncio
    async def test_an_engine_owned_lane_is_never_taken_by_this_control(self, session, app_with_router):
        """Adoption is for *legacy* lanes. An engine lane must be left alone.

        The `DIRECT_DISPATCH` filter is what keeps this control from becoming a way
        to seize a live engine lane — an `ENGINE_FLOW` claim is already the
        engine's, and taking it away would release a claim a running attempt's
        receipt is attributed to, which is the very stranding F1 refuses.
        """
        from src.orchestration.models import ClaimState, OrchestrationWorkClaim
        from src.orchestration.work_claims import ClaimBinding, ClaimOwner, OwnerKind, bind_run, claim_work
        from tests.orchestration.test_handoff_adoption import _exited_row

        run = "engine-run-1"
        flow, node, _ = await self._held_story(session, run_id="legacy-run-1", issue=5150)
        engine_claim = await claim_work(
            session,
            binding=ClaimBinding(org_id=ORG_A, provider_repository_id=987_654_321, issue_number=5151),
            owner=ClaimOwner(OwnerKind.ENGINE_FLOW, flow.id),
            event_id="engine-event",
        )
        await bind_run(session, org_id=ORG_A, claim_id=engine_claim.claim_id, generation=engine_claim.generation, run_id=run)
        node.issue_ref = "#5151"
        await session.flush()
        self._resolver(app_with_router, {run: _exited_row()})

        response = client_for(app_with_router).post(resume_route(node.id), json={"reconciled": True})

        assert response.status_code == 200, response.text
        claim = await session.get(OrchestrationWorkClaim, engine_claim.claim_id)
        assert (claim.state, claim.generation, claim.active_run_id) == (ClaimState.HELD.value, engine_claim.generation, run)

    @pytest.mark.asyncio
    async def test_another_tenants_legacy_lane_is_not_adopted(self, session, app_with_router):
        """Tenant isolation on the lane lookup, not just on the node.

        The claim is found by `(org_id, issue)` from the story, never from the
        request — but a missing `org_id` filter would let a resume in one tenant
        transfer an identically-numbered issue's lane in another. The two orgs here
        share an issue number precisely so that omission fails.
        """
        from src.orchestration.models import ClaimState, OrchestrationWorkClaim
        from src.orchestration.work_claims import ClaimBinding, ClaimOwner, OwnerKind, bind_run, claim_work
        from tests.orchestration.test_handoff_adoption import _exited_row

        foreign_run = "foreign-run-1"
        foreign = await claim_work(
            session,
            binding=ClaimBinding(org_id=ORG_B, provider_repository_id=987_654_321, issue_number=5144),
            owner=ClaimOwner(OwnerKind.DIRECT_DISPATCH, "resident-coordinator"),
            event_id="foreign-event",
        )
        await bind_run(session, org_id=ORG_B, claim_id=foreign.claim_id, generation=foreign.generation, run_id=foreign_run)
        _, node, own = await self._held_story(session, run_id="legacy-run-1")
        self._resolver(app_with_router, {"legacy-run-1": _exited_row(), foreign_run: _exited_row()})

        response = client_for(app_with_router).post(resume_route(node.id), json={"reconciled": True})

        assert response.status_code == 200, response.text
        # This tenant's lane transferred; the other tenant's is untouched.
        assert (await session.get(OrchestrationWorkClaim, own.claim_id)).generation == own.generation + 1
        foreign_row = await session.get(OrchestrationWorkClaim, foreign.claim_id)
        assert (foreign_row.state, foreign_row.generation) == (ClaimState.HELD.value, foreign.generation)

    @pytest.mark.asyncio
    async def test_a_story_with_no_legacy_lane_resumes_normally(self, session, app_with_router):
        """The ordinary case must stay ordinary, or this becomes a global stall.

        Most stories have no legacy claim at all. Without this test the lane lookup
        could be over-broad — or could refuse on absence — and every routine resume
        in the platform would start answering 409 with nothing in the suite to say
        so.
        """
        flow = await seed_flow(session)
        node = await seed_node(session, flow, kind=NodeKind.STORY.value, state=NodeState.FAILED.value)
        self._resolver(app_with_router, {})

        response = client_for(app_with_router).post(resume_route(node.id), json={"reconciled": True})

        assert response.status_code == 200, response.text
        assert await state_of(session, node.id) == NodeState.READY.value
        assert self._blocks(await decisions_for(session, flow.id)) == []

    @pytest.mark.asyncio
    async def test_a_faulting_liveness_lookup_blocks_rather_than_assuming_an_exit(self, session, app_with_router):
        """An unreadable liveness record is exactly when assuming an exit is unsafe.

        Refuse, do not degrade. The typed block routes to a provider-availability
        runbook rather than an ownership one, because the condition an operator has
        to clear is the lookup, not the claim.
        """
        from src.orchestration.execution_state import BlockCode
        from src.orchestration.models import ClaimState, OrchestrationWorkClaim

        flow, node, receipt = await self._held_story(session, run_id="legacy-run-1")
        self._resolver(app_with_router, fault=True)

        response = client_for(app_with_router).post(resume_route(node.id), json={"reconciled": True})

        assert response.status_code == 409
        assert (await session.get(OrchestrationWorkClaim, receipt.claim_id)).state == ClaimState.HELD.value
        blocks = self._blocks(await decisions_for(session, flow.id))
        assert blocks[0][1]["block_code"] == BlockCode.PROVIDER_UNAVAILABLE.value

    @pytest.mark.asyncio
    async def test_adoption_requires_an_accepted_policy(self, session, app_with_router):
        """Policy-bound only: a lane with no accepted plan cannot be adopted.

        Adopting under no policy would place work under engine ownership that no
        human ever admitted, and the plan version is resolved *server-side* from
        the flow so a caller cannot supply one. A flow with no accepted plan
        resolves to version 0, which `adopt_legacy_lane` refuses.
        """
        from src.orchestration.models import ClaimState, OrchestrationAcceptedPlan, OrchestrationWorkClaim
        from tests.orchestration.test_handoff_adoption import _exited_row

        run = "legacy-run-1"
        flow, node, receipt = await self._held_story(session, run_id=run)
        await session.execute(
            update(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == flow.id).values(superseded_at=datetime.now(UTC))
        )
        await session.flush()
        self._resolver(app_with_router, {run: _exited_row()})

        response = client_for(app_with_router).post(resume_route(node.id), json={"reconciled": True})

        assert response.status_code == 409
        assert await state_of(session, node.id) == NodeState.AWAITING_MERGE.value
        assert (await session.get(OrchestrationWorkClaim, receipt.claim_id)).state == ClaimState.HELD.value

    @pytest.mark.asyncio
    async def test_an_unreadable_policy_refuses_instead_of_falling_back(self, session, app_with_router):
        """F3's rule, applied here: refusal is not absence.

        A stored policy that cannot be validated must block adoption, not fall
        through to a legacy-style transfer. That fallback is how policy-bound work
        loses its restrictions, and it is the same defect the dispatch path was
        repaired for.
        """
        from src.orchestration.execution_state import BlockCode
        from src.orchestration.models import ClaimState, OrchestrationAcceptedPlan, OrchestrationWorkClaim
        from tests.orchestration.test_handoff_adoption import _exited_row

        run = "legacy-run-1"
        flow, node, receipt = await self._held_story(session, run_id=run)
        await session.execute(
            update(OrchestrationAcceptedPlan)
            .where(OrchestrationAcceptedPlan.flow_id == flow.id)
            .values(plan_document={"execution_policy": {"not": "a policy"}})
        )
        await session.flush()
        self._resolver(app_with_router, {run: _exited_row()})

        response = client_for(app_with_router).post(resume_route(node.id), json={"reconciled": True})

        assert response.status_code == 409
        assert await state_of(session, node.id) == NodeState.AWAITING_MERGE.value
        assert (await session.get(OrchestrationWorkClaim, receipt.claim_id)).state == ClaimState.HELD.value
        blocks = self._blocks(await decisions_for(session, flow.id))
        assert blocks[0][1]["block_code"] == BlockCode.AUTHORITY_UNVERIFIABLE.value

    @pytest.mark.asyncio
    async def test_adoption_needs_the_permission_like_every_other_write_here(self, session, app_with_router):
        """No softer door. Adoption runs behind the same `PLAN_APPROVE` gate.

        The check happens before the node is even resolved, so an unauthorized
        caller cannot reach the lane lookup — let alone transfer a lane.
        """
        from src.orchestration.models import ClaimState, OrchestrationWorkClaim
        from tests.orchestration.test_handoff_adoption import _exited_row

        run = "legacy-run-1"
        _, node, receipt = await self._held_story(session, run_id=run)
        self._resolver(app_with_router, {run: _exited_row()})

        response = client_for(app_with_router, permitted=False).post(resume_route(node.id), json={"reconciled": True})

        assert response.status_code == 403
        assert await state_of(session, node.id) == NodeState.AWAITING_MERGE.value
        assert (await session.get(OrchestrationWorkClaim, receipt.claim_id)).state == ClaimState.HELD.value

    @pytest.mark.asyncio
    async def test_accepted_plan_without_execution_policy_cannot_adopt(self, session, app_with_router):
        from src.orchestration.models import ClaimState, OrchestrationAcceptedPlan, OrchestrationWorkClaim
        from tests.orchestration.test_handoff_adoption import _exited_row

        flow, node, receipt = await self._held_story(session, run_id="legacy-run")
        await session.execute(update(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == flow.id).values(plan_document={}))
        await session.flush()
        self._resolver(app_with_router, {"legacy-run": _exited_row()})
        response = client_for(app_with_router).post(resume_route(node.id), json={"reconciled": True})
        assert response.status_code == 409, response.text
        claim = await session.get(OrchestrationWorkClaim, receipt.claim_id)
        assert (claim.state, claim.generation, claim.active_run_id) == (ClaimState.HELD.value, receipt.generation, "legacy-run")
        assert self._blocks(await decisions_for(session, flow.id))[-1][1]["block_code"] == "authority_unverifiable"


@pytest.mark.parametrize("change", ["unchanged", "attempt", "flow", "foreign"])
async def test_cli_resume_fences_exact_reviewed_node(app_with_router, session, change):
    flow = await seed_flow(session)
    node = await seed_node(session, flow, kind="eval", state="failed")
    await session.commit()
    with client_for(app_with_router) as client:
        before = client.get(f"/orchestration/nodes/{node.id}/recovery", params={"flow_id": flow.id})
        assert before.status_code == 200
        body = {"reason": "Retry after repair", "expected_revision": before.json()["revision"], "expected_flow_id": flow.id}
        if change == "attempt":
            node.attempts += 1
            await session.commit()
        if change == "flow":
            body["expected_flow_id"] = "other-flow"
        if change == "foreign":
            node.org_id = ORG_B
            await session.commit()
        response = client.post(resume_route(node.id), json=body)
    assert response.status_code == {"unchanged": 200, "attempt": 409, "flow": 404, "foreign": 404}[change]
    if change == "unchanged":
        assert response.json()["state"] == "ready"
        with client_for(app_with_router) as client:
            assert client.post(resume_route(node.id), json=body).status_code == 409
    else:
        assert await state_of(session, node.id) == "failed"


async def test_cli_recovery_read_rejects_missing_permission_and_foreign_flow(app_with_router, session):
    flow = await seed_flow(session)
    node = await seed_node(session, flow)
    await session.commit()
    with client_for(app_with_router, permitted=False) as client:
        assert client.get(f"/orchestration/nodes/{node.id}/recovery", params={"flow_id": flow.id}).status_code == 403
    with client_for(app_with_router) as client:
        assert client.get(f"/orchestration/nodes/{node.id}/recovery", params={"flow_id": "foreign"}).status_code == 404
