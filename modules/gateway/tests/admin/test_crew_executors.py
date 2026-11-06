"""Inventory listing uses the authenticated tenant and real membership roles."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.admin.routes import router
from src.auth.dependencies import get_current_user
from src.crew.inventory import CrewExecutorInventory
from src.crew.proxy import get_target_resolver
from src.crew.proxy import router as crew_router
from src.crew.targets import CrewLeaseFencedError
from src.shared.database import get_db
from src.shared.exceptions import BedrockGatewayError
from src.shared.models.base import Base
from src.shared.models.crew_executor import CrewExecutor
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.schemas.auth import TokenContext


@pytest.fixture
async def inventory_client():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        for tenant_id in ("tenant-a", "tenant-b"):
            session.add(Organization(id=tenant_id, name=tenant_id))
            session.add(User(id=f"admin-{tenant_id}", org_id=tenant_id, team_id="", name=tenant_id, email=f"{tenant_id}@example.test"))
            session.add(TenantMembership(user_id=f"admin-{tenant_id}", tenant_id=tenant_id, role="org_admin", is_active=True))
    inventory = CrewExecutorInventory(factory)
    await inventory.ensure(tenant_id="tenant-a", user_id="owner-a")
    await inventory.ensure(tenant_id="tenant-b", user_id="owner-b")
    caller = {"tenant": "tenant-b"}
    app = FastAPI()
    app.include_router(router)
    app.include_router(crew_router)

    class FencedResolver:
        async def resolve(self, owner, *, now):
            raise CrewLeaseFencedError("lease_fenced")

    app.dependency_overrides[get_target_resolver] = lambda: FencedResolver()

    @app.exception_handler(BedrockGatewayError)
    async def handle_error(request: Request, exc: BedrockGatewayError):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.error})

    async def current_user():
        return TokenContext(
            user_id=f"admin-{caller['tenant']}",
            org_id=caller["tenant"],
            team_id="",
            department_id="",
            account_type="human",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    async def database():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_db] = database
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, caller, factory
    await engine.dispose()


async def test_admin_list_only_selected_tenant(inventory_client):
    client, caller, _ = inventory_client
    response = await client.get("/admin/crew-executors")
    assert response.status_code == 200
    assert [(item["tenant_id"], item["user_id"], item["generation"]) for item in response.json()] == [("tenant-b", "owner-b", 1)]
    caller["tenant"] = "tenant-a"
    response = await client.get("/admin/crew-executors")
    assert response.status_code == 200
    assert [(item["tenant_id"], item["user_id"]) for item in response.json()] == [("tenant-a", "owner-a")]


async def test_cross_tenant_query_cannot_enumerate_and_ownerless_row_is_rejected(inventory_client):
    client, _, factory = inventory_client
    response = await client.get("/admin/crew-executors?tenant_id=tenant-a&offset=0&limit=1")
    assert response.status_code == 200
    assert [item["tenant_id"] for item in response.json()] == ["tenant-b"]
    with pytest.raises(IntegrityError):
        async with factory.begin() as session:
            session.add(CrewExecutor(tenant_id=None, user_id="orphan", state="ready", generation=1))


async def test_non_admin_cannot_list_executors(inventory_client):
    client, _, factory = inventory_client
    async with factory.begin() as session:
        await session.execute(update(TenantMembership).where(TenantMembership.tenant_id == "tenant-b").values(role="member"))
    response = await client.get("/admin/crew-executors")
    assert response.status_code == 403


async def test_revoked_member_cannot_resolve_crew_target(inventory_client):
    client, _, factory = inventory_client
    assert (await client.get("/crew/target")).status_code == 409
    async with factory.begin() as session:
        await session.execute(update(TenantMembership).where(TenantMembership.tenant_id == "tenant-b").values(revoked_at=datetime.now(UTC)))
    response = await client.get("/crew/target")
    assert response.status_code == 404
    assert response.json() == {
        "detail": {"error": "crew_scope_refused", "reason": "chat_principal_not_tenant_member", "detail": "no member row in tenant tenant-b"}
    }
