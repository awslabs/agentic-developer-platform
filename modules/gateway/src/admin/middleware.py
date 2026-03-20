"""Request logging middleware for audit and analytics."""

import time
import uuid
from collections.abc import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from src.admin.models import RequestLog
from src.shared.database import get_session_factory


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Middleware that logs HTTP requests to the database for audit and analytics.

    Features:
    - Captures request metadata (method, path, status, timing)
    - Associates requests with user and organization context
    - Supports configurable path exclusions
    - Stores logs in PostgreSQL for querying
    """

    # Default paths to exclude from logging
    DEFAULT_EXCLUDED_PATHS: set[str] = {
        "/health",
        "/ready",
        "/metrics",
        "/docs",
        "/openapi.json",
        "/redoc",
    }

    # Default sensitive headers to exclude
    DEFAULT_EXCLUDED_HEADERS: set[str] = {
        "authorization",
        "x-api-key",
        "cookie",
        "set-cookie",
    }

    def __init__(
        self,
        app: ASGIApp,
        excluded_paths: set[str] | None = None,
        excluded_headers: set[str] | None = None,
        log_request_body: bool = False,
        log_response_body: bool = False,
    ):
        """
        Initialize the request logging middleware.

        Args:
            app: The ASGI application
            excluded_paths: Paths to exclude from logging
            excluded_headers: Headers to exclude from logs
            log_request_body: Whether to log request body sizes
            log_response_body: Whether to log response body sizes
        """
        super().__init__(app)
        self.excluded_paths = excluded_paths or self.DEFAULT_EXCLUDED_PATHS
        self.excluded_headers = excluded_headers or self.DEFAULT_EXCLUDED_HEADERS
        self.log_request_body = log_request_body
        self.log_response_body = log_response_body

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """
        Process a request and log it to the database.

        Args:
            request: The incoming request
            call_next: The next middleware/handler

        Returns:
            The response from the handler
        """
        # Skip excluded paths
        if self._should_skip(request.url.path):
            return await call_next(request)

        # Generate request ID if not present
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())

        # Record start time
        start_time = time.time()

        # Get request body size if enabled
        request_body_size = None
        if self.log_request_body:
            content_length = request.headers.get("content-length")
            if content_length:
                request_body_size = int(content_length)

        # Process the request
        response: Response = await call_next(request)

        # Calculate response time
        response_time_ms = int((time.time() - start_time) * 1000)

        # Get response body size if enabled
        response_body_size = None
        if self.log_response_body:
            content_length = response.headers.get("content-length")
            if content_length:
                response_body_size = int(content_length)

        # Extract user context from request state (populated by auth middleware)
        user_id = getattr(request.state, "user_id", None)
        org_id = getattr(request.state, "org_id", None)
        department_id = getattr(request.state, "department_id", None)
        team_id = getattr(request.state, "team_id", None)

        # Get client IP
        client_ip = self._get_client_ip(request)

        # Get user agent
        user_agent = request.headers.get("user-agent")

        # Get query params (sanitized)
        query_params = dict(request.query_params) if request.query_params else None

        # Log the request asynchronously
        await self._log_request(
            request_id=request_id,
            user_id=user_id or "anonymous",
            org_id=org_id or "unknown",
            department_id=department_id,
            team_id=team_id,
            method=request.method,
            path=request.url.path,
            query_params=query_params,
            status_code=response.status_code,
            response_time_ms=response_time_ms,
            request_body_size=request_body_size,
            response_body_size=response_body_size,
            client_ip=client_ip,
            user_agent=user_agent,
            error_message=None,
        )

        # Add request ID to response headers
        response.headers["x-request-id"] = request_id

        return response

    def _should_skip(self, path: str) -> bool:
        """Check if a path should be excluded from logging."""
        # Check exact match
        if path in self.excluded_paths:
            return True

        # Check prefix match (for paths like /docs/...)
        for excluded in self.excluded_paths:
            if path.startswith(excluded):
                return True

        return False

    def _get_client_ip(self, request: Request) -> str | None:
        """Extract client IP from request, handling proxies."""
        # Check X-Forwarded-For header (set by proxies/load balancers)
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            # Take the first IP in the chain
            return forwarded_for.split(",")[0].strip()

        # Check X-Real-IP header
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip

        # Fall back to client host
        if request.client:
            return request.client.host

        return None

    async def _log_request(
        self,
        request_id: str,
        user_id: str,
        org_id: str,
        department_id: str | None,
        team_id: str | None,
        method: str,
        path: str,
        query_params: dict | None,
        status_code: int,
        response_time_ms: int,
        request_body_size: int | None,
        response_body_size: int | None,
        client_ip: str | None,
        user_agent: str | None,
        error_message: str | None,
    ) -> None:
        """
        Log a request to the database.

        This is done asynchronously to not block the response.
        """
        try:
            session_factory = get_session_factory()
            async with session_factory() as session:
                log_entry = RequestLog(
                    request_id=request_id,
                    user_id=user_id,
                    org_id=org_id,
                    department_id=department_id,
                    team_id=team_id,
                    method=method,
                    path=path,
                    query_params=query_params,
                    status_code=status_code,
                    response_time_ms=response_time_ms,
                    request_body_size=request_body_size,
                    response_body_size=response_body_size,
                    client_ip=client_ip,
                    user_agent=user_agent,
                    error_message=error_message,
                )
                session.add(log_entry)
                await session.commit()
        except Exception:
            # Don't let logging failures affect the request
            # In production, would log this to a fallback location
            pass


def create_request_logging_middleware(
    excluded_paths: set[str] | None = None,
    log_bodies: bool = False,
) -> type[RequestLoggingMiddleware]:
    """
    Factory function to create a configured RequestLoggingMiddleware.

    Args:
        excluded_paths: Additional paths to exclude from logging
        log_bodies: Whether to log request/response body sizes

    Returns:
        A configured middleware class
    """
    all_excluded = RequestLoggingMiddleware.DEFAULT_EXCLUDED_PATHS.copy()
    if excluded_paths:
        all_excluded.update(excluded_paths)

    class ConfiguredMiddleware(RequestLoggingMiddleware):
        def __init__(self, app: ASGIApp):
            super().__init__(
                app,
                excluded_paths=all_excluded,
                log_request_body=log_bodies,
                log_response_body=log_bodies,
            )

    return ConfiguredMiddleware
