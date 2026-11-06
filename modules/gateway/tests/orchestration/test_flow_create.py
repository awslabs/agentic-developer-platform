"""Tests for plan ingress: `POST /orchestration/flows`.

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
  - **Dispatchability is visible at submission, with EVERY cause**: a plan can
    trip several independent dispatch preconditions — a story/eval node with no
    routable issue reference, an org that does not resolve to exactly one GitHub
    installation, an unconfigured dispatch repository. Each produces a successful
    submission that can never dispatch. All of them are surfaced in the response
    rather than left as a silent `undispatchable` counter a tick later, and
    surfaced *together*: a submitter told one of three causes fixes it,
    resubmits, and hits the same silent stall (#4334).
  - **Commit boundary**: the route commits, so the rows survive the request. A
    partially-committed graph — nodes but no edges — would make dispatch run work
    out of dependency order.

The session fixture is `test_compile.py`'s, including its two pysqlite hooks —
they are load-bearing, not boilerplate: without them `begin_nested()` becomes the
outermost unit of work and `compile_proposal`'s savepoint is not exercised.
"""

import json

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.config import Permission
from src.orchestration.compile import ApprovalContext
from src.orchestration.models import (
    DecisionKind,
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationFlow,
    OrchestrationNode,
)
from src.orchestration.proposal import LoopProposal, ProposedEdge, ProposedNode, RepositoryConnection
from src.orchestration.repository import OrchestrationRepository
from src.orchestration.state import ActorKind, NodeState
from src.products.models import Product
from src.shared.models.base import Base
from src.shared.models.organization import Organization, User
from tests.orchestration.legacy_plan import persist_legacy_plan
from tests.orchestration.product_fixture import seed_product, seed_product_membership

ORG_A = "org-alpha"
ORG_B = "org-beta"
FLOW = "delivery-loop"
SPEC_REVISION = "issue-4120-r1"
USER_ID = "cognito-sub-operator"

ROUTE = "/orchestration/flows"


@pytest.fixture(autouse=True)
def dispatch_repo_configured(monkeypatch):
    """A configured dispatch target repository, for every test by default.

    Autouse because "the engine has somewhere to deliver into" is the normal
    state of a deployed environment, and it is a precondition of dispatch — so a
    test asserting a plan is dispatchable must have it set or it is asserting
    against a misconfigured gateway. The unconfigured case is a real cause and is
    covered explicitly by its own test, which unsets this.
    """
    monkeypatch.setenv("BG_ORCH_DISPATCH_REPO", "aws-e/adp")


@pytest.fixture
def verified_repositories(monkeypatch):
    from src.orchestration import repository_binding

    async def resolve(_session, *, tenant_id, name, repository_id, issue, operation, installation_id, **_kwargs):
        assert tenant_id == ORG_A
        assert (name, repository_id) in {("demo/repo-a", 1001), ("demo/repo-b", 1002)}
        selected = installation_id or (12345 if repository_id == 1001 else 901)
        return repository_binding.VerifiedRepository("github", repository_id, name, selected, name, issue, operation)

    monkeypatch.setattr(repository_binding, "resolve_story_repository", resolve)


@pytest.fixture
def authenticated_story_provider(monkeypatch):
    from types import SimpleNamespace

    import httpx

    from src.admin.connections.github_client import GitHubAppClient
    from src.admin.installations.resolver import OwnerState
    from src.orchestration import repository_binding

    state = {"case": "valid", "paths": []}

    def respond(request):
        path = request.url.path
        state["paths"].append(path)
        case = state["case"]
        observed_name = "demo/renamed" if case in {"rename", "reused_name"} else "demo/repo-b"
        if path == "/repositories/1002":
            return httpx.Response(200, json={"id": 2002 if case == "id_mismatch" else 1002, "full_name": observed_name})
        if path == "/repos/demo/repo-b":
            return httpx.Response(200, json={"id": 2002, "full_name": "demo/repo-b"}) if case == "reused_name" else httpx.Response(404)
        if path == f"/repos/{observed_name}/issues/42":
            issue_repository = "demo/repo-a" if case == "issue_in_a" else observed_name
            return httpx.Response(
                200,
                json={
                    "id": 9001,
                    "node_id": "I_9001",
                    "number": 43 if case == "issue_number_mismatch" else 42,
                    "repository_url": f"https://api.github.com/repos/{issue_repository}",
                    "html_url": f"https://github.com/{issue_repository}/issues/42",
                },
            )
        pytest.fail(f"unexpected provider request {path}")

    transport = httpx.MockTransport(respond)
    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        return original_client(*args, transport=transport, **kwargs)

    async def owner(installation_id, *, db):
        assert installation_id == 11
        return SimpleNamespace(tenant_id=ORG_A, github_account_id="111"), OwnerState.RESOLVED

    async def credentials(tenant_id):
        assert tenant_id == ORG_A
        return "fictional-app", "fictional-key"

    async def token(*args, **kwargs):
        assert kwargs.get("repository_ids") == [1002] or kwargs.get("repositories") == ["repo-b"]
        return "synthetic-token", "unused"

    async def installation(_self, installation_id):
        assert installation_id == 11
        return {"account": {"id": 111}}

    monkeypatch.setattr(repository_binding, "resolve_installation_owner", owner)
    monkeypatch.setattr(repository_binding, "resolve_tenant_app_credentials", credentials)
    monkeypatch.setattr(repository_binding, "mint_installation_token_with_expiry", token)
    monkeypatch.setattr(repository_binding.httpx, "AsyncClient", client)
    monkeypatch.setattr(GitHubAppClient, "get_installation", installation)
    return state


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
        await seed_product(s, org_id=ORG_A, actor_id=USER_ID)
        await s.commit()
        yield s
    await engine.dispose()


def address(node_ref: str, *, epic: str = "epic-1", wave: str = "wave-1") -> str:
    return f"{FLOW}/{epic}/{wave}/{node_ref}"


