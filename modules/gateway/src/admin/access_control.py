"""Access control service for role-based authorization."""

import logging
import time

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.config import (
    CALLER_ROLE_RANK,
    PLATFORM_LEVEL_ROLES,
    ROLE_PERMISSIONS,
    ROLE_RANK,
    AdminRole,
    Permission,
    get_admin_config,
    membership_role_to_admin_role,
)
from src.admin.exceptions import AccessDeniedError, InvalidRoleError, InvalidScopeError
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from src.shared.schemas.auth import TokenContext

logger = logging.getLogger(__name__)

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
        # Issue #3989: agent-registry writes are org-scoped. Omitting this would
        # let a principal with an empty org_id skip the membership-deny below and
        # then short-circuit the target_org_id check (which requires a truthy
        # allowed_org_id), passing the scope check entirely.
        Permission.AGENT_REGISTER,
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
        # Issue #3987: keyed by (user_id, tenant_id) — a role resolved in one
        # tenant must never be served for another. Entries carry a monotonic
        # deadline so a role change can't be masked forever by a stale entry.
        self._role_cache: dict[tuple[str, str | None], tuple[float, tuple[AdminRole, str | None, str | None]]] = {}

    def _cache_get(self, key: tuple[str, str | None]) -> tuple[AdminRole, str | None, str | None] | None:
        """Return a cached role tuple if present and not expired."""
        entry = self._role_cache.get(key)
        if entry is None:
            return None
        deadline, value = entry
        if time.monotonic() >= deadline:
            del self._role_cache[key]
            return None
        return value

    def _cache_put(self, key: tuple[str, str | None], value: tuple[AdminRole, str | None, str | None]) -> None:
        """Cache a resolved role tuple with a TTL deadline."""
        ttl = get_admin_config().rbac_role_cache_ttl_seconds
        self._role_cache[key] = (time.monotonic() + ttl, value)

    async def _resolve_membership_role(self, context: TokenContext) -> tuple[AdminRole, str | None] | None:
        """Resolve the caller's role from their active tenant membership.

        ``tenant_memberships`` (migration 021) is the authority for org-level
        role; the token supplies identity only. Returns ``(role, tenant_id)``
        pinned to the *same* membership row so role and scope can never come
        from different tenants, or ``None`` when no membership can be resolved.

        Note: ``TenantMembership.user_id`` FKs to ``users.id`` (a Postgres UUID)
        while ``TokenContext.user_id`` is the Cognito ``sub`` claim, so this
        resolves through ``users.cognito_sub`` first — the same two-step lookup
        used for tenant resolution in ``admin/connections/routes.py``.

        Issue #3989: some callers reach this with ``user_id`` ALREADY resolved to
        ``users.id`` — ``auth/vault_routes._resolve_user_id_in_context`` rewrites
        the context in place before the vault service runs. Matching only on
        ``cognito_sub`` would resolve those callers to no membership at all, so a
        genuine org admin would fall to the least-privilege default and lose
        access to their own org's shared credentials. Accept either form.
        """
        if self.db is None:
            return None

        pg_user_id = (
            await self.db.execute(select(User.id).where(or_(User.cognito_sub == context.user_id, User.id == context.user_id)).limit(1))
        ).scalar_one_or_none()
        if not pg_user_id:
            return None

        rows = (
            await self.db.execute(
                select(TenantMembership.tenant_id, TenantMembership.role, TenantMembership.is_active).where(
                    TenantMembership.user_id == pg_user_id,
                )
            )
        ).all()
        if not rows:
            return None

        # Prefer the is_active row: after a switch-tenant call the token still
        # carries the previous org_id until refresh, but the DB is the source of
        # truth for the active workspace (matches the effective_org_id logic in
        # admin/connections/routes.py). Falling back to the token's org_id keeps
        # single-tenant callers working when no row is flagged active.
        active = next((r for r in rows if r[2]), None)
        if active is None:
            active = next((r for r in rows if r[0] == context.org_id), None)
        if active is None:
            return None

        tenant_id, stored_role, _ = active
        return membership_role_to_admin_role(stored_role), tenant_id

    async def get_user_role(self, context: TokenContext) -> tuple[AdminRole, str | None, str | None]:
        """
        Get the admin role for a user.

        Authority model (Issue #3987): the token establishes *identity*; the
        database establishes *authority*. Org-level role comes from the caller's
        active ``tenant_memberships`` row. Platform admin remains a token claim
        (``is_admin``) — a tracked follow-up will back it with a server-side
        platform-admin membership lookup.

        Args:
            context: The authenticated user's token context

        Returns:
            Tuple of (role, org_id, dept_id) - org_id/dept_id are the scope limits
        """
        # Platform admin resolves from the token claim alone and needs no DB hit,
        # so this path stays valid when no session was injected.
        if context.is_admin:
            return (AdminRole.PLATFORM_ADMIN, None, None)

        cache_key = (context.user_id, context.org_id)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        resolved: tuple[AdminRole, str | None] | None = None
        try:
            resolved = await self._resolve_membership_role(context)
        except Exception as exc:
            # Fail closed: never fall through to a higher role because a query
            # errored. Logged at WARN with a greppable event name.
            logger.warning(
                "rbac_role_lookup_failed user=%s org=%s error=%s",
                context.user_id,
                context.org_id,
                exc,
            )
            resolved = None

        if resolved is not None:
            role, tenant_id = resolved
            result = (role, tenant_id, None)
        else:
            # No membership row (or no session). Issue #3987 PR 2 made least
            # privilege the default, so this path now demotes to MEMBER rather
            # than granting the legacy ORG_ADMIN. Setting
            # BG_ADMIN_RBAC_LEAST_PRIVILEGE_DEFAULT=false restores the permissive
            # fallback as a runtime rollback lever.
            #
            # Logged unconditionally at WARN: post-flip the interesting event is
            # the demotion actually happening, so an operator must be able to grep
            # for no-row principals either way. `granted` distinguishes the two
            # modes (member = flipped, org_admin = rolled back).
            least_privilege = get_admin_config().rbac_least_privilege_default
            role = AdminRole.MEMBER if least_privilege else AdminRole.ORG_ADMIN
            logger.warning(
                "rbac_role_fallback user=%s org=%s granted=%s reason=no_active_membership",
                context.user_id,
                context.org_id,
                role.value,
            )
            result = (role, context.org_id, None)

        self._cache_put(cache_key, result)
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

    async def require_assignable_role(
        self,
        context: TokenContext,
        requested_role: str,
        target_org_id: str | None = None,
    ) -> None:
        """Enforce a role-assignment ceiling.

        The ``role`` field on user-create/update requests is a free-form string
        that becomes the user's ``custom:role`` claim (and hence their
        privilege). Callers must not be able to grant a role above their own
        privilege level. In particular, an org_admin must NOT be able to assign
        a platform-level role (``platform_admin``/``admin``) and thereby
        escalate a user out of their own organization.

        This guards *which role* may be granted. It is complementary to — not a
        replacement for — the ``check_permission(..., target_org_id=...)`` scope
        check that gates *which org* the caller may write to; call this after
        that check has passed.

        Args:
            context: The authenticated caller's token context.
            requested_role: The role the caller wants to assign.
            target_org_id: Organization the assignment targets (for logging).

        Raises:
            InvalidScopeError: A non-platform caller tried to assign a
                platform-level role.
            AccessDeniedError: The requested role outranks the caller.
            InvalidRoleError: The requested role is not a recognized role.
        """
        role, _, _ = await self.get_user_role(context)

        # Platform admins may assign any role.
        if role == AdminRole.PLATFORM_ADMIN:
            return

        normalized = (requested_role or "").strip().lower()

        # Platform-level roles are never assignable by a non-platform caller,
        # regardless of the caller's own rank.
        if normalized in PLATFORM_LEVEL_ROLES:
            raise InvalidScopeError(
                message="Cannot assign a platform-level role",
                allowed_scope=f"role_rank<={CALLER_ROLE_RANK.get(role, 0)}",
                requested_scope=f"role:{normalized}",
            )

        # Unknown roles are treated as maximally privileged: a platform admin
        # (handled above) may set arbitrary strings, but a non-platform caller
        # may only assign a role we recognize.
        requested_rank = ROLE_RANK.get(normalized)
        if requested_rank is None:
            raise InvalidRoleError(normalized or "<empty>")

        if requested_rank > CALLER_ROLE_RANK.get(role, 0):
            raise AccessDeniedError(
                message=f"Cannot assign role '{normalized}' above your own privilege level",
                user_role=role.value,
            )

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
