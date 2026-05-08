"""Tests for onboarding handler — access status, request, admin approve/deny.

Issue #538: Covers collision detection, idempotency, auto-approve, pending path,
admin approve/deny transactions, flag preflight, tenant_id validation,
deny-deletes-cognito, and deny-cognito-failure-metric.
"""

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.models.base import Base
from src.shared.models.onboarding import TenantAccessRequest
from src.shared.models.organization import Organization
from src.shared.schemas.auth import TokenContext

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def db_engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine) -> AsyncSession:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest.fixture
def new_user_context() -> TokenContext:
    """A user who has no org yet (fresh sign-in)."""
    return TokenContext(
        user_id="cognito-sub-new-user",
        org_id="",
        team_id="",
        department_id="",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def admin_context() -> TokenContext:
    return TokenContext(
        user_id="admin-sub-001",
        org_id="platform",
        team_id="platform",
        department_id="platform",
        account_type="human",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
async def app_client(db_engine, new_user_context):
    """Create test client with auth override and DB override."""
    from src.app import create_app
    from src.auth.dependencies import get_current_user
    from src.shared.database import get_db

    app = create_app()

    async def override_db():
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    async def override_auth():
        return new_user_context

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_auth

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


@pytest.fixture
async def admin_app_client(db_engine, admin_context):
    """Create test client with admin auth override."""
    from src.app import create_app
    from src.auth.dependencies import get_current_user, require_admin
    from src.shared.database import get_db

    app = create_app()

    async def override_db():
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    async def override_auth():
        return admin_context

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_auth
    app.dependency_overrides[require_admin] = override_auth

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /access/status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_access_status_new_user(app_client):
    """New user with no existing rows gets status='new'."""
    resp = await app_client.get("/access/status")
    assert resp.status_code == 200
    assert resp.json()["status"] == "new"


# ---------------------------------------------------------------------------
# POST /access/request — proposed_tenant_id validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_request_rejects_reserved_name(app_client):
    """Reserved names like 'admin' should be rejected with 422."""
    resp = await app_client.post(
        "/access/request",
        json={
            "proposed_tenant_id": "admin",
            "target_login": "testuser",
            "provider": "github",
            "provider_user_id": "12345",
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_request_rejects_short_id(app_client):
    """IDs shorter than 3 chars should be rejected."""
    resp = await app_client.post(
        "/access/request",
        json={
            "proposed_tenant_id": "ab",
            "target_login": "testuser",
            "provider": "github",
            "provider_user_id": "12345",
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_request_rejects_leading_hyphen(app_client):
    """Leading hyphen should be rejected."""
    resp = await app_client.post(
        "/access/request",
        json={
            "proposed_tenant_id": "-bad-name",
            "target_login": "testuser",
            "provider": "github",
            "provider_user_id": "12345",
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_request_rejects_uppercase(app_client):
    """Uppercase chars should be rejected."""
    resp = await app_client.post(
        "/access/request",
        json={
            "proposed_tenant_id": "BadName",
            "target_login": "testuser",
            "provider": "github",
            "provider_user_id": "12345",
        },
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /access/request — feature flag preflight
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "false"})
async def test_request_503_when_flag_off(app_client):
    """Returns unavailable status when V2 write flag is off."""
    resp = await app_client.post(
        "/access/request",
        json={
            "proposed_tenant_id": "my-tenant",
            "target_login": "testuser",
            "provider": "github",
            "provider_user_id": "12345",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "unavailable"


# ---------------------------------------------------------------------------
# POST /access/request — collision detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_request_collision_on_existing_org(app_client, db_engine):
    """409 collision when proposed_tenant_id matches an existing org."""
    # Seed an organization
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        org = Organization(
            id="taken-org",
            name="Taken Org",
            aws_accounts=[],
            role_mappings={},
            settings={},
        )
        session.add(org)
        await session.commit()

    resp = await app_client.post(
        "/access/request",
        json={
            "proposed_tenant_id": "taken-org",
            "target_login": "testuser",
            "provider": "github",
            "provider_user_id": "12345",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "collision"


# ---------------------------------------------------------------------------
# POST /access/request — pending path + idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_request_pending_path(app_client):
    """Non-auto-approve user gets pending status."""
    resp = await app_client.post(
        "/access/request",
        json={
            "proposed_tenant_id": "new-tenant",
            "target_login": "unknown-user",
            "provider": "github",
            "provider_user_id": "99999",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending"
    assert data["request_id"] is not None


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_request_duplicate_is_idempotent(app_client):
    """Second request from same user returns same pending request."""
    payload = {
        "proposed_tenant_id": "dup-tenant",
        "target_login": "dup-user",
        "provider": "github",
        "provider_user_id": "88888",
    }
    resp1 = await app_client.post("/access/request", json=payload)
    assert resp1.json()["status"] == "pending"
    request_id = resp1.json()["request_id"]

    # Second call — same user, different proposed_tenant_id but still pending
    payload2 = {
        "proposed_tenant_id": "different-tenant",
        "target_login": "dup-user",
        "provider": "github",
        "provider_user_id": "88888",
    }
    resp2 = await app_client.post("/access/request", json=payload2)
    assert resp2.json()["status"] == "pending"
    assert resp2.json()["request_id"] == request_id


# ---------------------------------------------------------------------------
# POST /access/request — auto-approve path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(
    os.environ,
    {
        "USER_IDENTITY_INDEX_V2_WRITE": "true",
        "ONBOARDING_AUTO_APPROVE_ORGS": '[{"login": "auto-user", "tenant_mode": "personal"}]',
    },
)
async def test_request_auto_approve(app_client):
    """Auto-approved user gets immediate approval."""
    mock_writer = MagicMock()
    mock_writer.put_user_identity = AsyncMock(return_value=True)

    with patch(
        "src.admin.identity.identity_index_writer.IdentityIndexWriter",
        return_value=mock_writer,
    ):
        resp = await app_client.post(
            "/access/request",
            json={
                "proposed_tenant_id": "auto-tenant",
                "target_login": "auto-user",
                "provider": "github",
                "provider_user_id": "77777",
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "approved"
    assert data["tenant_id"] == "auto-tenant"
    assert data["redirect"] == "/dashboard"


# ---------------------------------------------------------------------------
# Admin approve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_admin_approve_creates_all_rows(admin_app_client, db_engine):
    """Admin approve creates org, tenant, dept, team, user, identities."""
    # Seed a pending request
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        req = TenantAccessRequest(
            id="req-001",
            cognito_sub="sub-to-approve",
            provider="github",
            provider_user_id="55555",
            proposed_tenant_id="approved-tenant",
            target_login="approveduser",
            status="pending",
        )
        session.add(req)
        await session.commit()

    mock_writer = MagicMock()
    mock_writer.put_user_identity = AsyncMock(return_value=True)

    with patch(
        "src.admin.identity.identity_index_writer.IdentityIndexWriter",
        return_value=mock_writer,
    ):
        resp = await admin_app_client.post("/admin/access-requests/req-001/approve")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "approved"
    assert data["tenant_id"] == "approved-tenant"

    # Verify rows were created
    async with factory() as session:
        from src.shared.models.onboarding import Tenant
        from src.shared.models.organization import Department, Team, User
        from src.shared.models.vault import UserIdentity

        org = await session.get(Organization, "approved-tenant")
        assert org is not None

        tenant = await session.get(Tenant, "approved-tenant")
        assert tenant is not None
        assert tenant.display_name == "approveduser"

        from sqlalchemy import select

        # Check department
        stmt = select(Department).where(Department.org_id == "approved-tenant")
        result = await session.execute(stmt)
        dept = result.scalar_one()
        assert dept.name == "Default"

        # Check team
        stmt = select(Team).where(Team.org_id == "approved-tenant")
        result = await session.execute(stmt)
        team = result.scalar_one()
        assert team.name == "Default"
        assert team.department_id == dept.id

        # Check user
        stmt = select(User).where(User.org_id == "approved-tenant")
        result = await session.execute(stmt)
        user = result.scalar_one()
        assert user.team_id == team.id
        assert user.cognito_sub == "sub-to-approve"

        # Check user identities (2: cognito + github)
        stmt = select(UserIdentity).where(UserIdentity.user_id == user.id)
        result = await session.execute(stmt)
        identities = result.scalars().all()
        assert len(identities) == 2
        providers = {i.provider for i in identities}
        assert providers == {"cognito", "github"}
        # All identities have the correct team_id
        for ident in identities:
            assert ident.team_id == team.id


# ---------------------------------------------------------------------------
# Admin approve — idempotent re-approve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_admin_approve_idempotent(admin_app_client, db_engine):
    """Approving an already-approved request returns existing tenant_id."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        req = TenantAccessRequest(
            id="req-idem",
            cognito_sub="sub-idem",
            provider="github",
            provider_user_id="44444",
            proposed_tenant_id="idem-tenant",
            target_login="idemuser",
            status="approved",
        )
        session.add(req)
        await session.commit()

    resp = await admin_app_client.post("/admin/access-requests/req-idem/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert resp.json()["tenant_id"] == "idem-tenant"


# ---------------------------------------------------------------------------
# Admin approve — 503 when flag off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "false"})
async def test_admin_approve_503_when_flag_off(admin_app_client, db_engine):
    """Approve returns 503 when USER_IDENTITY_INDEX_V2_WRITE is off."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        req = TenantAccessRequest(
            id="req-flag",
            cognito_sub="sub-flag",
            provider="github",
            provider_user_id="33333",
            proposed_tenant_id="flag-tenant",
            target_login="flaguser",
            status="pending",
        )
        session.add(req)
        await session.commit()

    resp = await admin_app_client.post("/admin/access-requests/req-flag/approve")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Admin deny — calls AdminDeleteUser
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true", "COGNITO_USER_POOL_ID": "us-east-1_testpool"})
async def test_admin_deny_deletes_cognito_user(admin_app_client, db_engine):
    """Deny calls AdminDeleteUser on the Cognito sub."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        req = TenantAccessRequest(
            id="req-deny",
            cognito_sub="sub-to-deny",
            provider="github",
            provider_user_id="22222",
            proposed_tenant_id="deny-tenant",
            target_login="denyuser",
            status="pending",
        )
        session.add(req)
        await session.commit()

    mock_cognito = MagicMock()
    mock_cognito.admin_delete_user = MagicMock(return_value={})

    with patch("boto3.client", return_value=mock_cognito):
        resp = await admin_app_client.post("/admin/access-requests/req-deny/deny")

    assert resp.status_code == 200
    assert resp.json()["status"] == "denied"
    mock_cognito.admin_delete_user.assert_called_once_with(
        UserPoolId="us-east-1_testpool",
        Username="sub-to-deny",
    )


# ---------------------------------------------------------------------------
# Admin deny — UserNotFoundException treated as success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true", "COGNITO_USER_POOL_ID": "us-east-1_testpool"})
async def test_admin_deny_user_not_found_is_idempotent(admin_app_client, db_engine):
    """Deny treats UserNotFoundException as idempotent success."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        req = TenantAccessRequest(
            id="req-deny2",
            cognito_sub="sub-already-gone",
            provider="github",
            provider_user_id="11111",
            proposed_tenant_id="deny2-tenant",
            target_login="deny2user",
            status="pending",
        )
        session.add(req)
        await session.commit()

    mock_cognito = MagicMock()
    error_response = {"Error": {"Code": "UserNotFoundException", "Message": "User not found"}}
    mock_cognito.admin_delete_user = MagicMock(side_effect=type("ClientError", (Exception,), {"response": error_response})())

    with patch("boto3.client", return_value=mock_cognito):
        resp = await admin_app_client.post("/admin/access-requests/req-deny2/deny")

    # Should succeed — UserNotFoundException is idempotent
    assert resp.status_code == 200
    assert resp.json()["status"] == "denied"


# ---------------------------------------------------------------------------
# Admin deny — Cognito failure emits metric
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true", "COGNITO_USER_POOL_ID": "us-east-1_testpool"})
async def test_admin_deny_cognito_failure_emits_metric(admin_app_client, db_engine):
    """Deny emits OnboardingDeny.CognitoDeleteFailure on non-UserNotFound errors."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        req = TenantAccessRequest(
            id="req-deny3",
            cognito_sub="sub-fail",
            provider="github",
            provider_user_id="00001",
            proposed_tenant_id="deny3-tenant",
            target_login="deny3user",
            status="pending",
        )
        session.add(req)
        await session.commit()

    mock_cognito = MagicMock()
    # Non-UserNotFoundException error
    error_response = {"Error": {"Code": "InternalErrorException", "Message": "Service error"}}
    exc = type("ClientError", (Exception,), {"response": error_response})()
    mock_cognito.admin_delete_user = MagicMock(side_effect=exc)

    with (
        patch("boto3.client", return_value=mock_cognito),
        patch("src.admin.onboarding.approval._emit_metric") as mock_metric,
    ):
        resp = await admin_app_client.post("/admin/access-requests/req-deny3/deny")

    assert resp.status_code == 200
    assert resp.json()["status"] == "denied"
    mock_metric.assert_called_with("ADP/Onboarding", "OnboardingDeny.CognitoDeleteFailure")


# ---------------------------------------------------------------------------
# DDB write failure — best-effort (metric emitted, user sees 200)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_ddb_write_failure_best_effort(admin_app_client, db_engine):
    """DDB write failure post-commit still returns 200; metric emitted."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        req = TenantAccessRequest(
            id="req-ddb",
            cognito_sub="sub-ddb",
            provider="github",
            provider_user_id="66666",
            proposed_tenant_id="ddb-tenant",
            target_login="ddbuser",
            status="pending",
        )
        session.add(req)
        await session.commit()

    mock_writer = MagicMock()
    mock_writer.put_user_identity = AsyncMock(side_effect=Exception("DDB timeout"))

    with (
        patch(
            "src.admin.identity.identity_index_writer.IdentityIndexWriter",
            return_value=mock_writer,
        ),
        patch("src.admin.onboarding.approval._emit_metric") as mock_metric,
    ):
        resp = await admin_app_client.post("/admin/access-requests/req-ddb/approve")

    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    # Metric should have been emitted for DDB failure
    mock_metric.assert_called_with("ADP/Onboarding", "OnboardingApproval.DdbWriteFailure")
