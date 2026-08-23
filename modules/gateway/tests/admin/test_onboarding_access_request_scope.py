"""Issue #4018: org-scoped access-request approval.

The three access-request routes used to be gated on ``require_admin`` (PLATFORM
admin only — ``auth/dependencies.py`` deliberately excludes org_admin per #3981),
so an org's own administrator could neither see nor drain their own pending queue.
They are now gated on ``Permission.USER_MANAGE`` + an org-scope check resolved
from the caller's ``tenant_memberships`` row (#3987/#3998), never a token claim.

The load-bearing distinction these tests pin is the TWO REQUEST CLASSES:

  * class A — new-tenant: ``proposed_tenant_id`` names an org that does NOT exist
    yet (``_pick_tenant_id`` returns None on a slug collision, so this is true by
    construction). Stays platform-admin-only; hidden from an org admin's list.
  * class B — join-existing-org: ``proposed_tenant_id`` is a REAL org. The only
    class an org admin may decide.

Two escalation vectors are guarded explicitly:

  1. **Cross-tenant.** An org_admin must not list, approve, or deny another org's
     requests. Deny is destructive (``admin_delete_user``), so the deny tests
     assert on the Cognito mock — a 403 alone would not prove no account was
     deleted.
  2. **Peer-level privilege grant.** ``approve_request``'s existing-org branch
     hardcodes ``role="org_admin"``, and #3982's ceiling does NOT catch it
     (``ROLE_RANK["org_admin"]`` == ``CALLER_ROLE_RANK[ORG_ADMIN]`` == 2, and the
     check is ``>``, so 2 > 2 is False). The control is that the approval path
     DERIVES the role from GitHub org membership, so the granted role must be
     asserted directly — not assumed safe because a ceiling exists.

Fixture shape (per the #3021/#3027 lesson): contexts are ACCESS-token-shaped —
built as bare ``TokenContext`` objects with no ID-token/``custom:*`` claims. An
org_admin caller additionally needs real ``users`` + ``tenant_memberships`` rows,
because since #3987 authority comes from the DB; a token that merely *says*
org_admin resolves to MEMBER via the least-privilege fallback.
"""

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.models.base import Base, new_uuid
from src.shared.models.onboarding import TenantAccessRequest, TenantMembership
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.schemas.auth import TokenContext

pytestmark = pytest.mark.asyncio

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
V2_ON = {"USER_IDENTITY_INDEX_V2_WRITE": "true"}

# The org our org_admin caller administers, and a second org they must never reach.
OWN_ORG = "acme"
OTHER_ORG = "globex"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


def _context(user_id: str, org_id: str, *, is_admin: bool = False) -> TokenContext:
    """An ACCESS-token-shaped context: no ID-token claims, no custom:role."""
    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id="",
        department_id="",
        account_type="human",
        is_admin=is_admin,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


async def _seed_org(session: AsyncSession, org_id: str) -> Team:
    """Create an org with the default department + team an attach needs."""
    session.add(
        Organization(
            id=org_id,
            name=org_id,
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=["12345"],
            cognito_client_ids=[],
            member_approval_policy="require_admin_approval",
        )
    )
    dept = Department(id=new_uuid(), org_id=org_id, name="Default")
    session.add(dept)
    team = Team(id=new_uuid(), org_id=org_id, department_id=dept.id, name="Default")
    session.add(team)
    await session.flush()
    return team


async def _seed_admin(session: AsyncSession, *, cognito_sub: str, org_id: str, role: str, team_id: str) -> str:
    """Give ``cognito_sub`` a users row + membership row conferring ``role`` in ``org_id``.

    Both rows are required: TenantMembership.user_id FKs to users.id while the
    token carries the Cognito sub, so the resolver bridges via users.cognito_sub.
    """
    pg_user_id = new_uuid()
    session.add(
        User(
            id=pg_user_id,
            org_id=org_id,
            team_id=team_id,
            email=f"{cognito_sub}@example.test",
            name=cognito_sub,
            cognito_sub=cognito_sub,
            role=role,
        )
    )
    await session.flush()
    session.add(
        TenantMembership(
            id=new_uuid(),
            user_id=pg_user_id,
            tenant_id=org_id,
            role=role,
            is_active=True,
            joined_via="org_membership",
        )
    )
    await session.flush()
    return pg_user_id


