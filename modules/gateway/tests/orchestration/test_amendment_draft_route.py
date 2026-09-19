"""`POST /orchestration/flows/{flow_id}/amendments/drafts` — the authoring ingress (#4529).

The store's guarantees are tested in `test_pending_amendments.py`. This file tests the
*door*: what an authoring agent holding the route's permission can and cannot reach, and
what the response tells the human who has to decide.

The route's distinguishing property is that `PLAN_DRAFT` is **necessary but not
sufficient**. Unlike #4528's new-flow registration, an amendment names an existing plan of
record, so "which plan" cannot be left to the caller. Three things must agree before
anything is written:

  1. the caller holds `PLAN_DRAFT` (checked first, before any read, so a denied caller
     learns nothing about what exists);
  2. the presented `X-Agent-RunId` equals the `author_run_id` the *server* wrote on the
     named assignment — the server must have commissioned this run for this request;
  3. the path's `flow_id` equals the assignment's flow.

All three refusals are asserted to write nothing and, where they could leak, to be
indistinguishable from each other.

The response half matters as much as the authz half. Fresh drafts report
`pending_human_accept`, because a worker reporting a replan as *done* when
a proposal is merely waiting is the failure mode that loses the amendment: a human who
reads "amended" stops looking. And `accept_command` is asserted to carry the draft id and
to be parseable by the real parser, because the human types this string back.
"""

import pytest
import sqlalchemy as sa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.access_control import AccessControl
from src.admin.config import AdminRole, Permission
from src.orchestration.models import (
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationNode,
    OrchestrationPendingAmendment,
    OrchestrationWorkClaim,
    PendingAmendmentState,
)
from src.shared.exceptions import BedrockGatewayError
from src.shared.models.base import Base

from .test_pending_amendments import (
    ORG_A,
    ORG_B,
    accepted_flow,
    address,
    amended_proposal,
    base_proposal,
    open_request,
)

AUTHOR_RUN = "orch:author-run-1"
AGENT_USER_ID = "cognito-sub-aidlc-worker"


def route_for(flow_id: str) -> str:
    return f"/orchestration/flows/{flow_id}/amendments/drafts"


@pytest.fixture
async def session():
    """In-memory SQLite session with working SAVEPOINTs. Same two pysqlite hooks as
    `test_pending_amendments.py` — see that module's docstring for why they are
    load-bearing rather than boilerplate."""
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


def token_context(org_id: str, *, scope: str = "", user_id: str = AGENT_USER_ID):
    """A service caller's token.

    `scope` defaults to `""` (not `"internal"`), so these tests take the
    authenticated-org path and no DynamoDB resolver is consulted. The
    `internal`-scope path is `draft_binding.py`'s and is tested in
    `test_draft_binding.py`; what matters here is that this route delegates to the
    same shared helper as `/flows/drafts`, which `test_tenant_resolution_is_shared`
    asserts on the source rather than by re-testing the resolver.
    """
    from datetime import UTC, datetime, timedelta

    from src.shared.schemas.auth import TokenContext

    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id="",
        department_id="",
        account_type="service",
        scope=scope,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def get_access_control_dep():
    from src.orchestration.draft_routes import get_access_control

    return get_access_control


@pytest.fixture
def app_with_router(session):
    """A minimal app carrying only the draft router.

    Deliberately not `create_app()`: that pulls the whole middleware stack and would
    make an authz assertion here depend on all of it. Same shape as
    `test_registration.py`'s.
    """
    from src.auth.dependencies import get_current_user
    from src.orchestration.draft_routes import get_run_binding_resolver
    from src.orchestration.draft_routes import router as draft_router
    from src.shared.database import get_db

    app = FastAPI()
    app.include_router(draft_router)

    # `AccessDeniedError` carries status_code=403 and is translated by the app-level
    # handler in `create_app()`. Registered here because this minimal app skips it;
    # without it a denied caller surfaces as 500 and the authz assertions would be
    # testing the harness rather than the route.
    @app.exception_handler(BedrockGatewayError)
    async def _gateway_error_handler(_request: Request, exc: BedrockGatewayError):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.error, "message": exc.message})

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: token_context(ORG_A)
    # Never reached on the non-internal-scope path, and overridden anyway so a
    # regression that starts consulting it fails loudly here instead of trying to
    # reach DynamoDB.
    app.dependency_overrides[get_run_binding_resolver] = lambda: _ExplodingResolver()
    return app


