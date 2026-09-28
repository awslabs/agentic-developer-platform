"""Tests for D5 multi-tenant membership creation on login.

Issue #2953: Verifies that users join ALL matching org tenants (not just the first),
role mapping (D4), re-login additive behavior (D7), and username-slug fallback (D6).
"""

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.models.base import Base, new_uuid
from src.shared.models.onboarding import TenantAccessRequest, TenantMembership
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


def _mock_github_client(membership_map: dict[str, bool] | None = None, role: str = "member"):
    """Create a mock GitHub client.

    Args:
        membership_map: dict mapping org_name to membership boolean.
            If None, all orgs return True.
        role: GitHub org role to return ("member" or "admin").
    """
    mock_client = MagicMock()

    async def check_membership(installation_id, org_login, username):
        if membership_map is None:
            return True
        return membership_map.get(org_login, False)

    mock_client.check_org_membership = AsyncMock(side_effect=check_membership)
    mock_client.get_installation_token = AsyncMock(return_value="fake-token")
    mock_client.aclose = AsyncMock()
    mock_client._http_client = MagicMock()
    mock_client._http_client.get = AsyncMock(return_value=MagicMock(status_code=200, json=lambda: {"role": role}))
    return mock_client


async def _seed_org_with_team(factory, org_id: str, org_name: str, install_ids: list[str], created_at=None):
    """Seed an organization with a department and team."""
    async with factory() as session:
        org = Organization(
            id=org_id,
            name=org_name,
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=install_ids,
            member_approval_policy="auto_approve_org_members",
        )
        if created_at:
            org.created_at = created_at
        session.add(org)
        dept = Department(id=new_uuid(), org_id=org_id, name="Default")
        session.add(dept)
        team = Team(id=new_uuid(), org_id=org_id, department_id=dept.id, name="Default")
        session.add(team)
        await session.commit()


