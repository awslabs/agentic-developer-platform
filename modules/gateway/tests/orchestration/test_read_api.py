"""Tests for the flow read endpoint: `GET /orchestration/flows/{flow_id}`.

Issue #4212. This is the endpoint the graph view renders, and the guarantees under
test are the ones that decide whether the view can answer its own question:

  - **Pending nodes are in the payload.** A response holding only what already ran
    cannot answer "how much is left" (AC-1), which is the question the view exists
    for. This is asserted on a flow where *nothing* has run as well as on a
    part-run one — the second is the case a naive "return the active nodes" query
    passes while still being wrong.
  - **Edges are in the payload.** Without them parallel branches cannot be told
    from a flat sequence (AC-2), and "what unblocks when this passes" is
    unanswerable.
  - **Stalled is distinguishable from failed and halted** (AC-3). Not inferable
    from `state`: stall detection moves a node to `failed` (`stall.py`), so the
    endpoint has to derive it from the decision log or the distinction is lost.
  - **Cost stays three-valued per node** (AC-22). A node with no ledger row is
    `unknown` carrying a reason — never `0`, and never an amount.
  - **Tenant isolation**: another org's `flow_id` is a **404**, not a 403 and not
    an empty graph. The empty-graph case is called out separately because it is
    the plausible bug: `list_nodes` is org-scoped, so an implementation that skips
    the flow resolution returns `200` with no nodes, which reads as "no work".
  - **The permission gate runs before any read**, so a denied caller cannot learn
    whether the flow exists.

Session and app fixtures mirror `test_flow_create.py`, including its two pysqlite
hooks. `client_for` gates on `USAGE_READ` rather than `PLAN_APPROVE`: this is a
read, and requiring approval authority to see where delivery stands would be
authority creep in the direction that grants more than the operation needs.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.config import Permission
from src.orchestration.cost import COST_SCOPE_LABEL
from src.orchestration.models import DecisionKind, NodeKind, OrchestrationFlow, OrchestrationNode
from src.orchestration.repository import OrchestrationRepository
from src.orchestration.state import ActorKind, NodeState
from src.shared.models.base import Base
from src.shared.models.usage import UsageLog
from src.shared.schemas.auth import TokenContext

ORG_A = "org-alpha"
ORG_B = "org-beta"
FLOW_SLUG = "delivery-loop"
USER_ID = "cognito-sub-operator"


def route(flow_id: str) -> str:
    return f"/orchestration/flows/{flow_id}"


@pytest.fixture
async def session():
    """In-memory SQLite session with working SAVEPOINTs. See test_flow_create.py."""
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

    `org_id` is an authenticated Cognito claim and is never writable by a request
    header, which is what lets the route use it as the tenant directly.
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
    """A minimal app carrying only the orchestration router.

    Deliberately not `create_app()`: that pulls the whole middleware stack and
    would make an authz assertion here depend on all of it.
    """
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from src.auth.dependencies import get_current_user
    from src.orchestration.routes import router as orchestration_router
    from src.shared.database import get_db
    from src.shared.exceptions import BedrockGatewayError

    app = FastAPI()
    app.include_router(orchestration_router)

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

    Denial is expressed as `USAGE_READ` because that is the permission this route
    gates on — asserting a `PLAN_APPROVE` denial here would test a permission the
    route does not check.
    """
    from unittest.mock import AsyncMock, MagicMock

    from fastapi.testclient import TestClient

    from src.admin.access_control import AccessControl
    from src.admin.config import AdminRole
    from src.admin.exceptions import AccessDeniedError
    from src.auth.dependencies import get_current_user
    from src.orchestration.routes import get_access_control

    access = MagicMock(spec=AccessControl)
    if permitted:
        access.check_permission = AsyncMock(return_value=True)
    else:
        access.check_permission = AsyncMock(
            side_effect=AccessDeniedError(
                message="Permission 'usage:read' is required for this operation",
                required_permission=Permission.USAGE_READ.value,
                user_role="member",
            )
        )
    access.get_user_role = AsyncMock(return_value=(AdminRole("org_admin"), org_id, None))

    app.dependency_overrides[get_access_control] = lambda: access
    app.dependency_overrides[get_current_user] = lambda: _token_context(org_id)
    return TestClient(app, raise_server_exceptions=False)


