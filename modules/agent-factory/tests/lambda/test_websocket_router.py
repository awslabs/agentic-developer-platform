"""
Unit tests for the WebSocket response router.

Bug 1 from issue #68:
- Response router should resolve the *active* connection_id from the sessions
  table, not blindly use the stale snapshot in the SQS metadata.
- GoneException should clear the stale connection_id from the session row.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

ROUTER_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "gateway", "lambdas", "response"
)


@pytest.fixture(autouse=True)
def _patch_sys_path():
    original = sys.path.copy()
    sys.path.insert(0, ROUTER_DIR)
    yield
    sys.path = original


def _import_router():
    for mod_name in list(sys.modules.keys()):
        if mod_name in ("routers", "routers.websocket"):
            del sys.modules[mod_name]
    from routers.websocket import WebSocketRouter
    return WebSocketRouter


class TestActiveConnectionLookup:
    """Bug 1: route() should prefer the session table's active connection_id."""

    @mock_aws
    def test_uses_active_connection_from_session_table(self):
        # Setup DynamoDB with a session that has a *different* connection_id
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="test-sessions",
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.put_item(Item={
            "session_id": "sess-001",
            "connection_id": "conn-NEW",  # active connection after reconnect
        })

        WebSocketRouter = _import_router()
        router = WebSocketRouter("https://abc.execute-api.us-east-1.amazonaws.com/v1", sessions_table=table)

        # Mock the APIGW management client
        mock_client = MagicMock()
        router._client = mock_client

        metadata = {
            "connection_id": "conn-OLD",  # stale snapshot from SQS
            "session_id": "sess-001",
        }

        result = router.route("Hello!", metadata, "task-001")

        assert result is True
        # Verify post_to_connection was called with the NEW connection_id
        mock_client.post_to_connection.assert_called_once()
        call_kwargs = mock_client.post_to_connection.call_args[1]
        assert call_kwargs["ConnectionId"] == "conn-NEW"

    @mock_aws
    def test_falls_back_to_metadata_when_session_missing(self):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="test-sessions-empty",
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # No session row — TTL expired

        WebSocketRouter = _import_router()
        router = WebSocketRouter("https://abc.execute-api.us-east-1.amazonaws.com/v1", sessions_table=table)
        mock_client = MagicMock()
        router._client = mock_client

        metadata = {
            "connection_id": "conn-FALLBACK",
            "session_id": "sess-gone",
        }

        result = router.route("Reply", metadata, "task-002")

        assert result is True
        call_kwargs = mock_client.post_to_connection.call_args[1]
        assert call_kwargs["ConnectionId"] == "conn-FALLBACK"

    @mock_aws
    def test_falls_back_when_no_sessions_table(self):
        WebSocketRouter = _import_router()
        router = WebSocketRouter("https://abc.execute-api.us-east-1.amazonaws.com/v1", sessions_table=None)
        mock_client = MagicMock()
        router._client = mock_client

        metadata = {
            "connection_id": "conn-ONLY",
            "session_id": "sess-001",
        }

        result = router.route("Reply", metadata, "task-003")

        assert result is True
        call_kwargs = mock_client.post_to_connection.call_args[1]
        assert call_kwargs["ConnectionId"] == "conn-ONLY"

    @mock_aws
    def test_falls_back_when_session_has_no_connection_id(self):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="test-sessions-noconn",
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # Session exists but connection_id was cleared (GoneException cleanup)
        table.put_item(Item={"session_id": "sess-002"})

        WebSocketRouter = _import_router()
        router = WebSocketRouter("https://abc.execute-api.us-east-1.amazonaws.com/v1", sessions_table=table)
        mock_client = MagicMock()
        router._client = mock_client

        metadata = {
            "connection_id": "conn-META",
            "session_id": "sess-002",
        }

        result = router.route("Reply", metadata, "task-004")

        assert result is True
        call_kwargs = mock_client.post_to_connection.call_args[1]
        assert call_kwargs["ConnectionId"] == "conn-META"


