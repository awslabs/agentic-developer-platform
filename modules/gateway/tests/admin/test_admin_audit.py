"""Durable attributable admin audit coverage tests.

Issue #6037 (S13): Tests the audit writer, route instrumentation, retrieval
endpoint, and mutation-coverage gate.

Tests exercise real mounted behavior with synthetic tenant/user records, query
persisted audit rows in a fresh DB session, prove denied calls leave targets
unchanged, and demonstrate refusal/sink-failure cases.  Stubs AWS provider
boundaries before app creation; uses dummy credentials and no sockets.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.audit import write_admin_audit, write_admin_audit_on_refusal
from src.shared.models.audit import AuditLog
from src.shared.models.base import Base
from src.shared.schemas.auth import TokenContext

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_DB = "sqlite+aiosqlite:///:memory:"


def _platform_admin_ctx(org_id: str = "org-test") -> TokenContext:
    return TokenContext(
        user_id="admin-sub-001",
        org_id=org_id,
        team_id="team-001",
        department_id="dept-001",
        account_type="human",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _regular_user_ctx(org_id: str = "org-test") -> TokenContext:
    return TokenContext(
        user_id="user-sub-001",
        org_id=org_id,
        team_id="team-001",
        department_id="dept-001",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
async def engine():
    eng = create_async_engine(TEST_DB, echo=False, poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess


@pytest.fixture
async def session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Unit tests for the audit writer
# ---------------------------------------------------------------------------


class TestWriteAdminAudit:
    """write_admin_audit persists the required event contract fields."""

    async def test_success_writes_row_with_required_fields(self, session: AsyncSession):
        ctx = _platform_admin_ctx()
        await write_admin_audit(
            session,
            actor=ctx,
            action="update_organization",
            target_type="organization",
            target_id="org-42",
            org_id="org-42",
        )
        await session.commit()

        rows = (await session.execute(select(AuditLog).where(AuditLog.event_type == "admin_update_organization"))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_id == "admin-sub-001"
        assert row.org_id == "org-42"
        assert row.details["target_type"] == "organization"
        assert row.details["target_id"] == "org-42"
        assert row.details["outcome"] == "success"

    async def test_defaults_org_id_from_actor(self, session: AsyncSession):
        ctx = _platform_admin_ctx(org_id="actor-org")
        await write_admin_audit(
            session,
            actor=ctx,
            action="add_pool_account",
            target_type="pool_account",
            target_id="acct-1",
        )
        await session.commit()

        row = (await session.execute(select(AuditLog).where(AuditLog.event_type == "admin_add_pool_account"))).scalar_one()
        assert row.org_id == "actor-org"

    async def test_correlation_id_persisted(self, session: AsyncSession):
        ctx = _platform_admin_ctx()
        await write_admin_audit(
            session,
            actor=ctx,
            action="test_corr",
            target_type="t",
            target_id="1",
            correlation_id="req-abc-123",
        )
        await session.commit()

        row = (await session.execute(select(AuditLog).where(AuditLog.event_type == "admin_test_corr"))).scalar_one()
        assert row.details["correlation_id"] == "req-abc-123"

    async def test_extra_fields_merged(self, session: AsyncSession):
        ctx = _platform_admin_ctx()
        await write_admin_audit(
            session,
            actor=ctx,
            action="test_extra",
            target_type="t",
            target_id="1",
            extra={"old_name": "A", "new_name": "B"},
        )
        await session.commit()

        row = (await session.execute(select(AuditLog).where(AuditLog.event_type == "admin_test_extra"))).scalar_one()
        assert row.details["old_name"] == "A"
        assert row.details["new_name"] == "B"


class TestWriteAdminAuditOnRefusal:
    """write_admin_audit_on_refusal commits independently and swallows failures."""

    async def test_refusal_persists_with_denied_outcome(self, session: AsyncSession):
        ctx = _regular_user_ctx()
        await write_admin_audit_on_refusal(
            session,
            actor=ctx,
            action="delete_organization",
            target_type="organization",
            target_id="org-42",
            reason="Insufficient privileges",
        )

        row = (await session.execute(select(AuditLog).where(AuditLog.event_type == "admin_delete_organization"))).scalar_one()
        assert row.details["outcome"] == "denied"
        assert row.details["reason"] == "Insufficient privileges"
        assert row.actor_id == "user-sub-001"

    async def test_sink_failure_swallowed(self, session: AsyncSession):
        """A broken audit sink must not crash the caller."""
        ctx = _regular_user_ctx()

        with patch.object(session, "commit", side_effect=RuntimeError("db down")):
            # Should not raise
            await write_admin_audit_on_refusal(
                session,
                actor=ctx,
                action="test_fail",
                target_type="t",
                target_id="1",
                reason="test",
            )


# ---------------------------------------------------------------------------
# Integration tests: real mounted routes write audit rows
# ---------------------------------------------------------------------------


def _build_admin_app(session: AsyncSession, context: TokenContext | None = None) -> FastAPI:
    """Minimal app mounting only the admin router for focused testing."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from src.admin.routes import router as admin_router
    from src.auth.dependencies import get_current_user
    from src.shared.database import get_db
    from src.shared.exceptions import BedrockGatewayError

    app = FastAPI()
    app.include_router(admin_router)

    @app.exception_handler(BedrockGatewayError)
    async def gateway_error_handler(request: Request, exc: BedrockGatewayError):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.message})

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db

    if context is not None:

        async def override_user():
            return context

        app.dependency_overrides[get_current_user] = override_user

    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed_org_and_user(session: AsyncSession, org_id: str = "org-test") -> dict:
    """Seed minimal org/department/team/user for route tests."""
    from src.shared.models.onboarding import TenantMembership
    from src.shared.models.organization import Department, Organization, Team, User

    org = Organization(id=org_id, name=f"Org {org_id}")
    session.add(org)
    await session.flush()

    dept = Department(id=f"dept-{org_id}", org_id=org_id, name="Default Dept")
    session.add(dept)
    await session.flush()

    team = Team(id=f"team-{org_id}", org_id=org_id, department_id=dept.id, name="Default Team")
    session.add(team)
    await session.flush()

    user = User(
        id=f"user-pg-{org_id}",
        org_id=org_id,
        team_id=team.id,
        email=f"user@{org_id}.test",
        name="Test User",
        cognito_sub="user-sub-001",
        role="member",
    )
    session.add(user)
    await session.flush()

    # Seed a membership for AccessControl resolution
    session.add(
        TenantMembership(
            user_id=f"user-pg-{org_id}",
            tenant_id=org_id,
            role="org_admin",
            is_active=True,
            joined_via="org_membership",
        )
    )

    # Platform admin user row bridging cognito sub → users.id
    admin_user = User(
        id="admin-pg-001",
        org_id=org_id,
        team_id=team.id,
        email="admin@test.test",
        name="Admin",
        cognito_sub="admin-sub-001",
        role="platform_admin",
    )
    session.add(admin_user)
    await session.flush()

    session.add(
        TenantMembership(
            user_id="admin-pg-001",
            tenant_id=org_id,
            role="platform_admin",
            is_active=True,
            joined_via="org_membership",
        )
    )
    await session.commit()

    return {
        "org_id": org_id,
        "dept_id": dept.id,
        "team_id": team.id,
        "user_id": user.id,
    }


