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
                "eventSourceARN": "arn:aws:sqs:us-east-1:123:adp-dev-agent-gateway-responses",
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
                    "eventSourceARN": "arn:aws:sqs:us-east-1:123:adp-dev-agent-gateway-responses",
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
