"""Short-lived shared cost summaries for authorized flow display reads only.

The raw cost reader and policy admission never use this cache. Cache entries are
per database, tenant and literal flow prefix, so list and detail views share the
same measured addresses. Current nodes, permissions and policy are not cached.
"""

import asyncio
import hashlib
import json
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache

from fastapi import HTTPException
from prometheus_client import Counter, Histogram
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.config import get_settings
from src.shared.redis_client import create_redis_client

from .cost import CostStatus, NodeCost, get_cost_by_address_prefixes, roll_up_flow_cost
from .models import OrchestrationFlow, OrchestrationNode

logger = logging.getLogger(__name__)
SNAPSHOT_STARTED = "flow_display_snapshot_started"
CACHE_SECONDS = 10
REDIS_SECONDS = 0.25
WAIT_SECONDS = 2
# A full page's indexed ledger read can take over four seconds on a live tenant.
# Keep it bounded, with the lease longer than the read and freshness unchanged.
LOAD_SECONDS = 6
LEASE_MS = 8000
MAX_ENTRY_BYTES = 256 * 1024
EVENTS = Counter("bedrock_gateway_flow_cost_cache_total", "Display cost cache outcomes", ["outcome"])
REFRESH_SECONDS = Histogram("bedrock_gateway_flow_cost_cache_refresh_seconds", "Display cost ledger refresh duration")

_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
_PUBLISH = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
for i = 2, #KEYS do
    redis.call('PSETEX', KEYS[i], ARGV[2], ARGV[i + 1])
