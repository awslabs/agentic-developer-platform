"""
Logging middleware for BedrockGateway (pure ASGI).

This middleware provides:
- Unique request_id generation per request
- Context propagation via contextvars (org_id, user_id, team_id)
- X-Request-ID response header
- Request start/end logging with latency
- X-Gateway-Timing response header with per-segment latency breakdown (Issue #144)

IMPORTANT: This is a raw ASGI middleware — NOT BaseHTTPMiddleware.
All middleware in the chain must be pure ASGI to avoid the Starlette
BaseHTTPMiddleware hang bug when inner middleware short-circuits.
"""

import logging
import time
import uuid

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.shared.logging import clear_request_context, get_request_id, set_request_context
from src.shared.schemas.auth import TokenContext
from src.shared.timing import get_timings

logger = logging.getLogger(__name__)


# Paths that should not be logged (to reduce noise)
_SKIP_PATHS = {
    "/health",
    "/ready",
    "/metrics",
    "/docs",
    "/redoc",
    "/openapi.json",
}


class LoggingMiddleware:
    """
    Pure ASGI middleware for request logging and context propagation.

    Wraps the inner app's send() to intercept the response status code
    and inject headers (X-Request-ID, X-Gateway-Timing) without relying
    on BaseHTTPMiddleware's call_next() mechanism.
    """

    def __init__(self, app: ASGIApp, **kwargs) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "")
        should_skip = _should_skip(path)

        # Extract or generate request ID from headers
        headers_list = scope.get("headers", [])
        request_id = _extract_request_id(headers_list) or str(uuid.uuid4())

        # Set logging context
        set_request_context(request_id=request_id)

        # Initialize state on scope for downstream middleware
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        state["timings"] = {}

        # Build a read-only Request for logging helpers
        request = Request(scope, receive, send)

        start_time = time.monotonic()

        if not should_skip:
            _log_request_start(request)

        # Track response status for logging
        response_status = [0]

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_status[0] = message.get("status", 0)
                # Inject headers into the response
                resp_headers = list(message.get("headers", []))
                resp_headers.append((b"x-request-id", request_id.encode()))

                # Add timing header
                timings = get_timings(request)
                latency_ms = (time.monotonic() - start_time) * 1000
                timings.record("total", latency_ms)
                timing_header = timings.to_header()
                if timing_header:
                    resp_headers.append((b"x-gateway-timing", timing_header.encode()))

                message = {**message, "headers": resp_headers}

            await send(message)

            # Log on body completion
            if message["type"] == "http.response.body":
                more_body = message.get("more_body", False)
                if not more_body and not should_skip:
                    latency_ms = (time.monotonic() - start_time) * 1000
                    # Extract token context if available
                    token_context = state.get("token_context")
                    if token_context and isinstance(token_context, TokenContext):
                        set_request_context(
                            org_id=token_context.org_id,
                            user_id=token_context.user_id,
                            team_id=token_context.team_id,
                            department_id=token_context.department_id,
                        )
                    _log_request_end(request, response_status[0], latency_ms)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as e:
            latency_ms = (time.monotonic() - start_time) * 1000
            timings = get_timings(request)
            timings.record("total", latency_ms)
            logger.error(
                "Request failed with exception",
                extra={
                    "method": method,
                    "path": path,
                    "latency_ms": round(latency_ms, 2),
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "timings": timings.to_dict(),
                },
            )
            raise
        finally:
            clear_request_context()


def _should_skip(path: str) -> bool:
    if path in _SKIP_PATHS:
        return True
    for skip_path in _SKIP_PATHS:
        if path.startswith(skip_path + "/"):
            return True
    return False


def _extract_request_id(headers: list) -> str | None:
    for key, value in headers:
        if key == b"x-request-id":
            return value.decode()
    return None


def _log_request_start(request: Request) -> None:
    extra = {
        "event": "request_start",
        "method": request.method,
        "path": request.url.path,
    }
    query = str(request.query_params) if request.query_params else None
    if query:
        extra["query_string"] = query
    user_agent = request.headers.get("User-Agent")
    if user_agent:
        extra["user_agent"] = user_agent
    client_ip = _get_client_ip(request)
    if client_ip:
        extra["client_ip"] = client_ip
    logger.info("Request started", extra=extra)


def _log_request_end(request: Request, status_code: int, latency_ms: float) -> None:
    extra = {
        "event": "request_end",
        "method": request.method,
        "path": request.url.path,
        "status_code": status_code,
        "latency_ms": round(latency_ms, 2),
    }
    timings = get_timings(request)
    timings_dict = timings.to_dict()
    if timings_dict:
        extra["timings"] = timings_dict

    if status_code >= 500:
        logger.error("Request completed with server error", extra=extra)
    elif status_code >= 400:
        logger.warning("Request completed with client error", extra=extra)
    else:
        logger.info("Request completed", extra=extra)


def _get_client_ip(request: Request) -> str | None:
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip
    if request.client:
        return request.client.host
    return None


def get_current_request_id() -> str | None:
    """Get the current request ID from context."""
    return get_request_id()
