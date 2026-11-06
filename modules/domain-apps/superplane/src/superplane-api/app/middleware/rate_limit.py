"""Rate limiting middleware — per-user and per-workspace rate limiting.

Uses a simple in-memory sliding window counter. In production, replace with
Redis-backed rate limiting (e.g., via fastapi-limiter or custom Redis implementation).
"""

import hashlib
import logging
import time
from collections import OrderedDict

from fastapi import HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Default rate limits
DEFAULT_REQUESTS_PER_MINUTE = 60
DEFAULT_WINDOW_SECONDS = 60


class RateLimitEntry:
    """Sliding window counter for a single key."""

    __slots__ = ("timestamps", "last_seen")

    def __init__(self) -> None:
        self.timestamps: list[float] = []
        self.last_seen = time.monotonic()

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
        max_buckets: int = 10_000,
    ):
        super().__init__(app)
        self.requests_per_minute = requests_per_minute
        self.window_seconds = window_seconds
        self.max_buckets = max(1, max_buckets)
        self._buckets: OrderedDict[str, RateLimitEntry] = OrderedDict()

    async def _extract_key(self, request: Request) -> str:
        """Quota belongs to a verified principal, independent of path/token rotation.

        This verifies identity for accounting only; endpoint authorization remains
        authoritative. Invalid credentials share the transport IP quota, never a
        new bucket derived from arbitrary header text.
        """
        auth_header = request.headers.get("authorization", "").strip()
        host = request.client.host if request.client else "unknown"
        if request.url.path.startswith("/internal/observations"):
            from app.services.observations import load_submitters

            caller = load_submitters().resolve(auth_header) if auth_header else None
            if caller is not None:
                identity = hashlib.sha256(caller.submitter_id.encode()).hexdigest()
                return f"observation:{identity}"
            return f"observation:ip:{host}"
        scheme, _, token = auth_header.partition(" ")
        if scheme.lower() == "bearer" and token and len(token) <= 16_384:
            from app.config import settings

            try:
                if settings.domain_auth_enforced:
                    from app.auth import verify_access_token
                    from superplane_auth.policy import TRUSTED_VALIDATION_PATH, TokenRejectedError

                    policy = getattr(request.app.state, "domain_policy", None)
                    if policy is None:
                        return f"ip:{host}"
                    try:
                        claims = await run_in_threadpool(verify_access_token, token)
                        candidate = policy.admit_candidate(claims, validation_path=TRUSTED_VALIDATION_PATH)
                    except TokenRejectedError:
                        return f"ip:{host}"
                    identity = (candidate.org_id, candidate.subject)
                else:
                    from app.middleware.auth import decode_token

                    verified = decode_token(token)
                    identity = (str(verified.org_id), str(verified.user_id or verified.sub))
                # Length-prefixing avoids ambiguous org/subject concatenations.
                encoded = "".join(f"{len(part)}:{part}" for part in identity)
                return "principal:" + hashlib.sha256(encoded.encode()).hexdigest()
            except HTTPException:
                pass
        return f"ip:{host}"

    def _entry(self, key: str) -> RateLimitEntry | None:
        now = time.monotonic()
        # Entries are ordered by last access. Reclaim idle entries without
        # scanning the entire map, and never evict a live quota to admit a key.
        while self._buckets:
            first = next(iter(self._buckets))
            if now - self._buckets[first].last_seen < self.window_seconds:
                break
            self._buckets.popitem(last=False)
        if key not in self._buckets:
            if len(self._buckets) >= self.max_buckets:
                return None
            self._buckets[key] = RateLimitEntry()
        entry = self._buckets[key]
        entry.last_seen = now
        self._buckets.move_to_end(key)
        return entry

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

        key = await self._extract_key(request)
        entry = self._entry(key)

        if entry is None or not entry.is_allowed(self.window_seconds, self.requests_per_minute):
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
