"""Shared display summaries preserve money, isolation, expiry and fresh admission."""

import asyncio
import json
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from redis.exceptions import ConnectionError

from src.orchestration import cost_cache
from src.orchestration.cost import CostStatus, NodeCost, roll_up_flow_cost
from src.orchestration.cost_cache import DisplayCostCache, RefreshBusyError


@pytest.fixture
async def caches():
    server = fakeredis.FakeServer()
    instances = [DisplayCostCache(fakeredis.aioredis.FakeRedis(server=server), "database-a") for _ in range(12)]
    yield instances
    await asyncio.gather(*(cache.close() for cache in instances))


def measured(prefix="flow", amount="0.123456", *, tokens=17, calls=2):
    value = Decimal(amount)
    return NodeCost(prefix + "/epic/wave/story", CostStatus.NONE_INCURRED if value == 0 else CostStatus.KNOWN, value, tokens, calls)


async def read(cache, load, *, org="org-a", prefixes=None, snapshot_started=None):
    return await cache.get(
        org_id=org, prefixes=prefixes or ["flow"], load=load, snapshot_started=time.monotonic() if snapshot_started is None else snapshot_started
    )


async def test_a_hundred_readers_across_twelve_workers_share_one_batch_and_detail(caches):
    calls = []

    async def load(prefixes):
        calls.append(prefixes)
        await asyncio.sleep(0.03)
        return [measured(prefix) for prefix in prefixes]

    expected = [measured("first"), measured("second")]
    results = await asyncio.gather(*(read(caches[i % 12], load, prefixes=["second", "first", "first"]) for i in range(100)))
    assert all(result == expected for result in results)
    assert calls == [["first", "second"]]
    assert await read(caches[8], load, prefixes=["first"]) == [measured("first")]
    assert calls == [["first", "second"]]


async def test_organization_and_database_are_both_part_of_cache_identity(caches):
    cache = caches[0]
    one = AsyncMock(return_value=[measured(amount="1")])
    two = AsyncMock(return_value=[measured(amount="900")])
    assert await read(cache, one) == one.return_value
    assert await read(caches[1], two, org="org-b") == two.return_value
    other_database = DisplayCostCache(caches[2].client, "database-b")
    assert await read(other_database, two) == two.return_value
    assert await read(caches[3], two) == one.return_value
    assert two.await_count == 2


async def test_overlapping_and_literal_prefixes_do_not_double_count_or_merge_neighbors(caches):
    prefixes = ["flow", "flow/epic", "flow", "flow_", "flow%", "café", "flow\\name"]
    load = AsyncMock(return_value=[measured(p) for p in ["flow", "flow_", "flow%", "café", "flow\\name"]])
    result = await read(caches[0], load, prefixes=prefixes)
    assert set(result) == set(load.return_value)
    assert len(result) == len(load.return_value)
    assert "flow/epic" not in load.await_args.args[0]
    assert await read(caches[1], load, prefixes=prefixes) == result
    assert load.await_count == 1
    neighbor = AsyncMock(return_value=[])
    assert await read(caches[1], neighbor, prefixes=["flowX"]) == []
    neighbor.assert_awaited_once()


async def test_empty_measurement_is_cached_but_remains_unknown_and_expires(caches):
    now = [1000.0]
    cache = caches[0]
    cache.wall_clock = lambda: now[0]
    load = AsyncMock(return_value=[])
    empty = await read(cache, load)
    assert await read(cache, load) == []
    assert load.await_count == 1
    flow = SimpleNamespace(slug="flow")
    nodes = [SimpleNamespace(epic_ref="epic", wave_ref="wave", node_ref="story", kind="story")]
    aggregate = roll_up_flow_cost(flow=flow, nodes=nodes, measured_costs=empty)
    assert aggregate.status == CostStatus.UNKNOWN and aggregate.amount_usd is None
    load.return_value = [measured(amount="0", tokens=3, calls=1)]
    now[0] += 11
    result = await read(cache, load)
    aggregate = roll_up_flow_cost(flow=flow, nodes=nodes, measured_costs=result)
    assert aggregate.status == CostStatus.NONE_INCURRED and aggregate.amount_usd == Decimal(0)
    assert aggregate.call_count == 1 and aggregate.total_tokens == 3
    assert load.await_count == 2


