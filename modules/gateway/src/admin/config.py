"""Admin module configuration."""

from enum import Enum

from pydantic_settings import BaseSettings


class AdminRole(str, Enum):
    """Admin role levels with different permission scopes."""

    PLATFORM_ADMIN = "platform_admin"  # Full platform access
    ORG_ADMIN = "org_admin"  # Organization-scoped access
    DEPT_ADMIN = "dept_admin"  # Department-scoped access


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
    },
    AdminRole.DEPT_ADMIN: {
        Permission.BUDGET_READ,
        Permission.RATELIMIT_READ,
        Permission.USAGE_READ,
        Permission.LOGS_READ,
        Permission.USER_READ,
    },
}


class AdminConfig(BaseSettings):
    """Admin module configuration settings."""

    # Pagination
    default_page_size: int = 50
    max_page_size: int = 1000

    # Log retention
    log_retention_days: int = 90

    # Rate limiting for admin APIs
    admin_api_rate_limit: int = 100  # requests per minute

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