async def seed_flow(
    session: AsyncSession,
    *,
    org_id: str = ORG_A,
    slug: str = FLOW_SLUG,
    intent_ref: str | None = "4120",
) -> OrchestrationFlow:
    repo = OrchestrationRepository(session)
    return await repo.create_flow(org_id=org_id, slug=slug, title="Delivery loop", intent_ref=intent_ref)


async def seed_node(
    session: AsyncSession,
    flow: OrchestrationFlow,
    *,
    node_ref: str,
    kind: str = NodeKind.STORY.value,
    state: str = NodeState.PENDING.value,
    epic: str = "epic-1",
    wave: str = "wave-1",
    issue_ref: str | None = None,
    attempts: int = 0,
    org_id: str = ORG_A,
) -> OrchestrationNode:
    """One node. `state` defaults to `pending` — the look-ahead case."""
    repo = OrchestrationRepository(session)
    node = await repo.add_node(
        org_id=org_id,
        flow_id=flow.id,
        epic_ref=epic,
        wave_ref=wave,
        node_ref=node_ref,
        kind=kind,
        title=f"Node {node_ref}",
        issue_ref=issue_ref,
    )
    node.state = state
    node.attempts = attempts
    await session.flush()
    return node


async def seed_usage(
    session: AsyncSession,
    *,
    address: str,
    cost_usd: Decimal,
    org_id: str = ORG_A,
) -> None:
    """One ledger row at a graph address.

    `graph_address` is the join key the cost rollup groups by, and `agent_run_id`
    is the DynamoDB `event_id` (a uuid4) — never the KEDA pod name, which is the
    trap `cost.py`'s join-key guard exists to catch.
    """
    session.add(
        UsageLog(
            org_id=org_id,
            department_id="dept",
            team_id="team",
            user_id=USER_ID,
            account_type="service",
            model="anthropic.claude-3-5-sonnet",
            input_tokens=100,
            output_tokens=50,
            cost_usd=cost_usd,
            latency_ms=100,
            status_code=200,
            graph_address=address,
            agent_run_id="4f1d2a90-1111-4222-8333-444455556666",
        )
    )
    await session.flush()


def address_of(node: OrchestrationNode, *, slug: str = FLOW_SLUG) -> str:
    return f"{slug}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}"


def node_by_ref(body: dict, node_ref: str) -> dict:
    return next(node for node in body["nodes"] if node["node_ref"] == node_ref)


class TestTenantIsolation:
    """A cross-org flow_id is 404 — not 403, and not an empty graph."""

    @pytest.mark.asyncio
    async def test_another_orgs_flow_is_404(self, session, app_with_router):
        """The headline isolation guarantee.

        404 rather than 403 deliberately: a 403 confirms the id exists somewhere,
        which lets a caller enumerate flows by status code.
        """
        foreign = await seed_flow(session, org_id=ORG_B)
        await seed_node(session, foreign, node_ref="story-a", org_id=ORG_B)

        response = client_for(app_with_router, org_id=ORG_A).get(route(foreign.id))

        assert response.status_code == 404, response.text

    @pytest.mark.asyncio
    async def test_another_orgs_flow_is_not_a_200_with_an_empty_graph(self, session, app_with_router):
        """The plausible bug, asserted separately from the 404.

        `list_nodes` is itself org-scoped, so an implementation that skips the
        flow resolution and just lists nodes returns `200` with `nodes: []`. That
        passes any "no data leaked" check while telling the operator "no work",
        which is a different and more misleading answer than "not found".
        """
        foreign = await seed_flow(session, org_id=ORG_B)
        await seed_node(session, foreign, node_ref="story-a", org_id=ORG_B)

        response = client_for(app_with_router, org_id=ORG_A).get(route(foreign.id))

        assert response.status_code != 200
        assert "nodes" not in response.json()

    @pytest.mark.asyncio
    async def test_no_foreign_node_titles_appear_in_the_response(self, session, app_with_router):
        foreign = await seed_flow(session, org_id=ORG_B)
        await seed_node(session, foreign, node_ref="secret-story", org_id=ORG_B)

        response = client_for(app_with_router, org_id=ORG_A).get(route(foreign.id))

        assert "secret-story" not in response.text

    @pytest.mark.asyncio
    async def test_an_absent_flow_is_also_404(self, session, app_with_router):
        """Absent and another-tenant's are the same status, by design."""
        response = client_for(app_with_router).get(route("does-not-exist"))

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_own_flow_is_readable(self, session, app_with_router):
        """The control for the isolation tests: the caller's own flow is 200."""
        flow = await seed_flow(session)
        await seed_node(session, flow, node_ref="story-a")

        response = client_for(app_with_router).get(route(flow.id))

        assert response.status_code == 200, response.text
        assert response.json()["flow_id"] == flow.id


