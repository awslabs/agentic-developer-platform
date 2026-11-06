"""Cross-worker structural reads are versioned, isolated and bounded."""

import asyncio
import json
import time
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from src.orchestration.read_cache import FlowReadCache


@pytest.fixture
async def caches():
    server = fakeredis.FakeServer()
    instances = [FlowReadCache(fakeredis.aioredis.FakeRedis(server=server), "database-one") for _ in range(12)]
    yield instances
    await asyncio.gather(*(cache.close() for cache in instances))


async def read(cache, load, *, org="org-one", revision="revision-one", parameters=None):
    return await cache.get(org_id=org, revision=revision, operation="list", parameters=parameters or [], load=load)


async def test_hundred_readers_across_twelve_replicas_share_one_projection(caches):
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.03)
        return {"flows": [{"id": "one", "status": "running"}]}

    results = await asyncio.gather(*(read(caches[i % 12], load) for i in range(100)))
    assert all(result == results[0] for result in results)
    assert calls == 1


async def test_changed_revision_tenant_filters_and_database_cannot_reuse_data(caches):
    original = AsyncMock(return_value={"flows": ["old"]})
    changed = AsyncMock(return_value={"flows": ["new"]})
    assert await read(caches[0], original) == original.return_value
    for kwargs in ({"revision": "revision-two"}, {"org": "org-two"}, {"parameters": ["stalled"]}):
        assert await read(caches[1], changed, **kwargs) == changed.return_value
    other = FlowReadCache(caches[2].client, "another-database")
    assert await read(other, changed) == changed.return_value
    assert changed.await_count == 4
    assert await read(caches[3], changed) == original.return_value


async def test_failed_or_cancelled_load_does_not_publish_or_strand_lease(caches):
    broken = AsyncMock(side_effect=OSError("SQL unavailable"))
    with pytest.raises(OSError):
        await read(caches[0], broken)
    entered = asyncio.Event()

    async def blocked():
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(read(caches[1], blocked))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    healthy = AsyncMock(return_value={"flows": []})
    assert await read(caches[2], healthy) == {"flows": []}
    healthy.assert_awaited_once()


async def test_redis_outage_bounds_concurrent_database_work():
    cache = FlowReadCache(None, "database")
    active = peak = 0

    async def load():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {}

    await asyncio.gather(*(read(cache, load, parameters=[i]) for i in range(40)))
    assert peak == 4


async def test_expired_or_corrupt_entry_is_recomputed(caches):
    load = AsyncMock(return_value={"flows": ["original"]})
    await read(caches[0], load)
    keys = await caches[0].client.keys("flow-projection:*")
    assert len(keys) == 1
    key = keys[0]
    stale = json.loads(await caches[0].client.get(key))
    stale["observed_at"] = time.time() - 6
    await caches[0].client.set(key, json.dumps(stale))
    load.return_value = {"flows": ["fresh"]}
    assert await read(caches[1], load) == load.return_value
    await caches[0].client.set(key, "broken JSON")
    assert await read(caches[2], load) == load.return_value
    assert load.await_count == 3


async def test_lost_lease_cannot_publish_over_new_owner(caches):
    async def superseded():
        leases = await caches[0].client.keys("*:refresh")
        assert len(leases) == 1
        await caches[0].client.set(leases[0], "replacement-owner")
        return {"flows": ["old"]}

    assert await read(caches[0], superseded) == {"flows": ["old"]}
    keys = await caches[0].client.keys("flow-projection:*")
    assert len(keys) == 1 and keys[0].endswith(b":refresh")
    assert await caches[0].client.get(keys[0]) == b"replacement-owner"


async def test_lease_miss_race_reuses_just_published_entry(caches, monkeypatch):
    load = AsyncMock(return_value={"flows": ["shared"]})
    original_set = caches[0].client.set

    async def publish_before_lease(*args, **kwargs):
        await read(caches[1], load)
        return await original_set(*args, **kwargs)

    monkeypatch.setattr(caches[0].client, "set", publish_before_lease)
    assert await read(caches[0], load) == load.return_value
    load.assert_awaited_once()
