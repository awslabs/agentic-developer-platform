"""Transient accounting failures retry the receipt, never the model request."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import DBAPIError

from src.usage import persistence


def database_error(code="55P03", *, invalidated=False):
    error = Exception("private database details")
    error.sqlstate = code
    return DBAPIError("private SQL", {"secret": "private binding"}, error, connection_invalidated=invalidated)


@pytest.mark.parametrize("code", ["55P03", "40001", "40P01", "08006", "57P01", "57014"])
async def test_retryable_transactions_succeed_without_exposing_sql(monkeypatch, caplog, code):
    sleep = AsyncMock()
    monkeypatch.setattr(persistence.asyncio, "sleep", sleep)
    write = AsyncMock(side_effect=[database_error(code), None])
    await persistence.persist_usage_with_retry(write, request_id="server-request", retry_safe=True)
    assert write.await_count == 2 and sleep.await_count == 1
    record = next(r for r in caplog.records if r.message == "Retrying usage settlement transaction")
    assert record.sqlstate == code and record.request_id == "server-request"
    assert "private" not in caplog.text


async def test_persistent_failure_is_bounded_and_propagates(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(persistence.asyncio, "sleep", sleep)
    write = AsyncMock(side_effect=database_error())
    with pytest.raises(DBAPIError):
        await persistence.persist_usage_with_retry(write, request_id="server-request", retry_safe=True)
    assert write.await_count == 4 and sleep.await_count == 3


@pytest.mark.parametrize("error", [database_error("23505"), database_error("42703"), ValueError("bad receipt")])
async def test_permanent_errors_are_not_retried(error):
    write = AsyncMock(side_effect=error)
    with pytest.raises(type(error)):
        await persistence.persist_usage_with_retry(write, request_id="server-request", retry_safe=True)
    write.assert_awaited_once()


@pytest.mark.parametrize("request_id,retry_safe", [(None, True), ("request", False)])
async def test_unreceipted_writes_are_not_replayed(request_id, retry_safe):
    write = AsyncMock(side_effect=database_error())
    with pytest.raises(DBAPIError):
        await persistence.persist_usage_with_retry(write, request_id=request_id, retry_safe=retry_safe)
    write.assert_awaited_once()


async def test_cancellation_is_not_swallowed():
    write = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await persistence.persist_usage_with_retry(write, request_id="request", retry_safe=True)
    write.assert_awaited_once()


def test_malformed_driver_code_is_not_logged():
    assert persistence.usage_write_error_details(database_error("private database text"))["sqlstate"] is None
