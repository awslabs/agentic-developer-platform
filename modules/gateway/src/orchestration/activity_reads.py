"""Bounded, shared reads of authorized story display activity.

Callers still resolve permissions and current attempt/continuation bindings on
every request. These observations never authorize execution or budget spending.
"""

import asyncio
import hashlib
import json
import logging
import math
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import astuple, dataclass
from functools import lru_cache
from threading import Event

from sqlalchemy.engine import make_url

from src.activity.service import _get_table_name
from src.shared.config import get_settings
from src.shared.model_admission import runtime_budget_wait
from src.shared.redis_client import create_redis_client

from . import node_activity

logger = logging.getLogger(__name__)
CACHE_SECONDS = 5
REDIS_SECONDS = 0.25
MAX_ENTRY_BYTES = 128 * 1024
MAX_LOCAL_BYTES = 2 * 1024 * 1024
MAX_PENDING = 128
_PUBLISH = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('PSETEX', KEYS[2], ARGV[2], ARGV[3])
redis.call('DEL', KEYS[1])
return 1
"""
_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


class RedisUnavailableError(Exception):
    pass


@dataclass
class _Flight:
    task: asyncio.Task
    readers: int = 0


@dataclass(frozen=True)
class _Observation:
    execution: node_activity.StoryExecution
    expires_at: float | None = None


class ActivityReader:
    def __init__(self, client=None, namespace="", *, cache_enabled=True, clock=None, wall_clock=time.time):
        self.client = client
        self.namespace = namespace
        self.cache_enabled = cache_enabled
        self.clock = clock or (lambda: node_activity.time.monotonic())
        self.wall_clock = wall_clock
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="story-activity")
        self.slots = asyncio.Semaphore(32)
        self.redis_slots = asyncio.Semaphore(16)
        self.pending = {}
        self.local = OrderedDict()
        self.local_bytes = 0
        self.unavailable_until = 0.0
        self.closed = False

    def key(self, org_id, run_id, continuation):
        scope = [self.namespace, org_id, run_id, astuple(continuation) if continuation else None]
        identity = hashlib.sha256(json.dumps(scope, separators=(",", ":")).encode()).hexdigest()
        return f"story-display:v1:{{{identity}}}"

    def _decode(self, raw, key, run_id):
        if raw is None:
            return None
        try:
            if len(raw) > MAX_ENTRY_BYTES:
                raise ValueError
            value = json.loads(raw)
            observed = value["observed_at"]
            if value["key"] != key or type(observed) not in (int, float) or not math.isfinite(observed):
                raise ValueError
            if not 0 <= self.wall_clock() - observed < CACHE_SECONDS:
                return None
            result = node_activity.StoryExecution.model_validate(value["execution"], strict=True)
            if result.run_id != run_id or not result.history_complete or not result.runs:
                raise ValueError
            return _Observation(result, observed + CACHE_SECONDS)
        except (ValueError, TypeError, KeyError, UnicodeError):
            return None

    def _remember(self, key, raw):
        if key in self.local:
            self.local_bytes -= len(self.local.pop(key))
        self.local[key] = raw
        self.local_bytes += len(raw)
        while len(self.local) > 256 or self.local_bytes > MAX_LOCAL_BYTES:
            _, evicted = self.local.popitem(last=False)
            self.local_bytes -= len(evicted)

    async def _redis(self, operation):
        try:
            async with asyncio.timeout(REDIS_SECONDS), self.redis_slots:
                return await operation()
        except Exception as exc:
            raise RedisUnavailableError from exc

    def _degrade(self):
        if self.clock() >= self.unavailable_until:
            logger.warning("Shared story activity cache unavailable; using bounded local reads")
        self.unavailable_until = self.clock() + 1

    async def _source(self, org_id, run_id, continuation, deadline):
        remaining = deadline - self.clock()
        if remaining <= 0 or self.closed:
            return None
        try:
            async with asyncio.timeout(remaining):
                await self.slots.acquire()
        except TimeoutError:
            return None
        cancelled = Event()
        loop = asyncio.get_running_loop()
        try:
            future = self.executor.submit(node_activity._read_bounded_execution, org_id, run_id, deadline, cancelled, continuation)
        except BaseException:
            self.slots.release()
            raise

        def release(_done):
            # A cancelled asyncio waiter must not release capacity while its
            # blocking SDK call is still running. The actual thread owns it.
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(self.slots.release)

        future.add_done_callback(release)
        try:
            async with asyncio.timeout(max(0, deadline - self.clock())):
                result = await asyncio.shield(asyncio.wrap_future(future))
            if self.clock() >= deadline or result is None or result.run_id != run_id:
                return None
            return result
        except TimeoutError:
            return None
        finally:
            cancelled.set()
            future.cancel()

    async def _refresh(self, key, org_id, run_id, continuation, deadline):
        lease = key + ":refresh"
        token = uuid.uuid4().hex
        owns_lease = False
        use_redis = self.cache_enabled and self.client is not None and self.clock() >= self.unavailable_until
        try:
            if use_redis:
                try:
                    while self.clock() < deadline:
                        raw = await self._redis(lambda: self.client.get(key))
                        if (result := self._decode(raw, key, run_id)) is not None:
                            self._remember(key, raw)
                            return result
                        if await self._redis(lambda: self.client.set(lease, token, nx=True, px=6000)):
                            owns_lease = True
                            # A preceding owner may have published between our
                            # GET and lease acquisition.
                            raw = await self._redis(lambda: self.client.get(key))
                            if (result := self._decode(raw, key, run_id)) is not None:
                                self._remember(key, raw)
                                return result
                            break
                        await asyncio.sleep(min(0.05, max(0, deadline - self.clock())))
                    if not owns_lease:
                        return None
                except RedisUnavailableError:
                    self._degrade()
                    use_redis = False
            started = self.clock()
            observed_at = self.wall_clock()
            result = await self._source(org_id, run_id, continuation, deadline)
            if result is not None and self.cache_enabled and result.history_complete and result.runs:
                raw = json.dumps({"key": key, "observed_at": observed_at, "execution": result.model_dump(mode="json")}).encode()
                ttl = int((CACHE_SECONDS - (self.clock() - started)) * 1000)
                if len(raw) <= MAX_ENTRY_BYTES and ttl > 0:
                    publish = True
                    if owns_lease and use_redis:
                        try:
                            publish = bool(await self._redis(lambda: self.client.eval(_PUBLISH, 2, lease, key, token, ttl, raw)))
                        except RedisUnavailableError:
                            self._degrade()
                    if publish:
                        # Local entries have the same source-age limit as Redis.
                        self._remember(key, raw)
            return _Observation(result) if result is not None else None
        finally:
            if owns_lease:
                with suppress(RedisUnavailableError):
                    await self._redis(lambda: self.client.eval(_RELEASE, 1, lease, token))

    async def _shared(self, key, org_id, run_id, continuation, deadline):
        flight = self.pending.get(key)
        if flight is None:
            if len(self.pending) >= MAX_PENDING or self.closed:
                return None
            task = asyncio.create_task(self._refresh(key, org_id, run_id, continuation, deadline))
            flight = self.pending[key] = _Flight(task)

            def finished(done):
                if self.pending.get(key) is flight:
                    self.pending.pop(key, None)
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(finished)
        flight.readers += 1
        try:
            async with asyncio.timeout(max(0, deadline - self.clock())):
                result = await asyncio.shield(flight.task)
            return result
        except TimeoutError:
            return None
        finally:
            flight.readers -= 1
            if not flight.readers and not flight.task.done():
                if self.pending.get(key) is flight:
                    self.pending.pop(key, None)
                flight.task.cancel()
                await asyncio.gather(flight.task, return_exceptions=True)

    async def get_many(self, *, org_id, run_ids, continuations, deadline):
        run_ids = list(dict.fromkeys(run_ids))
        found = dict.fromkeys(run_ids)
        if self.closed or self.clock() >= deadline:
            return found
        keys = {run_id: self.key(org_id, run_id, continuations.get(run_id)) for run_id in run_ids}
        if self.cache_enabled:
            for run_id, key in keys.items():
                found[run_id] = self._decode(self.local.get(key), key, run_id)
            missing = [run_id for run_id in run_ids if found[run_id] is None]
            if missing and self.client is not None and self.clock() >= self.unavailable_until:
                try:
                    values = await self._redis(lambda: self.client.mget([keys[run_id] for run_id in missing]))
                    if not isinstance(values, list | tuple) or len(values) != len(missing):
                        raise RedisUnavailableError
                    for run_id, raw in zip(missing, values, strict=True):
                        found[run_id] = self._decode(raw, keys[run_id], run_id)
                        if found[run_id] is not None:
                            self._remember(keys[run_id], raw)
                except RedisUnavailableError:
                    self._degrade()
        missing = [run_id for run_id in run_ids if found[run_id] is None]
        tasks = [asyncio.create_task(self._shared(keys[r], org_id, r, continuations.get(r), deadline)) for r in missing]
        try:
            results = await asyncio.gather(*tasks)
            found.update(zip(missing, results, strict=True))
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        # Preserve provider reads completed before the deadline even when other
        # members time out. A cache hit, however, must still be fresh at return.
        found = {
            run_id: observation.execution.model_copy(deep=True)
            if observation is not None and (observation.expires_at is None or self.wall_clock() < observation.expires_at)
            else None
            for run_id, observation in found.items()
        }
        # Budget-wait diagnostics have their own expiry independent of this
        # short display cache. Never extend a worker's reported wait.
        for result in found.values():
            if result is None:
                continue
            for activity in [*result.runs, *([result.activity] if result.activity else [])]:
                if wait := activity.runtime_budget_wait:
                    activity.runtime_budget_wait = runtime_budget_wait(
                        status=wait.blocker, updated_at=wait.observed_at, run_status=activity.status, liveness=activity.liveness
                    )
        return found

    async def close(self):
        self.closed = True
        tasks = [flight.task for flight in self.pending.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.executor.shutdown(wait=False, cancel_futures=True)
        if self.client is not None:
            with suppress(Exception):
                async with asyncio.timeout(REDIS_SECONDS):
                    await self.client.aclose()


@lru_cache(maxsize=1)
def shared_activity_reader():
    settings = get_settings()
    url = make_url(settings.database_url)
    database = [settings.rds_host, settings.rds_port, settings.rds_dbname] if settings.rds_host else [url.host, url.port, url.database]
    namespace = json.dumps([database, settings.aws_region, _get_table_name()])
    client = None
    if settings.flow_activity_cache_enabled and settings.redis_url:
        try:
            client = create_redis_client(settings.redis_url, socket_timeout=REDIS_SECONDS, socket_connect_timeout=REDIS_SECONDS, max_connections=16)
        except Exception:
            logger.warning("Shared story activity cache configuration unavailable; using bounded local reads")
    return ActivityReader(client, namespace, cache_enabled=settings.flow_activity_cache_enabled)


async def close_activity_reader():
    if shared_activity_reader.cache_info().currsize:
        await shared_activity_reader().close()
        shared_activity_reader.cache_clear()
