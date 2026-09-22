"""
Unit tests for the Response Lambda handler.

Tests 12-14 from the issue:
 12. Consume SQS message -> push to WS (mocked). Assert connection_id and data shape.
 13. Stale connection (GoneException) -> does not 500, silently drops.
 14. Malformed SQS message -> batchItemFailures, does not retry forever.
"""

from __future__ import annotations

import json
import os
import sys
import time
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

RESPONSE_HANDLER_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "gateway", "lambdas", "response"
)


@pytest.fixture(autouse=True)
def _patch_sys_path():
    """Add the response Lambda directory to sys.path."""
    original = sys.path.copy()
    sys.path.insert(0, RESPONSE_HANDLER_DIR)
    yield
    sys.path = original


@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("INPUT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/adp-dev-agent-gateway-tasks")
    monkeypatch.setenv("SESSIONS_TABLE_NAME", "adp-dev-agent-gateway-sessions")
    monkeypatch.setenv("WS_API_ENDPOINT", "https://abc123.execute-api.us-east-1.amazonaws.com/v1")
    monkeypatch.setenv("WS_API_ID", "abc123")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("AWS_REGION_NAME", "us-east-1")


@pytest.fixture
def mocked_aws_services(mock_env):
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="adp-dev-agent-gateway-sessions",
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        sqs = boto3.client("sqs", region_name="us-east-1")
        sqs.create_queue(QueueName="adp-dev-agent-gateway-tasks")
        yield {"ddb": ddb, "sqs": sqs}


def _import_handler():
    """Import the response handler module fresh."""
    for mod_name in list(sys.modules.keys()):
        if mod_name in ("handler", "routers", "routers.websocket", "routers.slack", "routers.rest"):
            del sys.modules[mod_name]
    import handler
    return handler


def _make_sqs_event(records: list[dict]) -> dict:
    """Create a synthetic SQS event for the response Lambda."""
    return {
        "Records": [
            {
                "messageId": f"msg-{i}",
                "receiptHandle": f"handle-{i}",
                "body": json.dumps(r),
                "attributes": {},
                "messageAttributes": {},
                "md5OfBody": "",
                "eventSource": "aws:sqs",
                "eventSourceARN": "arn:aws:sqs:us-east-1:123:adp-dev-agent-gateway-responses.fifo",
                "awsRegion": "us-east-1",
            }
            for i, r in enumerate(records)
        ]
    }


class TestResponseRouting:
    """Test 12: Consume SQS message, push to WS."""

    def test_webchat_response_routes_to_websocket(self, mocked_aws_services):
        handler = _import_handler()

        # Mock the WebSocket router
        mock_ws = MagicMock()
        mock_ws.route.return_value = True
        handler.ws_router = mock_ws

        event = _make_sqs_event([{
            "task_id": "task-001",
            "session_id": "sess-001",
            "thread_id": "thr-001",
            "connection_id": "conn-001",
            "channel": "webchat",
            "channel_metadata": {"connection_id": "conn-001"},
            "result": "Here is the analysis result.",
            "status": "completed",
            "completed_at": int(time.time()),
        }])

        result = handler.lambda_handler(event, None)

        assert result.get("statusCode") == 200 or "batchItemFailures" not in result
        # Verify the WS router was called with the right content
        mock_ws.route.assert_called_once()
        call_args = mock_ws.route.call_args
        assert "Here is the analysis result." in call_args[0][0]  # content
        assert call_args[0][1].get("connection_id") == "conn-001"  # metadata
        assert call_args[0][2] == "task-001"  # task_id


class TestOwnerPrincipalPropagation:
    """#5660 (A07): the enqueue-time owner must reach the router.

    The router refuses to let a session row redirect delivery unless the row's
    owner matches the task's. That check is inert if the owner never arrives, so
    propagation is asserted here rather than assumed.
    """

    def _route_metadata(self, mocked_aws_services, record: dict) -> dict:
        handler = _import_handler()
        mock_ws = MagicMock()
        mock_ws.route.return_value = True
        handler.ws_router = mock_ws
        handler.lambda_handler(_make_sqs_event([record]), None)
        mock_ws.route.assert_called_once()
        return mock_ws.route.call_args[0][1]

    def test_top_level_owner_principal_reaches_the_router(self, mocked_aws_services):
        metadata = self._route_metadata(mocked_aws_services, {
            "task_id": "task-001",
            "session_id": "sess-001",
            "connection_id": "conn-001",
            "channel": "webchat",
            "channel_metadata": {"connection_id": "conn-001"},
            "owner_principal": '["org-a","org-a","team-a","user-a","webchat"]',
            "text": "Reply",
            "status": "completed",
        })
        assert metadata.get("owner_principal") == '["org-a","org-a","team-a","user-a","webchat"]'

    def test_owner_in_channel_metadata_is_not_trusted(self, mocked_aws_services):
        metadata = self._route_metadata(mocked_aws_services, {
            "task_id": "task-002",
            "session_id": "sess-002",
            "connection_id": "conn-002",
            "channel": "webchat",
            "channel_metadata": {
                "connection_id": "conn-002",
                "owner_principal": '["org-b","org-b","team-b","user-b","webchat"]',
            },
            "text": "Reply",
            "status": "completed",
        })
        assert "owner_principal" not in metadata

    def test_absent_owner_principal_is_not_fabricated(self, mocked_aws_services):
        metadata = self._route_metadata(mocked_aws_services, {
            "task_id": "task-003",
            "session_id": "sess-003",
            "connection_id": "conn-003",
            "channel": "webchat",
            "channel_metadata": {"connection_id": "conn-003"},
            "text": "Reply",
            "status": "completed",
        })
        assert "owner_principal" not in metadata


