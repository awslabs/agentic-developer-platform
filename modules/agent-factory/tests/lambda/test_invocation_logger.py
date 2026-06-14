"""
Unit tests for Phase 4 — Non-GitHub invocation capture (Slack + WebChat).

Tests from the issue validation section:
  1. Slack trigger -> row written with channel="slack", resolved user_id,
     topic from text, status="webhook_received"
  2. WebChat trigger -> row with channel="webchat", user_id=cognito_sub
  3. Writer raising -> does NOT propagate out of handle_long_running
     (message still enqueued)
  4. Unresolved Slack user -> no row (message not enqueued, magic-link issued)
  5. event_id/arrived_at written match the SQS envelope values
"""

from __future__ import annotations

import io
import json
import os
import sys
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws


# ---------------------------------------------------------------------------
# Helpers to import the handler with mocked env / boto3
# ---------------------------------------------------------------------------

HANDLER_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "gateway", "lambdas", "ingest")


@pytest.fixture(autouse=True)
def _patch_sys_path():
    """Add the ingest Lambda directory to sys.path so handler.py can be imported."""
    original = sys.path.copy()
    sys.path.insert(0, HANDLER_DIR)
    yield
    sys.path = original


@pytest.fixture
def mock_env(monkeypatch):
    """Set required environment variables for the ingest handler."""
    monkeypatch.setenv(
        "INPUT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/adp-dev-agent-gateway-tasks"
    )
    monkeypatch.setenv(
        "RESPONSE_QUEUE_URL",
        "https://sqs.us-east-1.amazonaws.com/123/adp-dev-agent-gateway-responses.fifo",
    )
    monkeypatch.setenv("SESSIONS_TABLE_NAME", "adp-dev-agent-gateway-sessions")
    monkeypatch.setenv("AWS_REGION_NAME", "us-east-1")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "")
    monkeypatch.setenv("SLACK_BOT_USER_ID", "")
    monkeypatch.setenv("WEBHOOK_EVENTS_TABLE", "adp-dev-webhook-events")


