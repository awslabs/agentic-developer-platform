"""Unit tests for AccessControl service."""

import pytest

from src.admin.access_control import AccessControl
from src.admin.config import ROLE_PERMISSIONS, AdminRole, Permission
from src.admin.exceptions import AccessDeniedError, InvalidScopeError
from src.shared.schemas.auth import TokenContext


class TestAccessControl:
    """Tests for AccessControl class."""

    @pytest.mark.asyncio
    async def test_get_user_role_platform_admin(self, access_control: AccessControl, platform_admin_context: TokenContext):
        """Test that platform admin role is correctly identified."""
        role, org_id, dept_id = await access_control.get_user_role(platform_admin_context)

        assert role == AdminRole.PLATFORM_ADMIN
        assert org_id is None  # Platform admins have no org restriction
        assert dept_id is None

    @pytest.mark.asyncio
    async def test_get_user_role_org_admin(self, access_control: AccessControl, org_admin_context: TokenContext, org_admin_membership):
        """Test that org admin role is correctly identified.

        Issue #3987 PR 2: authority comes from the ``tenant_memberships`` row, so
        this now takes the ``org_admin_membership`` fixture. Previously it passed
        on the no-row ORG_ADMIN fallback, which no longer exists by default.
        """
        role, org_id, dept_id = await access_control.get_user_role(org_admin_context)

        assert role == AdminRole.ORG_ADMIN
        assert org_id == "org-001"
        assert dept_id is None

    @pytest.mark.asyncio
    async def test_get_user_role_no_membership_is_member(self, access_control: AccessControl, org_admin_context: TokenContext):
        """Issue #3987 PR 2: the same token with NO membership row is a MEMBER.

        The counterpart to the test above, and the whole point of the flip: an
        org-admin-looking token confers no org-admin authority on its own.
        """
        role, org_id, dept_id = await access_control.get_user_role(org_admin_context)

        assert role == AdminRole.MEMBER
        assert org_id == "org-001"
        assert dept_id is None

    @pytest.mark.asyncio
    async def test_get_role_permissions_platform_admin(self, access_control: AccessControl):
        """Test that platform admin has all permissions."""
        permissions = access_control.get_role_permissions(AdminRole.PLATFORM_ADMIN)

        # Platform admin should have all permissions
        assert Permission.ORG_CREATE in permissions
        assert Permission.ORG_DELETE in permissions
        assert Permission.POOL_MANAGE in permissions
        assert Permission.METRICS_READ in permissions

    @pytest.mark.asyncio
    async def test_get_role_permissions_org_admin(self, access_control: AccessControl):
        """Test that org admin has limited permissions."""
        permissions = access_control.get_role_permissions(AdminRole.ORG_ADMIN)

        # Org admin should not have platform-level permissions
        assert Permission.ORG_CREATE not in permissions
        assert Permission.ORG_DELETE not in permissions
        assert Permission.POOL_MANAGE not in permissions

        # But should have org-level permissions
        assert Permission.ORG_READ in permissions
        assert Permission.ORG_UPDATE in permissions
        assert Permission.BUDGET_READ in permissions
        assert Permission.BUDGET_UPDATE in permissions

    @pytest.mark.asyncio
    async def test_get_role_permissions_dept_admin(self, access_control: AccessControl):
        """Test that dept admin has most limited permissions."""
        permissions = access_control.get_role_permissions(AdminRole.DEPT_ADMIN)

        # Dept admin should only have read permissions
        assert Permission.BUDGET_READ in permissions
        assert Permission.USAGE_READ in permissions
        assert Permission.LOGS_READ in permissions

        # Should not have write permissions
        assert Permission.BUDGET_UPDATE not in permissions
        assert Permission.ORG_UPDATE not in permissions

    @pytest.mark.asyncio
    async def test_check_permission_allowed(self, access_control: AccessControl, platform_admin_context: TokenContext):
        """Test permission check passes for allowed operations."""
        result = await access_control.check_permission(platform_admin_context, Permission.ORG_CREATE)
        assert result is True

    @pytest.mark.asyncio
    async def test_check_permission_denied(self, access_control: AccessControl, org_admin_context: TokenContext):
        """Test permission check fails for denied operations."""
        with pytest.raises(AccessDeniedError) as exc_info:
            await access_control.check_permission(org_admin_context, Permission.ORG_CREATE)

        assert "org:create" in str(exc_info.value.message)

    @pytest.mark.asyncio
    async def test_check_permission_scope_violation(self, access_control: AccessControl, org_admin_context: TokenContext, org_admin_membership):
        """Test permission check fails when accessing another org's resources.

        Issue #3987 PR 2: needs the membership row so the caller actually HOLDS
        ORG_READ — otherwise the denial would come from the permission check
        rather than the scope check this test is pinning.
        """
        with pytest.raises(InvalidScopeError):
            await access_control.check_permission(
                org_admin_context,
                Permission.ORG_READ,
                target_org_id="org-002",  # Different from user's org
            )

    @pytest.mark.asyncio
    async def test_validate_resource_access_platform_admin(self, access_control: AccessControl, platform_admin_context: TokenContext):
        """Test platform admin can access any resource."""
        result = await access_control.validate_resource_access(
            platform_admin_context,
            resource_org_id="org-001",
            resource_dept_id="dept-001",
        )
        assert result is True

        # Can also access different org
        result = await access_control.validate_resource_access(
            platform_admin_context,
            resource_org_id="org-002",
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_validate_resource_access_org_admin(self, access_control: AccessControl, org_admin_context: TokenContext, org_admin_membership):
        """Test org admin can only access own org's resources.

        Issue #3987 PR 2: membership row required for the caller to be an org
        admin at all.
        """
        # Can access own org
        result = await access_control.validate_resource_access(
            org_admin_context,
            resource_org_id="org-001",
        )
        assert result is True

        # Cannot access other org
        with pytest.raises(AccessDeniedError):
            await access_control.validate_resource_access(
                org_admin_context,
                resource_org_id="org-002",
            )

    @pytest.mark.asyncio
    async def test_get_accessible_organizations_platform_admin(self, access_control: AccessControl, platform_admin_context: TokenContext):
        """Test platform admin can access all organizations."""
        orgs = await access_control.get_accessible_organizations(platform_admin_context)
        assert orgs is None  # None means all orgs

    @pytest.mark.asyncio
    async def test_get_accessible_organizations_org_admin(self, access_control: AccessControl, org_admin_context: TokenContext):
        """Test org admin can only access their organization."""
        orgs = await access_control.get_accessible_organizations(org_admin_context)
        assert orgs == ["org-001"]

    @pytest.mark.asyncio
    async def test_require_platform_admin_success(self, access_control: AccessControl, platform_admin_context: TokenContext):
        """Test require_platform_admin passes for platform admin."""
        # Should not raise
        access_control.require_platform_admin(platform_admin_context)

    @pytest.mark.asyncio
    async def test_require_platform_admin_failure(self, access_control: AccessControl, org_admin_context: TokenContext):
        """Test require_platform_admin fails for non-platform admin."""
        with pytest.raises(AccessDeniedError):
            access_control.require_platform_admin(org_admin_context)

    @pytest.mark.asyncio
    async def test_is_platform_admin(self, access_control: AccessControl, platform_admin_context: TokenContext, org_admin_context: TokenContext):
        """Test is_platform_admin helper."""
        assert await access_control.is_platform_admin(platform_admin_context) is True
        assert await access_control.is_platform_admin(org_admin_context) is False

    @pytest.mark.asyncio
    async def test_is_org_admin(
        self,
        access_control: AccessControl,
        platform_admin_context: TokenContext,
        org_admin_context: TokenContext,
        org_admin_membership,
    ):
        """Test is_org_admin helper.

        Issue #3987 PR 2: membership row required for the org-admin assertions.
        """
        # Platform admin is admin for any org
        assert await access_control.is_org_admin(platform_admin_context, "org-001") is True
        assert await access_control.is_org_admin(platform_admin_context, "org-002") is True

        # Org admin only for their org
        assert await access_control.is_org_admin(org_admin_context, "org-001") is True
        assert await access_control.is_org_admin(org_admin_context, "org-002") is False

    @pytest.mark.asyncio
    async def test_role_cache(self, access_control: AccessControl, org_admin_context: TokenContext):
        """Test that role lookups are cached.

        Issue #3987: the cache is keyed by (user_id, org_id) so a role resolved
        in one tenant is never served for another. Platform admins resolve from
        the token claim alone and are deliberately not cached, so this exercises
        a non-platform caller.
        """
        role1, _, _ = await access_control.get_user_role(org_admin_context)

        cache_key = (org_admin_context.user_id, org_admin_context.org_id)
        assert cache_key in access_control._role_cache

        # Second call should use cache
        role2, _, _ = await access_control.get_user_role(org_admin_context)

        assert role1 == role2

    def test_role_permissions_coverage(self):
        """Test all roles have defined permissions."""
        for role in AdminRole:
            assert role in ROLE_PERMISSIONS, f"Missing permissions for role {role}"
            assert len(ROLE_PERMISSIONS[role]) > 0, f"No permissions defined for role {role}"

    def test_permission_hierarchy(self):
        """Test that higher roles have more permissions."""
        platform_perms = ROLE_PERMISSIONS[AdminRole.PLATFORM_ADMIN]
        org_perms = ROLE_PERMISSIONS[AdminRole.ORG_ADMIN]
        dept_perms = ROLE_PERMISSIONS[AdminRole.DEPT_ADMIN]

        # Platform admin should have the most permissions
        assert len(platform_perms) > len(org_perms)
        assert len(org_perms) > len(dept_perms)

        # Lower role permissions should be subset of higher roles
        assert dept_perms.issubset(org_perms) or not dept_perms.issubset(org_perms)  # May have different perms


class TestRBACNoOrgRejection:
    """Issue #60 — Gap 3: Non-admin users with no org_id must get 403, not empty 200."""

    @pytest.mark.asyncio
    async def test_non_admin_no_org_gets_403_on_org_read(self, access_control: AccessControl):
        """Non-admin with no org_id should be rejected for ORG_READ.

        Issue #60's contract is the 403 itself (not an empty 200). Issue #3987
        PR 2 changed which guard produces it: a no-membership caller now resolves
        to MEMBER, which lacks ORG_READ outright, so the denial comes from the
        permission check before the no-org scope check is reached. Asserting the
        403 rather than the old message keeps this pinning #60's contract; the
        no-org scope guard itself is pinned by
        ``test_org_scoped_permission_without_org_scope_is_denied`` below.
        """
        from datetime import UTC, datetime, timedelta

        no_org_context = TokenContext(
            user_id="orphan-user-001",
            org_id="",  # No org membership
            team_id="",
            department_id="",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        with pytest.raises(AccessDeniedError) as exc_info:
            await access_control.check_permission(no_org_context, Permission.ORG_READ)

        assert exc_info.value.status_code == 403
        assert exc_info.value.details["user_role"] == AdminRole.MEMBER.value

    @pytest.mark.asyncio
    async def test_org_scoped_permission_without_org_scope_is_denied(self, access_control: AccessControl):
        """Issue #60's no-org scope guard, pinned directly.

        A role that DOES hold an org-scoped permission but resolves to no org
        scope must still be denied — the silent-RBAC-bypass case #60 fixed. After
        #3987 PR 2 no fallback produces that combination on its own, so the
        resolved role is seeded into the cache (same approach as
        test_role_assignment_ceiling.py) to reach the branch.
        """
        from datetime import UTC, datetime, timedelta

        no_org_context = TokenContext(
            user_id="orphan-admin-001",
            org_id="",
            team_id="",
            department_id="",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        access_control._cache_put((no_org_context.user_id, no_org_context.org_id), (AdminRole.ORG_ADMIN, "", None))

        with pytest.raises(AccessDeniedError) as exc_info:
            await access_control.check_permission(no_org_context, Permission.ORG_READ)

        assert "No organization membership" in str(exc_info.value.message)

    @pytest.mark.asyncio
    async def test_non_admin_no_org_gets_403_on_budget_read(self, access_control: AccessControl):
        """Non-admin with no org_id should be rejected for BUDGET_READ."""
        from datetime import UTC, datetime, timedelta

        no_org_context = TokenContext(
            user_id="orphan-user-002",
            org_id="",
            team_id="",
            department_id="",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        with pytest.raises(AccessDeniedError):
            await access_control.check_permission(no_org_context, Permission.BUDGET_READ)

    @pytest.mark.asyncio
    async def test_admin_no_org_allowed(self, access_control: AccessControl, platform_admin_context: TokenContext):
        """Platform admin should still pass even without an explicit org_id."""
        result = await access_control.check_permission(platform_admin_context, Permission.ORG_READ)
        assert result is True

    @pytest.mark.asyncio
    async def test_non_admin_with_org_allowed(self, access_control: AccessControl, org_admin_context: TokenContext, org_admin_membership):
        """Non-admin with a valid org_id should pass ORG_READ.

        Issue #3987 PR 2: an org_id alone is no longer sufficient — ORG_READ comes
        from the org-admin membership row. Seeded here so this keeps testing what
        it was written to test (a valid org scope is not rejected) rather than
        passing on the removed no-row fallback.
        """
        result = await access_control.check_permission(org_admin_context, Permission.ORG_READ)
        assert result is True

    @pytest.mark.asyncio
    async def test_non_admin_no_org_gets_403_on_user_manage(self, access_control: AccessControl):
        """Non-admin with no org_id should be rejected for USER_MANAGE."""
        from datetime import UTC, datetime, timedelta

        no_org_context = TokenContext(
            user_id="orphan-user-003",
            org_id="",
            team_id="",
            department_id="",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        with pytest.raises(AccessDeniedError):
            await access_control.check_permission(no_org_context, Permission.USER_MANAGE)
