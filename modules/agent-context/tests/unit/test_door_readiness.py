"""Readiness follows actual ACL access, including bounded failure and recovery."""

import asyncio
import json
import threading
from unittest.mock import Mock, MagicMock

import pytest

from door import server
from door.acl import PostgresACLStore


@pytest.fixture
def readiness_state(monkeypatch):
    state = server.AppState()
    monkeypatch.setattr(server, "state", state)
    return state


async def test_readiness_tracks_database_failure_and_recovery(readiness_state):
    pool = MagicMock()
    store = PostgresACLStore(pool)
    readiness_state.acl_store = store
    pool.getconn.side_effect = ConnectionError("private-host private-password")

    failed = await server.readiness_check()
    assert failed.status_code == 503
    assert json.loads(failed.body)["reason"] == "ConnectionError"
    assert b"private" not in failed.body

    pool.getconn.side_effect = None
    recovered = await server.readiness_check()
    assert recovered.status_code == 200
    assert pool.getconn.return_value.cursor.return_value.__enter__.return_value.execute.called
    pool.putconn.assert_called_once_with(pool.getconn.return_value)

    pool.getconn.return_value.cursor.return_value.__enter__.return_value.execute.side_effect = (
        PermissionError("ACL schema no longer readable")
    )
    degraded = await server.readiness_check()
    assert degraded.status_code == 503
    assert json.loads(degraded.body)["reason"] == "PermissionError"


async def test_slow_acl_probe_is_bounded_and_not_duplicated(readiness_state, monkeypatch):
    release = threading.Event()
    store = Mock(check_health=Mock(side_effect=lambda: release.wait(5)))
    readiness_state.acl_store = store
    monkeypatch.setattr(server, "ACL_READINESS_TIMEOUT_SECONDS", 0.01)
    try:
        responses = await asyncio.wait_for(
            asyncio.gather(server.readiness_check(), server.readiness_check()), timeout=1
        )
        assert [response.status_code for response in responses] == [503, 503]
        store.check_health.assert_called_once()
        another = await server.readiness_check()
        assert another.status_code == 503
        store.check_health.assert_called_once()
    finally:
        release.set()
        await readiness_state.acl_probe_task
    assert (await server.readiness_check()).status_code == 200


async def test_missing_acl_store_is_not_ready(readiness_state):
    assert (await server.readiness_check()).status_code == 503