def _pending(
    *,
    request_id: str,
    tenant_id: str,
    login: str,
    sub: str,
) -> TenantAccessRequest:
    return TenantAccessRequest(
        id=request_id,
        cognito_sub=sub,
        provider="github",
        provider_user_id=f"gh-{login}",
        proposed_tenant_id=tenant_id,
        target_login=login,
        motivation="please let me in",
        status="pending",
    )


@pytest.fixture
async def seeded(db_engine):
    """Both orgs, an org_admin of OWN_ORG, a dept_admin, and four pending requests.

    Requests: one class-B per org, plus two class-A (new-tenant) requests — one
    whose slug is unrelated and one whose slug deliberately equals OWN_ORG's
    *name-like* sibling, to prove the class test is "does the org exist", not a
    string comparison against the caller's org.
    """
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        own_team = await _seed_org(session, OWN_ORG)
        await _seed_org(session, OTHER_ORG)

        await _seed_admin(session, cognito_sub="sub-org-admin", org_id=OWN_ORG, role="org_admin", team_id=own_team.id)
        await _seed_admin(session, cognito_sub="sub-dept-admin", org_id=OWN_ORG, role="dept_admin", team_id=own_team.id)
        await _seed_admin(session, cognito_sub="sub-plain-member", org_id=OWN_ORG, role="member", team_id=own_team.id)

        # class B — join an org that exists
        session.add(_pending(request_id="req-own-b", tenant_id=OWN_ORG, login="joiner", sub="sub-joiner"))
        session.add(_pending(request_id="req-other-b", tenant_id=OTHER_ORG, login="outsider", sub="sub-outsider"))
        # class A — new tenant; these orgs do NOT exist
        session.add(_pending(request_id="req-new-a", tenant_id="brand-new-co", login="founder", sub="sub-founder"))
        session.add(_pending(request_id="req-new-a2", tenant_id="another-new-co", login="founder2", sub="sub-founder2"))

        await session.commit()
    return db_engine


def _client(db_engine, context: TokenContext) -> AsyncClient:
    from src.app import create_app
    from src.auth.dependencies import get_current_user
    from src.shared.database import get_db

    app = create_app()
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_db():
        async with factory() as session:
            yield session

    async def override_auth():
        return context

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_auth
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _github_role_client(role: str):
    """Mock GitHubAppClient whose org-membership lookup reports ``role``."""
    client = MagicMock()
    client.get_installation_token = AsyncMock(return_value="fake-token")
    client.aclose = AsyncMock()
    client._http_client = MagicMock()
    client._http_client.get = AsyncMock(return_value=MagicMock(status_code=200, json=lambda: {"role": role}))
    return client


def _patch_github(role: str):
    """Patch the GitHub role derivation to report ``role`` for the requester."""
    client = _github_role_client(role)
    return (
        patch("src.admin.connections.github_client.GitHubAppClient", return_value=client),
        patch("src.admin.connections.service._get_github_app_credentials", return_value=("app-id", "fake-pem")),
    )


# ---------------------------------------------------------------------------
# GET /admin/access-requests — scope + class filtering
# ---------------------------------------------------------------------------


async def test_org_admin_lists_only_own_org_join_requests(seeded):
    """An org admin sees their own org's class-B request and nothing else.

    Specifically NOT: the other org's class-B request, and NOT either class-A
    new-tenant request (which they cannot approve, so listing them would only
    produce rows that 403 on click).
    """
    async with _client(seeded, _context("sub-org-admin", OWN_ORG)) as client:
        resp = await client.get("/admin/access-requests")

    assert resp.status_code == 200
    ids = {r["id"] for r in resp.json()["requests"]}
    assert ids == {"req-own-b"}