class TestPermissionGate:
    """Gated on USAGE_READ, checked before anything is read."""

    @pytest.mark.asyncio
    async def test_denied_caller_gets_403(self, session, app_with_router):
        flow = await seed_flow(session)
        await seed_node(session, flow, node_ref="story-a")

        response = client_for(app_with_router, permitted=False).get(route(flow.id))

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_the_gate_runs_before_any_read(self, session, app_with_router):
        """A denied caller must not learn whether the flow exists.

        Asserted by comparing the two denials: an existing flow and an absent one
        must be indistinguishable to a caller without permission. If the read ran
        first, one would be 404 and the other 403.
        """
        flow = await seed_flow(session)
        client = client_for(app_with_router, permitted=False)

        existing = client.get(route(flow.id))
        absent = client.get(route("does-not-exist"))

        assert existing.status_code == absent.status_code == 403

    @pytest.mark.asyncio
    async def test_the_permission_checked_is_usage_read_not_plan_approve(self, session, app_with_router):
        """A read must not require the write authority over promotion state.

        Requiring `PLAN_APPROVE` would mean nobody could see where delivery stands
        without also being able to accept plans.
        """
        from unittest.mock import AsyncMock, MagicMock

        from fastapi.testclient import TestClient

        from src.admin.access_control import AccessControl
        from src.admin.config import AdminRole
        from src.orchestration.routes import get_access_control

        flow = await seed_flow(session)
        access = MagicMock(spec=AccessControl)
        access.check_permission = AsyncMock(return_value=True)
        access.get_user_role = AsyncMock(return_value=(AdminRole("org_admin"), ORG_A, None))
        app_with_router.dependency_overrides[get_access_control] = lambda: access

        TestClient(app_with_router).get(route(flow.id))

        assert access.check_permission.await_args.args[1] is Permission.USAGE_READ


