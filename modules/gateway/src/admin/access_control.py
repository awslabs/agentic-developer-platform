"""Access control service for role-based authorization."""

from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.config import ROLE_PERMISSIONS, AdminRole, Permission
from src.admin.exceptions import AccessDeniedError, InvalidRoleError, InvalidScopeError
from src.shared.schemas.auth import TokenContext

# Issue #60: Permissions that require the caller to belong to at least one
# organization. Non-admin users with no org_id are rejected up front instead
# of silently returning empty results.
_ORG_SCOPED_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.ORG_READ,
        Permission.ORG_UPDATE,
        Permission.ORG_CREATE,
        Permission.ORG_DELETE,
        Permission.BUDGET_READ,
        Permission.BUDGET_UPDATE,
        Permission.RATELIMIT_READ,
        Permission.RATELIMIT_UPDATE,
        Permission.USAGE_READ,
        Permission.LOGS_READ,
        Permission.LOGS_EXPORT,
        Permission.USER_READ,
        Permission.USER_MANAGE,
        Permission.METRICS_READ,
    }
)


class AccessControl:
    """
    Role-based access control service.

    Supports three role levels:
    - platform_admin: Full access to all organizations and operations
    - org_admin: Access scoped to their organization
    - dept_admin: Access scoped to their department
    """

    def __init__(self, db: AsyncSession | None = None):
        """Initialize access control service.

        Args:
            db: Optional database session for role lookups
        """
        self.db = db
        self._role_cache: dict[str, tuple[AdminRole, str | None, str | None]] = {}

    async def get_user_role(self, context: TokenContext) -> tuple[AdminRole, str | None, str | None]:
        """
        Get the admin role for a user.

        Args:
            context: The authenticated user's token context

        Returns:
            Tuple of (role, org_id, dept_id) - org_id/dept_id are the scope limits

        Note:
            Currently determines role from context.is_admin flag.
            In production, would look up from UserRole table.
        """
        # Check cache first
        if context.user_id in self._role_cache:
            return self._role_cache[context.user_id]

        # Determine role from context
        # In production, this would query the UserRole table
        if context.is_admin:
            # Platform admins have is_admin=True and no org restrictions
            role = AdminRole.PLATFORM_ADMIN
            org_id = None
            dept_id = None
        else:
            # Regular authenticated users are org admins for their own org
            # In production, would check UserRole table for actual role
            role = AdminRole.ORG_ADMIN
            org_id = context.org_id
            dept_id = None

        result = (role, org_id, dept_id)
        self._role_cache[context.user_id] = result
        return result

    def get_role_permissions(self, role: AdminRole) -> set[Permission]:
        """
        Get all permissions for a given role.

        Args:
            role: The admin role

        Returns:
            Set of permissions granted to the role
        """
        if role not in ROLE_PERMISSIONS:
            raise InvalidRoleError(role.value if hasattr(role, "value") else str(role))
        return ROLE_PERMISSIONS[role]

    async def check_permission(
        self,
        context: TokenContext,
        permission: Permission,
        target_org_id: str | None = None,
        target_dept_id: str | None = None,
    ) -> bool:
        """
        Check if a user has a specific permission.

        Args:
            context: The authenticated user's token context
            permission: The permission to check
            target_org_id: Optional target organization ID for scope check
            target_dept_id: Optional target department ID for scope check

        Returns:
            True if the user has the permission

        Raises:
            AccessDeniedError: If the user does not have the permission
        """
        role, allowed_org_id, allowed_dept_id = await self.get_user_role(context)
        permissions = self.get_role_permissions(role)

        # Check if the role has the permission
        if permission not in permissions:
            raise AccessDeniedError(
                message=f"Permission '{permission.value}' is required for this operation",
                required_permission=permission.value,
                user_role=role.value,
            )

        # Check scope if not platform admin
        if role != AdminRole.PLATFORM_ADMIN:
            # Issue #60: Non-admin users with no org membership must be rejected
            # for org-scoped permissions. Without this, they get 200 with empty
            # data instead of 403, which is a silent RBAC bypass.
            if not allowed_org_id and permission in _ORG_SCOPED_PERMISSIONS:
                raise AccessDeniedError(
                    message="No organization membership — cannot access admin resources",
                    required_permission=permission.value,
                    user_role=role.value,
                )

            if target_org_id and allowed_org_id and target_org_id != allowed_org_id:
                raise InvalidScopeError(
                    message="Cannot access resources from another organization",
                    allowed_scope=f"org:{allowed_org_id}",
                    requested_scope=f"org:{target_org_id}",
                )

            if target_dept_id and allowed_dept_id and target_dept_id != allowed_dept_id:
                raise InvalidScopeError(
                    message="Cannot access resources from another department",
                    allowed_scope=f"dept:{allowed_dept_id}",
                    requested_scope=f"dept:{target_dept_id}",
                )

        return True

    async def validate_resource_access(
        self,
        context: TokenContext,
        resource_org_id: str,
        resource_dept_id: str | None = None,
    ) -> bool:
        """
        Validate that a user can access a specific resource.

        Args:
            context: The authenticated user's token context
            resource_org_id: The organization ID that owns the resource
            resource_dept_id: Optional department ID that owns the resource

        Returns:
            True if access is allowed

        Raises:
            AccessDeniedError: If access is denied
        """
        role, allowed_org_id, allowed_dept_id = await self.get_user_role(context)

        # Platform admins can access everything
        if role == AdminRole.PLATFORM_ADMIN:
            return True

        # Org admins can access their own org
        if role == AdminRole.ORG_ADMIN:
            if allowed_org_id != resource_org_id:
                raise AccessDeniedError(
                    message="Cannot access resources from another organization",
                )
            return True

        # Dept admins can only access their own department
        if role == AdminRole.DEPT_ADMIN:
            if allowed_org_id != resource_org_id:
                raise AccessDeniedError(
                    message="Cannot access resources from another organization",
                )
            if resource_dept_id and allowed_dept_id != resource_dept_id:
                raise AccessDeniedError(
                    message="Cannot access resources from another department",
                )
            return True

        raise AccessDeniedError(message="Insufficient permissions")

    async def get_accessible_organizations(self, context: TokenContext) -> list[str] | None:
        """
        Get list of organization IDs the user can access.

        Args:
            context: The authenticated user's token context

        Returns:
            List of org IDs, or None if all orgs are accessible (platform admin)
        """
        role, allowed_org_id, _ = await self.get_user_role(context)

        if role == AdminRole.PLATFORM_ADMIN:
            return None  # Can access all

        if allowed_org_id:
            return [allowed_org_id]

        return []

    async def get_accessible_departments(self, context: TokenContext, org_id: str) -> list[str] | None:
        """
        Get list of department IDs the user can access within an organization.

        Args:
            context: The authenticated user's token context
            org_id: The organization ID

        Returns:
            List of department IDs, or None if all departments are accessible
        """
        role, allowed_org_id, allowed_dept_id = await self.get_user_role(context)

        # Platform admin or org admin can access all departments
        if role in (AdminRole.PLATFORM_ADMIN, AdminRole.ORG_ADMIN):
            return None

        # Dept admin can only access their department
        if role == AdminRole.DEPT_ADMIN and allowed_dept_id:
            return [allowed_dept_id]

        return []

    def require_platform_admin(self, context: TokenContext) -> None:
        """
        Require that the user is a platform admin.

        Args:
            context: The authenticated user's token context

        Raises:
            AccessDeniedError: If the user is not a platform admin
        """
        if not context.is_admin:
            raise AccessDeniedError(
                message="This operation requires platform administrator privileges",
            )

    async def is_platform_admin(self, context: TokenContext) -> bool:
        """
        Check if a user is a platform admin.

        Args:
            context: The authenticated user's token context

        Returns:
            True if the user is a platform admin
        """
        role, _, _ = await self.get_user_role(context)
        return role == AdminRole.PLATFORM_ADMIN

    async def is_org_admin(self, context: TokenContext, org_id: str) -> bool:
        """
        Check if a user is an org admin for a specific organization.

        Args:
            context: The authenticated user's token context
            org_id: The organization ID

        Returns:
            True if the user is an org admin for the organization
        """
        role, allowed_org_id, _ = await self.get_user_role(context)

        if role == AdminRole.PLATFORM_ADMIN:
            return True

        if role == AdminRole.ORG_ADMIN and allowed_org_id == org_id:
            return True

        return False

    async def is_dept_admin(self, context: TokenContext, org_id: str, dept_id: str) -> bool:
        """
        Check if a user is a dept admin for a specific department.

        Args:
            context: The authenticated user's token context
            org_id: The organization ID
            dept_id: The department ID

        Returns:
            True if the user has dept admin access
        """
        role, allowed_org_id, allowed_dept_id = await self.get_user_role(context)

        if role == AdminRole.PLATFORM_ADMIN:
            return True

        if role == AdminRole.ORG_ADMIN and allowed_org_id == org_id:
            return True

        if role == AdminRole.DEPT_ADMIN and allowed_org_id == org_id and allowed_dept_id == dept_id:
            return True

        return False