class _ExplodingResolver:
    async def resolve(self, _run_id):  # pragma: no cover - reached only by a regression
        raise AssertionError("a non-internal-scope caller must not consult the run-binding resolver")


def client_for(app, *, permitted: bool, role: str = AdminRole.MEMBER.value):
    """A TestClient whose access control is stubbed to permit or deny.

    Denial is expressed as `PLAN_DRAFT` because that is the permission this route
    gates on. `role` defaults to MEMBER, which is what a registry-resolved agent
    principal actually resolves to — the blast radius of the weakest holder is the
    one worth testing.
    """
    from unittest.mock import AsyncMock, MagicMock

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


async def count_rows(session: AsyncSession, model) -> int:
    return (await session.execute(sa.select(sa.func.count()).select_from(model.__table__))).scalar_one()


class TestAuthorization:
    async def test_a_commissioned_run_registers_a_pending_draft(self, session, app_with_router):
        flow_id = await accepted_flow(session)
        request = await open_request(session, flow_id)
        client = client_for(app_with_router, permitted=True)

        response = client.post(
            route_for(flow_id),
            params={"request_id": request.id},
            headers={"X-Agent-RunId": AUTHOR_RUN},
            json=amended_proposal().model_dump(mode="json"),
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["flow_id"] == flow_id
        assert body["request_id"] == request.id
        assert body["already_registered"] is False
        assert body["base_plan_version"] == 1

        stored = (await session.execute(sa.select(OrchestrationPendingAmendment))).scalar_one()
        assert stored.id == body["draft_id"]
        assert stored.state == PendingAmendmentState.PENDING.value

    async def test_the_route_gates_on_plan_draft_not_plan_approve(self, session, app_with_router):
        """The permission an agent principal can actually hold.

        Asserted on the recorded call rather than by reading the source. Gating on
        `PLAN_APPROVE` here would mean an authoring agent needs approval authority to
        propose an amendment — and an agent holding approval authority could accept
        the amendment it just wrote, which is the self-approval the EPIC forbids.
        """
        flow_id = await accepted_flow(session)
        request = await open_request(session, flow_id)
        client = client_for(app_with_router, permitted=True)

        client.post(
            route_for(flow_id),
            params={"request_id": request.id},
            headers={"X-Agent-RunId": AUTHOR_RUN},
            json=amended_proposal().model_dump(mode="json"),
        )

        access = app_with_router.dependency_overrides[get_access_control_dep()]()
        called = access.check_permission.await_args
        assert called.args[1] is Permission.PLAN_DRAFT
        # The AUTHENTICATED org, not the resolved owning tenant. See the long comment
        # in `register_draft`: `PLAN_DRAFT` is org-scoped, so passing the run's tenant
        # would trip the org-scope arm and turn a working path into a 403.
        assert called.kwargs["target_org_id"] == ORG_A

    async def test_a_caller_without_the_permission_gets_403_and_writes_nothing(self, session, app_with_router):
        """And the gate runs before the request is even looked up.

        So a denied caller cannot use the route to discover whether an assignment
        exists — the refusal is identical whether `request_id` is real or invented.
        """
        flow_id = await accepted_flow(session)
        request = await open_request(session, flow_id)
        client = client_for(app_with_router, permitted=False)

        real = client.post(
            route_for(flow_id),
            params={"request_id": request.id},
            headers={"X-Agent-RunId": AUTHOR_RUN},
            json=amended_proposal().model_dump(mode="json"),
        )
        invented = client.post(
            route_for(flow_id),
            params={"request_id": "no-such-request"},
            headers={"X-Agent-RunId": AUTHOR_RUN},
            json=amended_proposal().model_dump(mode="json"),
        )

        assert real.status_code == 403, real.text
        assert invented.status_code == real.status_code
        assert await count_rows(session, OrchestrationPendingAmendment) == 0

    async def test_a_run_that_was_not_commissioned_gets_404_and_writes_nothing(self, session, app_with_router):
        """THE binding, at the route. Permission alone is not enough.

        Without this, any caller that can reach this route could file an amendment
        against any assignment in its tenant — including one commissioned for a
        different flow, which is the target-ambiguity failure #4556 closed.
        """
        flow_id = await accepted_flow(session)
        request = await open_request(session, flow_id)
        client = client_for(app_with_router, permitted=True)

        response = client.post(
            route_for(flow_id),
            params={"request_id": request.id},
            headers={"X-Agent-RunId": "orch:some-other-run"},
            json=amended_proposal().model_dump(mode="json"),
        )

        assert response.status_code == 404, response.text
        assert response.json()["detail"]["error"] == "no_open_authoring_request"
        assert await count_rows(session, OrchestrationPendingAmendment) == 0

    async def test_no_run_header_at_all_is_refused(self, session, app_with_router):
        """An absent header must not match an unassigned request.

        Otherwise a caller sending nothing could claim a request the server has not
        yet published an envelope for.
        """
        flow_id = await accepted_flow(session)
        request = await open_request(session, flow_id)
        client = client_for(app_with_router, permitted=True)

        response = client.post(
            route_for(flow_id),
            params={"request_id": request.id},
            json=amended_proposal().model_dump(mode="json"),
        )

        assert response.status_code == 404, response.text
        assert await count_rows(session, OrchestrationPendingAmendment) == 0

    async def test_an_unknown_request_and_a_wrong_run_are_indistinguishable(self, session, app_with_router):
        """Same status and same error code, so ids cannot be enumerated."""
        flow_id = await accepted_flow(session)
        request = await open_request(session, flow_id)
        client = client_for(app_with_router, permitted=True)
        payload = amended_proposal().model_dump(mode="json")

        wrong_run = client.post(
            route_for(flow_id),
            params={"request_id": request.id},
            headers={"X-Agent-RunId": "orch:not-mine"},
            json=payload,
        )
        unknown = client.post(
            route_for(flow_id),
            params={"request_id": "11111111-1111-1111-1111-111111111111"},
            headers={"X-Agent-RunId": AUTHOR_RUN},
            json=payload,
        )

        assert wrong_run.status_code == unknown.status_code == 404
        assert wrong_run.json()["detail"]["error"] == unknown.json()["detail"]["error"]

    async def test_another_tenants_assignment_is_refused(self, session, app_with_router):
        """The tenant comes from the caller's resolved org, and scopes the lookup.

        A real assignment id from another tenant, with its real run id, still gets the
        same 404 — the request lookup is tenant-scoped in the query rather than
        checked afterwards.
        """
        from src.auth.dependencies import get_current_user

        flow_id = await accepted_flow(session)
        request = await open_request(session, flow_id)
        # The caller now authenticates as a different tenant.
        app_with_router.dependency_overrides[get_current_user] = lambda: token_context(ORG_B)
        client = client_for(app_with_router, permitted=True)

        response = client.post(
            route_for(flow_id),
            params={"request_id": request.id},
            headers={"X-Agent-RunId": AUTHOR_RUN},
            json=amended_proposal().model_dump(mode="json"),
        )

        assert response.status_code == 404, response.text
        assert await count_rows(session, OrchestrationPendingAmendment) == 0

    async def test_a_flow_that_is_not_the_assignments_flow_is_refused(self, session, app_with_router):
        """The path's flow is a check, not the source of truth.

        The draft attaches to the flow the *assignment* names, so a path flow that
        disagrees is a caller trying to re-aim its output — refused, and with the same
        opaque 404 so it learns nothing about whether that other flow exists.
        """
        flow_a = await accepted_flow(session)
        flow_b = await accepted_flow(session, proposal=base_proposal(flow="other-flow"))
        request_a = await open_request(session, flow_a)
        client = client_for(app_with_router, permitted=True)

        response = client.post(
            route_for(flow_b),
            params={"request_id": request_a.id},
            headers={"X-Agent-RunId": AUTHOR_RUN},
            json=amended_proposal(flow="other-flow").model_dump(mode="json"),
        )

        assert response.status_code == 404, response.text
        assert response.json()["detail"]["error"] == "no_open_authoring_request"
        assert await count_rows(session, OrchestrationPendingAmendment) == 0

    def test_tenant_resolution_is_shared_with_the_new_flow_route(self):
        """Both draft routes resolve the owning tenant through the same helper.

        Asserted on the source rather than by re-testing `draft_binding.py`, because
        the risk is divergence, not correctness: two copies of that block would be two
        chances for one of them to grow a fallback to `attributed_org_id`, and a
        fallback is the whole #4132 bypass — reachable by any caller who can make the
        binding lookup fail. Neither handler may read `attributed_org_id` at all.
        """
        import inspect

        from src.orchestration.draft_routes import register_amendment, register_draft

        for handler in (register_draft, register_amendment):
            source = inspect.getsource(handler)
            assert "_resolve_owning_tenant" in source, f"{handler.__name__} resolves the tenant some other way"
            assert "attributed_org_id" not in source, (
                f"{handler.__name__} reads attributed_org_id, which is caller-influenced and must never gate access"
            )

    async def test_request_id_is_required(self, session, app_with_router):
        """No default, and no "resolve the flow's open request" convenience.

        A route that inferred the assignment would let an author file against an ask
        it was not given — the authorization would become "is there any open request
        on this flow", which is not a check on the caller at all.
        """
        flow_id = await accepted_flow(session)
        await open_request(session, flow_id)
        client = client_for(app_with_router, permitted=True)

        response = client.post(
            route_for(flow_id),
            headers={"X-Agent-RunId": AUTHOR_RUN},
            json=amended_proposal().model_dump(mode="json"),
        )

        assert response.status_code == 422, response.text
        assert await count_rows(session, OrchestrationPendingAmendment) == 0


class TestInertness:
    async def test_registering_through_the_route_writes_no_graph_state(self, session, app_with_router):
        """THE property that makes the weaker permission safe, asserted end-to-end.

        Counted through the HTTP boundary rather than only at the store, because the
        route is where a future "helpfully compile it too" change would land.
        """
        flow_id = await accepted_flow(session)
        request = await open_request(session, flow_id)
        before = {
            model.__tablename__: await count_rows(session, model)
            for model in (OrchestrationNode, OrchestrationEdge, OrchestrationDecision, OrchestrationWorkClaim)
        }
        client = client_for(app_with_router, permitted=True)

        assert (
            client.post(
                route_for(flow_id),
                params={"request_id": request.id},
                headers={"X-Agent-RunId": AUTHOR_RUN},
                json=amended_proposal().model_dump(mode="json"),
            ).status_code
            == 201
        )

        after = {
            model.__tablename__: await count_rows(session, model)
            for model in (OrchestrationNode, OrchestrationEdge, OrchestrationDecision, OrchestrationWorkClaim)
        }
        assert after == before

    async def test_the_accepted_plan_still_in_force_is_the_original(self, session, app_with_router):
        """Registration is not acceptance. The plan of record does not move."""
        from src.orchestration.models import OrchestrationAcceptedPlan

        flow_id = await accepted_flow(session)
        request = await open_request(session, flow_id)
        client = client_for(app_with_router, permitted=True)

        client.post(
            route_for(flow_id),
            params={"request_id": request.id},
            headers={"X-Agent-RunId": AUTHOR_RUN},
            json=amended_proposal().model_dump(mode="json"),
        )

        in_force = (await session.execute(sa.select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.superseded_at.is_(None)))).scalar_one()
        assert in_force.version == 1
        assert await count_rows(session, OrchestrationAcceptedPlan) == 1

    async def test_the_draft_records_no_acceptance_actor(self, session, app_with_router):
        """An author cannot set who accepted, or that anything was accepted."""
        flow_id = await accepted_flow(session)
        request = await open_request(session, flow_id)
        client = client_for(app_with_router, permitted=True)

        client.post(
            route_for(flow_id),
            params={"request_id": request.id},
            headers={"X-Agent-RunId": AUTHOR_RUN},
            json=amended_proposal().model_dump(mode="json"),
        )

        stored = (await session.execute(sa.select(OrchestrationPendingAmendment))).scalar_one()
        assert stored.accepted_by is None
        assert stored.accepted_by_decision_id is None
        assert stored.accepted_plan_version is None
        assert stored.decided_at is None