class TestPendingNodesArePresent:
    """AC-1: the payload carries work that has not started."""

    @pytest.mark.asyncio
    async def test_a_flow_where_nothing_has_run_returns_every_node(self, session, app_with_router):
        """The core case. A view rendering only executed work shows an empty graph
        here and cannot answer "how much is left"."""
        flow = await seed_flow(session)
        await seed_node(session, flow, node_ref="story-a", state=NodeState.PENDING.value)
        await seed_node(session, flow, node_ref="story-b", state=NodeState.PENDING.value)
        await seed_node(session, flow, node_ref="eval-1", kind=NodeKind.EVAL.value, state=NodeState.PENDING.value)

        body = client_for(app_with_router).get(route(flow.id)).json()

        assert len(body["nodes"]) == 3
        assert {node["state"] for node in body["nodes"]} == {NodeState.PENDING.value}

    @pytest.mark.asyncio
    async def test_pending_nodes_survive_alongside_finished_ones(self, session, app_with_router):
        """The case a naive "return the active nodes" query passes while still
        being wrong: a part-run flow must still carry its look-ahead."""
        flow = await seed_flow(session)
        await seed_node(session, flow, node_ref="story-done", state=NodeState.PASSED.value)
        await seed_node(session, flow, node_ref="story-live", state=NodeState.RUNNING.value)
        await seed_node(session, flow, node_ref="story-later", state=NodeState.PENDING.value, wave="wave-2")

        body = client_for(app_with_router).get(route(flow.id)).json()

        assert len(body["nodes"]) == 3
        assert node_by_ref(body, "story-later")["state"] == NodeState.PENDING.value

    @pytest.mark.asyncio
    async def test_every_node_carries_its_address_components_and_kind(self, session, app_with_router):
        """The view groups by EPIC and wave to derive its containers, so the
        components have to be on each node."""
        flow = await seed_flow(session)
        await seed_node(session, flow, node_ref="gate-1", kind=NodeKind.GATE.value, epic="epic-2", wave="wave-3")

        node = node_by_ref(client_for(app_with_router).get(route(flow.id)).json(), "gate-1")

        assert node["epic_ref"] == "epic-2"
        assert node["wave_ref"] == "wave-3"
        assert node["kind"] == NodeKind.GATE.value
        assert node["id"]

    @pytest.mark.asyncio
    async def test_issue_ref_and_attempts_are_exposed(self, session, app_with_router):
        """`issue_ref` is what the deep link is built from; `attempts` is what
        "defect cycle N of M" is rendered from."""
        flow = await seed_flow(session)
        await seed_node(session, flow, node_ref="story-a", issue_ref="4206", attempts=2)

        node = node_by_ref(client_for(app_with_router).get(route(flow.id)).json(), "story-a")

        assert node["issue_ref"] == "4206"
        assert node["attempts"] == 2

    @pytest.mark.asyncio
    async def test_flow_metadata_carries_the_intent_reference(self, session, app_with_router):
        """The origin strip opens with intent → inception → fan-out, so the intent
        has to be readable from the payload."""
        flow = await seed_flow(session, intent_ref="4120")

        body = client_for(app_with_router).get(route(flow.id)).json()

        assert body["intent_ref"] == "4120"
        assert body["slug"] == FLOW_SLUG
        assert body["title"] == "Delivery loop"


class TestEdgesArePresent:
    """AC-2: parallel branches are only renderable from edges."""

    @pytest.mark.asyncio
    async def test_edges_are_returned_as_node_id_pairs(self, session, app_with_router):
        flow = await seed_flow(session)
        story = await seed_node(session, flow, node_ref="story-a")
        eval_node = await seed_node(session, flow, node_ref="eval-1", kind=NodeKind.EVAL.value)
        await OrchestrationRepository(session).add_edge(
            org_id=ORG_A,
            flow_id=flow.id,
            from_node_id=story.id,
            to_node_id=eval_node.id,
        )

        body = client_for(app_with_router).get(route(flow.id)).json()

        assert body["edges"] == [{"from_node_id": story.id, "to_node_id": eval_node.id}]

    @pytest.mark.asyncio
    async def test_a_fan_out_returns_every_branch(self, session, app_with_router):
        """Two nodes depending on one predecessor. Dropping either edge would make
        the branches render as a flat sequence, which AC-2 forbids."""
        flow = await seed_flow(session)
        root = await seed_node(session, flow, node_ref="story-root")
        left = await seed_node(session, flow, node_ref="story-left")
        right = await seed_node(session, flow, node_ref="story-right")
        repo = OrchestrationRepository(session)
        await repo.add_edge(org_id=ORG_A, flow_id=flow.id, from_node_id=root.id, to_node_id=left.id)
        await repo.add_edge(org_id=ORG_A, flow_id=flow.id, from_node_id=root.id, to_node_id=right.id)

        body = client_for(app_with_router).get(route(flow.id)).json()

        assert len(body["edges"]) == 2
        assert {edge["to_node_id"] for edge in body["edges"]} == {left.id, right.id}

    @pytest.mark.asyncio
    async def test_a_flow_with_no_edges_returns_an_empty_list(self, session, app_with_router):
        flow = await seed_flow(session)
        await seed_node(session, flow, node_ref="story-a")

        assert client_for(app_with_router).get(route(flow.id)).json()["edges"] == []


