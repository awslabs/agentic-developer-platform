"""Admin module custom exceptions."""

from src.shared.exceptions import BedrockGatewayError


class AccessDeniedError(BedrockGatewayError):
    """Raised when a user does not have sufficient permissions."""

    def __init__(
        self,
        message: str = "Access denied",
        required_permission: str | None = None,
        user_role: str | None = None,
    ):
        details = {}
        if required_permission:
            details["required_permission"] = required_permission
        if user_role:
            details["user_role"] = user_role
        super().__init__(
            error="access_denied",
            message=message,
            status_code=403,
            details=details if details else None,
        )


class InvalidRoleError(BedrockGatewayError):
    """Raised when an invalid role is specified."""

    def __init__(self, role: str):
        super().__init__(
            error="invalid_role",
            message=f"Invalid admin role: {role}",
            status_code=400,
            details={"role": role, "valid_roles": ["platform_admin", "org_admin", "dept_admin"]},
        )


class ResourceNotFoundError(BedrockGatewayError):
    """Raised when a requested resource is not found."""

    def __init__(
        self,
        resource_type: str,
        resource_id: str,
    ):
        super().__init__(
            error="resource_not_found",
            message=f"{resource_type} with id '{resource_id}' not found",
            status_code=404,
            details={"resource_type": resource_type, "resource_id": resource_id},
        )


class ResourceConflictError(BedrockGatewayError):
    """Raised when there is a resource conflict (e.g., duplicate name)."""

    def __init__(
        self,
        resource_type: str,
        field: str,
        value: str,
    ):
        super().__init__(
            error="resource_conflict",
            message=f"{resource_type} with {field} '{value}' already exists",
            status_code=409,
            details={"resource_type": resource_type, "field": field, "value": value},
        )


class InvalidScopeError(BedrockGatewayError):
    """Raised when a user tries to access resources outside their scope."""

    def __init__(
        self,
        message: str = "Operation outside allowed scope",
        allowed_scope: str | None = None,
        requested_scope: str | None = None,
    ):
        details = {}
        if allowed_scope:
            details["allowed_scope"] = allowed_scope
        if requested_scope:
            details["requested_scope"] = requested_scope
        super().__init__(
            error="invalid_scope",
            message=message,
            status_code=403,
            details=details if details else None,
        )


class PoolConfigurationError(BedrockGatewayError):
    """Raised when there is an issue with pool configuration."""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(
            error="pool_configuration_error",
            message=message,
            status_code=400,
            details=details,
        )


class CognitoNotConfiguredError(BedrockGatewayError):
    """Raised when Cognito is not configured but a Cognito operation is requested.

    Issue #226: Error for when Cognito-backed endpoints are called but
    COGNITO_USER_POOL_ID environment variable is not set.
    """

    def __init__(self):
        super().__init__(
            error="cognito_not_configured",
            message="Cognito User Pool is not configured. Set COGNITO_USER_POOL_ID environment variable.",
            status_code=503,
            details={"hint": "Cognito integration is required for this endpoint. Please configure the COGNITO_USER_POOL_ID environment variable."},
        )
