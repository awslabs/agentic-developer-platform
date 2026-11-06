"""Independent PostgreSQL sessions serialize owner cancel with worker claims."""

import asyncio
from unittest.mock import AsyncMock

from l5_services.workers.bindings import execute_setup_work
from sqlalchemy import event

from src import l5_knowledge_cancel
from tests.products.test_l5_knowledge_cancel import cancel
from tests.products.test_l5_knowledge_effects_postgres import (
    claimed_knowledge as claimed_knowledge,
)
from tests.products.test_l5_knowledge_effects_postgres import (
    consent_fixture as consent_fixture,
)
from tests.products.test_l5_knowledge_effects_postgres import (
    knowledge_fixture as knowledge_fixture,
)
from tests.products.test_l5_knowledge_effects_postgres import (
    knowledge_worker as knowledge_worker,
)
from tests.products.test_l5_knowledge_effects_postgres import (
    l5_owner as l5_owner,
)
from tests.products.test_l5_knowledge_effects_postgres import (
    pg_server as pg_server,
)
from tests.products.test_l5_knowledge_effects_postgres import (
    pg_url as pg_url,
)
from tests.products.test_l5_knowledge_effects_postgres import (
    require_migration_before_fixture_metadata as require_migration_before_fixture_metadata,
)
from tests.products.test_l5_knowledge_effects_postgres import (
    test_engine as test_engine,
)
from tests.products.test_l5_knowledge_resume import blocked

__all__ = [
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


async def runnable(db, row):
    await blocked(db, row)
    row.work.blocked_at = None
    row.operation.state = "accepted"
    await db.commit()
    return {"schema_version": "1.0", "work_id": row.work.work_id, "generation": row.work.generation}


async def test_cancel_lock_wins_and_waiting_worker_cannot_claim(test_client, db_session, test_engine, knowledge_worker, monkeypatch):
    row, worker, native = knowledge_worker
    envelope = await runnable(db_session, row)
    locked, worker_update = asyncio.Event(), asyncio.Event()
    actual = l5_knowledge_cancel.unstarted_effects

    async def hold(*args):
        result = await actual(*args)
        locked.set()
        await asyncio.wait_for(worker_update.wait(), timeout=10)
        return result

    def observed(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE l5_service_setup_work"):
            worker_update.set()

    monkeypatch.setattr(l5_knowledge_cancel, "unstarted_effects", hold)
    event.listen(test_engine.sync_engine, "before_cursor_execute", observed)
    callback = AsyncMock()
    try:
        cancellation = asyncio.create_task(cancel(test_client, row))
        await asyncio.wait_for(locked.wait(), timeout=10)
        execution = asyncio.create_task(execute_setup_work(worker.sessions, envelope, callback, knowledge_execute=callback))
        response, executed = await asyncio.wait_for(asyncio.gather(cancellation, execution), timeout=20)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", observed)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "cancelled" and not executed and worker_update.is_set()
    callback.assert_not_awaited()
    assert not native.calls


async def test_worker_claim_wins_before_effect_and_cancel_cannot_cross_it(test_client, db_session, knowledge_worker):
    row, worker, native = knowledge_worker
    envelope = await runnable(db_session, row)
    claimed, release = asyncio.Event(), asyncio.Event()

    async def execute(intent, claim):
        claimed.set()
        await asyncio.wait_for(release.wait(), timeout=10)
        return await worker.execute(intent, claim)

    execution = asyncio.create_task(execute_setup_work(worker.sessions, envelope, AsyncMock(), knowledge_execute=execute))
    try:
        await asyncio.wait_for(claimed.wait(), timeout=10)
        response = await cancel(test_client, row)
        assert response.status_code == 409 and response.json()["detail"]["code"] == "knowledge_work_already_running"
        assert not native.calls
    finally:
        release.set()
    assert await asyncio.wait_for(execution, timeout=15)
    assert native.calls.count("create") == 1