class TestStalledIsDistinguishable:
    """AC-3: stalled, halted and plainly-failed must not look the same.

    A stall moves the node to `failed` (`stall.py`), so `state` alone collapses a
    stall into an ordinary failure. The endpoint derives the difference from the
    append-only decision log.
    """

    async def _record(
        self,
        session: AsyncSession,
        flow: OrchestrationFlow,
        node: OrchestrationNode,
        *,
        kind: DecisionKind,
    ) -> None:
        await OrchestrationRepository(session).append_decision(
            org_id=ORG_A,
            flow_id=flow.id,
            node_id=node.id,
            kind=kind.value,
            actor_id="service:orchestration-stall",
            actor_role="service",
            actor_kind=ActorKind.SERVICE.value,
            reason="stalled: 1200s in 'running' exceeds the 900s threshold",
            from_state=NodeState.RUNNING.value,
            to_state=NodeState.FAILED.value,
        )

    @pytest.mark.asyncio
    async def test_a_stalled_node_is_flagged(self, session, app_with_router):
        flow = await seed_flow(session)
        node = await seed_node(session, flow, node_ref="story-stalled", state=NodeState.FAILED.value)
        await self._record(session, flow, node, kind=DecisionKind.NODE_STALLED)

        body = client_for(app_with_router).get(route(flow.id)).json()

        assert node_by_ref(body, "story-stalled")["stalled"] is True

    @pytest.mark.asyncio
    async def test_a_plainly_failed_node_is_not_flagged(self, session, app_with_router):
        """The discriminator. Both nodes are `failed`; only one stalled."""
        flow = await seed_flow(session)
        stalled = await seed_node(session, flow, node_ref="story-stalled", state=NodeState.FAILED.value)
        await seed_node(session, flow, node_ref="story-failed", state=NodeState.FAILED.value)
        await self._record(session, flow, stalled, kind=DecisionKind.NODE_STALLED)

        body = client_for(app_with_router).get(route(flow.id)).json()

        assert node_by_ref(body, "story-stalled")["stalled"] is True
        assert node_by_ref(body, "story-failed")["stalled"] is False

    @pytest.mark.asyncio
    async def test_a_halted_node_is_not_reported_as_stalled(self, session, app_with_router):
        """`halted` is readable from `state` and means something different: the
        defect-cycle bound was exhausted, so resuming it back into that cycle is
        the wrong move. Conflating the two would offer the wrong recovery."""
        flow = await seed_flow(session)
        node = await seed_node(session, flow, node_ref="story-halted", state=NodeState.HALTED.value, attempts=3)
        await self._record(session, flow, node, kind=DecisionKind.NODE_HALTED)

        body = client_for(app_with_router).get(route(flow.id)).json()

        halted = node_by_ref(body, "story-halted")
        assert halted["stalled"] is False
        assert halted["state"] == NodeState.HALTED.value

    @pytest.mark.asyncio
    async def test_a_node_that_stalled_then_halted_reads_as_halted(self, session, app_with_router):
        """Latest-wins, not any-match. A node that stalled, was resumed, and then
        exhausted its bound must not still read as stalled — the two findings call
        for opposite operator responses."""
        flow = await seed_flow(session)
        node = await seed_node(session, flow, node_ref="story-both", state=NodeState.HALTED.value, attempts=3)
        await self._record(session, flow, node, kind=DecisionKind.NODE_STALLED)
        await self._record(session, flow, node, kind=DecisionKind.NODE_HALTED)

        body = client_for(app_with_router).get(route(flow.id)).json()

        assert node_by_ref(body, "story-both")["stalled"] is False

    @pytest.mark.asyncio
    async def test_flow_level_decisions_do_not_flag_any_node(self, session, app_with_router):
        """A decision with `node_id = NULL` (plan acceptance) must not be
        attributed to a node."""
        flow = await seed_flow(session)
        await seed_node(session, flow, node_ref="story-a", state=NodeState.RUNNING.value)
        await OrchestrationRepository(session).append_decision(
            org_id=ORG_A,
            flow_id=flow.id,
            kind=DecisionKind.PLAN_ACCEPTED.value,
            actor_id=USER_ID,
            actor_role="org_admin",
            actor_kind=ActorKind.HUMAN.value,
        )

        body = client_for(app_with_router).get(route(flow.id)).json()

        assert all(node["stalled"] is False for node in body["nodes"])


