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
OWNER_A = '["org-a","org-a","team-a","user-a","webchat"]'
OWNER_VICTIM = '["org-victim","org-victim","team-v","user-victim","webchat"]'
OWNER_ATTACKER = '["org-attacker","org-attacker","team-a","user-attacker","webchat"]'


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


def _make_sessions_table(name: str, item: dict | None = None):
    """Create a moto-backed sessions table, optionally seeded with one row."""
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    table = ddb.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    if item:
        table.put_item(Item=item)
    return table


class TestDeliveryOwnership:
    """#5660 (A07): a session row must not redirect another owner's stream.

    `session_id` reaches this path from the client, so the row it resolves may be
    one a caller named rather than one they own. When the task records the owner it
    was enqueued for, a row naming a different owner must not move delivery.
    """

    @mock_aws
    def test_row_naming_another_owner_does_not_redirect_delivery(self):
        # The row was rebound to the attacker's live connection.
        table = _make_sessions_table("test-sessions-owner-mismatch", {
            "session_id": "sess-victim",
            "connection_id": "conn-ATTACKER",
            "owner_principal": OWNER_ATTACKER,
        })

        WebSocketRouter = _import_router()
        router = WebSocketRouter("https://abc.execute-api.us-east-1.amazonaws.com/v1", sessions_table=table)
        mock_client = MagicMock()
        router._client = mock_client

        result = router.route("Private agent output", {
            "connection_id": "conn-VICTIM",
            "session_id": "sess-victim",
            "owner_principal": OWNER_VICTIM,
        }, "task-owned-by-victim")

        assert result is True
        # Delivered to the task's own connection, never the row's.
        call_kwargs = mock_client.post_to_connection.call_args[1]
        assert call_kwargs["ConnectionId"] == "conn-VICTIM"
        for call in mock_client.post_to_connection.call_args_list:
            assert call[1]["ConnectionId"] != "conn-ATTACKER"

    @mock_aws
    def test_refusal_emits_a_metric(self):
        table = _make_sessions_table("test-sessions-owner-metric", {
            "session_id": "sess-victim",
            "connection_id": "conn-ATTACKER",
            "owner_principal": OWNER_ATTACKER,
        })

        WebSocketRouter = _import_router()
        router = WebSocketRouter("https://abc.execute-api.us-east-1.amazonaws.com/v1", sessions_table=table)
        router._client = MagicMock()

        with patch("routers.websocket.logger") as mock_logger:
            router.route("Reply", {
                "connection_id": "conn-VICTIM",
                "session_id": "sess-victim",
                "owner_principal": OWNER_VICTIM,
            }, "task-1")

        emitted = [
            json.loads(c[0][0])
            for c in mock_logger.info.call_args_list
            if c[0] and isinstance(c[0][0], str) and c[0][0].startswith("{")
        ]
        mismatch = [m for m in emitted if m.get("DeliveryOwnerMismatch")]
        assert mismatch, f"expected a DeliveryOwnerMismatch metric, got {emitted}"
        assert mismatch[0]["reason"] == "session_owner_mismatch"

    @mock_aws
    def test_unowned_legacy_row_does_not_redirect_a_stamped_task(self):
        # A row predating owner recording carries no attribution, so it cannot be
        # shown to match. Quarantine rather than guess.
        table = _make_sessions_table("test-sessions-owner-legacy", {
            "session_id": "sess-legacy",
            "connection_id": "conn-UNKNOWN",
        })

        WebSocketRouter = _import_router()
        router = WebSocketRouter("https://abc.execute-api.us-east-1.amazonaws.com/v1", sessions_table=table)
        mock_client = MagicMock()
        router._client = mock_client

        router.route("Reply", {
            "connection_id": "conn-TASK",
            "session_id": "sess-legacy",
            "owner_principal": OWNER_A,
        }, "task-2")

        assert mock_client.post_to_connection.call_args[1]["ConnectionId"] == "conn-TASK"

    @mock_aws
    def test_matching_owner_still_follows_a_reconnect(self):
        # The reconnect fix (#68) must survive: a row whose owner matches the task
        # is still authoritative for the current connection.
        table = _make_sessions_table("test-sessions-owner-match", {
            "session_id": "sess-own",
            "connection_id": "conn-NEW",
            "owner_principal": OWNER_A,
        })

        WebSocketRouter = _import_router()
        router = WebSocketRouter("https://abc.execute-api.us-east-1.amazonaws.com/v1", sessions_table=table)
        mock_client = MagicMock()
        router._client = mock_client

        router.route("Reply", {
            "connection_id": "conn-OLD",
            "session_id": "sess-own",
            "owner_principal": OWNER_A,
        }, "task-3")

        assert mock_client.post_to_connection.call_args[1]["ConnectionId"] == "conn-NEW"

    @mock_aws
    def test_task_without_an_owner_stamp_uses_only_its_immutable_connection(self):
        table = _make_sessions_table("test-sessions-owner-absent", {
            "session_id": "sess-x",
            "connection_id": "conn-NEW",
            "owner_principal": OWNER_A,
        })

        WebSocketRouter = _import_router()
        router = WebSocketRouter("https://abc.execute-api.us-east-1.amazonaws.com/v1", sessions_table=table)
        mock_client = MagicMock()
        router._client = mock_client

        router.route("Reply", {"connection_id": "conn-OLD", "session_id": "sess-x"}, "task-4")

        assert mock_client.post_to_connection.call_args[1]["ConnectionId"] == "conn-OLD"


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
            "owner_principal": OWNER_A,
        })

        WebSocketRouter = _import_router()
        router = WebSocketRouter("https://abc.execute-api.us-east-1.amazonaws.com/v1", sessions_table=table)

        # Mock the APIGW management client
        mock_client = MagicMock()
        router._client = mock_client

        metadata = {
            "connection_id": "conn-OLD",  # stale snapshot from SQS
            "session_id": "sess-001",
            "owner_principal": OWNER_A,
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


class TestFrameChunking:
    """Issue #85, Problem A: large frames split into numbered chunks."""

    def test_small_content_no_chunk_fields(self):
        """A 5 KB response should be sent as a single frame with no chunk_* fields."""
        WebSocketRouter = _import_router()
        router = WebSocketRouter("https://abc.execute-api.us-east-1.amazonaws.com/v1", sessions_table=None)
        mock_client = MagicMock()
        router._client = mock_client

        small_content = "Hello, world! " * 300  # ~4.2 KB
        metadata = {"connection_id": "conn-small"}

        result = router.route(small_content, metadata, "task-small")

        assert result is True
        mock_client.post_to_connection.assert_called_once()
        call_kwargs = mock_client.post_to_connection.call_args[1]
        frame = json.loads(call_kwargs["Data"].decode("utf-8"))

        # No chunk fields on small frames
        assert "chunk_index" not in frame
        assert "chunk_total" not in frame
        assert frame["type"] == "response"
        assert frame["content"] == small_content

    def test_large_content_produces_chunks(self):
        """A 30 KB response should be split into 2+ frames with chunk_index/chunk_total."""
        WebSocketRouter = _import_router()
        router = WebSocketRouter("https://abc.execute-api.us-east-1.amazonaws.com/v1", sessions_table=None)
        mock_client = MagicMock()
        router._client = mock_client

        # 30 KB of content
        large_content = "A" * 30_000
        metadata = {"connection_id": "conn-large"}

        result = router.route(large_content, metadata, "task-large")

        assert result is True
        assert mock_client.post_to_connection.call_count >= 2

        # Verify chunk fields and reassembly
        frames = []
        for call in mock_client.post_to_connection.call_args_list:
            payload = call[1]["Data"].decode("utf-8")
            frame = json.loads(payload)
            frames.append(frame)
            # Each frame must be under 24 KB
            assert len(payload.encode("utf-8")) <= 24 * 1024

        chunk_total = frames[0]["chunk_total"]
        assert chunk_total == len(frames)

        # Verify sequential chunk_index
        for i, frame in enumerate(frames, start=1):
            assert frame["chunk_index"] == i
            assert frame["chunk_total"] == chunk_total
            assert frame["task_id"] == "task-large"
            assert frame["type"] == "response"

        # Reassemble and verify content is preserved
        reassembled = "".join(f["content"] for f in frames)
        assert reassembled == large_content

    def test_chunk_preserves_progress_extra_fields(self):
        """Chunked progress frames should carry kind and turn."""
        WebSocketRouter = _import_router()
        router = WebSocketRouter("https://abc.execute-api.us-east-1.amazonaws.com/v1", sessions_table=None)
        mock_client = MagicMock()
        router._client = mock_client

        large_content = "B" * 30_000
        metadata = {
            "connection_id": "conn-prog",
            "response_type": "progress",
            "progress_kind": "thinking",
            "progress_turn": 5,
        }

        result = router.route(large_content, metadata, "task-prog")

        assert result is True
        assert mock_client.post_to_connection.call_count >= 2

        for call in mock_client.post_to_connection.call_args_list:
            frame = json.loads(call[1]["Data"].decode("utf-8"))
            assert frame["type"] == "progress"
            assert frame["kind"] == "thinking"
            assert frame["turn"] == 5
            assert "chunk_index" in frame
            assert "chunk_total" in frame

    def test_exact_boundary_no_chunking(self):
        """Content exactly at 24 KB (with envelope) should not be chunked."""
        WebSocketRouter = _import_router()
        router = WebSocketRouter("https://abc.execute-api.us-east-1.amazonaws.com/v1", sessions_table=None)
        mock_client = MagicMock()
        router._client = mock_client

        # Build content that when JSON-wrapped stays under 24 KB.
        # The envelope adds ~100-200 bytes, so 23 KB of content should fit.
        content = "C" * (23 * 1024)
        metadata = {"connection_id": "conn-boundary"}

        router.route(content, metadata, "task-boundary")

        mock_client.post_to_connection.assert_called_once()
        frame = json.loads(mock_client.post_to_connection.call_args[1]["Data"].decode("utf-8"))
        assert "chunk_index" not in frame
