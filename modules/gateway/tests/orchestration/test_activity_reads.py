"""Shared display activity stays bounded, tenant scoped, fresh and cancellable."""

import asyncio
import json
import time
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from src.orchestration import activity_reads, node_activity
from src.orchestration.activity_reads import ActivityReader
from src.orchestration.continuation_activity import ContinuationActivity


@pytest.fixture
async def readers():
    server = fakeredis.FakeServer()
    instances = [ActivityReader(fakeredis.aioredis.FakeRedis(server=server), "database-a") for _ in range(12)]
    yield instances
    await asyncio.gather(*(reader.close() for reader in instances))


def execution(run="run-1"):
    return node_activity.StoryExecution(
        run_id=run,
        history_complete=True,
        runs=[
            node_activity.StoryRun(invocation_id=run, persona="developer", status="complete", liveness="exited", invoked_at="2026-10-26T00:00:00Z")
        ],
    )


async def read(reader, *, org="org-a", runs=None, continuations=None, seconds=5):
    return await reader.get_many(org_id=org, run_ids=runs or ["run-1"], continuations=continuations or {}, deadline=time.monotonic() + seconds)


async def test_hundred_viewers_across_twelve_workers_share_one_provider_read(readers, monkeypatch):
    calls = []

    def source(org, run, deadline, cancelled, continuation):
        calls.append((org, run))
        time.sleep(0.05)
        return execution(run)

    monkeypatch.setattr(node_activity, "_read_bounded_execution", source)
    results = await asyncio.gather(*(read(readers[i % 12]) for i in range(100)))
    assert all(result == {"run-1": execution()} for result in results)
    assert calls == [("org-a", "run-1")]
    results[0]["run-1"].runs.clear()
    assert (await read(readers[0]))["run-1"] == execution()
    assert len(calls) == 1


async def test_scope_run_and_full_continuation_identity_isolate_observations(readers, monkeypatch):
    calls = []

    def source(org, run, deadline, cancelled, continuation):
        calls.append((org, run, continuation))
        return execution(run)

    monkeypatch.setattr(node_activity, "_read_bounded_execution", source)
    await read(readers[0])
    await read(readers[1], org="org-b")
    await read(readers[2], runs=["run-2"])
    readers[3].namespace = "database-b"
    await read(readers[3])
    first = ContinuationActivity(invocation_id="child", correlation_id="chain", persona="reviewer", cycle=1)
    await read(readers[4], continuations={"run-1": first})
    second = ContinuationActivity(invocation_id="child", correlation_id="chain", persona="reviewer", cycle=2)
    await read(readers[5], continuations={"run-1": second})
    await read(readers[6], continuations={"run-1": first})
    assert len(calls) == 6


async def test_cache_expires_from_provider_start_and_does_not_store_partial_or_empty(readers, monkeypatch):
    now = [1000.0]
    for reader in readers:
        reader.wall_clock = lambda: now[0]
    calls = []
    result = execution()

    def source(*args):
        calls.append(args)
        return result.model_copy(deep=True) if result else None

    monkeypatch.setattr(node_activity, "_read_bounded_execution", source)
    await read(readers[0])
    now[0] += 4.9
    await read(readers[1])
    assert len(calls) == 1
    now[0] += 0.1
    result = execution()
    result.runs[0].status = "failed"
    assert (await read(readers[1]))["run-1"].runs[0].status == "failed"
    assert len(calls) == 2
    for partial in [None, node_activity.StoryExecution(run_id="run-1"), execution()]:
        if partial:
            partial.history_complete = False
        result = partial
        now[0] += 6
        before = len(calls)
        await read(readers[0])
        await read(readers[0])
        assert len(calls) == before + 2


async def test_cached_hit_expiring_while_another_member_loads_is_not_returned(readers, monkeypatch):
    now = [1000.0]
    readers[0].wall_clock = lambda: now[0]

    def source(org, run, *args):
        if run == "slow":
            now[0] += 2
        return execution(run)

    monkeypatch.setattr(node_activity, "_read_bounded_execution", source)
    await read(readers[0])
    now[0] += 4
    result = await read(readers[0], runs=["run-1", "slow"])
    assert result["run-1"] is None
    assert result["slow"] == execution("slow")


@pytest.mark.parametrize("raw", [b"bad-json", b"[]", b"{}", b"x" * (128 * 1024 + 1)])
async def test_corrupt_cache_degrades_to_source(readers, monkeypatch, raw):
    reader = readers[0]
    await reader.client.set(reader.key("org-a", "run-1", None), raw)
    monkeypatch.setattr(node_activity, "_read_bounded_execution", lambda *args: execution())
    assert (await read(reader))["run-1"] == execution()


async def test_wrong_run_and_key_cannot_be_replayed(readers, monkeypatch):
    reader = readers[0]
    monkeypatch.setattr(node_activity, "_read_bounded_execution", lambda org, run, *args: execution(run))
    await read(reader)
    key = reader.key("org-a", "run-1", None)
    raw = json.loads(await reader.client.get(key))
    for field, value in [("key", "foreign-key"), ("execution", execution("foreign-run").model_dump())]:
        corrupt = {**raw, field: value}
        reader.local.clear()
        reader.local_bytes = 0
        await reader.client.set(key, json.dumps(corrupt))
        assert (await read(reader))["run-1"] == execution()


async def test_redis_outage_and_malformed_mget_use_bounded_local_fallback(readers, monkeypatch):
    monkeypatch.setattr(node_activity, "_read_bounded_execution", lambda *args: execution())
    for client in [AsyncMock(mget=AsyncMock(side_effect=ConnectionError)), AsyncMock(mget=AsyncMock(return_value=[]))]:
        reader = readers[0]
        reader.client = client
        reader.local.clear()
        reader.local_bytes = 0
        reader.unavailable_until = 0
        assert (await read(reader))["run-1"] == execution()
        assert reader.unavailable_until > time.monotonic()