async def test_platform_admin_still_lists_every_org_and_class(seeded):
    """Regression: the platform-admin queue is unchanged — all orgs, both classes."""
    async with _client(seeded, _context("sub-platform", "platform", is_admin=True)) as client:
        resp = await client.get("/admin/access-requests")

    assert resp.status_code == 200
    ids = {r["id"] for r in resp.json()["requests"]}
    assert ids == {"req-own-b", "req-other-b", "req-new-a", "req-new-a2"}


async def test_org_admin_with_no_pending_requests_sees_empty_list(db_engine):
    """An org admin whose queue is genuinely empty gets 200 + [], not a 403."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        team = await _seed_org(session, OWN_ORG)
        await _seed_admin(session, cognito_sub="sub-org-admin", org_id=OWN_ORG, role="org_admin", team_id=team.id)
        # A pending request for a DIFFERENT org must not leak into this list.
        await _seed_org(session, OTHER_ORG)
        session.add(_pending(request_id="req-other-b", tenant_id=OTHER_ORG, login="outsider", sub="sub-outsider"))
        await session.commit()

    async with _client(db_engine, _context("sub-org-admin", OWN_ORG)) as client:
        resp = await client.get("/admin/access-requests")

    assert resp.status_code == 200
    assert resp.json()["requests"] == []


async def test_dept_admin_cannot_list(seeded):
    """dept_admin does not hold USER_MANAGE → 403 (not an empty list)."""
    async with _client(seeded, _context("sub-dept-admin", OWN_ORG)) as client:
        resp = await client.get("/admin/access-requests")

    assert resp.status_code == 403


async def test_plain_member_cannot_list(seeded):
    """A member with a real membership row still lacks USER_MANAGE → 403."""
    async with _client(seeded, _context("sub-plain-member", OWN_ORG)) as client:
        resp = await client.get("/admin/access-requests")

    assert resp.status_code == 403


async def test_no_membership_caller_cannot_list(seeded):
    """No membership row → least-privilege MEMBER (#4015/#4026) → 403.

    This is the #3989 empty-scope trap: USER_MANAGE is in
    ``_ORG_SCOPED_PERMISSIONS``, so a caller with no resolvable org is denied
    rather than short-circuiting the scope comparison and reading everything.
    """
    async with _client(seeded, _context("sub-nobody", "")) as client:
        resp = await client.get("/admin/access-requests")

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST .../approve — org-scoped approval and the granted role
# ---------------------------------------------------------------------------


@patch.dict(os.environ, V2_ON)
async def test_org_admin_approve_own_org_grants_member_not_org_admin(seeded):
    """The core privilege-grant regression.

    ``approve_request``'s existing-org branch would have written
    ``role="org_admin"`` here, and #3982's ceiling would NOT have stopped it
    (2 > 2 is False). The approval path must derive the role from GitHub instead:
    a plain GitHub org member becomes ``member``.
    """
    patch_client, patch_creds = _patch_github("member")
    async with _client(seeded, _context("sub-org-admin", OWN_ORG)) as client:
        with patch_client, patch_creds:
            resp = await client.post("/admin/access-requests/req-own-b/approve")

    assert resp.status_code == 200
    assert resp.json() == {"status": "approved", "tenant_id": OWN_ORG}

    factory = async_sessionmaker(seeded, expire_on_commit=False)
    async with factory() as session:
        user = await session.scalar(select(User).where(User.cognito_sub == "sub-joiner"))
        assert user is not None, "approved requester must get a users row"
        assert user.org_id == OWN_ORG
        assert user.role == "member"

        membership = await session.scalar(select(TenantMembership).where(TenantMembership.user_id == user.id))
        assert membership is not None, "membership is the authority the read side trusts (#3987)"
        assert membership.tenant_id == OWN_ORG
        assert membership.role == "member", "an org_admin approval must not mint a co-admin"

        request = await session.get(TenantAccessRequest, "req-own-b")
        assert request.status == "approved"
        assert request.decided_by == "sub-org-admin"


@patch.dict(os.environ, V2_ON)
async def test_org_admin_approve_grants_org_admin_only_when_github_says_admin(seeded):
    """The derived role is honoured in both directions.

    A requester who genuinely IS a GitHub org admin gets ``org_admin`` — this is
    the same role the auto-approve path would have granted, so scoped approval
    doesn't silently downgrade legitimate admins either.
    """
    patch_client, patch_creds = _patch_github("admin")
    async with _client(seeded, _context("sub-org-admin", OWN_ORG)) as client:
        with patch_client, patch_creds:
            resp = await client.post("/admin/access-requests/req-own-b/approve")

    assert resp.status_code == 200

    factory = async_sessionmaker(seeded, expire_on_commit=False)
    async with factory() as session:
        user = await session.scalar(select(User).where(User.cognito_sub == "sub-joiner"))
        membership = await session.scalar(select(TenantMembership).where(TenantMembership.user_id == user.id))
        assert membership.role == "org_admin"


@patch.dict(os.environ, V2_ON)
async def test_org_admin_cannot_approve_other_org(seeded):
    """Cross-tenant approve → 403, and no user is created in the other org."""
    patch_client, patch_creds = _patch_github("member")
    async with _client(seeded, _context("sub-org-admin", OWN_ORG)) as client:
        with patch_client, patch_creds:
            resp = await client.post("/admin/access-requests/req-other-b/approve")

    assert resp.status_code == 403

    factory = async_sessionmaker(seeded, expire_on_commit=False)
    async with factory() as session:
        assert await session.scalar(select(User).where(User.cognito_sub == "sub-outsider")) is None
        request = await session.get(TenantAccessRequest, "req-other-b")
        assert request.status == "pending"


@patch.dict(os.environ, V2_ON)
async def test_org_admin_cannot_approve_new_tenant_request(seeded):
    """Class A stays platform-admin-only: creating a tenant is not an org act."""
    patch_client, patch_creds = _patch_github("member")
    async with _client(seeded, _context("sub-org-admin", OWN_ORG)) as client:
        with patch_client, patch_creds:
            resp = await client.post("/admin/access-requests/req-new-a/approve")

    assert resp.status_code == 403

    factory = async_sessionmaker(seeded, expire_on_commit=False)
    async with factory() as session:
        assert await session.get(Organization, "brand-new-co") is None, "no org may be created by an org admin"
        request = await session.get(TenantAccessRequest, "req-new-a")
        assert request.status == "pending"


@patch.dict(os.environ, V2_ON)
async def test_org_admin_approve_rejects_platform_level_derived_role(seeded):
    """Defence-in-depth: a platform-level role string never survives the ceiling.

    ``require_assignable_role`` is not what blocks the org_admin→org_admin grant
    (equal ranks), but it IS what blocks a platform-level string — so if the
    derivation is ever changed or compromised into returning ``platform_admin``,
    the request fails closed instead of escalating a user out of the org.
    """
    async with _client(seeded, _context("sub-org-admin", OWN_ORG)) as client:
        with patch("src.admin.onboarding.handler._determine_role_for_matched_user", AsyncMock(return_value="platform_admin")):
            resp = await client.post("/admin/access-requests/req-own-b/approve")

    assert resp.status_code == 403

    factory = async_sessionmaker(seeded, expire_on_commit=False)
    async with factory() as session:
        assert await session.scalar(select(User).where(User.cognito_sub == "sub-joiner")) is None


@patch.dict(os.environ, V2_ON)
async def test_platform_admin_approve_new_tenant_unchanged(seeded):
    """Regression: the platform-admin class-A path still creates the org."""
    mock_writer = MagicMock()
    mock_writer.put_user_identity = AsyncMock(return_value=True)

    async with _client(seeded, _context("sub-platform", "platform", is_admin=True)) as client:
        with patch("src.admin.identity.identity_index_writer.IdentityIndexWriter", return_value=mock_writer):
            resp = await client.post("/admin/access-requests/req-new-a/approve")

    assert resp.status_code == 200
    assert resp.json()["tenant_id"] == "brand-new-co"

    factory = async_sessionmaker(seeded, expire_on_commit=False)
    async with factory() as session:
        assert await session.get(Organization, "brand-new-co") is not None


async def test_dept_admin_cannot_approve(seeded):
    """dept_admin lacks USER_MANAGE → 403 on the decision route too."""
    async with _client(seeded, _context("sub-dept-admin", OWN_ORG)) as client:
        resp = await client.post("/admin/access-requests/req-own-b/approve")

    assert resp.status_code == 403


async def test_approve_unknown_request_is_404_for_platform_admin(seeded):
    """Response shape for a missing request is unchanged."""
    async with _client(seeded, _context("sub-platform", "platform", is_admin=True)) as client:
        resp = await client.post("/admin/access-requests/does-not-exist/approve")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST .../deny — destructive, so scope must fail closed BEFORE Cognito
# ---------------------------------------------------------------------------


async def test_org_admin_deny_cross_tenant_never_deletes_cognito_user(seeded):
    """A scope failure on deny must not reach ``admin_delete_user``.

    Asserting only the 403 would not prove this: the deletion happens post-commit
    inside ``deny_request``, so the mock is the real assertion.
    """
    mock_cognito = MagicMock()
    async with _client(seeded, _context("sub-org-admin", OWN_ORG)) as client:
        with (
            patch.dict(os.environ, {"BG_COGNITO_USER_POOL_ID": "us-east-1_pool"}),
            patch("boto3.client", return_value=mock_cognito),
        ):
            resp = await client.post("/admin/access-requests/req-other-b/deny")

    assert resp.status_code == 403
    mock_cognito.admin_delete_user.assert_not_called()

    factory = async_sessionmaker(seeded, expire_on_commit=False)
    async with factory() as session:
        request = await session.get(TenantAccessRequest, "req-other-b")
        assert request.status == "pending", "another org's request must be untouched"


async def test_org_admin_deny_new_tenant_request_never_deletes_cognito_user(seeded):
    """Class-A deny is platform-admin-only, and likewise deletes nothing."""
    mock_cognito = MagicMock()
    async with _client(seeded, _context("sub-org-admin", OWN_ORG)) as client:
        with (
            patch.dict(os.environ, {"BG_COGNITO_USER_POOL_ID": "us-east-1_pool"}),
            patch("boto3.client", return_value=mock_cognito),
        ):
            resp = await client.post("/admin/access-requests/req-new-a/deny")

    assert resp.status_code == 403
    mock_cognito.admin_delete_user.assert_not_called()


async def test_org_admin_can_deny_own_org_request(seeded):
    """The happy path: an org admin may deny their own org's join request."""
    mock_cognito = MagicMock()
    async with _client(seeded, _context("sub-org-admin", OWN_ORG)) as client:
        with (
            patch.dict(os.environ, {"BG_COGNITO_USER_POOL_ID": "us-east-1_pool"}),
            patch("boto3.client", return_value=mock_cognito),
        ):
            resp = await client.post("/admin/access-requests/req-own-b/deny", json={"decision_note": "not this quarter"})

    assert resp.status_code == 200
    mock_cognito.admin_delete_user.assert_called_once_with(UserPoolId="us-east-1_pool", Username="sub-joiner")

    factory = async_sessionmaker(seeded, expire_on_commit=False)
    async with factory() as session:
        request = await session.get(TenantAccessRequest, "req-own-b")
        assert request.status == "denied"
        assert request.decided_by == "sub-org-admin"
        assert request.decision_note == "not this quarter"


async def test_dept_admin_deny_never_deletes_cognito_user(seeded):
    """dept_admin → 403 before the Cognito client is even built."""
    mock_cognito = MagicMock()
    async with _client(seeded, _context("sub-dept-admin", OWN_ORG)) as client:
        with (
            patch.dict(os.environ, {"BG_COGNITO_USER_POOL_ID": "us-east-1_pool"}),
            patch("boto3.client", return_value=mock_cognito),
        ):
            resp = await client.post("/admin/access-requests/req-own-b/deny")

    assert resp.status_code == 403
    mock_cognito.admin_delete_user.assert_not_called()
