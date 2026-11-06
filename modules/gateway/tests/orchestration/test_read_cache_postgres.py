"""Real PostgreSQL write/rollback boundaries invalidate Redis projections."""

# ruff: noqa: F811 -- imported pytest fixtures are requested by the tests

import fakeredis.aioredis
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic.migration import MigrationContext
from alembic.operations import Operations
from src.orchestration import read_cache
from src.orchestration.routes import _flow_read_snapshot
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401
from tests.orchestration.decision_projection_fixture import migration


@pytest.fixture
async def database(pg_url, monkeypatch):
    engine = create_async_engine(to_async_url(pg_url))

    def create(connection):
        for table in ("orchestration_flows", "orchestration_nodes", "orchestration_decisions", "orchestration_accepted_plans"):
            connection.execute(text(f"CREATE TABLE {table} (id text PRIMARY KEY, org_id text NOT NULL, state text NOT NULL)"))
        connection.execute(text("CREATE TABLE unrelated_logs (id int PRIMARY KEY)"))
        connection.commit()
        context = MigrationContext.configure(connection)
        with Operations.context(context), context.begin_transaction():
            migration("143_flow_read_revisions").upgrade()

    async with engine.connect() as connection:
        await connection.run_sync(create)
    cache = read_cache.FlowReadCache(fakeredis.aioredis.FakeRedis(), "database")
    monkeypatch.setattr(read_cache, "flow_read_cache", lambda: cache)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await cache.close()
    await engine.dispose()


async def revision(db, org="one"):
    return await db.scalar(
        text("SELECT md5(string_agg(flow_id || revision, ',' ORDER BY flow_id)) FROM orchestration_read_revisions WHERE org_id=:org"), {"org": org}
    )


async def test_bulk_writes_updates_deletes_rollbacks_and_org_moves(database):
    async with database() as db:
        await db.execute(text("INSERT INTO orchestration_flows VALUES ('a','one','running'),('b','two','pending')"))
        await db.commit()
        first, other = await revision(db), await revision(db, "two")
        await db.execute(text("INSERT INTO orchestration_nodes VALUES ('1','one','ready'),('2','one','ready')"))
        await db.commit()
        second = await revision(db)
        assert second != first and await revision(db, "two") == other
        await db.execute(text("UPDATE orchestration_nodes SET state='passed'"))
        assert await revision(db) != second
        await db.rollback()
        assert await revision(db) == second
        await db.execute(text("UPDATE orchestration_nodes SET org_id='two' WHERE id='1'"))
        await db.commit()
        assert await revision(db) != second and await revision(db, "two") != other
        before_delete = await revision(db)
        await db.execute(text("DELETE FROM orchestration_nodes WHERE id='2'"))
        await db.commit()
        assert await revision(db) != before_delete
        before_logs = await revision(db)
        await db.execute(text("INSERT INTO unrelated_logs VALUES (1)"))
        await db.commit()
        assert await revision(db) == before_logs


async def test_committed_worker_write_bypasses_old_cache_but_old_snapshot_remains_consistent(database):
    async with database() as writer:
        await writer.execute(text("INSERT INTO orchestration_flows VALUES ('a','one','running')"))
        await writer.commit()

        async def read(snapshot):
            async def load():
                return {"state": await snapshot.scalar(text("SELECT state FROM orchestration_flows WHERE id='a'"))}

            return await read_cache.cached_projection(snapshot, org_id="one", operation="list", parameters=[], load=load)

        async with database() as request, _flow_read_snapshot(request) as old:
            assert await read(old) == {"state": "running"}
            await writer.execute(text("UPDATE orchestration_flows SET state='complete' WHERE id='a'"))
            await writer.commit()
            assert await read(old) == {"state": "running"}
        async with database() as request, _flow_read_snapshot(request) as fresh:
            assert await read(fresh) == {"state": "complete"}


async def test_uncommitted_mutation_read_cannot_publish_to_shared_cache(database):
    async with database() as db:
        await db.execute(text("INSERT INTO orchestration_flows VALUES ('a','one','private')"))

        async def load():
            return {"state": await db.scalar(text("SELECT state FROM orchestration_flows WHERE id='a'"))}

        assert await read_cache.cached_projection(db, org_id="one", operation="list", parameters=[], load=load) == {"state": "private"}
        await db.rollback()
        async with _flow_read_snapshot(db) as snapshot:
            assert await snapshot.scalar(text("SELECT count(*) FROM orchestration_flows")) == 0


async def test_request_auth_read_releases_pool_slot_before_structural_snapshot(pg_url):
    import asyncio

    engine = create_async_engine(to_async_url(pg_url), pool_size=1, max_overflow=0, pool_timeout=0.2)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as request:
            request.info["http_request_session"] = True
            assert await request.scalar(text("SELECT 1")) == 1
            async with asyncio.timeout(1), _flow_read_snapshot(request) as snapshot:
                assert await snapshot.scalar(text("SELECT 2")) == 2
                assert await snapshot.scalar(text("SHOW transaction_read_only")) == "on"
        assert engine.pool.checkedout() == 0
    finally:
        await engine.dispose()


async def test_cache_invalidation_does_not_serialize_independent_flow_writers(database):
    import asyncio

    async with database() as first, database() as second:
        await first.execute(text("INSERT INTO orchestration_flows VALUES ('a','one','running'),('b','one','running')"))
        await first.commit()
        await first.execute(text("UPDATE orchestration_flows SET state='passed' WHERE id='a'"))
        async with asyncio.timeout(1):
            await second.execute(text("UPDATE orchestration_flows SET state='failed' WHERE id='b'"))
            await second.commit()
        await first.commit()


async def test_truncate_and_migration_removal_preserve_read_correctness(database):
    async with database() as db:
        await db.execute(text("INSERT INTO orchestration_flows VALUES ('a','one','running')"))
        await db.commit()
        before = await revision(db)
        await db.execute(text("TRUNCATE orchestration_flows"))
        await db.commit()
        assert await revision(db) != before
        await db.execute(text("INSERT INTO orchestration_flows VALUES ('a','one','running')"))
        await db.commit()

    def remove(connection):
        context = MigrationContext.configure(connection)
        with Operations.context(context), context.begin_transaction():
            migration("143_flow_read_revisions").downgrade()

    async with database() as request:
        async with request.bind.connect() as connection:
            await connection.run_sync(remove)
        async with _flow_read_snapshot(request) as snapshot:

            async def load():
                return await snapshot.scalar(text("SELECT count(*) FROM orchestration_flows"))

            assert await read_cache.cached_projection(snapshot, org_id="one", operation="list", parameters=[], load=load) == 1
        async with request.bind.begin() as connection:
            # Metadata-only local setup does not install invalidation triggers.
            from src.orchestration.models import OrchestrationReadRevision

            await connection.run_sync(OrchestrationReadRevision.__table__.create)
        async with _flow_read_snapshot(request) as snapshot:
            cache = read_cache.flow_read_cache()
            keys_before = await cache.client.keys("*")
            assert await read_cache.cached_projection(snapshot, org_id="one", operation="list", parameters=[], load=load) == 1
            assert await cache.client.keys("*") == keys_before
