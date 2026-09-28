"""Tests for multi-user tenant matching in the onboarding handler.

Issue #719: Verifies that users from the same GitHub org are attached to the
existing ADP tenant instead of creating a new one.
"""

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.models.base import Base, new_uuid
from src.shared.models.onboarding import TenantAccessRequest
from src.shared.models.organization import Department, Organization, Team, User
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
async def app_client(db_engine, new_user_context):
    """Create test client with auth override and DB override."""
    from httpx import ASGITransport, AsyncClient

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


def _fake_bearer(claims: dict) -> str:
    """Build an unsigned Bearer token whose base64 payload decodes to claims."""
    import base64 as _b64
    import json as _json

    payload = _b64.urlsafe_b64encode(_json.dumps(claims).encode("utf-8")).decode("ascii").rstrip("=")
    return f"Bearer header.{payload}.signature"


def _github_claims(login: str, numeric_id: str) -> dict:
    return {
        "custom:github_username": login,
        "cognito:username": f"github_{numeric_id}",
    }


# ---------------------------------------------------------------------------
# Test 1: First user from new org creates a fresh tenant (no match)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_first_user_from_new_org_creates_tenant(app_client, db_engine):
    """Sign-in flow with no existing org -> creates tenant, user becomes admin."""
    # No orgs with github_installation_ids set => _find_matching_tenant_for_user returns None
    # => falls through to slug-based new-tenant creation
    resp = await app_client.post(
        "/access/request",
        json={"motivation": "I want to join"},
        headers={"Authorization": _fake_bearer(_github_claims("newuser", "10001"))},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending"
    assert data["request_id"] is not None

    # Verify the stored row has proposed_tenant_id matching slugified login
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        req = await session.get(TenantAccessRequest, data["request_id"])
        assert req is not None
        assert req.proposed_tenant_id == "newuser"


# ---------------------------------------------------------------------------
# Test 2: Second user from same org attaches to existing tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_second_user_from_same_org_attaches_to_existing(app_client, db_engine):
    """Sign-in flow when org has installation -> user joins existing tenant as member."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # Seed an existing org with a GitHub installation + a team
    async with factory() as session:
        org = Organization(
            id="acme",
            name="acme",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=["12345"],
            member_approval_policy="auto_approve_org_members",
        )
        session.add(org)
        dept = Department(id=new_uuid(), org_id="acme", name="Default")
        session.add(dept)
        team = Team(id=new_uuid(), org_id="acme", department_id=dept.id, name="Default")
        session.add(team)
        await session.commit()

    # Mock GitHub API to confirm membership
    mock_client = MagicMock()
    mock_client.check_org_membership = AsyncMock(return_value=True)
    mock_client.get_installation_token = AsyncMock(return_value="fake-token")
    mock_client.aclose = AsyncMock()
    mock_client._http_client = MagicMock()
    mock_client._http_client.get = AsyncMock(return_value=MagicMock(status_code=200, json=lambda: {"role": "member"}))

    with (
        patch(
            "src.admin.connections.github_client.GitHubAppClient",
            return_value=mock_client,
        ),
        patch(
            "src.admin.connections.service._get_github_app_credentials",
            return_value=("app-id", "fake-pem"),
        ),
    ):
        resp = await app_client.post(
            "/access/request",
            json={"motivation": "joining acme"},
            headers={"Authorization": _fake_bearer(_github_claims("bob", "20002"))},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "approved"
    assert data["tenant_id"] == "acme"

    # Verify user row was created with org_id = "acme"
    async with factory() as session:
        stmt = select(User).where(User.cognito_sub == "cognito-sub-new-user")
        result = await session.execute(stmt)
        user = result.scalar_one()
        assert user.org_id == "acme"
        assert user.role == "member"

        # Verify TenantAccessRequest row was created with status="approved"
        stmt = select(TenantAccessRequest).where(TenantAccessRequest.cognito_sub == "cognito-sub-new-user")
        result = await session.execute(stmt)
        req = result.scalar_one()
        assert req.status == "approved"
        assert req.decided_by == "system:org-member-match"


# ---------------------------------------------------------------------------
# Test 3: Match uses install_id, not slug
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_match_by_install_id_not_slug(app_client, db_engine):
    """Match succeeds even though user's slug != org name. Proves algorithm uses install_id."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        org = Organization(
            id="acme-corp",
            name="acme-corp",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=["99999"],
            member_approval_policy="auto_approve_org_members",
        )
        session.add(org)
        dept = Department(id=new_uuid(), org_id="acme-corp", name="Default")
        session.add(dept)
        team = Team(id=new_uuid(), org_id="acme-corp", department_id=dept.id, name="Default")
        session.add(team)
        await session.commit()

    mock_client = MagicMock()
    mock_client.check_org_membership = AsyncMock(return_value=True)
    mock_client.get_installation_token = AsyncMock(return_value="fake-token")
    mock_client.aclose = AsyncMock()
    mock_client._http_client = MagicMock()
    mock_client._http_client.get = AsyncMock(return_value=MagicMock(status_code=200, json=lambda: {"role": "member"}))

    with (
        patch(
            "src.admin.connections.github_client.GitHubAppClient",
            return_value=mock_client,
        ),
        patch(
            "src.admin.connections.service._get_github_app_credentials",
            return_value=("app-id", "fake-pem"),
        ),
    ):
        resp = await app_client.post(
            "/access/request",
            json={"motivation": "joining"},
            headers={"Authorization": _fake_bearer(_github_claims("alice", "30003"))},
        )

    assert resp.status_code == 200
    data = resp.json()
    # "alice" slug != "acme-corp" org name, but match succeeded via install_id
    assert data["status"] == "approved"
    assert data["tenant_id"] == "acme-corp"