async def test_expiry_reloads_late_usage_and_corrections_without_rounding(caches):
    cache = caches[0]
    now = [1000.0]
    cache.wall_clock = lambda: now[0]
    load = AsyncMock(return_value=[measured(amount="0.000001", calls=1)])
    assert (await read(cache, load))[0].amount_usd == Decimal("0.000001")
    load.return_value = [measured(amount="1.234567", calls=2)]
    assert (await read(cache, load))[0].call_count == 1
    now[0] += 11
    assert await read(cache, load) == load.return_value
    load.return_value = [measured(amount="0.000002", calls=2)]
    now[0] += 11
    assert await read(cache, load) == load.return_value


async def test_cache_never_freezes_node_unknown_or_partial_status(caches):
    load = AsyncMock(return_value=[measured()])
    flow = SimpleNamespace(slug="flow")
    known = SimpleNamespace(epic_ref="epic", wave_ref="wave", node_ref="story", kind="story")
    new = SimpleNamespace(epic_ref="epic", wave_ref="wave", node_ref="new", kind="story")
    first = roll_up_flow_cost(flow=flow, nodes=[known], measured_costs=await read(caches[0], load))
    second = roll_up_flow_cost(flow=flow, nodes=[known, new], measured_costs=await read(caches[1], load))
    assert first.partial is False
    assert second.partial and second.unknown_node_count == 1
    assert second.amount_usd == first.amount_usd
    assert second.nodes[1].status == CostStatus.UNKNOWN
    assert load.await_count == 1


async def test_ttl_includes_age_of_the_source_snapshot(caches):
    cache = caches[0]
    load = AsyncMock(return_value=[measured()])
    assert await read(cache, load, snapshot_started=time.monotonic() - 6) == load.return_value
    _, keys = cache.keys("org-a", ["flow"])
    assert 0 < await cache.client.pttl(keys[0]) <= 4000
    await cache.client.delete(keys[0])
    assert await read(cache, load, snapshot_started=time.monotonic() - 11) == load.return_value
    assert await cache.client.get(keys[0]) is None


async def test_mixed_batch_reloads_hits_that_expire_during_another_flow_refresh(caches):
    cache = caches[0]
    now = [1000.0]
    cache.wall_clock = lambda: now[0]
    await read(cache, AsyncMock(return_value=[measured(amount="1")]))
    now[0] += 9
    batches = []

    async def load(prefixes):
        batches.append(prefixes)
        now[0] += 2
        return [measured(p, amount="2") for p in prefixes]

    result = await read(cache, load, prefixes=["flow", "other"])
    assert batches == [["other"], ["flow"]]
    assert result == [measured("flow", amount="2"), measured("other", amount="2")]


@pytest.mark.parametrize("corruption", ["org", "prefix", "foreign_address", "version", "nan", "calls", "duplicate", "invalid_json", "future"])
async def test_corrupt_or_wrong_scope_values_are_never_served(caches, corruption):
    cache = caches[0]
    load = AsyncMock(return_value=[measured()])
    await read(cache, load)
    _, keys = cache.keys("org-a", ["flow"])
    value = json.loads(await cache.client.get(keys[0]))
    if corruption == "org":
        value["org_id"] = "other"
    elif corruption == "prefix":
        value["prefix"] = "other"
    elif corruption == "foreign_address":
        value["rows"][0][0] = "flow-other/story"
    elif corruption == "version":
        value["version"] = 99
    elif corruption == "nan":
        value["rows"][0][1] = "NaN"
    elif corruption == "calls":
        value["rows"][0][3] = 0
    elif corruption == "duplicate":
        value["rows"].append(value["rows"][0])
    elif corruption == "future":
        value["observed_at"] += 100
    await cache.client.set(keys[0], "invalid" if corruption == "invalid_json" else json.dumps(value))
    assert await read(cache, load) == load.return_value
    assert load.await_count == 2