async def test_cancelling_one_subscriber_does_not_cancel_shared_read(readers, monkeypatch):
    started, release = Event(), Event()
    calls = []

    def source(org, run, deadline, cancelled, continuation):
        calls.append(cancelled)
        started.set()
        assert release.wait(3)
        assert not cancelled.is_set()
        return execution(run)

    monkeypatch.setattr(node_activity, "_read_bounded_execution", source)
    first = asyncio.create_task(read(readers[0]))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        second = asyncio.create_task(read(readers[0]))
        for _ in range(100):
            if next(iter(readers[0].pending.values())).readers == 2:
                break
            await asyncio.sleep(0.001)
        assert next(iter(readers[0].pending.values())).readers == 2
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    finally:
        release.set()
    assert (await second)["run-1"] == execution()
    assert len(calls) == 1


async def test_worker_limit_is_per_process_and_cancelled_queue_never_starts(readers, monkeypatch):
    reader = readers[0]
    reader.cache_enabled = False
    started, release = Event(), Event()
    lock = Lock()
    calls = []

    def source(org, run, deadline, cancelled, continuation):
        with lock:
            calls.append(cancelled)
            if len(calls) == 4:
                started.set()
        assert release.wait(3)
        return execution(run)

    monkeypatch.setattr(node_activity, "_read_bounded_execution", source)
    tasks = [asyncio.create_task(read(reader, runs=[f"run-{i}-{j}" for j in range(20)])) for i in range(10)]
    try:
        assert await asyncio.to_thread(started.wait, 1)
        await asyncio.sleep(0.02)
        assert len(reader.pending) <= 128
        assert len(calls) == 4
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert all(cancelled.is_set() for cancelled in calls)
        # Running threads still own four slots; queued work has been cancelled.
        await asyncio.sleep(0.01)
        assert reader.slots._value == 28
    finally:
        release.set()
    await asyncio.sleep(0.02)
    assert len(calls) == 4
    assert reader.slots._value == 32


async def test_lease_recheck_and_fenced_publish(readers, monkeypatch):
    reader = readers[0]
    key = reader.key("org-a", "run-1", None)
    payload = json.dumps({"key": key, "observed_at": time.time(), "execution": execution().model_dump()})
    client = AsyncMock()
    client.mget.return_value = [None]
    client.get.side_effect = [None, payload]
    client.set.return_value = True
    reader.client = client

    def source(*args):
        pytest.fail("Lease winner must recheck a preceding publication")

    monkeypatch.setattr(node_activity, "_read_bounded_execution", source)
    assert (await read(reader))["run-1"] == execution()
    reader.local.clear()
    reader.local_bytes = 0
    client.get.side_effect = [None, None]
    client.eval.return_value = 0
    monkeypatch.setattr(node_activity, "_read_bounded_execution", lambda *args: execution())
    assert (await read(reader))["run-1"] == execution()
    assert not reader.local  # An expired owner cannot overwrite newer observations.


async def test_shutdown_signals_readers_and_rejects_further_work(readers, monkeypatch):
    started, release = Event(), Event()
    events = []

    def source(org, run, deadline, cancelled, continuation):
        events.append(cancelled)
        started.set()
        release.wait(2)
        return execution(run)

    reader = readers[0]
    monkeypatch.setattr(node_activity, "_read_bounded_execution", source)
    task = asyncio.create_task(read(reader))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        await reader.close()
        assert events[0].is_set()
        assert await read(reader) == {"run-1": None}
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_slow_source_does_not_receive_a_new_five_second_cache_lifetime(readers, monkeypatch):
    reader = readers[0]
    elapsed = [0.0]
    # Use one synthetic time domain: subtracting a real fractional monotonic
    # origin can turn four seconds into 4.000000000000455 and a 1000ms TTL into
    # 999ms, which this fakeredis version truncates to a whole-second expiry.
    reader.clock = lambda: elapsed[0]
    reader.wall_clock = lambda: 1000 + elapsed[0]
    monkeypatch.setattr(fakeredis._basefakesocket, "time", SimpleNamespace(time=reader.wall_clock))

    def source(*args):
        elapsed[0] = 4
        return execution()

    monkeypatch.setattr(node_activity, "_read_bounded_execution", source)
    assert (await reader.get_many(org_id="org-a", run_ids=["run-1"], continuations={}, deadline=5.0))["run-1"] == execution()
    key = reader.key("org-a", "run-1", None)
    assert await reader.client.pttl(key) == 1000
    elapsed[0] = 4.5
    assert await reader.client.pttl(key) == 500
    elapsed[0] = 5
    assert reader._decode(reader.local[key], key, "run-1") is None
    elapsed[0] = 5.001
    assert await reader.client.pttl(key) == -2


async def test_local_storage_and_serialized_entries_are_bounded(readers, monkeypatch):
    reader = readers[0]
    large = execution()
    large.runs[0].invocation_id = "x" * activity_reads.MAX_ENTRY_BYTES
    calls = []

    def source(*args):
        calls.append(args)
        return large

    monkeypatch.setattr(node_activity, "_read_bounded_execution", source)
    await read(reader)
    await read(reader)
    assert len(calls) == 2 and not reader.local
    for index in range(300):
        reader._remember(str(index), b"x" * 10_000)
    assert len(reader.local) <= 256
    assert reader.local_bytes <= activity_reads.MAX_LOCAL_BYTES
