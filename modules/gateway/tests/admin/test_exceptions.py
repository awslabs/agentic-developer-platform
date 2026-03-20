"""Unit tests for admin module exceptions."""

import pytest

from src.admin.exceptions import (
    AccessDeniedError,
    InvalidRoleError,
    InvalidScopeError,
    PoolConfigurationError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from src.shared.exceptions import BedrockGatewayError


class TestAccessDeniedError:
    """Tests for AccessDeniedError exception."""

    def test_instantiation_default_message(self):
        """Test AccessDeniedError can be instantiated with default message."""
        error = AccessDeniedError()

        assert error.error == "access_denied"
        assert error.message == "Access denied"
        assert error.status_code == 403
        assert error.details is None

    def test_instantiation_custom_message(self):
        """Test AccessDeniedError can be instantiated with custom message."""
        error = AccessDeniedError(message="Custom access denied message")

        assert error.message == "Custom access denied message"
        assert error.status_code == 403

    def test_instantiation_with_permission(self):
        """Test AccessDeniedError with required permission."""
        error = AccessDeniedError(
            message="Cannot create org",
            required_permission="org:create",
        )

        assert error.details is not None
        assert error.details["required_permission"] == "org:create"
        assert "user_role" not in error.details

    def test_instantiation_with_role(self):
        """Test AccessDeniedError with user role."""
        error = AccessDeniedError(
            message="Cannot delete org",
            user_role="org_admin",
        )

        assert error.details is not None
        assert error.details["user_role"] == "org_admin"
        assert "required_permission" not in error.details

    def test_instantiation_with_all_details(self):
        """Test AccessDeniedError with all details."""
        error = AccessDeniedError(
            message="Full access denied",
            required_permission="pool:manage",
            user_role="dept_admin",
        )

        assert error.details is not None
        assert error.details["required_permission"] == "pool:manage"
        assert error.details["user_role"] == "dept_admin"

    def test_inheritance(self):
        """Test AccessDeniedError inherits from BedrockGatewayError."""
        error = AccessDeniedError()
        assert isinstance(error, BedrockGatewayError)
        assert isinstance(error, Exception)

    def test_string_representation(self):
        """Test AccessDeniedError string representation."""
        error = AccessDeniedError(message="Test message")
        assert str(error) == "Test message"


class TestInvalidRoleError:
    """Tests for InvalidRoleError exception."""

    def test_instantiation(self):
        """Test InvalidRoleError can be instantiated."""
        error = InvalidRoleError(role="invalid_role")

        assert error.error == "invalid_role"
        assert error.message == "Invalid admin role: invalid_role"
        assert error.status_code == 400

    def test_details_contain_role(self):
        """Test InvalidRoleError details contain the invalid role."""
        error = InvalidRoleError(role="super_admin")

        assert error.details is not None
        assert error.details["role"] == "super_admin"

    def test_details_contain_valid_roles(self):
        """Test InvalidRoleError details contain valid roles."""
        error = InvalidRoleError(role="test")

        assert error.details is not None
        assert "valid_roles" in error.details
        assert "platform_admin" in error.details["valid_roles"]
        assert "org_admin" in error.details["valid_roles"]
        assert "dept_admin" in error.details["valid_roles"]

    def test_inheritance(self):
        """Test InvalidRoleError inherits from BedrockGatewayError."""
        error = InvalidRoleError(role="test")
        assert isinstance(error, BedrockGatewayError)

    def test_string_representation(self):
        """Test InvalidRoleError string representation."""
        error = InvalidRoleError(role="bad_role")
        assert "bad_role" in str(error)


class TestResourceNotFoundError:
    """Tests for ResourceNotFoundError exception."""

    def test_instantiation(self):
        """Test ResourceNotFoundError can be instantiated."""
        error = ResourceNotFoundError(
            resource_type="Organization",
            resource_id="org-001",
        )

        assert error.error == "resource_not_found"
        assert error.message == "Organization with id 'org-001' not found"
        assert error.status_code == 404

    def test_details(self):
        """Test ResourceNotFoundError details."""
        error = ResourceNotFoundError(
            resource_type="PoolAccount",
            resource_id="pool-xyz",
        )

        assert error.details is not None
        assert error.details["resource_type"] == "PoolAccount"
        assert error.details["resource_id"] == "pool-xyz"

    def test_various_resource_types(self):
        """Test ResourceNotFoundError with various resource types."""
        resource_types = ["Organization", "User", "Budget", "RateLimit", "Log"]
        for rtype in resource_types:
            error = ResourceNotFoundError(resource_type=rtype, resource_id="test-id")
            assert rtype in error.message

    def test_inheritance(self):
        """Test ResourceNotFoundError inherits from BedrockGatewayError."""
        error = ResourceNotFoundError(resource_type="Test", resource_id="id")
        assert isinstance(error, BedrockGatewayError)


class TestResourceConflictError:
    """Tests for ResourceConflictError exception."""

    def test_instantiation(self):
        """Test ResourceConflictError can be instantiated."""
        error = ResourceConflictError(
            resource_type="Organization",
            field="name",
            value="Existing Org",
        )

        assert error.error == "resource_conflict"
        assert error.message == "Organization with name 'Existing Org' already exists"
        assert error.status_code == 409

    def test_details(self):
        """Test ResourceConflictError details."""
        error = ResourceConflictError(
            resource_type="User",
            field="email",
            value="test@example.com",
        )

        assert error.details is not None
        assert error.details["resource_type"] == "User"
        assert error.details["field"] == "email"
        assert error.details["value"] == "test@example.com"

    def test_various_conflict_scenarios(self):
        """Test ResourceConflictError with various scenarios."""
        scenarios = [
            ("Organization", "name", "Acme Corp"),
            ("PoolAccount", "role_arn", "arn:aws:iam::123:role/Role"),
            ("User", "username", "john_doe"),
        ]
        for resource_type, field, value in scenarios:
            error = ResourceConflictError(
                resource_type=resource_type,
                field=field,
                value=value,
            )
            assert resource_type in error.message
            assert field in error.message
            assert value in error.message

    def test_inheritance(self):
        """Test ResourceConflictError inherits from BedrockGatewayError."""
        error = ResourceConflictError(resource_type="Test", field="id", value="1")
        assert isinstance(error, BedrockGatewayError)


class TestInvalidScopeError:
    """Tests for InvalidScopeError exception."""

    def test_instantiation_default_message(self):
        """Test InvalidScopeError can be instantiated with default message."""
        error = InvalidScopeError()

        assert error.error == "invalid_scope"
        assert error.message == "Operation outside allowed scope"
        assert error.status_code == 403
        assert error.details is None

    def test_instantiation_custom_message(self):
        """Test InvalidScopeError with custom message."""
        error = InvalidScopeError(message="Cannot access other org's resources")

        assert error.message == "Cannot access other org's resources"

    def test_instantiation_with_allowed_scope(self):
        """Test InvalidScopeError with allowed scope."""
        error = InvalidScopeError(allowed_scope="org-001")

        assert error.details is not None
        assert error.details["allowed_scope"] == "org-001"
        assert "requested_scope" not in error.details

    def test_instantiation_with_requested_scope(self):
        """Test InvalidScopeError with requested scope."""
        error = InvalidScopeError(requested_scope="org-002")

        assert error.details is not None
        assert error.details["requested_scope"] == "org-002"
        assert "allowed_scope" not in error.details

    def test_instantiation_with_all_details(self):
        """Test InvalidScopeError with all details."""
        error = InvalidScopeError(
            message="Scope violation",
            allowed_scope="org-001",
            requested_scope="org-002",
        )

        assert error.details is not None
        assert error.details["allowed_scope"] == "org-001"
        assert error.details["requested_scope"] == "org-002"

    def test_inheritance(self):
        """Test InvalidScopeError inherits from BedrockGatewayError."""
        error = InvalidScopeError()
        assert isinstance(error, BedrockGatewayError)


class TestPoolConfigurationError:
    """Tests for PoolConfigurationError exception."""

    def test_instantiation_message_only(self):
        """Test PoolConfigurationError with message only."""
        error = PoolConfigurationError(message="Pool configuration invalid")

        assert error.error == "pool_configuration_error"
        assert error.message == "Pool configuration invalid"
        assert error.status_code == 400
        assert error.details is None

    def test_instantiation_with_details(self):
        """Test PoolConfigurationError with details."""
        details = {"account_id": "123456789012", "reason": "Invalid IAM role"}
        error = PoolConfigurationError(
            message="Failed to add pool account",
            details=details,
        )

        assert error.message == "Failed to add pool account"
        assert error.details is not None
        assert error.details["account_id"] == "123456789012"
        assert error.details["reason"] == "Invalid IAM role"

    def test_inheritance(self):
        """Test PoolConfigurationError inherits from BedrockGatewayError."""
        error = PoolConfigurationError(message="test")
        assert isinstance(error, BedrockGatewayError)


class TestExceptionHttpStatusCodes:
    """Tests verifying all exceptions have correct HTTP status codes."""

    def test_access_denied_is_403(self):
        """Test AccessDeniedError has status code 403."""
        assert AccessDeniedError().status_code == 403

    def test_invalid_role_is_400(self):
        """Test InvalidRoleError has status code 400."""
        assert InvalidRoleError(role="test").status_code == 400

    def test_resource_not_found_is_404(self):
        """Test ResourceNotFoundError has status code 404."""
        assert ResourceNotFoundError(resource_type="Test", resource_id="id").status_code == 404

    def test_resource_conflict_is_409(self):
        """Test ResourceConflictError has status code 409."""
        assert ResourceConflictError(resource_type="Test", field="id", value="1").status_code == 409

    def test_invalid_scope_is_403(self):
        """Test InvalidScopeError has status code 403."""
        assert InvalidScopeError().status_code == 403

    def test_pool_configuration_is_400(self):
        """Test PoolConfigurationError has status code 400."""
        assert PoolConfigurationError(message="test").status_code == 400


class TestExceptionRaisability:
    """Tests verifying all exceptions can be raised and caught."""

    def test_raise_access_denied(self):
        """Test AccessDeniedError can be raised and caught."""
        with pytest.raises(AccessDeniedError) as exc_info:
            raise AccessDeniedError(message="Test")
        assert exc_info.value.status_code == 403

    def test_raise_invalid_role(self):
        """Test InvalidRoleError can be raised and caught."""
        with pytest.raises(InvalidRoleError) as exc_info:
            raise InvalidRoleError(role="bad")
        assert exc_info.value.status_code == 400

    def test_raise_resource_not_found(self):
        """Test ResourceNotFoundError can be raised and caught."""
        with pytest.raises(ResourceNotFoundError) as exc_info:
            raise ResourceNotFoundError(resource_type="Test", resource_id="id")
        assert exc_info.value.status_code == 404

    def test_raise_resource_conflict(self):
        """Test ResourceConflictError can be raised and caught."""
        with pytest.raises(ResourceConflictError) as exc_info:
            raise ResourceConflictError(resource_type="Test", field="name", value="val")
        assert exc_info.value.status_code == 409

    def test_raise_invalid_scope(self):
        """Test InvalidScopeError can be raised and caught."""
        with pytest.raises(InvalidScopeError) as exc_info:
            raise InvalidScopeError()
        assert exc_info.value.status_code == 403

    def test_raise_pool_configuration(self):
        """Test PoolConfigurationError can be raised and caught."""
        with pytest.raises(PoolConfigurationError) as exc_info:
            raise PoolConfigurationError(message="Test")
        assert exc_info.value.status_code == 400

    def test_catch_as_bedrock_gateway_error(self):
        """Test all admin exceptions can be caught as BedrockGatewayError."""
        exceptions = [
            AccessDeniedError(),
            InvalidRoleError(role="test"),
            ResourceNotFoundError(resource_type="Test", resource_id="id"),
            ResourceConflictError(resource_type="Test", field="name", value="val"),
            InvalidScopeError(),
            PoolConfigurationError(message="test"),
        ]

        for exc in exceptions:
            with pytest.raises(BedrockGatewayError):
                raise exc

    def test_catch_as_exception(self):
        """Test all admin exceptions can be caught as base Exception."""
        exceptions = [
            AccessDeniedError(),
            InvalidRoleError(role="test"),
            ResourceNotFoundError(resource_type="Test", resource_id="id"),
            ResourceConflictError(resource_type="Test", field="name", value="val"),
            InvalidScopeError(),
            PoolConfigurationError(message="test"),
        ]

        for exc in exceptions:
            with pytest.raises(Exception):
                raise exc
