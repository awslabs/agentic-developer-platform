"""
Rate limiting exceptions.

This module defines custom exceptions for the rate limiting system.
"""

from src.shared.exceptions import BedrockGatewayError


class RateLimitError(BedrockGatewayError):
    """Base exception for rate limiting errors."""

    def __init__(self, message: str, status_code: int = 500, details: dict | None = None):
        super().__init__("rate_limit_error", message, status_code, details)


class RateLimitExceededError(BedrockGatewayError):
    """Exception raised when a rate limit is exceeded."""

    def __init__(
        self,
        limit_type: str,
        limit: int,
        retry_after: int,
        entity_type: str | None = None,
        entity_id: str | None = None,
    ):
        details = {
            "type": limit_type,
            "limit": limit,
            "retry_after_seconds": retry_after,
        }
        if entity_type:
            details["entity_type"] = entity_type
        if entity_id:
            details["entity_id"] = entity_id

        super().__init__(
            "rate_limited",
            f"Rate limit exceeded: {limit_type}",
            429,
            details,
        )
        self.limit_type = limit_type
        self.limit = limit
        self.retry_after = retry_after


class TooManyConcurrentRequestsError(BedrockGatewayError):
    """Exception raised when concurrent request limit is exceeded."""

    def __init__(self, limit: int, current: int, retry_after: int = 5):
        super().__init__(
            "too_many_concurrent_requests",
            f"Too many concurrent requests. Limit: {limit}, Current: {current}",
            429,
            {
                "limit": limit,
                "current": current,
                "retry_after_seconds": retry_after,
            },
        )
        self.limit = limit
        self.current = current
        self.retry_after = retry_after


class RateLimitConfigError(BedrockGatewayError):
    """Exception raised when rate limit configuration is invalid."""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(
            "rate_limit_config_error",
            message,
            400,
            details,
        )


class BackendConnectionError(BedrockGatewayError):
    """Exception raised when backend connection fails."""

    def __init__(self, backend_type: str, message: str):
        super().__init__(
            "backend_connection_error",
            f"Failed to connect to {backend_type} backend: {message}",
            503,
            {"backend_type": backend_type},
        )
