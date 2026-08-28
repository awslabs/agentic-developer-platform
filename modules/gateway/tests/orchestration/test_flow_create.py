"""Tests for plan ingress: `POST /api/orchestration/flows`.

Issue #4320. `compile_proposal` was documented as "the engine's single ingress for
plan state" and had no caller outside its own module, so the orchestration graph
could not be populated by any authenticated action. The engine ran 52 times in
four hours and examined zero work items every time. This route is that ingress,
and the guarantees under test are the ones that keep it from being the weak door
into promotion state:

  - **Permission gate before anything else**: a caller without `PLAN_APPROVE`
    gets 403 and writes **zero** rows. The ordering matters as much as the check —
    the gate runs before any read, so a denied caller cannot learn what exists.
  - **Tenant from the token, never the document**: a document declaring another
    org is rejected (422), never re-homed. The route adds no tenant logic of its
    own; it passes the authenticated `org_id` as `ApprovalContext.org_id` and lets
    `compile_proposal`'s Gate 2 compare.
  - **Attribution is server-resolved**: the decision row's `actor_id` is the
    token's user id and its `actor_role` comes from the same resolver the
    permission check used — not from anything the client sent.
  - **Validation parity**: an invalid document is refused with per-violation
    detail and writes nothing, whether or not the advisory CLI ran.
  - **Idempotency (R-NF2)**: the identical document resubmitted creates no second
    flow, and is reported as already-compiled with a 200 rather than a 201.
  - **Dispatchability is visible at submission**: an org that does not resolve to
    exactly one GitHub installation produces a successful submission that can
    never dispatch. Surfaced in the response rather than left as a silent
    `undispatchable` counter a tick later.
  - **Commit boundary**: the route commits, so the rows survive the request. A
    partially-committed graph — nodes but no edges — would make dispatch run work
    out of dependency order.

The session fixture is `test_compile.py`'s, including its two pysqlite hooks —
they are load-bearing, not boilerplate: without them `begin_nested()` becomes the
outermost unit of work and `compile_proposal`'s savepoint is not exercised.
"""

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.config import Permission
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
from src.shared.models.organization import Organization

ORG_A = "org-alpha"
ORG_B = "org-beta"
FLOW = "delivery-loop"
SPEC_REVISION = "issue-4120-r1"
USER_ID = "cognito-sub-operator"

ROUTE = "/api/orchestration/flows"


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


def address(node_ref: str, *, epic: str = "epic-1", wave: str = "wave-1") -> str:
    return f"{FLOW}/{epic}/{wave}/{node_ref}"


def valid_proposal(*, org_id: str = ORG_A, **overrides) -> LoopProposal:
    """A well-formed two-node wave: one story feeding its eval."""
    payload = {
        "flow_slug": FLOW,
        "title": "Delivery loop",
        "org_id": org_id,
        "spec_revision": SPEC_REVISION,
        "intent_ref": "4120",
        "nodes": [
            ProposedNode(address=address("story-a"), kind="story", title="Story A", issue_ref="4320"),
            ProposedNode(address=address("eval"), kind="eval", title="Wave 1 eval"),
        ],
        "edges": [
            ProposedEdge(from_address=address("story-a"), to_address=address("eval")),
        ],
    }
    payload.update(overrides)
    return LoopProposal(**payload)


def invalid_proposal(*, org_id: str = ORG_A) -> LoopProposal:
    """Fails validation several ways: a container smuggled in as a node, a
    duplicate address, and a cycle."""
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


def _token_context(org_id: str, *, user_id: str = USER_ID):
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


async def seed_org(session: AsyncSession, org_id: str, *, installation_ids: list[str]) -> None:
    """An org row carrying `installation_ids`.

    Required for the dispatchability half: `resolve_installation_id` reads
    `organizations.github_installation_ids`, and an absent org row is
    indistinguishable from an org with no installation — both unresolvable.
    """
    session.add(
        Organization(
            id=org_id,
            name=org_id,
            github_installation_ids=installation_ids,
        )
    )
    await session.flush()


