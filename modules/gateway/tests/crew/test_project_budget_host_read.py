"""Dev Box budget reads run with the verified human owner's visibility."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from src.auth.dependencies import get_current_user
from src.crew.host_routes import HOST_RUN_ROUTE_PATHS
from src.products import budget_routes
from src.proxy.routes import set_agent_run_id_from_header
from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext


@pytest.fixture
def host_app(monkeypatch):
    app = FastAPI()
    app.include_router(budget_routes.router)
    identity = TokenContext(
        user_id="instance-role",
        org_id="platform",
        team_id="",
        department_id="",
        account_type="service",
        auth_source="iam",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    app.dependency_overrides[get_current_user] = lambda: identity
    app.dependency_overrides[get_db] = lambda: object()
    app.dependency_overrides[set_agent_run_id_from_header] = lambda: "host-run"
    budget_view = AsyncMock(return_value={"product_id": "product-a", "project_id": "project-a", "budget_usd": None, "flows": []})
    host_target = AsyncMock(return_value=(SimpleNamespace(tenant_id="tenant-a", user_id="owner-a"), object()))
    monkeypatch.setattr(budget_routes, "budget_view", budget_view)
    monkeypatch.setattr(budget_routes, "current_host_target", host_target)
    return app, budget_view, host_target


async def test_host_read_uses_verified_owner_scope(host_app):
    app, budget_view, host_target = host_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/projects/project-a/budget")

    assert response.status_code == 200
    assert response.json()["budget_usd"] is None
    assert response.headers["cache-control"] == "no-store"
    assert host_target.await_args.args[:2] == (app.dependency_overrides[get_current_user](), "host-run")
    actor = budget_view.await_args.args[1]
    assert (actor.org_id, actor.user_id, actor.account_type, actor.is_admin) == ("tenant-a", "owner-a", "human", False)
    assert budget_view.await_args.args[2] == "project-a"
    assert "/v1/projects/{project_id}/budget" in HOST_RUN_ROUTE_PATHS
    route = next(route for route in app.routes if route.path == "/v1/projects/{project_id}/budget")
    assert set_agent_run_id_from_header in {dependency.call for dependency in route.dependant.dependencies}


async def test_host_without_live_owner_target_is_denied(host_app):
    app, budget_view, host_target = host_app
    host_target.side_effect = HTTPException(403, "Host run is not authorized")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/projects/project-a/budget")

    assert response.status_code == 403
    budget_view.assert_not_awaited()


async def test_host_read_registration_does_not_allow_budget_writes(host_app):
    app, budget_view, host_target = host_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put("/v1/projects/project-a/budget", json={"budget_usd": "250.00"})

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "access_denied"
    budget_view.assert_not_awaited()
    host_target.assert_not_awaited()


@pytest.fixture
def authenticated_host_app(monkeypatch):
    app = FastAPI()
    app.include_router(budget_routes.router)
    app.dependency_overrides[get_db] = lambda: object()
    instance = "i-00000000000000001"
    role = "arn:aws:iam::000000000000:role/adp-dev-box-guest-test"
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_apigw_headers", True)
    monkeypatch.setattr(settings, "apigw_provenance_secret", "synthetic-edge-proof")
    monkeypatch.setattr("src.auth.dependencies.get_settings", lambda: settings)
    monkeypatch.setattr("src.auth.caller_provenance.get_settings", lambda: settings)
    registry = Mock()
    registry.get_agent_by_role_arn.return_value = {
        "agent_id": "test-host",
        "org_id": "platform",
        "team_id": "",
        "role_arn": role,
        "scope": "internal",
        "requires_host_identity": True,
    }
    monkeypatch.setattr("src.auth.agent_registry.get_agent_registry_service", lambda: registry)
    row = {
        "kind": "crew",
        "target_kind": "ec2-instance",
        "status": "active",
        "executor_scope": "session",
        "is_human_rooted": True,
        "root_human_id": "owner-a",
        "user_id": "owner-a",
        "tenant_id": "tenant-a",
        "target_ref": instance,
        "host_role_arn": role,
    }
    routing_run = AsyncMock(return_value=row)
    monkeypatch.setattr("src.proxy.bedrock_principal.read_routing_run", routing_run)
    store = SimpleNamespace(read_run=Mock(return_value=row))
    monkeypatch.setattr("src.agentauth.chat_crew_run.current_crew_store", lambda: store)
    target = SimpleNamespace(host_run_id="host-run", host_instance_ref=instance)
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=target))
    monkeypatch.setattr("src.crew.proxy.get_target_resolver", lambda: resolver)
    view = AsyncMock(return_value={"product_id": "product-a", "project_id": "project-a", "budget_usd": None, "flows": []})
    monkeypatch.setattr(budget_routes, "budget_view", view)
    headers = {
        "X-Caller-Identity": f"arn:aws:sts::000000000000:assumed-role/adp-dev-box-guest-test/{instance}",
        "X-Adp-Edge-Provenance": "synthetic-edge-proof",
        "X-Agent-RunId": "host-run",
    }
    return SimpleNamespace(
        app=app,
        headers=headers,
        registry=registry,
        routing_run=routing_run,
        store=store,
        target=target,
        resolver=resolver,
        view=view,
    )


async def test_real_host_dependencies_share_the_verified_context(authenticated_host_app):
    host = authenticated_host_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host.app), base_url="http://test") as client:
        response = await client.get("/v1/projects/project-a/budget", headers=host.headers)

    assert response.status_code == 200, response.text
    host.registry.get_agent_by_role_arn.assert_called_once_with("arn:aws:iam::000000000000:role/adp-dev-box-guest-test")
    host.routing_run.assert_awaited_once_with("host-run")
    host.store.read_run.assert_called_once_with("host-run")
    owner = host.resolver.resolve.await_args.args[0]
    assert (owner.tenant_id, owner.user_id) == ("tenant-a", "owner-a")
    actor = host.view.await_args.args[1]
    assert (actor.org_id, actor.user_id, actor.account_type, actor.is_admin) == ("tenant-a", "owner-a", "human", False)
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("invalid", ["missing_run", "foreign_instance", "missing_proof", "unregistered", "stale_target"])
async def test_real_host_dependencies_reject_unverified_hosts(authenticated_host_app, invalid):
    host = authenticated_host_app
    if invalid == "missing_run":
        host.headers.pop("X-Agent-RunId")
    elif invalid == "foreign_instance":
        host.headers["X-Caller-Identity"] = host.headers["X-Caller-Identity"].replace("i-00000000000000001", "i-00000000000000002")
    elif invalid == "missing_proof":
        host.headers.pop("X-Adp-Edge-Provenance")
    elif invalid == "unregistered":
        host.registry.get_agent_by_role_arn.return_value = None
    else:
        host.target.host_run_id = "replacement-run"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host.app), base_url="http://test") as client:
        response = await client.get("/v1/projects/project-a/budget", headers=host.headers)

    assert response.status_code == (401 if invalid == "missing_proof" else 403), response.text
    host.view.assert_not_awaited()