def valid_proposal(*, org_id: str = ORG_A, **overrides) -> LoopProposal:
    """A well-formed two-node wave: one story feeding its eval.

    Note the eval node carries no `issue_ref`. Evaluations dispatch to an issue
    exactly as stories do, so this proposal is well-formed as a *document* and
    still not fully routable — which is why the dispatchability tests below build
    their own fully-routed proposal rather than reusing this one.
    """
    payload = {
        "flow_slug": FLOW,
        "title": "Delivery loop",
        "product_id": f"product-{org_id}",
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


def routable_proposal(*, org_id: str = ORG_A, **overrides) -> LoopProposal:
    """A plan every node of which can actually be dispatched.

    Story and eval both carry an issue; the gate deliberately does not, because a
    gate is presented by the tick and never consumes a worker. So this document is
    the positive control for "no false positives": if the route flags anything
    here, it is flagging a plan the engine would dispatch.
    """
    payload = {
        "flow_slug": FLOW,
        "title": "Delivery loop",
        "org_id": org_id,
        "spec_revision": SPEC_REVISION,
        "intent_ref": "4120",
        "nodes": [
            ProposedNode(address=address("story-a"), kind="story", title="Story A", issue_ref="4320"),
            ProposedNode(address=address("eval"), kind="eval", title="Wave 1 eval", issue_ref="4321"),
            ProposedNode(address=address("gate"), kind="gate", title="Wave 1 gate"),
        ],
        "edges": [
            ProposedEdge(from_address=address("story-a"), to_address=address("eval")),
            ProposedEdge(from_address=address("eval"), to_address=address("gate")),
        ],
    }
    payload.update(overrides)
    return LoopProposal(**payload)


def causes_of(body: dict) -> set[str]:
    """The stable cause ids from a submission response."""
    return {cause["cause"] for cause in body["dispatch_blocked_causes"]}


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
    @pytest.mark.parametrize(
        ("second_repository_id", "ordered"),
        [(1001, False), (1001, True), (1002, False)],
        ids=["same-repository-unordered", "same-repository-ordered", "different-repositories-unordered"],
    )
    async def test_duplicate_work_scenario_through_flow_acceptance(
        self, session, app_with_router, second_repository_id, ordered, verified_repositories
    ):
        await seed_org(session, ORG_A, installation_ids=["12345"])
        await session.merge(User(id=USER_ID, org_id=ORG_A, team_id="team", email="owner@example.test"))
        await seed_product_membership(session, org_id=ORG_A, user_id=USER_ID)
        session.add(
            Product(
                team_id=f"product-team-{ORG_A}",
                id="product-1",
                tenant_id=ORG_A,
                name="Demo",
                purpose="Demo",
                intended_users=["team"],
                owner_user_id=USER_ID,
                starting_point="running_product",
                created_by=USER_ID,
            )
        )
        await session.flush()
        proposal = valid_proposal(product_id="product-1")
        proposal.nodes[0].issue_ref = "42"
        proposal.nodes[0].repository_identity = "demo/repo-a"
        proposal.nodes[0].provider_repository_id = 1001
        proposal.nodes.insert(
            1,
            ProposedNode(
                address=address("story-b"),
                kind="story",
                title="Follow-up",
                issue_ref="#42",
                repository_identity="demo/repo-a" if second_repository_id == 1001 else "demo/repo-b",
                provider_repository_id=second_repository_id,
            ),
        )
        proposal.edges.append(ProposedEdge(from_address=address("story-b"), to_address=address("eval")))
        if ordered:
            proposal.edges.append(ProposedEdge(from_address=address("story-a"), to_address=address("story-b")))

        response = client_for(app_with_router, permitted=True).post(ROUTE, json=proposal.model_dump(mode="json"))

        if second_repository_id == 1001 and not ordered:
            assert response.status_code == 422, response.text
            assert {violation["rule"] for violation in response.json()["detail"]["violations"]} >= {"unordered_same_issue"}
            for table in (OrchestrationFlow, OrchestrationNode, OrchestrationEdge, OrchestrationAcceptedPlan, OrchestrationDecision):
                assert await count_rows(session, table) == 0
            return

        assert response.status_code == 201, response.text
        flow_id = response.json()["flow_id"]
        plan = await OrchestrationRepository(session).get_accepted_plan(org_id=ORG_A, flow_id=flow_id)
        assert plan.plan_document["product_id"] == "product-1"
        assert {(node["provider_repository_id"], node["issue_ref"]) for node in plan.plan_document["nodes"] if node["kind"] == "story"} == {
            (1001, "42"),
            (second_repository_id, "#42"),
        }
        projected = await submitted_flow_nodes(session, flow_id)
        assert {projected[address(story)].provider_repository_id for story in ("story-a", "story-b")} == {1001, second_repository_id}
        assert ({"from_address": address("story-a"), "to_address": address("story-b")} in plan.plan_document["edges"]) is ordered

    @pytest.mark.asyncio
    async def test_same_repository_repeated_issue_requires_accepted_order(self, session, app_with_router, verified_repositories):
        await seed_org(session, ORG_A, installation_ids=["12345"])
        await session.merge(User(id=USER_ID, org_id=ORG_A, team_id="team", email="owner@example.test"))
        await seed_product_membership(session, org_id=ORG_A, user_id=USER_ID)
        session.add(
            Product(
                team_id=f"product-team-{ORG_A}",
                id="product-1",
                tenant_id=ORG_A,
                name="Demo",
                purpose="Demo",
                intended_users=["team"],
                owner_user_id=USER_ID,
                starting_point="running_product",
                created_by=USER_ID,
            )
        )
        await session.flush()
        proposal = valid_proposal(product_id="product-1")
        proposal.nodes[0].issue_ref = "42"
        proposal.nodes[0].repository_identity = "demo/repo-a"
        proposal.nodes[0].provider_repository_id = 1001
        proposal.nodes.insert(
            1,
            ProposedNode(
                address=address("story-b"),
                kind="story",
                title="Follow-up",
                issue_ref="#42",
                repository_identity="demo/repo-a",
                provider_repository_id=1001,
            ),
        )
        proposal.edges.append(ProposedEdge(from_address=address("story-b"), to_address=address("eval")))
        client = client_for(app_with_router, permitted=True)

        unordered = client.post(ROUTE, json=proposal.model_dump(mode="json"))
        assert unordered.status_code == 422, unordered.text
        assert "unordered_same_issue" in str(unordered.json())
        assert await count_rows(session, OrchestrationFlow) == 0
        assert await count_rows(session, OrchestrationAcceptedPlan) == 0

        proposal.edges.append(ProposedEdge(from_address=address("story-a"), to_address=address("story-b")))
        ordered = client.post(ROUTE, json=proposal.model_dump(mode="json"))
        assert ordered.status_code == 201, ordered.text
        plan = await OrchestrationRepository(session).get_accepted_plan(org_id=ORG_A, flow_id=ordered.json()["flow_id"])
        assert {("demo/repo-a", 1001, "42"), ("demo/repo-a", 1001, "#42")} <= {
            (node["repository_identity"], node["provider_repository_id"], node["issue_ref"])
            for node in plan.plan_document["nodes"]
            if node["kind"] == "story"
        }
        assert {"from_address": address("story-a"), "to_address": address("story-b")} in plan.plan_document["edges"]

    @pytest.mark.asyncio
    async def test_product_bound_stories_in_two_repositories(self, session, app_with_router, verified_repositories):
        await seed_org(session, ORG_A, installation_ids=["12345"])
        await session.merge(User(id=USER_ID, org_id=ORG_A, team_id="team", email="owner@example.test"))
        await seed_product_membership(session, org_id=ORG_A, user_id=USER_ID)
        session.add(
            Product(
                team_id=f"product-team-{ORG_A}",
                id="product-1",
                tenant_id=ORG_A,
                name="Demo",
                purpose="Demo",
                intended_users=["team"],
                owner_user_id=USER_ID,
                starting_point="running_product",
                created_by=USER_ID,
            )
        )
        await session.flush()
        proposal = valid_proposal(product_id="product-1")
        proposal.nodes[0].issue_ref = "42"
        proposal.nodes[0].repository_identity = "demo/repo-a"
        proposal.nodes[0].provider_repository_id = 1001
        proposal.nodes.insert(
            1,
            ProposedNode(
                address=address("story-b"),
                kind="story",
                title="Story B",
                issue_ref="#42",
                repository_identity="demo/repo-b",
                provider_repository_id=1002,
            ),
        )
        proposal.edges.append(ProposedEdge(from_address=address("story-b"), to_address=address("eval")))
        proposal.nodes[1].repository_connection = RepositoryConnection(provider="github", installation_id=901)

        response = client_for(app_with_router, permitted=True).post(ROUTE, json=proposal.model_dump(mode="json"))

        assert response.status_code == 201, response.text
        flow_id = response.json()["flow_id"]
        flow = await session.get(OrchestrationFlow, flow_id)
        plan = await OrchestrationRepository(session).get_accepted_plan(org_id=ORG_A, flow_id=flow_id)
        stories = [node for node in (await submitted_flow_nodes(session, flow_id)).values() if node.kind == "story"]
        assert flow.product_id == plan.plan_document["product_id"] == "product-1"
        assert {(node.repository_identity, node.provider_repository_id) for node in stories} == {("demo/repo-a", 1001), ("demo/repo-b", 1002)}
        assert response.json()["product_id"] == "product-1"
        client = client_for(app_with_router, permitted=True)
        graph = client.get(f"{ROUTE}/{flow_id}")
        assert graph.status_code == 200, graph.text
        assert graph.json()["product_id"] == "product-1"
        assert next(node for node in graph.json()["nodes"] if node["node_ref"] == "story-b")["repository_connection"] == {
            "provider": "github",
            "installation_id": 901,
        }
        assert {(node["repository_identity"], node["provider_repository_id"]) for node in graph.json()["nodes"] if node["kind"] == "story"} == {
            ("demo/repo-a", 1001),
            ("demo/repo-b", 1002),
        }
        summaries = client.get(ROUTE)
        assert summaries.status_code == 200, summaries.text
        assert next(item for item in summaries.json()["flows"] if item["id"] == flow_id)["product_id"] == "product-1"
        assert response.json()["dispatchable"] is False
        assert "repository_execution_withheld" in causes_of(response.json())
        assert "compatible execution readers" in response.json()["dispatch_blocked_reason"]

        retried = client_for(app_with_router, permitted=True).post(ROUTE, json=proposal.model_dump(mode="json"))
        assert retried.status_code == 200, retried.text
        assert retried.json()["flow_id"] == flow_id
        assert retried.json()["dispatchable"] is False
        assert "repository_execution_withheld" in causes_of(retried.json())

    @pytest.mark.asyncio
    async def test_two_verified_stories_admit_repositories_but_withhold_worker_dispatch(
        self, session, app_with_router, verified_repositories, monkeypatch
    ):
        from src.orchestration import repository_binding
        from src.orchestration.accepted_node import accepted_node_contract
        from src.orchestration.dispatch_pass import DispatchPassConfig, run_dispatch_pass
        from src.orchestration.models import OrchestrationWorkClaim
        from src.orchestration.work_admission import resolve_accepted_repository

        await seed_org(session, ORG_A, installation_ids=["12345", "901", "777"])
        await seed_product(session, org_id=ORG_A, actor_id=USER_ID, product_id="product-1")
        proposal = valid_proposal(product_id="product-1")
        proposal.nodes[0].issue_ref = "42"
        proposal.nodes[0].repository_identity = "demo/repo-a"
        proposal.nodes[0].provider_repository_id = 1001
        proposal.nodes.insert(
            1,
            ProposedNode(
                address=address("story-b"),
                kind="story",
                title="Story B",
                issue_ref="42",
                repository_identity="demo/repo-b",
                provider_repository_id=1002,
                repository_connection=RepositoryConnection(provider="github", installation_id=901),
            ),
        )
        proposal.nodes[2].issue_ref = "43"
        proposal.edges.append(ProposedEdge(from_address=address("story-b"), to_address=address("eval")))
        calls = []
        resolve = repository_binding.resolve_story_repository

        async def checked(*args, **kwargs):
            calls.append((kwargs["operation"], kwargs["name"], kwargs.get("installation_id")))
            return await resolve(*args, **kwargs)

        monkeypatch.setattr(repository_binding, "resolve_story_repository", checked)
        accepted = client_for(app_with_router, permitted=True).post(ROUTE, json=proposal.model_dump(mode="json"))
        assert accepted.status_code == 201, accepted.text
        assert causes_of(accepted.json()) == {"repository_execution_withheld"}
        flow_id = accepted.json()["flow_id"]
        flow = await session.get(OrchestrationFlow, flow_id)
        flow.execution_paused = False
        nodes = await submitted_flow_nodes(session, flow_id)
        stories = [node for node in nodes.values() if node.kind == "story"]
        assert {(node.repository_identity, node.provider_repository_id, node.repository_connection["installation_id"]) for node in stories} == {
            ("demo/repo-a", 1001, 12345),
            ("demo/repo-b", 1002, 901),
        }
        admission_results = {
            await resolve_accepted_repository(session, node=story, contract=await accepted_node_contract(session, story), repo="demo/default")
            for story in stories
        }
        assert admission_results == {(12345, 1001, "demo/repo-a"), (901, 1002, "demo/repo-b")}
        assert [call[0] for call in calls].count("accept") == 2
        assert {call[2] for call in calls if call[0] == "dispatch"} == {12345, 901}
        for story in stories:
            story.state = NodeState.READY.value
        await session.flush()
        monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
        monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
        report = await run_dispatch_pass(
            session,
            DispatchPassConfig(queue_url="https://sqs.us-east-1.amazonaws.com/123456789012/test.fifo", repo="demo/default"),
        )
        assert report.errors == report.admission_refused == report.dispatched == 0
        assert report.undispatchable == 2
        assert report.pending == []
        assert (await session.scalars(sa.select(OrchestrationWorkClaim))).all() == []
        assert all(story.state == NodeState.READY.value and story.attempts == 0 for story in stories)

    @pytest.mark.asyncio
    async def test_unbound_product_story_cannot_use_default_installation_at_admission(self, session, app_with_router):
        from src.orchestration.accepted_node import accepted_node_contract
        from src.orchestration.work_admission import resolve_accepted_repository
        from src.orchestration.work_claims import WorkClaimError

        await seed_org(session, ORG_A, installation_ids=["12345"])
        await seed_product(session, org_id=ORG_A, actor_id=USER_ID, product_id="product-1")
        proposal = valid_proposal(product_id="product-1")
        accepted = client_for(app_with_router, permitted=True).post(ROUTE, json=proposal.model_dump(mode="json"))
        assert accepted.status_code == 201, accepted.text
        assert "repository_verification_pending" in causes_of(accepted.json())
        story = (await submitted_flow_nodes(session, accepted.json()["flow_id"]))[address("story-a")]
        with pytest.raises(WorkClaimError) as caught:
            await resolve_accepted_repository(session, node=story, contract=await accepted_node_contract(session, story), repo="demo/default")
        assert caught.value.code == "repository_unavailable"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("case", "selector", "expected"),
        [
            ("missing", False, "repository_unavailable"),
            ("wrong_tenant", True, "connection_not_owned"),
            ("revoked", True, "connection_revoked"),
            ("ambiguous", False, "repository_connection_ambiguous"),
            ("policy", True, "repository_not_permitted"),
            ("provider_unavailable", True, "repository_verification_unavailable"),
            ("owner_unavailable", True, "repository_verification_unavailable"),
        ],
    )
    async def test_repository_refusals_through_flow_acceptance(self, session, app_with_router, monkeypatch, case, selector, expected):
        from types import SimpleNamespace

        from src.admin.installations.resolver import OwnerState
        from src.orchestration import repository_binding
        from src.orchestration.models import OrchestrationWorkClaim
        from src.shared.models.vault import ChannelTenantMap

        await seed_org(session, ORG_A, installation_ids=["11", "22"])
        await seed_product(session, org_id=ORG_A, actor_id=USER_ID, product_id="product-1")
        for installation_id in () if case == "missing" else (11, 22) if case == "ambiguous" else (11,):
            session.add(
                ChannelTenantMap(
                    provider="github",
                    provider_scope_id=str(installation_id + 100),
                    org_id=ORG_A,
                    installation_id=str(installation_id),
                    install_metadata={"account_id": str(installation_id + 100)},
                )
            )
        await session.flush()
        reads = []

        async def owner(installation_id, *, db):
            if case == "owner_unavailable":
                raise TimeoutError("owner metadata unavailable")
            tenant_id = ORG_B if case == "wrong_tenant" else ORG_A
            state = OwnerState.REVOKED if case == "revoked" else OwnerState.RESOLVED
            return SimpleNamespace(tenant_id=tenant_id, github_account_id=str(installation_id + 100)), state

        async def read(_tenant, installation_id, name, repository_id, issue):
            reads.append(installation_id)
            if case == "provider_unavailable":
                raise TimeoutError("provider metadata unavailable")
            assert (name, repository_id, issue) == ("demo/repo-a", 1001, 42)
            return {"id": 1001, "full_name": name}, {"account": {"id": installation_id + 100}}, {"id": issue + 10000, "node_id": f"I_{issue}"}

        monkeypatch.setattr(repository_binding, "resolve_installation_owner", owner)
        monkeypatch.setattr(repository_binding, "_provider_read", read)
        proposal = valid_proposal(product_id="product-1")
        proposal.nodes[0].issue_ref = "42"
        proposal.nodes[0].repository_identity = "demo/repo-a"
        proposal.nodes[0].provider_repository_id = 1001
        if selector:
            proposal.nodes[0].repository_connection = RepositoryConnection(provider="github", installation_id=11)
        if case == "policy":
            from tests.orchestration.test_execution_policy_acceptance import a_policy

            proposal.execution_policy = a_policy(repository_ids=["demo/other"], evaluation_acceptance={})
        response = client_for(app_with_router, permitted=True).post(ROUTE, json=proposal.model_dump(mode="json"))
        assert response.status_code == 422, response.text
        assert [item["rule"] for item in response.json()["detail"]["violations"]] == [expected]
        assert await count_rows(session, OrchestrationFlow) == 0
        assert await count_rows(session, OrchestrationAcceptedPlan) == 0
        assert await count_rows(session, OrchestrationWorkClaim) == 0
        assert len(reads) <= 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("case", "expected"),
        [
            ("missing", "connection_not_owned"),
            ("wrong_tenant", "connection_not_owned"),
            ("revoked", "connection_revoked"),
            ("provider_unavailable", "repository_verification_unavailable"),
            ("owner_unavailable", "repository_verification_unavailable"),
            ("invalid_issue", "repository_invalid"),
        ],
    )
    async def test_repository_refusals_through_direct_admission(self, session, app_with_router, monkeypatch, case, expected):
        from types import SimpleNamespace

        from src.admin.installations.resolver import OwnerState
        from src.orchestration import repository_binding
        from src.orchestration.accepted_node import accepted_node_contract
        from src.orchestration.models import OrchestrationWorkClaim
        from src.orchestration.work_admission import resolve_accepted_repository
        from src.orchestration.work_claims import WorkClaimError
        from src.shared.models.vault import ChannelTenantMap

        await seed_org(session, ORG_A, installation_ids=["11"])
        await seed_product(session, org_id=ORG_A, actor_id=USER_ID, product_id="product-1")
        connection = ChannelTenantMap(
            provider="github",
            provider_scope_id="111",
            org_id=ORG_A,
            installation_id="11",
            install_metadata={"account_id": "111"},
        )
        session.add(connection)
        await session.flush()
        state = {"case": "available"}

        async def owner(installation_id, *, db):
            if state["case"] == "owner_unavailable":
                raise TimeoutError("owner metadata unavailable")
            tenant_id = ORG_B if state["case"] == "wrong_tenant" else ORG_A
            owner_state = OwnerState.REVOKED if state["case"] == "revoked" else OwnerState.RESOLVED
            return SimpleNamespace(tenant_id=tenant_id, github_account_id="111"), owner_state

        async def read(_tenant, installation_id, name, repository_id, issue):
            if state["case"] == "provider_unavailable":
                raise TimeoutError("provider metadata unavailable")
            return {"id": 1001, "full_name": name}, {"account": {"id": 111}}, {"id": issue + 10000, "node_id": f"I_{issue}"}

        monkeypatch.setattr(repository_binding, "resolve_installation_owner", owner)
        monkeypatch.setattr(repository_binding, "_provider_read", read)
        proposal = valid_proposal(product_id="product-1")
        proposal.nodes[0].issue_ref = "42"
        proposal.nodes[0].repository_identity = "demo/repo-a"
        proposal.nodes[0].provider_repository_id = 1001
        proposal.nodes[0].repository_connection = RepositoryConnection(provider="github", installation_id=11)
        accepted = client_for(app_with_router, permitted=True).post(ROUTE, json=proposal.model_dump(mode="json"))
        assert accepted.status_code == 201, accepted.text
        assert "repository_execution_withheld" in causes_of(accepted.json())
        story = (await submitted_flow_nodes(session, accepted.json()["flow_id"]))[address("story-a")]
        state["case"] = case
        if case == "missing":
            await session.delete(connection)
            await session.flush()
        if case == "invalid_issue":
            story.issue_ref = "not-an-issue"
            await session.flush()
        with pytest.raises(WorkClaimError) as caught:
            await resolve_accepted_repository(session, node=story, contract=await accepted_node_contract(session, story), repo="demo/default")
        assert caught.value.code == expected
        assert await count_rows(session, OrchestrationWorkClaim) == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("case", "expected"),
        [
            ("issue_in_a", "repository_identity_mismatch"),
            ("issue_number_mismatch", "repository_identity_mismatch"),
            ("id_mismatch", "repository_identity_mismatch"),
            ("reused_name", "repository_identity_mismatch"),
            ("rename", None),
        ],
    )
    async def test_authenticated_issue_identity_through_acceptance(self, session, app_with_router, authenticated_story_provider, case, expected):
        from src.orchestration.models import OrchestrationWorkClaim
        from src.shared.models.vault import ChannelTenantMap

        await seed_org(session, ORG_A, installation_ids=["11"])
        await seed_product(session, org_id=ORG_A, actor_id=USER_ID, product_id="product-1")
        session.add(
            ChannelTenantMap(provider="github", provider_scope_id="111", org_id=ORG_A, installation_id="11", install_metadata={"account_id": "111"})
        )
        await session.flush()
        proposal = valid_proposal(product_id="product-1")
        proposal.nodes[0].issue_ref = "42"
        proposal.nodes[0].repository_identity = "demo/repo-b"
        proposal.nodes[0].provider_repository_id = 1002
        proposal.nodes[0].repository_connection = RepositoryConnection(provider="github", installation_id=11)
        authenticated_story_provider["case"] = case

        response = client_for(app_with_router, permitted=True).post(ROUTE, json=proposal.model_dump(mode="json"))

        if expected:
            assert response.status_code == 422, response.text
            assert [item["rule"] for item in response.json()["detail"]["violations"]] == [expected]
            assert await count_rows(session, OrchestrationAcceptedPlan) == 0
            assert await count_rows(session, OrchestrationWorkClaim) == 0
        else:
            assert response.status_code == 201, response.text
            assert "repository_execution_withheld" in causes_of(response.json())
            story = (await submitted_flow_nodes(session, response.json()["flow_id"]))[address("story-a")]
            plan = await OrchestrationRepository(session).get_accepted_plan(org_id=ORG_A, flow_id=response.json()["flow_id"])
            assert story.repository_identity == plan.plan_document["nodes"][0]["repository_identity"] == "demo/repo-b"
            assert story.provider_repository_id == plan.plan_document["nodes"][0]["provider_repository_id"] == 1002
            assert "/repos/demo/renamed/issues/42" in authenticated_story_provider["paths"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("case", "expected"),
        [
            ("issue_in_a", "repository_identity_mismatch"),
            ("reused_name", "repository_identity_mismatch"),
            ("projection_issue", "repository_binding_changed"),
            ("projection_name", "repository_binding_changed"),
            ("rename", None),
        ],
    )
    async def test_authenticated_issue_identity_through_direct_admission(
        self, session, app_with_router, authenticated_story_provider, case, expected
    ):
        from src.orchestration.accepted_node import accepted_node_contract
        from src.orchestration.models import OrchestrationWorkClaim
        from src.orchestration.work_admission import resolve_accepted_repository
        from src.orchestration.work_claims import WorkClaimError
        from src.shared.models.vault import ChannelTenantMap

        await seed_org(session, ORG_A, installation_ids=["11"])
        await seed_product(session, org_id=ORG_A, actor_id=USER_ID, product_id="product-1")
        session.add(
            ChannelTenantMap(provider="github", provider_scope_id="111", org_id=ORG_A, installation_id="11", install_metadata={"account_id": "111"})
        )
        await session.flush()
        proposal = valid_proposal(product_id="product-1")
        proposal.nodes[0].issue_ref = "42"
        proposal.nodes[0].repository_identity = "demo/repo-b"
        proposal.nodes[0].provider_repository_id = 1002
        proposal.nodes[0].repository_connection = RepositoryConnection(provider="github", installation_id=11)
        response = client_for(app_with_router, permitted=True).post(ROUTE, json=proposal.model_dump(mode="json"))
        assert response.status_code == 201, response.text
        story = (await submitted_flow_nodes(session, response.json()["flow_id"]))[address("story-a")]
        plan = await OrchestrationRepository(session).get_accepted_plan(org_id=ORG_A, flow_id=response.json()["flow_id"])
        authenticated_story_provider["case"] = case
        if case == "projection_issue":
            story.issue_ref = "43"
        if case == "projection_name":
            story.repository_identity = "demo/repo-a"
        await session.flush()
        contract = await accepted_node_contract(session, story)
        if expected:
            with pytest.raises(WorkClaimError) as caught:
                await resolve_accepted_repository(session, node=story, contract=contract, repo="demo/default")
            assert caught.value.code == expected
        else:
            assert await resolve_accepted_repository(session, node=story, contract=contract, repo="demo/default") == (11, 1002, "demo/renamed")
        assert plan.plan_document["nodes"][0]["issue_ref"] == "42"
        assert plan.plan_document["nodes"][0]["repository_identity"] == "demo/repo-b"
        assert await count_rows(session, OrchestrationWorkClaim) == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("change", "expected"),
        [
            ("other_connection", "repository_unavailable"),
            ("revoked", "connection_revoked"),
            ("reused", "repository_identity_mismatch"),
        ],
    )
    async def test_previewed_connection_change_refuses_at_route_and_admission_with_dispatch_held(
        self, session, app_with_router, monkeypatch, change, expected
    ):
        from types import SimpleNamespace

        from src.admin.installations.resolver import OwnerState
        from src.orchestration import repository_binding
        from src.orchestration.accepted_node import accepted_node_contract
        from src.orchestration.dispatch_pass import DispatchPassConfig, run_dispatch_pass
        from src.orchestration.models import OrchestrationWorkClaim
        from src.orchestration.planning import PlanningInputs, ResolvedRepository, plan_from_draft
        from src.orchestration.work_admission import resolve_accepted_repository
        from src.orchestration.work_claims import WorkClaimError
        from src.shared.models.vault import ChannelTenantMap

        await seed_org(session, ORG_A, installation_ids=["11", "22"])
        await seed_product(session, org_id=ORG_A, actor_id=USER_ID, product_id="product-1")
        for installation_id in (11, 22):
            session.add(
                ChannelTenantMap(
                    provider="github",
                    provider_scope_id=str(installation_id + 100),
                    org_id=ORG_A,
                    installation_id=str(installation_id),
                    install_metadata={"account_id": str(installation_id + 100)},
                )
            )
        await session.flush()
        authority = {11: 1001, 22: None}
        revoked = False
        reads = []

        async def owner(installation_id, *, db):
            if revoked and installation_id == 11:
                return None, OwnerState.REVOKED
            return SimpleNamespace(tenant_id=ORG_A, github_account_id=str(installation_id + 100)), OwnerState.RESOLVED

        async def read(_tenant, installation_id, name, repository_id, issue):
            reads.append(installation_id)
            if not authority[installation_id]:
                return None
            return (
                {"id": authority[installation_id], "full_name": name},
                {"account": {"id": installation_id + 100}},
                {"id": issue + 10000, "node_id": f"I_{issue}"},
            )

        monkeypatch.setattr(repository_binding, "resolve_installation_owner", owner)
        monkeypatch.setattr(repository_binding, "_provider_read", read)
        preview = await repository_binding.resolve_story_repository(
            session, tenant_id=ORG_A, name="demo/repo-a", issue=42, operation="preview", installation_id=11
        )
        proposal = plan_from_draft(
            PlanningInputs(
                flow_slug="preview-flow",
                title="Reviewed story",
                outcomes=("Ship story",),
                issue_ref="42",
                intent="Ship story",
                org_id=ORG_A,
                repository=ResolvedRepository(preview.full_name, preview.installation_id, True, preview.repository_id),
                last_activity_epoch=1800000000,
            )
        )
        proposal.product_id = "product-1"
        proposal.proposed_execution_policy = None
        assert proposal.nodes[0].repository_connection.installation_id == 11
        assert proposal.nodes[0].provider_repository_id == 1001
        client = client_for(app_with_router, permitted=True)

        def change_authority():
            nonlocal revoked
            if change == "other_connection":
                authority[11], authority[22] = None, 1001
            elif change == "revoked":
                revoked = True
            else:
                authority[11] = 2002

        change_authority()
        reads.clear()
        refused = client.post(ROUTE, json=proposal.model_dump(mode="json"))
        assert refused.status_code == 422, refused.text
        assert [item["rule"] for item in refused.json()["detail"]["violations"]] == [expected]
        assert reads == ([] if revoked else [11])
        assert await count_rows(session, OrchestrationAcceptedPlan) == 0
        authority[11], authority[22], revoked = 1001, None, False
        accepted = client.post(ROUTE, json=proposal.model_dump(mode="json"))
        assert accepted.status_code == 201, accepted.text
        assert "repository_execution_withheld" in causes_of(accepted.json())
        flow_id = accepted.json()["flow_id"]
        plan = await OrchestrationRepository(session).get_accepted_plan(org_id=ORG_A, flow_id=flow_id)
        story = next(node for node in (await submitted_flow_nodes(session, flow_id)).values() if node.kind == "story")
        assert plan.plan_document["nodes"][0]["repository_connection"]["installation_id"] == 11
        assert story.repository_connection["installation_id"] == 11
        change_authority()
        reads.clear()
        with pytest.raises(WorkClaimError) as caught:
            await resolve_accepted_repository(session, node=story, contract=await accepted_node_contract(session, story), repo="demo/default")
        assert caught.value.code == expected
        assert reads == ([] if revoked else [11])
        flow = await session.get(OrchestrationFlow, flow_id)
        flow.execution_paused = False
        story.state = NodeState.READY.value
        await session.flush()
        monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
        monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
        report = await run_dispatch_pass(
            session,
            DispatchPassConfig(queue_url="https://sqs.us-east-1.amazonaws.com/123456789012/test.fifo", repo="demo/default"),
        )
        assert report.dispatched == 0 and report.pending == [] and report.undispatchable == 1
        assert story.state == NodeState.READY.value and story.attempts == 0
        assert await count_rows(session, OrchestrationWorkClaim) == 0

    @pytest.mark.asyncio
    async def test_acceptance_and_admission_reject_a_resolver_that_switches_the_reviewed_installation(
        self, session, app_with_router, verified_repositories, monkeypatch
    ):
        from dataclasses import replace

        from src.orchestration import repository_binding
        from src.orchestration.accepted_node import accepted_node_contract
        from src.orchestration.models import OrchestrationWorkClaim
        from src.orchestration.work_admission import resolve_accepted_repository
        from src.orchestration.work_claims import WorkClaimError

        await seed_org(session, ORG_A, installation_ids=["11", "22"])
        await seed_product(session, org_id=ORG_A, actor_id=USER_ID, product_id="product-1")
        proposal = valid_proposal(product_id="product-1")
        proposal.nodes[0].issue_ref = "42"
        proposal.nodes[0].repository_identity = "demo/repo-a"
        proposal.nodes[0].provider_repository_id = 1001
        proposal.nodes[0].repository_connection = RepositoryConnection(provider="github", installation_id=11)
        switched = True
        resolve = repository_binding.resolve_story_repository

        async def inconsistent(*args, **kwargs):
            verified = await resolve(*args, **kwargs)
            return replace(verified, installation_id=22) if switched else verified

        monkeypatch.setattr(repository_binding, "resolve_story_repository", inconsistent)
        client = client_for(app_with_router, permitted=True)
        refused = client.post(ROUTE, json=proposal.model_dump(mode="json"))
        assert refused.status_code == 422, refused.text
        assert [item["rule"] for item in refused.json()["detail"]["violations"]] == ["repository_binding_changed"]
        assert await count_rows(session, OrchestrationAcceptedPlan) == 0
        switched = False
        accepted = client.post(ROUTE, json=proposal.model_dump(mode="json"))
        assert accepted.status_code == 201, accepted.text
        story = (await submitted_flow_nodes(session, accepted.json()["flow_id"]))[address("story-a")]
        assert story.repository_connection["installation_id"] == 11
        switched = True
        with pytest.raises(WorkClaimError) as caught:
            await resolve_accepted_repository(session, node=story, contract=await accepted_node_contract(session, story), repo="demo/default")
        assert caught.value.code == "repository_binding_changed"
        assert await count_rows(session, OrchestrationWorkClaim) == 0

    @pytest.mark.asyncio
    async def test_legacy_accepted_flow_reads_and_retries_without_changing_identity(self, session, app_with_router):
        from .test_compile import valid_proposal as literal_legacy_proposal

        await seed_org(session, ORG_A, installation_ids=["12345"])
        proposal = literal_legacy_proposal(product_id=None)
        document = proposal.model_dump(mode="json")
        assert "product_id" not in document
        assert document["nodes"][0]["repository_identity"] is None
        expected_hash = "445deca4c2b5f2db7954752501c3e5779d5fa2c36b29554fc2e705c2a7f5c587"
        client = client_for(app_with_router, permitted=True)

        await persist_legacy_plan(session, proposal, ApprovalContext(org_id=ORG_A, actor_id=USER_ID, actor_role="org_admin"))
        await session.commit()
        first = client.post(ROUTE, json=document)
        assert first.status_code == 200, first.text
        flow_id = first.json()["flow_id"]
        decision_id = first.json()["decision_id"]
        assert first.json()["product_id"] is None
        assert first.json()["plan_hash"] == expected_hash
        assert "repository_verification_pending" not in causes_of(first.json())
        repository = OrchestrationRepository(session)
        plan = await repository.get_accepted_plan(org_id=ORG_A, flow_id=flow_id)
        plan_id = plan.id
        original_bytes = json.dumps(plan.plan_document, sort_keys=True, separators=(",", ":")).encode()
        story_id = (await submitted_flow_nodes(session, flow_id))["demo-flow/epic-1/wave-1/story-a"].id
        (await session.get(OrchestrationNode, story_id)).attempts = 2
        await session.commit()
        session.expire_all()

        detail = client.get(f"{ROUTE}/{flow_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["product_id"] is None
        story = next(item for item in detail.json()["nodes"] if item["id"] == story_id)
        assert story["attempts"] == 2
        assert (story["repository_identity"], story["provider_repository_id"], story["repository_connection"]) == (None, None, None)
        summaries = client.get(ROUTE)
        assert summaries.status_code == 200, summaries.text
        assert next(item for item in summaries.json()["flows"] if item["id"] == flow_id)["product_id"] is None
        versions = client.get(f"{ROUTE}/{flow_id}/plans")
        assert versions.status_code == 200, versions.text
        assert versions.json()[0]["plan_hash"] == expected_hash
        assert json.dumps(versions.json()[0]["plan_document"], sort_keys=True, separators=(",", ":")).encode() == original_bytes

        retry = client.post(ROUTE, json=document)
        assert retry.status_code == 200, retry.text
        assert (retry.json()["flow_id"], retry.json()["decision_id"], retry.json()["plan_hash"], retry.json()["product_id"]) == (
            flow_id,
            decision_id,
            expected_hash,
            None,
        )
        current = await repository.get_accepted_plan(org_id=ORG_A, flow_id=flow_id)
        assert current.id == plan_id
        assert json.dumps(current.plan_document, sort_keys=True, separators=(",", ":")).encode() == original_bytes
        assert (await session.get(OrchestrationNode, story_id)).attempts == 2
        assert await count_rows(session, OrchestrationAcceptedPlan) == 1
        assert await count_rows(session, OrchestrationDecision) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("product_id", [None, "missing", "inaccessible"])
    async def test_repository_bound_submission_rejects_invalid_product(self, session, app_with_router, product_id):
        await seed_org(session, ORG_A, installation_ids=["12345"])
        if product_id == "inaccessible":
            await seed_product_membership(session, org_id=ORG_A, user_id="someone-else")
            session.add(
                Product(
                    team_id=f"product-team-{ORG_A}",
                    id=product_id,
                    tenant_id=ORG_A,
                    name="Other",
                    purpose="Other",
                    intended_users=["team"],
                    owner_user_id="someone-else",
                    starting_point="running_product",
                    created_by="someone-else",
                )
            )
            await session.flush()
        proposal = valid_proposal(product_id=product_id)
        proposal.nodes[0].repository_identity = "demo/repo-a"
        proposal.nodes[0].provider_repository_id = 1001

        response = client_for(app_with_router, permitted=True).post(ROUTE, json=proposal.model_dump(mode="json"))

        assert response.status_code == 422, response.text
        assert await count_rows(session, OrchestrationFlow) == 0
        assert await count_rows(session, OrchestrationAcceptedPlan) == 0

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

        app_with_router.dependency_overrides[get_current_user] = lambda: _token_context(ORG_B, user_id="org-b-actor")
        await seed_product(session, org_id=ORG_B, actor_id="org-b-actor")
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
    async def test_a_fully_routed_plan_is_dispatchable(self, session, app_with_router, verified_repositories):
        """No false positives: a plan the engine would dispatch is reported clean.

        The load-bearing direction of this test is the *negative* space. Every
        cause added to this route is a chance to flag a healthy plan, and a route
        that reported everything blocked would be as useless as one that reported
        nothing — the operator would learn to ignore it.
        """
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        proposal = routable_proposal()
        story = proposal.nodes[0]
        story.repository_identity = "demo/repo-a"
        story.provider_repository_id = 1001
        story.repository_connection = RepositoryConnection(provider="github", installation_id=12345)
        await persist_legacy_plan(session, proposal, ApprovalContext(org_id=ORG_A, actor_id=USER_ID, actor_role="org_admin"))
        await session.commit()
        body = client.post(ROUTE, json=proposal.model_dump(mode="json")).json()

        assert body["dispatchable"] is True
        assert body["dispatch_blocked_reason"] is None
        assert body["dispatch_blocked_causes"] == []

    @pytest.mark.asyncio
    async def test_a_gate_without_an_issue_ref_is_not_flagged(self, session, app_with_router):
        """Gates are presented by the tick and never consume a worker.

        `routable_proposal` carries a gate with no `issue_ref` and is asserted
        dispatchable above; this pins the reason why, so a future tightening of the
        issue-routing rule to "every node" fails here rather than reporting every
        real plan blocked.
        """
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        await persist_legacy_plan(session, routable_proposal(), ApprovalContext(org_id=ORG_A, actor_id=USER_ID, actor_role="org_admin"))
        await session.commit()
        flow_id = client.post(ROUTE, json=routable_proposal().model_dump(mode="json")).json()["flow_id"]

        nodes = await submitted_flow_nodes(session, flow_id)
        gate = nodes[address("gate")]
        assert gate.issue_ref is None, "the fixture must actually exercise a gate with no issue"

    @pytest.mark.asyncio
    async def test_a_story_without_an_issue_ref_is_reported(self, session, app_with_router):
        """The precondition that was previously reported by nothing.

        The engine checks this one FIRST, so a plan tripping it never dispatched
        and the submitter was told only about the installation — or, when the
        installation was fine, nothing at all.
        """
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        proposal = routable_proposal()
        proposal.nodes[0].issue_ref = None

        await persist_legacy_plan(session, proposal, ApprovalContext(org_id=ORG_A, actor_id=USER_ID, actor_role="org_admin"))
        await session.commit()
        body = client.post(ROUTE, json=proposal.model_dump(mode="json")).json()

        assert body["dispatchable"] is False
        assert causes_of(body) == {"legacy_binding_unresolved", "missing_issue_ref"}
        assert "story-a" in body["dispatch_blocked_reason"]

    @pytest.mark.asyncio
    async def test_an_eval_without_an_issue_ref_is_reported(self, session, app_with_router):
        """Evaluations dispatch to an issue exactly as stories do.

        `_fetch_ready_nodes` selects both kinds, so an eval with no issue stalls
        identically. Reporting only stories would leave the eval half of every
        wave silently undeliverable.
        """
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        proposal = routable_proposal()
        proposal.nodes[1].issue_ref = None

        await persist_legacy_plan(session, proposal, ApprovalContext(org_id=ORG_A, actor_id=USER_ID, actor_role="org_admin"))
        await session.commit()
        body = client.post(ROUTE, json=proposal.model_dump(mode="json")).json()

        assert body["dispatchable"] is False
        assert causes_of(body) == {"legacy_binding_unresolved", "missing_issue_ref"}
        assert "eval" in body["dispatch_blocked_reason"]

    @pytest.mark.asyncio
    async def test_a_malformed_issue_ref_is_reported_distinctly(self, session, app_with_router):
        """A separate cause from a missing one: it needs a different fix.

        Missing means nobody has filed the issue yet. Malformed means the plan
        document names something that is not an issue number, which will never
        resolve itself no matter how long the operator waits.
        """
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        proposal = routable_proposal()
        proposal.nodes[0].issue_ref = "not-an-issue"

        await persist_legacy_plan(session, proposal, ApprovalContext(org_id=ORG_A, actor_id=USER_ID, actor_role="org_admin"))
        await session.commit()
        body = client.post(ROUTE, json=proposal.model_dump(mode="json")).json()

        assert body["dispatchable"] is False
        assert causes_of(body) == {"legacy_binding_unresolved", "malformed_issue_ref"}

    @pytest.mark.asyncio
    async def test_all_routing_blockers_are_reported_together(self, session, app_with_router):
        """The substance of #4334: causes are enumerated, not short-circuited.

        A plan tripping three preconditions and told one sends the operator to fix
        that one, resubmit, and get the same silent nothing — which is the bug
        being fixed, reappearing one cause later.
        """
        await seed_org(session, ORG_A, installation_ids=["12345", "67890"])
        client = client_for(app_with_router, permitted=True)

        proposal = routable_proposal()
        proposal.nodes[0].issue_ref = None
        proposal.nodes[1].issue_ref = "not-an-issue"

        await persist_legacy_plan(session, proposal, ApprovalContext(org_id=ORG_A, actor_id=USER_ID, actor_role="org_admin"))
        await session.commit()
        body = client.post(ROUTE, json=proposal.model_dump(mode="json")).json()

        assert body["dispatchable"] is False
        assert causes_of(body) == {"legacy_binding_unresolved", "missing_issue_ref", "malformed_issue_ref", "ambiguous_installation"}
        reason = body["dispatch_blocked_reason"]
        assert "exactly one GitHub installation" in reason
        assert "story-a" in reason and "eval" in reason

    @pytest.mark.asyncio
    async def test_an_unconfigured_dispatch_repo_is_reported_as_unknown(self, session, app_with_router, monkeypatch):
        """A gateway with no dispatch repository cannot confirm the plan is routable.

        Reported as `unknown_dispatch_repo` rather than a confirmed block: the
        gateway pod and the scheduled tick read this setting from separate places,
        so this process's view is evidence about the gateway and not proof about
        the deployed tick. Claiming the tick is broken would be a guess; claiming
        the plan is fine would hide a real cause.
        """
        monkeypatch.delenv("BG_ORCH_DISPATCH_REPO", raising=False)
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        await persist_legacy_plan(session, routable_proposal(), ApprovalContext(org_id=ORG_A, actor_id=USER_ID, actor_role="org_admin"))
        await session.commit()
        body = client.post(ROUTE, json=routable_proposal().model_dump(mode="json")).json()

        assert body["dispatchable"] is False
        assert causes_of(body) == {"legacy_binding_unresolved"}

    @pytest.mark.asyncio
    async def test_the_disabled_sentinel_counts_as_unconfigured(self, session, app_with_router, monkeypatch):
        """Terraform writes the literal "disabled" when no repo is set.

        `agent-authority-coordinator.tf` cannot write an empty SSM SecureString, so
        the unconfigured state reaches the pod as this string. Treating it as a
        configured repository named "disabled" would report every plan dispatchable
        on exactly the environments where none is.
        """
        monkeypatch.setenv("BG_ORCH_DISPATCH_REPO", "disabled")
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        await persist_legacy_plan(session, routable_proposal(), ApprovalContext(org_id=ORG_A, actor_id=USER_ID, actor_role="org_admin"))
        await session.commit()
        body = client.post(ROUTE, json=routable_proposal().model_dump(mode="json")).json()

        assert causes_of(body) == {"legacy_binding_unresolved"}

    @pytest.mark.asyncio
    async def test_a_cause_detail_names_only_the_submitters_own_nodes(self, session, app_with_router):
        """Tenant isolation of the reason string itself.

        The detail must identify the blocked node well enough to fix it while
        carrying nothing from outside the caller's own flow. `node_ref` is a
        tenant-local address; a node id or an installation id would not be.
        """
        await seed_org(session, ORG_A, installation_ids=["12345", "67890"])
        client = client_for(app_with_router, permitted=True)

        proposal = routable_proposal()
        proposal.nodes[0].issue_ref = None
        await persist_legacy_plan(session, proposal, ApprovalContext(org_id=ORG_A, actor_id=USER_ID, actor_role="org_admin"))
        await session.commit()
        body = client.post(ROUTE, json=proposal.model_dump(mode="json")).json()

        details = " ".join(cause["detail"] for cause in body["dispatch_blocked_causes"])
        assert "12345" not in details and "67890" not in details, "installation ids must not appear in a reason string"

    @pytest.mark.asyncio
    async def test_the_route_agrees_with_the_dispatch_guard_on_the_same_node(self, session, app_with_router):
        """Behavioural proof that the predicate is shared, not merely similar.

        The structural `is` assertion below proves the route imports the engine's
        function. This proves the two produce the same verdict on real committed
        rows: for every node of a submitted flow, the route's per-node cause and
        `routing_blocker_for_node` — the function `_dispatch_one_unclaimed` itself
        branches on — must agree. If they ever diverged, the route would promise
        dispatchable for a plan the engine silently refuses.
        """
        from src.orchestration.dispatch_pass import RoutingBlocker, routing_blocker_for_node

        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        proposal = routable_proposal()
        proposal.nodes[0].issue_ref = None  # story: missing
        proposal.nodes[1].issue_ref = "not-an-issue"  # eval: malformed
        await persist_legacy_plan(session, proposal, ApprovalContext(org_id=ORG_A, actor_id=USER_ID, actor_role="org_admin"))
        await session.commit()
        body = client.post(ROUTE, json=proposal.model_dump(mode="json")).json()

        nodes = await submitted_flow_nodes(session, body["flow_id"])
        blockers = {routing_blocker_for_node(kind=n.kind, issue_ref=n.issue_ref) for n in nodes.values()}

        # The gate contributes None; the story and eval contribute one each.
        assert blockers == {None, RoutingBlocker.MISSING_ISSUE_REF, RoutingBlocker.MALFORMED_ISSUE_REF}
        assert causes_of(body) >= {b.value for b in blockers if b is not None}, "the route must report exactly what the engine's guard refuses"

    @pytest.mark.asyncio
    async def test_exactly_one_installation_does_not_resolve_legacy_repository(self, session, app_with_router):
        await seed_org(session, ORG_A, installation_ids=["12345"])
        client = client_for(app_with_router, permitted=True)

        await persist_legacy_plan(session, routable_proposal(), ApprovalContext(org_id=ORG_A, actor_id=USER_ID, actor_role="org_admin"))
        await session.commit()
        body = client.post(ROUTE, json=routable_proposal().model_dump(mode="json")).json()

        assert body["dispatchable"] is False
        assert causes_of(body) == {"legacy_binding_unresolved"}

    @pytest.mark.asyncio
    async def test_no_installation_is_reported_undispatchable(self, session, app_with_router):
        await seed_org(session, ORG_A, installation_ids=[])
        client = client_for(app_with_router, permitted=True)

        await persist_legacy_plan(session, routable_proposal(), ApprovalContext(org_id=ORG_A, actor_id=USER_ID, actor_role="org_admin"))
        await session.commit()
        body = client.post(ROUTE, json=routable_proposal().model_dump(mode="json")).json()

        assert body["dispatchable"] is False
        assert "exactly one GitHub installation" in body["dispatch_blocked_reason"]
        assert causes_of(body) == {"legacy_binding_unresolved", "ambiguous_installation"}

    @pytest.mark.asyncio
    async def test_two_installations_are_reported_undispatchable(self, session, app_with_router):
        """More than one is as fail-closed as zero: guessing would dispatch into a
        repository nobody asked for."""
        await seed_org(session, ORG_A, installation_ids=["12345", "67890"])
        client = client_for(app_with_router, permitted=True)

        await persist_legacy_plan(session, routable_proposal(), ApprovalContext(org_id=ORG_A, actor_id=USER_ID, actor_role="org_admin"))
        await session.commit()
        body = client.post(ROUTE, json=routable_proposal().model_dump(mode="json")).json()

        assert body["dispatchable"] is False
        assert "exactly one GitHub installation" in body["dispatch_blocked_reason"]
        assert causes_of(body) == {"legacy_binding_unresolved", "ambiguous_installation"}

    @pytest.mark.asyncio
    async def test_the_installation_reason_wording_is_unchanged(self, session, app_with_router):
        """#4334's named regression check: the pre-existing string is preserved.

        Asserted by equality, not substring. This response field predates the
        multi-cause change and anything already matching on its wording — a
        dashboard, a log query, the wave-4 evaluation harness — must be unaffected
        by causes being added around it.
        """
        await seed_org(session, ORG_A, installation_ids=[])
        client = client_for(app_with_router, permitted=True)

        await persist_legacy_plan(session, routable_proposal(), ApprovalContext(org_id=ORG_A, actor_id=USER_ID, actor_role="org_admin"))
        await session.commit()
        body = client.post(ROUTE, json=routable_proposal().model_dump(mode="json")).json()

        assert body["dispatch_blocked_reason"].endswith(
            f"org {ORG_A!r} does not resolve to exactly one GitHub installation, so no node in this flow can be "
            "dispatched; the engine will count every node undispatchable until exactly one installation is configured"
        )

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
    async def test_a_plan_blocked_on_every_cause_still_commits_with_201(self, session, app_with_router):
        """The report-not-rejection contract, at the worst case it has to hold for.

        A plan tripping every precondition is still a validly approved plan whose
        rows are exactly what a correct submission produces. Turning the fullest
        report into a refusal would present an operational data problem as a
        malformed document — the second bug class #4334's impact table names.
        """
        await seed_org(session, ORG_A, installation_ids=["12345", "67890"])
        client = client_for(app_with_router, permitted=True)

        proposal = routable_proposal(product_id=f"product-{ORG_A}")
        proposal.nodes[0].issue_ref = None
        proposal.nodes[1].issue_ref = "not-an-issue"

        response = client.post(ROUTE, json=proposal.model_dump(mode="json"))

        assert response.status_code == 201, "a fully-blocked plan is reported, never rejected"
        assert response.json()["dispatchable"] is False
        assert await count_rows(session, OrchestrationFlow) == 1
        assert await count_rows(session, OrchestrationNode) == 3

    @pytest.mark.asyncio
    async def test_an_undispatchable_resubmission_is_still_200(self, session, app_with_router):
        """Idempotency is unaffected by the cause enumeration.

        The causes are computed after the commit on both paths, so a resubmission
        reports the same blockers while still creating nothing — a client must not
        see a retry of a blocked plan as a fresh 201.
        """
        await seed_org(session, ORG_A, installation_ids=[])
        client = client_for(app_with_router, permitted=True)
        payload = routable_proposal(product_id=f"product-{ORG_A}").model_dump(mode="json")

        first = client.post(ROUTE, json=payload)
        second = client.post(ROUTE, json=payload)

        assert first.status_code == 201
        assert second.status_code == 200
        assert second.json()["already_compiled"] is True
        assert causes_of(second.json()) == causes_of(first.json()), "a resubmission must report the same blockers"

    @pytest.mark.asyncio
    async def test_the_route_uses_the_same_resolver_as_dispatch(self):
        """Structural, not behavioural.

        If the route computed dispatchability its own way, the two would drift — and
        the drift would be invisible in the worst direction: the route promising a
        plan is dispatchable that dispatch then silently refuses.

        Both preconditions are asserted the same way. `routing_blocker_for_node` is
        the function `_dispatch_one_unclaimed` itself branches on, so an `is` match
        here means the route cannot be evaluating a copy of the rule.
        """
        from src.orchestration import routes as routes_module
        from src.orchestration.dispatch_pass import resolve_installation_id, routing_blocker_for_node

        assert routes_module.resolve_installation_id is resolve_installation_id
        assert routes_module.routing_blocker_for_node is routing_blocker_for_node


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
            f"/orchestration/flows/{flow_id}/amendments",
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

        response = client.get(f"/orchestration/flows/{flow_id}/plans")

        assert response.status_code == 200, response.text
        versions = response.json()
        assert len(versions) == 1
        assert versions[0]["version"] == 1
        assert {node["address"] for node in versions[0]["plan_document"]["nodes"]} == {address("story-a"), address("eval")}


@pytest.mark.asyncio
async def test_readiness_does_not_require_global_for_verified_product_story(session, app_with_router, verified_repositories, monkeypatch):
    monkeypatch.setenv("ADP_PRODUCT_STORY_DISPATCH_ENABLED", "true")
    monkeypatch.delenv("BG_ORCH_DISPATCH_REPO", raising=False)
    await seed_org(session, ORG_A, installation_ids=["12345", "901"])
    await seed_product(session, org_id=ORG_A, actor_id=USER_ID, product_id="product-1")
    proposal = valid_proposal(product_id="product-1")
    story = proposal.nodes[0]
    story.issue_ref = "42"
    story.repository_identity = "demo/repo-a"
    story.provider_repository_id = 1001
    story.repository_connection = RepositoryConnection(provider="github", installation_id=12345)
    proposal.nodes[1].issue_ref = "43"

    response = client_for(app_with_router, permitted=True).post(ROUTE, json=proposal.model_dump(mode="json"))
    assert response.status_code == 201, response.text
    assert causes_of(response.json()) == set()
    assert response.json()["dispatchable"] is True
