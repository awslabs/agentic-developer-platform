"""Issue #4849: the membership-write call sites actually project.

``test_member_org_ids_projection.py`` pins the helper's semantics. This suite
pins that the *callers* invoke it — the half of the issue that was really broken:
5 of 7 membership-write paths projected nothing at all, and the two idempotent
branches (re-approve, reinstall) early-returned past their caller's post-commit
work. Those replay branches are precisely the recurring event that can heal a
projection whose earlier write failed, so skipping them was backwards.

Each test drives the real function and asserts on the writer, so deleting a
``project_member_org_ids`` call fails a test rather than silently regressing to
a stale projection that only a fail-closed reader downstream would notice.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.connections.service import _create_installer_membership
from src.admin.onboarding.approval import approve_request, attach_approved_member
from src.shared.models.base import new_uuid
from src.shared.models.onboarding import TenantAccessRequest, TenantMembership
from src.shared.models.organization import Department, Organization, Team, User

pytestmark = pytest.mark.asyncio

V2_ON = {"USER_IDENTITY_INDEX_V2_WRITE": "true"}


def _writer() -> MagicMock:
    writer = MagicMock()
    writer.update_user_membership_orgs = AsyncMock(return_value=True)
    writer.put_user_identity = AsyncMock(return_value=True)
    return writer


def _projected_orgs(writer: MagicMock) -> list[list[str]]:
    """Every member_org_ids list passed to the targeted projection write."""
    return [sorted(c.kwargs["member_org_ids"]) for c in writer.update_user_membership_orgs.await_args_list]


def _pending_request(*, tenant_id: str, login: str, sub: str, provider_user_id: str = "90001") -> TenantAccessRequest:
    return TenantAccessRequest(
        id=new_uuid(),
        cognito_sub=sub,
        provider="github",
        provider_user_id=provider_user_id,
        proposed_tenant_id=tenant_id,
        target_login=login,
        motivation="test",
        status="pending",
    )


async def _org(db: AsyncSession, org_id: str) -> Team:
    db.add(
        Organization(
            id=org_id,
            name=org_id,
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[],
            cognito_client_ids=[],
        )
    )
    dept = Department(id=new_uuid(), org_id=org_id, name="Default")
    db.add(dept)
    team = Team(id=new_uuid(), org_id=org_id, department_id=dept.id, name="Default")
    db.add(team)
    await db.flush()
    return team


# ---------------------------------------------------------------------------
# approve_request — new-org path (had a projection, now via the shared helper)
# ---------------------------------------------------------------------------


@patch.dict(os.environ, V2_ON)
async def test_approve_request_projects_the_new_membership(db_session: AsyncSession):
    request = _pending_request(tenant_id="acme-new", login="acmeowner", sub="sub-owner")
    db_session.add(request)
    await db_session.flush()

    writer = _writer()
    await approve_request(db=db_session, request=request, admin_sub="admin-1", identity_writer=writer)

    assert ["acme-new"] in _projected_orgs(writer)


# ---------------------------------------------------------------------------
# approve_request — the idempotent re-approve branch (#4849: had NO projection)
# ---------------------------------------------------------------------------


@patch.dict(os.environ, V2_ON)
async def test_reapprove_projects_the_healed_membership(db_session: AsyncSession):
    """The pre-#4006 no-row cohort: re-approve creates the missing membership.

    That heal was invisible to DDB before #4849 — the branch returned before the
    post-commit projection block, so the user stayed unprojected (and therefore
    NOT eligible to a fail-closed reader) despite now holding a real membership.
    """
    team = await _org(db_session, "legacy-org")
    db_session.add(
        User(
            id="legacy-admin-1",
            org_id="legacy-org",
            team_id=team.id,
            email="legacy@github.onboard",
            name="legacyadmin",
            cognito_sub="sub-legacy",
            role="org_admin",
        )
    )
    await db_session.commit()

    request = _pending_request(tenant_id="legacy-org", login="legacyadmin", sub="sub-legacy")
    db_session.add(request)
    await db_session.flush()

    writer = _writer()
    await approve_request(db=db_session, request=request, admin_sub="admin-1", identity_writer=writer)

    # The membership was healed in Postgres...
    rows = list((await db_session.execute(select(TenantMembership).where(TenantMembership.user_id == "legacy-admin-1"))).scalars().all())
    assert [r.tenant_id for r in rows] == ["legacy-org"]
    # ...and the projection was refreshed to match. (No user_identities rows in
    # this fixture, so the helper finds no GitHub id to project onto — what is
    # asserted is that the branch *runs* the projection rather than returning past
    # it; the org list itself is covered by the helper's own suite.)
    writer.update_user_membership_orgs.assert_not_awaited()


@patch.dict(os.environ, V2_ON)
async def test_reapprove_projects_full_org_list_for_a_linked_user(db_session: AsyncSession):
    """Same branch, with a GitHub identity present: the real projection fires."""
    from src.shared.models.vault import UserIdentity

    team = await _org(db_session, "legacy-org")
    db_session.add(
        User(
            id="legacy-admin-2",
            org_id="legacy-org",
            team_id=team.id,
            email="legacy2@github.onboard",
            name="legacyadmin2",
            cognito_sub="sub-legacy-2",
            role="org_admin",
        )
    )
    db_session.add(
        UserIdentity(
            id=new_uuid(),
            user_id="legacy-admin-2",
            org_id="legacy-org",
            team_id=team.id,
            provider="github",
            provider_user_id="90002",
            verification_method="oauth",
        )
    )
    await db_session.commit()

    request = _pending_request(tenant_id="legacy-org", login="legacyadmin2", sub="sub-legacy-2", provider_user_id="90002")
    db_session.add(request)
    await db_session.flush()

    writer = _writer()
    await approve_request(db=db_session, request=request, admin_sub="admin-1", identity_writer=writer)

    writer.update_user_membership_orgs.assert_awaited()
    assert ["legacy-org"] in _projected_orgs(writer)


# ---------------------------------------------------------------------------
# attach_approved_member — #4018 org-admin approvals + login auto-match.
# Had NO projection at all before #4849.
# ---------------------------------------------------------------------------


@patch.dict(os.environ, V2_ON)
async def test_attach_approved_member_projects_the_membership(db_session: AsyncSession):
    """Every member attached to an EXISTING org used to stay unprojected."""
    await _org(db_session, "existing-org")
    await db_session.commit()

    request = _pending_request(tenant_id="existing-org", login="joiner", sub="sub-joiner", provider_user_id="90003")
    db_session.add(request)
    await db_session.flush()

    writer = MagicMock()
    writer.update_user_membership_orgs = AsyncMock(return_value=True)
    # attach_approved_member builds its own writer; patch the class it constructs.
    with (
        patch("src.admin.identity.identity_index_writer.IdentityIndexWriter", return_value=writer),
        patch("src.admin.onboarding.approval.sync_cognito_role_claims"),
    ):
        await attach_approved_member(
            db_session,
            request,
            granted_role="member",
            decided_by="admin-1",
        )

    writer.update_user_membership_orgs.assert_awaited()
    kwargs = writer.update_user_membership_orgs.await_args.kwargs
    assert kwargs["provider_user_id"] == "90003"
    assert kwargs["member_org_ids"] == ["existing-org"]


@patch.dict(os.environ, V2_ON)
async def test_attach_approved_member_projects_after_the_commit(db_session: AsyncSession):
    """Ordering: the projection must see the membership as committed.

    If it ran before the commit it would publish a membership a rollback erases;
    asserting the org list is non-empty at write time is what proves the call
    sits after ``db.commit()`` rather than before ``upsert_tenant_membership``.
    """
    await _org(db_session, "existing-org-2")
    await db_session.commit()

    request = _pending_request(tenant_id="existing-org-2", login="joiner2", sub="sub-joiner-2", provider_user_id="90004")
    db_session.add(request)
    await db_session.flush()

    writer = MagicMock()
    writer.update_user_membership_orgs = AsyncMock(return_value=True)
    with (
        patch("src.admin.identity.identity_index_writer.IdentityIndexWriter", return_value=writer),
        patch("src.admin.onboarding.approval.sync_cognito_role_claims"),
    ):
        await attach_approved_member(
            db_session,
            request,
            granted_role="member",
            decided_by="admin-1",
        )

    assert writer.update_user_membership_orgs.await_args.kwargs["member_org_ids"] == ["existing-org-2"]


# ---------------------------------------------------------------------------
# _create_installer_membership — install callback, including the reinstall
# idempotent-skip branch (#4849: it returned before projecting)
# ---------------------------------------------------------------------------


async def _installer(db: AsyncSession, org_id: str, github_id: str) -> User:
    from src.shared.models.vault import UserIdentity

    team = await _org(db, org_id)
    user = User(
        id=new_uuid(),
        org_id=org_id,
        team_id=team.id,
        email=f"installer-{github_id}@example.com",
        cognito_sub=f"sub-{github_id}",
    )
    db.add(user)
    db.add(
        UserIdentity(
            id=new_uuid(),
            user_id=user.id,
            org_id=org_id,
            team_id=team.id,
            provider="github",
            provider_user_id=github_id,
            verification_method="oauth",
        )
    )
    await db.commit()
    return user


async def test_install_callback_projects_the_new_membership(db_session: AsyncSession):
    user = await _installer(db_session, "install-org", "70001")

    writer = _writer()
    with patch("src.admin.identity.identity_index_writer.IdentityIndexWriter", return_value=writer):
        await _create_installer_membership(
            user_row=user,
            tenant_id="install-org",
            github_org_login="install-org",
            db=db_session,
        )

    assert _projected_orgs(writer) == [["install-org"]]


async def test_reinstall_still_projects_despite_the_idempotent_skip(db_session: AsyncSession):
    """A reinstall writes nothing — and must still refresh the projection.

    Nothing new is committed on this branch, so the projection is a pure read of
    already-committed state. It is also the one recurring event that can heal a
    user whose earlier projection write failed, which is why the early return
    does not skip it.
    """
    user = await _installer(db_session, "install-org-2", "70002")

    writer = _writer()
    with patch("src.admin.identity.identity_index_writer.IdentityIndexWriter", return_value=writer):
        # First install creates the membership.
        await _create_installer_membership(
            user_row=user,
            tenant_id="install-org-2",
            github_org_login="install-org-2",
            db=db_session,
        )
        writer.update_user_membership_orgs.reset_mock()

        # Reinstall: idempotent skip, but the projection must still fire.
        await _create_installer_membership(
            user_row=user,
            tenant_id="install-org-2",
            github_org_login="install-org-2",
            db=db_session,
        )

    assert _projected_orgs(writer) == [["install-org-2"]]

    # And still exactly one membership row — the heal must not duplicate.
    rows = list((await db_session.execute(select(TenantMembership).where(TenantMembership.user_id == user.id))).scalars().all())
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# AdminService.update_user — the explicit ROLE-CHANGE path (#4019), projecting
# post-commit via set_membership_role (PR #4916 review F3: had no coverage)
# ---------------------------------------------------------------------------


async def _linked_user(db: AsyncSession, org_id: str, github_id: str, *, role: str = "member", with_membership: bool = True) -> User:
    from src.shared.models.vault import UserIdentity

    team = await _org(db, org_id)
    user = User(
        id=new_uuid(),
        org_id=org_id,
        team_id=team.id,
        email=f"user-{github_id}@example.com",
        name=f"user-{github_id}",
        role=role,
    )
    db.add(user)
    db.add(
        UserIdentity(
            id=new_uuid(),
            user_id=user.id,
            org_id=org_id,
            team_id=team.id,
            provider="github",
            provider_user_id=github_id,
            verification_method="oauth",
        )
    )
    if with_membership:
        db.add(
            TenantMembership(
                user_id=user.id,
                tenant_id=org_id,
                role=role,
                is_active=True,
                joined_via="test_seed",
            )
        )
    await db.commit()
    return user


async def test_role_change_projects_the_membership(db_session: AsyncSession):
    """update_user with a role writes tenant_memberships and must project it."""
    from src.admin.service import AdminService
    from src.shared.schemas.admin import UserUpdateRequest

    user = await _linked_user(db_session, "role-org", "60001", role="member")

    writer = _writer()
    with patch("src.admin.identity.identity_index_writer.IdentityIndexWriter", return_value=writer):
        await AdminService(db=db_session).update_user(
            "role-org",
            user.id,
            UserUpdateRequest(role="org_admin"),
        )

    writer.update_user_membership_orgs.assert_awaited()
    kwargs = writer.update_user_membership_orgs.await_args.kwargs
    assert kwargs["provider_user_id"] == "60001"
    assert kwargs["member_org_ids"] == ["role-org"]


@pytest.mark.parametrize("method", ["channel_placement", "self_asserted", "admin_manual", "oauth"])
async def test_role_change_only_projects_membership_through_proven_identity(db_session: AsyncSession, method):
    from src.admin.service import AdminService
    from src.shared.models.vault import UserIdentity
    from src.shared.schemas.admin import UserUpdateRequest

    user = await _linked_user(db_session, "proof-role-org", "60003", role="member")
    identity = await db_session.scalar(select(UserIdentity).where(UserIdentity.user_id == user.id))
    identity.verification_method = method
    await db_session.commit()

    writer = _writer()
    with patch("src.admin.identity.identity_index_writer.IdentityIndexWriter", return_value=writer):
        await AdminService(db=db_session).update_user("proof-role-org", user.id, UserUpdateRequest(role="org_admin"))

    # An authorized platform-role edit still changes the real membership, but
    # does not verify an external account the member has only claimed.
    membership = await db_session.scalar(select(TenantMembership).where(TenantMembership.user_id == user.id))
    assert membership.role == "org_admin"
    assert identity.verification_method == method
    writer.update_user_membership_orgs.assert_awaited_once_with(
        provider_user_id="60003", member_org_ids=["proof-role-org"] if method in {"oauth", "admin_manual"} else [], provider="github"
    )


async def test_role_change_that_creates_the_membership_row_projects_it(db_session: AsyncSession):
    """A role change can CREATE the membership (user had none in this tenant),
    so the org list itself changes here — the projection must see the new org."""
    from src.admin.service import AdminService
    from src.shared.schemas.admin import UserUpdateRequest

    user = await _linked_user(db_session, "role-org-2", "60002", role="member", with_membership=False)

    writer = _writer()
    with patch("src.admin.identity.identity_index_writer.IdentityIndexWriter", return_value=writer):
        await AdminService(db=db_session).update_user(
            "role-org-2",
            user.id,
            UserUpdateRequest(role="org_admin"),
        )

    assert ["role-org-2"] in _projected_orgs(writer)
    rows = list((await db_session.execute(select(TenantMembership).where(TenantMembership.user_id == user.id))).scalars().all())
    assert [(r.tenant_id, r.role) for r in rows] == [("role-org-2", "org_admin")]


async def test_name_only_update_does_not_project(db_session: AsyncSession):
    """No role in the request -> no membership write -> nothing to project."""
    from src.admin.service import AdminService
    from src.shared.schemas.admin import UserUpdateRequest

    user = await _linked_user(db_session, "role-org-3", "60003", role="member")

    writer = _writer()
    with patch("src.admin.identity.identity_index_writer.IdentityIndexWriter", return_value=writer):
        await AdminService(db=db_session).update_user(
            "role-org-3",
            user.id,
            UserUpdateRequest(name="Renamed"),
        )

    writer.update_user_membership_orgs.assert_not_awaited()


# ---------------------------------------------------------------------------
# UsersService.create_user — identity-service create (#387/#4006), projecting
# post-commit for admin-level roles (PR #4916 review F3: had no coverage)
# ---------------------------------------------------------------------------


def _users_service(db: AsyncSession, writer: MagicMock):
    from src.admin.identity.users_service import UsersService

    cognito_sync = MagicMock()
    cognito_sync.create_user_and_invite = AsyncMock(
        side_effect=lambda **kw: {"Username": kw["email"], "Attributes": [{"Name": "sub", "Value": "sub-" + kw["email"]}]}
    )
    cognito_sync.ensure_user_group = AsyncMock()
    writer.sync_user_identities = AsyncMock(return_value=None)
    return UsersService(db, cognito_sync=cognito_sync, identity_writer=writer)


async def test_users_service_create_admin_projects_the_membership(db_session: AsyncSession):
    """An admin-level create writes a membership row that was never projected
    before #4849 (sync_user_identities deliberately preserves member_org_ids)."""
    from src.admin.identity.schemas import UserCreateRequest, UserIdentityInput

    team = await _org(db_session, "create-org")
    await db_session.commit()

    writer = _writer()
    service = _users_service(db_session, writer)
    await service.create_user(
        "create-org",
        UserCreateRequest(
            email="new-admin@example.com",
            name="New Admin",
            role="org_admin",
            team_id=team.id,
            identities=[UserIdentityInput(provider="github", provider_user_id="60010", provider_username="newadmin")],
            send_invite=False,
        ),
    )

    writer.update_user_membership_orgs.assert_awaited()
    kwargs = writer.update_user_membership_orgs.await_args.kwargs
    assert kwargs["provider_user_id"] == "60010"
    assert kwargs["member_org_ids"] == ["create-org"]