class TestOwnerGatedPersistence:
    OWNER = '["org-a","org-a","team-a","user-a","webchat"]'
    GENERATION = 100

    def _seed(self, mocked_aws_services):
        table = mocked_aws_services["ddb"].Table("adp-dev-agent-gateway-sessions")
        table.put_item(Item={
            "session_id": "sess-owned",
            "owner_principal": self.OWNER,
            "created_at": self.GENERATION,
            "messages": [],
            "threads": {},
        })
        return table

    def test_mismatched_owner_cannot_persist_response(self, mocked_aws_services):
        table = self._seed(mocked_aws_services)
        handler = _import_handler()
        handler.ws_router = MagicMock()

        handler.lambda_handler(_make_sqs_event([{
            "task_id": "task-other",
            "session_id": "sess-owned",
            "connection_id": "conn-other",
            "channel": "webchat",
            "owner_principal": '["org-b","org-b","team-b","user-b","webchat"]',
            "session_generation": self.GENERATION,
            "text": "private response",
            "status": "completed",
        }]), None)

        row = table.get_item(Key={"session_id": "sess-owned"})["Item"]
        assert row["messages"] == []
        assert "last_response" not in row

    def test_same_owner_old_generation_cannot_persist_response(self, mocked_aws_services):
        table = self._seed(mocked_aws_services)
        handler = _import_handler()
        handler.ws_router = MagicMock()

        handler.lambda_handler(_make_sqs_event([{
            "task_id": "task-stale",
            "session_id": "sess-owned",
            "connection_id": "conn-old",
            "channel": "webchat",
            "owner_principal": self.OWNER,
            "session_generation": self.GENERATION - 1,
            "text": "stale response",
            "status": "completed",
        }]), None)

        row = table.get_item(Key={"session_id": "sess-owned"})["Item"]
        assert row["messages"] == []
        assert "last_response" not in row

    def test_matching_owner_persists_response(self, mocked_aws_services):
        table = self._seed(mocked_aws_services)
        handler = _import_handler()
        handler.ws_router = MagicMock()

        handler.lambda_handler(_make_sqs_event([{
            "task_id": "task-own",
            "session_id": "sess-owned",
            "connection_id": "conn-own",
            "channel": "webchat",
            "owner_principal": self.OWNER,
            "session_generation": self.GENERATION,
            "text": "authorized response",
            "status": "completed",
        }]), None)

        row = table.get_item(Key={"session_id": "sess-owned"})["Item"]
        assert row["messages"][0]["content"] == "authorized response"


