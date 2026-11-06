"""Readiness follows actual ACL access, including bounded failure and recovery."""

import asyncio
import json
import threading
from unittest.mock import AsyncMock, Mock, MagicMock

import pytest

from door import server
from door.acl import PostgresACLStore
from door.project_filter import ProjectScope
from door.search_backend import ZoektUnavailable


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


async def test_code_search_without_index_is_not_empty_success(readiness_state):
    readiness_state.acl_store = Mock()
    readiness_state.zoekt = None
    readiness_state.experience_tool = Mock()

    result = await server._dispatch_tool(
        "search", {"query": "fixture-sentinel", "scope": "code"}, {}, None
    )

    assert result == {
        "error": "Knowledge code index is not ready.",
        "code": "knowledge_index_not_ready",
    }
    readiness_state.acl_store.get_allowed_repos.assert_not_called()
    readiness_state.experience_tool.assert_not_called()


async def test_pending_project_index_never_looks_like_an_empty_success(readiness_state):
    scope = ProjectScope(project_id="fixture-project", repo_names=frozenset({"fixture/repo"}))
    pool = MagicMock()
    cursor = pool.getconn.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [("fixture/repo", "pending")]
    readiness_state.db_pool = pool
    readiness_state.zoekt = Mock(search=AsyncMock(return_value=[]))
    query = {"query": "no-matching-sentinel", "scope": "code"}

    pending = await server._handle_search(query, caller=None, project_scope=scope)
    assert pending == {"error": "Knowledge code index is not ready.", "code": "knowledge_index_not_ready"}
    assert "no-matching-sentinel" not in str(pending)
    readiness_state.zoekt.search.assert_not_awaited()
    assert pool.putconn.call_count == 1
    assert cursor.execute.call_count == 1
    sql, parameters = cursor.execute.call_args.args
    assert "index_run_stages" in sql and "index_runs" in sql and parameters == (["fixture/repo"],)

    cursor.fetchall.return_value = [("fixture/repo", "verified")]
    ready = await server._handle_search(query, caller=None, project_scope=scope)
    assert ready == {"results": [], "total": 0, "query": "no-matching-sentinel"}
    readiness_state.zoekt.search.assert_awaited_once()
    assert pool.putconn.call_count == 2


@pytest.mark.parametrize("rows", [[], [("fixture/repo", None)], [("fixture/repo", "failed")],
                                  [("fixture/repo", "verified"), ("fixture/other", "pending")]])
async def test_code_search_requires_verified_latest_stage_for_every_project_repo(readiness_state, rows):
    scope = ProjectScope(project_id="fixture-project", repo_names=frozenset({"fixture/repo", "fixture/other"}))
    pool = MagicMock()
    pool.getconn.return_value.cursor.return_value.__enter__.return_value.fetchall.return_value = rows
    readiness_state.db_pool = pool
    readiness_state.zoekt = Mock(search=AsyncMock(return_value=[]))

    result = await server._handle_search({"query": "sentinel", "scope": "code"}, caller=None, project_scope=scope)

    assert result["code"] == "knowledge_index_not_ready"
    readiness_state.zoekt.search.assert_not_awaited()
    pool.putconn.assert_called_once()


async def test_pending_index_status_unavailable_fails_closed(readiness_state):
    scope = ProjectScope(project_id="fixture-project", repo_names=frozenset({"fixture/repo"}))
    pool = Mock()
    pool.getconn.side_effect = ConnectionError("private database unavailable")
    readiness_state.db_pool = pool
    readiness_state.zoekt = Mock(search=AsyncMock())
    result = await server._handle_search({"query": "sentinel", "scope": "code"}, caller=None, project_scope=scope)
    assert result == {"error": "Knowledge code index is not ready.", "code": "knowledge_index_not_ready"}
    assert "private database" not in str(result)
    readiness_state.zoekt.search.assert_not_awaited()


async def test_zoekt_failure_is_an_explicit_error_not_an_empty_result(readiness_state):
    readiness_state.zoekt = Mock(search=AsyncMock(side_effect=ZoektUnavailable()))
    result = await server._handle_search({"query": "private-sentinel", "scope": "code"}, caller=None)
    assert result == {"error": "Knowledge search backend is unavailable.", "code": "knowledge_backend_unavailable"}
    assert "private-sentinel" not in str(result)
    readiness_state.zoekt.search.assert_awaited_once()
