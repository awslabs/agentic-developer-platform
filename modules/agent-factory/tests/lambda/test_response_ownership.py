"""
Response handler ownership verification (#5615 / S16 continuation).

PR #5857 added the owner_principal ConditionExpression to every response
handler mutation. These tests verify that a response destined for a session
with a DIFFERENT owner is refused at every persistence and bookkeeping path —
not just the delivery redirect, which has its own tests in
test_response_handler.py.

Covered:
  1. _append_response         — refuses a mismatched owner_principal
  2. _set_thread_processing   — refuses a mismatched owner_principal
  3. _clear_thread_processing — refuses a mismatched owner_principal
  4. _clear_session_processing — refuses a mismatched owner_principal
  5. process_response (integration) — a response with no owner_principal
     does not persist or mutate session state
  6. delivery redirect          — owner mismatch falls back to task's own
     connection, never to the session row's connection
  7. _append_response          — refuses a mismatched session_generation
     even when owner_principal matches

Every test seeds a real moto DynamoDB session row, then drives the response
handler function with a different owner or generation.  The assertions check
both the return value AND the DDB row to confirm nothing was written.
"""

from __future__ import annotations

import json
import os
import sys
import time
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

RESPONSE_HANDLER_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "gateway", "lambdas", "response"
)

# Canonical owner principals — two tenants whose ownership should never mix.
OWNER_A = json.dumps(["tenant-a", "org-a", "team-a", "user-a", "webchat"], separators=(",", ":"))
OWNER_B = json.dumps(["tenant-b", "org-b", "team-b", "user-b", "webchat"], separators=(",", ":"))
SESSION_GENERATION = 1000000


@pytest.fixture(autouse=True)
def _patch_sys_path():
    original = sys.path.copy()
    sys.path.insert(0, RESPONSE_HANDLER_DIR)
    yield
    sys.path = original


def _reimport_handler():
    for mod in list(sys.modules):
        if mod == "handler" or mod.startswith("routers"):
            del sys.modules[mod]
    import handler
    return handler


@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("INPUT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/queue.fifo")
    monkeypatch.setenv("SESSIONS_TABLE_NAME", "test-sessions")
    monkeypatch.setenv("WS_API_ENDPOINT", "https://ws.example.com/v1")
    monkeypatch.setenv("WS_API_ID", "ws123")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("AWS_REGION_NAME", "us-east-1")


@pytest.fixture
def aws(mock_env):
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="test-sessions",
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-sessions")
        yield {"table": table, "ddb": ddb}


def _seed_session(table, session_id="sess-owned", owner=OWNER_A,
                  generation=SESSION_GENERATION, **extra):
    item = {
        "session_id": session_id,
        "owner_principal": owner,
        "created_at": generation,
        "connection_id": "conn-original",
        "channel": "webchat",
        "messages": [],
        "threads": {},
        "updated_at": generation,
        "expires_at": generation + 86400,
    }
    item.update(extra)
    table.put_item(Item=item)
    return item


def _get_session(table, session_id="sess-owned"):
    return table.get_item(Key={"session_id": session_id}, ConsistentRead=True).get("Item", {})


# ─── Test: _append_response refuses a mismatched owner ──────────

class TestAppendResponseOwnership:
    """Verify that _append_response will not persist a reply into a session
    whose owner_principal does not match the task's owner."""

    def test_mismatched_owner_is_refused(self, aws):
        handler = _reimport_handler()
        _seed_session(aws["table"])

        result = handler._append_response(
            "sess-owned", "Reply text", "task-1", int(time.time()),
            OWNER_B,  # wrong owner
            SESSION_GENERATION,
        )

        assert result is False
        row = _get_session(aws["table"])
        assert row["messages"] == []

    def test_correct_owner_is_accepted(self, aws):
        handler = _reimport_handler()
        _seed_session(aws["table"])

        result = handler._append_response(
            "sess-owned", "Reply text", "task-1", int(time.time()),
            OWNER_A,
            SESSION_GENERATION,
        )

        assert result is True
        row = _get_session(aws["table"])
        assert len(row["messages"]) == 1
        assert row["messages"][0]["content"] == "Reply text"

    def test_mismatched_generation_is_refused(self, aws):
        """Same owner, wrong generation — the session was re-created between
        enqueue and delivery.  The response must not land in the new session."""
        handler = _reimport_handler()
        _seed_session(aws["table"])

        result = handler._append_response(
            "sess-owned", "Stale reply", "task-1", int(time.time()),
            OWNER_A,
            SESSION_GENERATION + 1,  # wrong generation
        )

        assert result is False
        row = _get_session(aws["table"])
        assert row["messages"] == []

    def test_absent_owner_in_session_is_refused(self, aws):
        """A legacy session row with no owner_principal must not accept
        responses, even if the task claims the same (empty) owner."""
        handler = _reimport_handler()
        aws["table"].put_item(Item={
            "session_id": "sess-legacy",
            "created_at": SESSION_GENERATION,
            "connection_id": "conn-legacy",
            "channel": "webchat",
            "messages": [],
            "threads": {},
        })

        result = handler._append_response(
            "sess-legacy", "Reply", "task-1", int(time.time()),
            OWNER_A, SESSION_GENERATION,
        )

        assert result is False
        row = _get_session(aws["table"], "sess-legacy")
        assert row["messages"] == []


# ─── Test: _set_thread_processing refuses a mismatched owner ────