def _make_bedrock_response(classification: dict) -> dict:
    """Create a mock Bedrock invoke_model return value."""
    body_bytes = json.dumps(
        {
            "content": [{"type": "text", "text": json.dumps(classification)}],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
    ).encode()
    return {"body": io.BytesIO(body_bytes)}


@pytest.fixture
def mocked_aws_services(mock_env):
    """Spin up moto DynamoDB + SQS with the webhook-events table."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        # Sessions table (existing)
        ddb.create_table(
            TableName="adp-dev-agent-gateway-sessions",
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # Webhook events table (Phase 1)
        events_table = ddb.create_table(
            TableName="adp-dev-webhook-events",
            KeySchema=[
                {"AttributeName": "event_id", "KeyType": "HASH"},
                {"AttributeName": "arrived_at", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "event_id", "AttributeType": "S"},
                {"AttributeName": "arrived_at", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # Create SQS queues
        sqs_client = boto3.client("sqs", region_name="us-east-1")
        sqs_client.create_queue(QueueName="adp-dev-agent-gateway-tasks")
        sqs_client.create_queue(
            QueueName="adp-dev-agent-gateway-responses.fifo",
            Attributes={"FifoQueue": "true"},
        )

        yield {"ddb": ddb, "events_table": events_table, "sqs": sqs_client}


def _import_handler(mock_bedrock=None):
    """Import the handler module fresh (after env/path setup)."""
    for mod_name in list(sys.modules.keys()):
        if mod_name in (
            "handler",
            "classifier",
            "channels",
            "channels.base",
            "channels.webchat",
            "channels.slack",
            "github_dispatch",
            "invocation_logger",
            "user_resolver",
        ):
            del sys.modules[mod_name]

    import handler

    if mock_bedrock is not None:
        import classifier

        classifier._bedrock_client = mock_bedrock

    return handler


def _get_invocation_rows(ddb):
    """Scan the webhook-events table and return all items."""
    table = ddb.Table("adp-dev-webhook-events")
    resp = table.scan()
    return resp.get("Items", [])


# ===========================================================================
# Tests
# ===========================================================================


class TestSlackInvocationCapture:
    """Slack trigger -> invocation row written with correct fields."""

    def test_slack_long_running_writes_invocation_row(self, mocked_aws_services):
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response(
            {
                "path": "long_running",
                "persona": "developer",
                "response": None,
                "thread_action": "new",
                "reasoning": "Code task from Slack",
            }
        )

        handler = _import_handler(mock_bedrock=mock_bedrock)

        # Simulate a Slack message arriving (bypass signature verification by
        # calling handle_unified_message directly with a Slack-typed message)
        from channels.base import ChannelType, UnifiedMessage

        message = UnifiedMessage(
            message_id="slack-msg-uuid-001",
            channel=ChannelType.SLACK,
            channel_id="C12345",
            user_id="usr-resolved-42",
            user_name="alice",
            text="Please fix the login bug in the auth module",
            platform_data={"team_id": "T001", "tenant_id": "acme-corp"},
        )

        result = handler.handle_unified_message(message)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "processing"

        # Verify invocation row was written
        rows = _get_invocation_rows(mocked_aws_services["ddb"])
        assert len(rows) == 1

        row = rows[0]
        assert row["event_id"] == "slack-msg-uuid-001"
        assert row["channel"] == "slack"
        assert row["user_id"] == "usr-resolved-42"
        assert row["status"] == "webhook_received"
        assert row["persona"] == "developer"
        assert "login bug" in row["topic"]
        assert row["event_type"] == "chat_message"
        assert row["action"] == "invoke"
        # arrived_at should be ISO format
        assert "T" in row["arrived_at"] and row["arrived_at"].endswith("Z")


class TestWebChatInvocationCapture:
    """WebChat trigger -> row with channel="webchat", user_id=cognito_sub."""

    def test_webchat_long_running_writes_invocation_row(self, mocked_aws_services):
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response(
            {
                "path": "long_running",
                "persona": "architect",
                "response": None,
                "thread_action": "new",
                "reasoning": "Architecture analysis requested",
            }
        )

        handler = _import_handler(mock_bedrock=mock_bedrock)

        from channels.base import ChannelType, UnifiedMessage

        message = UnifiedMessage(
            message_id="webchat-msg-uuid-002",
            channel=ChannelType.WEBCHAT,
            channel_id="ws-channel-1",
            user_id="cognito-sub-abc123",
            user_name="bob",
            text="Review the database schema for the new feature",
            platform_data={"connection_id": "conn-99", "tenant_id": "acme-corp"},
        )

        result = handler.handle_unified_message(message)

        assert result["statusCode"] == 200

        rows = _get_invocation_rows(mocked_aws_services["ddb"])
        assert len(rows) == 1

        row = rows[0]
        assert row["event_id"] == "webchat-msg-uuid-002"
        assert row["channel"] == "webchat"
        assert row["user_id"] == "cognito-sub-abc123"
        assert row["status"] == "webhook_received"
        assert row["persona"] == "architect"
        assert row["tenant_id"] == "acme-corp"


class TestWriterFailureDoesNotPropagate:
    """Writer raising does NOT propagate — message still enqueued."""

    def test_ddb_error_does_not_block_message_handling(self, mocked_aws_services):
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response(
            {
                "path": "long_running",
                "persona": "developer",
                "response": None,
                "thread_action": "new",
                "reasoning": "Task",
            }
        )

        handler = _import_handler(mock_bedrock=mock_bedrock)

        from channels.base import ChannelType, UnifiedMessage

        message = UnifiedMessage(
            message_id="msg-uuid-fail-test",
            channel=ChannelType.SLACK,
            channel_id="C12345",
            user_id="usr-1",
            user_name="charlie",
            text="Do some work",
            platform_data={"tenant_id": "t1"},
        )

        # Patch log_invocation to raise
        import invocation_logger

        original_log = invocation_logger.log_invocation
        invocation_logger.log_invocation = MagicMock(side_effect=Exception("DDB is down"))

        try:
            result = handler.handle_unified_message(message)
            # Should still succeed — message was enqueued
            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert body["status"] == "processing"
        finally:
            invocation_logger.log_invocation = original_log

        # Verify SQS message was still sent
        sqs = mocked_aws_services["sqs"]
        queue_url = sqs.get_queue_url(QueueName="adp-dev-agent-gateway-tasks")["QueueUrl"]
        msgs = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10)
        assert len(msgs.get("Messages", [])) >= 1


class TestUnresolvedUserNoRow:
    """Unresolved Slack user -> no invocation row (no run was started).

    The handler returns early with a magic-link response when user resolution
    returns UnresolvedUser — the message is NOT enqueued and no invocation row
    is written. We test this by calling handle_unified_message indirectly via
    lambda_handler with identity resolution enabled and resolve_user mocked.
    """

    def test_unresolved_user_does_not_write_row(self, mocked_aws_services, monkeypatch):
        """When user resolution returns UnresolvedUser, no invocation row is written."""
        monkeypatch.setenv("ENABLE_USER_IDENTITIES", "1")
        monkeypatch.setenv("RESOLVER_BASE_URL", "http://fake-resolver:8080")

        handler = _import_handler()

        from channels.base import ChannelType, UnifiedMessage
        from user_resolver import UnresolvedUser

        message = UnifiedMessage(
            message_id="msg-unresolved-001",
            channel=ChannelType.SLACK,
            channel_id="C12345",
            user_id="",
            user_name="unknown-user",
            text="Help me",
            platform_data={"team_id": "T001"},
            provider="slack",
            provider_user_id="U_UNKNOWN",
        )

        # Patch the handler module's imported resolve_user to return UnresolvedUser
        original_resolve = handler.resolve_user
        handler.resolve_user = MagicMock(
            return_value=UnresolvedUser(magic_link_url="https://example.com/link")
        )
        # Also patch the ENABLE_USER_IDENTITIES flag on the handler module
        original_flag = handler.ENABLE_USER_IDENTITIES
        handler.ENABLE_USER_IDENTITIES = True

        # Bypass the Slack adapter's signature verification and parse_event
        slack_adapter = handler.ADAPTERS["slack"]
        original_verify = slack_adapter.verify_request
        original_parse = slack_adapter.parse_event
        slack_adapter.verify_request = MagicMock(return_value=True)
        slack_adapter.parse_event = MagicMock(return_value=message)

        try:
            result = handler.lambda_handler(
                {
                    "headers": {
                        "x-slack-signature": "v0=fake",
                        "x-slack-request-timestamp": "9999999999",
                    },
                    "body": json.dumps(
                        {
                            "type": "event_callback",
                            "event": {
                                "type": "message",
                                "text": "Help me",
                                "user": "U_UNKNOWN",
                                "channel": "C12345",
                                "ts": "123.456",
                            },
                        }
                    ),
                },
                None,
            )
        finally:
            handler.resolve_user = original_resolve
            handler.ENABLE_USER_IDENTITIES = original_flag
            slack_adapter.verify_request = original_verify
            slack_adapter.parse_event = original_parse

        # The handler returns 200 with unresolved_user status
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body.get("status") == "unresolved_user"

        # No invocation row should be written
        rows = _get_invocation_rows(mocked_aws_services["ddb"])
        assert len(rows) == 0


class TestEventIdArrivedAtMatchSqsEnvelope:
    """event_id/arrived_at written to DDB match the SQS envelope values."""

    def test_key_contract_alignment(self, mocked_aws_services):
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response(
            {
                "path": "long_running",
                "persona": "developer",
                "response": None,
                "thread_action": "new",
                "reasoning": "Task",
            }
        )

        handler = _import_handler(mock_bedrock=mock_bedrock)

        from channels.base import ChannelType, UnifiedMessage

        message = UnifiedMessage(
            message_id="key-contract-uuid-99",
            channel=ChannelType.WEBCHAT,
            channel_id="ws-1",
            user_id="cognito-sub-xyz",
            user_name="dana",
            text="Analyze this code",
            platform_data={"connection_id": "conn-77", "tenant_id": "t1"},
        )

        result = handler.handle_unified_message(message)
        assert result["statusCode"] == 200

        # Read the SQS message to extract envelope values
        sqs = mocked_aws_services["sqs"]
        queue_url = sqs.get_queue_url(QueueName="adp-dev-agent-gateway-tasks")["QueueUrl"]
        msgs = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1)
        assert msgs.get("Messages")
        sqs_body = json.loads(msgs["Messages"][0]["Body"])

        # Read the DDB row
        rows = _get_invocation_rows(mocked_aws_services["ddb"])
        assert len(rows) == 1
        row = rows[0]

        # THE KEY CONTRACT: DDB event_id == SQS message_id, DDB arrived_at == SQS arrived_at
        assert row["event_id"] == sqs_body["message_id"]
        assert row["arrived_at"] == sqs_body["arrived_at"]
        assert row["event_id"] == "key-contract-uuid-99"


class TestDirectResponseNoInvocationRow:
    """direct_response path should NOT write an invocation row (no long-running work)."""

    def test_direct_response_skips_invocation_logging(self, mocked_aws_services):
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response(
            {
                "path": "direct_response",
                "persona": "developer",
                "response": "Hello! I can help.",
                "thread_action": "none",
                "reasoning": "Simple greeting",
            }
        )

        handler = _import_handler(mock_bedrock=mock_bedrock)

        from channels.base import ChannelType, UnifiedMessage

        message = UnifiedMessage(
            message_id="direct-msg-001",
            channel=ChannelType.WEBCHAT,
            channel_id="ws-1",
            user_id="user-1",
            user_name="eve",
            text="Hello!",
            platform_data={"connection_id": "conn-1"},
        )

        result = handler.handle_unified_message(message)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "completed"

        # No invocation row — direct responses don't start an agent run
        rows = _get_invocation_rows(mocked_aws_services["ddb"])
        assert len(rows) == 0


class TestTopicFallback:
    """Empty text should produce '(untitled)' topic."""

    def test_empty_text_uses_untitled_fallback(self, mocked_aws_services):
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response(
            {
                "path": "long_running",
                "persona": "developer",
                "response": None,
                "thread_action": "new",
                "reasoning": "Task",
            }
        )

        handler = _import_handler(mock_bedrock=mock_bedrock)

        from channels.base import ChannelType, UnifiedMessage

        # Message with empty text (edge case — classifier still routes it)
        message = UnifiedMessage(
            message_id="empty-text-msg-001",
            channel=ChannelType.SLACK,
            channel_id="C1",
            user_id="usr-1",
            user_name="frank",
            text="",
            platform_data={"tenant_id": "t1"},
        )

        handler.handle_unified_message(message)

        rows = _get_invocation_rows(mocked_aws_services["ddb"])
        assert len(rows) == 1
        assert rows[0]["topic"] == "(untitled)"
