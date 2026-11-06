"""Migrated PostgreSQL installations can drain retained request analytics."""

import uuid

import fakeredis.aioredis
import pytest
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic.migration import MigrationContext
from alembic.operations import Operations
from src.admin.models import RequestLog
from src.admin.request_log_queue import GROUP, RequestLogQueue
from tests.admin.test_request_log_queue import record
from tests.migrations.conftest_postgres import to_async_url, upgrade
from tests.orchestration.decision_projection_fixture import migration


async def test_missing_table_repair_drains_retained_batch_and_replays_idempotently(pg_url):
    upgrade(pg_url, "143_flow_read_revisions")
    engine = create_async_engine(to_async_url(pg_url))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    queue = RequestLogQueue(fakeredis.aioredis.FakeRedis(decode_responses=True), str(uuid.uuid4()), factory=lambda: factory)
    rows = [record() for _ in range(20)]
    try:
        for row in rows:
            assert await queue.enqueue(row)
        with pytest.raises(ProgrammingError, match="request_logs.*does not exist"):
            await queue.flush()
        assert (await queue.client.xpending(queue.key, GROUP))["pending"] == 20
        upgrade(pg_url, "144_request_logs")
        assert await queue.flush() == 20
        assert await queue.enqueue(rows[0])
        assert await queue.flush() == 1
        async with factory() as db:
            assert await db.scalar(select(func.count()).select_from(RequestLog)) == 20
            assert set((await db.scalars(select(RequestLog.id))).all()) == {row["id"] for row in rows}
        assert await queue.client.xlen(queue.key) == 0
        assert (await queue.client.xpending(queue.key, GROUP))["pending"] == 0
        async with engine.connect() as connection:
            columns = await connection.run_sync(lambda db: inspect(db).get_columns("request_logs"))
            indexes = await connection.run_sync(lambda db: inspect(db).get_indexes("request_logs"))
        assert {c["name"] for c in columns} == set(RequestLog.__table__.columns.keys())
        assert {i["name"] for i in indexes} == {i.name for i in RequestLog.__table__.indexes}
    finally:
        await queue.close()
        await engine.dispose()


def test_adopts_existing_table_and_preserves_records_on_rollback(pg_url):
    upgrade(pg_url, "143_flow_read_revisions")
    engine = create_engine(pg_url)
    row = record()
    try:
        with engine.begin() as connection:
            RequestLog.__table__.create(connection)
            connection.execute(RequestLog.__table__.insert().values(row))
        upgrade(pg_url, "144_request_logs")
        with engine.begin() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                migration("144_request_logs").downgrade()
                migration("144_request_logs").upgrade()
            assert connection.scalar(select(RequestLog.id)) == row["id"]
    finally:
        engine.dispose()
