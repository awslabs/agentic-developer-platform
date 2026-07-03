"""
Rate Limit Enforcement Middleware (pure ASGI).

This middleware intercepts proxy requests to check rate limit constraints
before forwarding to Bedrock. It supports hierarchical enforcement across
the entity hierarchy (user → team → department → organization).

Issue #144: Added timing instrumentation for ratelimit_check segment.

IMPORTANT: This is a raw ASGI middleware — NOT BaseHTTPMiddleware.
All middleware in the chain must be pure ASGI to avoid the Starlette
BaseHTTPMiddleware hang bug when inner middleware short-circuits.
"""

import json

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from src.shared.enforced_paths import ENFORCED_PATHS
from src.shared.logging import get_logger
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.common import RateLimitCheckResult
from src.shared.timing import get_timings

from .service import RateLimitService

logger = get_logger(__name__)


class RateLimitEnforcementMiddleware:
    """
    Pure ASGI middleware for enforcing rate limits on proxy requests.

    When rate limit is exceeded, writes a 429 response directly via ASGI send().
    """

    def __init__(
        self,
        app: ASGIApp,
        ratelimit_service: RateLimitService | None = None,
    ):
        self.app = app
        self._ratelimit_service = ratelimit_service

    @property
    def ratelimit_service(self) -> RateLimitService:
        if self._ratelimit_service is None:
            self._ratelimit_service = RateLimitService()
        return self._ratelimit_service

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        if not self._should_enforce(path):
            await self.app(scope, receive, send)
            return

        state = scope.get("state", {})
        token_context: TokenContext | None = state.get("token_context")

        if not token_context:
            await self.app(scope, receive, send)
            return

        # Build a Request for timing access
        request = Request(scope, receive, send)

        timings = get_timings(request)
        with timings.time_segment("ratelimit_check"):
            check_result = await self.ratelimit_service.check_rate_limit(token_context)

            if not check_result.allowed:
                # Drain request body before responding
                while True:
                    msg = await receive()
                    if msg.get("type") == "http.disconnect":
                        return
                    if not msg.get("more_body", False):
                        break
                await self._send_rate_limited(send, check_result)
                return

            consume_result = await self.ratelimit_service.consume_rate_limit(token_context)

            if not consume_result.allowed:
                await self._send_rate_limited(send, consume_result)
                return

        # Track concurrent request
        tracked = True

        try:
            await self.app(scope, receive, send)
        finally:
            if tracked:
                await self.ratelimit_service.release_concurrent(token_context)

    def _should_enforce(self, path: str) -> bool:
        return any(path.startswith(enforced) for enforced in ENFORCED_PATHS)

    async def _send_rate_limited(self, send, result: RateLimitCheckResult) -> None:
        """Write a 429 JSON response directly via ASGI send()."""
        retry_after = result.retry_after_seconds or 60

        error_body = {
            "error": "rate_limited",
            "message": f"Rate limit exceeded ({result.limit_type or 'request'} limit)",
            "details": {
                "limit_type": result.limit_type,
                "limit": result.limit,
                "remaining": result.remaining or 0,
                "reset_seconds": retry_after,
            },
        }

        body_bytes = json.dumps(error_body).encode("utf-8")

        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body_bytes)).encode()),
            (b"retry-after", str(retry_after).encode()),
            (b"x-ratelimit-remaining", str(result.remaining or 0).encode()),
        ]
        if result.limit:
            headers.append((b"x-ratelimit-limit", str(result.limit).encode()))

        logger.warning(f"Rate limited - blocking request: limit_type={result.limit_type}, limit={result.limit}, retry_after={retry_after}s")

        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": headers,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body_bytes,
            }
        )


def create_ratelimit_enforcement_middleware(
    ratelimit_service: RateLimitService | None = None,
):
    """Factory function to create the rate limit enforcement middleware."""

    def middleware(app: ASGIApp) -> RateLimitEnforcementMiddleware:
        return RateLimitEnforcementMiddleware(app, ratelimit_service=ratelimit_service)

    return middleware
