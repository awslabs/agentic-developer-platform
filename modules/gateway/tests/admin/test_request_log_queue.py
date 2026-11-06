"""Request analytics survive replay without extending the response's SQL work."""

import asyncio
import os
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
import redis.asyncio as redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.admin import request_log_queue as queues
from src.admin.models import RequestLog
from src.admin.request_log_queue import GROUP, RequestLogQueue


def record():
    return {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC),
        "org_id": "workspace",
        "user_id": "user",
        "method": "GET",
        "path": "/orchestration/flows",
        "status_code": 200,
        "response_time_ms": 12,
    }


@pytest.fixture
async def queue(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'logs.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(RequestLog.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    target = os.environ.get("ADP_TEST_REDIS_URL")
    client = redis.from_url(target, decode_responses=True) if target else fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RequestLogQueue(client, f"test-{uuid.uuid4()}", factory=lambda: factory)
    yield queue
    await client.delete(queue.key)
    await queue.close()
    await engine.dispose()


async def count_rows(queue):
    async with queue.factory()() as db:
        return await db.scalar(select(func.count()).select_from(RequestLog))


async def test_enqueued_requests_do_not_acquire_sql_and_commit_as_one_batch(queue):
    assert all(await asyncio.gather(*(queue.enqueue(record()) for _ in range(40))))
    assert await count_rows(queue) == 0
    assert await queue.flush() == 40
    assert await count_rows(queue) == 40
    assert await queue.client.xlen(queue.key) == 0


async def test_full_queue_does_not_trim_existing_records(queue, monkeypatch):
    monkeypatch.setattr(queues, "CAPACITY", 2)
    assert await queue.enqueue(record())
    assert await queue.enqueue(record())
    assert not await queue.enqueue(record())
    assert await queue.flush() == 2
    assert await count_rows(queue) == 2


async def test_pending_crashed_consumer_is_reclaimed_without_duplicate_rows(queue, monkeypatch):
    row = record()
    assert await queue.enqueue(row)
    await queue._group()
    # Simulate a committed batch whose acknowledgement was lost with its pod.
    await queue.client.xreadgroup(GROUP, "crashed-worker", {queue.key: ">"}, count=1)
    async with queue.factory()() as db:
        db.add(RequestLog(**row))
        await db.commit()
    monkeypatch.setattr(queues, "RECLAIM_MS", 0)
    assert await queue.flush() == 1
    assert await count_rows(queue) == 1
    assert await queue.client.xlen(queue.key) == 0


async def test_failed_commit_retains_pending_batch_for_retry(queue):
    assert await queue.enqueue(record())
    original = queue.factory

    def unavailable():
        raise OSError("database offline")

    queue.factory = unavailable
    with pytest.raises(OSError):
        await queue.flush()
    assert (await queue.client.xpending(queue.key, GROUP))["pending"] == 1
    queue.factory = original
    assert await queue.flush() == 1
    assert await count_rows(queue) == 1


async def test_queue_failure_or_shutdown_requests_synchronous_fallback(queue, monkeypatch):
    monkeypatch.setattr(queue.client, "eval", AsyncMock(side_effect=OSError("Redis offline")))
    assert not await queue.enqueue(record())
    queue.closing = True
    assert not await queue.enqueue(record())


async def test_database_namespaces_do_not_consume_each_others_records(queue):
    other = RequestLogQueue(queue.client, "database-two", factory=queue.factory)
    assert await queue.enqueue(record())
    assert await other.flush() == 0
    assert await queue.flush() == 1