# ---------------------------------------------------------------------------
# Test 1: Single org match creates TenantMembership row with correct role
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_single_org_match_creates_membership(app_client, db_engine):
    """Verified org member -> tenant_memberships row created with correct role."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    await _seed_org_with_team(factory, "acme", "acme", ["12345"])

    mock_client = _mock_github_client(role="member")

    with (
        patch("src.admin.connections.github_client.GitHubAppClient", return_value=mock_client),
        patch("src.admin.connections.service._get_github_app_credentials", return_value=("app-id", "fake-pem")),
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

    # Verify TenantMembership row was created
    async with factory() as session:
        stmt = select(User).where(User.cognito_sub == "cognito-sub-new-user")
        result = await session.execute(stmt)
        user = result.scalar_one()

        stmt = select(TenantMembership).where(TenantMembership.user_id == user.id)
        result = await session.execute(stmt)
        memberships = result.scalars().all()
        assert len(memberships) == 1
        assert memberships[0].tenant_id == "acme"
        assert memberships[0].role == "member"
        assert memberships[0].is_active is True
        assert memberships[0].joined_via == "org_membership"


# ---------------------------------------------------------------------------
# Test 2: GitHub org admin maps to role='org_admin' in membership
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_github_org_admin_maps_to_org_admin_role(app_client, db_engine):
    """GitHub-org-admin -> role='org_admin' in membership."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    await _seed_org_with_team(factory, "acme", "acme", ["12345"])

    mock_client = _mock_github_client(role="admin")

    with (
        patch("src.admin.connections.github_client.GitHubAppClient", return_value=mock_client),
        patch("src.admin.connections.service._get_github_app_credentials", return_value=("app-id", "fake-pem")),
    ):
        resp = await app_client.post(
            "/access/request",
            json={"motivation": "joining acme as admin"},
            headers={"Authorization": _fake_bearer(_github_claims("alice", "30003"))},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"

    async with factory() as session:
        stmt = select(User).where(User.cognito_sub == "cognito-sub-new-user")
        result = await session.execute(stmt)
        user = result.scalar_one()

        stmt = select(TenantMembership).where(TenantMembership.user_id == user.id)
        result = await session.execute(stmt)
        memberships = result.scalars().all()
        assert len(memberships) == 1
        assert memberships[0].role == "org_admin"


# ---------------------------------------------------------------------------
# Test 3: User in 2 orgs -> 2 tenant_memberships rows; first is_active=true
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_user_in_two_orgs_creates_two_memberships(app_client, db_engine):
    """User in 2 orgs -> 2 tenant_memberships rows; first (by created_at) is is_active=true."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # Seed two orgs with different created_at to ensure deterministic ordering
    t1 = datetime(2024, 1, 1, tzinfo=UTC)
    t2 = datetime(2024, 6, 1, tzinfo=UTC)
    await _seed_org_with_team(factory, "alpha-org", "alpha-org", ["11111"], created_at=t1)
    await _seed_org_with_team(factory, "beta-org", "beta-org", ["22222"], created_at=t2)

    mock_client = _mock_github_client(membership_map={"alpha-org": True, "beta-org": True}, role="member")

    with (
        patch("src.admin.connections.github_client.GitHubAppClient", return_value=mock_client),
        patch("src.admin.connections.service._get_github_app_credentials", return_value=("app-id", "fake-pem")),
    ):
        resp = await app_client.post(
            "/access/request",
            json={"motivation": "joining both"},
            headers={"Authorization": _fake_bearer(_github_claims("multiuser", "40004"))},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "approved"
    # First match by created_at is alpha-org
    assert data["tenant_id"] == "alpha-org"

    async with factory() as session:
        stmt = select(User).where(User.cognito_sub == "cognito-sub-new-user")
        result = await session.execute(stmt)
        user = result.scalar_one()

        stmt = select(TenantMembership).where(TenantMembership.user_id == user.id)
        result = await session.execute(stmt)
        memberships = result.scalars().all()
        assert len(memberships) == 2

        # Sort by tenant_id for predictable assertions
        by_tenant = {m.tenant_id: m for m in memberships}
        assert "alpha-org" in by_tenant
        assert "beta-org" in by_tenant
        assert by_tenant["alpha-org"].is_active is True
        assert by_tenant["beta-org"].is_active is False
        assert by_tenant["alpha-org"].joined_via == "org_membership"
        assert by_tenant["beta-org"].joined_via == "org_membership"


# ---------------------------------------------------------------------------
# Test 4: Re-login with existing membership skips duplicates (D7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_relogin_skips_existing_memberships(app_client, db_engine):
    """User in 3 orgs, one already has membership (re-login) -> only 2 new rows; existing untouched."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    t1 = datetime(2024, 1, 1, tzinfo=UTC)
    t2 = datetime(2024, 6, 1, tzinfo=UTC)
    t3 = datetime(2024, 9, 1, tzinfo=UTC)
    await _seed_org_with_team(factory, "org-a", "org-a", ["11111"], created_at=t1)
    await _seed_org_with_team(factory, "org-b", "org-b", ["22222"], created_at=t2)
    await _seed_org_with_team(factory, "org-c", "org-c", ["33333"], created_at=t3)

    # Pre-create user and an existing membership for org-a (simulating prior login)
    async with factory() as session:
        user = User(
            id="user-existing-1",
            org_id="org-a",
            team_id=(await session.execute(select(Team).where(Team.org_id == "org-a"))).scalar_one().id,
            email="existing@test.com",
            name="existinguser",
            cognito_sub="cognito-sub-new-user",
            role="member",
        )
        session.add(user)
        membership = TenantMembership(
            user_id="user-existing-1",
            tenant_id="org-a",
            role="member",
            is_active=True,
            joined_via="org_membership",
        )
        session.add(membership)
        await session.commit()

    mock_client = _mock_github_client(
        membership_map={"org-a": True, "org-b": True, "org-c": True},
        role="member",
    )

    with (
        patch("src.admin.connections.github_client.GitHubAppClient", return_value=mock_client),
        patch("src.admin.connections.service._get_github_app_credentials", return_value=("app-id", "fake-pem")),
    ):
        # The user already exists (cognito_sub match), so get_access_status would
        # return "registered". We need to test the membership creation logic directly.
        # Since submit_access_request checks for existing User first via idempotency,
        # we test the internal function directly.
        from src.admin.onboarding.handler import (
            MatchedTenant,
            _create_memberships_for_matches,
        )

        async with factory() as session:
            matched = [
                MatchedTenant(org_id="org-a", org_name="org-a", install_id=11111),
                MatchedTenant(org_id="org-b", org_name="org-b", install_id=22222),
                MatchedTenant(org_id="org-c", org_name="org-c", install_id=33333),
            ]
            await _create_memberships_for_matches(
                db=session,
                user_id="user-existing-1",
                matched_tenants=matched,
                github_login="existinguser",
            )
            await session.commit()

    # Verify: 3 total memberships (1 existing + 2 new), existing untouched
    async with factory() as session:
        stmt = select(TenantMembership).where(TenantMembership.user_id == "user-existing-1")
        result = await session.execute(stmt)
        memberships = result.scalars().all()
        assert len(memberships) == 3

        by_tenant = {m.tenant_id: m for m in memberships}
        # Original membership unchanged
        assert by_tenant["org-a"].is_active is True
        # New memberships have is_active=False (user already has an active one)
        assert by_tenant["org-b"].is_active is False
        assert by_tenant["org-c"].is_active is False


# ---------------------------------------------------------------------------
# Test 5: No org match -> username-slug fallback (D6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_no_org_match_uses_username_slug(app_client, db_engine):
    """Non-member of any org -> username-slug tenant via _pick_tenant_id (D6)."""
    # No orgs seeded -> _find_matching_tenants_for_user returns []
    resp = await app_client.post(
        "/access/request",
        json={"motivation": "solo user"},
        headers={"Authorization": _fake_bearer(_github_claims("solouser", "50005"))},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending"
    assert data["request_id"] is not None

    # Verify proposed_tenant_id is the slugified username
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        req = await session.get(TenantAccessRequest, data["request_id"])
        assert req is not None
        assert req.proposed_tenant_id == "solouser"


# ---------------------------------------------------------------------------
# Test 6: GitHub API failure for one org -> that org skipped, others matched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_github_api_failure_skips_failing_org(app_client, db_engine):
    """GitHub API failure for one org -> that org skipped, others still matched."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    t1 = datetime(2024, 1, 1, tzinfo=UTC)
    t2 = datetime(2024, 6, 1, tzinfo=UTC)
    await _seed_org_with_team(factory, "good-org", "good-org", ["11111"], created_at=t1)
    await _seed_org_with_team(factory, "bad-org", "bad-org", ["22222"], created_at=t2)

    mock_client = MagicMock()
    call_count = {"n": 0}

    async def selective_membership(installation_id, org_login, username):
        call_count["n"] += 1
        if org_login == "bad-org":
            raise Exception("API timeout")
        return True

    mock_client.check_org_membership = AsyncMock(side_effect=selective_membership)
    mock_client.get_installation_token = AsyncMock(return_value="fake-token")
    mock_client.aclose = AsyncMock()
    mock_client._http_client = MagicMock()
    mock_client._http_client.get = AsyncMock(return_value=MagicMock(status_code=200, json=lambda: {"role": "member"}))

    with (
        patch("src.admin.connections.github_client.GitHubAppClient", return_value=mock_client),
        patch("src.admin.connections.service._get_github_app_credentials", return_value=("app-id", "fake-pem")),
    ):
        resp = await app_client.post(
            "/access/request",
            json={"motivation": "testing partial failure"},
            headers={"Authorization": _fake_bearer(_github_claims("partialuser", "60006"))},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "approved"
    # good-org matched, bad-org was skipped due to API failure
    assert data["tenant_id"] == "good-org"

    # Verify only good-org membership created
    async with factory() as session:
        stmt = select(User).where(User.cognito_sub == "cognito-sub-new-user")
        result = await session.execute(stmt)
        user = result.scalar_one()

        stmt = select(TenantMembership).where(TenantMembership.user_id == user.id)
        result = await session.execute(stmt)
        memberships = result.scalars().all()
        assert len(memberships) == 1
        assert memberships[0].tenant_id == "good-org"


# ---------------------------------------------------------------------------
# Test 7: D7 — user has username membership, org registered, re-login adds org
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_d7_username_membership_plus_new_org(db_engine):
    """D7: user has username membership -> org registered -> re-login adds org alongside."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # Seed the org (newly registered)
    await _seed_org_with_team(factory, "new-org", "new-org", ["44444"])

    # Pre-create user with username tenant and membership
    async with factory() as session:
        # Create the username org
        username_org = Organization(
            id="myuser",
            name="myuser",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[],
        )
        session.add(username_org)
        dept = Department(id=new_uuid(), org_id="myuser", name="Default")
        session.add(dept)
        team = Team(id=new_uuid(), org_id="myuser", department_id=dept.id, name="Default")
        session.add(team)
        await session.commit()

        user = User(
            id="user-d7-test",
            org_id="myuser",
            team_id=team.id,
            email="myuser@github.onboard",
            name="myuser",
            cognito_sub="cognito-sub-d7",
            role="org_admin",
        )
        session.add(user)
        membership = TenantMembership(
            user_id="user-d7-test",
            tenant_id="myuser",
            role="org_admin",
            is_active=True,
            joined_via="username_self",
        )
        session.add(membership)
        await session.commit()

    # Test _create_memberships_for_matches with the new org
    from src.admin.onboarding.handler import MatchedTenant, _create_memberships_for_matches

    mock_client = _mock_github_client(role="member")

    with (
        patch("src.admin.connections.github_client.GitHubAppClient", return_value=mock_client),
        patch("src.admin.connections.service._get_github_app_credentials", return_value=("app-id", "fake-pem")),
    ):
        async with factory() as session:
            matched = [MatchedTenant(org_id="new-org", org_name="new-org", install_id=44444)]
            await _create_memberships_for_matches(
                db=session,
                user_id="user-d7-test",
                matched_tenants=matched,
                github_login="myuser",
            )
            await session.commit()

    # Verify: username membership unchanged (still is_active=True),
    # new org membership added alongside (is_active=False because user already has active)
    async with factory() as session:
        stmt = select(TenantMembership).where(TenantMembership.user_id == "user-d7-test")
        result = await session.execute(stmt)
        memberships = result.scalars().all()
        assert len(memberships) == 2

        by_tenant = {m.tenant_id: m for m in memberships}
        # Username membership unchanged
        assert by_tenant["myuser"].is_active is True
        assert by_tenant["myuser"].joined_via == "username_self"
        # New org membership added
        assert by_tenant["new-org"].is_active is False
        assert by_tenant["new-org"].joined_via == "org_membership"
        assert by_tenant["new-org"].role == "member"


# ---------------------------------------------------------------------------
# Test 8: _find_matching_tenants_for_user returns all matches ordered by created_at
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_matching_tenants_returns_all_ordered(db_engine):
    """_find_matching_tenants_for_user returns ALL matches ordered by created_at."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # Seed 3 orgs with different created_at
    t1 = datetime(2024, 1, 1, tzinfo=UTC)
    t2 = datetime(2024, 3, 1, tzinfo=UTC)
    t3 = datetime(2024, 6, 1, tzinfo=UTC)
    await _seed_org_with_team(factory, "first-org", "first-org", ["111"], created_at=t1)
    await _seed_org_with_team(factory, "second-org", "second-org", ["222"], created_at=t2)
    await _seed_org_with_team(factory, "third-org", "third-org", ["333"], created_at=t3)

    mock_client = _mock_github_client(membership_map={"first-org": True, "second-org": True, "third-org": True})

    with (
        patch("src.admin.connections.github_client.GitHubAppClient", return_value=mock_client),
        patch("src.admin.connections.service._get_github_app_credentials", return_value=("app-id", "fake-pem")),
    ):
        from src.admin.onboarding.handler import _find_matching_tenants_for_user

        async with factory() as session:
            results = await _find_matching_tenants_for_user(session, "testuser")

    assert len(results) == 3
    assert results[0].org_id == "first-org"
    assert results[1].org_id == "second-org"
    assert results[2].org_id == "third-org"


# ---------------------------------------------------------------------------
# Test 9: _find_matching_tenants_for_user returns empty when no credentials
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_matching_tenants_no_credentials(db_engine):
    """No GitHub App credentials -> returns empty list (not error)."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    await _seed_org_with_team(factory, "some-org", "some-org", ["999"])

    with patch(
        "src.admin.connections.service._get_github_app_credentials",
        return_value=("", ""),
    ):
        from src.admin.onboarding.handler import _find_matching_tenants_for_user

        async with factory() as session:
            results = await _find_matching_tenants_for_user(session, "testuser")

    assert results == []
