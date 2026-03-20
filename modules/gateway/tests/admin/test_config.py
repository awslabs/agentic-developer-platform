"""Unit tests for admin module configuration."""

import os
from unittest.mock import patch

from src.admin.config import (
    ROLE_PERMISSIONS,
    AdminConfig,
    AdminRole,
    Permission,
    get_admin_config,
    set_admin_config,
)


class TestAdminRole:
    """Tests for AdminRole enum."""

    def test_admin_role_values(self):
        """Test AdminRole enum has correct values."""
        assert AdminRole.PLATFORM_ADMIN.value == "platform_admin"
        assert AdminRole.ORG_ADMIN.value == "org_admin"
        assert AdminRole.DEPT_ADMIN.value == "dept_admin"

    def test_admin_role_count(self):
        """Test AdminRole enum has exactly 3 roles."""
        assert len(AdminRole) == 3

    def test_admin_role_is_string_enum(self):
        """Test AdminRole is a string enum."""
        for role in AdminRole:
            assert isinstance(role, str)
            assert isinstance(role.value, str)

    def test_admin_role_uniqueness(self):
        """Test all AdminRole values are unique."""
        values = [role.value for role in AdminRole]
        assert len(values) == len(set(values))


class TestPermission:
    """Tests for Permission enum."""

    def test_permission_count(self):
        """Test Permission enum has expected number of permissions."""
        # Should have 16 permissions based on the source file
        assert len(Permission) == 16

    def test_org_permissions_exist(self):
        """Test organization management permissions exist."""
        assert Permission.ORG_CREATE.value == "org:create"
        assert Permission.ORG_READ.value == "org:read"
        assert Permission.ORG_UPDATE.value == "org:update"
        assert Permission.ORG_DELETE.value == "org:delete"

    def test_budget_permissions_exist(self):
        """Test budget management permissions exist."""
        assert Permission.BUDGET_READ.value == "budget:read"
        assert Permission.BUDGET_UPDATE.value == "budget:update"

    def test_ratelimit_permissions_exist(self):
        """Test rate limit management permissions exist."""
        assert Permission.RATELIMIT_READ.value == "ratelimit:read"
        assert Permission.RATELIMIT_UPDATE.value == "ratelimit:update"

    def test_pool_permissions_exist(self):
        """Test pool management permissions exist."""
        assert Permission.POOL_READ.value == "pool:read"
        assert Permission.POOL_MANAGE.value == "pool:manage"

    def test_usage_and_logs_permissions_exist(self):
        """Test usage and logs permissions exist."""
        assert Permission.USAGE_READ.value == "usage:read"
        assert Permission.LOGS_READ.value == "logs:read"
        assert Permission.LOGS_EXPORT.value == "logs:export"

    def test_user_permissions_exist(self):
        """Test user management permissions exist."""
        assert Permission.USER_READ.value == "user:read"
        assert Permission.USER_MANAGE.value == "user:manage"

    def test_metrics_permission_exists(self):
        """Test metrics permission exists."""
        assert Permission.METRICS_READ.value == "metrics:read"

    def test_permission_is_string_enum(self):
        """Test Permission is a string enum."""
        for perm in Permission:
            assert isinstance(perm, str)
            assert isinstance(perm.value, str)

    def test_permission_uniqueness(self):
        """Test all Permission values are unique."""
        values = [perm.value for perm in Permission]
        assert len(values) == len(set(values))

    def test_permission_format(self):
        """Test all permissions follow the resource:action format."""
        for perm in Permission:
            assert ":" in perm.value, f"Permission {perm.value} does not follow resource:action format"
            parts = perm.value.split(":")
            assert len(parts) == 2, f"Permission {perm.value} has invalid format"
            assert parts[0], f"Permission {perm.value} has empty resource"
            assert parts[1], f"Permission {perm.value} has empty action"