async def test_users_service_create_member_projects_membership(db_session: AsyncSession):
    """Ordinary native members publish their explicit org membership too."""
    from src.admin.identity.schemas import UserCreateRequest, UserIdentityInput

    team = await _org(db_session, "create-org-2")
    await db_session.commit()

    writer = _writer()
    service = _users_service(db_session, writer)
    await service.create_user(
        "create-org-2",
        UserCreateRequest(
            email="new-member@example.com",
            name="New Member",
            role="member",
            team_id=team.id,
            identities=[UserIdentityInput(provider="github", provider_user_id="60011", provider_username="newmember")],
            send_invite=False,
        ),
    )

    writer.update_user_membership_orgs.assert_awaited()
    assert writer.update_user_membership_orgs.await_args.kwargs["member_org_ids"] == ["create-org-2"]


@patch.dict(os.environ, V2_ON)
async def test_attach_approved_member_survives_a_projection_failure(db_session: AsyncSession):
    """A DDB fault must not fail an approval whose Postgres rows are committed."""
    await _org(db_session, "existing-org-3")
    await db_session.commit()

    request = _pending_request(tenant_id="existing-org-3", login="joiner3", sub="sub-joiner-3", provider_user_id="90005")
    db_session.add(request)
    await db_session.flush()

    writer = MagicMock()
    writer.update_user_membership_orgs = AsyncMock(side_effect=RuntimeError("ddb down"))
    with (
        patch("src.admin.identity.identity_index_writer.IdentityIndexWriter", return_value=writer),
        patch("src.admin.onboarding.approval.sync_cognito_role_claims"),
    ):
        tenant_id = await attach_approved_member(
            db_session,
            request,
            granted_role="member",
            decided_by="admin-1",
        )

    assert tenant_id == "existing-org-3"
    assert request.status == "approved"
    user = await db_session.scalar(select(User).where(User.cognito_sub == "sub-joiner-3"))
    assert user is not None
    rows = list((await db_session.execute(select(TenantMembership).where(TenantMembership.user_id == user.id))).scalars().all())
    assert [r.tenant_id for r in rows] == ["existing-org-3"]
