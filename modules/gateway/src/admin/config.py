"""Admin module configuration."""

from enum import Enum

from pydantic_settings import BaseSettings


class AdminRole(str, Enum):
    """Admin role levels with different permission scopes."""

    PLATFORM_ADMIN = "platform_admin"  # Full platform access
    ORG_ADMIN = "org_admin"  # Organization-scoped access
    DEPT_ADMIN = "dept_admin"  # Department-scoped access
    MEMBER = "member"  # Issue #3987: least-privilege default (no admin authority)


class Permission(str, Enum):
    """Granular permissions for admin operations."""

    # Organization management
    ORG_CREATE = "org:create"
    ORG_READ = "org:read"
    ORG_UPDATE = "org:update"
    ORG_DELETE = "org:delete"

    # Budget management
    BUDGET_READ = "budget:read"
    BUDGET_UPDATE = "budget:update"

    # Rate limit management
    RATELIMIT_READ = "ratelimit:read"
    RATELIMIT_UPDATE = "ratelimit:update"

    # Pool management
    POOL_READ = "pool:read"
    POOL_MANAGE = "pool:manage"

    # Usage and logs
    USAGE_READ = "usage:read"
    LOGS_READ = "logs:read"
    LOGS_EXPORT = "logs:export"

    # User management
    USER_READ = "user:read"
    USER_MANAGE = "user:manage"

    # Metrics
    METRICS_READ = "metrics:read"

    # Issue #3989: agent-registry writes. The registry is the IAM-authentication
    # database — a row resolves a role_arn to an authenticated `service`
    # identity in an org (src/auth/agent_registry.py). Writes therefore need a
    # dedicated permission rather than reusing ORG_UPDATE, which is held by
    # every role that can edit any org attribute.
    AGENT_REGISTER = "agent:register"


# Role to permissions mapping
ROLE_PERMISSIONS: dict[AdminRole, set[Permission]] = {
    AdminRole.PLATFORM_ADMIN: {
        Permission.ORG_CREATE,
        Permission.ORG_READ,
        Permission.ORG_UPDATE,
        Permission.ORG_DELETE,
        Permission.BUDGET_READ,
        Permission.BUDGET_UPDATE,
        Permission.RATELIMIT_READ,
        Permission.RATELIMIT_UPDATE,
        Permission.POOL_READ,
        Permission.POOL_MANAGE,
        Permission.USAGE_READ,
        Permission.LOGS_READ,
        Permission.LOGS_EXPORT,
        Permission.USER_READ,
        Permission.USER_MANAGE,
        Permission.METRICS_READ,
        Permission.AGENT_REGISTER,
    },
    AdminRole.ORG_ADMIN: {
        Permission.ORG_READ,
        Permission.ORG_UPDATE,
        Permission.BUDGET_READ,
        Permission.BUDGET_UPDATE,
        Permission.RATELIMIT_READ,
        Permission.RATELIMIT_UPDATE,
        Permission.USAGE_READ,
        Permission.LOGS_READ,
        Permission.LOGS_EXPORT,
        Permission.USER_READ,
        Permission.USER_MANAGE,
        # Issue #3989: an org admin may register agents into their OWN org; the
        # target_org_id scope check in check_permission() enforces the boundary.
        Permission.AGENT_REGISTER,
    },
    AdminRole.DEPT_ADMIN: {
        Permission.BUDGET_READ,
        Permission.RATELIMIT_READ,
        Permission.USAGE_READ,
        Permission.LOGS_READ,
        Permission.USER_READ,
    },
    # Issue #3987: least-privilege role for an authenticated principal with no
    # admin-level tenant membership. Deliberately holds a single own-scope read
    # permission rather than the empty set: get_role_permissions() raises
    # InvalidRoleError for an unmapped role, which surfaces as a 500 instead of
    # the 403 an unprivileged caller must get.
    AdminRole.MEMBER: {
        Permission.USAGE_READ,
    },
}


# Role-assignment ceiling.
#
# The ``role`` written onto a user record (custom:role in Cognito) is a
# free-form string on the create/update request. The permission + scope checks
# gate *which org* a caller may write to, but NOT *which role* they may grant.
# Without a ceiling an org_admin can create a user with role="platform_admin"
# and escalate privilege out of their own organization. These tables let
# AccessControl.require_assignable_role() enforce that a caller may only grant a
# role at or below their own privilege level.

# Roles that confer PLATFORM-wide admin (is_admin=True in the token). They may
# ONLY be assigned by a platform admin — never by an org or dept admin.
PLATFORM_LEVEL_ROLES: frozenset[str] = frozenset({"platform_admin", "admin"})

