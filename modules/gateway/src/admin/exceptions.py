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


class MemberRemovalConflictError(BedrockGatewayError):
    """A member cannot be deleted without affecting retained data or another org."""

    def __init__(self, message: str):
        super().__init__(error="member_removal_conflict", message=message, status_code=409)


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


class UnknownPlatformUserError(BedrockGatewayError):
    """Raised when an org-membership write names a ``users.id`` nobody holds.

    Issue #4943. 422, not 404, and the distinction carries information the operator
    needs. On this route the org in the path exists and the caller may write to it —
    what is wrong is a value in the *body*, so the request is unprocessable rather
    than the resource being absent. A 404 here would also be actively misleading in
    the one place it matters: the sibling team-add route returns 404 precisely to
    mean "that person is not in this org", which is the condition this route exists
    to fix. Two different diagnoses must not share one status code on the flow that
    chains them.
    """

    def __init__(self, user_id: str):
        super().__init__(
            error="unknown_platform_user",
            message=f"No platform user with id '{user_id}' exists. Pick a person from the roster rather than typing an id.",
            status_code=422,
            details={"user_id": user_id},
        )


class SecondPrimaryTeamError(BedrockGatewayError):
    """Raised when a write would give a user a second primary team in one org.

    Issue #4840. A user has at most one primary team per org, because
    ``users.team_id`` (and therefore the ``custom:team_id`` Cognito claim) can only
    project ONE team — two primaries make that projection ambiguous, and which team
    a user appears to be on would depend on row order.

    ``error`` is the stable, machine-readable contract the admin UI (T2b) branches
    on to surface this to the operator; the message is the human half. Do not
    change the ``error`` string without updating the UI that asserts on it.
    """

    ERROR_CODE = "team_membership_second_primary"

    def __init__(self, user_id: str, existing_team_id: str, requested_team_id: str):
        super().__init__(
            error=self.ERROR_CODE,
            message=(
                f"User already has a primary team ('{existing_team_id}'). A user can have only one primary team per organization — "
                f"unset the current primary before making '{requested_team_id}' primary, or send the full membership set with exactly one primary."
            ),
            status_code=409,
            details={
                "user_id": user_id,
                "existing_primary_team_id": existing_team_id,
                "requested_primary_team_id": requested_team_id,
            },
        )