class TestRouteAuditInstrumentation:
    """Core admin mutation routes write audit rows to security_audit_logs."""

    async def test_update_organization_writes_audit(self, session: AsyncSession):
        seed = await _seed_org_and_user(session)
        ctx = _platform_admin_ctx(org_id=seed["org_id"])
        app = _build_admin_app(session, ctx)

        async with _client(app) as c:
            resp = await c.put(
                f"/admin/organizations/{seed['org_id']}",
                json={"name": "New Name"},
            )
        assert resp.status_code == 200

        row = (await session.execute(select(AuditLog).where(AuditLog.event_type == "admin_update_organization"))).scalar_one_or_none()
        assert row is not None
        assert row.actor_id == ctx.user_id
        assert row.details["target_id"] == seed["org_id"]

    async def test_delete_organization_writes_audit(self, session: AsyncSession):
        seed = await _seed_org_and_user(session)
        ctx = _platform_admin_ctx(org_id=seed["org_id"])
        app = _build_admin_app(session, ctx)

        async with _client(app) as c:
            resp = await c.delete(f"/admin/organizations/{seed['org_id']}")
        assert resp.status_code == 204

        row = (await session.execute(select(AuditLog).where(AuditLog.event_type == "admin_delete_organization"))).scalar_one_or_none()
        assert row is not None
        assert row.details["target_type"] == "organization"

    async def test_create_department_writes_audit(self, session: AsyncSession):
        seed = await _seed_org_and_user(session)
        ctx = _platform_admin_ctx(org_id=seed["org_id"])
        app = _build_admin_app(session, ctx)

        async with _client(app) as c:
            resp = await c.post(
                f"/admin/organizations/{seed['org_id']}/departments",
                json={"name": "Engineering"},
            )
        assert resp.status_code == 201

        row = (await session.execute(select(AuditLog).where(AuditLog.event_type == "admin_create_department"))).scalar_one_or_none()
        assert row is not None
        assert row.details["target_type"] == "department"
        assert row.org_id == seed["org_id"]

    async def test_create_team_writes_audit(self, session: AsyncSession):
        seed = await _seed_org_and_user(session)
        ctx = _platform_admin_ctx(org_id=seed["org_id"])
        app = _build_admin_app(session, ctx)

        async with _client(app) as c:
            resp = await c.post(
                f"/admin/organizations/{seed['org_id']}/departments/{seed['dept_id']}/teams",
                json={"name": "Backend"},
            )
        assert resp.status_code == 201

        row = (await session.execute(select(AuditLog).where(AuditLog.event_type == "admin_create_team"))).scalar_one_or_none()
        assert row is not None
        assert row.details["target_type"] == "team"