# Rank of each assignable user role. A caller may assign a role only if its rank
# is <= the caller's own rank. Roles not listed here are treated as maximally
# privileged (only a platform admin can assign them) and additionally rejected
# as invalid for non-platform callers.
ROLE_RANK: dict[str, int] = {
    "user": 0,
    "member": 0,
    "viewer": 0,
    "dept_admin": 1,
    "org_admin": 2,
    "admin": 3,
    "platform_admin": 3,
}

# The privilege rank held by each admin role, used as the assignment ceiling.
CALLER_ROLE_RANK: dict[AdminRole, int] = {
    AdminRole.PLATFORM_ADMIN: 3,
    AdminRole.ORG_ADMIN: 2,
    AdminRole.DEPT_ADMIN: 1,
    # Issue #3987: a member may assign no role at all.
    AdminRole.MEMBER: 0,
}


# Issue #3987: mapping from a stored tenant_memberships.role string to the
# AdminRole vocabulary used by the permission tables.
#
# Four role vocabularies are live in this module and they disagree: the AdminRole
# enum, ROLE_RANK above, the strings actually written into
# tenant_memberships.role by the onboarding paths, and whatever arbitrary
# users.role value migration 021's backfill copied in. This is the single
# explicit, fail-closed reconciliation point.
#
# Note that platform-level strings map to ORG_ADMIN, *not* PLATFORM_ADMIN: a
# tenant membership row is scoped to one tenant by construction and must never
# be able to confer unscoped platform authority. Platform admin comes from the
# token's is_admin claim only (see #3981, which removed the org_admin -> platform
# escalation bridge).
_MEMBERSHIP_ROLE_TO_ADMIN_ROLE: dict[str, AdminRole] = {
    "platform_admin": AdminRole.ORG_ADMIN,
    "admin": AdminRole.ORG_ADMIN,
    "org_admin": AdminRole.ORG_ADMIN,
    "dept_admin": AdminRole.DEPT_ADMIN,
    "member": AdminRole.MEMBER,
    "user": AdminRole.MEMBER,
    "viewer": AdminRole.MEMBER,
}


def membership_role_to_admin_role(stored_role: str | None) -> AdminRole:
    """Map a stored membership role string to an AdminRole, failing closed.

    Args:
        stored_role: Value of ``tenant_memberships.role``, possibly None.

    Returns:
        The corresponding AdminRole. An unrecognized, empty, or NULL value maps
        to ``AdminRole.MEMBER`` (least privilege) rather than raising — an
        unmapped role reaching ``get_role_permissions`` would be a 500, and
        defaulting to anything higher is the privilege escalation this issue
        exists to remove.
    """
    normalized = (stored_role or "").strip().lower()
    return _MEMBERSHIP_ROLE_TO_ADMIN_ROLE.get(normalized, AdminRole.MEMBER)


class AdminConfig(BaseSettings):
    """Admin module configuration settings."""

    # Pagination
    default_page_size: int = 50
    max_page_size: int = 1000

    # Log retention
    log_retention_days: int = 90

    # Rate limiting for admin APIs
    admin_api_rate_limit: int = 100  # requests per minute

    # Issue #3987 (PR 2 of 2): least privilege is now the default. A principal
    # with no is_active, admin-level tenant_memberships row resolves to
    # AdminRole.MEMBER, not the legacy ORG_ADMIN — that permissive fallback was
    # the privilege-escalation class #3987 exists to close. Principals who *do*
    # have a row are unaffected; their role comes from the row.
    #
    # Rollback lever: set BG_ADMIN_RBAC_LEAST_PRIVILEGE_DEFAULT=false to restore
    # the legacy ORG_ADMIN fallback at runtime, without a revert.
    #
    # Before enabling in a new environment, run
    # scripts/audit_org_admin_memberships.py and resolve-or-accept every genuine
    # org admin that lacks an is_active role='org_admin' membership row — they
    # will be demoted to MEMBER by this default.
    rbac_least_privilege_default: bool = True

    # Seconds a resolved role stays cached on an AccessControl instance.
    rbac_role_cache_ttl_seconds: float = 30.0

    model_config = {"env_prefix": "BG_ADMIN_"}


_admin_config: AdminConfig | None = None


def get_admin_config() -> AdminConfig:
    """Get admin configuration singleton."""
    global _admin_config
    if _admin_config is None:
        _admin_config = AdminConfig()
    return _admin_config


def set_admin_config(config: AdminConfig) -> None:
    """Set admin configuration (for testing)."""
    global _admin_config
    _admin_config = config
