"""Per-tenant sliding-window rate limiting via DynamoDB atomic counters.

Uses the `adp-<env>-rate-limits` table with atomic ADD operations to implement
a sliding-window counter. Each window is a 5-minute bucket keyed by tenant_id
and the window start timestamp.

Table schema:
  PK: tenant_id
  SK: window (e.g., "2026-05-01T20:00" for 5-min windows)
  Attributes: count (atomic counter), ttl (epoch seconds)

Default limits (configurable per tenant via tenant-registry):
  - 50 agent dispatches per 5 minutes per tenant
  - 500 agent dispatches per hour per tenant

When rate-limited: caller should return HTTP 429 with Retry-After header.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

# Window size: 5 minutes
WINDOW_SIZE_SECONDS = 5 * 60

# Default limits
DEFAULT_LIMIT_PER_WINDOW = 50  # 50 dispatches per 5-min window
DEFAULT_LIMIT_PER_HOUR = 500  # 500 dispatches per hour (12 windows)

# TTL: keep rate-limit rows for 2 hours (cleanup old windows)
RATE_LIMIT_TTL_SECONDS = 2 * 60 * 60


@dataclass
class RateLimitResult:
    """Result of a rate limit check."""

    allowed: bool
    current_count: int
    limit: int
    window: str
    retry_after_seconds: int = 0

    @property
    def headers(self) -> dict[str, str]:
        """HTTP headers to include in the response."""
        headers: dict[str, str] = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.limit - self.current_count)),
            "X-RateLimit-Window": self.window,
        }
        if not self.allowed:
            headers["Retry-After"] = str(self.retry_after_seconds)
        return headers


class RateLimiter:
    """Per-tenant sliding-window rate limiter backed by DynamoDB.

    Usage:
        limiter = RateLimiter(table_name="adp-dev-rate-limits")
        result = limiter.check_and_increment("acme-corp")
        if not result.allowed:
            return {"statusCode": 429, "headers": result.headers, ...}
    """

    def __init__(
        self,
        table_name: str,
        region: str = "us-east-1",
        limit_per_window: int = DEFAULT_LIMIT_PER_WINDOW,
        limit_per_hour: int = DEFAULT_LIMIT_PER_HOUR,
    ):
        self._table_name = table_name
        self._dynamodb = boto3.resource("dynamodb", region_name=region)
        self._table = self._dynamodb.Table(table_name)
        self._limit_per_window = limit_per_window
        self._limit_per_hour = limit_per_hour

    def _current_window(self, now: float | None = None) -> str:
        """Get the current 5-minute window key.

        Rounds down to the nearest 5-minute boundary.
        Format: "YYYY-MM-DDTHH:MM" (minutes always divisible by 5).
        """
        if now is None:
            now = time.time()
        window_start = int(now) - (int(now) % WINDOW_SIZE_SECONDS)
        return time.strftime("%Y-%m-%dT%H:%M", time.gmtime(window_start))

    def _hour_windows(self, now: float | None = None) -> list[str]:
        """Get all 5-minute window keys in the current hour (last 12 windows)."""
        if now is None:
            now = time.time()
        windows = []
        for i in range(12):  # 12 x 5 min = 1 hour
            window_start = (
                int(now) - (int(now) % WINDOW_SIZE_SECONDS) - (i * WINDOW_SIZE_SECONDS)
            )
            windows.append(time.strftime("%Y-%m-%dT%H:%M", time.gmtime(window_start)))
        return windows

    def check_and_increment(
        self,
        tenant_id: str,
        *,
        limit_per_window: int | None = None,
        limit_per_hour: int | None = None,
        now: float | None = None,
    ) -> RateLimitResult:
        """Check rate limit and atomically increment the counter.

        This is a single atomic operation — safe under concurrent Lambda invocations.
        Uses DynamoDB's ADD expression for lock-free atomic increment.

        Args:
            tenant_id: The tenant to rate-limit.
            limit_per_window: Override the per-window limit (for custom tenant config).
            limit_per_hour: Override the per-hour limit (for custom tenant config).
            now: Override current time (for testing).

        Returns:
            RateLimitResult indicating whether the request is allowed.
        """
        if now is None:
            now = time.time()

        window = self._current_window(now)
        per_window_limit = limit_per_window or self._limit_per_window
        per_hour_limit = limit_per_hour or self._limit_per_hour
        ttl = int(now) + RATE_LIMIT_TTL_SECONDS

        # First check: current window count (pre-increment read)
        current_count = self._get_window_count(tenant_id, window)
        if current_count >= per_window_limit:
            retry_after = WINDOW_SIZE_SECONDS - (int(now) % WINDOW_SIZE_SECONDS)
            logger.warning(
                "Rate limited tenant=%s window=%s count=%d limit=%d",
                tenant_id,
                window,
                current_count,
                per_window_limit,
            )
            return RateLimitResult(
                allowed=False,
                current_count=current_count,
                limit=per_window_limit,
                window=window,
                retry_after_seconds=retry_after,
            )

        # Second check: hourly aggregate
        hour_count = self._get_hour_count(tenant_id, now)
        if hour_count >= per_hour_limit:
            # Find seconds until the oldest window in the hour expires
            retry_after = WINDOW_SIZE_SECONDS - (int(now) % WINDOW_SIZE_SECONDS)
            logger.warning(
                "Rate limited (hourly) tenant=%s hour_count=%d limit=%d",
                tenant_id,
                hour_count,
                per_hour_limit,
            )
            return RateLimitResult(
                allowed=False,
                current_count=hour_count,
                limit=per_hour_limit,
                window=window,
                retry_after_seconds=retry_after,
            )

        # Atomically increment the counter
        new_count = self._increment(tenant_id, window, ttl)

        return RateLimitResult(
            allowed=True,
            current_count=new_count,
            limit=per_window_limit,
            window=window,
        )

    def _get_window_count(self, tenant_id: str, window: str) -> int:
        """Get the current count for a specific window."""
        try:
            response = self._table.get_item(
                Key={"tenant_id": tenant_id, "window": window},
                ProjectionExpression="#c",
                ExpressionAttributeNames={"#c": "count"},
            )
            item = response.get("Item", {})
            return int(item.get("count", 0))
        except Exception as e:
            logger.error(
                "Failed to get window count for %s/%s: %s", tenant_id, window, e
            )
            # Fail open — don't block requests on DDB errors
            return 0

    def _get_hour_count(self, tenant_id: str, now: float | None = None) -> int:
        """Get the aggregate count across all windows in the last hour."""
        windows = self._hour_windows(now)
        total = 0
        try:
            # Batch get all windows for this tenant in the last hour
            response = self._table.query(
                KeyConditionExpression=(
                    Key("tenant_id").eq(tenant_id) & Key("window").gte(windows[-1])
                ),
            )
            for item in response.get("Items", []):
                total += int(item.get("count", 0))
        except Exception as e:
            logger.error("Failed to get hour count for %s: %s", tenant_id, e)
            # Fail open
            return 0
        return total

    def _increment(self, tenant_id: str, window: str, ttl: int) -> int:
        """Atomically increment the counter for a window. Returns new count."""
        try:
            response = self._table.update_item(
                Key={"tenant_id": tenant_id, "window": window},
                UpdateExpression="ADD #c :inc SET #t = :ttl",
                ExpressionAttributeNames={"#c": "count", "#t": "ttl"},
                ExpressionAttributeValues={":inc": 1, ":ttl": ttl},
                ReturnValues="UPDATED_NEW",
            )
            return int(response["Attributes"]["count"])
        except Exception as e:
            logger.error(
                "Failed to increment rate limit for %s/%s: %s", tenant_id, window, e
            )
            # Fail open — return 1 to indicate we "counted" it but don't block
            return 1

    def get_status(self, tenant_id: str, now: float | None = None) -> dict[str, Any]:
        """Get current rate limit status for a tenant (for diagnostics).

        Returns:
            Dict with current_window_count, hour_count, limits, etc.
        """
        if now is None:
            now = time.time()
        window = self._current_window(now)
        window_count = self._get_window_count(tenant_id, window)
        hour_count = self._get_hour_count(tenant_id, now)

        return {
            "tenant_id": tenant_id,
            "current_window": window,
            "window_count": window_count,
            "window_limit": self._limit_per_window,
            "hour_count": hour_count,
            "hour_limit": self._limit_per_hour,
            "window_remaining": max(0, self._limit_per_window - window_count),
            "hour_remaining": max(0, self._limit_per_hour - hour_count),
        }