class TestResponseContract:
    @pytest.mark.parametrize("state", ["accepted", "superseded", "rejected"])
    async def test_terminal_replay_reports_state_without_accept_command(self, session, app_with_router, state):
        flow_id = await accepted_flow(session)
        request = await open_request(session, flow_id)
        client = client_for(app_with_router, permitted=True)
        arguments = {
            "params": {"request_id": request.id},
            "headers": {"X-Agent-RunId": AUTHOR_RUN},
            "json": amended_proposal().model_dump(mode="json"),
        }
        first = client.post(route_for(flow_id), **arguments)
        assert first.status_code == 201, first.text
        await session.execute(
            sa.update(OrchestrationPendingAmendment).where(OrchestrationPendingAmendment.id == first.json()["draft_id"]).values(state=state)
        )
        await session.commit()
        replay = client.post(route_for(flow_id), **arguments)
        assert replay.status_code == 200, replay.text
        assert replay.json()["draft_id"] == first.json()["draft_id"]
        assert replay.json()["status"] == state
        assert replay.json()["accept_command"] == ""

    async def test_pending_registration_and_replay_report_pending_human_accept(self, session, app_with_router):
        """The literal, on a fresh registration AND on a retry.

        This is the "not a misleading successful replan" requirement expressed where
        the worker reads it. A response that ever said anything else on a path that
        wrote a draft would let a worker report an amendment as applied — and a human
        who reads "amended" stops looking, so the amendment is lost.
        """
        flow_id = await accepted_flow(session)
        request = await open_request(session, flow_id)
        client = client_for(app_with_router, permitted=True)
        payload = amended_proposal().model_dump(mode="json")

        first = client.post(
            route_for(flow_id),
            params={"request_id": request.id},
            headers={"X-Agent-RunId": AUTHOR_RUN},
            json=payload,
        )
        second = client.post(
            route_for(flow_id),
            params={"request_id": request.id},
            headers={"X-Agent-RunId": AUTHOR_RUN},
            json=payload,
        )

        assert first.json()["status"] == "pending_human_accept"
        assert second.json()["status"] == "pending_human_accept"

    async def test_an_identical_resubmission_gets_200_not_201(self, session, app_with_router):
        """A fail-soft author's retry is not a second draft to choose between."""
        flow_id = await accepted_flow(session)
        request = await open_request(session, flow_id)
        client = client_for(app_with_router, permitted=True)
        payload = amended_proposal().model_dump(mode="json")

        first = client.post(
            route_for(flow_id),
            params={"request_id": request.id},
            headers={"X-Agent-RunId": AUTHOR_RUN},
            json=payload,
        )
        second = client.post(
            route_for(flow_id),
            params={"request_id": request.id},
            headers={"X-Agent-RunId": AUTHOR_RUN},
            json=payload,
        )

        assert first.status_code == 201
        assert second.status_code == 200, second.text
        assert second.json()["already_registered"] is True
        assert second.json()["draft_id"] == first.json()["draft_id"]
        assert await count_rows(session, OrchestrationPendingAmendment) == 1

    async def test_the_accept_command_names_the_draft(self, session, app_with_router):
        """Composed server-side, and it must carry the id.

        A bare `@agent-engine accept` answers the acceptance gate; it must never
        select an amendment. So the command handed to the human is the only form that
        can apply this draft, and it is built here rather than by the worker so the
        bridge's two halves cannot word it differently.
        """
        flow_id = await accepted_flow(session)
        request = await open_request(session, flow_id)
        client = client_for(app_with_router, permitted=True)

        body = client.post(
            route_for(flow_id),
            params={"request_id": request.id},
            headers={"X-Agent-RunId": AUTHOR_RUN},
            json=amended_proposal().model_dump(mode="json"),
        ).json()

        assert body["accept_command"] == f"@agent-engine accept amendment {body['draft_id']}"

    async def test_the_gate_diff_is_computed_server_side(self, session, app_with_router):
        """From the two documents, not from the author's account of its own changes.

        An authoring agent summarising which gates it removed is precisely the claim
        that must not be trusted, since a removed gate is a removed human decision.
        """
        flow_id = await accepted_flow(session)
        request = await open_request(session, flow_id)
        client = client_for(app_with_router, permitted=True)

        body = client.post(
            route_for(flow_id),
            params={"request_id": request.id},
            headers={"X-Agent-RunId": AUTHOR_RUN},
            json=amended_proposal().model_dump(mode="json"),
        ).json()

        diff = body["gate_diff"]
        assert diff["added"] == [address("spend-gate")]
        assert diff["removed"] == []
        assert diff["unchanged"] == [address("deploy-gate", wave="wave-2")]
        assert diff["changes_gating"] is True

    async def test_an_amendment_that_removes_a_gate_says_so(self, session, app_with_router):
        """The dangerous direction, reported plainly rather than as an absence."""
        flow_id = await accepted_flow(session)
        request = await open_request(session, flow_id)
        client = client_for(app_with_router, permitted=True)
        # Same waves, but the wave-2 deploy gate is gone.
        gateless = amended_proposal(extra_gate=False)
        gateless = gateless.model_copy(
            update={
                "nodes": [node for node in gateless.nodes if node.kind != "gate"],
                "edges": [edge for edge in gateless.edges if "deploy-gate" not in edge.to_address],
            }
        )

        body = client.post(
            route_for(flow_id),
            params={"request_id": request.id},
            headers={"X-Agent-RunId": AUTHOR_RUN},
            json=gateless.model_dump(mode="json"),
        ).json()

        assert body["gate_diff"]["removed"] == [address("deploy-gate", wave="wave-2")]
        assert body["gate_diff"]["changes_gating"] is True

    async def test_the_response_carries_no_extra_fields(self, session, app_with_router):
        """`extra="forbid"` on the model, pinned on the wire.

        The worker renders this into a human-facing comment, so a field appearing or
        vanishing silently is a rendering bug in a place nobody is watching.
        """
        flow_id = await accepted_flow(session)
        request = await open_request(session, flow_id)
        client = client_for(app_with_router, permitted=True)

        body = client.post(
            route_for(flow_id),
            params={"request_id": request.id},
            headers={"X-Agent-RunId": AUTHOR_RUN},
            json=amended_proposal().model_dump(mode="json"),
        ).json()

        assert set(body) == {
            "draft_id",
            "flow_id",
            "request_id",
            "base_plan_version",
            "proposal_hash",
            "gate_diff",
            "already_registered",
            "status",
            "accept_command",
            "flow_url",
        }

    async def test_the_accept_command_is_what_the_parser_recognises(self, session, app_with_router):
        """End-to-end against the real parser, not a restatement of the format.

        The whole reason the command is composed server-side is that a human types it
        back. If the string this route hands out is not the string
        `engine_commands.py` parses, the human follows a working instruction that
        does nothing — and the failure is silent on both ends.
        """
        from src.orchestration.adapters.github_commands import CommandVerb, parse_engine_command

        flow_id = await accepted_flow(session)
        request = await open_request(session, flow_id)
        client = client_for(app_with_router, permitted=True)

        body = client.post(
            route_for(flow_id),
            params={"request_id": request.id},
            headers={"X-Agent-RunId": AUTHOR_RUN},
            json=amended_proposal().model_dump(mode="json"),
        ).json()

        parsed = parse_engine_command(body["accept_command"])
        assert parsed is not None, "the route handed out a command the parser does not recognise"
        # And it must reach the amendment path, not the acceptance gate. `\Aaccept\b`
        # matches this string too, so the verb — not merely "it parsed" — is the
        # assertion that matters.
        assert parsed.verb is CommandVerb.ACCEPT_AMENDMENT
        assert parsed.draft_ref == body["draft_id"]
