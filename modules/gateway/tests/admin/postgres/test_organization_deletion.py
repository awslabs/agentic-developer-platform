"""Issue #5033: exercise both deletion routes on the actual migrated PG schema.

No production services are contacted. Authentication supplies a test principal;
route authorization and SQL are real. Cognito/DynamoDB collaborators are mocked.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.admin.access_control import AccessControl
from src.admin.identity import router as identity_routes
from src.admin.identity.organizations_service import OrganizationsService
from src.admin.org_members import add_user_to_org
from src.admin.routes import get_access_control, get_admin_service, router
from src.admin.service import AdminService
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.exceptions import BedrockGatewayError
from src.shared.models.onboarding import Tenant, TenantMembership
from src.shared.models.organization import Organization, Team, TeamMembership, User
from src.shared.models.usage import UsageLog
from src.shared.models.vault import ChannelTenantMap, UserCredential, UserIdentity
from src.shared.schemas.auth import TokenContext
from tests.migrations.conftest_postgres import to_async_url, upgrade


@pytest.fixture
async def sessions(pg_url):
    upgrade(pg_url)
    engine = create_async_engine(to_async_url(pg_url))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def seeded(sessions):
    """Canonical login in home, real org-placement write into target, third org."""
    async with sessions() as db:
        db.add_all(
            [
                Organization(id=org, name=org, github_installation_ids=[f"{org}-installation"], cognito_client_ids=[f"{org}-client"])
                for org in ("home", "target", "third", "empty")
            ]
        )
        await db.flush()
        db.add_all([Team(id=f"{org}-team", org_id=org, department_id=f"{org}-dept", name="Default") for org in ("home", "target")])
        db.add(
            User(id="canonical", org_id="home", team_id="home-team", email="test@example.invalid", cognito_sub="login-sub", cognito_username="login")
        )
        await db.flush()
        db.add(TeamMembership(id="home-team-membership", user_id="canonical", org_id="home", team_id="home-team", is_primary=True))
        db.add_all(
            [
                ChannelTenantMap(id=f"channel-{org}", org_id=org, provider="github", provider_scope_id=f"{org}-installation")
                for org in ("home", "target")
            ]
        )
        db.add(TenantMembership(id="home-membership", user_id="canonical", tenant_id="home", role="org_admin", is_active=True))
        db.add(TenantMembership(id="third-membership", user_id="canonical", tenant_id="third", role="member", is_active=False))
        db.add(
            UserIdentity(
                id="github-home",
                user_id="canonical",
                org_id="home",
                team_id="home-team",
                provider="github",
                provider_user_id="123",
                verification_method="oauth",
                is_primary=True,
            )
        )
        await db.flush()
        placement = await add_user_to_org(db, user_id="canonical", org_id="target")
        db.add(TeamMembership(id="target-team-membership", user_id=placement.id, org_id="target", team_id="target-team", is_primary=True))
        placement.team_id = "target-team"
        for user in (await db.get(User, "canonical"), placement):
            db.add(
                UserCredential(
                    id=f"cred-{user.org_id}",
                    org_id=user.org_id,
                    user_id=user.id,
                    service="test",
                    credential_type="api_key",
                    label="test",
                    secret_arn=f"arn:aws:secretsmanager:us-east-1:000000000000:secret:test-{user.org_id}",
                )
            )
            db.add(
                UsageLog(
                    id=f"history-{user.org_id}",
                    org_id=user.org_id,
                    user_id=user.id,
                    team_id=user.team_id,
                    department_id="dept",
                    model="test",
                    input_tokens=1,
                    output_tokens=1,
                    cost_usd=Decimal("0.01"),
                    latency_ms=1,
                    status_code=200,
                )
            )
        await db.commit()
        return placement.id


def principal(*, admin=True):
    return TokenContext(
        user_id="login-sub",
        org_id="home",
        team_id="home-team",
        department_id="home-dept",
        account_type="human",
        is_admin=admin,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def make_app(db, monkeypatch, *, admin=True):
    index = AsyncMock()
    cognito = AsyncMock()
    app = FastAPI()
    app.include_router(router)
    app.include_router(identity_routes.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: principal(admin=admin)
    app.dependency_overrides[get_admin_service] = lambda: AdminService(db, identity_index=index)
    app.dependency_overrides[get_access_control] = lambda: AccessControl(db)
    monkeypatch.setattr(
        identity_routes, "OrganizationsService", lambda session: OrganizationsService(session, identity_index=index, cognito_sync=cognito)
    )

    @app.exception_handler(BedrockGatewayError)
    async def gateway_error_handler(request: Request, exc: BedrockGatewayError):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.error, "message": exc.message})

    return app, index, cognito


async def retained_rows(db):
    """Compare all account/credential/history data, not just row counts."""
    tables = (User.__table__, UserIdentity.__table__, UserCredential.__table__, UsageLog.__table__, Team.__table__)
    return {table.name: list((await db.execute(select(table).order_by(table.c.id))).mappings()) for table in tables}


@pytest.mark.parametrize("loaded", [False, True])
async def test_hard_delete_member_preserves_accounts_and_other_orgs(sessions, seeded, monkeypatch, loaded):
    async with sessions() as db:
        before = await retained_rows(db)
        org = await db.get(Organization, "target")
        if loaded:
            await db.refresh(org, ["memberships"])
            assert len(org.memberships) == 1
        else:
            assert "memberships" in inspect(org).unloaded
        app, index, cognito = make_app(db, monkeypatch)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete("/admin/organizations/target")
            assert response.status_code == 204, response.text
            assert (await client.delete("/admin/organizations/target")).status_code == 404
        index.delete_all_for_org.assert_awaited_once_with(["target-installation"], ["target-client"])
        assert not cognito.method_calls
    # Read through a fresh identity map: cached memberships cannot fake success.
    async with sessions() as db:
        assert await db.get(Organization, "target") is None
        assert await db.scalar(select(TenantMembership.id).where(TenantMembership.tenant_id == "target")) is None
        assert await db.get(TeamMembership, "target-team-membership") is None
        assert await db.get(TeamMembership, "home-team-membership") is not None
        assert await db.get(ChannelTenantMap, "channel-target") is None
        assert await db.get(ChannelTenantMap, "channel-home") is not None
        assert await retained_rows(db) == before
        assert (await db.get(TenantMembership, "home-membership")).is_active
        assert (await db.get(TenantMembership, "third-membership")).tenant_id == "third"
        assert await db.get(Organization, "home") is not None
        assert await db.get(Organization, "third") is not None


@pytest.mark.parametrize("path", ["/admin", "/api/admin/identity"])
async def test_empty_and_missing_organization(sessions, seeded, monkeypatch, path):
    async with sessions() as db:
        app, _, _ = make_app(db, monkeypatch)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.delete(f"{path}/organizations/empty")).status_code == 204
            assert (await client.delete(f"{path}/organizations/absent")).status_code == 404


@pytest.mark.parametrize("loaded", [False, True])
async def test_archive_keeps_memberships_and_repeated_archive(sessions, seeded, monkeypatch, loaded):
    async with sessions() as db:
        before = await retained_rows(db)
        memberships = list((await db.execute(select(TenantMembership.__table__).order_by(TenantMembership.id))).mappings())
        org = await db.get(Organization, "target")
        if loaded:
            await db.refresh(org, ["memberships"])
        app, index, cognito = make_app(db, monkeypatch)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for _ in range(2):
                response = await client.delete("/api/admin/identity/organizations/target")
                assert response.status_code == 204, response.text
        assert index.delete_org_identities.await_count == 2
        index.delete_all_for_org.assert_not_awaited()
        assert not cognito.method_calls
    async with sessions() as db:
        assert (await db.get(Organization, "target")).settings["status"] == "archived"
        assert await retained_rows(db) == before
        assert list((await db.execute(select(TenantMembership.__table__).order_by(TenantMembership.id))).mappings()) == memberships
        assert await db.get(TeamMembership, "target-team-membership") is not None


@pytest.mark.parametrize("path", ["/admin", "/api/admin/identity"])
async def test_org_admin_cannot_delete(sessions, seeded, monkeypatch, path):
    async with sessions() as db:
        before = await retained_rows(db)
        app, index, cognito = make_app(db, monkeypatch, admin=False)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Caller has real org_admin membership in home; neither their own
            # org nor a foreign org is deletable with that scope.
            for org in ("home", "target"):
                assert (await client.delete(f"{path}/organizations/{org}")).status_code == 403
        assert not index.mock_calls
        assert not cognito.method_calls
        assert await retained_rows(db) == before
        assert await db.get(Organization, "home") is not None
        assert await db.get(Organization, "target") is not None


@pytest.mark.parametrize("loaded", [False, True])
async def test_retained_tenant_fk_refuses_and_rolls_back(sessions, seeded, monkeypatch, loaded):
    async with sessions() as db:
        db.add(Tenant(id="target", display_name="Retained tenant"))
        await db.commit()
        before = await retained_rows(db)
        org = await db.get(Organization, "target")
        if loaded:
            await db.refresh(org, ["memberships", "tenant"])
        app, index, cognito = make_app(db, monkeypatch)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete("/admin/organizations/target")
            assert response.status_code == 409, response.text
            assert response.json()["error"] == "conflict"
            assert "archive" in response.json()["message"].lower()
        # Failed delete rolled back its cascades and left the session usable.
        assert await db.get(Organization, "target") is not None
        assert await db.get(Tenant, "target") is not None
        assert await db.scalar(select(TenantMembership.id).where(TenantMembership.tenant_id == "target"))
        assert await db.get(TeamMembership, "target-team-membership") is not None
        assert await retained_rows(db) == before
        assert not index.mock_calls
        assert not cognito.method_calls


async def test_actual_membership_fk_is_nonnull_and_cascades(sessions):
    async with sessions() as db:
        fk = await db.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'tenant_memberships'::regclass "
                "AND contype = 'f' AND confrelid = 'organizations'::regclass"
            )
        )
        assert "ON DELETE CASCADE" in fk
        assert await db.scalar(text("SELECT attnotnull FROM pg_attribute WHERE attrelid = 'tenant_memberships'::regclass AND attname = 'tenant_id'"))