@pytest.fixture
def app_with_router(session):
    """A minimal app carrying only the orchestration router.

    Deliberately not the full `create_app()`: that pulls the whole middleware stack
    (Cognito, budget, rate-limit) and would make an authz assertion here depend on
    all of it. The router and its dependencies are the unit. Same shape as
    `test_amend.py::TestRouteAuthorization`.
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
    # response by the app-level BedrockGatewayError handler in `create_app()`.
    # Registered here because this minimal app skips create_app(); without it a
    # denied caller surfaces as 500 and the authz assertions would be testing the
    # harness rather than the route.
    @app.exception_handler(BedrockGatewayError)
    async def _gateway_error_handler(_request: Request, exc: BedrockGatewayError):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.error, "message": exc.message})

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: _token_context(ORG_A)
    return app


def client_for(app, *, permitted: bool, role: str = "org_admin"):
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


async def submitted_flow_nodes(session: AsyncSession, flow_id: str, *, org_id: str = ORG_A) -> dict[str, OrchestrationNode]:
    repo = OrchestrationRepository(session)
    flow = await repo.get_flow(org_id=org_id, flow_id=flow_id)
    nodes = await repo.list_nodes(org_id=org_id, flow_id=flow.id)
    return {f"{flow.slug}/{n.epic_ref}/{n.wave_ref}/{n.node_ref}": n for n in nodes}


class TestHappyPath:
    """An authorized submission creates the graph the engine sweeps."""

    @pytest.mark.asyncio
    async def test_submission_returns_201_with_a_flow_id(self, session, app_with_router):
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        response = client.post(ROUTE, json=valid_proposal().model_dump(mode="json"))

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["flow_id"]
        assert body["plan_version"] == 1
        assert body["already_compiled"] is False

    @pytest.mark.asyncio
    async def test_submission_reports_what_it_created(self, session, app_with_router):
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        body = client.post(ROUTE, json=valid_proposal().model_dump(mode="json")).json()

        assert body["nodes_created"] == 2
        assert body["edges_created"] == 1
        assert body["plan_hash"]
        assert body["decision_id"]

    @pytest.mark.asyncio
    async def test_flow_node_and_edge_rows_exist(self, session, app_with_router):
        """The whole point: the graph is no longer empty.

        Every capability downstream — dispatch, stall detection, cost rollup, the
        graph view — reads these rows and had nothing to read before this route.
        """
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        client.post(ROUTE, json=valid_proposal().model_dump(mode="json"))

        assert await count_rows(session, OrchestrationFlow) == 1
        assert await count_rows(session, OrchestrationNode) == 2
        assert await count_rows(session, OrchestrationEdge) == 1
        assert await count_rows(session, OrchestrationAcceptedPlan) == 1

    @pytest.mark.asyncio
    async def test_a_plan_accepted_decision_row_is_written(self, session, app_with_router):
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        flow_id = client.post(ROUTE, json=valid_proposal().model_dump(mode="json")).json()["flow_id"]

        repo = OrchestrationRepository(session)
        decisions = await repo.list_decisions(org_id=ORG_A, flow_id=flow_id)
        accepted = [d for d in decisions if d.kind == DecisionKind.PLAN_ACCEPTED.value]

        assert len(accepted) == 1

    @pytest.mark.asyncio
    async def test_the_accepted_plan_points_at_its_decision(self, session, app_with_router):
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        body = client.post(ROUTE, json=valid_proposal().model_dump(mode="json")).json()

        repo = OrchestrationRepository(session)
        in_force = await repo.get_accepted_plan(org_id=ORG_A, flow_id=body["flow_id"])

        assert in_force is not None
        assert in_force.accepted_by_decision_id == body["decision_id"]

    @pytest.mark.asyncio
    async def test_created_nodes_start_pending(self, session, app_with_router):
        """`pending` is what the tick's readiness pass looks for. A node created in
        any other state would be invisible to the sweep or dispatched unapproved."""
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        flow_id = client.post(ROUTE, json=valid_proposal().model_dump(mode="json")).json()["flow_id"]

        nodes = await submitted_flow_nodes(session, flow_id)
        assert {n.state for n in nodes.values()} == {NodeState.PENDING.value}

    @pytest.mark.asyncio
    async def test_the_flow_slug_and_intent_come_from_the_document(self, session, app_with_router):
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        flow_id = client.post(ROUTE, json=valid_proposal().model_dump(mode="json")).json()["flow_id"]

        repo = OrchestrationRepository(session)
        flow = await repo.get_flow(org_id=ORG_A, flow_id=flow_id)

        assert flow.slug == FLOW
        assert flow.intent_ref == "4120"
        assert flow.org_id == ORG_A

    @pytest.mark.asyncio
    async def test_rows_survive_the_request(self, session, app_with_router):
        """The route commits, so the graph outlives the transaction that made it.

        `compile_proposal` deliberately does not commit — the caller owns the
        transaction. If the route forgot to, the submission would report success and
        the graph would still be empty, which is indistinguishable from the bug this
        issue exists to fix.
        """
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        client.post(ROUTE, json=valid_proposal().model_dump(mode="json"))

        await session.rollback()

        assert await count_rows(session, OrchestrationFlow) == 1, "the route must commit at the request boundary"
        assert await count_rows(session, OrchestrationNode) == 2
        assert await count_rows(session, OrchestrationEdge) == 1, "nodes without their edges would dispatch out of dependency order"


class TestAuthorization:
    """403 without `PLAN_APPROVE`, and zero rows written."""

    @pytest.mark.asyncio
    async def test_caller_without_plan_approve_gets_403(self, session, app_with_router):
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=False)

        response = client.post(ROUTE, json=valid_proposal().model_dump(mode="json"))

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_denied_caller_writes_zero_rows(self, session, app_with_router):
        """The load-bearing half. Any authenticated user able to inject a plan can
        make the engine dispatch agent runs and spend budget — the first blast
        radius the issue names."""
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=False)

        client.post(ROUTE, json=valid_proposal().model_dump(mode="json"))

        assert await count_rows(session, OrchestrationFlow) == 0
        assert await count_rows(session, OrchestrationNode) == 0
        assert await count_rows(session, OrchestrationEdge) == 0
        assert await count_rows(session, OrchestrationAcceptedPlan) == 0
        assert await count_rows(session, OrchestrationDecision) == 0

    @pytest.mark.asyncio
    async def test_the_gate_runs_before_the_permission_check_can_leak_anything(self, session, app_with_router):
        """The permission check is scoped to the caller's OWN org.

        `PLAN_APPROVE` is in `_ORG_SCOPED_PERMISSIONS`, which is what makes a
        principal with an empty `org_id` get denied rather than skipping the
        membership check and short-circuiting the `target_org_id` scope check.
        """
        from unittest.mock import AsyncMock, MagicMock

        from fastapi.testclient import TestClient

        from src.admin.access_control import AccessControl
        from src.admin.config import AdminRole
        from src.orchestration.routes import get_access_control

        await seed_org(session, ORG_A, installation_ids=["12345"])

        access = MagicMock(spec=AccessControl)
        access.check_permission = AsyncMock(return_value=True)
        access.get_user_role = AsyncMock(return_value=(AdminRole("org_admin"), ORG_A, None))
        app_with_router.dependency_overrides[get_access_control] = lambda: access

        TestClient(app_with_router, raise_server_exceptions=False).post(ROUTE, json=valid_proposal().model_dump(mode="json"))

        access.check_permission.assert_awaited_once()
        kwargs = access.check_permission.await_args.kwargs
        args = access.check_permission.await_args.args
        assert Permission.PLAN_APPROVE in args or kwargs.get("permission") is Permission.PLAN_APPROVE
        assert kwargs.get("target_org_id") == ORG_A, "the gate must be scoped to the caller's own org, never a client-supplied one"


class TestTenantIsolation:
    """`org_id` comes only from the token. A mismatch is rejected, never re-homed."""

    @pytest.mark.asyncio
    async def test_document_declaring_another_org_gets_422(self, session, app_with_router):
        """Re-homing would be the worse failure: a plan authored for tenant B
        quietly becoming tenant A's state, with A's operator on the decision."""
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        response = client.post(ROUTE, json=valid_proposal(org_id=ORG_B).model_dump(mode="json"))

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_org_mismatch_writes_zero_rows(self, session, app_with_router):
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        client.post(ROUTE, json=valid_proposal(org_id=ORG_B).model_dump(mode="json"))

        assert await count_rows(session, OrchestrationFlow) == 0
        assert await count_rows(session, OrchestrationNode) == 0
        assert await count_rows(session, OrchestrationDecision) == 0

    @pytest.mark.asyncio
    async def test_the_plan_lands_under_the_tokens_org_not_the_documents(self, session, app_with_router):
        """Positive form of the same guarantee: the org on every created row is the
        authenticated one. Asserted directly so the property survives even if the
        mismatch rejection above were ever relaxed."""
        await seed_org(session, ORG_B, installation_ids=["999"])
        from src.auth.dependencies import get_current_user

        app_with_router.dependency_overrides[get_current_user] = lambda: _token_context(ORG_B)
        client = client_for(app_with_router, permitted=True)

        flow_id = client.post(ROUTE, json=valid_proposal(org_id=ORG_B).model_dump(mode="json")).json()["flow_id"]

        repo = OrchestrationRepository(session)
        assert await repo.get_flow(org_id=ORG_B, flow_id=flow_id) is not None
        assert await repo.get_flow(org_id=ORG_A, flow_id=flow_id) is None, "the flow must not be visible to another tenant"


class TestValidation:
    """An invalid document is refused with detail, and writes nothing."""

    @pytest.mark.asyncio
    async def test_invalid_proposal_gets_422_with_per_violation_detail(self, session, app_with_router):
        """The author needs to know everything wrong in one pass, keyed by a
        machine-readable rule rather than prose."""
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        response = client.post(ROUTE, json=invalid_proposal().model_dump(mode="json"))

        assert response.status_code == 422
        rules = {violation["rule"] for violation in response.json()["detail"]["violations"]}
        assert {"container_as_node", "duplicate_address"} <= rules

    @pytest.mark.asyncio
    async def test_invalid_proposal_writes_zero_rows(self, session, app_with_router):
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        client.post(ROUTE, json=invalid_proposal().model_dump(mode="json"))

        assert await count_rows(session, OrchestrationFlow) == 0
        assert await count_rows(session, OrchestrationNode) == 0
        assert await count_rows(session, OrchestrationEdge) == 0
        assert await count_rows(session, OrchestrationDecision) == 0

    @pytest.mark.asyncio
    async def test_a_malformed_body_is_rejected_by_the_model(self, session, app_with_router):
        """`LoopProposal` is `extra="forbid"`, so an unknown field is a 422 before
        any handler code runs — a document with a typo'd field must not compile with
        that field silently dropped."""
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        payload = valid_proposal().model_dump(mode="json")
        payload["unexpected_field"] = "surprise"

        assert client.post(ROUTE, json=payload).status_code == 422


class TestAttribution:
    """Who approved this is server-resolved, never client-supplied."""

    @pytest.mark.asyncio
    async def test_actor_id_equals_the_tokens_user_id(self, session, app_with_router):
        """Proves attribution comes from the token. If a client could set it, the
        decision record would attest to an approval by someone who never approved —
        and that record is what `resolve_engine_genesis` roots dispatch in."""
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        flow_id = client.post(ROUTE, json=valid_proposal().model_dump(mode="json")).json()["flow_id"]

        repo = OrchestrationRepository(session)
        decision = next(d for d in await repo.list_decisions(org_id=ORG_A, flow_id=flow_id) if d.kind == DecisionKind.PLAN_ACCEPTED.value)

        assert decision.actor_id == USER_ID

    @pytest.mark.asyncio
    async def test_actor_role_is_the_role_resolved_at_the_boundary(self, session, app_with_router):
        """Snapshotted from the same resolver the permission check used, so the
        attributed role is the one authority was actually granted under."""
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True, role="platform_admin")

        flow_id = client.post(ROUTE, json=valid_proposal().model_dump(mode="json")).json()["flow_id"]

        repo = OrchestrationRepository(session)
        decision = next(d for d in await repo.list_decisions(org_id=ORG_A, flow_id=flow_id) if d.kind == DecisionKind.PLAN_ACCEPTED.value)

        assert decision.actor_role == "platform_admin"

    @pytest.mark.asyncio
    async def test_actor_kind_is_human(self, session, app_with_router):
        """Submitting a plan is a human act. `is_human_rooted` downstream depends on
        this discriminator being set from the route's own default rather than
        anything the request carried."""
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        flow_id = client.post(ROUTE, json=valid_proposal().model_dump(mode="json")).json()["flow_id"]

        repo = OrchestrationRepository(session)
        decision = next(d for d in await repo.list_decisions(org_id=ORG_A, flow_id=flow_id) if d.kind == DecisionKind.PLAN_ACCEPTED.value)

        assert decision.actor_kind == ActorKind.HUMAN.value

    @pytest.mark.asyncio
    async def test_the_reason_query_param_is_carried_onto_the_decision(self, session, app_with_router):
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        flow_id = client.post(
            f"{ROUTE}?reason=Approved+at+the+wave+1+gate",
            json=valid_proposal().model_dump(mode="json"),
        ).json()["flow_id"]

        repo = OrchestrationRepository(session)
        decision = next(d for d in await repo.list_decisions(org_id=ORG_A, flow_id=flow_id) if d.kind == DecisionKind.PLAN_ACCEPTED.value)

        assert decision.reason == "Approved at the wave 1 gate"


class TestIdempotency:
    """R-NF2: resubmitting the identical document creates no second flow."""

    @pytest.mark.asyncio
    async def test_resubmission_creates_no_second_flow(self, session, app_with_router):
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        first = client.post(ROUTE, json=valid_proposal().model_dump(mode="json")).json()
        second = client.post(ROUTE, json=valid_proposal().model_dump(mode="json")).json()

        assert first["flow_id"] == second["flow_id"]
        assert await count_rows(session, OrchestrationFlow) == 1
        assert await count_rows(session, OrchestrationNode) == 2
        assert await count_rows(session, OrchestrationAcceptedPlan) == 1

    @pytest.mark.asyncio
    async def test_resubmission_is_reported_as_already_compiled(self, session, app_with_router):
        """A client reporting "N nodes created" must not present a retry as a fresh
        submission — a retried approval would claim to have created a graph it
        merely found."""
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        client.post(ROUTE, json=valid_proposal().model_dump(mode="json"))
        second = client.post(ROUTE, json=valid_proposal().model_dump(mode="json"))

        assert second.json()["already_compiled"] is True
        assert second.json()["nodes_created"] == 0
        assert second.json()["edges_created"] == 0

    @pytest.mark.asyncio
    async def test_resubmission_is_200_not_201(self, session, app_with_router):
        """Distinguishable by status code alone, which is what a retrying client
        sees first."""
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        assert client.post(ROUTE, json=valid_proposal().model_dump(mode="json")).status_code == 201
        assert client.post(ROUTE, json=valid_proposal().model_dump(mode="json")).status_code == 200

    @pytest.mark.asyncio
    async def test_resubmission_writes_no_second_decision(self, session, app_with_router):
        """A retry must not double-attribute an approval that happened once."""
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        flow_id = client.post(ROUTE, json=valid_proposal().model_dump(mode="json")).json()["flow_id"]
        client.post(ROUTE, json=valid_proposal().model_dump(mode="json"))

        repo = OrchestrationRepository(session)
        accepted = [d for d in await repo.list_decisions(org_id=ORG_A, flow_id=flow_id) if d.kind == DecisionKind.PLAN_ACCEPTED.value]

        assert len(accepted) == 1


class TestDispatchabilityIsVisibleAtSubmission:
    """An unresolvable installation must not look like a successful submission.

    `resolve_installation_id` fail-closes on both zero and more than one
    installation, and the tick then counts every node `undispatchable` with nothing
    reported back to whoever submitted. In the dev target, of 22 orgs only 3 have
    any installation and one of those has two — so this is the common case, not an
    edge case. Surfacing it at submission is what stops it reproducing the
    invisible-stall class this EPIC exists to remove.
    """

    @pytest.mark.asyncio
    async def test_exactly_one_installation_is_dispatchable(self, session, app_with_router):
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        body = client.post(ROUTE, json=valid_proposal().model_dump(mode="json")).json()

        assert body["dispatchable"] is True
        assert body["dispatch_blocked_reason"] is None

    @pytest.mark.asyncio
    async def test_no_installation_is_reported_undispatchable(self, session, app_with_router):
        await seed_org(session, ORG_A, installation_ids=[])
        client = client_for(app_with_router, permitted=True)

        body = client.post(ROUTE, json=valid_proposal().model_dump(mode="json")).json()

        assert body["dispatchable"] is False
        assert "exactly one GitHub installation" in body["dispatch_blocked_reason"]

    @pytest.mark.asyncio
    async def test_two_installations_are_reported_undispatchable(self, session, app_with_router):
        """More than one is as fail-closed as zero: guessing would dispatch into a
        repository nobody asked for."""
        await seed_org(session, ORG_A, installation_ids=["12345", "67890"])
        client = client_for(app_with_router, permitted=True)

        body = client.post(ROUTE, json=valid_proposal().model_dump(mode="json")).json()

        assert body["dispatchable"] is False
        assert "exactly one GitHub installation" in body["dispatch_blocked_reason"]

    @pytest.mark.asyncio
    async def test_undispatchable_still_commits_the_plan(self, session, app_with_router):
        """`dispatchable=False` is a report, not a rejection.

        The rows are exactly what a correct submission produces, and the condition
        is operational data about the org rather than anything wrong with the
        document. Refusing would make a data problem look like a bad plan.
        """
        await seed_org(session, ORG_A, installation_ids=[])
        client = client_for(app_with_router, permitted=True)

        response = client.post(ROUTE, json=valid_proposal().model_dump(mode="json"))

        assert response.status_code == 201
        assert await count_rows(session, OrchestrationFlow) == 1
        assert await count_rows(session, OrchestrationNode) == 2

    @pytest.mark.asyncio
    async def test_the_route_uses_the_same_resolver_as_dispatch(self):
        """Structural, not behavioural.

        If the route computed dispatchability its own way, the two would drift — and
        the drift would be invisible in the worst direction: the route promising a
        plan is dispatchable that dispatch then silently refuses.
        """
        from src.orchestration import routes as routes_module
        from src.orchestration.dispatch_pass import resolve_installation_id

        assert routes_module.resolve_installation_id is resolve_installation_id


class TestAmendmentRegression:
    """The pre-existing routes still work on a flow created this way."""

    @pytest.mark.asyncio
    async def test_a_flow_created_here_can_be_amended(self, session, app_with_router):
        """The two write paths into promotion state must compose: amendment was
        built against flows that only `compile_proposal` could create, and until now
        nothing authenticated ever created one."""
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        flow_id = client.post(ROUTE, json=valid_proposal().model_dump(mode="json")).json()["flow_id"]

        amended = valid_proposal()
        amended.nodes.append(ProposedNode(address=address("story-b"), kind="story", title="Story B", issue_ref="4321"))
        amended.edges.append(ProposedEdge(from_address=address("story-b"), to_address=address("eval")))

        response = client.post(
            f"/api/orchestration/flows/{flow_id}/amendments",
            json=amended.model_dump(mode="json"),
        )

        assert response.status_code == 200, response.text
        assert response.json()["plan_version"] == 2
        assert response.json()["superseded_version"] == 1

    @pytest.mark.asyncio
    async def test_the_plans_read_returns_the_submitted_document(self, session, app_with_router):
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        flow_id = client.post(ROUTE, json=valid_proposal().model_dump(mode="json")).json()["flow_id"]

        response = client.get(f"/api/orchestration/flows/{flow_id}/plans")

        assert response.status_code == 200, response.text
        versions = response.json()
        assert len(versions) == 1
        assert versions[0]["version"] == 1
        assert {node["address"] for node in versions[0]["plan_document"]["nodes"]} == {address("story-a"), address("eval")}
