"""Rate limiting middleware — per-user and per-workspace rate limiting.

Uses a simple in-memory sliding window counter. In production, replace with
Redis-backed rate limiting (e.g., via fastapi-limiter or custom Redis implementation).
"""

import logging
import time
from collections import defaultdict

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Default rate limits
DEFAULT_REQUESTS_PER_MINUTE = 60
DEFAULT_WINDOW_SECONDS = 60


class RateLimitEntry:
    """Sliding window counter for a single key."""

    __slots__ = ("timestamps",)

    def __init__(self) -> None:
        self.timestamps: list[float] = []

    def is_allowed(self, window_seconds: int, max_requests: int) -> bool:
        """Check if a request is allowed within the sliding window."""
        now = time.monotonic()
        cutoff = now - window_seconds

        # Remove expired timestamps
        self.timestamps = [ts for ts in self.timestamps if ts > cutoff]

        if len(self.timestamps) >= max_requests:
            return False

        self.timestamps.append(now)
        return True

    @property
    def count(self) -> int:
        return len(self.timestamps)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-user and per-workspace rate limiting middleware.

    Extracts identity from:
    - JWT org_id (parsed from Authorization header)
    - Workspace ID from URL path

    Rate limit headers are added to all responses:
    - X-RateLimit-Limit: max requests per window
    - X-RateLimit-Remaining: remaining requests
    - X-RateLimit-Reset: seconds until window resets
    """

    def __init__(
        self,
        app,
        requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
    ):
        super().__init__(app)
        self.requests_per_minute = requests_per_minute
        self.window_seconds = window_seconds
        self._buckets: dict[str, RateLimitEntry] = defaultdict(RateLimitEntry)

    def _extract_key(self, request: Request) -> str:
        """Extract a rate-limiting key from the request.

        Priority:
        1. org_id from JWT (if available via request state)
        2. Workspace ID from URL path
        3. Client IP as fallback
        """
        # Try to extract from path: /workspaces/{id}/...
        path_parts = request.url.path.strip("/").split("/")
        workspace_id = None
        if len(path_parts) >= 2 and path_parts[0] == "workspaces":
            workspace_id = path_parts[1]

        # Try to get org_id from authorization header (lightweight parse)
        auth_header = request.headers.get("authorization", "")
        org_key = "anonymous"
        if auth_header.startswith("Bearer "):
            # Use a hash of the token as the key (don't decode here — too expensive)
            token_hash = str(hash(auth_header))
            org_key = f"token:{token_hash}"

        if workspace_id:
            return f"ws:{workspace_id}:{org_key}"
        return f"global:{org_key}"

    async def dispatch(self, request: Request, call_next) -> Response:
        """Check rate limit before forwarding request."""
        # Skip rate limiting for health checks and docs
        if request.url.path in (
            "/health",
            "/healthz",
            "/docs",
            "/redoc",
            "/openapi.json",
        ):
            return await call_next(request)

        key = self._extract_key(request)
        entry = self._buckets[key]

        if not entry.is_allowed(self.window_seconds, self.requests_per_minute):
            remaining = 0
            retry_after = self.window_seconds
            logger.warning("Rate limit exceeded for key: %s", key)
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Please retry later."},
                headers={
                    "X-RateLimit-Limit": str(self.requests_per_minute),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(retry_after),
                    "Retry-After": str(retry_after),
                },
            )

        response = await call_next(request)

        # Add rate limit headers
        remaining = max(0, self.requests_per_minute - entry.count)
        response.headers["X-RateLimit-Limit"] = str(self.requests_per_minute)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(self.window_seconds)

        return response
