"""Role-update endpoint: PUT /admin/organizations/{org_id}/users/{user_id} (#4019).

Before this endpoint existed, role changes happened only out-of-band via Cognito
CLI scripts — the product had no way to promote or demote anyone.

**Why these tests assert what they assert.** The architect review of #4019 found
that the obvious implementation is a silent authorization no-op, and — critically
— that the obvious *tests* pass against the broken behavior:

- Post-#3998/#4026, ``tenant_memberships.role`` is the only store conferring
  org-level authority. A write to ``users.role`` + Cognito ``custom:role``
  changes the UI badge and the token claim while granting nothing. Asserting the
  API response (200) or ``users.role`` therefore proves nothing.
- ``upsert_tenant_membership`` deliberately never *lowers* a role, so a demotion
  routed through it returns 200 while the user keeps full authority.

So the promotion/demotion tests here assert **effective authorization outcomes**:
they resolve the target's role through the real ``AccessControl`` afterwards and
check that a permission the new role governs is actually granted/denied. The
``tenant_memberships.role`` column is asserted alongside as the proximate cause.
"""

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.admin.access_control import AccessControl
from src.admin.config import AdminRole, Permission
from src.admin.exceptions import AccessDeniedError, InvalidScopeError
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.schemas.auth import TokenContext

pytestmark = pytest.mark.asyncio


def make_context(user_id: str, org_id: str, *, is_admin: bool = False) -> TokenContext:
    """Build a token context. user_id is the Cognito sub, as in production."""
    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id="team-001",
        department_id="dept-001",
        account_type="human",
        is_admin=is_admin,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


async def seed_user(
    db,
    *,
    pg_id: str,
    cognito_sub: str,
    org_id: str,
    users_role: str = "member",
    membership_role: str | None = None,
) -> None:
    """Create a users row and, when given, its authoritative membership row."""
    db.add(
        User(
            id=pg_id,
            org_id=org_id,
            team_id="team-001",
            email=f"{pg_id}@example.test",
            name=pg_id,
            cognito_sub=cognito_sub,
            role=users_role,
        )
    )
    await db.flush()
    if membership_role is not None:
        db.add(
            TenantMembership(
                user_id=pg_id,
                tenant_id=org_id,
                role=membership_role,
                is_active=True,
                joined_via="test_seed",
            )
        )
    await db.flush()


@pytest.fixture
async def seeded(db_engine):
    """Two orgs, an org_admin caller, a member target, and a foreign-org user."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        session.add(Organization(id="org-001", name="Org One"))
        session.add(Organization(id="org-002", name="Org Two"))
        await session.flush()
        # The caller: a genuine org admin (membership row, not just a claim).
        await seed_user(
            session,
            pg_id="pg-caller",
            cognito_sub="sub-caller",
            org_id="org-001",
            users_role="org_admin",
            membership_role="org_admin",
        )
        # The target: a plain member of the same org.
        await seed_user(
            session,
            pg_id="pg-target",
            cognito_sub="sub-target",
            org_id="org-001",
            users_role="member",
            membership_role="member",
        )
        # A user in a different org, for the tenant-isolation test.
        await seed_user(
            session,
            pg_id="pg-foreign",
            cognito_sub="sub-foreign",
            org_id="org-002",
            users_role="member",
            membership_role="member",
        )
        # A platform-level target an org_admin must not be able to touch.
        await seed_user(
            session,
            pg_id="pg-platform",
            cognito_sub="sub-platform",
            org_id="org-001",
            users_role="platform_admin",
            membership_role="org_admin",
        )
        await session.commit()


@pytest.fixture
def cognito_env():
    """Pool id present + a mock boto3 client, so the sync path is exercised."""
    mock_client = MagicMock()
    mock_client.list_users.return_value = {
        "Users": [{"Username": "sub-target", "Attributes": [{"Name": "sub", "Value": "sub-target"}, {"Name": "custom:org_id", "Value": "org-001"}]}]
    }
    with (
        patch.dict(os.environ, {"BG_COGNITO_USER_POOL_ID": "us-east-1_pool"}, clear=False),
        patch("boto3.client", return_value=mock_client),
    ):
        yield mock_client


def client_for(db_engine, context: TokenContext) -> AsyncClient:
    """Build a test client whose authenticated caller is ``context``."""
    from src.app import create_app
    from src.auth.dependencies import get_current_user
    from src.shared.database import get_db

    app = create_app()

    async def override_db():
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    async def override_auth():
        return context

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_auth
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def membership_role_of(db_engine, pg_id: str, org_id: str = "org-001") -> str | None:
    """Read the authoritative role column straight out of the DB."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        row = (
            await session.execute(
                select(TenantMembership.role).where(
                    TenantMembership.user_id == pg_id,
                    TenantMembership.tenant_id == org_id,
                )
            )
        ).scalar_one_or_none()
    return row


