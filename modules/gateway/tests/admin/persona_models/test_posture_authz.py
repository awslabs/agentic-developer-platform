"""PMM-07 posture surface authority: platform-admin only, on every route.

Three decisions make these tests mean something, mirroring the bedrock_routing
harness they are modelled on:

**``AccessControl`` is never mocked.**  The routes construct it against the
request's real session, so ``require_platform_admin`` runs for real against a
real ``tenant_memberships`` row.  Authority is the property under test; a mocked
gate would assert a guarantee it never exercised.

**Roles come from the database, never from a claim.**  ``is_admin`` is set only
for the platform admin, mirroring ``auth/dependencies.py``, which deliberately
excludes ``org_admin`` from that flag.  That exclusion is what the 403s prove.

**The org-admin denial is the point.**  The policy-settings row carries no
``TenantMixin`` because the posture applies across every tenant, so a tenant
administrator must not be able to flip platform-wide enforcement — not even for
"their own" organization, since the setting has no organization.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.admin.persona_models.posture_routes import router as posture_router
from src.agentauth.runtime_posture import reset_posture_cache
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.exceptions import BedrockGatewayError
from src.shared.models.audit import AuditLog
from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.persona_models import PersonaModelPolicySetting
from src.shared.schemas.auth import TokenContext

ORG_ID = "org-5425-acme"
TEAM_ID = "team-1"
CLASS = "claude-agent-sdk"

MEMBER_ID, MEMBER_SUB = "user-member", "sub-member"
ORG_ADMIN_ID, ORG_ADMIN_SUB = "user-org-admin", "sub-org-admin"
PLATFORM_ADMIN_ID, PLATFORM_ADMIN_SUB = "user-platform-admin", "sub-platform-admin"

#: Every route on the surface, as (method, path, body).  Parameterised as one
#: list so a route added later without a gate fails these tests rather than
#: quietly going untested.
ALL_ROUTES = [
    ("GET", f"/admin/persona-models/posture/{CLASS}", None),
    ("PUT", f"/admin/persona-models/posture/{CLASS}", {"posture": "enforcing", "expected_revision": 1}),
]


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_posture_cache()
    yield
    reset_posture_cache()


@pytest.fixture
async def engine():
    e = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=sa.pool.StaticPool, connect_args={"check_same_thread": False})
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield e
    await e.dispose()


@pytest.fixture
async def session(engine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest.fixture
async def seeded(session: AsyncSession) -> None:
    """One tenant, three people with real membership rows, one settings row."""
    session.add(Organization(id=ORG_ID, name="Acme Corp"))
    for canonical, sub, role in (
        (MEMBER_ID, MEMBER_SUB, "member"),
        (ORG_ADMIN_ID, ORG_ADMIN_SUB, "org_admin"),
        (PLATFORM_ADMIN_ID, PLATFORM_ADMIN_SUB, "platform_admin"),
    ):
        session.add(User(id=canonical, cognito_sub=sub, email=f"{sub}@example.com", org_id=ORG_ID, team_id=TEAM_ID))
        session.add(TenantMembership(user_id=canonical, tenant_id=ORG_ID, role=role, is_active=True))
    session.add(
        PersonaModelPolicySetting(
            compatibility_class=CLASS,
            revision=1,
            posture_revision=1,
            enforcement_posture="report_only",
        )
    )
    await session.commit()


def context_for(sub: str, *, is_admin: bool = False) -> TokenContext:
    return TokenContext(
        user_id=sub,
        org_id=ORG_ID,
        team_id=TEAM_ID,
        department_id="",
        account_type="human",
        auth_source="jwt",
        is_admin=is_admin,
        expires_at=date(2099, 1, 1),
    )


def build_app(session: AsyncSession, context: TokenContext | None) -> FastAPI:
    """Mount the posture router alone.  ``AccessControl`` is NOT overridden."""
    app = FastAPI()
    app.include_router(posture_router)

    # The same handler src/app.py registers.  Without it a raised
    # AccessDeniedError surfaces as an unhandled 500 and every 403 assertion
    # would be measuring the test app's gap instead of the route's behaviour.
    @app.exception_handler(BedrockGatewayError)
    async def gateway_error_handler(request: Request, exc: BedrockGatewayError):
        content = {"error": exc.error, "message": exc.message}
        if exc.details:
            content["details"] = exc.details
        return JSONResponse(status_code=exc.status_code, content=content)

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    if context is not None:
        app.dependency_overrides[get_current_user] = lambda: context
    return app


def client_for(session: AsyncSession, context: TokenContext | None) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=build_app(session, context)), base_url="http://test")


async def _call(client, method: str, path: str, body: dict | None):
    if method == "GET":
        return await client.get(path)
    return await client.put(path, json=body)


async def _stored(session: AsyncSession) -> tuple[str, int]:
    session.expire_all()
    row = await session.get(PersonaModelPolicySetting, CLASS)
    return row.enforcement_posture, row.posture_revision


class TestOnlyPlatformAdminsReach:
    @pytest.mark.parametrize(("method", "path", "body"), ALL_ROUTES, ids=lambda v: str(v))
    async def test_org_admin_is_denied_on_every_route(self, session, seeded, method, path, body):
        """A tenant admin cannot flip platform-wide enforcement.

        The org admin is a real ``org_admin`` by ``tenant_memberships`` and
        legitimately holds their own organization's permissions.  The refusal
        comes from the caller's authority alone — and "it is my own org" is not a
        softer door here, because the posture belongs to no organization.
        """
        async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
            response = await _call(client, method, path, body)
        assert response.status_code == 403, f"{method} {path} -> {response.status_code}: {response.text}"
        assert await _stored(session) == ("report_only", 1), "a denied request must write nothing"

    @pytest.mark.parametrize(("method", "path", "body"), ALL_ROUTES, ids=lambda v: str(v))
    async def test_ordinary_member_is_denied_on_every_route(self, session, seeded, method, path, body):
        async with client_for(session, context_for(MEMBER_SUB)) as client:
            response = await _call(client, method, path, body)
        assert response.status_code == 403, f"{method} {path} -> {response.status_code}: {response.text}"
        assert await _stored(session) == ("report_only", 1)

    @pytest.mark.parametrize(("method", "path", "body"), ALL_ROUTES, ids=lambda v: str(v))
    async def test_service_account_is_denied_on_every_route(self, session, seeded, method, path, body):
        """A workload identity must never change the platform's enforcement state."""
        context = context_for("sub-service", is_admin=False)
        context = context.model_copy(update={"account_type": "service", "auth_source": "iam"})
        async with client_for(session, context) as client:
            response = await _call(client, method, path, body)
        assert response.status_code == 403, f"{method} {path} -> {response.status_code}: {response.text}"
        assert await _stored(session) == ("report_only", 1)

    async def test_denied_writes_leave_no_change_audit(self, session, seeded):
        async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
            await client.put(
                f"/admin/persona-models/posture/{CLASS}",
                json={"posture": "enforcing", "expected_revision": 1},
            )
        rows = list(await session.scalars(sa.select(AuditLog).where(AuditLog.event_type == "persona_model_posture_changed")))
        assert rows == []

    async def test_platform_admin_can_read_and_change(self, session, seeded):
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            read = await client.get(f"/admin/persona-models/posture/{CLASS}")
            assert read.status_code == 200, read.text
            assert read.json()["posture"] == "report_only"
            assert read.json()["posture_revision"] == 1
            assert sorted(read.json()["supported_postures"]) == ["disabled", "enforcing", "report_only"]
            assert read.json()["propagation_bound_seconds"] >= 0

            wrote = await client.put(
                f"/admin/persona-models/posture/{CLASS}",
                json={"posture": "enforcing", "expected_revision": 1, "reason": "staged flip"},
            )
            assert wrote.status_code == 200, wrote.text
            assert wrote.json()["posture"] == "enforcing"
            assert wrote.json()["posture_revision"] == 2

        assert await _stored(session) == ("enforcing", 2)