class TestRolePermissions:
    """Tests for role-to-permission mapping."""

    def test_all_roles_have_permissions(self):
        """Test all AdminRole values have defined permissions."""
        for role in AdminRole:
            assert role in ROLE_PERMISSIONS, f"Missing permissions for role {role}"

    def test_platform_admin_has_all_permissions(self):
        """Test platform admin has all defined permissions."""
        platform_perms = ROLE_PERMISSIONS[AdminRole.PLATFORM_ADMIN]
        assert len(platform_perms) == len(Permission)
        for perm in Permission:
            assert perm in platform_perms, f"Platform admin missing permission {perm}"

    def test_platform_admin_has_exclusive_permissions(self):
        """Test platform admin has permissions not given to org admin."""
        platform_perms = ROLE_PERMISSIONS[AdminRole.PLATFORM_ADMIN]
        org_perms = ROLE_PERMISSIONS[AdminRole.ORG_ADMIN]

        # These should be platform-admin only
        assert Permission.ORG_CREATE in platform_perms
        assert Permission.ORG_CREATE not in org_perms
        assert Permission.ORG_DELETE in platform_perms
        assert Permission.ORG_DELETE not in org_perms
        assert Permission.POOL_MANAGE in platform_perms
        assert Permission.POOL_MANAGE not in org_perms

    def test_org_admin_permissions(self):
        """Test org admin has expected permissions."""
        org_perms = ROLE_PERMISSIONS[AdminRole.ORG_ADMIN]

        # Should have these
        assert Permission.ORG_READ in org_perms
        assert Permission.ORG_UPDATE in org_perms
        assert Permission.BUDGET_READ in org_perms
        assert Permission.BUDGET_UPDATE in org_perms
        assert Permission.RATELIMIT_READ in org_perms
        assert Permission.RATELIMIT_UPDATE in org_perms
        assert Permission.USAGE_READ in org_perms
        assert Permission.LOGS_READ in org_perms
        assert Permission.LOGS_EXPORT in org_perms
        assert Permission.USER_READ in org_perms
        assert Permission.USER_MANAGE in org_perms

    def test_dept_admin_permissions(self):
        """Test dept admin has expected (limited) permissions."""
        dept_perms = ROLE_PERMISSIONS[AdminRole.DEPT_ADMIN]

        # Should have these read-only permissions
        assert Permission.BUDGET_READ in dept_perms
        assert Permission.RATELIMIT_READ in dept_perms
        assert Permission.USAGE_READ in dept_perms
        assert Permission.LOGS_READ in dept_perms
        assert Permission.USER_READ in dept_perms

        # Should NOT have these write permissions
        assert Permission.BUDGET_UPDATE not in dept_perms
        assert Permission.RATELIMIT_UPDATE not in dept_perms
        assert Permission.ORG_UPDATE not in dept_perms
        assert Permission.USER_MANAGE not in dept_perms

    def test_permission_hierarchy_platform_is_superset(self):
        """Test platform admin permissions are superset of org admin."""
        platform_perms = ROLE_PERMISSIONS[AdminRole.PLATFORM_ADMIN]
        org_perms = ROLE_PERMISSIONS[AdminRole.ORG_ADMIN]

        assert org_perms.issubset(platform_perms)

    def test_role_permission_counts(self):
        """Test role permission counts follow hierarchy."""
        platform_count = len(ROLE_PERMISSIONS[AdminRole.PLATFORM_ADMIN])
        org_count = len(ROLE_PERMISSIONS[AdminRole.ORG_ADMIN])
        dept_count = len(ROLE_PERMISSIONS[AdminRole.DEPT_ADMIN])

        assert platform_count > org_count > dept_count

    def test_role_permissions_are_sets(self):
        """Test role permissions are stored as sets."""
        for role, perms in ROLE_PERMISSIONS.items():
            assert isinstance(perms, set), f"Permissions for {role} should be a set"


class TestAdminConfig:
    """Tests for AdminConfig class."""

    def test_admin_config_defaults(self):
        """Test AdminConfig has correct default values."""
        config = AdminConfig()

        assert config.default_page_size == 50
        assert config.max_page_size == 1000
        assert config.log_retention_days == 90
        assert config.admin_api_rate_limit == 100

    def test_admin_config_custom_values(self):
        """Test AdminConfig accepts custom values."""
        config = AdminConfig(
            default_page_size=25,
            max_page_size=500,
            log_retention_days=30,
            admin_api_rate_limit=50,
        )

        assert config.default_page_size == 25
        assert config.max_page_size == 500
        assert config.log_retention_days == 30
        assert config.admin_api_rate_limit == 50

    def test_admin_config_env_prefix(self):
        """Test AdminConfig uses correct env prefix."""
        assert AdminConfig.model_config.get("env_prefix") == "BG_ADMIN_"

    def test_admin_config_from_env(self):
        """Test AdminConfig reads from environment variables."""
        with patch.dict(
            os.environ,
            {
                "BG_ADMIN_DEFAULT_PAGE_SIZE": "100",
                "BG_ADMIN_MAX_PAGE_SIZE": "2000",
                "BG_ADMIN_LOG_RETENTION_DAYS": "180",
                "BG_ADMIN_ADMIN_API_RATE_LIMIT": "200",
            },
        ):
            config = AdminConfig()
            assert config.default_page_size == 100
            assert config.max_page_size == 2000
            assert config.log_retention_days == 180
            assert config.admin_api_rate_limit == 200


class TestGetAdminConfig:
    """Tests for get_admin_config function."""

    def setup_method(self):
        """Reset the config singleton before each test."""
        set_admin_config(None)

    def teardown_method(self):
        """Reset the config singleton after each test."""
        set_admin_config(None)

    def test_get_admin_config_returns_instance(self):
        """Test get_admin_config returns an AdminConfig instance."""
        config = get_admin_config()
        assert isinstance(config, AdminConfig)

    def test_get_admin_config_singleton(self):
        """Test get_admin_config returns the same instance."""
        config1 = get_admin_config()
        config2 = get_admin_config()
        assert config1 is config2

    def test_set_admin_config(self):
        """Test set_admin_config sets the singleton."""
        custom_config = AdminConfig(default_page_size=10)
        set_admin_config(custom_config)

        result = get_admin_config()
        assert result is custom_config
        assert result.default_page_size == 10

    def test_set_admin_config_to_none(self):
        """Test set_admin_config can reset to None."""
        get_admin_config()  # Initialize
        set_admin_config(None)

        # Should create a new instance
        new_config = get_admin_config()
        assert new_config is not None
        assert isinstance(new_config, AdminConfig)