async def effective_role(db_engine, cognito_sub: str, org_id: str = "org-001") -> AdminRole:
    """Resolve a user's role the way production authz does — via AccessControl."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        access = AccessControl(db=session)
        role, _, _ = await access.get_user_role(make_context(cognito_sub, org_id))
    return role


class TestPromotion:
    """An org_admin promoting a member to dept_admin must actually grant authority."""

    async def test_org_admin_promotes_member_to_dept_admin(self, db_engine, seeded, cognito_env):
        async with client_for(db_engine, make_context("sub-caller", "org-001")) as client:
            resp = await client.put(
                "/admin/organizations/org-001/users/pg-target",
                json={"role": "dept_admin"},
            )

        assert resp.status_code == 200
        # Proximate cause: the authoritative column moved.
        assert await membership_role_of(db_engine, "pg-target") == "dept_admin"
        # What actually matters: authorization now resolves to the new role.
        assert await effective_role(db_engine, "sub-target") == AdminRole.DEPT_ADMIN

    async def test_platform_admin_may_assign_any_role(self, db_engine, seeded, cognito_env):
        async with client_for(db_engine, make_context("sub-root", "org-001", is_admin=True)) as client:
            resp = await client.put(
                "/admin/organizations/org-001/users/pg-target",
                json={"role": "org_admin"},
            )

        assert resp.status_code == 200
        assert await membership_role_of(db_engine, "pg-target") == "org_admin"
        assert await effective_role(db_engine, "sub-target") == AdminRole.ORG_ADMIN

    async def test_promotion_grants_the_permission_the_role_governs(self, db_engine, seeded, cognito_env):
        """End-to-end authority check: a member cannot manage users; after promotion to
        org_admin the same principal can. This is the assertion a users.role-only
        implementation fails."""
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            access = AccessControl(db=session)
            with pytest.raises(AccessDeniedError):
                await access.check_permission(
                    make_context("sub-target", "org-001"),
                    Permission.USER_MANAGE,
                    target_org_id="org-001",
                )

        async with client_for(db_engine, make_context("sub-root", "org-001", is_admin=True)) as client:
            resp = await client.put(
                "/admin/organizations/org-001/users/pg-target",
                json={"role": "org_admin"},
            )
        assert resp.status_code == 200

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            access = AccessControl(db=session)
            assert await access.check_permission(
                make_context("sub-target", "org-001"),
                Permission.USER_MANAGE,
                target_org_id="org-001",
            )


class TestDemotionActuallyRevokes:
    """The highest-severity case in #4019.

    ``upsert_tenant_membership`` never lowers a role, so a demotion routed through
    it returns 200 and leaves the user fully privileged. These tests fail against
    that behavior and pass only with a demotion-capable write.
    """

    async def test_demotion_lowers_the_authoritative_column(self, db_engine, seeded, cognito_env):
        assert await membership_role_of(db_engine, "pg-platform") == "org_admin"

        async with client_for(db_engine, make_context("sub-root", "org-001", is_admin=True)) as client:
            resp = await client.put(
                "/admin/organizations/org-001/users/pg-platform",
                json={"role": "member"},
            )

        assert resp.status_code == 200
        assert await membership_role_of(db_engine, "pg-platform") == "member"

    async def test_demotion_revokes_effective_authorization(self, db_engine, seeded, cognito_env):
        """The security-critical assertion: after demotion the user is DENIED a
        permission their old role granted."""
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            access = AccessControl(db=session)
            assert await access.check_permission(
                make_context("sub-platform", "org-001"),
                Permission.USER_MANAGE,
                target_org_id="org-001",
            )

        async with client_for(db_engine, make_context("sub-root", "org-001", is_admin=True)) as client:
            resp = await client.put(
                "/admin/organizations/org-001/users/pg-platform",
                json={"role": "member"},
            )
        assert resp.status_code == 200

        assert await effective_role(db_engine, "sub-platform") == AdminRole.MEMBER
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            access = AccessControl(db=session)
            with pytest.raises(AccessDeniedError):
                await access.check_permission(
                    make_context("sub-platform", "org-001"),
                    Permission.USER_MANAGE,
                    target_org_id="org-001",
                )

    async def test_demotion_never_deletes_the_membership_row(self, db_engine, seeded, cognito_env):
        """ "Remove role" means demote to member, not delete the row: a no-row
        principal log-spams ``rbac_role_fallback`` on every request and would
        silently regain ORG_ADMIN if the least-privilege lever were rolled back."""
        async with client_for(db_engine, make_context("sub-root", "org-001", is_admin=True)) as client:
            await client.put(
                "/admin/organizations/org-001/users/pg-platform",
                json={"role": "member"},
            )

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            rows = (await session.execute(select(TenantMembership).where(TenantMembership.user_id == "pg-platform"))).scalars().all()
        assert len(rows) == 1
        assert rows[0].role == "member"
        assert rows[0].is_active is True


class TestCeilingAndTargetGuards:
    async def test_org_admin_cannot_assign_platform_admin(self, db_engine, seeded, cognito_env):
        """The escalation vector: an org_admin must not grant platform authority."""
        async with client_for(db_engine, make_context("sub-caller", "org-001")) as client:
            resp = await client.put(
                "/admin/organizations/org-001/users/pg-target",
                json={"role": "platform_admin"},
            )

        assert resp.status_code == 403
        # And nothing moved.
        assert await membership_role_of(db_engine, "pg-target") == "member"
        assert await effective_role(db_engine, "sub-target") == AdminRole.MEMBER

    async def test_org_admin_cannot_modify_a_platform_admin_target(self, db_engine, seeded, cognito_env):
        """Every individual check passes (own org, member is below the ceiling), yet
        the operation would strip a superior's privilege — require_modifiable_target
        is what stops it."""
        async with client_for(db_engine, make_context("sub-caller", "org-001")) as client:
            resp = await client.put(
                "/admin/organizations/org-001/users/pg-platform",
                json={"role": "member"},
            )

        assert resp.status_code == 403
        assert await membership_role_of(db_engine, "pg-platform") == "org_admin"

    async def test_caller_cannot_change_their_own_role(self, db_engine, seeded, cognito_env):
        """Replaces the unimplementable last-platform-admin count (#3981): blocks the
        realistic lockout of an admin demoting themselves."""
        async with client_for(db_engine, make_context("sub-caller", "org-001")) as client:
            resp = await client.put(
                "/admin/organizations/org-001/users/pg-caller",
                json={"role": "member"},
            )

        assert resp.status_code == 403
        assert await membership_role_of(db_engine, "pg-caller") == "org_admin"

    async def test_platform_admin_cannot_change_their_own_role(self, db_engine, seeded, cognito_env):
        """The self-demotion block applies to platform admins too — they are exactly
        who the lockout guard exists for."""
        async with client_for(db_engine, make_context("sub-caller", "org-001", is_admin=True)) as client:
            resp = await client.put(
                "/admin/organizations/org-001/users/pg-caller",
                json={"role": "member"},
            )

        assert resp.status_code == 403
        assert await membership_role_of(db_engine, "pg-caller") == "org_admin"

    async def test_unknown_role_rejected(self, db_engine, seeded, cognito_env):
        async with client_for(db_engine, make_context("sub-caller", "org-001")) as client:
            resp = await client.put(
                "/admin/organizations/org-001/users/pg-target",
                json={"role": "superuser"},
            )

        assert resp.status_code == 400
        assert await membership_role_of(db_engine, "pg-target") == "member"


class TestTenantIsolation:
    async def test_cross_org_update_denied_before_any_cognito_call(self, db_engine, seeded, cognito_env):
        """An org_admin must not edit another org's user — and must be stopped before
        any Cognito write is attempted."""
        async with client_for(db_engine, make_context("sub-caller", "org-001")) as client:
            resp = await client.put(
                "/admin/organizations/org-002/users/pg-foreign",
                json={"role": "org_admin"},
            )

        assert resp.status_code == 403
        cognito_env.admin_update_user_attributes.assert_not_called()
        assert await membership_role_of(db_engine, "pg-foreign", "org-002") == "member"
        assert await effective_role(db_engine, "sub-foreign", "org-002") == AdminRole.MEMBER

    async def test_user_outside_org_is_404(self, db_engine, seeded, cognito_env):
        """A platform admin targeting the right user under the wrong org gets 404,
        not a cross-tenant write."""
        async with client_for(db_engine, make_context("sub-root", "org-001", is_admin=True)) as client:
            resp = await client.put(
                "/admin/organizations/org-001/users/pg-foreign",
                json={"role": "org_admin"},
            )

        assert resp.status_code == 404
        assert await membership_role_of(db_engine, "pg-foreign", "org-002") == "member"


class TestCognitoSyncIsBestEffort:
    """DB-first ordering (#4019 I1, mirroring onboarding/approval.py).

    Cognito-first would invert the risk: a Cognito success + DB failure leaves a
    token asserting authority the authoritative store never granted.
    """

    async def test_cognito_failure_leaves_the_authoritative_write_committed(self, db_engine, seeded):
        mock_client = MagicMock()
        mock_client.admin_update_user_attributes.side_effect = Exception("cognito down")
        with (
            patch.dict(os.environ, {"BG_COGNITO_USER_POOL_ID": "us-east-1_pool"}, clear=False),
            patch("boto3.client", return_value=mock_client),
        ):
            async with client_for(db_engine, make_context("sub-root", "org-001", is_admin=True)) as client:
                resp = await client.put(
                    "/admin/organizations/org-001/users/pg-target",
                    json={"role": "dept_admin"},
                )

        assert resp.status_code == 200
        assert await membership_role_of(db_engine, "pg-target") == "dept_admin"
        assert await effective_role(db_engine, "sub-target") == AdminRole.DEPT_ADMIN

    async def test_role_claim_is_written_to_cognito(self, db_engine, seeded, cognito_env):
        async with client_for(db_engine, make_context("sub-root", "org-001", is_admin=True)) as client:
            resp = await client.put(
                "/admin/organizations/org-001/users/pg-target",
                json={"role": "dept_admin"},
            )

        assert resp.status_code == 200
        cognito_env.admin_update_user_attributes.assert_called_once()
        kwargs = cognito_env.admin_update_user_attributes.call_args.kwargs
        assert kwargs["Username"] == "sub-target"
        attrs = {a["Name"]: a["Value"] for a in kwargs["UserAttributes"]}
        assert attrs["custom:role"] == "dept_admin"
        assert attrs == {"custom:role": "dept_admin"}

    async def test_github_username_is_resolved_before_writing(self, db_engine, seeded):
        """Use the exact-sub lookup's actual username, checking its selected org."""
        mock_client = MagicMock()
        mock_client.list_users.return_value = {
            "Users": [
                {"Username": "GitHub_20402445", "Attributes": [{"Name": "sub", "Value": "sub-target"}, {"Name": "custom:org_id", "Value": "org-001"}]}
            ]
        }

        with (
            patch.dict(os.environ, {"BG_COGNITO_USER_POOL_ID": "us-east-1_pool"}, clear=False),
            patch("boto3.client", return_value=mock_client),
        ):
            async with client_for(db_engine, make_context("sub-root", "org-001", is_admin=True)) as client:
                resp = await client.put(
                    "/admin/organizations/org-001/users/pg-target",
                    json={"role": "dept_admin"},
                )

        assert resp.status_code == 200
        mock_client.list_users.assert_called_once()
        assert mock_client.list_users.call_args.kwargs["Filter"] == 'sub = "sub-target"'
        assert mock_client.admin_update_user_attributes.call_count == 1
        assert mock_client.admin_update_user_attributes.call_args.kwargs["Username"] == "GitHub_20402445"


class TestUsersRoleMirror:
    async def test_platform_role_diverges_from_membership_by_design(self, db_engine, seeded, cognito_env):
        """A platform-level grant stores ``org_admin`` in the tenant row (a scoped row
        must never confer platform authority, #3981) while ``users.role`` echoes what
        was requested. Pinned so nobody "fixes" the normalization."""
        async with client_for(db_engine, make_context("sub-root", "org-001", is_admin=True)) as client:
            resp = await client.put(
                "/admin/organizations/org-001/users/pg-target",
                json={"role": "platform_admin"},
            )

        assert resp.status_code == 200
        assert resp.json()["role"] == "platform_admin"
        assert await membership_role_of(db_engine, "pg-target") == "org_admin"

    async def test_name_only_update_leaves_role_untouched(self, db_engine, seeded, cognito_env):
        async with client_for(db_engine, make_context("sub-caller", "org-001")) as client:
            resp = await client.put(
                "/admin/organizations/org-001/users/pg-target",
                json={"name": "Renamed Person"},
            )

        assert resp.status_code == 200
        assert resp.json()["name"] == "Renamed Person"
        assert await membership_role_of(db_engine, "pg-target") == "member"
        # No role in the request means no Cognito claim write.
        cognito_env.admin_update_user_attributes.assert_not_called()


class TestAvailableRolesIsCeilingFiltered:
    """The picker must only offer roles the server will accept (#4019 C4).

    The old hardcoded list omitted ``dept_admin`` (so the UI could not offer the
    supported promotion) and included ``service_account``, which is absent from
    ROLE_RANK and so raised InvalidRoleError for every non-platform caller.
    """

    async def test_org_admin_list_excludes_platform_roles(self, db_engine, seeded):
        async with client_for(db_engine, make_context("sub-caller", "org-001")) as client:
            resp = await client.get("/admin/users/roles")

        assert resp.status_code == 200
        roles = resp.json()["roles"]
        assert "platform_admin" not in roles
        assert "admin" not in roles
        assert "dept_admin" in roles
        assert "member" in roles

    async def test_no_role_is_offered_that_the_server_would_reject(self, db_engine, seeded):
        """Every offered role must survive the caller's own assignment ceiling."""
        async with client_for(db_engine, make_context("sub-caller", "org-001")) as client:
            resp = await client.get("/admin/users/roles")
        roles = resp.json()["roles"]

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            access = AccessControl(db=session)
            for role in roles:
                await access.require_assignable_role(make_context("sub-caller", "org-001"), role, target_org_id="org-001")

    async def test_service_account_is_never_offered(self, db_engine, seeded):
        """It is not in ROLE_RANK, so offering it guaranteed an InvalidRoleError."""
        async with client_for(db_engine, make_context("sub-caller", "org-001")) as client:
            resp = await client.get("/admin/users/roles")
        assert "service_account" not in resp.json()["roles"]

    async def test_platform_admin_sees_every_assignable_role(self, db_engine, seeded):
        async with client_for(db_engine, make_context("sub-root", "org-001", is_admin=True)) as client:
            resp = await client.get("/admin/users/roles")

        roles = resp.json()["roles"]
        assert "platform_admin" in roles
        assert "dept_admin" in roles


class TestRequireModifiableTarget:
    """Unit-level coverage of the new guard's own contract."""

    async def test_platform_admin_may_modify_anyone(self, access_control, platform_admin_context):
        await access_control.require_modifiable_target(platform_admin_context, "org_admin")
        await access_control.require_modifiable_target(platform_admin_context, "platform_admin", target_is_platform_admin=True)

    async def test_org_admin_may_modify_member_and_dept_admin(self, access_control, org_admin_context, org_admin_membership):
        await access_control.require_modifiable_target(org_admin_context, "member")
        await access_control.require_modifiable_target(org_admin_context, "dept_admin")
        await access_control.require_modifiable_target(org_admin_context, "org_admin")

    async def test_org_admin_cannot_modify_platform_level_target(self, access_control, org_admin_context, org_admin_membership):
        with pytest.raises(AccessDeniedError):
            await access_control.require_modifiable_target(org_admin_context, "platform_admin")
        with pytest.raises(AccessDeniedError):
            await access_control.require_modifiable_target(org_admin_context, "admin")
        with pytest.raises(AccessDeniedError):
            await access_control.require_modifiable_target(org_admin_context, "member", target_is_platform_admin=True)

    async def test_dept_admin_cannot_modify_org_admin(self, access_control, dept_admin_context):
        with pytest.raises(AccessDeniedError):
            await access_control.require_modifiable_target(dept_admin_context, "org_admin")

    async def test_unknown_target_role_is_treated_as_member(self, access_control, org_admin_context, org_admin_membership):
        """A no-membership principal resolves to MEMBER, so an unrecognized stored
        string must stay modifiable rather than becoming permanently untouchable."""
        await access_control.require_modifiable_target(org_admin_context, "some-legacy-string")
        await access_control.require_modifiable_target(org_admin_context, None)


class TestSetMembershipRoleHelper:
    """``set_membership_role`` must lower, unlike ``upsert_tenant_membership``."""

    async def test_lowers_an_existing_role(self, db_session):
        from src.admin.memberships import set_membership_role

        db_session.add(Organization(id="org-h", name="H"))
        await db_session.flush()
        await seed_user(
            db_session,
            pg_id="pg-h",
            cognito_sub="sub-h",
            org_id="org-h",
            users_role="org_admin",
            membership_role="org_admin",
        )

        await set_membership_role(db_session, user_id="pg-h", tenant_id="org-h", role="member")

        row = (await db_session.execute(select(TenantMembership).where(TenantMembership.user_id == "pg-h"))).scalar_one()
        assert row.role == "member"

    async def test_upsert_helper_still_refuses_to_lower(self, db_session):
        """Guards the boundary: the idempotent onboarding helper must keep its
        never-lower rule, which is correct for its own callers."""
        from src.admin.memberships import upsert_tenant_membership

        db_session.add(Organization(id="org-u", name="U"))
        await db_session.flush()
        await seed_user(
            db_session,
            pg_id="pg-u",
            cognito_sub="sub-u",
            org_id="org-u",
            users_role="org_admin",
            membership_role="org_admin",
        )

        await upsert_tenant_membership(db_session, user_id="pg-u", tenant_id="org-u", role="member", joined_via="test")

        row = (await db_session.execute(select(TenantMembership).where(TenantMembership.user_id == "pg-u"))).scalar_one()
        assert row.role == "org_admin"

    async def test_normalizes_platform_role_to_org_admin(self, db_session):
        from src.admin.memberships import set_membership_role

        db_session.add(Organization(id="org-n", name="N"))
        await db_session.flush()
        await seed_user(
            db_session,
            pg_id="pg-n",
            cognito_sub="sub-n",
            org_id="org-n",
            users_role="member",
            membership_role="member",
        )

        await set_membership_role(db_session, user_id="pg-n", tenant_id="org-n", role="platform_admin")

        row = (await db_session.execute(select(TenantMembership).where(TenantMembership.user_id == "pg-n"))).scalar_one()
        assert row.role == "org_admin"

    async def test_creates_a_row_when_none_exists(self, db_session):
        from src.admin.memberships import set_membership_role

        db_session.add(Organization(id="org-c", name="C"))
        await db_session.flush()
        await seed_user(db_session, pg_id="pg-c", cognito_sub="sub-c", org_id="org-c", users_role="member")

        await set_membership_role(db_session, user_id="pg-c", tenant_id="org-c", role="dept_admin")

        row = (await db_session.execute(select(TenantMembership).where(TenantMembership.user_id == "pg-c"))).scalar_one()
        assert row.role == "dept_admin"
        assert row.is_active is True


class TestScopeErrorsAreDistinct:
    """Sanity: the two guards raise different, correctly-flavoured errors."""

    async def test_assignable_role_error_is_about_the_grant(self, access_control, org_admin_context, org_admin_membership):
        with pytest.raises(InvalidScopeError):
            await access_control.require_assignable_role(org_admin_context, "platform_admin", target_org_id="org-001")

    async def test_modifiable_target_error_mentions_the_target(self, access_control, org_admin_context, org_admin_membership):
        with pytest.raises(AccessDeniedError) as exc:
            await access_control.require_modifiable_target(org_admin_context, "platform_admin")
        assert "platform administrator" in str(exc.value).lower()