class TestThreeValuedCost:
    """AC-22 at the API boundary: absence is never a number."""

    @pytest.mark.asyncio
    async def test_a_node_with_no_ledger_row_is_unknown_never_zero(self, session, app_with_router):
        flow = await seed_flow(session)
        await seed_node(session, flow, node_ref="story-a")

        node = node_by_ref(client_for(app_with_router).get(route(flow.id)).json(), "story-a")

        assert node["cost"]["status"] == "unknown"
        assert node["cost"]["amount_usd"] is None

    @pytest.mark.asyncio
    async def test_an_unknown_node_cost_carries_a_reason(self, session, app_with_router):
        """Bare "unknown" with no explanation reads as a UI bug, so the reason
        travels with it."""
        flow = await seed_flow(session)
        await seed_node(session, flow, node_ref="story-a")

        node = node_by_ref(client_for(app_with_router).get(route(flow.id)).json(), "story-a")

        assert node["cost"]["reason"] == "not_started"

    @pytest.mark.asyncio
    async def test_gate_and_eval_nodes_report_not_costable(self, session, app_with_router):
        """Both are `unknown`, but they are not the same news: a gate will never
        cost anything, while an unrun story may still cost money later."""
        flow = await seed_flow(session)
        await seed_node(session, flow, node_ref="gate-1", kind=NodeKind.GATE.value)
        await seed_node(session, flow, node_ref="eval-1", kind=NodeKind.EVAL.value)

        body = client_for(app_with_router).get(route(flow.id)).json()

        assert node_by_ref(body, "gate-1")["cost"]["reason"] == "not_costable"
        assert node_by_ref(body, "eval-1")["cost"]["reason"] == "not_costable"

    @pytest.mark.asyncio
    async def test_a_measured_node_carries_its_exact_amount(self, session, app_with_router):
        """A string on the wire, not a float: `Numeric(10, 6)` through a float
        loses the sub-cent precision that is most of an agent call's cost."""
        flow = await seed_flow(session)
        node = await seed_node(session, flow, node_ref="story-a", state=NodeState.PASSED.value)
        await seed_usage(session, address=address_of(node), cost_usd=Decimal("19.500000"))

        node_body = node_by_ref(client_for(app_with_router).get(route(flow.id)).json(), "story-a")

        assert node_body["cost"]["status"] == "known"
        assert Decimal(node_body["cost"]["amount_usd"]) == Decimal("19.5")

    @pytest.mark.asyncio
    async def test_a_verified_zero_is_none_incurred_not_unknown(self, session, app_with_router):
        """The two mean opposite things and both total zero dollars, which is
        exactly why the status is on the wire instead of inferred."""
        flow = await seed_flow(session)
        node = await seed_node(session, flow, node_ref="story-free", state=NodeState.PASSED.value)
        await seed_usage(session, address=address_of(node), cost_usd=Decimal("0"))

        node_body = node_by_ref(client_for(app_with_router).get(route(flow.id)).json(), "story-free")

        assert node_body["cost"]["status"] == "none_incurred"
        assert Decimal(node_body["cost"]["amount_usd"]) == Decimal("0")

    @pytest.mark.asyncio
    async def test_every_node_figure_carries_the_scope_label(self, session, app_with_router):
        """A figure that silently excludes build/infra cost reads as a total."""
        flow = await seed_flow(session)
        await seed_node(session, flow, node_ref="story-a")

        body = client_for(app_with_router).get(route(flow.id)).json()

        assert all(node["cost"]["scope"] for node in body["nodes"])
        assert "excludes build/infra" in body["nodes"][0]["cost"]["scope"]


