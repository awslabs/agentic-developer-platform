"""Server-side role resolution from tenant_memberships (Issue #3987).

Child C of sub-EPIC #3984. `AccessControl.get_user_role()` used to derive the
caller's role purely from token claims, mapping every non-platform authenticated
principal to ORG_ADMIN for their own org. These tests pin the new authority
model: the token establishes identity, `tenant_memberships.role` establishes
org-level authority, and anything unresolvable fails closed.

PR 1 (#3998) shipped the row lookup but kept the permissive no-row ORG_ADMIN
fallback behind `rbac_least_privilege_default=False`. PR 2 (#4015) flipped that
default to True, so a principal with no active admin-level row now resolves to
MEMBER. `BG_ADMIN_RBAC_LEAST_PRIVILEGE_DEFAULT=false` is the rollback lever.

Note these tests use the REAL AccessControl against a real session — the specs in
tests/usage/ and tests/activity/ mock AccessControl and give zero coverage here.
"""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import (
    AdminConfig,
    AdminRole,
    Permission,
    get_admin_config,
    membership_role_to_admin_role,
    set_admin_config,
)
from src.admin.exceptions import AccessDeniedError
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.schemas.auth import TokenContext

pytestmark = pytest.mark.asyncio


def make_context(cognito_sub: str, org_id: str, *, is_admin: bool = False) -> TokenContext:
    """Build a token context. user_id is the Cognito sub, as in production."""
    return TokenContext(
        user_id=cognito_sub,
        org_id=org_id,
        team_id="team-001",
        department_id="dept-001",
        account_type="human",
        is_admin=is_admin,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


async def seed_org(db: AsyncSession, org_id: str) -> None:
    db.add(Organization(id=org_id, name=f"org-name-{org_id}"))
    await db.flush()


async def seed_user(db: AsyncSession, *, pg_id: str, cognito_sub: str, org_id: str, users_role: str = "member") -> None:
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


async def seed_membership(db: AsyncSession, *, pg_user_id: str, tenant_id: str, role: str, is_active: bool = True) -> None:
    db.add(
        TenantMembership(
            user_id=pg_user_id,
            tenant_id=tenant_id,
            role=role,
            is_active=is_active,
            joined_via="org_membership",
        )
    )
    await db.flush()


@pytest.fixture
def least_privilege_config():
    """Pin least privilege explicitly.

    This is the shipped default since #3987 PR 2, so the fixture is no longer what
    *enables* the behaviour — it keeps these specs independent of the default (and
    of the rollback lever) rather than silently coupled to it. The default itself
    is pinned by ``test_least_privilege_is_the_shipped_default``.
    """
    set_admin_config(AdminConfig(rbac_least_privilege_default=True))
    yield
    set_admin_config(AdminConfig())


@pytest.fixture(autouse=True)
def reset_config():
    """Keep the module-level config singleton from leaking across tests."""
    yield
    set_admin_config(AdminConfig())


class TestMembershipRoleMapping:
    """The fail-closed string -> AdminRole mapper."""

    async def test_org_admin_maps_to_org_admin(self):
        assert membership_role_to_admin_role("org_admin") == AdminRole.ORG_ADMIN

    async def test_member_maps_to_member(self):
        assert membership_role_to_admin_role("member") == AdminRole.MEMBER

    @pytest.mark.parametrize("value", [None, "", "   ", "wat", "superuser", "OWNER"])
    async def test_unknown_or_null_maps_to_least_privilege(self, value):
        """Unknown/NULL must fail closed to MEMBER — never ORG_ADMIN, never raise.

        A raise would surface as a 500 rather than the 403 an unprivileged caller
        must receive (get_role_permissions raises InvalidRoleError).
        """
        assert membership_role_to_admin_role(value) == AdminRole.MEMBER

    async def test_case_and_whitespace_insensitive(self):
        assert membership_role_to_admin_role("  Org_Admin  ") == AdminRole.ORG_ADMIN

    async def test_platform_strings_do_not_confer_platform_admin(self):
        """A tenant membership row must never grant unscoped platform authority.

        Regression guard for #3981, which removed the org_admin -> platform_admin
        escalation bridge. A membership row is scoped to one tenant by
        construction, so 'admin'/'platform_admin' stored there maps to the
        tenant-scoped ORG_ADMIN.
        """
        assert membership_role_to_admin_role("platform_admin") == AdminRole.ORG_ADMIN
        assert membership_role_to_admin_role("admin") == AdminRole.ORG_ADMIN


class TestDbBackedRoleResolution:
    """get_user_role() reads authority from tenant_memberships."""

    async def test_org_admin_membership_resolves_to_org_admin(self, db_session: AsyncSession):
        await seed_org(db_session, "org-a")
        await seed_user(db_session, pg_id="pg-user-1", cognito_sub="sub-1", org_id="org-a")
        await seed_membership(db_session, pg_user_id="pg-user-1", tenant_id="org-a", role="org_admin")
        await db_session.commit()

        ac = AccessControl(db=db_session)
        role, org_id, dept_id = await ac.get_user_role(make_context("sub-1", "org-a"))

        assert role == AdminRole.ORG_ADMIN
        assert org_id == "org-a"
        assert dept_id is None

    async def test_resolution_goes_through_cognito_sub(self, db_session: AsyncSession):
        """TenantMembership.user_id is users.id, but the token carries the Cognito sub.

        If the lookup used context.user_id directly it would match zero rows and
        every caller would silently take the fallback path — which would make the
        PR 2 flip lock out 100% of users. This test pins the indirection: the
        Cognito sub and the Postgres user id are deliberately different values.
        """
        await seed_org(db_session, "org-a")
        await seed_user(db_session, pg_id="pg-uuid-distinct", cognito_sub="cognito-sub-distinct", org_id="org-a")
        await seed_membership(db_session, pg_user_id="pg-uuid-distinct", tenant_id="org-a", role="org_admin")
        await db_session.commit()

        ac = AccessControl(db=db_session)
        role, _, _ = await ac.get_user_role(make_context("cognito-sub-distinct", "org-a"))

        assert role == AdminRole.ORG_ADMIN

    async def test_member_membership_resolves_to_member(self, db_session: AsyncSession, least_privilege_config):
        await seed_org(db_session, "org-a")
        await seed_user(db_session, pg_id="pg-user-2", cognito_sub="sub-2", org_id="org-a")
        await seed_membership(db_session, pg_user_id="pg-user-2", tenant_id="org-a", role="member")
        await db_session.commit()

        ac = AccessControl(db=db_session)
        role, org_id, _ = await ac.get_user_role(make_context("sub-2", "org-a"))

        assert role == AdminRole.MEMBER
        assert org_id == "org-a"

    async def test_member_row_is_not_org_admin_independent_of_the_flip(self, db_session: AsyncSession):
        """A resolved 'member' row is authoritative regardless of the fallback flag.

        Takes no config fixture on purpose: the row path must not be affected by
        ``rbac_least_privilege_default`` in either direction. This was the security
        value PR 1 (#3998) delivered on its own — a principal who HAS a membership
        row is never auto-promoted to ORG_ADMIN — and PR 2 must not disturb it.
        """
        await seed_org(db_session, "org-a")
        await seed_user(db_session, pg_id="pg-user-3", cognito_sub="sub-3", org_id="org-a")
        await seed_membership(db_session, pg_user_id="pg-user-3", tenant_id="org-a", role="member")
        await db_session.commit()

        ac = AccessControl(db=db_session)
        role, _, _ = await ac.get_user_role(make_context("sub-3", "org-a"))

        assert role == AdminRole.MEMBER

    async def test_active_row_wins_over_token_org_id(self, db_session: AsyncSession):
        """Role and scope must both come from the active membership row.

        After switch-tenant the token still carries the previous org_id until
        refresh. Resolving role against one tenant and org_id against another is
        the split-brain the design calls out.
        """
        await seed_org(db_session, "org-stale")
        await seed_org(db_session, "org-active")
        await seed_user(db_session, pg_id="pg-user-4", cognito_sub="sub-4", org_id="org-stale")
        await seed_membership(db_session, pg_user_id="pg-user-4", tenant_id="org-stale", role="member", is_active=False)
        await seed_membership(db_session, pg_user_id="pg-user-4", tenant_id="org-active", role="org_admin", is_active=True)
        await db_session.commit()

        ac = AccessControl(db=db_session)
        role, org_id, _ = await ac.get_user_role(make_context("sub-4", "org-stale"))

        assert role == AdminRole.ORG_ADMIN
        assert org_id == "org-active"


class TestFailClosedFallback:
    """Unresolvable principals must never get ORG_ADMIN once the flip lands."""

    async def test_unmapped_principal_is_least_privilege_after_flip(self, db_session: AsyncSession, least_privilege_config):
        """The headline fix: no membership row -> MEMBER, not ORG_ADMIN."""
        ac = AccessControl(db=db_session)
        role, _, _ = await ac.get_user_role(make_context("sub-nobody", "org-a"))

        assert role == AdminRole.MEMBER

    async def test_least_privilege_is_the_shipped_default(self, db_session: AsyncSession):
        """#3987 PR 2: no-row -> MEMBER with NO flag set anywhere.

        Deliberately takes no config fixture. Every other no-row test opts in via
        ``least_privilege_config``, which would still pass if the shipped default
        were False — this one proves the flip is *effective*, not merely settable,
        and is the test that fails if someone reverts config.py.
        """
        ac = AccessControl(db=db_session)
        role, org_id, _ = await ac.get_user_role(make_context("sub-nobody", "org-a"))

        assert role == AdminRole.MEMBER
        assert org_id == "org-a"

    async def test_env_var_rolls_back_to_legacy_org_admin_fallback(self, db_session: AsyncSession, monkeypatch):
        """BG_ADMIN_RBAC_LEAST_PRIVILEGE_DEFAULT=false restores ORG_ADMIN.

        The documented rollback lever for #3987 PR 2 — an operator must be able to
        undo the flip without a revert. Goes through the env var rather than the
        kwarg so the ``BG_ADMIN_`` prefix wiring is covered too: a typo'd env
        name would silently leave the lever dead.

        Replaces the former ``test_unmapped_principal_keeps_org_admin_before_flip``,
        which asserted the same ORG_ADMIN outcome as the *default*.
        """
        monkeypatch.setenv("BG_ADMIN_RBAC_LEAST_PRIVILEGE_DEFAULT", "false")
        set_admin_config(AdminConfig())
        assert get_admin_config().rbac_least_privilege_default is False

        ac = AccessControl(db=db_session)
        role, org_id, _ = await ac.get_user_role(make_context("sub-nobody", "org-a"))

        assert role == AdminRole.ORG_ADMIN
        assert org_id == "org-a"

    async def test_no_row_demotion_is_logged(self, db_session: AsyncSession, caplog):
        """The demotion must stay greppable post-flip.

        Pre-flip the ``rbac_role_fallback`` WARN was emitted only in the
        ``not least_privilege`` branch, so making least privilege the default
        would have silenced it exactly when it started to matter. Operators grep
        this line to find no-row principals.
        """
        ac = AccessControl(db=db_session)
        with caplog.at_level(logging.WARNING, logger="src.admin.access_control"):
            await ac.get_user_role(make_context("sub-nobody", "org-a"))

        assert "rbac_role_fallback" in caplog.text
        assert "reason=no_active_membership" in caplog.text
        assert f"granted={AdminRole.MEMBER.value}" in caplog.text

    async def test_user_exists_but_has_no_membership_rows(self, db_session: AsyncSession, least_privilege_config):
        await seed_org(db_session, "org-a")
        await seed_user(db_session, pg_id="pg-user-5", cognito_sub="sub-5", org_id="org-a")
        await db_session.commit()

        ac = AccessControl(db=db_session)
        role, _, _ = await ac.get_user_role(make_context("sub-5", "org-a"))

        assert role == AdminRole.MEMBER

    async def test_no_db_session_does_not_crash(self, least_privilege_config):
        """AccessControl() is constructed with no session in tests/auth/.

        Must fail closed to least privilege rather than raising or falling open.
        """
        ac = AccessControl()
        role, _, _ = await ac.get_user_role(make_context("sub-6", "org-a"))

        assert role == AdminRole.MEMBER

    async def test_platform_admin_needs_no_db_session(self):
        """The is_admin path must stay valid with no session injected."""
        ac = AccessControl()
        role, org_id, dept_id = await ac.get_user_role(make_context("sub-admin", "platform", is_admin=True))

        assert role == AdminRole.PLATFORM_ADMIN
        assert org_id is None
        assert dept_id is None

    async def test_query_error_fails_closed(self, least_privilege_config):
        """A DB error must not fall open to a higher role."""

        class ExplodingSession:
            async def execute(self, *args, **kwargs):
                raise RuntimeError("connection reset")

        ac = AccessControl(db=ExplodingSession())
        role, _, _ = await ac.get_user_role(make_context("sub-7", "org-a"))

        assert role == AdminRole.MEMBER


class TestMemberPermissionEnforcement:
    """A member-role principal must be denied org-admin permissions."""

    @pytest.fixture
    async def member_ac(self, db_session: AsyncSession) -> AccessControl:
        await seed_org(db_session, "org-a")
        await seed_user(db_session, pg_id="pg-member", cognito_sub="sub-member", org_id="org-a")
        await seed_membership(db_session, pg_user_id="pg-member", tenant_id="org-a", role="member")
        await db_session.commit()
        return AccessControl(db=db_session)

    @pytest.mark.parametrize(
        "permission",
        [
            Permission.ORG_UPDATE,
            Permission.USER_MANAGE,
            Permission.BUDGET_UPDATE,
            Permission.RATELIMIT_UPDATE,
            Permission.LOGS_EXPORT,
        ],
    )
    async def test_member_denied_org_admin_permissions(self, member_ac: AccessControl, permission: Permission):
        """These are exactly the permissions every user used to inherit."""
        with pytest.raises(AccessDeniedError):
            await member_ac.check_permission(make_context("sub-member", "org-a"), permission)

    async def test_member_denied_is_403_not_500(self, member_ac: AccessControl):
        """An unmapped role reaching get_role_permissions would be InvalidRoleError (500).

        MEMBER carries a non-empty permission set precisely so denial surfaces as
        AccessDeniedError (403).
        """
        with pytest.raises(AccessDeniedError) as exc_info:
            await member_ac.check_permission(make_context("sub-member", "org-a"), Permission.ORG_UPDATE)

        assert exc_info.value.status_code == 403
        assert exc_info.value.details["user_role"] == AdminRole.MEMBER.value

    async def test_member_is_not_org_admin(self, member_ac: AccessControl):
        assert await member_ac.is_org_admin(make_context("sub-member", "org-a"), "org-a") is False

    async def test_member_cannot_assign_any_role(self, member_ac: AccessControl):
        """CALLER_ROLE_RANK[MEMBER] == 0, so no role is assignable."""
        with pytest.raises(AccessDeniedError):
            await member_ac.require_assignable_role(make_context("sub-member", "org-a"), "dept_admin", target_org_id="org-a")

    async def test_seeded_org_admin_retains_access(self, db_session: AsyncSession, least_privilege_config):
        """The single most important regression test — catches a bad backfill."""
        await seed_org(db_session, "org-a")
        await seed_user(db_session, pg_id="pg-admin", cognito_sub="sub-admin-2", org_id="org-a", users_role="org_admin")
        await seed_membership(db_session, pg_user_id="pg-admin", tenant_id="org-a", role="org_admin")
        await db_session.commit()

        ac = AccessControl(db=db_session)
        ctx = make_context("sub-admin-2", "org-a")

        assert await ac.check_permission(ctx, Permission.ORG_UPDATE) is True
        assert await ac.check_permission(ctx, Permission.USER_MANAGE) is True
        assert await ac.is_org_admin(ctx, "org-a") is True


class TestTenantIsolation:
    """A role in org A must grant nothing in org B."""

    async def test_org_admin_in_one_tenant_is_not_admin_in_another(self, db_session: AsyncSession, least_privilege_config):
        await seed_org(db_session, "org-a")
        await seed_org(db_session, "org-b")
        await seed_user(db_session, pg_id="pg-cross", cognito_sub="sub-cross", org_id="org-a")
        await seed_membership(db_session, pg_user_id="pg-cross", tenant_id="org-a", role="org_admin")
        await db_session.commit()

        ac = AccessControl(db=db_session)
        ctx = make_context("sub-cross", "org-a")

        assert await ac.is_org_admin(ctx, "org-a") is True
        assert await ac.is_org_admin(ctx, "org-b") is False

    async def test_cross_tenant_resource_access_denied(self, db_session: AsyncSession, least_privilege_config):
        await seed_org(db_session, "org-a")
        await seed_org(db_session, "org-b")
        await seed_user(db_session, pg_id="pg-cross-2", cognito_sub="sub-cross-2", org_id="org-a")
        await seed_membership(db_session, pg_user_id="pg-cross-2", tenant_id="org-a", role="org_admin")
        await db_session.commit()

        ac = AccessControl(db=db_session)
        ctx = make_context("sub-cross-2", "org-a")

        assert await ac.validate_resource_access(ctx, resource_org_id="org-a") is True
        with pytest.raises(AccessDeniedError):
            await ac.validate_resource_access(ctx, resource_org_id="org-b")

    async def test_membership_in_other_tenant_does_not_grant_token_org(self, db_session: AsyncSession, least_privilege_config):
        """org_admin in org-b must not confer authority in the token's org-a."""
        await seed_org(db_session, "org-a")
        await seed_org(db_session, "org-b")
        await seed_user(db_session, pg_id="pg-cross-3", cognito_sub="sub-cross-3", org_id="org-a")
        await seed_membership(db_session, pg_user_id="pg-cross-3", tenant_id="org-b", role="org_admin")
        await db_session.commit()

        ac = AccessControl(db=db_session)
        ctx = make_context("sub-cross-3", "org-a")

        # Scope is pinned to the resolved (active) membership tenant, so the
        # caller cannot act on org-a.
        assert await ac.is_org_admin(ctx, "org-a") is False


class TestRoleCache:
    """Cache correctness in an authority-resolution path."""

    async def test_cache_keyed_by_user_and_tenant(self, db_session: AsyncSession, least_privilege_config):
        """A role resolved for one tenant must not be served for another."""
        await seed_org(db_session, "org-a")
        await seed_org(db_session, "org-b")
        await seed_user(db_session, pg_id="pg-k", cognito_sub="sub-k", org_id="org-a")
        await seed_membership(db_session, pg_user_id="pg-k", tenant_id="org-a", role="org_admin")
        await db_session.commit()

        ac = AccessControl(db=db_session)
        await ac.get_user_role(make_context("sub-k", "org-a"))

        assert ("sub-k", "org-a") in ac._role_cache
        assert ("sub-k", "org-b") not in ac._role_cache

    async def test_cache_entry_expires(self, db_session: AsyncSession, least_privilege_config, monkeypatch):
        """A stale entry must not mask a role change forever."""
        set_admin_config(AdminConfig(rbac_least_privilege_default=True, rbac_role_cache_ttl_seconds=30.0))

        await seed_org(db_session, "org-a")
        await seed_user(db_session, pg_id="pg-ttl", cognito_sub="sub-ttl", org_id="org-a")
        await seed_membership(db_session, pg_user_id="pg-ttl", tenant_id="org-a", role="org_admin")
        await db_session.commit()

        ac = AccessControl(db=db_session)
        role, _, _ = await ac.get_user_role(make_context("sub-ttl", "org-a"))
        assert role == AdminRole.ORG_ADMIN

        # Demote in the DB — the cached entry would otherwise still say org_admin.
        await db_session.execute(TenantMembership.__table__.update().where(TenantMembership.user_id == "pg-ttl").values(role="member"))
        await db_session.commit()

        real_monotonic = __import__("time").monotonic
        monkeypatch.setattr("src.admin.access_control.time.monotonic", lambda: real_monotonic() + 3600)

        role, _, _ = await ac.get_user_role(make_context("sub-ttl", "org-a"))
        assert role == AdminRole.MEMBER
