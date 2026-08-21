"""Regression tests for AccessControl.require_assignable_role.

These guard a privilege-escalation vector: the ``role`` field on the
create-user endpoint (POST /admin/organizations/{org}/teams/{team}/users) is a
free-form string that becomes the new user's ``custom:role`` claim. The
ORG_UPDATE scope check gates *which org* an org_admin may write to, but nothing
gated *which role* they could grant — so an org_admin could create a user with
role="platform_admin" and escalate a user out of their own organization.

require_assignable_role enforces a ceiling: a caller may only grant a role at or
below their own privilege, and platform-level roles are platform-admin-only.
Org-admin -> org-admin delegation within the same org remains allowed (it is a
supported feature).
"""

import pytest

from src.admin.access_control import AccessControl
from src.admin.exceptions import AccessDeniedError, InvalidRoleError, InvalidScopeError
from src.shared.schemas.auth import TokenContext


class TestOrgAdminCannotEscalate:
    """An org_admin (is_admin=False) may not grant privilege above its own.

    Issue #3987 PR 2: every test here takes ``org_admin_membership`` so the caller
    is a *genuine* org admin. Without the row the caller resolves to MEMBER, which
    can assign nothing at all — the assertions would still pass but would no
    longer exercise the org_admin ceiling this file exists to guard.
    """

    async def test_org_admin_cannot_assign_platform_admin(self, access_control: AccessControl, org_admin_context: TokenContext, org_admin_membership):
        # The core regression: this is exactly the PoC vector.
        with pytest.raises(InvalidScopeError):
            await access_control.require_assignable_role(org_admin_context, "platform_admin", target_org_id="org-001")

    async def test_org_admin_cannot_assign_admin_alias(self, access_control: AccessControl, org_admin_context: TokenContext, org_admin_membership):
        # "admin" also resolves to platform admin in the is_admin predicate.
        with pytest.raises(InvalidScopeError):
            await access_control.require_assignable_role(org_admin_context, "admin", target_org_id="org-001")

    async def test_org_admin_cannot_bypass_with_case(self, access_control: AccessControl, org_admin_context: TokenContext, org_admin_membership):
        # Case/whitespace variants must not slip past the ceiling.
        with pytest.raises(InvalidScopeError):
            await access_control.require_assignable_role(org_admin_context, "  Platform_Admin ", target_org_id="org-001")

    async def test_org_admin_unknown_role_rejected(self, access_control: AccessControl, org_admin_context: TokenContext, org_admin_membership):
        with pytest.raises(InvalidRoleError):
            await access_control.require_assignable_role(org_admin_context, "superuser", target_org_id="org-001")

    async def test_org_admin_empty_role_rejected(self, access_control: AccessControl, org_admin_context: TokenContext, org_admin_membership):
        with pytest.raises(InvalidRoleError):
            await access_control.require_assignable_role(org_admin_context, "", target_org_id="org-001")


class TestOrgAdminDelegationPreserved:
    """Legitimate org-admin delegation of same/lower roles keeps working.

    Issue #3987 PR 2: these need ``org_admin_membership`` because the caller's
    ceiling is derived from their *resolved* role, which now comes from the
    ``tenant_memberships`` row rather than the removed no-row ORG_ADMIN fallback.
    The escalation tests above deliberately do NOT seed it — a MEMBER caller is
    denied at least as hard, so they still pin what they were written to pin.
    """

    async def test_org_admin_can_assign_org_admin(self, access_control: AccessControl, org_admin_context: TokenContext, org_admin_membership):
        # Feature: an org_admin may create more org_admins within its own org.
        await access_control.require_assignable_role(org_admin_context, "org_admin", target_org_id="org-001")

    async def test_org_admin_can_assign_regular_user(self, access_control: AccessControl, org_admin_context: TokenContext, org_admin_membership):
        await access_control.require_assignable_role(org_admin_context, "user", target_org_id="org-001")

    async def test_org_admin_can_assign_member(self, access_control: AccessControl, org_admin_context: TokenContext, org_admin_membership):
        await access_control.require_assignable_role(org_admin_context, "member", target_org_id="org-001")

    async def test_org_admin_can_assign_dept_admin(self, access_control: AccessControl, org_admin_context: TokenContext, org_admin_membership):
        await access_control.require_assignable_role(org_admin_context, "dept_admin", target_org_id="org-001")

    async def test_member_caller_cannot_assign_org_admin(self, access_control: AccessControl, org_admin_context: TokenContext):
        """Issue #3987 PR 2: the same token WITHOUT a membership row may delegate nothing.

        CALLER_ROLE_RANK[MEMBER] == 0, so a no-row principal can no longer mint
        org admins — previously the fallback made this succeed.
        """
        with pytest.raises(AccessDeniedError):
            await access_control.require_assignable_role(org_admin_context, "org_admin", target_org_id="org-001")


class TestPlatformAdminUnrestricted:
    """Platform admins keep the ability to assign any role."""

    async def test_platform_admin_can_assign_platform_admin(self, access_control: AccessControl, platform_admin_context: TokenContext):
        await access_control.require_assignable_role(platform_admin_context, "platform_admin", target_org_id="org-001")

    async def test_platform_admin_can_assign_org_admin(self, access_control: AccessControl, platform_admin_context: TokenContext):
        await access_control.require_assignable_role(platform_admin_context, "org_admin", target_org_id="org-001")

    async def test_platform_admin_can_assign_user(self, access_control: AccessControl, platform_admin_context: TokenContext):
        await access_control.require_assignable_role(platform_admin_context, "user", target_org_id="org-001")


class TestAssignmentCeilingRaisesAccessDenied:
    """A recognized-but-too-high role raises AccessDeniedError (not scope)."""

    async def test_dept_admin_cannot_assign_org_admin(self, access_control: AccessControl):
        # There is no dept_admin membership fixture, so to exercise the
        # DEPT_ADMIN ceiling we seed the role cache directly.
        # Issue #3987: entries are keyed by (user_id, org_id) and carry a
        # monotonic TTL deadline, so seed via the _cache_put helper.
        from src.admin.config import AdminRole

        ctx = TokenContext(
            user_id="dept-admin-xyz",
            org_id="org-001",
            team_id="team-001",
            department_id="dept-001",
            account_type="human",
            is_admin=False,
            expires_at=org_admin_expiry(),
        )
        access_control._cache_put((ctx.user_id, ctx.org_id), (AdminRole.DEPT_ADMIN, "org-001", "dept-001"))
        with pytest.raises(AccessDeniedError):
            await access_control.require_assignable_role(ctx, "org_admin", target_org_id="org-001")


def org_admin_expiry():
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) + timedelta(hours=1)