class TestRefusalsThroughTheRoute:
    async def test_stale_revision_is_a_conflict_that_writes_nothing(self, session, seeded):
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            response = await client.put(
                f"/admin/persona-models/posture/{CLASS}",
                json={"posture": "enforcing", "expected_revision": 7},
            )
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["reason"] == "posture_revision_conflict"
        assert detail["current_posture_revision"] == 1
        assert await _stored(session) == ("report_only", 1)

    async def test_unsupported_posture_is_refused_and_audited(self, session, seeded):
        """Schema validation must not swallow this: the refusal needs an audit row."""
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            response = await client.put(
                f"/admin/persona-models/posture/{CLASS}",
                json={"posture": "mandatory", "expected_revision": 1},
            )
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["reason"] == "runtime_posture_unsupported"
        assert await _stored(session) == ("report_only", 1)

        rows = list(await session.scalars(sa.select(AuditLog).where(AuditLog.event_type == "persona_model_posture_rejected")))
        assert len(rows) == 1
        assert rows[0].details["requested_posture"] == "mandatory"

    async def test_unprovisioned_class_is_refused(self, session, seeded):
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            response = await client.put(
                "/admin/persona-models/posture/codex-sdk",
                json={"posture": "enforcing", "expected_revision": 1},
            )
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["reason"] == "runtime_posture_unavailable"

    async def test_unknown_class_is_refused(self, session, seeded):
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            response = await client.get("/admin/persona-models/posture/not-a-class")
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["reason"] == "compatibility_class_unknown"

    async def test_rollback_is_the_same_audited_operation(self, session, seeded):
        """§9: reverting enforcement is as version-checked as applying it."""
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            forward = await client.put(
                f"/admin/persona-models/posture/{CLASS}",
                json={"posture": "enforcing", "expected_revision": 1, "reason": "staged flip"},
            )
            assert forward.status_code == 200, forward.text
            back = await client.put(
                f"/admin/persona-models/posture/{CLASS}",
                json={"posture": "report_only", "expected_revision": 2, "reason": "operational rollback"},
            )
            assert back.status_code == 200, back.text
            assert back.json()["posture"] == "report_only"
            assert back.json()["posture_revision"] == 3

        assert await _stored(session) == ("report_only", 3)
        rows = sorted(
            await session.scalars(sa.select(AuditLog).where(AuditLog.event_type == "persona_model_posture_changed")),
            key=lambda row: row.details["after_posture_revision"],
        )
        assert [row.details["after_posture"] for row in rows] == ["enforcing", "report_only"]
        assert [row.details["change_reason"] for row in rows] == ["staged flip", "operational rollback"]