class TestOwnedResponseBookkeeping:
    OWNER = '["org-a","org-a","team-a","user-a","webchat"]'
    GENERATION = 100

    def test_recreated_session_is_refused_before_queue_or_update(self, mocked_aws_services):
        handler = _import_handler()
        table = MagicMock()
        table.get_item.return_value = {
            "Item": {
                "owner_principal": '["org-b","org-b","team-b","user-b","webchat"]',
                "created_at": 200,
                "last_response_task_id": "task-old",
                "threads": {
                    "thread-1": {
                        "messages": [{"role": "user", "content": "do not consume"}],
                    }
                },
            }
        }
        handler.sessions_table = table
        handler.sqs = MagicMock()

        handler._check_thread_and_reenqueue(
            "sess-reused", "thread-1", {}, 123, self.OWNER,
            self.GENERATION, "task-old",
        )

        assert table.get_item.call_args.kwargs["ConsistentRead"] is True
        table.update_item.assert_not_called()
        handler.sqs.send_message.assert_not_called()

    def test_recreation_between_read_and_claim_has_no_queue_side_effect(self, mocked_aws_services):
        handler = _import_handler()
        table = MagicMock()
        table.get_item.return_value = {
            "Item": {
                "owner_principal": self.OWNER,
                "created_at": self.GENERATION,
                "last_response_task_id": "task-old",
                "connection_id": "conn-old",
                "channel": "webchat",
                "threads": {
                    "thread-1": {
                        "persona": "developer",
                        "messages": [{"role": "user", "content": "pending"}],
                    }
                },
            }
        }
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}},
            "UpdateItem",
        )
        handler.sessions_table = table
        handler.sqs = MagicMock()

        handler._check_thread_and_reenqueue(
            "sess-reused", "thread-1", {}, 123, self.OWNER,
            self.GENERATION, "task-old",
        )

        condition = table.update_item.call_args.kwargs["ConditionExpression"]
        assert "owner_principal = :owner" in condition
        assert "created_at = :generation" in condition
        assert "last_response_task_id = :response_task" in condition
        handler.sqs.send_message.assert_not_called()

    def test_message_and_lock_clears_are_owner_and_response_conditioned(self, mocked_aws_services):
        handler = _import_handler()
        table = MagicMock()
        handler.sessions_table = table

        assert handler._clear_thread_messages(
            "sess-1", "thread-1", self.OWNER, self.GENERATION,
            "task-old", "task-next",
        )
        message_clear = table.update_item.call_args.kwargs
        assert "owner_principal = :owner" in message_clear["ConditionExpression"]
        assert "created_at = :generation" in message_clear["ConditionExpression"]
        assert "last_response_task_id = :response_task" in message_clear["ConditionExpression"]
        assert "processing_task_id = :processing_task" in message_clear["ConditionExpression"]

        table.reset_mock()
        assert handler._clear_session_processing(
            "sess-1", self.OWNER, self.GENERATION, "task-old",
        )
        session_clear = table.update_item.call_args.kwargs
        assert "owner_principal = :owner" in session_clear["ConditionExpression"]
        assert "created_at = :generation" in session_clear["ConditionExpression"]
        assert "last_response_task_id = :response_task" in session_clear["ConditionExpression"]


class TestContentExtraction:
    """Issue #89: Verify content extraction works for all worker payload shapes."""

    def test_ts_worker_completed_payload_uses_text_field(self, mocked_aws_services):
        """TS chat-agent sends {text, status:'completed'} — content must come from `text`."""
        handler = _import_handler()

        mock_ws = MagicMock()
        mock_ws.route.return_value = True
        handler.ws_router = mock_ws

        event = _make_sqs_event([{
            "task_id": "task-ts-001",
            "session_id": "sess-ts-001",
            "thread_id": "thr-001",
            "connection_id": "conn-001",
            "channel": "webchat",
            "text": "Here is the full 4792-char analysis from the TS worker.",
            "status": "completed",
        }])

        result = handler.lambda_handler(event, None)

        assert result.get("statusCode") == 200 or "batchItemFailures" not in result
        mock_ws.route.assert_called_once()
        call_args = mock_ws.route.call_args
        assert call_args[0][0] == "Here is the full 4792-char analysis from the TS worker."
        assert call_args[0][1].get("status") == "completed"

    def test_ts_worker_failed_payload_uses_text_field(self, mocked_aws_services):
        """TS chat-agent sends {text, status:'failed'} — content must come from `text`."""
        handler = _import_handler()

        mock_ws = MagicMock()
        mock_ws.route.return_value = True
        handler.ws_router = mock_ws

        event = _make_sqs_event([{
            "task_id": "task-ts-002",
            "session_id": "sess-ts-002",
            "thread_id": "thr-001",
            "connection_id": "conn-002",
            "channel": "webchat",
            "text": "Error: something went wrong",
            "status": "failed",
        }])

        result = handler.lambda_handler(event, None)

        assert result.get("statusCode") == 200 or "batchItemFailures" not in result
        mock_ws.route.assert_called_once()
        assert mock_ws.route.call_args[0][0] == "Error: something went wrong"

    def test_legacy_python_worker_uses_result_field(self, mocked_aws_services):
        """Legacy Python worker sends {result, status:'completed'} — still works."""
        handler = _import_handler()

        mock_ws = MagicMock()
        mock_ws.route.return_value = True
        handler.ws_router = mock_ws

        event = _make_sqs_event([{
            "task_id": "task-py-001",
            "session_id": "sess-py-001",
            "connection_id": "conn-003",
            "channel": "webchat",
            "result": "Legacy Python worker result.",
            "status": "completed",
        }])

        handler.lambda_handler(event, None)

        mock_ws.route.assert_called_once()
        assert mock_ws.route.call_args[0][0] == "Legacy Python worker result."

    def test_progress_frame_still_uses_text_field(self, mocked_aws_services):
        """Progress frames use `text` — no regression from the fix."""
        handler = _import_handler()

        mock_ws = MagicMock()
        mock_ws.route.return_value = True
        handler.ws_router = mock_ws

        event = _make_sqs_event([{
            "task_id": "task-prog-001",
            "session_id": "sess-prog-001",
            "connection_id": "conn-004",
            "channel": "webchat",
            "text": "Searching codebase...",
            "status": "progress",
            "kind": "tool_use",
            "turn": 1,
        }])

        handler.lambda_handler(event, None)

        mock_ws.route.assert_called_once()
        assert mock_ws.route.call_args[0][0] == "Searching codebase..."
        assert mock_ws.route.call_args[0][1].get("response_type") == "progress"

    def test_content_field_fallback(self, mocked_aws_services):
        """Generic payload with `content` field works as last fallback."""
        handler = _import_handler()

        mock_ws = MagicMock()
        mock_ws.route.return_value = True
        handler.ws_router = mock_ws

        event = _make_sqs_event([{
            "task_id": "task-gen-001",
            "session_id": "sess-gen-001",
            "connection_id": "conn-005",
            "channel": "webchat",
            "content": "Generic content field.",
            "status": "completed",
        }])

        handler.lambda_handler(event, None)

        mock_ws.route.assert_called_once()
        assert mock_ws.route.call_args[0][0] == "Generic content field."

    def test_text_takes_priority_over_result_and_content(self, mocked_aws_services):
        """When multiple fields are present, `text` wins."""
        handler = _import_handler()

        mock_ws = MagicMock()
        mock_ws.route.return_value = True
        handler.ws_router = mock_ws

        event = _make_sqs_event([{
            "task_id": "task-multi-001",
            "session_id": "sess-multi-001",
            "connection_id": "conn-006",
            "channel": "webchat",
            "text": "Text field wins",
            "result": "Result field loses",
            "content": "Content field loses",
            "status": "completed",
        }])

        handler.lambda_handler(event, None)

        mock_ws.route.assert_called_once()
        assert mock_ws.route.call_args[0][0] == "Text field wins"