class TestGoneExceptionCleanup:
    """Bug 1: GoneException clears connection_id from the session row."""

    @mock_aws
    def test_gone_clears_connection_from_session(self):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="test-sessions-gone",
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.put_item(Item={
            "session_id": "sess-stale",
            "connection_id": "conn-STALE",
        })

        WebSocketRouter = _import_router()
        router = WebSocketRouter("https://abc.execute-api.us-east-1.amazonaws.com/v1", sessions_table=table)

        # Simulate GoneException
        mock_client = MagicMock()
        gone_error = ClientError(
            {"Error": {"Code": "GoneException", "Message": "Connection gone"}},
            "PostToConnection",
        )
        mock_client.post_to_connection.side_effect = gone_error
        router._client = mock_client

        metadata = {
            "connection_id": "conn-STALE",
            "session_id": "sess-stale",
        }

        result = router.route("Reply", metadata, "task-gone")

        assert result is False

        # Verify connection_id was removed from the session row
        item = table.get_item(Key={"session_id": "sess-stale"}).get("Item", {})
        assert "connection_id" not in item

    @mock_aws
    def test_gone_does_not_clear_if_already_reconnected(self):
        """If a new connection came in between send and GoneException, don't nuke it."""
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="test-sessions-race",
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # Session was reconnected — connection_id is now fresh
        table.put_item(Item={
            "session_id": "sess-race",
            "connection_id": "conn-FRESH",
        })

        WebSocketRouter = _import_router()
        router = WebSocketRouter("https://abc.execute-api.us-east-1.amazonaws.com/v1", sessions_table=table)

        mock_client = MagicMock()
        gone_error = ClientError(
            {"Error": {"Code": "GoneException", "Message": "Connection gone"}},
            "PostToConnection",
        )
        mock_client.post_to_connection.side_effect = gone_error
        router._client = mock_client

        # The metadata carries the OLD stale connection
        metadata = {
            "connection_id": "conn-STALE-OLD",
            "session_id": "sess-race",
        }

        # Resolve will find conn-FRESH from the table, but that also goes stale.
        # Actually, the resolve finds conn-FRESH, sends to conn-FRESH, gets GoneException.
        # Cleanup tries to REMOVE connection_id WHERE conn == conn-FRESH.
        # For the race scenario, let's say the metadata connection_id is stale,
        # but the table has the FRESH one. The resolve picks FRESH, sends to FRESH,
        # gets GoneException. The cleanup tries to clear FRESH. But what if
        # another reconnect happened between the send and the cleanup?
        # Let's simulate: after GoneException, update the row to a NEW connection
        # Then cleanup should fail the condition check (conn != stale)
        # Actually, to properly test this, we need to intercept between the send failure
        # and the cleanup. Let's test the simpler case: cleanup on a row where
        # the stored connection_id differs from the stale one.

        # Manually set a new connection after the router resolved but before cleanup
        # We'll test the condition expression directly
        table.put_item(Item={
            "session_id": "sess-race",
            "connection_id": "conn-SUPER-FRESH",  # updated by new reconnect
        })

        # Now call cleanup with the connection that was just resolved (conn-FRESH)
        router._cleanup_connection("conn-FRESH", "sess-race")

        # conn-SUPER-FRESH should survive — condition check fails
        item = table.get_item(Key={"session_id": "sess-race"}).get("Item", {})
        assert item.get("connection_id") == "conn-SUPER-FRESH"


class TestProgressFrameRouting:
    """Verify progress frames carry kind and turn metadata."""

    @mock_aws
    def test_heartbeat_progress_frame_shape(self):
        WebSocketRouter = _import_router()
        router = WebSocketRouter("https://abc.execute-api.us-east-1.amazonaws.com/v1", sessions_table=None)
        mock_client = MagicMock()
        router._client = mock_client

        metadata = {
            "connection_id": "conn-hb",
            "response_type": "progress",
            "progress_kind": "heartbeat",
            "progress_turn": 3,
        }

        router.route("thinking...", metadata, "task-hb")

        call_kwargs = mock_client.post_to_connection.call_args[1]
        frame = json.loads(call_kwargs["Data"].decode("utf-8"))
        assert frame["type"] == "progress"
        assert frame["kind"] == "heartbeat"
        assert frame["turn"] == 3
        assert frame["content"] == "thinking..."