async def test_expired_owner_cannot_overwrite_or_unlock_its_successor(caches):
    cache = caches[0]
    lease, keys = cache.keys("org-a", ["flow"])
    newer = [measured(amount="99")]

    async def stale_load(_prefixes):
        await cache.client.set(lease, "successor", px=5000)
        payload = {"version": 1, "org_id": "org-a", "prefix": "flow", "observed_at": time.time(), "rows": [[newer[0].address, "99", 17, 2]]}
        await cache.client.set(keys[0], json.dumps(payload), px=10000)
        return [measured(amount="1")]

    assert await read(cache, stale_load) == [measured(amount="1")]
    assert await cache.client.get(lease) == b"successor"
    forbidden = AsyncMock(side_effect=AssertionError("fresh value must win"))
    assert await read(caches[1], forbidden) == newer


async def test_cancelled_owner_releases_lease_and_follower_can_retry(caches):
    started = asyncio.Event()

    async def blocked(_prefixes):
        started.set()
        await asyncio.Event().wait()

    owner = asyncio.create_task(read(caches[0], blocked))
    await started.wait()
    following = asyncio.create_task(read(caches[1], AsyncMock(return_value=[measured()])))
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert await following == [measured()]
    lease, _ = caches[0].keys("org-a", ["flow"])
    assert await caches[0].client.get(lease) is None


async def test_abandoned_lease_has_a_bounded_wait_and_does_not_fan_out_to_sql(caches, monkeypatch):
    monkeypatch.setattr(cost_cache, "WAIT_SECONDS", 0.02)
    cache = caches[0]
    lease, _ = cache.keys("org-a", ["flow"])
    await cache.client.set(lease, "other-worker", px=5000)
    load = AsyncMock(return_value=[measured()])
    with pytest.raises(RefreshBusyError):
        await read(cache, load)
    load.assert_not_awaited()


async def test_redis_outage_shares_and_bounds_fallback_queries(caches, monkeypatch):
    cache = caches[0]
    monkeypatch.setattr(cache.client, "mget", AsyncMock(side_effect=ConnectionError("offline")))
    calls = 0
    active = 0
    peak = 0

    async def load(prefixes):
        nonlocal calls, active, peak
        calls += 1
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return [measured(p) for p in prefixes]

    results = await asyncio.gather(*(read(cache, load) for _ in range(25)))
    assert results == [[measured()]] * 25
    assert calls == 1
    await asyncio.gather(*(read(cache, load, prefixes=[f"other-{i}"]) for i in range(5)))
    assert peak == 1
    assert cache.pending == {}


async def test_write_failure_does_not_repeat_successful_sql(caches, monkeypatch):
    cache = caches[0]
    monkeypatch.setattr(cache.client, "eval", AsyncMock(side_effect=ConnectionError("write failed")))
    load = AsyncMock(return_value=[measured()])
    assert await read(cache, load) == load.return_value
    load.assert_awaited_once()


async def test_cancelled_fallback_owner_wakes_followers_and_releases_capacity(caches):
    cache = caches[0]
    cache.unavailable_until = time.monotonic() + 60
    started = asyncio.Event()

    async def blocked(_prefixes):
        started.set()
        await asyncio.Event().wait()

    owner = asyncio.create_task(read(cache, blocked))
    await started.wait()
    follower = asyncio.create_task(read(cache, blocked))
    await asyncio.sleep(0)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    with pytest.raises(RefreshBusyError):
        await follower
    assert cache.pending == {}
    assert await read(cache, AsyncMock(return_value=[measured()])) == [measured()]


async def test_refresh_timeout_releases_lease_and_never_caches_empty_costs(caches, monkeypatch):
    monkeypatch.setattr(cost_cache, "LOAD_SECONDS", 0.02)
    cache = caches[0]

    async def blocked(_prefixes):
        await asyncio.sleep(1)
        return []

    with pytest.raises(RefreshBusyError):
        await read(cache, blocked)
    lease, keys = cache.keys("org-a", ["flow"])
    assert await cache.client.mget([lease, *keys]) == [None, None]
    assert await read(cache, AsyncMock(return_value=[measured()])) == [measured()]


async def test_large_result_is_not_truncated_to_fit_cache(caches, monkeypatch):
    monkeypatch.setattr(cost_cache, "MAX_ENTRY_BYTES", 10)
    load = AsyncMock(return_value=[measured()])
    assert await read(caches[0], load) == [measured()]
    _, keys = caches[0].keys("org-a", ["flow"])
    assert await caches[0].client.get(keys[0]) is None