# ---------------------------------------------------------------------------
# Test 4: Membership API failure falls through to new-tenant creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_membership_api_failure_falls_through(app_client, db_engine):
    """GitHub API error -> match returns None -> user falls through to new-tenant creation."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        org = Organization(
            id="failorg",
            name="failorg",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=["77777"],
            member_approval_policy="auto_approve_org_members",
        )
        session.add(org)
        await session.commit()

    mock_client = MagicMock()
    mock_client.check_org_membership = AsyncMock(side_effect=Exception("API timeout"))
    mock_client.aclose = AsyncMock()

    with (
        patch(
            "src.admin.connections.github_client.GitHubAppClient",
            return_value=mock_client,
        ),
        patch(
            "src.admin.connections.service._get_github_app_credentials",
            return_value=("app-id", "fake-pem"),
        ),
    ):
        resp = await app_client.post(
            "/access/request",
            json={"motivation": "testing"},
            headers={"Authorization": _fake_bearer(_github_claims("failuser", "40004"))},
        )

    assert resp.status_code == 200
    data = resp.json()
    # Falls through to new-tenant creation (pending), not auto-attached
    assert data["status"] == "pending"
    assert data["request_id"] is not None

    # Verify no user was created with org_id="failorg"
    async with factory() as session:
        stmt = select(User).where(User.org_id == "failorg")
        result = await session.execute(stmt)
        assert result.scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# Test 5: require_admin_approval policy creates pending request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_require_admin_approval_policy_creates_pending(app_client, db_engine):
    """Org with require_admin_approval -> membership verified but user stays pending."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        org = Organization(
            id="strict-org",
            name="strict-org",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=["55555"],
            member_approval_policy="require_admin_approval",
        )
        session.add(org)
        dept = Department(id=new_uuid(), org_id="strict-org", name="Default")
        session.add(dept)
        team = Team(id=new_uuid(), org_id="strict-org", department_id=dept.id, name="Default")
        session.add(team)
        await session.commit()

    mock_client = MagicMock()
    mock_client.check_org_membership = AsyncMock(return_value=True)
    mock_client.aclose = AsyncMock()

    with (
        patch(
            "src.admin.connections.github_client.GitHubAppClient",
            return_value=mock_client,
        ),
        patch(
            "src.admin.connections.service._get_github_app_credentials",
            return_value=("app-id", "fake-pem"),
        ),
    ):
        resp = await app_client.post(
            "/access/request",
            json={"motivation": "strict org"},
            headers={"Authorization": _fake_bearer(_github_claims("strictuser", "50005"))},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending"
    assert data["request_id"] is not None

    # Verify NO user row was created (pending until admin approves)
    async with factory() as session:
        stmt = select(User).where(User.cognito_sub == "cognito-sub-new-user")
        result = await session.execute(stmt)
        assert result.scalar_one_or_none() is None

        # But a TenantAccessRequest was created with status=pending
        stmt = select(TenantAccessRequest).where(TenantAccessRequest.cognito_sub == "cognito-sub-new-user")
        result = await session.execute(stmt)
        req = result.scalar_one()
        assert req.status == "pending"
        assert req.proposed_tenant_id == "strict-org"


# ---------------------------------------------------------------------------
# Test 6: install_callback appends to github_installation_ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_callback_appends_to_github_installation_ids(db_engine):
    """Completing a GitHub App install appends install_id to org.github_installation_ids."""
    from src.admin.connections.service import _append_installation_id_to_org

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # Seed an org with empty installation_ids
    async with factory() as session:
        org = Organization(
            id="myorg",
            name="myorg",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[],
        )
        session.add(org)
        await session.commit()

    # Append an installation ID
    async with factory() as session:
        await _append_installation_id_to_org(
            installation_id=12345,
            caller_org_id="myorg",
            db=session,
        )

    # Verify it was appended
    async with factory() as session:
        org = await session.get(Organization, "myorg")
        assert "12345" in org.github_installation_ids

    # Verify idempotency — append same ID again
    async with factory() as session:
        await _append_installation_id_to_org(
            installation_id=12345,
            caller_org_id="myorg",
            db=session,
        )

    async with factory() as session:
        org = await session.get(Organization, "myorg")
        assert org.github_installation_ids.count("12345") == 1

    # Append a different ID
    async with factory() as session:
        await _append_installation_id_to_org(
            installation_id=67890,
            caller_org_id="myorg",
            db=session,
        )

    async with factory() as session:
        org = await session.get(Organization, "myorg")
        assert "12345" in org.github_installation_ids
        assert "67890" in org.github_installation_ids