class TestRollupCost:
    """The flow-level figure, with the labels that stop it being misread."""

    @pytest.mark.asyncio
    async def test_the_rollup_is_present_with_its_scope_label(self, session, app_with_router):
        flow = await seed_flow(session)
        node = await seed_node(session, flow, node_ref="story-a", state=NodeState.PASSED.value)
        await seed_usage(session, address=address_of(node), cost_usd=Decimal("31.50"))

        cost = client_for(app_with_router).get(route(flow.id)).json()["cost"]

        assert cost["status"] == "known"
        assert Decimal(cost["amount_usd"]) == Decimal("31.5")
        assert "excludes build/infra" in cost["scope"]

    @pytest.mark.asyncio
    async def test_a_rollup_containing_an_unmeasured_node_is_partial(self, session, app_with_router):
        """AC-21. The total omits an unmeasured contribution, so it is a lower
        bound — presenting it as a total is how decisions get made on wrong
        figures."""
        flow = await seed_flow(session)
        measured = await seed_node(session, flow, node_ref="story-a", state=NodeState.PASSED.value)
        await seed_node(session, flow, node_ref="story-b", state=NodeState.PENDING.value)
        await seed_usage(session, address=address_of(measured), cost_usd=Decimal("10.00"))

        cost = client_for(app_with_router).get(route(flow.id)).json()["cost"]

        assert cost["partial"] is True
        assert cost["unknown_node_count"] == 1
        assert cost["node_count"] == 2

    @pytest.mark.asyncio
    async def test_a_flow_with_nothing_measured_is_unknown_not_zero(self, session, app_with_router):
        """An untouched flow has not been established to be free."""
        flow = await seed_flow(session)
        await seed_node(session, flow, node_ref="story-a")

        cost = client_for(app_with_router).get(route(flow.id)).json()["cost"]

        assert cost["status"] == "unknown"
        assert cost["amount_usd"] is None
        assert cost["reason"] is not None

    @pytest.mark.asyncio
    async def test_another_orgs_usage_rows_are_not_counted(self, session, app_with_router):
        """Same address, different tenant. The rollup must not pick it up."""
        flow = await seed_flow(session)
        node = await seed_node(session, flow, node_ref="story-a", state=NodeState.PASSED.value)
        await seed_usage(session, address=address_of(node), cost_usd=Decimal("99.00"), org_id=ORG_B)

        cost = client_for(app_with_router).get(route(flow.id)).json()["cost"]

        assert cost["status"] == "unknown"
        assert cost["amount_usd"] is None


class TestNoApprovalRecordLeak:
    """The route reads `orchestration_decisions` but must not surface them.

    This is the counterpart to the note in `test_internal_plane_guard.py`'s
    allowlist. `_stalled_node_ids` consults the decision log, and decisions carry
    gate attribution — `actor_id`, `actor_role`, `actor_kind`, reason text. That is
    the approval record, and it is gated on `PLAN_APPROVE`, not on the
    `USAGE_READ` this route requires.

    So the boundary is: one derived boolean per node comes out, nothing else. That
    cannot be asserted from source text (the read is behind a helper), so it is
    asserted here on the response body — a change that starts returning
    attribution fails this test, which is the signal to raise the permission.
    """

    @pytest.mark.asyncio
    async def test_no_attribution_field_reaches_the_response(self, session, app_with_router):
        flow = await seed_flow(session)
        node = await seed_node(session, flow, node_ref="story-stalled", state=NodeState.FAILED.value)
        await OrchestrationRepository(session).append_decision(
            org_id=ORG_A,
            flow_id=flow.id,
            node_id=node.id,
            kind=DecisionKind.NODE_STALLED.value,
            actor_id="cognito-sub-the-approver",
            actor_role="org_admin",
            actor_kind=ActorKind.HUMAN.value,
            reason="a reason that must not be rendered by this route",
        )

        response = client_for(app_with_router).get(route(flow.id))

        # The flag derived from those rows IS present — that is the point of
        # reading them at all.
        assert node_by_ref(response.json(), "story-stalled")["stalled"] is True
        # The attribution behind it is not.
        for leaked in (
            "cognito-sub-the-approver",
            "actor_id",
            "actor_role",
            "actor_kind",
            "a reason that must not be rendered by this route",
            "decisions",
        ):
            assert leaked not in response.text, f"{leaked!r} leaked into a USAGE_READ response"

    @pytest.mark.asyncio
    async def test_the_accepted_plan_document_is_not_returned(self, session, app_with_router):
        """`plan_document` is "what was approved" and belongs to the PLAN_APPROVE
        read (`GET /plans`), not to this one."""
        flow = await seed_flow(session)
        await seed_node(session, flow, node_ref="story-a")
        await OrchestrationRepository(session).record_accepted_plan(
            org_id=ORG_A,
            flow_id=flow.id,
            plan_document={"secret_plan_key": "must not be rendered"},
            plan_hash="deadbeef",
        )

        response = client_for(app_with_router).get(route(flow.id))

        assert "secret_plan_key" not in response.text
        assert "plan_document" not in response.text


