"""Migration-backed abandonment serializes with actual worker claims."""

import asyncio
from unittest.mock import AsyncMock

from l5_services.workers.bindings import execute_setup_work
from sqlalchemy import event

from src import l5_knowledge_abandon
from tests.products.test_l5_knowledge_abandon import abandon, effect
from tests.products.test_l5_knowledge_abandon import logout_boundary as logout_boundary
from tests.products.test_l5_knowledge_effects_postgres import claimed_knowledge as claimed_knowledge
from tests.products.test_l5_knowledge_effects_postgres import consent_fixture as consent_fixture
from tests.products.test_l5_knowledge_effects_postgres import knowledge_fixture as knowledge_fixture
from tests.products.test_l5_knowledge_effects_postgres import knowledge_worker as knowledge_worker
from tests.products.test_l5_knowledge_effects_postgres import l5_owner as l5_owner
from tests.products.test_l5_knowledge_effects_postgres import pg_server as pg_server
from tests.products.test_l5_knowledge_effects_postgres import pg_url as pg_url
from tests.products.test_l5_knowledge_effects_postgres import require_migration_before_fixture_metadata as require_migration_before_fixture_metadata
from tests.products.test_l5_knowledge_effects_postgres import test_engine as test_engine

__all__ = [
    "logout_boundary",
    "claimed_knowledge",
    "consent_fixture",
    "knowledge_fixture",
    "knowledge_worker",
    "l5_owner",
    "pg_server",
    "pg_url",
    "require_migration_before_fixture_metadata",
    "test_engine",
]


async def test_abandon_lock_wins_and_queued_worker_cannot_claim(test_client, db_session, test_engine, knowledge_worker, monkeypatch):
    row, worker, native = await effect(db_session, knowledge_worker, "started")
    locked, attempted = asyncio.Event(), asyncio.Event()
    actual = l5_knowledge_abandon.retained_effects

    async def hold(*args):
        result = await actual(*args)
        locked.set()
        await asyncio.wait_for(attempted.wait(), 10)
        return result

    def observed(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE l5_service_setup_work"):
            attempted.set()

    monkeypatch.setattr(l5_knowledge_abandon, "retained_effects", hold)
    event.listen(test_engine.sync_engine, "before_cursor_execute", observed)
    callback = AsyncMock()
    calls = list(native.calls)
    try:
        closing = asyncio.create_task(abandon(test_client, row))
        await asyncio.wait_for(locked.wait(), 10)
        execution = asyncio.create_task(
            execute_setup_work(
                worker.sessions,
                {"schema_version": "1.0", "work_id": row.work.work_id, "generation": row.work.generation},
                callback,
                knowledge_execute=callback,
            )
        )
        result, executed = await asyncio.wait_for(asyncio.gather(closing, execution), 20)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", observed)
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "abandoned" and not executed
    callback.assert_not_awaited()
    assert native.calls == calls


async def test_claimed_worker_prevents_abandonment_and_uncertain_recovery_never_rewrites(test_client, db_session, knowledge_worker):
    row, worker, native = await effect(db_session, knowledge_worker, "unknown")
    claimed, release = asyncio.Event(), asyncio.Event()
    native.lose_ack = False

    async def execute(intent, claim):
        claimed.set()
        await asyncio.wait_for(release.wait(), 10)
        return await worker.execute(intent, claim)

    execution = asyncio.create_task(
        execute_setup_work(
            worker.sessions,
            {"schema_version": "1.0", "work_id": row.work.work_id, "generation": row.work.generation},
            AsyncMock(),
            knowledge_execute=execute,
        )
    )
    try:
        await asyncio.wait_for(claimed.wait(), 10)
        result = await abandon(test_client, row)
        assert result.status_code == 409 and result.json()["detail"]["code"] == "knowledge_work_already_running"
        assert native.calls.count("create") == 1
    finally:
        release.set()
    assert await asyncio.wait_for(execution, 15)
    assert native.calls.count("create") == 1 and "update" not in native.calls
    await db_session.refresh(row.operation)
    assert row.operation.state == "waiting"
