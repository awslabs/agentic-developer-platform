"""
Unit tests for append_message dedupe and empty-content guards.

Bug 3 from issue #68:
- append_message should reject empty/whitespace-only content.
- append_message should deduplicate identical messages (same role + content
  within 5 seconds).
- The response Lambda's _append_response is the canonical path for final
  replies; no double-writes.
"""

from __future__ import annotations

import json
import os
import sys
import time
from decimal import Decimal
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from tests.conftest import mock_apigw_event

HANDLER_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "gateway", "lambdas", "ingest"
)


@pytest.fixture(autouse=True)
def _patch_sys_path():
    original = sys.path.copy()
    sys.path.insert(0, HANDLER_DIR)
    yield
    sys.path = original


@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("INPUT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/adp-dev-agent-gateway-tasks")
    monkeypatch.setenv("RESPONSE_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/adp-dev-agent-gateway-responses")
    monkeypatch.setenv("SESSIONS_TABLE_NAME", "adp-dev-agent-gateway-sessions")
    monkeypatch.setenv("AWS_REGION_NAME", "us-east-1")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "")
    monkeypatch.setenv("SLACK_BOT_USER_ID", "")


@pytest.fixture
def mocked_aws_services(mock_env):
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="adp-dev-agent-gateway-sessions",
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        sqs_client = boto3.client("sqs", region_name="us-east-1")
        sqs_client.create_queue(QueueName="adp-dev-agent-gateway-tasks")
        sqs_client.create_queue(QueueName="adp-dev-agent-gateway-responses")
        yield {"ddb": ddb, "table": table, "sqs": sqs_client}


def _import_handler(mock_bedrock=None):
    for mod_name in list(sys.modules.keys()):
        if mod_name in ("handler", "classifier", "channels", "channels.base",
                        "channels.webchat", "channels.slack", "github_dispatch"):
            del sys.modules[mod_name]
    import handler
    if mock_bedrock is not None:
        import classifier
        classifier._bedrock_client = mock_bedrock
    return handler


def _get_session_messages(table, session_id):
    resp = table.get_item(Key={"session_id": session_id})
    return resp.get("Item", {}).get("messages", [])


class TestAppendMessageEmptyGuard:
    """append_message with empty content should no-op."""

    def test_empty_string_rejected(self, mocked_aws_services):
        handler = _import_handler()
        table = mocked_aws_services["table"]

        # Create a session row first
        table.put_item(Item={"session_id": "sess-empty", "messages": []})

        handler.append_message("sess-empty", "assistant", "", int(time.time()))

        messages = _get_session_messages(table, "sess-empty")
        assert len(messages) == 0

    def test_whitespace_only_rejected(self, mocked_aws_services):
        handler = _import_handler()
        table = mocked_aws_services["table"]

        table.put_item(Item={"session_id": "sess-ws", "messages": []})

        handler.append_message("sess-ws", "assistant", "   \n\t  ", int(time.time()))

        messages = _get_session_messages(table, "sess-ws")
        assert len(messages) == 0

    def test_none_content_rejected(self, mocked_aws_services):
        handler = _import_handler()
        table = mocked_aws_services["table"]

        table.put_item(Item={"session_id": "sess-none", "messages": []})

        handler.append_message("sess-none", "assistant", None, int(time.time()))

        messages = _get_session_messages(table, "sess-none")
        assert len(messages) == 0

    def test_valid_content_accepted(self, mocked_aws_services):
        handler = _import_handler()
        table = mocked_aws_services["table"]

        table.put_item(Item={"session_id": "sess-valid", "messages": []})

        handler.append_message("sess-valid", "assistant", "Hello!", int(time.time()))

        messages = _get_session_messages(table, "sess-valid")
        assert len(messages) == 1
        assert messages[0]["content"] == "Hello!"


class TestAppendMessageDedupe:
    """Identical messages within 5s should be deduped."""

    def test_duplicate_within_5s_produces_one_row(self, mocked_aws_services):
        handler = _import_handler()
        table = mocked_aws_services["table"]

        table.put_item(Item={"session_id": "sess-dup", "messages": []})

        now = int(time.time())
        handler.append_message("sess-dup", "assistant", "On it! Working on your request.", now)
        handler.append_message("sess-dup", "assistant", "On it! Working on your request.", now)

        messages = _get_session_messages(table, "sess-dup")
        assert len(messages) == 1

    def test_different_content_not_deduped(self, mocked_aws_services):
        handler = _import_handler()
        table = mocked_aws_services["table"]

        table.put_item(Item={"session_id": "sess-diff", "messages": []})

        now = int(time.time())
        handler.append_message("sess-diff", "assistant", "Message A", now)
        handler.append_message("sess-diff", "assistant", "Message B", now)

        messages = _get_session_messages(table, "sess-diff")
        assert len(messages) == 2

    def test_different_role_not_deduped(self, mocked_aws_services):
        handler = _import_handler()
        table = mocked_aws_services["table"]

        table.put_item(Item={"session_id": "sess-role", "messages": []})

        now = int(time.time())
        handler.append_message("sess-role", "user", "Hello", now)
        handler.append_message("sess-role", "assistant", "Hello", now)

        messages = _get_session_messages(table, "sess-role")
        assert len(messages) == 2

    def test_same_content_after_6s_not_deduped(self, mocked_aws_services):
        handler = _import_handler()
        table = mocked_aws_services["table"]

        table.put_item(Item={"session_id": "sess-time", "messages": []})

        now = int(time.time())
        handler.append_message("sess-time", "assistant", "Same message", now - 6)
        handler.append_message("sess-time", "assistant", "Same message", now)

        messages = _get_session_messages(table, "sess-time")
        assert len(messages) == 2


class TestLongRunningNoDoubleAck:
    """long_running with escalation_note should produce exactly one ack message."""

    def _make_bedrock_response(self, classification: dict) -> dict:
        import io
        body_bytes = json.dumps({
            "content": [{"type": "text", "text": json.dumps(classification)}],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }).encode()
        return {"body": io.BytesIO(body_bytes)}

    def test_escalation_note_appended_once(self, mocked_aws_services):
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = self._make_bedrock_response({
            "path": "long_running",
            "persona": "developer",
            "response": None,
            "thread_action": "new",
            "reasoning": "Complex analysis needed",
            "escalation_note": "Working on your analysis now!",
        })

        handler = _import_handler(mock_bedrock=mock_bedrock)
        table = mocked_aws_services["table"]

        event = mock_apigw_event(
            route_key="$default",
            body={"action": "message", "text": "Analyze the codebase", "session_id": "sess-esc"},
            connection_id="conn-esc",
            authorizer_claims={"sub": "user-esc", "email": "esc@example.com"},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200

        messages = _get_session_messages(table, "sess-esc")
        # Should have: 1 user message + 1 assistant ack (the escalation note)
        assistant_msgs = [m for m in messages if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        assert "Working on your analysis now!" in assistant_msgs[0]["content"]