class TestStaleConnection:
    """Test 13: Stale connection does not 500."""

    def test_gone_connection_handled_gracefully(self, mocked_aws_services):
        handler = _import_handler()

        # Mock WebSocket router to return False (connection gone)
        mock_ws = MagicMock()
        mock_ws.route.return_value = False
        handler.ws_router = mock_ws

        event = _make_sqs_event([{
            "task_id": "task-stale",
            "session_id": "sess-stale",
            "thread_id": "",
            "connection_id": "conn-gone",
            "channel": "webchat",
            "result": "Some result",
            "status": "completed",
            "completed_at": int(time.time()),
        }])

        result = handler.lambda_handler(event, None)

        # Should NOT have failures — the stale connection is handled, not an error
        failures = result.get("batchItemFailures", [])
        assert len(failures) == 0


class TestMalformedMessage:
    """Test 14: Malformed SQS message -> batchItemFailures."""

    def test_invalid_json_body_reported_as_failure(self, mocked_aws_services):
        handler = _import_handler()

        event = {
            "Records": [
                {
                    "messageId": "msg-bad",
                    "receiptHandle": "handle-bad",
                    "body": "this is not valid json{{{",
                    "attributes": {},
                    "messageAttributes": {},
                    "md5OfBody": "",
                    "eventSource": "aws:sqs",
                    "eventSourceARN": "arn:aws:sqs:us-east-1:123:adp-dev-agent-gateway-responses.fifo",
                    "awsRegion": "us-east-1",
                }
            ]
        }

        result = handler.lambda_handler(event, None)
        # Invalid JSON should be reported as a batch failure (goes to DLQ)
        failures = result.get("batchItemFailures", [])
        assert len(failures) == 1
        assert failures[0]["itemIdentifier"] == "msg-bad"

    def test_missing_fields_handled_gracefully(self, mocked_aws_services):
        handler = _import_handler()

        # Mock the WS router to avoid real API calls
        mock_ws = MagicMock()
        mock_ws.route.return_value = True
        handler.ws_router = mock_ws

        event = _make_sqs_event([{
            # Minimal message — missing many fields
            "result": "partial result",
        }])

        result = handler.lambda_handler(event, None)
        # Should not crash — missing fields use defaults
        failures = result.get("batchItemFailures", [])
        assert len(failures) == 0
