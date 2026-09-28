"""
In-memory rate limit backend.

This module provides an in-memory implementation of the rate limit backend
using token buckets for RPM/TPM limits and counters for concurrent requests.
"""

import asyncio
import threading
import time
from collections import defaultdict
from datetime import datetime

from ..backend import RateLimitBackend
from ..models import EntityType, LimitType, RateLimitState
from ..token_bucket import TokenBucket


class InMemoryBackend(RateLimitBackend):
    """
    In-memory rate limit backend using token buckets.

    This implementation is suitable for single-instance deployments
    or development/testing environments. For distributed deployments,
    use the RedisBackend instead.

    Features:
    - Thread-safe token bucket implementation
    - Automatic TTL-based cleanup of expired entries
    - Concurrent request tracking
    """

    def __init__(self, cleanup_interval: float = 60.0, entry_ttl: float = 3600.0) -> None:
        """
        Initialize the in-memory backend.

        Args:
            cleanup_interval: Seconds between cleanup runs (default: 60)
            entry_ttl: Time-to-live for inactive entries in seconds (default: 3600)
        """
        self._buckets: dict[str, TokenBucket] = {}
        self._bucket_metadata: dict[str, float] = {}  # key -> last access time
        self._concurrent_counts: dict[str, int] = defaultdict(int)
        self._concurrent_limits: dict[str, int] = {}
        self._lock = threading.Lock()
        self._cleanup_interval = cleanup_interval
        self._entry_ttl = entry_ttl
        self._cleanup_task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """Start the cleanup background task."""
        if not self._running:
            self._running = True
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self) -> None:
        """Background task to cleanup expired entries."""
        while self._running:
            try:
                await asyncio.sleep(self._cleanup_interval)
                self._cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception:
                # Log and continue
                pass

    def _cleanup_expired(self) -> None:
        """Remove expired entries based on TTL."""
        now = time.monotonic()
        with self._lock:
            expired_keys = [key for key, last_access in self._bucket_metadata.items() if now - last_access > self._entry_ttl]
            for key in expired_keys:
                self._buckets.pop(key, None)
                self._bucket_metadata.pop(key, None)
                self._concurrent_counts.pop(key, None)
                self._concurrent_limits.pop(key, None)

    def _get_bucket_key(self, key: str, limit_type: LimitType) -> str:
        """Generate a unique bucket key."""
        return f"{key}:{limit_type.value}"

    def _get_or_create_bucket(self, key: str, limit_type: LimitType, max_tokens: int, refill_rate: float) -> TokenBucket:
        """Get existing bucket or create a new one."""
        bucket_key = self._get_bucket_key(key, limit_type)

        with self._lock:
            if bucket_key not in self._buckets:
                self._buckets[bucket_key] = TokenBucket(capacity=max_tokens, refill_rate=refill_rate)
            self._bucket_metadata[bucket_key] = time.monotonic()
            return self._buckets[bucket_key]

    async def check_limit(
        self,
        key: str,
        limit_type: LimitType,
        max_tokens: int,
        refill_rate: float,
    ) -> tuple[bool, int, int]:
        """Check if a rate limit would be exceeded without consuming."""
        bucket = self._get_or_create_bucket(key, limit_type, max_tokens, refill_rate)
        allowed, remaining, wait_time = bucket.check(1.0)
        return allowed, int(remaining), int(wait_time) if wait_time != float("inf") else 60

    async def consume(
        self,
        key: str,
        limit_type: LimitType,
        max_tokens: int,
        refill_rate: float,
        tokens_to_consume: int = 1,
    ) -> tuple[bool, int, int]:
        """Attempt to consume tokens from a rate limit bucket."""
        bucket = self._get_or_create_bucket(key, limit_type, max_tokens, refill_rate)
        success, remaining, wait_time = bucket.try_consume(float(tokens_to_consume))
        return success, int(remaining), int(wait_time) if wait_time != float("inf") else 60

    async def get_remaining(
        self,
        key: str,
        limit_type: LimitType,
        max_tokens: int,
        refill_rate: float,
    ) -> int:
        """Get the number of remaining tokens."""
        bucket = self._get_or_create_bucket(key, limit_type, max_tokens, refill_rate)
        return int(bucket.get_remaining())

    async def get_state(
        self,
        key: str,
        limit_type: LimitType,
    ) -> RateLimitState | None:
        """Get the current state of a rate limit bucket."""
        bucket_key = self._get_bucket_key(key, limit_type)

        with self._lock:
            bucket = self._buckets.get(bucket_key)
            if not bucket:
                return None

            state = bucket.get_state()
            # Parse entity info from key (format: entity_type:entity_id:org_id)
            parts = key.split(":")
            entity_type_str = parts[0] if len(parts) > 0 else "user"
            entity_id = parts[1] if len(parts) > 1 else key

            try:
                entity_type = EntityType(entity_type_str)
            except ValueError:
                entity_type = EntityType.USER

            return RateLimitState(
                entity_type=entity_type,
                entity_id=entity_id,
                limit_type=limit_type,
                tokens=state.tokens,
                max_tokens=bucket.capacity,
                refill_rate=bucket.refill_rate,
                last_refill=datetime.utcnow(),
            )

    async def reset(self, key: str, limit_type: LimitType) -> None:
        """Reset a rate limit bucket to full capacity."""
        bucket_key = self._get_bucket_key(key, limit_type)

        with self._lock:
            bucket = self._buckets.get(bucket_key)
            if bucket:
                bucket.reset()

    async def increment_concurrent(self, key: str) -> tuple[bool, int, int]:
        """Increment concurrent request counter."""
        with self._lock:
            limit = self._concurrent_limits.get(key, 100)  # Default limit
            current = self._concurrent_counts[key]

            if current >= limit:
                return False, current, limit

            self._concurrent_counts[key] = current + 1
            return True, current + 1, limit

    async def decrement_concurrent(self, key: str) -> int:
        """Decrement concurrent request counter."""
        with self._lock:
            current = self._concurrent_counts.get(key, 0)
            if current > 0:
                self._concurrent_counts[key] = current - 1
                return current - 1
            return 0

    async def get_concurrent_count(self, key: str) -> int:
        """Get current concurrent request count."""
        with self._lock:
            return self._concurrent_counts.get(key, 0)

    async def set_concurrent_limit(self, key: str, limit: int) -> None:
        """Set the concurrent request limit for a key."""
        with self._lock:
            self._concurrent_limits[key] = limit

    async def close(self) -> None:
        """Close and cleanup backend resources."""
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

        with self._lock:
            self._buckets.clear()
            self._bucket_metadata.clear()
            self._concurrent_counts.clear()
            self._concurrent_limits.clear()

    def clear(self) -> None:
        """Clear all stored data (useful for testing)."""
        with self._lock:
            self._buckets.clear()
            self._bucket_metadata.clear()
            self._concurrent_counts.clear()
            self._concurrent_limits.clear()
