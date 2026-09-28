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
    ("GET", f"/admin/persona-models/posture/{CLASS}/history/1", None),
    (
        "POST",
        f"/admin/persona-models/posture/{CLASS}/rollback",
        {"expected_revision": 2, "historical_revision": 1, "operation_id": "2ae4a67c-020c-4a3b-ae31-7c68520d6904", "reason": "rollback"},
    ),
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
    return await client.request(method, path, json=body)


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


class TestCanonicalActorIsRequired:
    """The audit actor must be a verified canonical identity, or the change is refused.

    Operator-reproduced defect: a platform admin whose subject had no ``users``
    row received HTTP 200 and the raw token subject was persisted as
    ``updated_by`` and as the audit ``actor_id`` — an identifier in a different
    namespace from every other actor in the table, so the one record whose purpose
    is accountability for a platform-wide enforcement change was unjoinable.
    """

    async def test_unregistered_platform_admin_is_refused_and_writes_nothing(self, session, seeded):
        """A valid admin token with no identity row cannot change enforcement."""
        async with client_for(session, context_for("signed-admin-sub", is_admin=True)) as client:
            response = await client.put(
                f"/admin/persona-models/posture/{CLASS}",
                json={"posture": "enforcing", "expected_revision": 1},
            )
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["reason"] == "actor_identity_unresolved"
        assert await _stored(session) == ("report_only", 1)

    async def test_the_raw_subject_is_never_persisted(self, session, seeded):
        """Not as updated_by, and not as an audit actor on the refusal either."""
        async with client_for(session, context_for("signed-admin-sub", is_admin=True)) as client:
            await client.put(
                f"/admin/persona-models/posture/{CLASS}",
                json={"posture": "enforcing", "expected_revision": 1},
            )
        row = await session.get(PersonaModelPolicySetting, CLASS)
        assert row.updated_by != "signed-admin-sub"
        audits = list(await session.scalars(sa.select(AuditLog)))
        assert all(audit.actor_id != "signed-admin-sub" for audit in audits), "a raw subject reached the audit trail"

    async def test_the_refusal_is_itself_audited(self, session, seeded):
        async with client_for(session, context_for("signed-admin-sub", is_admin=True)) as client:
            await client.put(
                f"/admin/persona-models/posture/{CLASS}",
                json={"posture": "enforcing", "expected_revision": 1},
            )
        rows = list(await session.scalars(sa.select(AuditLog).where(AuditLog.event_type == "persona_model_posture_rejected")))
        assert len(rows) == 1
        assert rows[0].details["reason"] == "actor_identity_unresolved"
        assert rows[0].actor_id is None

    async def test_the_database_itself_prevents_a_duplicate_subject(self, session, seeded):
        """Why the ambiguity branch is defence in depth, recorded as a test.

        A second account sharing the sub cannot be created: ``uq_users_cognito_sub``
        is unique across all non-NULL subs.  Pinning that here keeps the service's
        docstring honest — the multi-match branch exists for a partially-migrated
        database, not because duplicate subs happen — and would fail loudly if the
        constraint were ever relaxed to per-workspace uniqueness, at which point
        the "which workspace identity acted?" question becomes real.
        """
        session.add(Organization(id="org-other", name="Other Corp"))
        session.add(
            User(
                id="user-platform-admin-elsewhere",
                cognito_sub=PLATFORM_ADMIN_SUB,
                email="elsewhere@example.com",
                org_id="org-other",
                team_id=TEAM_ID,
            )
        )
        with pytest.raises(sa.exc.IntegrityError) as err:
            await session.commit()
        assert "cognito_sub" in str(err.value)
        await session.rollback()

    async def test_multiple_canonical_matches_are_refused(self, session, seeded, monkeypatch):
        """The branch itself, driven directly since the schema blocks the data."""
        from src.admin.persona_models import posture_service

        async def two_matches(_statement, *_args, **_kwargs):
            return ["user-a", "user-b"]

        monkeypatch.setattr(session, "scalars", two_matches)
        with pytest.raises(posture_service.PostureMutationError) as err:
            await posture_service.resolve_posture_actor_id(session, PLATFORM_ADMIN_SUB)
        assert err.value.reason == "actor_identity_unresolved"

    @pytest.mark.parametrize("blank", ["", "   ", None])
    async def test_an_empty_subject_is_refused(self, session, seeded, blank):
        from src.admin.persona_models import posture_service

        with pytest.raises(posture_service.PostureMutationError) as err:
            await posture_service.resolve_posture_actor_id(session, blank)
        assert err.value.reason == "actor_identity_unresolved"

    async def test_a_failed_identity_read_refuses_rather_than_substituting(self, session, seeded, monkeypatch):
        """An unauditable change must not proceed on a degraded identity read."""
        from src.admin.persona_models import posture_service

        real_scalars = session.scalars

        async def failing_scalars(statement, *args, **kwargs):
            if "users" in str(statement).lower():
                raise sa.exc.OperationalError("SELECT users", {}, Exception("identity store unreachable"))
            return await real_scalars(statement, *args, **kwargs)

        monkeypatch.setattr(session, "scalars", failing_scalars)
        with pytest.raises(posture_service.PostureMutationError) as err:
            await posture_service.resolve_posture_actor_id(session, PLATFORM_ADMIN_SUB)
        assert err.value.reason == "actor_identity_unresolved"

    async def test_the_registered_admin_is_attributed_by_canonical_id(self, session, seeded):
        """The positive case: the canonical id, not the subject, is recorded."""
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            response = await client.put(
                f"/admin/persona-models/posture/{CLASS}",
                json={"posture": "enforcing", "expected_revision": 1},
            )
        assert response.status_code == 200, response.text
        row = await session.get(PersonaModelPolicySetting, CLASS)
        assert row.updated_by == PLATFORM_ADMIN_ID
        assert row.updated_by != PLATFORM_ADMIN_SUB
        rows = list(await session.scalars(sa.select(AuditLog).where(AuditLog.event_type == "persona_model_posture_changed")))
        assert [audit.actor_id for audit in rows] == [PLATFORM_ADMIN_ID]


