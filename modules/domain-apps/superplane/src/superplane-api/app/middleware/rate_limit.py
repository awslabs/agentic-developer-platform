"""Rate limiting middleware — per-user and per-workspace rate limiting.

Uses a simple in-memory sliding window counter. In production, replace with
Redis-backed rate limiting (e.g., via fastapi-limiter or custom Redis implementation).
"""

import hashlib
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
        """Partition known observation callers by their configured identity.

        Unauthenticated callers use the transport's client address; a spoofable
        forwarded header cannot buy a fresh quota. Existing bearer traffic keeps
        its token bucket. No raw credential is retained in a key or log.
        """
        auth_header = request.headers.get("authorization", "").strip()
        if request.url.path.startswith("/internal/observations"):
            from app.services.observations import load_submitters

            caller = load_submitters().resolve(auth_header) if auth_header else None
            if caller is not None:
                identity = hashlib.sha256(caller.submitter_id.encode()).hexdigest()
                return f"observation:{identity}"
            host = request.client.host if request.client else "unknown"
            return f"observation:ip:{host}"
        org_key = "anonymous"
        if auth_header.startswith("Bearer "):
            org_key = "token:" + hashlib.sha256(auth_header.encode()).hexdigest()
        else:
            org_key = "ip:" + (request.client.host if request.client else "unknown")
        parts = request.url.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "workspaces":
            return f"ws:{parts[1]}:{org_key}"
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
