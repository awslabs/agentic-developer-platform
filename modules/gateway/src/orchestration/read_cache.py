"""Shared structural projections, fenced by the caller's committed DB snapshot.

The database revision changes in the same transaction as any orchestration
write. Authorization, costs, live provider history and mutation readers are not
cached here. Only explicitly marked, read-only display snapshots may use it.
"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from functools import lru_cache

from fastapi import HTTPException
from prometheus_client import Counter
from sqlalchemy import text
from sqlalchemy.engine import make_url

from src.shared.config import get_settings
from src.shared.redis_client import create_redis_client

from .cost_cache import SNAPSHOT_STARTED

logger = logging.getLogger(__name__)
CACHE_SECONDS = 5
MAX_BYTES = 1024 * 1024
WAIT_SECONDS = 2
EVENTS = Counter("bedrock_gateway_flow_projection_cache_total", "Versioned flow display cache outcomes", ["outcome"])
_RELEASE = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0"
_PUBLISH = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('PSETEX', KEYS[2], ARGV[2], ARGV[3])
redis.call('DEL', KEYS[1])
return 1
"""


class FlowReadCache:
    def __init__(self, client, namespace):
        self.client = client
        self.namespace = namespace
        self.slots = asyncio.Semaphore(4)
        self.unavailable_until = 0.0

    async def _redis(self, operation):
        async with asyncio.timeout(0.2):
            return await operation

    def _decode(self, raw, identity):
        if raw and len(raw) <= MAX_BYTES:
            try:
                data = json.loads(raw)
                if data["identity"] == identity and 0 <= time.time() - data["observed_at"] < CACHE_SECONDS:
                    return True, data["value"]
            except (ValueError, KeyError, TypeError):
                EVENTS.labels("invalid").inc()
        return False, None

    async def get(self, *, org_id, revision, operation, parameters, load):
        identity = json.dumps([self.namespace, org_id, revision, operation, parameters], sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(identity.encode()).hexdigest()
        key = f"flow-projection:v1:{{{digest}}}"
        lease = key + ":refresh"
        token = uuid.uuid4().hex
        owned = False
        use_redis = self.client is not None and time.monotonic() >= self.unavailable_until
        deadline = time.monotonic() + WAIT_SECONDS
        try:
            while use_redis and not owned:
                try:
                    raw = await self._redis(self.client.get(key))
                    found, value = self._decode(raw, identity)
                    if found:
                        EVENTS.labels("hit").inc()
                        return value
                    owned = bool(await self._redis(self.client.set(lease, token, nx=True, px=4000)))
                    if owned:
                        # Another owner may publish between our GET and SET NX.
                        found, value = self._decode(await self._redis(self.client.get(key)), identity)
                        if found:
                            EVENTS.labels("hit").inc()
                            return value
                except Exception:
                    self.unavailable_until = time.monotonic() + 5
                    use_redis = False
                    EVENTS.labels("redis_unavailable").inc()
                if use_redis and not owned:
                    if time.monotonic() >= deadline:
                        raise HTTPException(503, "Flow summary refresh busy", headers={"Retry-After": "1"})
                    await asyncio.sleep(0.025)
            try:
                async with asyncio.timeout(WAIT_SECONDS):
                    await self.slots.acquire()
            except TimeoutError as exc:
                raise HTTPException(503, "Flow summary refresh busy", headers={"Retry-After": "1"}) from exc
            try:
                async with asyncio.timeout(3):
                    observed = time.time()
                    value = await load()
                EVENTS.labels("load").inc()
            except TimeoutError as exc:
                raise HTTPException(503, "Flow summary refresh timed out", headers={"Retry-After": "1"}) from exc
            finally:
                self.slots.release()
            if owned:
                raw = json.dumps({"identity": identity, "observed_at": observed, "value": value}, separators=(",", ":"))
                remaining_ms = int((CACHE_SECONDS - (time.time() - observed)) * 1000)
                if len(raw.encode()) <= MAX_BYTES and remaining_ms > 0:
                    try:
                        await self._redis(self.client.eval(_PUBLISH, 2, lease, key, token, remaining_ms, raw))
                    except Exception:
                        EVENTS.labels("publish_unavailable").inc()
            return value
        finally:
            if owned:
                try:
                    await self._redis(self.client.eval(_RELEASE, 1, lease, token))
                except Exception:
                    pass  # Finite lease also releases a cancelled/disconnected owner.

    async def close(self):
        if self.client is not None:
            await self.client.aclose()


@lru_cache(maxsize=1)
def flow_read_cache():
    settings = get_settings()
    if not settings.flow_read_cache_enabled or not settings.redis_url:
        return None
    url = make_url(settings.database_url)
    namespace = f"{settings.rds_host}:{settings.rds_port}/{settings.rds_dbname}" if settings.rds_host else f"{url.host}:{url.port}/{url.database}"
    try:
        client = create_redis_client(settings.redis_url, socket_timeout=0.2, socket_connect_timeout=0.2, max_connections=32)
    except Exception:
        logger.warning("Flow projection cache configuration unavailable; using bounded reads")
        client = None
    return FlowReadCache(client, namespace)


async def close_flow_read_cache():
    if flow_read_cache.cache_info().currsize:
        cache = flow_read_cache()
        if cache is not None:
            await cache.close()
        flow_read_cache.cache_clear()


async def cached_projection(db, *, org_id, operation, parameters, load, flow_id=None):
    if db.bind.dialect.name != "postgresql" or SNAPSHOT_STARTED not in db.info:
        return await load()
    cache = flow_read_cache()
    if cache is None:
        return await load()
    revision_key = ("flow_read_revision", org_id, flow_id)
    if revision_key not in db.info:
        # Backend rollout may precede its additive migration. Keep ordinary reads
        # available until the revision table and its write triggers are installed.
        available = await db.scalar(
            text(
                "SELECT to_regclass('orchestration_read_revisions') IS NOT NULL AND EXISTS ("
                "SELECT 1 FROM pg_trigger WHERE tgrelid = to_regclass('orchestration_flows') "
                "AND tgname = 'adp_flow_revision_update' AND tgenabled IN ('O', 'A'))"
            )
        )
        if not available:
            return await load()
        db.info[revision_key] = await db.scalar(
            text(
                "SELECT md5(string_agg(flow_id || ':' || revision, ',' ORDER BY flow_id)) "
                "FROM orchestration_read_revisions WHERE org_id = :org "
                "AND (CAST(:flow AS text) IS NULL OR flow_id IN (:flow, '*'))"
            ),
            {"org": org_id, "flow": flow_id},
        )
    revision = db.info[revision_key]
    return await cache.get(org_id=org_id, revision=revision, operation=operation, parameters=parameters, load=load)