class TestExpectedRevisionIsStrictAtTheBoundary:
    """Compare-and-set is only a guarantee if the revision survives parsing.

    Operator-reproduced defect: ``expected_revision: true`` received HTTP 200 and
    moved the setting to revision 2.  Pydantic coerced the boolean to ``1`` for a
    plain ``int`` field, so the service's own bool-rejecting validator was handed
    an already-laundered value and had nothing left to reject.
    """

    @pytest.mark.parametrize(
        "bad",
        [True, False, 1.5, "1", "", None, 0, -1, [1], {"revision": 1}],
        ids=["true", "false", "fractional", "numeric-string", "empty-string", "null", "zero", "negative", "list", "object"],
    )
    async def test_a_non_positive_integer_revision_changes_nothing(self, session, seeded, bad):
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            response = await client.put(
                f"/admin/persona-models/posture/{CLASS}",
                json={"posture": "enforcing", "expected_revision": bad},
            )
        assert response.status_code == 422, f"expected_revision={bad!r} -> {response.status_code}: {response.text}"
        assert await _stored(session) == ("report_only", 1), f"expected_revision={bad!r} changed the setting"

    async def test_a_missing_revision_changes_nothing(self, session, seeded):
        """A caller that never read the revision cannot compare-and-set."""
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            response = await client.put(f"/admin/persona-models/posture/{CLASS}", json={"posture": "enforcing"})
        assert response.status_code == 422, response.text
        assert await _stored(session) == ("report_only", 1)

    async def test_a_valid_integer_revision_still_works(self, session, seeded):
        """Strictness must not break the supported path."""
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            response = await client.put(
                f"/admin/persona-models/posture/{CLASS}",
                json={"posture": "enforcing", "expected_revision": 1},
            )
        assert response.status_code == 200, response.text
        assert await _stored(session) == ("enforcing", 2)

    async def test_a_stale_integer_revision_is_still_a_conflict(self, session, seeded):
        """The boundary check must not shadow the compare-and-set refusal."""
        async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
            first = await client.put(
                f"/admin/persona-models/posture/{CLASS}",
                json={"posture": "enforcing", "expected_revision": 1},
            )
            assert first.status_code == 200, first.text
            stale = await client.put(
                f"/admin/persona-models/posture/{CLASS}",
                json={"posture": "disabled", "expected_revision": 1},
            )
        assert stale.status_code == 409, stale.text
        assert stale.json()["detail"]["reason"] == "posture_revision_conflict"
        assert await _stored(session) == ("enforcing", 2)


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


async def test_cli_posture_replay_and_authoritative_rollback(session, seeded):
    path = f"/admin/persona-models/posture/{CLASS}"
    body = dict(posture="enforcing", expected_revision=1, operation_id="e1fc3954-b69d-4f80-81b2-f4b1f9fb68eb", reason="qualified change")
    async with client_for(session, context_for(PLATFORM_ADMIN_SUB, is_admin=True)) as client:
        first = await client.put(path, json=body)
        assert first.status_code == 200, first.text
        again = await client.put(path, json=body)
        assert again.status_code == 200 and again.json() == first.json()
        history = await client.get(path + "/history/1")
        assert history.status_code == 200 and history.json()["posture"] == "report_only"
        rollback = dict(
            expected_revision=2, historical_revision=1, operation_id="c050e376-13ab-478a-bcb2-a0f5116509ba", reason="restore audited revision"
        )
        restored = await client.post(path + "/rollback", json=rollback)
        assert restored.status_code == 200, restored.text
        assert (restored.json()["posture"], restored.json()["posture_revision"]) == ("report_only", 3)
        assert (await client.post(path + "/rollback", json=rollback)).json() == restored.json()
        assert (await client.put(path, json=body)).json() == first.json()
        assert (await client.get(path)).json()["posture_revision"] == 3
        assert (await client.put(path, json={**body, "posture": "disabled"})).status_code == 409
    audits = list(await session.scalars(sa.select(AuditLog).where(AuditLog.event_type == "persona_model_posture_changed")))
    assert len(audits) == 2
    restored_audit = next(row for row in audits if row.details["after_posture_revision"] == 3)
    assert restored_audit.details["rollback_revision"] == 1
    assert restored_audit.details["rollback_audit_id"] == history.json()["audit_id"]
