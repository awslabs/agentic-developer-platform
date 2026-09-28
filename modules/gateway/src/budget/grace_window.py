"""
Bounded fail-open grace window for budget enforcement (Issue #4075).

## Why this exists

Budget enforcement defaults to fail-CLOSED: if the ledger cannot be read, the
request is denied rather than admitted, because admitting it means uncapped
model spend for the duration of the fault.

A *bare* fail-closed flip is its own outage, though. The "no budget configured
⇒ allow" check happens after the session is acquired, so a DB blip never gets
far enough to learn whether a budget even exists — and budget rows are opt-in.
A naive flip therefore denies every request on every enforced path for every
tenant, capped or not, on any transient RDS IAM-token expiry. That is the same
class of self-inflicted incident as the broker-lockout / ``ALLOW_OPEN_SIGNUP``
outages.

So: allow for up to N seconds of *consecutive* ledger-read failures, alarm the
moment the window engages, and deny once it expires. Transient faults degrade
gracefully; sustained faults stop leaking spend.

## Why the state is shared, not in-process

The gateway runs 2 replicas × 4 uvicorn workers = **8 independent processes**,
each with its own heap. An in-process timer would therefore be 8 independent
windows — 8× the intended aggregate exposure — and every one of them would
reset on any rollout, crash-loop, or scale event, so a sustained outage
overlapping an HPA event would re-open the window indefinitely. That is exactly
the unbounded fail-open this module exists to remove, arriving by the back door.

One shared Redis key gives one window cluster-wide that survives restarts.

## The Redis-is-also-down case

Redis and RDS share a VPC and can fail together, so this must not depend on a
second store being healthy in order to fail *safely*. If Redis is unreachable
we fall back to a per-process window with a deliberately SHORT bound
(``_FALLBACK_GRACE_SECONDS``), accepting an approximate window over either
losing all grace or trusting an unbounded one.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import redis.asyncio as redis

from src.shared.logging import get_logger
from src.shared.redis_client import create_redis_client

logger = get_logger(__name__)

# Redis key holding the timestamp of the first failure in the current streak.
# TTL'd so a recovered system cannot leave a stale streak behind.
_WINDOW_KEY = "budget:failopen:window_start"

# Per-process fallback bound used only when Redis itself is unreachable.
# Deliberately short: with 8 processes each running its own fallback window,
# aggregate exposure is ~8× this value, so it must stay small.
_FALLBACK_GRACE_SECONDS = 5

# Atomically record a failure and report whether we are still inside the window.
#
# Returns {inside_window, elapsed_seconds}. SET NX is what makes this correct
# under concurrency: whichever process observes the first failure of a streak
# fixes the window start, and every other process — in every other pod — then
# measures against that same origin instead of starting its own window.
_REGISTER_FAILURE_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local grace = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])

local started = redis.call('SET', key, now, 'NX', 'EX', ttl)
if started then
    return {1, 0}
end

local window_start = tonumber(redis.call('GET', key))
if window_start == nil then
    -- Key expired between the SET NX and the GET; treat as a fresh streak.
    redis.call('SET', key, now, 'EX', ttl)
    return {1, 0}
end

local elapsed = now - window_start
if elapsed < grace then
    return {1, elapsed}
end

return {0, elapsed}
"""


class GraceWindow:
    """Tracks consecutive budget-check failures against a bounded time window.

    Args:
        grace_seconds: How long consecutive failures may be tolerated.
        redis_url: Redis connection URL. ``None`` forces the in-process
            fallback (used by tests and by single-process local dev).
        clock: Injectable monotonic-ish time source, so tests can assert the
            allow→deny transition without sleeping.
    """

    def __init__(
        self,
        grace_seconds: int,
        redis_url: str | None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._grace_seconds = grace_seconds
        self._redis_url = redis_url
        self._clock = clock or time.time
        self._client: redis.Redis | None = None
        self._script: redis.client.Script | None = None
        # In-process fallback state, used only when Redis is unavailable.
        self._local_window_start: float | None = None

    async def _get_client(self) -> redis.Redis:
        """Get or create the Redis client, registering the Lua script once."""
        if self._client is None:
            if not self._redis_url:
                raise RuntimeError("no redis_url configured")
            self._client = create_redis_client(self._redis_url, encoding="utf-8", decode_responses=True)
            self._script = self._client.register_script(_REGISTER_FAILURE_SCRIPT)
        return self._client

    async def register_failure(self) -> bool:
        """Record a ledger-read failure.

        Returns:
            ``True`` if the request may still be allowed under grace,
            ``False`` if the window has expired and the request must be denied.
        """
        if self._grace_seconds <= 0:
            # Grace disabled — deny immediately on any failure.
            return False

        try:
            client = await self._get_client()
            now = self._clock()
            # TTL slightly exceeds the window so the key cannot outlive its
            # own relevance, but is long enough that a streak isn't reset
            # mid-window by expiry.
            ttl = max(self._grace_seconds * 2, 10)
            result = await self._script(  # type: ignore[misc]
                keys=[_WINDOW_KEY],
                args=[now, self._grace_seconds, ttl],
                client=client,
            )
            return bool(int(result[0]))
        except Exception as e:
            # Redis is down too (shared-VPC correlated failure). Fall back to a
            # short per-process window rather than losing all grace.
            logger.warning(f"Grace window Redis unavailable, using short in-process fallback: {e}")
            return self._register_failure_local()

    def _register_failure_local(self) -> bool:
        """Per-process fallback window with a short bound."""
        now = self._clock()
        bound = min(self._grace_seconds, _FALLBACK_GRACE_SECONDS)

        if self._local_window_start is None:
            self._local_window_start = now
            return True

        return (now - self._local_window_start) < bound

    async def clear(self) -> None:
        """Reset the window after a successful ledger read.

        The window must track *consecutive* failures: without this, unrelated
        failures hours apart would accumulate into a single expired streak and
        the very first blip after a long healthy period would deny outright.
        """
        self._local_window_start = None

        if not self._redis_url:
            return

        try:
            client = await self._get_client()
            await client.delete(_WINDOW_KEY)
        except Exception as e:
            # A failed clear is not worth failing the request over — the key
            # TTL bounds the damage.
            logger.debug(f"Grace window clear failed (non-fatal): {e}")

    async def close(self) -> None:
        """Close the Redis client."""
        if self._client:
            await self._client.aclose()
            self._client = None
            self._script = None
