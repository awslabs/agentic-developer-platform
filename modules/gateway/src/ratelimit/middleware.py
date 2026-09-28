"""
Rate limiting middleware for FastAPI.

This module provides middleware that intercepts requests and
enforces rate limits using the RateLimitService.
"""

import logging
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from src.shared.schemas.auth import TokenContext

from .service import RateLimitService

logger = logging.getLogger(__name__)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    FastAPI middleware for rate limit enforcement.

    This middleware:
    - Intercepts incoming requests
    - Checks rate limits using RateLimitService
    - Returns 429 responses when limits are exceeded
    - Adds rate limit headers to responses
    - Releases concurrent slots after request completion
    """

    # Paths that bypass rate limiting
    EXEMPT_PATHS = {
        "/health",
        "/ready",
        "/metrics",
        "/docs",
        "/redoc",
        "/openapi.json",
    }

    def __init__(
        self,
        app: Callable,
        rate_limit_service: RateLimitService | None = None,
        exempt_paths: set[str] | None = None,
    ) -> None:
        """
        Initialize the middleware.

        Args:
            app: The ASGI application
            rate_limit_service: Rate limit service instance
            exempt_paths: Additional paths to exempt from rate limiting
        """
        super().__init__(app)
        self._service = rate_limit_service or RateLimitService()
        self._exempt_paths = self.EXEMPT_PATHS.copy()
        if exempt_paths:
            self._exempt_paths.update(exempt_paths)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Process the request with rate limiting."""
        # Skip rate limiting for exempt paths
        if self._is_exempt(request.url.path):
            return await call_next(request)

        # Get token context from request state (set by auth middleware)
        context: TokenContext | None = getattr(request.state, "token_context", None)

        if not context:
            # No authentication context - skip rate limiting
            # (auth middleware should handle unauthenticated requests)
            return await call_next(request)

        # Check rate limits
        check_result = await self._service.check_rate_limit(context)

        if not check_result.allowed:
            return self._create_rate_limit_response(check_result)

        # Consume rate limit tokens
        consume_result = await self._service.consume_rate_limit(context)

        if not consume_result.allowed:
            return self._create_rate_limit_response(consume_result)

        try:
            # Process the request
            response = await call_next(request)

            # Add rate limit headers
            response = await self._add_rate_limit_headers(response, context)

            return response
        finally:
            # Release concurrent request slot
            await self._service.release_concurrent(context)

    def _is_exempt(self, path: str) -> bool:
        """Check if a path is exempt from rate limiting."""
        # Exact match
        if path in self._exempt_paths:
            return True

        # Prefix match for paths like /docs/*
        for exempt_path in self._exempt_paths:
            if path.startswith(exempt_path + "/"):
                return True

        return False

    def _create_rate_limit_response(self, check_result) -> JSONResponse:
        """Create a 429 rate limit response."""
        error_body = {
            "error": "rate_limited",
            "message": f"Rate limit exceeded: {check_result.limit_type}",
            "type": check_result.limit_type,
            "limit": check_result.limit,
            "retry_after_seconds": check_result.retry_after_seconds,
        }

        headers = {
            "Retry-After": str(check_result.retry_after_seconds or 60),
            "X-RateLimit-Limit": str(check_result.limit or 0),
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(check_result.retry_after_seconds or 60),
        }

        return JSONResponse(
            status_code=429,
            content=error_body,
            headers=headers,
        )

    async def _add_rate_limit_headers(
        self,
        response: Response,
        context: TokenContext,
    ) -> Response:
        """Add rate limit headers to the response."""
        try:
            # Get current status for the user
            from .models import EntityType

            entity_type = EntityType.SERVICE_ACCOUNT if context.account_type == "service" else EntityType.USER
            status = await self._service.get_status(
                entity_type,
                context.user_id,
                context.org_id,
                context.account_type == "service",
            )

            # Add RPM headers if available
            if status.rpm_limit is not None:
                response.headers["X-RateLimit-Limit"] = str(status.rpm_limit)
                if status.rpm_remaining is not None:
                    response.headers["X-RateLimit-Remaining"] = str(status.rpm_remaining)
                if status.rpm_reset_seconds is not None:
                    response.headers["X-RateLimit-Reset"] = str(status.rpm_reset_seconds)

        except Exception as e:
            logger.warning(f"Failed to add rate limit headers: {e}")

        return response


def create_rate_limit_middleware(
    rate_limit_service: RateLimitService | None = None,
    exempt_paths: set[str] | None = None,
) -> type[RateLimitMiddleware]:
    """
    Factory to create rate limit middleware with configuration.

    Args:
        rate_limit_service: Rate limit service instance
        exempt_paths: Additional paths to exempt

    Returns:
        Configured middleware class
    """

    class ConfiguredRateLimitMiddleware(RateLimitMiddleware):
        def __init__(self, app: Callable) -> None:
            super().__init__(app, rate_limit_service, exempt_paths)

    return ConfiguredRateLimitMiddleware
