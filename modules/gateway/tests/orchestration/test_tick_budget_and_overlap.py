"""A slow evaluation read cannot poison the tick, and two ticks cannot overlap (#7889).

Production shape (adp-tools-platform, 2026-10-16 07:01–07:28Z): the L4 live evaluation
became ready; its provider reads outlived the 15 s observation budget in every tick,
the ``asyncio.timeout`` around the whole observation cancelled an in-flight statement,
the savepoint went invalid, and every later pass of the tick failed with
``PendingRollbackError`` — fifteen fatal ticks in a row. Two overlapping invocations
(reserved concurrency 2, 50–90 s each) were waiting on each other's row locks.
"""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.orchestration.dispatch_pass import recover_session
from src.orchestration.repository_evaluation import _ProviderBudget
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.tick_handler import acquire_tick_lock
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401


@pytest.fixture
async def pg_sessions(pg_url):  # noqa: F811
    """Plain sessions on a real PostgreSQL: advisory locks and aborted transactions are server behaviour, not schema."""
    engine = create_async_engine(to_async_url(pg_url))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def test_provider_budget_names_the_refusal_instead_of_cancelling_the_tick():
    async with _ProviderBudget(seconds=5):
        await asyncio.sleep(0)  # within budget: nothing raised
    with pytest.raises(CycleBlockedError, match="repository_evaluation_time_budget"):
        async with _ProviderBudget(seconds=0.01):
            await asyncio.sleep(1)


async def test_recover_session_rolls_back_an_aborted_transaction_so_later_nodes_run(pg_sessions):
    """The failing statement here stands for a cancelled or deadlocked one; the symptom is the same."""
    async with pg_sessions() as session:
        assert await recover_session(session, node_id="healthy") is False  # a usable session is left alone
        try:
            await session.execute(text("SELECT * FROM table_that_does_not_exist_7889"))
        except Exception:
            pass  # swallowed, as a buggy caller would — the transaction is now aborted
        assert await recover_session(session, node_id="poisoned") is True
        assert (await session.execute(text("SELECT 1"))).scalar_one() == 1


async def test_only_one_tick_holds_the_engine_lock(pg_sessions):
    async with pg_sessions() as first, pg_sessions() as second:
        assert await acquire_tick_lock(first) is True
        assert await acquire_tick_lock(second) is False  # an overlapping tick skips
        await first.rollback()  # transaction-scoped: released with the holder's transaction
        assert await acquire_tick_lock(second) is True
        await second.rollback()


async def test_provider_budget_scopes_share_one_observation_deadline(monkeypatch):
    """The token mint and the evidence reads of one observation draw on the same budget, as the old whole-observation timeout did."""
    from types import SimpleNamespace

    from src.orchestration import repository_evaluation

    now = [100.0]
    durations = []

    def timeout(seconds):
        durations.append(seconds)
        return asyncio.timeout(seconds)

    # Advance only the provider's clock. Real asyncio cancellation still runs,
    # without requiring the CI scheduler to wake within a 20 ms margin.
    monkeypatch.setattr(repository_evaluation, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(repository_evaluation, "asyncio", SimpleNamespace(timeout=timeout))
    token = repository_evaluation._OBSERVATION_DEADLINE.set(105.0)
    try:
        async with _ProviderBudget():
            now[0] = 103.0
        async with _ProviderBudget():
            now[0] = 105.0
        with pytest.raises(CycleBlockedError, match="repository_evaluation_time_budget"):
            async with _ProviderBudget():
                await asyncio.sleep(0)  # exhausted shared budget cancels on yield
        assert durations == [5.0, 2.0, 0.0]
    finally:
        repository_evaluation._OBSERVATION_DEADLINE.reset(token)
