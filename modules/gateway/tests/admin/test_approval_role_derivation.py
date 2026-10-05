"""Every approval branch derives the granted role — #5666 (A11).

#4018 fixed the ORG-ADMIN approval branch: it derives the role from GitHub org
membership and passes it to ``attach_approved_member``. The PLATFORM-ADMIN branch
kept calling ``approve_request``, whose existing-org branch hardcoded
``role="org_admin"`` at five points. So a platform admin approving a request to JOIN
an existing org silently minted a co-administrator of somebody else's tenant — and
synced ``custom:role=org_admin`` onto their Cognito user too.

That is the incomplete-fix-on-a-parallel-path shape the issue warns about, and it
had no test: ``test_onboarding_access_request_scope.py`` covers the org-admin branch
thoroughly and the platform-admin branch only for the NEW-tenant class, where
``org_admin`` is the correct answer and so the defect is invisible.

These tests are organized around the four (approver × request class) combinations,
because that grid is what makes the gap legible:

    approver         JOIN_EXISTING          CREATE_NEW
    platform_admin   member (was org_admin) org_admin  (correct: owner)
    org_admin        member  (#4018)        refused (not their act)

Plus: the role is reported to the approver, persisted identically in ``users.role``,
the membership row and the Cognito claims, and cannot be widened by the derivation
being skipped.
"""

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.shared.models.audit import AuditLog  # noqa: F401 -- register audit table before db_engine creates metadata
from src.shared.models.base import new_uuid
from src.shared.models.onboarding import TenantAccessRequest, TenantMembership
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.schemas.auth import TokenContext

pytestmark = pytest.mark.asyncio

V2_ON = {"USER_IDENTITY_INDEX_V2_WRITE": "true"}

EXISTING_ORG = "existing-co"
NEW_ORG = "brand-new-co"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _seed_org(session, org_id: str) -> Team:
    session.add(
        Organization(
            id=org_id,
            name=org_id,
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=["1"],
            cognito_client_ids=[],
        )
    )
    dept = Department(id=new_uuid(), org_id=org_id, name="Default")
    session.add(dept)
    team = Team(id=new_uuid(), org_id=org_id, department_id=dept.id, name="Default")
    session.add(team)
    await session.flush()
    return team


def _pending(request_id: str, tenant_id: str, login: str, sub: str) -> TenantAccessRequest:
    return TenantAccessRequest(
        id=request_id,
        cognito_sub=sub,
        provider="github",
        provider_user_id="12345",
        proposed_tenant_id=tenant_id,
        target_login=login,
        motivation="please let me in",
        status="pending",
    )


