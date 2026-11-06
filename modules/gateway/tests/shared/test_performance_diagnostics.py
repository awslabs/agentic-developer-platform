"""Local database diagnostic tests; no target database or credentials required."""

import asyncio
from unittest.mock import AsyncMock

from src.shared import performance_diagnostics as diagnostics


async def test_active_blocker_capture_is_bounded_and_uses_separate_read_only_connection(monkeypatch):
    class FakeConnection:
        def __init__(self):
            self.fetch = AsyncMock(
                return_value=[
                    {
                        "pid": 22,
                        "blocker_pids": [33],
                        "query_age_ms": 24,
                        "waiter_xact_age_ms": 32,
                        "blocker_xact_age_ms": 91,
                        "waiter_query_id": 123,
                        "blocker_query_ids": [456],
                    }
                ]
            )
            self.terminated = False

        def terminate(self):
            self.terminated = True

    connection = FakeConnection()
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr(diagnostics.asyncpg, "connect", connect)

    result = await diagnostics.capture_blockers("postgresql://diagnostic-role:private@localhost/fixture")

    assert result == diagnostics.BlockerSample("captured", (diagnostics.Blocker(22, (33,), 24, 32, 91, 123, (456,)),))
    assert "LIMIT 100" in connection.fetch.await_args.args[0]
    assert "waiter.query AS" not in connection.fetch.await_args.args[0]
    assert "blocker.query AS" not in connection.fetch.await_args.args[0]
    assert connect.await_args.kwargs["server_settings"]["default_transaction_read_only"] == "on"
    assert connect.await_args.kwargs["server_settings"]["statement_timeout"] == "500"
    assert connection.terminated


async def test_sample_caps_rows_and_blockers_without_copying_query_or_bind_fields(monkeypatch):
    rows = [
        {
            "pid": pid,
            "blocker_pids": list(range(110)),
            "query_age_ms": 50,
            "query": "SELECT test-private-query",
            "binds": "test-private-bind",
            "tenant_id": "test-private-tenant",
        }
        for pid in range(150)
    ]
    connection = AsyncMock()
    connection.terminate = lambda: None
    connection.fetch.return_value = rows
    monkeypatch.setattr(diagnostics.asyncpg, "connect", AsyncMock(return_value=connection))

    sample = await diagnostics.capture_blockers("postgresql://private-role:test-private-password@localhost/fixture")

    assert sample.status == "captured"
    assert len(sample.blockers) == 100
    assert len(sample.blockers[0].blocker_pids) == 100
    serialized = repr(sample)
    for value in ("test-private-query", "test-private-bind", "test-private-tenant", "test-private-password"):
        assert value not in serialized


async def test_capture_failure_does_not_return_database_url_or_error(monkeypatch):
    connect = AsyncMock(side_effect=OSError("postgresql://diagnostic-role:secret@localhost/fixture"))
    monkeypatch.setattr(diagnostics.asyncpg, "connect", connect)
    failures = diagnostics.DIAGNOSTIC_FAILURES._value.get()

    assert await diagnostics.capture_blockers("postgresql://diagnostic-role:secret@localhost/fixture") == diagnostics.BlockerSample("failed")
    assert await diagnostics.capture_blockers(None) == diagnostics.BlockerSample("disabled")
    assert connect.await_count == 1
    assert diagnostics.DIAGNOSTIC_FAILURES._value.get() == failures + 1


async def test_concurrent_capture_sheds_work(monkeypatch):
    capture_lock = asyncio.Lock()
    monkeypatch.setattr(diagnostics, "_capture_lock", capture_lock)
    await capture_lock.acquire()
    try:
        assert await diagnostics.capture_blockers("postgresql://private@localhost/fixture") == diagnostics.BlockerSample("busy")
    finally:
        capture_lock.release()


async def test_busy_optional_watcher_is_dropped_without_backlog_or_request_delay():
    previous_drops = diagnostics.DIAGNOSTIC_DROPS._value.get()
    first = diagnostics.start_pressure_capture("postgresql://private@localhost/fixture", "first")
    assert first is not None
    try:
        assert diagnostics.start_pressure_capture("postgresql://private@localhost/fixture", "second") is None
        assert diagnostics.DIAGNOSTIC_DROPS._value.get() == previous_drops + 1
    finally:
        diagnostics.finish_pressure_capture(first)

    next_request = diagnostics.start_pressure_capture("postgresql://private@localhost/fixture", "next")
    assert next_request is not None
    diagnostics.finish_pressure_capture(next_request)
    await asyncio.sleep(0)


async def test_connection_timeout_is_bounded(monkeypatch):
    async def slow_connect(**kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(diagnostics.asyncpg, "connect", slow_connect)
    assert await asyncio.wait_for(diagnostics.capture_blockers("postgresql://private@localhost/fixture"), 1) == diagnostics.BlockerSample("failed")
