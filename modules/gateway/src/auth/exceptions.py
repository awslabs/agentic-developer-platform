"""
Authentication-specific exceptions for the Bedrock Gateway Auth module.

These exceptions extend the base BedrockGatewayError class and provide
specific error handling for authentication flows.
"""

from src.shared.exceptions import BedrockGatewayError


class AuthenticationError(BedrockGatewayError):
    """Raised when authentication fails."""

    def __init__(self, message: str = "Authentication failed"):
        super().__init__("authentication_failed", message, 401)


class AuthorizationError(BedrockGatewayError):
    """Raised when authorization fails."""

    def __init__(self, message: str = "Authorization failed"):
        super().__init__("authorization_failed", message, 403)


class TokenValidationError(BedrockGatewayError):
    """Raised when token validation fails."""

    def __init__(self, message: str = "Token validation failed"):
        super().__init__("token_validation_failed", message, 401)


class STSClientError(BedrockGatewayError):
    """Raised when AWS STS operations fail."""

    def __init__(self, message: str = "AWS STS operation failed", details: dict | None = None):
        super().__init__("sts_operation_failed", message, 500, details)


class ServiceAccountNotFoundError(BedrockGatewayError):
    """Raised when a service account is not found."""

    def __init__(self, identifier: str):
        super().__init__("service_account_not_found", f"Service account '{identifier}' not found", 404)


class DuplicateServiceAccountError(BedrockGatewayError):
    """Raised when attempting to create a duplicate service account."""

    def __init__(self, identifier: str):
        super().__init__("duplicate_service_account", f"Service account '{identifier}' already exists", 409)


class TenantResolutionError(BedrockGatewayError):
    """Raised when tenant resolution fails."""

    def __init__(self, message: str = "Unable to resolve tenant information"):
        super().__init__("tenant_resolution_failed", message, 500)


class TokenGenerationError(BedrockGatewayError):
    """Raised when token generation fails."""

    def __init__(self, message: str = "Token generation failed"):
        super().__init__("token_generation_failed", message, 500)


class TokenStorageError(BedrockGatewayError):
    """Raised when token storage operations fail."""

    def __init__(self, message: str = "Token storage operation failed"):
        super().__init__("token_storage_failed", message, 500)
