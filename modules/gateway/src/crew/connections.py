"""Environment-wide admission for Crew dashboard connections."""

import asyncio
import hashlib
import os
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from src.shared.config import get_settings
from src.shared.redis_client import create_redis_client

LEASE_SECONDS = 45
RENEW_SECONDS = 10
REDIS_TIMEOUT_SECONDS = 2
# The Kiro Crew dashboard is a code-split SPA: a browser fetches ~28 chunks at once
# over HTTP/2 on first load, on top of its SSE/websocket streams, so a cap of 20
# refused a normal first load (dev, 2026-10-20, #8223).
MAX_CONNECTIONS_PER_USER = 64

_ACQUIRE = """
local now = redis.call('TIME')
local current = tonumber(now[1]) + tonumber(now[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', current)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then return 0 end
redis.call('ZADD', KEYS[1], current + tonumber(ARGV[2]), ARGV[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]) * 2)
return 1
"""
_RENEW = """
local now = redis.call('TIME')
local current = tonumber(now[1]) + tonumber(now[2]) / 1000000
local expiry = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not expiry or tonumber(expiry) <= current then return 0 end
redis.call('ZADD', KEYS[1], current + tonumber(ARGV[2]), ARGV[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]) * 2)
return 1
"""
_RELEASE = "return redis.call('ZREM', KEYS[1], ARGV[1])"


class CrewConnectionUnavailableError(Exception):
    pass


class CrewConnectionLimitError(Exception):
    pass


@dataclass
class CrewConnectionLease:
    registry: "CrewConnectionRegistry"
    key: str
    token: str
    deadline: float
    released: bool = False

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic()) if not self.released else 0.0

    async def renew(self) -> bool:
        if self.remaining() <= 0:
            return False
        started = time.monotonic()
        try:
            result = await self.registry.evaluate(_RENEW, self.key, self.token, LEASE_SECONDS)
        except Exception:
            self.deadline = 0
            return False
        self.deadline = started + LEASE_SECONDS if result == 1 else 0
        return self.remaining() > 0

    async def release(self) -> None:
        if self.released:
            return
        self.released = True
        self.deadline = 0
        try:
            await self.registry.evaluate(_RELEASE, self.key, self.token)
        except Exception:
            pass
        finally:
            await self.registry.client.aclose()


class CrewConnectionRegistry:
    def __init__(self, client, namespace: str, limit: int):
        self.client = client
        self.namespace = hashlib.sha256(namespace.encode()).hexdigest()[:24]
        self.limit = limit

    async def evaluate(self, script: str, key: str, *args):
        async with asyncio.timeout(REDIS_TIMEOUT_SECONDS):
            return await self.client.eval(script, 1, key, *args)

    async def acquire(self, tenant_id: str, user_id: str) -> CrewConnectionLease:
        owner = hashlib.sha256(f"{tenant_id}\0{user_id}".encode()).hexdigest()
        key = f"crew-proxy:{self.namespace}:{owner}"
        token = uuid.uuid4().hex
        started = time.monotonic()
        try:
            result = await self.evaluate(_ACQUIRE, key, token, LEASE_SECONDS, self.limit)
        except Exception as exc:
            await self.client.aclose()
            raise CrewConnectionUnavailableError from exc
        if result != 1:
            await self.client.aclose()
            raise CrewConnectionLimitError
        lease = CrewConnectionLease(self, key, token, started + LEASE_SECONDS)
        if lease.remaining() <= 0:
            await lease.release()
            raise CrewConnectionUnavailableError
        return lease


def crew_connection_registry() -> CrewConnectionRegistry:
    settings = get_settings()
    if not settings.redis_url:
        raise CrewConnectionUnavailableError
    try:
        limit = min(int(os.environ.get("CREW_PROXY_MAX_CONN_PER_USER", "64")), MAX_CONNECTIONS_PER_USER)
        if limit < 1:
            raise ValueError("invalid Crew connection limit")
        endpoint = urlsplit(settings.redis_url)
        namespace = settings.redis_cache_name or f"{endpoint.hostname}:{endpoint.port or 6379}{endpoint.path or '/0'}"
        return CrewConnectionRegistry(
            create_redis_client(settings.redis_url, socket_timeout=REDIS_TIMEOUT_SECONDS, socket_connect_timeout=REDIS_TIMEOUT_SECONDS),
            namespace,
            limit,
        )
    except Exception as exc:
        raise CrewConnectionUnavailableError from exc
