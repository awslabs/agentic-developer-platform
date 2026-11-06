"""Migration-backed independent-session ownership of one native effect start."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from l5_services.devops.knowledge_work import current_context, resolve_admitted
from l5_services.models.knowledge_effect import KnowledgeEffect
from sqlalchemy import event, inspect, select

from tests.migrations.conftest_postgres import pg_server as pg_server
from tests.migrations.conftest_postgres import pg_url as pg_url
from tests.products.test_l5_devops_knowledge_admission import knowledge_fixture as knowledge_fixture
from tests.products.test_l5_knowledge_effects import knowledge_worker as knowledge_worker
from tests.products.test_l5_knowledge_work import claimed_knowledge as claimed_knowledge
from tests.products.test_l5_owner_delegation import consent_fixture as consent_fixture
from tests.products.test_l5_owner_delegation import l5_owner as l5_owner
from tests.products.test_l5_setup_continuations_postgres import test_engine as test_engine

__all__ = ["pg_server", "pg_url", "knowledge_fixture", "knowledge_worker", "claimed_knowledge", "consent_fixture", "l5_owner", "test_engine"]


@pytest.fixture(autouse=True)
async def require_migration_before_fixture_metadata(test_engine):
    # consent_fixture also creates metadata for SQLite tests. Assert the real
    # Gateway upgrade installed the tables before that helper can mask a gap.
    async with test_engine.connect() as connection:
        assert await connection.run_sync(lambda sync: inspect(sync).has_table("l5_devops_knowledge_effects"))
        assert await connection.run_sync(lambda sync: inspect(sync).has_table("l5_devops_knowledge_ownership"))


async def test_independent_sessions_allow_only_one_started_native_effect(db_session, test_engine, knowledge_worker):
    row, worker, native = knowledge_worker
    async with worker.sessions() as session:
        context = await current_context(session, row.intent, row.claim)
    assets = await resolve_admitted(context, row.claim, worker.sources, AsyncMock())
    asset = assets[0]
    plan = native_plan = worker.driver.prepare("native-space", asset, row.intent.operation_id, None)
    await worker.effects.plan(row.intent, row.claim, asset.source_ref, native_plan)
    first_lock = asyncio.Event()
    second_lock = asyncio.Event()
    query_count = 0

    def observed(_connection, _cursor, statement, _parameters, _context, _many):
        nonlocal query_count
        if "l5_service_configurations" in statement and "FOR UPDATE" in statement:
            query_count += 1
            if query_count >= 2:
                second_lock.set()

    original_read = worker.effects._read
    reads = 0

    async def held_read(*args):
        nonlocal reads
        result = await original_read(*args)
        reads += 1
        if reads == 1:
            first_lock.set()
            await asyncio.wait_for(second_lock.wait(), timeout=10)
        return result

    worker.effects._read = held_read
    event.listen(test_engine.sync_engine, "before_cursor_execute", observed)
    try:
        one = asyncio.create_task(worker.effects.start(row.intent, row.claim, asset.source_ref))
        await asyncio.wait_for(first_lock.wait(), timeout=10)
        two = asyncio.create_task(worker.effects.start(row.intent, row.claim, asset.source_ref))
        result = await asyncio.wait_for(asyncio.gather(one, two), timeout=20)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", observed)
    assert sorted(result) == [False, True] and query_count >= 2
    events = (await db_session.scalars(select(KnowledgeEffect).order_by(KnowledgeEffect.sequence))).all()
    assert [item.phase for item in events] == ["planned", "started"]
    assert events[0].payload == plan
    assert "create" not in native.calls and "update" not in native.calls
