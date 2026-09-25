"""Atomic environment-wide SSE admission using the existing authenticated Redis.

All three counters share a cluster hash slot. Expiring, unguessable leases bound
lost-pod cleanup; a response must stop writing when its lease cannot be renewed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from src.shared.config import get_settings
from src.shared.redis_client import create_redis_client
from src.tasks import errors
from src.tasks.limits import (
    SSE_MAX_STREAMS_PER_ENVIRONMENT,
    SSE_MAX_STREAMS_PER_PRINCIPAL,
    SSE_MAX_STREAMS_PER_TASK,
)

LEASE_SECONDS = 45.0
RENEW_SECONDS = 10.0
REDIS_TIMEOUT_SECONDS = 2.0

# Use Redis' clock for expiry, and check every scope before reserving any scope.
ACQUIRE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
for i = 1, 3 do
    redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', now)
end
for i = 1, 3 do
    if redis.call('ZCARD', KEYS[i]) >= tonumber(ARGV[i + 2]) then
        return i
    end
end
for i = 1, 3 do
    redis.call('ZADD', KEYS[i], now + tonumber(ARGV[2]), ARGV[1])
    redis.call('EXPIRE', KEYS[i], math.ceil(tonumber(ARGV[2]) * 2))
end
return 0
"""
RENEW = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
for i = 1, 3 do
    local expiry = redis.call('ZSCORE', KEYS[i], ARGV[1])
    if not expiry or tonumber(expiry) <= now then return 0 end
end
for i = 1, 3 do
    redis.call('ZADD', KEYS[i], now + tonumber(ARGV[2]), ARGV[1])
    redis.call('EXPIRE', KEYS[i], math.ceil(tonumber(ARGV[2]) * 2))
end
return 1
"""
RELEASE = """
for i = 1, 3 do redis.call('ZREM', KEYS[i], ARGV[1]) end
return 1
"""


def _digest(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


@dataclass
class StreamLease:
    registry: RedisStreamRegistry
    keys: tuple[str, str, str]
    token: str
    deadline: float
    released: bool = False

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic()) if not self.released else 0.0

    async def renew(self) -> bool:
        # Refuse local expiry too: an event-loop stall must not resurrect a lease.
        if self.remaining() <= 0:
            return False
        started = time.monotonic()
        try:
            result = await self.registry.evaluate(RENEW, self.keys, self.token, LEASE_SECONDS)
        except Exception:
            self.deadline = 0
            return False
        if result != 1:
            self.deadline = 0
            return False
        # Request start is conservative even when the Redis reply was delayed.
        self.deadline = started + LEASE_SECONDS
        return self.remaining() > 0

    async def release(self) -> None:
        if self.released:
            return
        self.released = True
        self.deadline = 0
        try:
            await self.registry.evaluate(RELEASE, self.keys, self.token)
        except Exception:
            # Expiry is the cleanup backstop during a Redis outage or pod loss.
            pass


class RedisStreamRegistry:
    def __init__(self, client, namespace: str):
        self.client = client
        self.prefix = "{task-sse:" + _digest(namespace)[:24] + "}"

    async def evaluate(self, script: str, keys: tuple[str, str, str], *args):
        async with asyncio.timeout(REDIS_TIMEOUT_SECONDS):
            return await self.client.eval(script, 3, *keys, *args)

    async def acquire_lease(self, *, tenant_id: str, principal_id: str, task_id: str) -> StreamLease:
        keys = (
            self.prefix + ":environment",
            self.prefix + ":principal:" + _digest(tenant_id, principal_id),
            self.prefix + ":task:" + _digest(tenant_id, task_id),
        )
        token = uuid.uuid4().hex
        started = time.monotonic()
        try:
            result = await self.evaluate(
                ACQUIRE,
                keys,
                token,
                LEASE_SECONDS,
                SSE_MAX_STREAMS_PER_ENVIRONMENT,
                SSE_MAX_STREAMS_PER_PRINCIPAL,
                SSE_MAX_STREAMS_PER_TASK,
            )
        except Exception as exc:
            raise errors.prerequisite_unavailable("Task API stream admission is unavailable.") from exc
        if result:
            scope = {1: "environment", 2: "principal", 3: "task"}.get(result, "environment")
            raise errors.rate_limited(f"This {scope} is at its concurrent Task API stream limit.")
        lease = StreamLease(self, keys, token, started + LEASE_SECONDS)
        if lease.remaining() <= 0:
            await lease.release()
            raise errors.prerequisite_unavailable("Task API stream admission expired.")
        return lease


def configured_registry() -> RedisStreamRegistry:
    settings = get_settings()
    if not settings.redis_url:
        raise errors.prerequisite_unavailable("Task API stream admission is unavailable.")
    try:
        endpoint = urlsplit(settings.redis_url)
        namespace = settings.redis_cache_name or f"{endpoint.hostname}:{endpoint.port or 6379}{endpoint.path or '/0'}"
        return RedisStreamRegistry(
            create_redis_client(settings.redis_url, socket_timeout=REDIS_TIMEOUT_SECONDS, socket_connect_timeout=REDIS_TIMEOUT_SECONDS),
            namespace,
        )
    except Exception as exc:
        raise errors.prerequisite_unavailable("Task API stream admission is unavailable.") from exc