class TestContainersAreNotReturned:
    """§8.2 of the design contract: container state is derived, never stored.

    Returning a computed wave/EPIC state here would publish it as authoritative
    and create a second source of truth for a value its children already imply.
    """

    @pytest.mark.asyncio
    async def test_the_payload_has_no_container_collection(self, session, app_with_router):
        flow = await seed_flow(session)
        await seed_node(session, flow, node_ref="story-a", epic="epic-1", wave="wave-1")

        body = client_for(app_with_router).get(route(flow.id)).json()

        assert "waves" not in body
        assert "epics" not in body
        assert "containers" not in body

    @pytest.mark.asyncio
    async def test_only_the_three_executable_kinds_appear(self, session, app_with_router):
        """The node table holds story/eval/gate only, and the response must not
        synthesise a fourth kind for a container."""
        flow = await seed_flow(session)
        await seed_node(session, flow, node_ref="story-a", kind=NodeKind.STORY.value)
        await seed_node(session, flow, node_ref="eval-1", kind=NodeKind.EVAL.value)
        await seed_node(session, flow, node_ref="gate-1", kind=NodeKind.GATE.value)

        body = client_for(app_with_router).get(route(flow.id)).json()

        assert {node["kind"] for node in body["nodes"]} <= {kind.value for kind in NodeKind}


class TestCostRouteSharesTheSerialiser:
    """`GET /flows/{id}/cost` had no HTTP-level test before this story.

    It matters here because that route was refactored onto the serialisers this
    story added (`_node_cost_response` / `_flow_cost_response`), replacing its
    inline projection. Extracting shared code with tests on only one of its two
    callers means a future edit to the serialiser can silently change the older
    response shape. These tests pin the caller that had nothing holding it.
    """

    @pytest.mark.asyncio
    async def test_the_cost_route_projects_the_same_node_shape(self, session, app_with_router):
        """Same serialiser, so the same keys — asserted against the graph route's
        own output rather than a hand-written list, which would drift."""
        flow = await seed_flow(session)
        node = await seed_node(session, flow, node_ref="story-a")
        await seed_usage(session, address=address_of(node), cost_usd=Decimal("7.250000"))
        client = client_for(app_with_router)

        cost_body = client.get(f"{route(flow.id)}/cost").json()
        graph_body = client.get(route(flow.id)).json()

        assert cost_body["nodes"][0].keys() == node_by_ref(graph_body, "story-a")["cost"].keys()
        assert cost_body["nodes"][0]["amount_usd"] == "7.250000"
        assert cost_body["nodes"][0]["scope"] == COST_SCOPE_LABEL

    @pytest.mark.asyncio
    async def test_an_unmeasured_node_is_unknown_on_the_cost_route_too(self, session, app_with_router):
        """AC-22 holds on both readers of the serialiser, not just the new one."""
        flow = await seed_flow(session)
        await seed_node(session, flow, node_ref="story-a")

        body = client_for(app_with_router).get(f"{route(flow.id)}/cost").json()

        assert body["nodes"][0]["status"] == "unknown"
        assert body["nodes"][0]["amount_usd"] is None
        assert body["partial"] is True

    @pytest.mark.asyncio
    async def test_another_orgs_flow_is_404_on_the_cost_route(self, session, app_with_router):
        flow = await seed_flow(session, org_id=ORG_B, slug="other-tenant-flow")
        await seed_node(session, flow, node_ref="story-a", org_id=ORG_B)

        response = client_for(app_with_router, org_id=ORG_A).get(f"{route(flow.id)}/cost")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_the_cost_route_is_gated_on_usage_read(self, session, app_with_router):
        flow = await seed_flow(session)
        await seed_node(session, flow, node_ref="story-a")

        response = client_for(app_with_router, permitted=False).get(f"{route(flow.id)}/cost")

        assert response.status_code == 403