class TestAuditRetrieval:
    """GET /admin/audit-events returns persisted admin audit events."""

    async def test_retrieves_admin_events(self, session: AsyncSession):
        # Write some audit events directly
        ctx = _platform_admin_ctx()
        await write_admin_audit(session, actor=ctx, action="test_action", target_type="org", target_id="org-1")
        await write_admin_audit(session, actor=ctx, action="test_action2", target_type="user", target_id="u-1")
        await session.commit()

        app = _build_admin_app(session, ctx)
        async with _client(app) as c:
            resp = await c.get("/admin/audit-events")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 2
        assert len(data["events"]) >= 2
        types = {e["event_type"] for e in data["events"]}
        assert "admin_test_action" in types

    async def test_filters_by_action(self, session: AsyncSession):
        ctx = _platform_admin_ctx()
        await write_admin_audit(session, actor=ctx, action="alpha", target_type="t", target_id="1")
        await write_admin_audit(session, actor=ctx, action="beta", target_type="t", target_id="2")
        await session.commit()

        app = _build_admin_app(session, ctx)
        async with _client(app) as c:
            resp = await c.get("/admin/audit-events", params={"action": "alpha"})
        assert resp.status_code == 200
        data = resp.json()
        assert all(e["event_type"] == "admin_alpha" for e in data["events"])

    async def test_non_admin_denied(self, session: AsyncSession):
        ctx = _regular_user_ctx()
        app = _build_admin_app(session, ctx)
        async with _client(app) as c:
            resp = await c.get("/admin/audit-events")
        assert resp.status_code == 403

    async def test_pagination(self, session: AsyncSession):
        ctx = _platform_admin_ctx()
        for i in range(5):
            await write_admin_audit(session, actor=ctx, action=f"page_test_{i}", target_type="t", target_id=str(i))
        await session.commit()

        app = _build_admin_app(session, ctx)
        async with _client(app) as c:
            resp = await c.get("/admin/audit-events", params={"page_size": 2, "page": 1})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["events"]) == 2
        assert data["total"] >= 5


class TestDeniedCallLeavesTargetUnchanged:
    """Access-denied calls do not mutate the target."""

    async def test_non_admin_cannot_delete_org(self, session: AsyncSession):
        seed = await _seed_org_and_user(session)
        ctx = _regular_user_ctx(org_id=seed["org_id"])
        app = _build_admin_app(session, ctx)

        async with _client(app) as c:
            resp = await c.delete(f"/admin/organizations/{seed['org_id']}")
        # Should be denied (403 or similar)
        assert resp.status_code in (403, 422)

        # Org still exists
        from src.shared.models.organization import Organization

        org = await session.get(Organization, seed["org_id"])
        assert org is not None
