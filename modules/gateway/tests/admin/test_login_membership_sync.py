"""Tests for login-time membership sync (Issue #3017).

Verifies that existing users get joined to org tenants created after their
initial onboarding, via the matcher wired into GET /access/status.
"""

import base64
import json
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.shared.models.base import Base, new_uuid
from src.shared.models.onboarding import TenantMembership
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


def _fake_bearer(claims: dict) -> str:
    """Build an unsigned Bearer token whose base64 payload decodes to claims."""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).decode("ascii").rstrip("=")
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
        return team


async def _seed_existing_user(factory, user_id: str, org_id: str, team_id: str, cognito_sub: str, github_login: str):
    """Seed an existing user in the database."""
    async with factory() as session:
        user = User(
            id=user_id,
            org_id=org_id,
            team_id=team_id,
            email=f"{github_login}@github.onboard",
            name=github_login,
            cognito_sub=cognito_sub,
            role="member",
        )
        session.add(user)
        await session.commit()
        return user


# ---------------------------------------------------------------------------
# Test 1: Existing user + org tenant with matching org -> login -> membership created
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_login_creates_membership_for_matching_org(db_engine):
    """Existing user logs in, org tenant exists with matching GitHub org -> membership row created."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # Seed existing user's personal org (no install IDs)
    async with factory() as session:
        personal_org = Organization(
            id="personal-org",
            name="personal-org",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[],
        )
        session.add(personal_org)
        dept = Department(id=new_uuid(), org_id="personal-org", name="Default")
        session.add(dept)
        team = Team(id=new_uuid(), org_id="personal-org", department_id=dept.id, name="Default")
        session.add(team)
        await session.commit()

        # Create existing user
        user = User(
            id="user-existing-login",
            org_id="personal-org",
            team_id=team.id,
            email="loginuser@github.onboard",
            name="loginuser",
            cognito_sub="cognito-sub-login-user",
            role="member",
        )
        session.add(user)
        await session.commit()

    # Seed org tenant created AFTER user onboarded
    await _seed_org_with_team(factory, "new-corp", "new-corp", ["77777"])

    # Build the app client with the existing user's token context
    existing_user_context = TokenContext(
        user_id="cognito-sub-login-user",
        org_id="personal-org",
        team_id="team-1",
        department_id="dept-1",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    from httpx import ASGITransport, AsyncClient

    from src.app import create_app
    from src.auth.dependencies import get_current_user
    from src.shared.database import get_db

    app = create_app()

    async def override_db():
        async with factory() as session:
            yield session

    async def override_auth():
        return existing_user_context

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_auth

    mock_client = _mock_github_client(membership_map={"new-corp": True}, role="member")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with (
            patch("src.admin.connections.github_client.GitHubAppClient", return_value=mock_client),
            patch("src.admin.connections.service._get_github_app_credentials", return_value=("app-id", "fake-pem")),
        ):
            resp = await client.get(
                "/access/status",
                headers={"Authorization": _fake_bearer(_github_claims("loginuser", "90009"))},
            )

    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["status"] == "registered"

    # Verify TenantMembership row was created
    async with factory() as session:
        stmt = select(TenantMembership).where(TenantMembership.user_id == "user-existing-login")
        result = await session.execute(stmt)
        memberships = result.scalars().all()
        assert len(memberships) == 1
        assert memberships[0].tenant_id == "new-corp"
        assert memberships[0].role == "member"
        assert memberships[0].joined_via == "org_membership"


# ---------------------------------------------------------------------------
# Test 2: Idempotent — second login does not create duplicate rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_login_idempotent_no_duplicate_memberships(db_engine):
    """Second login with same user -> no duplicate membership rows (D7 guard)."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # Seed org tenant
    await _seed_org_with_team(factory, "corp-org", "corp-org", ["55555"])

    # Seed existing user + existing membership for that org
    async with factory() as session:
        org = Organization(
            id="home-org",
            name="home-org",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[],
        )
        session.add(org)
        dept = Department(id=new_uuid(), org_id="home-org", name="Default")
        session.add(dept)
        team = Team(id=new_uuid(), org_id="home-org", department_id=dept.id, name="Default")
        session.add(team)
        await session.commit()

        user = User(
            id="user-idem-1",
            org_id="home-org",
            team_id=team.id,
            email="idemuser@github.onboard",
            name="idemuser",
            cognito_sub="cognito-sub-idem",
            role="member",
        )
        session.add(user)
        # Pre-existing membership for corp-org (from prior login)
        membership = TenantMembership(
            user_id="user-idem-1",
            tenant_id="corp-org",
            role="member",
            is_active=True,
            joined_via="org_membership",
            github_org_id="corp-org",
        )
        session.add(membership)
        await session.commit()

    existing_user_context = TokenContext(
        user_id="cognito-sub-idem",
        org_id="home-org",
        team_id="team-1",
        department_id="dept-1",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    from httpx import ASGITransport, AsyncClient

    from src.app import create_app
    from src.auth.dependencies import get_current_user
    from src.shared.database import get_db

    app = create_app()

    async def override_db():
        async with factory() as session:
            yield session

    async def override_auth():
        return existing_user_context

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_auth

    mock_client = _mock_github_client(membership_map={"corp-org": True}, role="member")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with (
            patch("src.admin.connections.github_client.GitHubAppClient", return_value=mock_client),
            patch("src.admin.connections.service._get_github_app_credentials", return_value=("app-id", "fake-pem")),
        ):
            resp = await client.get(
                "/access/status",
                headers={"Authorization": _fake_bearer(_github_claims("idemuser", "10010"))},
            )

    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["status"] == "registered"

    # Verify still only 1 membership (no duplicates)
    async with factory() as session:
        stmt = select(TenantMembership).where(TenantMembership.user_id == "user-idem-1")
        result = await session.execute(stmt)
        memberships = result.scalars().all()
        assert len(memberships) == 1
        assert memberships[0].tenant_id == "corp-org"


# ---------------------------------------------------------------------------
# Test 3: No matching org -> login unchanged, no rows created
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_login_no_match_no_memberships_created(db_engine):
    """User not in any org -> login returns registered, no membership rows created."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # Seed org tenant where user is NOT a member
    await _seed_org_with_team(factory, "other-corp", "other-corp", ["88888"])

    # Seed existing user
    async with factory() as session:
        org = Organization(
            id="solo-org",
            name="solo-org",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[],
        )
        session.add(org)
        dept = Department(id=new_uuid(), org_id="solo-org", name="Default")
        session.add(dept)
        team = Team(id=new_uuid(), org_id="solo-org", department_id=dept.id, name="Default")
        session.add(team)
        await session.commit()

        user = User(
            id="user-no-match-1",
            org_id="solo-org",
            team_id=team.id,
            email="nomatch@github.onboard",
            name="nomatchuser",
            cognito_sub="cognito-sub-nomatch",
            role="member",
        )
        session.add(user)
        await session.commit()

    existing_user_context = TokenContext(
        user_id="cognito-sub-nomatch",
        org_id="solo-org",
        team_id="team-1",
        department_id="dept-1",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    from httpx import ASGITransport, AsyncClient

    from src.app import create_app
    from src.auth.dependencies import get_current_user
    from src.shared.database import get_db

    app = create_app()

    async def override_db():
        async with factory() as session:
            yield session

    async def override_auth():
        return existing_user_context

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_auth

    # Mock: user is NOT a member of other-corp
    mock_client = _mock_github_client(membership_map={"other-corp": False}, role="member")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with (
            patch("src.admin.connections.github_client.GitHubAppClient", return_value=mock_client),
            patch("src.admin.connections.service._get_github_app_credentials", return_value=("app-id", "fake-pem")),
        ):
            resp = await client.get(
                "/access/status",
                headers={"Authorization": _fake_bearer(_github_claims("nomatchuser", "20020"))},
            )

    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["status"] == "registered"

    # Verify no memberships created
    async with factory() as session:
        stmt = select(TenantMembership).where(TenantMembership.user_id == "user-no-match-1")
        result = await session.execute(stmt)
        memberships = result.scalars().all()
        assert len(memberships) == 0


# ---------------------------------------------------------------------------
# Test 4: User NOT in GitHub org -> NO membership (over-join guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_login_user_not_in_github_org_no_overjoin(db_engine):
    """User NOT in the GitHub org -> no membership created (over-join guard)."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # Seed two orgs: user is member of one, NOT the other
    t1 = datetime(2024, 1, 1, tzinfo=UTC)
    t2 = datetime(2024, 6, 1, tzinfo=UTC)
    await _seed_org_with_team(factory, "my-org", "my-org", ["11111"], created_at=t1)
    await _seed_org_with_team(factory, "not-my-org", "not-my-org", ["22222"], created_at=t2)

    # Seed existing user
    async with factory() as session:
        user_org = Organization(
            id="user-home",
            name="user-home",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[],
        )
        session.add(user_org)
        dept = Department(id=new_uuid(), org_id="user-home", name="Default")
        session.add(dept)
        team = Team(id=new_uuid(), org_id="user-home", department_id=dept.id, name="Default")
        session.add(team)
        await session.commit()

        user = User(
            id="user-guard-1",
            org_id="user-home",
            team_id=team.id,
            email="guarduser@github.onboard",
            name="guarduser",
            cognito_sub="cognito-sub-guard",
            role="member",
        )
        session.add(user)
        await session.commit()

    existing_user_context = TokenContext(
        user_id="cognito-sub-guard",
        org_id="user-home",
        team_id="team-1",
        department_id="dept-1",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    from httpx import ASGITransport, AsyncClient

    from src.app import create_app
    from src.auth.dependencies import get_current_user
    from src.shared.database import get_db

    app = create_app()

    async def override_db():
        async with factory() as session:
            yield session

    async def override_auth():
        return existing_user_context

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_auth

    # User is member of my-org but NOT not-my-org
    mock_client = _mock_github_client(membership_map={"my-org": True, "not-my-org": False}, role="member")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with (
            patch("src.admin.connections.github_client.GitHubAppClient", return_value=mock_client),
            patch("src.admin.connections.service._get_github_app_credentials", return_value=("app-id", "fake-pem")),
        ):
            resp = await client.get(
                "/access/status",
                headers={"Authorization": _fake_bearer(_github_claims("guarduser", "30030"))},
            )

    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["status"] == "registered"

    # Verify only my-org membership created, NOT not-my-org
    async with factory() as session:
        stmt = select(TenantMembership).where(TenantMembership.user_id == "user-guard-1")
        result = await session.execute(stmt)
        memberships = result.scalars().all()
        assert len(memberships) == 1
        assert memberships[0].tenant_id == "my-org"
        # not-my-org must NOT have a membership
        tenant_ids = {m.tenant_id for m in memberships}
        assert "not-my-org" not in tenant_ids


# ---------------------------------------------------------------------------
# Test 5: sync_memberships_on_login unit test — direct function call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_sync_memberships_on_login_direct(db_engine):
    """Direct call to sync_memberships_on_login creates expected memberships."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # Seed org tenants
    t1 = datetime(2024, 1, 1, tzinfo=UTC)
    t2 = datetime(2024, 6, 1, tzinfo=UTC)
    await _seed_org_with_team(factory, "org-x", "org-x", ["100"], created_at=t1)
    await _seed_org_with_team(factory, "org-y", "org-y", ["200"], created_at=t2)

    # Seed existing user with their own org
    async with factory() as session:
        user_org = Organization(
            id="my-personal",
            name="my-personal",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[],
        )
        session.add(user_org)
        dept = Department(id=new_uuid(), org_id="my-personal", name="Default")
        session.add(dept)
        team = Team(id=new_uuid(), org_id="my-personal", department_id=dept.id, name="Default")
        session.add(team)
        await session.commit()

        user = User(
            id="user-sync-1",
            org_id="my-personal",
            team_id=team.id,
            email="syncuser@github.onboard",
            name="syncuser",
            cognito_sub="cognito-sub-sync",
            role="member",
        )
        session.add(user)
        await session.commit()

    mock_client = _mock_github_client(membership_map={"org-x": True, "org-y": True}, role="member")

    with (
        patch("src.admin.connections.github_client.GitHubAppClient", return_value=mock_client),
        patch("src.admin.connections.service._get_github_app_credentials", return_value=("app-id", "fake-pem")),
    ):
        from src.admin.onboarding.handler import sync_memberships_on_login

        async with factory() as session:
            # Fetch the user
            stmt = select(User).where(User.id == "user-sync-1")
            result = await session.execute(stmt)
            user = result.scalar_one()

            await sync_memberships_on_login(session, user, "syncuser")

    # Verify memberships created for both orgs
    async with factory() as session:
        stmt = select(TenantMembership).where(TenantMembership.user_id == "user-sync-1")
        result = await session.execute(stmt)
        memberships = result.scalars().all()
        assert len(memberships) == 2

        by_tenant = {m.tenant_id: m for m in memberships}
        assert "org-x" in by_tenant
        assert "org-y" in by_tenant
        # First should be active (user has no prior active membership)
        assert by_tenant["org-x"].is_active is True
        assert by_tenant["org-y"].is_active is False


# ---------------------------------------------------------------------------
# Test 6: Onboarding path (submit_access_request) unaffected — regression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_onboarding_path_still_works(db_engine):
    """Regression: new user onboarding via POST /access/request still works."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    await _seed_org_with_team(factory, "onboard-org", "onboard-org", ["66666"])

    new_user_context = TokenContext(
        user_id="cognito-sub-brand-new",
        org_id="",
        team_id="",
        department_id="",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    from httpx import ASGITransport, AsyncClient

    from src.app import create_app
    from src.auth.dependencies import get_current_user
    from src.shared.database import get_db

    app = create_app()

    async def override_db():
        async with factory() as session:
            yield session

    async def override_auth():
        return new_user_context

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_auth

    mock_client = _mock_github_client(membership_map={"onboard-org": True}, role="member")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with (
            patch("src.admin.connections.github_client.GitHubAppClient", return_value=mock_client),
            patch("src.admin.connections.service._get_github_app_credentials", return_value=("app-id", "fake-pem")),
        ):
            resp = await client.post(
                "/access/request",
                json={"motivation": "new user onboarding"},
                headers={"Authorization": _fake_bearer(_github_claims("brandnew", "40040"))},
            )

    app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "approved"
    assert data["tenant_id"] == "onboard-org"

    # Verify user and membership created via onboarding path
    async with factory() as session:
        stmt = select(User).where(User.cognito_sub == "cognito-sub-brand-new")
        result = await session.execute(stmt)
        user = result.scalar_one()

        stmt = select(TenantMembership).where(TenantMembership.user_id == user.id)
        result = await session.execute(stmt)
        memberships = result.scalars().all()
        assert len(memberships) == 1
        assert memberships[0].tenant_id == "onboard-org"


# ---------------------------------------------------------------------------
# Test 7: Missing GitHub login in claims -> sync skipped gracefully
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.dict(os.environ, {"USER_IDENTITY_INDEX_V2_WRITE": "true"})
async def test_login_no_github_claims_skips_sync(db_engine):
    """If JWT claims don't contain github_username, sync is skipped (no error)."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # Seed org
    await _seed_org_with_team(factory, "skip-org", "skip-org", ["99999"])

    # Seed existing user
    async with factory() as session:
        org = Organization(
            id="user-org-skip",
            name="user-org-skip",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[],
        )
        session.add(org)
        dept = Department(id=new_uuid(), org_id="user-org-skip", name="Default")
        session.add(dept)
        team = Team(id=new_uuid(), org_id="user-org-skip", department_id=dept.id, name="Default")
        session.add(team)
        await session.commit()

        user = User(
            id="user-skip-1",
            org_id="user-org-skip",
            team_id=team.id,
            email="skipuser@github.onboard",
            name="skipuser",
            cognito_sub="cognito-sub-skip",
            role="member",
        )
        session.add(user)
        await session.commit()

    existing_user_context = TokenContext(
        user_id="cognito-sub-skip",
        org_id="user-org-skip",
        team_id="team-1",
        department_id="dept-1",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    from httpx import ASGITransport, AsyncClient

    from src.app import create_app
    from src.auth.dependencies import get_current_user
    from src.shared.database import get_db

    app = create_app()

    async def override_db():
        async with factory() as session:
            yield session

    async def override_auth():
        return existing_user_context

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_auth

    # Send a token WITHOUT github_username claim
    no_github_claims = {"cognito:username": "some_user", "sub": "cognito-sub-skip"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/access/status",
            headers={"Authorization": _fake_bearer(no_github_claims)},
        )

    app.dependency_overrides.clear()

    # Should still return registered without error
    assert resp.status_code == 200
    assert resp.json()["status"] == "registered"

    # No memberships created (sync was skipped)
    async with factory() as session:
        stmt = select(TenantMembership).where(TenantMembership.user_id == "user-skip-1")
        result = await session.execute(stmt)
        memberships = result.scalars().all()
        assert len(memberships) == 0