class TestSetThreadProcessingOwnership:

    def test_mismatched_owner_refuses_thread_lock(self, aws):
        handler = _reimport_handler()
        _seed_session(aws["table"], threads={
            "t1": {"topic": "test", "processing_task_id": "", "messages": []},
        }, last_response_task_id="task-done")

        result = handler._set_thread_processing(
            "sess-owned", "t1", "task-new",
            OWNER_B,  # wrong owner
            SESSION_GENERATION, "task-done",
        )

        assert result is False
        row = _get_session(aws["table"])
        assert row["threads"]["t1"]["processing_task_id"] == ""

    def test_correct_owner_accepts_thread_lock(self, aws):
        handler = _reimport_handler()
        _seed_session(aws["table"], threads={
            "t1": {"topic": "test", "processing_task_id": "", "messages": []},
        }, last_response_task_id="task-done")

        result = handler._set_thread_processing(
            "sess-owned", "t1", "task-new",
            OWNER_A,
            SESSION_GENERATION, "task-done",
        )

        assert result is True
        row = _get_session(aws["table"])
        assert row["threads"]["t1"]["processing_task_id"] == "task-new"


# ─── Test: _clear_thread_processing refuses a mismatched owner ──

class TestClearThreadProcessingOwnership:

    def test_mismatched_owner_refuses_thread_unlock(self, aws):
        handler = _reimport_handler()
        _seed_session(aws["table"], threads={
            "t1": {"topic": "test", "processing_task_id": "task-running", "messages": []},
        }, last_response_task_id="task-running")

        result = handler._clear_thread_processing(
            "sess-owned", "t1",
            OWNER_B,  # wrong owner
            SESSION_GENERATION, "task-running", "task-running",
        )

        assert result is False
        row = _get_session(aws["table"])
        assert row["threads"]["t1"]["processing_task_id"] == "task-running"


# ─── Test: _clear_session_processing refuses a mismatched owner ─

class TestClearSessionProcessingOwnership:

    def test_mismatched_owner_refuses_session_unlock(self, aws):
        handler = _reimport_handler()
        _seed_session(aws["table"], processing_task_id="task-running",
                      last_response_task_id="task-running")

        result = handler._clear_session_processing(
            "sess-owned",
            OWNER_B,  # wrong owner
            SESSION_GENERATION, "task-running",
        )

        assert result is False
        row = _get_session(aws["table"])
        assert row.get("processing_task_id") == "task-running"


# ─── Test: process_response integration — no owner → no persist ─

class TestProcessResponseWithoutOwner:
    """When a response arrives without owner_principal, it must not persist
    or mutate session state, regardless of whether the session exists."""

    def test_ownerless_response_does_not_persist(self, aws):
        handler = _reimport_handler()
        _seed_session(aws["table"])

        # Mock the WS router so the delivery path doesn't fail
        mock_ws = MagicMock()
        mock_ws.route.return_value = True
        handler.ws_router = mock_ws

        # Simulate a response SQS message with no owner_principal
        handler._process_response({
            "task_id": "task-no-owner",
            "session_id": "sess-owned",
            "thread_id": "",
            "connection_id": "conn-original",
            "channel": "webchat",
            "result": "Unowned reply",
            "status": "completed",
            "completed_at": int(time.time()),
            # owner_principal intentionally absent
        })

        row = _get_session(aws["table"])
        assert row["messages"] == [], "An ownerless response must not persist"


# ─── Test: delivery redirect refuses owner mismatch ──────────────

class TestDeliveryRedirectOwnership:
    """The WebSocket router resolves the active connection_id from the
    session row — but only if the task's owner matches the row's owner.
    A mismatch must fall back to the task's own connection, never to the
    session row's connection (which may belong to a different user)."""

    def test_owner_mismatch_uses_task_connection(self, aws):
        _reimport_handler()
        _seed_session(aws["table"], connection_id="conn-victim")

        from routers.websocket import WebSocketRouter
        router = WebSocketRouter.__new__(WebSocketRouter)
        router._sessions_table = aws["table"]

        metadata = {
            "connection_id": "conn-attacker-task",
            "session_id": "sess-owned",
            "owner_principal": OWNER_B,  # task claims owner B
        }

        resolved = router._resolve_connection_id(metadata)

        # Must NOT resolve to conn-victim (the session row's connection)
        assert resolved != "conn-victim"
        # Falls back to the task's own connection
        assert resolved == "conn-attacker-task"

    def test_matching_owner_resolves_to_active_connection(self, aws):
        _reimport_handler()
        _seed_session(aws["table"], connection_id="conn-reconnected")

        from routers.websocket import WebSocketRouter
        router = WebSocketRouter.__new__(WebSocketRouter)
        router._sessions_table = aws["table"]

        metadata = {
            "connection_id": "conn-stale",
            "session_id": "sess-owned",
            "owner_principal": OWNER_A,
        }

        resolved = router._resolve_connection_id(metadata)

        # Owner matches — resolve to the session's current connection
        assert resolved == "conn-reconnected"

    def test_absent_task_owner_uses_task_connection(self, aws):
        """A task with no owner_principal must never redirect to any session
        row, even one that also has no owner."""
        _reimport_handler()
        aws["table"].put_item(Item={
            "session_id": "sess-noowner",
            "connection_id": "conn-someone",
            "messages": [], "threads": {},
        })

        from routers.websocket import WebSocketRouter
        router = WebSocketRouter.__new__(WebSocketRouter)
        router._sessions_table = aws["table"]

        metadata = {
            "connection_id": "conn-task",
            "session_id": "sess-noowner",
            # owner_principal absent
        }

        resolved = router._resolve_connection_id(metadata)
        assert resolved == "conn-task"
