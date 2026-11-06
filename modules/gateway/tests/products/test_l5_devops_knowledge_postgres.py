"""Real independent-session row locking for concurrent knowledge admission."""

import asyncio

from l5_services.models.setup import SetupIntent, SetupWork
from sqlalchemy import event, func, select

from src.products.models import ProductSourceSnapshot
from tests.migrations.conftest_postgres import pg_server as pg_server
from tests.migrations.conftest_postgres import pg_url as pg_url
from tests.products.test_l5_devops_knowledge_admission import (
    consent,
    submit,
)
from tests.products.test_l5_devops_knowledge_admission import (
    consent_fixture as consent_fixture,
)
from tests.products.test_l5_devops_knowledge_admission import (
    knowledge_fixture as knowledge_fixture,
)
from tests.products.test_l5_devops_knowledge_admission import (
    l5_owner as l5_owner,
)
from tests.products.test_l5_setup_continuations_postgres import test_engine as test_engine

__all__ = ["pg_server", "pg_url", "consent_fixture", "knowledge_fixture", "l5_owner", "test_engine"]


async def test_concurrent_same_key_locks_capture_and_returns_original_operation(test_client, db_session, test_engine, knowledge_fixture):
    fixture = knowledge_fixture
    await consent(test_client, fixture)
    second_lock_seen = asyncio.Event()
    lock_queries = 0

    def query_started(_conn, _cursor, statement, _parameters, _context, _many):
        nonlocal lock_queries
        if "l5_service_admission_preparations" in statement and "FOR UPDATE" in statement:
            lock_queries += 1
            if lock_queries >= 2:
                second_lock_seen.set()

    event.listen(test_engine.sync_engine, "before_cursor_execute", query_started)
    capture = fixture["capture"].side_effect

    async def held_capture(*args):
        # The second SQL session has reached the locked preparation before the
        # first connector capture returns. No sleeps or shared-session shortcut.
        await asyncio.wait_for(second_lock_seen.wait(), timeout=10)
        return await capture(*args)

    fixture["capture"].side_effect = held_capture
    try:
        first, second = await asyncio.wait_for(asyncio.gather(submit(test_client, fixture), submit(test_client, fixture)), timeout=30)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", query_started)
    assert first.status_code == second.status_code == 202, (first.text, second.text)
    assert first.json() == second.json()
    assert lock_queries >= 2 and fixture["capture"].await_count == 1
    assert await db_session.scalar(select(func.count()).select_from(SetupIntent)) == 1
    assert await db_session.scalar(select(func.count()).select_from(SetupWork)) == 1
    assert await db_session.scalar(select(func.count()).select_from(ProductSourceSnapshot)) == 1