class TestSourceLevelGate:
    """A route added later without the gate must fail here, not in production."""

    def test_every_handler_requires_platform_admin(self):
        source = Path("src/admin/persona_models/posture_routes.py").read_text()
        handlers = re.findall(
            r"@router\.(get|put|post|delete|patch)\([^)]*\)\s*\nasync def (\w+)\((.*?)\n(?=@router\.|\Z)",
            source,
            re.DOTALL,
        )
        assert handlers, "no handlers found — the parser, not the source, is wrong"
        assert len(handlers) == len(ALL_ROUTES), "ALL_ROUTES must list every route on this surface"
        for _method, name, body in handlers:
            assert "require_platform_admin" in body, f"{name} lacks the platform-admin gate"

    def test_the_surface_does_not_use_the_tenant_admin_gate(self):
        """A tenant-scoped permission here would let an org admin flip enforcement.

        Comments and docstrings are stripped before the check so that *explaining*
        why the tenant gate is wrong does not trip the assertion.
        """
        source = Path("src/admin/persona_models/posture_routes.py").read_text()
        code = "\n".join(line.partition("#")[0] for line in source.splitlines())
        # Drop docstrings (the module docstring names the rejected gate on purpose).
        code = re.sub(r'"""(?:.|\n)*?"""', "", code)
        assert "ORG_UPDATE" not in code
        assert "check_permission" not in code
        assert "require_platform_admin" in code