@pytest.fixture
async def seeded(db_engine):
    """One EXISTING org plus a join request for it, and a new-tenant request."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        await _seed_org(session, EXISTING_ORG)
        # JOIN_EXISTING: the org already exists.
        session.add(_pending("req-join", EXISTING_ORG, "joiner", "sub-joiner"))
        # CREATE_NEW: NEW_ORG deliberately does not exist.
        session.add(_pending("req-create", NEW_ORG, "founder", "sub-founder"))
        await session.commit()
    return db_engine


def _platform_admin() -> TokenContext:
    """Access-token-shaped platform admin (is_admin resolves without a DB row)."""
    return TokenContext(
        user_id="sub-platform",
        org_id="platform",
        team_id="",
        department_id="",
        account_type="human",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


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


def _patch_github(role: str):
    """Make the GitHub org-membership lookup report ``role`` for the requester."""
    client = MagicMock()
    client.get_installation_token = AsyncMock(return_value="fake-token")
    client.aclose = AsyncMock()
    client._http_client = MagicMock()
    client._http_client.get = AsyncMock(
        return_value=MagicMock(status_code=200, json=lambda: {"role": role, "state": "active", "user": {"id": 12345}})
    )
    return (
        patch("src.admin.connections.github_client.GitHubAppClient", return_value=client),
        patch("src.admin.connections.service._get_github_app_credentials", return_value=("app-id", "fake-pem")),
    )


async def _approve(db_engine, request_id: str, github_role: str, context: TokenContext | None = None):
    patch_client, patch_creds = _patch_github(github_role)
    async with _client(db_engine, context or _platform_admin()) as client:
        with patch_client, patch_creds, patch("src.admin.cognito_claims.sync_cognito_role_claims"):
            return await client.post(f"/admin/access-requests/{request_id}/approve")


# ---------------------------------------------------------------------------
# The gap: platform admin approving a JOIN of an existing org
# ---------------------------------------------------------------------------


class TestPlatformAdminJoinExistingOrg:
    """The branch that had no role derivation at all."""

    @patch.dict(os.environ, V2_ON)
    async def test_plain_github_member_is_not_made_org_admin(self, seeded):
        """The core regression of this finding.

        A platform admin approving somebody's request to join an EXISTING org must
        grant ``member``. The previous code path reached
        ``approve_request``'s hardcoded ``org_admin``.
        """
        resp = await _approve(seeded, "req-join", "member")
        assert resp.status_code == 200
        assert resp.json()["granted_role"] == "member"

        factory = async_sessionmaker(seeded, expire_on_commit=False)
        async with factory() as session:
            user = await session.scalar(select(User).where(User.cognito_sub == "sub-joiner"))
            assert user is not None, "an approved joiner must get a users row"
            assert user.org_id == EXISTING_ORG

            membership = await session.scalar(select(TenantMembership).where(TenantMembership.user_id == user.id))
            assert membership is not None, "membership is the authority access_control resolves from (#3987)"
            assert membership.role == "member", "a platform admin approving a JOIN of an existing org must not mint a co-administrator of it"

    @patch.dict(os.environ, V2_ON)
    async def test_genuine_github_org_admin_still_gets_org_admin(self, seeded):
        """Over-restriction guard: a real org admin is not downgraded."""
        resp = await _approve(seeded, "req-join", "admin")
        assert resp.status_code == 200
        assert resp.json()["granted_role"] == "org_admin"

        factory = async_sessionmaker(seeded, expire_on_commit=False)
        async with factory() as session:
            user = await session.scalar(select(User).where(User.cognito_sub == "sub-joiner"))
            membership = await session.scalar(select(TenantMembership).where(TenantMembership.user_id == user.id))
            assert membership.role == "org_admin"

    @patch.dict(os.environ, V2_ON)
    async def test_role_is_identical_in_user_membership_and_claims(self, seeded):
        """One derivation must reach all three sinks, or they drift.

        The defect had the membership row and the Cognito claims hardcoded
        *separately*, so fixing one would still have left the token asserting
        org_admin. The claims call is asserted here rather than mocked away.
        """
        patch_client, patch_creds = _patch_github("member")
        with patch("src.admin.onboarding.approval.sync_cognito_role_claims") as claims:
            async with _client(seeded, _platform_admin()) as client:
                with patch_client, patch_creds:
                    resp = await client.post("/admin/access-requests/req-join/approve")

        assert resp.status_code == 200
        reported = resp.json()["granted_role"]

        factory = async_sessionmaker(seeded, expire_on_commit=False)
        async with factory() as session:
            user = await session.scalar(select(User).where(User.cognito_sub == "sub-joiner"))
            membership = await session.scalar(select(TenantMembership).where(TenantMembership.user_id == user.id))
            team = await session.get(Team, user.team_id)

        # attach_approved_member owns the claims sync for this class; assert the
        # role it would publish matches, wherever the call was made from.
        assert reported == "member"
        assert user.role == "member"
        assert membership.role == "member"
        claims.assert_called_once_with(
            cognito_sub="sub-joiner",
            org_id=EXISTING_ORG,
            role="member",
            team_id=user.team_id,
            department_id=team.department_id,
        )


# ---------------------------------------------------------------------------
# The legitimate create/claim-owner case must keep its role
# ---------------------------------------------------------------------------


class TestCreateNewOrgRetainsOwnerRole:
    @patch.dict(os.environ, V2_ON)
    async def test_new_tenant_approval_grants_org_admin(self, seeded):
        """The requester owns the org this approval creates, so org_admin is right.

        This is the case that made the defect invisible: the platform-admin branch
        was only ever tested here, where the hardcoded value happened to be correct.
        """
        resp = await _approve(seeded, "req-create", "member")
        assert resp.status_code == 200
        assert resp.json()["granted_role"] == "org_admin"

        factory = async_sessionmaker(seeded, expire_on_commit=False)
        async with factory() as session:
            org = await session.get(Organization, NEW_ORG)
            assert org is not None, "approving a new-tenant request must create the org"

            user = await session.scalar(select(User).where(User.cognito_sub == "sub-founder"))
            assert user.role == "org_admin"
            membership = await session.scalar(select(TenantMembership).where(TenantMembership.user_id == user.id))
            assert membership.role == "org_admin", "the org's creator must retain owner authority"

    @patch.dict(os.environ, V2_ON)
    async def test_github_role_does_not_downgrade_an_org_creator(self, seeded):
        """The GitHub lookup is irrelevant to CREATE_NEW — there is no org to query.

        Asserted so a future refactor cannot make org creation depend on a GitHub
        answer about an organization that does not exist yet, which would strip the
        founder's authority.
        """
        resp = await _approve(seeded, "req-create", "member")
        assert resp.json()["granted_role"] == "org_admin"


# ---------------------------------------------------------------------------
# The response reports the grant
# ---------------------------------------------------------------------------


class TestApprovalReportsGrantedRole:
    @patch.dict(os.environ, V2_ON)
    async def test_response_includes_granted_role(self, seeded):
        """With the role derived server-side, the response is the only place the
        approver learns what authority they conferred."""
        resp = await _approve(seeded, "req-join", "member")
        body = resp.json()
        assert set(body) == {"status", "tenant_id", "granted_role"}
        assert body["status"] == "approved"
        assert body["tenant_id"] == EXISTING_ORG

    @patch.dict(os.environ, V2_ON)
    async def test_idempotent_reapprove_reports_the_role_actually_held(self, seeded):
        """A second approve must report the role in force, not re-derive a new one.

        If the GitHub answer changed since the first approval, re-deriving would
        report a role the user does not hold — making the response misleading exactly
        when an operator is double-checking.
        """
        first = await _approve(seeded, "req-join", "member")
        assert first.json()["granted_role"] == "member"

        # Same request, but GitHub now claims the user is an admin.
        second = await _approve(seeded, "req-join", "admin")
        assert second.status_code == 200
        assert second.json()["granted_role"] == "member", "an already-approved request must report the role actually held, not a fresh derivation"

        factory = async_sessionmaker(seeded, expire_on_commit=False)
        async with factory() as session:
            user = await session.scalar(select(User).where(User.cognito_sub == "sub-joiner"))
            membership = await session.scalar(select(TenantMembership).where(TenantMembership.user_id == user.id))
            assert membership.role == "member", "re-approval must not escalate an existing membership"


# ---------------------------------------------------------------------------
# The derivation itself
# ---------------------------------------------------------------------------


class TestDerivationIsFailClosed:
    async def test_unknown_github_answer_falls_back_to_member(self, db_session):
        """An empty or unrecognised GitHub answer must not become an admin grant."""
        from src.admin.onboarding.approval_decision import (
            MEMBER_ROLE,
            RequestClass,
            derive_approval_decision,
        )

        await _seed_org(db_session, EXISTING_ORG)
        request = _pending("req-x", EXISTING_ORG, "joiner", "sub-joiner")
        db_session.add(request)
        await db_session.flush()

        access = MagicMock()
        access.require_assignable_role = AsyncMock()
        access.require_modifiable_target = AsyncMock()

        for answer in ("", None, "   "):
            decision = await derive_approval_decision(
                db_session,
                access,
                _platform_admin(),
                request,
                role_for_existing_org=AsyncMock(return_value=answer),
            )
            assert decision.request_class is RequestClass.JOIN_EXISTING
            assert decision.granted_role == MEMBER_ROLE, f"GitHub answer {answer!r} must not widen the grant"

    async def test_both_ceiling_guards_run_on_every_branch(self, db_session):
        """``require_assignable_role`` AND ``require_modifiable_target`` must both be
        exercised. The latter ran on NO approval branch before this change, so an
        approval could rewrite the role of someone who outranked the approver."""
        from src.admin.onboarding.approval_decision import derive_approval_decision

        await _seed_org(db_session, EXISTING_ORG)
        request = _pending("req-y", EXISTING_ORG, "joiner", "sub-joiner")
        db_session.add(request)
        await db_session.flush()

        access = MagicMock()
        access.require_assignable_role = AsyncMock()
        access.require_modifiable_target = AsyncMock()

        await derive_approval_decision(
            db_session,
            access,
            _platform_admin(),
            request,
            role_for_existing_org=AsyncMock(return_value="member"),
        )

        access.require_assignable_role.assert_awaited_once()
        access.require_modifiable_target.assert_awaited_once()

    async def test_existing_platform_admin_target_is_protected(self, db_session):
        """A target holding platform authority is passed to the guard as such.

        Platform admin is not representable in a membership row (#3981), so the
        decision resolves it from ``users.role`` and hands it to
        ``require_modifiable_target``, which refuses for non-platform callers.
        """
        from src.admin.onboarding.approval_decision import derive_approval_decision

        team = await _seed_org(db_session, EXISTING_ORG)
        db_session.add(
            User(
                id=new_uuid(),
                org_id=EXISTING_ORG,
                team_id=team.id,
                email="pa@example.com",
                name="pa",
                cognito_sub="sub-joiner",
                role="platform_admin",
            )
        )
        request = _pending("req-z", EXISTING_ORG, "joiner", "sub-joiner")
        db_session.add(request)
        await db_session.flush()

        access = MagicMock()
        access.require_assignable_role = AsyncMock()
        access.require_modifiable_target = AsyncMock()

        await derive_approval_decision(
            db_session,
            access,
            _platform_admin(),
            request,
            role_for_existing_org=AsyncMock(return_value="member"),
        )

        assert access.require_modifiable_target.await_args.kwargs["target_is_platform_admin"] is True

    async def test_target_role_is_read_from_the_membership_not_users_role(self, db_session):
        """``users.role`` is a display mirror; the membership row is the authority.

        Pinned because passing the mirror would let a stale value decide whether a
        target may be modified.
        """
        from src.admin.onboarding.approval_decision import derive_approval_decision

        team = await _seed_org(db_session, EXISTING_ORG)
        user_id = new_uuid()
        db_session.add(
            User(
                id=user_id,
                org_id=EXISTING_ORG,
                team_id=team.id,
                email="j@example.com",
                name="j",
                cognito_sub="sub-joiner",
                role="member",  # mirror says member...
            )
        )
        db_session.add(
            TenantMembership(
                id=new_uuid(),
                user_id=user_id,
                tenant_id=EXISTING_ORG,
                role="org_admin",  # ...authority says org_admin
                is_active=True,
                joined_via="org_membership",
            )
        )
        request = _pending("req-w", EXISTING_ORG, "joiner", "sub-joiner")
        db_session.add(request)
        await db_session.flush()

        access = MagicMock()
        access.require_assignable_role = AsyncMock()
        access.require_modifiable_target = AsyncMock()

        await derive_approval_decision(
            db_session,
            access,
            _platform_admin(),
            request,
            role_for_existing_org=AsyncMock(return_value="member"),
        )

        assert access.require_modifiable_target.await_args.kwargs["target_current_role"] == "org_admin"


class TestRequestClassComesFromTheDatabase:
    async def test_class_is_decided_by_org_existence_not_a_client_field(self, db_session):
        """``TenantAccessRequest`` has no class column, and must not gain one that a
        requester could set — the class decides which branch may approve it."""
        from src.admin.onboarding.approval_decision import RequestClass, derive_approval_decision

        access = MagicMock()
        access.require_assignable_role = AsyncMock()
        access.require_modifiable_target = AsyncMock()

        # Same request shape, only the org's existence differs.
        absent = _pending("req-absent", "no-such-org", "founder", "sub-founder")
        db_session.add(absent)
        await db_session.flush()
        decision = await derive_approval_decision(
            db_session,
            access,
            _platform_admin(),
            absent,
            role_for_existing_org=AsyncMock(return_value="member"),
        )
        assert decision.request_class is RequestClass.CREATE_NEW
        assert decision.creates_organization is True

        await _seed_org(db_session, EXISTING_ORG)
        present = _pending("req-present", EXISTING_ORG, "joiner", "sub-joiner")
        db_session.add(present)
        await db_session.flush()
        decision = await derive_approval_decision(
            db_session,
            access,
            _platform_admin(),
            present,
            role_for_existing_org=AsyncMock(return_value="member"),
        )
        assert decision.request_class is RequestClass.JOIN_EXISTING
        assert decision.creates_organization is False


async def test_target_role_and_platform_protection_use_distinct_scopes(db_session):
    from src.admin.onboarding.approval_decision import _target_current_role

    other_team = await _seed_org(db_session, "other-org")
    target_team = await _seed_org(db_session, EXISTING_ORG)
    other = User(
        id=new_uuid(),
        org_id="other-org",
        team_id=other_team.id,
        email="other@example.com",
        name="other",
        cognito_sub="same-sub",
        role="platform_admin",
    )
    target = User(
        id=new_uuid(), org_id=EXISTING_ORG, team_id=target_team.id, email="target@example.com", name="target", cognito_sub=None, role="member"
    )
    db_session.add_all([other, target])
    await db_session.flush()
    from src.shared.identity.workspaces import link_login_to_workspace

    await link_login_to_workspace(db_session, other, target)
    db_session.add(TenantMembership(user_id=target.id, tenant_id=EXISTING_ORG, role="org_admin", is_active=True, joined_via="onboarding_approval"))
    await db_session.flush()
    request = _pending("scoped-guard", EXISTING_ORG, "joiner", "same-sub")
    assert await _target_current_role(db_session, request) == ("org_admin", True)


@patch.dict(os.environ, V2_ON)
async def test_existing_account_approval_updates_role_mirror_and_claims(db_session):
    from src.admin.onboarding.approval import approve_request

    team = await _seed_org(db_session, EXISTING_ORG)
    user = User(
        id=new_uuid(),
        org_id=EXISTING_ORG,
        team_id=team.id,
        email="existing@example.com",
        name="existing",
        cognito_sub="existing-sub",
        role="org_admin",
    )
    request = _pending("mirror-request", EXISTING_ORG, "joiner", "existing-sub")
    db_session.add_all([user, request])
    await db_session.commit()
    with patch("src.admin.onboarding.approval.sync_cognito_role_claims") as claims:
        assert await approve_request(db_session, request, "platform-sub", granted_role="member") == EXISTING_ORG
    await db_session.refresh(user)
    membership = await db_session.scalar(select(TenantMembership).where(TenantMembership.user_id == user.id))
    assert user.role == membership.role == "member"
    claims.assert_called_once_with(cognito_sub="existing-sub", org_id=EXISTING_ORG, role="member", team_id="")


@patch.dict(os.environ, V2_ON)
async def test_approval_audit_records_incomplete_claim_sync(seeded):
    patch_client, patch_creds = _patch_github("member")
    with patch("src.admin.onboarding.approval.sync_cognito_role_claims", return_value=False):
        async with _client(seeded, _platform_admin()) as client:
            with patch_client, patch_creds:
                response = await client.post("/admin/access-requests/req-join/approve")
    assert response.status_code == 200
    operation_id = response.headers["X-Admin-Operation-Id"]
    factory = async_sessionmaker(seeded, expire_on_commit=False)
    async with factory() as session:
        receipts = (await session.scalars(select(AuditLog))).all()
    receipts = [row for row in receipts if row.details.get("operation_id") == operation_id]
    assert len(receipts) == 2
    terminal = next(row for row in receipts if row.event_type == "admin_approve_access_request")
    assert terminal.details["outcome"] == "reconciliation_required"
    assert terminal.details["target_id"] == "req-join"
    assert terminal.details["target_tenant"] == EXISTING_ORG


@patch.dict(os.environ, V2_ON)
async def test_new_tenant_for_existing_login_keeps_one_cognito_subject(db_session):
    from src.admin.onboarding.approval import approve_request
    from src.shared.identity.workspaces import workspace_user

    team = await _seed_org(db_session, EXISTING_ORG)
    canonical = User(
        id=new_uuid(),
        org_id=EXISTING_ORG,
        team_id=team.id,
        email="canonical@example.com",
        name="canonical",
        cognito_sub="existing-login",
        role="platform_admin",
    )
    request = _pending("new-workspace-request", NEW_ORG, "founder", "existing-login")
    db_session.add_all([canonical, request])
    await db_session.commit()
    with patch("src.admin.onboarding.approval.sync_cognito_role_claims", return_value=True) as claims:
        assert await approve_request(db_session, request, "approver", granted_role="org_admin") == NEW_ORG
    target = await workspace_user(db_session, "existing-login", NEW_ORG)
    assert target is not None and target.id != canonical.id
    assert target.cognito_sub is None
    assert target.role == "org_admin"
    assert canonical.role == "platform_admin"
    assert claims.call_args.kwargs["role"] == "platform_admin"