end
redis.call('DEL', KEYS[1])
return 1
"""

Loader = Callable[[list[str]], Awaitable[list[NodeCost]]]


class RefreshBusyError(Exception):
    """A bounded display refresh cannot finish; never invent a zero cost."""


class DisplayCostRefreshUnavailable(HTTPException):
    """An optional display cost could not be refreshed within its bounded read."""

    def __init__(self):
        super().__init__(503, "Flow cost summary refresh is busy; retry shortly", headers={"Retry-After": "1"})


class CacheUnavailableError(Exception):
    pass


@dataclass
class _Entry:
    costs: list[NodeCost]
    # A direct SQL result belongs to this request's snapshot. Only reused cache
    # entries need expiry checked again while other prefixes are being loaded.
    expires_at: float = math.inf


def _within(address: str, prefix: str) -> bool:
    return address == prefix or address.startswith(prefix + "/")


def _prefixes(prefixes: list[str]) -> list[str]:
    result = []
    for prefix in sorted(set(prefixes)):
        if not any(_within(prefix, parent) for parent in result):
            result.append(prefix)
    return result


class DisplayCostCache:
    def __init__(self, client, namespace: str, *, clock=time.monotonic, wall_clock=time.time):
        self.client = client
        self.namespace = namespace
        self.clock = clock
        self.wall_clock = wall_clock
        self.unavailable_until = 0.0
        self.pending: dict[tuple, asyncio.Future] = {}
        self.fallback_slot = asyncio.Semaphore(1)

    def keys(self, org_id: str, prefixes: list[str]) -> tuple[str, list[str]]:
        tenant = hashlib.sha256(json.dumps([self.namespace, org_id]).encode()).hexdigest()
        # One hash slot per tenant makes fenced multi-prefix publication atomic
        # on both standalone Redis and Redis Cluster.
        base = f"flow-display-cost:v1:{{{tenant}}}"
        return base + ":refresh", [base + ":" + hashlib.sha256(p.encode()).hexdigest() for p in prefixes]

    async def _redis(self, operation):
        try:
            async with asyncio.timeout(REDIS_SECONDS):
                return await operation
        except Exception as exc:
            raise CacheUnavailableError from exc

    def _decode(self, raw, org_id: str, prefix: str) -> _Entry | None:
        if raw is None:
            return None
        try:
            if len(raw) > MAX_ENTRY_BYTES:
                raise ValueError
            data = json.loads(raw)
            if data["version"] != 1 or data["org_id"] != org_id or data["prefix"] != prefix:
                raise ValueError
            observed = data["observed_at"]
            if type(observed) not in (float, int) or not math.isfinite(observed) or not 0 <= self.wall_clock() - observed < CACHE_SECONDS:
                raise ValueError
            if not isinstance(data["rows"], list):
                raise ValueError
            costs = []
            seen = set()
            for address, amount, tokens, calls in data["rows"]:
                if not isinstance(address, str) or not _within(address, prefix) or address in seen:
                    raise ValueError
                if not isinstance(amount, str) or type(tokens) is not int or tokens < 0 or type(calls) is not int or calls < 1:
                    raise ValueError
                total = Decimal(amount)
                if not total.is_finite():
                    raise ValueError
                costs.append(NodeCost(address, CostStatus.NONE_INCURRED if total == 0 else CostStatus.KNOWN, total, tokens, calls))
                seen.add(address)
            return _Entry(costs, observed + CACHE_SECONDS)
        except (ValueError, TypeError, KeyError, InvalidOperation, UnicodeError):
            EVENTS.labels("invalid").inc()
            return None

    async def _read(self, org_id: str, prefixes: list[str]) -> dict[str, _Entry]:
        _, keys = self.keys(org_id, prefixes)
        raw = await self._redis(self.client.mget(keys))
        if not isinstance(raw, list | tuple) or len(raw) != len(prefixes):
            raise CacheUnavailableError
        return {prefix: costs for prefix, value in zip(prefixes, raw, strict=True) if (costs := self._decode(value, org_id, prefix)) is not None}

    async def _load(self, load: Loader, prefixes: list[str]) -> list[NodeCost]:
        started = self.clock()
        try:
            async with asyncio.timeout(LOAD_SECONDS):
                return await load(prefixes)
        except TimeoutError as exc:
            raise RefreshBusyError from exc
        finally:
            REFRESH_SECONDS.observe(max(0, self.clock() - started))

    async def _fallback(self, org_id: str, prefixes: list[str], load: Loader) -> list[NodeCost]:
        # If Redis fails, same-process waiters still share one read. Bound work
        # across distinct requests instead of releasing every cache miss to SQL.
        key = (org_id, tuple(prefixes))
        if future := self.pending.get(key):
            try:
                async with asyncio.timeout(LOAD_SECONDS):
                    return list(await asyncio.shield(future))
            except TimeoutError as exc:
                raise RefreshBusyError from exc
        future = asyncio.get_running_loop().create_future()
        # Consume exceptions even when an owner has no followers.
        future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        self.pending[key] = future
        try:
            async with asyncio.timeout(LOAD_SECONDS):
                async with self.fallback_slot:
                    result = await self._load(load, prefixes)
            future.set_result(result)
            return result
        except BaseException as exc:
            future.set_exception(RefreshBusyError() if isinstance(exc, asyncio.CancelledError) else exc)
            if isinstance(exc, TimeoutError):
                raise RefreshBusyError from exc
            raise
        finally:
            self.pending.pop(key, None)

    async def get(self, *, org_id: str, prefixes: list[str], load: Loader, snapshot_started: float) -> list[NodeCost]:
        prefixes = _prefixes(prefixes)
        if not prefixes:
            return []
        if self.client is None or self.clock() < self.unavailable_until:
            return await self._fallback(org_id, prefixes, load)
        found = {}
        try:
            found = await self._read(org_id, prefixes)
            EVENTS.labels("hit" if len(found) == len(prefixes) else "miss").inc()
            deadline = self.clock() + WAIT_SECONDS
            while True:
                # Mixed hit/miss batches must not retain an entry that expired
                # while another flow's ledger query or refresh lease was pending.
                found = {p: entry for p, entry in found.items() if self.wall_clock() < entry.expires_at}
                missing = [p for p in prefixes if p not in found]
                if not missing:
                    break
                if self.clock() >= deadline:
                    EVENTS.labels("busy").inc()
                    raise RefreshBusyError
                lease, _ = self.keys(org_id, missing)
                token = uuid.uuid4().hex
                if await self._redis(self.client.set(lease, token, nx=True, px=LEASE_MS)):
                    try:
                        # A publisher may have won between MGET and SET NX.
                        found.update(await self._read(org_id, missing))
                        missing = [p for p in missing if p not in found]
                        if missing:
                            EVENTS.labels("refresh").inc()
                            costs = await self._load(load, missing)
                            age = max(0, self.clock() - snapshot_started)
                            observed_at = self.wall_clock() - age
                            payloads = []
                            publish_prefixes = []
                            for prefix in missing:
                                found[prefix] = _Entry([cost for cost in costs if _within(cost.address, prefix)])
                                payload = json.dumps(
                                    {
                                        "version": 1,
                                        "org_id": org_id,
                                        "prefix": prefix,
                                        "observed_at": observed_at,
                                        "rows": [[c.address, str(c.amount_usd), c.total_tokens, c.call_count] for c in found[prefix].costs],
                                    },
                                    separators=(",", ":"),
                                )
                                if len(payload.encode()) <= MAX_ENTRY_BYTES:
                                    publish_prefixes.append(prefix)
                                    payloads.append(payload)
                                else:
                                    EVENTS.labels("oversize").inc()
                            ttl_ms = int((CACHE_SECONDS - max(0, self.clock() - snapshot_started)) * 1000)
                            if ttl_ms > 0 and publish_prefixes:
                                _, keys = self.keys(org_id, publish_prefixes)
                                try:
                                    await self._redis(self.client.eval(_PUBLISH, 1 + len(keys), lease, *keys, token, ttl_ms, *payloads))
                                except CacheUnavailableError:
                                    # The SQL read succeeded: a failed cache write
                                    # must not repeat it or lose the correct result.
                                    self._degrade()
                    finally:
                        with suppress(CacheUnavailableError):
                            await self._redis(self.client.eval(_RELEASE, 1, lease, token))
                else:
                    EVENTS.labels("wait").inc()
                    await asyncio.sleep(0.05)
                    found.update(await self._read(org_id, missing))
            return [cost for prefix in prefixes for cost in found[prefix].costs]
        except CacheUnavailableError:
            self._degrade()
            # Read one complete batch so a partial hit cannot expire while the
            # fallback is queued. The successful-publication path above handles
            # write failures separately and never repeats a successful SQL read.
            return await self._fallback(org_id, prefixes, load)

    def _degrade(self):
        if self.clock() >= self.unavailable_until:
            logger.warning("Shared flow display cost cache unavailable; using bounded ledger reads")
        self.unavailable_until = self.clock() + 1
        EVENTS.labels("unavailable").inc()

    async def close(self):
        if self.client is not None:
            with suppress(Exception):
                async with asyncio.timeout(REDIS_SECONDS):
                    await self.client.aclose()


@lru_cache(maxsize=1)
def display_cost_cache() -> DisplayCostCache | None:
    settings = get_settings()
    if not settings.flow_cost_cache_enabled or not settings.redis_url:
        return None
    url = make_url(settings.database_url)
    namespace = f"{settings.rds_host}:{settings.rds_port}/{settings.rds_dbname}" if settings.rds_host else f"{url.host}:{url.port}/{url.database}"
    try:
        client = create_redis_client(settings.redis_url, socket_timeout=REDIS_SECONDS, socket_connect_timeout=REDIS_SECONDS, max_connections=16)
    except Exception:
        logger.warning("Shared flow display cost cache configuration unavailable; using bounded ledger reads")
        client = None
    return DisplayCostCache(client, namespace)


async def close_display_cost_cache():
    if display_cost_cache.cache_info().currsize:
        cache = display_cost_cache()
        if cache is not None:
            await cache.close()
        display_cost_cache.cache_clear()


async def get_display_costs(db: AsyncSession, *, org_id: str, address_prefixes: list[str]) -> list[NodeCost]:
    async def load(prefixes):
        return await get_cost_by_address_prefixes(db, org_id=org_id, address_prefixes=prefixes)

    # Only the dedicated committed, read-only display snapshot may publish.
    # SQLite/test sessions and arbitrary caller transactions remain uncached.
    started = db.info.get(SNAPSHOT_STARTED)
    if started is None or not address_prefixes:
        return await load(address_prefixes)
    cache = display_cost_cache()
    if cache is None:
        return await load(address_prefixes)
    try:
        return await cache.get(org_id=org_id, prefixes=address_prefixes, load=load, snapshot_started=started)
    except RefreshBusyError as exc:
        raise DisplayCostRefreshUnavailable() from exc


async def get_display_flow_cost(db: AsyncSession, *, org_id: str, flow: OrchestrationFlow, nodes: list[OrchestrationNode]):
    costs = await get_display_costs(db, org_id=org_id, address_prefixes=[flow.slug])
    return roll_up_flow_cost(flow=flow, nodes=nodes, measured_costs=costs)
