"""
Unit tests for the Ingest Lambda handler.

Tests 5-11 from the issue:
  5. $connect event with valid JWT -> DynamoDB PutItem
  6. sendMessage classified as direct_response -> Bedrock reply, no SQS
  7. sendMessage classified as long_running -> SQS enqueue, no immediate reply
  8. Both action:"message" and action:"sendMessage" accepted (PR #9 regression)
  9. Malformed payload -> 400 structured error
 10. Classifier Bedrock failure falls through to long_running
 11. $disconnect event removes session
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

from tests.conftest import mock_apigw_event


# ---------------------------------------------------------------------------
# Helpers to import the handler with mocked env / boto3
# ---------------------------------------------------------------------------

HANDLER_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "gateway", "lambdas", "ingest"
)


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
    monkeypatch.setenv("INPUT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/adp-dev-agent-gateway-tasks")
    monkeypatch.setenv("RESPONSE_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/adp-dev-agent-gateway-responses.fifo")
    monkeypatch.setenv("SESSIONS_TABLE_NAME", "adp-dev-agent-gateway-sessions")
    monkeypatch.setenv("AWS_REGION_NAME", "us-east-1")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "")
    monkeypatch.setenv("SLACK_BOT_USER_ID", "")


def _make_bedrock_response(classification: dict) -> dict:
    """Create a mock Bedrock invoke_model return value."""
    body_bytes = json.dumps({
        "content": [{"type": "text", "text": json.dumps(classification)}],
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }).encode()
    return {"body": io.BytesIO(body_bytes)}


@pytest.fixture
def mocked_aws_services(mock_env):
    """Spin up moto DynamoDB + SQS, patch boto3 clients used by the handler."""
    with mock_aws():
        # Create DynamoDB table
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="adp-dev-agent-gateway-sessions",
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        # Create SQS queues
        sqs_client = boto3.client("sqs", region_name="us-east-1")
        sqs_client.create_queue(QueueName="adp-dev-agent-gateway-tasks")
        sqs_client.create_queue(
            QueueName="adp-dev-agent-gateway-responses.fifo",
            Attributes={"FifoQueue": "true"},
        )

        yield {"ddb": ddb, "table": table, "sqs": sqs_client}


def _import_handler(mock_bedrock=None):
    """Import the handler module fresh (after env/path setup).

    If mock_bedrock is provided, patches the classifier's Bedrock client.
    """
    # Clear any cached module imports
    for mod_name in list(sys.modules.keys()):
        if mod_name in ("handler", "classifier", "channels", "channels.base",
                        "channels.webchat", "channels.slack", "github_dispatch"):
            del sys.modules[mod_name]

    import handler  # noqa: F811

    if mock_bedrock is not None:
        # Patch the classifier's cached client directly
        import classifier
        classifier._bedrock_client = mock_bedrock

    return handler


# ===========================================================================
# Tests
# ===========================================================================


class TestConnectEvent:
    """Test 5: $connect event handling."""

    def test_connect_returns_200(self, mocked_aws_services):
        handler = _import_handler()
        event = mock_apigw_event(
            route_key="$connect",
            connection_id="conn-001",
            token="fake-jwt",
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200
        assert "Connected" in result["body"]


class TestDisconnectEvent:
    """Test 11: $disconnect event handling."""

    def test_disconnect_returns_200(self, mocked_aws_services):
        handler = _import_handler()
        event = mock_apigw_event(route_key="$disconnect", connection_id="conn-001")
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200
        assert "Disconnected" in result["body"]


class TestDirectResponsePath:
    """Test 6: sendMessage classified as direct_response."""

    def test_direct_response_returns_completed(self, mocked_aws_services):
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response({
            "path": "direct_response",
            "persona": "developer",
            "response": "Hello! I'm here to help.",
            "thread_action": "none",
            "reasoning": "Simple greeting",
        })

        handler = _import_handler(mock_bedrock=mock_bedrock)
        event = mock_apigw_event(
            route_key="$default",
            body={"action": "message", "text": "Hello!", "session_id": "sess-001"},
            connection_id="conn-001",
            authorizer_claims={"sub": "user-1", "email": "test@example.com"},
        )
        result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "completed"
        assert "task_id" in body


class TestLongRunningPath:
    """Test 7: sendMessage classified as long_running -> SQS enqueue."""

    def test_long_running_enqueues_to_sqs(self, mocked_aws_services):
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response({
            "path": "long_running",
            "persona": "developer",
            "response": None,
            "thread_action": "new",
            "reasoning": "Complex analysis needed",
        })

        handler = _import_handler(mock_bedrock=mock_bedrock)
        event = mock_apigw_event(
            route_key="$default",
            body={"action": "message", "text": "Analyze the codebase architecture", "session_id": "sess-002"},
            connection_id="conn-002",
            authorizer_claims={"sub": "user-2", "email": "test2@example.com"},
        )
        result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "processing"
        assert "thread_id" in body

        # Verify message was enqueued to SQS
        sqs = mocked_aws_services["sqs"]
        resp = sqs.receive_message(
            QueueUrl="https://sqs.us-east-1.amazonaws.com/123/adp-dev-agent-gateway-tasks",
            MaxNumberOfMessages=1,
            WaitTimeSeconds=0,
        )
        messages = resp.get("Messages", [])
        assert len(messages) >= 1
        task = json.loads(messages[0]["Body"])
        assert task["session_id"] == "sess-002"
        assert task["message"] == "Analyze the codebase architecture"


class TestFollowUpThreadReuse:
    """Regression: follow_up on an idle thread used to call create_thread,
    which clobbered messages/topic. Also: the handler only passed threads
    with a live processing_task_id to the classifier, so idle follow-ups
    were never routed as follow_ups. Tests here pin the fixed behaviour:
    follow-up on idle threads reuses the thread without clobbering, and
    both idle + processing threads are visible to the classifier."""

    def _setup_session_with_idle_thread(self, mocked_aws_services):
        """Seed the sessions table with an idle long_running thread."""
        table = mocked_aws_services["table"]
        table.put_item(Item={
            "session_id": "sess-follow",
            "user_workspace": "user-3#webchat",
            "connection_id": "conn-3",
            "channel": "webchat",
            "created_at": 1000,
            "updated_at": 1000,
            "messages": [
                {"role": "user", "content": "original topic", "timestamp": 1000},
                {"role": "assistant", "content": "original reply", "timestamp": 1001},
            ],
            "threads": {
                "thread-1": {
                    "topic": "original topic discussion",
                    "path": "long_running",
                    "persona": "developer",
                    "processing_task_id": "",  # idle — prior turn finished
                    "messages": [],
                    "created_at": 1000,
                },
            },
            "expires_at": 9999999999,
        })

    def test_follow_up_on_idle_thread_reuses_thread_id(self, mocked_aws_services):
        self._setup_session_with_idle_thread(mocked_aws_services)

        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response({
            "path": "long_running",
            "persona": "developer",
            "thread_action": "follow_up",
            "follow_up_thread_id": "thread-1",
            "reasoning": "refines the prior topic",
        })

        handler = _import_handler(mock_bedrock=mock_bedrock)
        event = mock_apigw_event(
            route_key="$default",
            body={"action": "message", "text": "tell me more about that", "session_id": "sess-follow"},
            connection_id="conn-3",
            authorizer_claims={"sub": "user-3"},
        )
        result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        # Reuses the existing thread_id, not a fresh one.
        assert body["thread_id"] == "thread-1"
        assert body["status"] == "processing"

        # And the original thread's topic/messages were NOT clobbered.
        table = mocked_aws_services["table"]
        stored = table.get_item(Key={"session_id": "sess-follow"})["Item"]
        thread = stored["threads"]["thread-1"]
        assert thread["topic"] == "original topic discussion"
        # processing_task_id is now set to the new task.
        assert thread["processing_task_id"]

    def test_classifier_sees_idle_threads_as_candidates(self, mocked_aws_services):
        """The handler should pass all recent threads — idle or not — into
        classify_message.active_threads so it can pick follow_up properly."""
        self._setup_session_with_idle_thread(mocked_aws_services)

        captured = {"active_threads": None}

        def wrap(mock_bedrock):
            orig = mock_bedrock.invoke_model

            def invoke(**kwargs):
                body = json.loads(kwargs["body"])
                # Find the user content → "Active threads:" / "idle is normal" section.
                for msg in body.get("messages", []):
                    if msg.get("role") == "user":
                        captured["active_threads"] = msg["content"]
                return orig(**kwargs)

            mock_bedrock.invoke_model = invoke
            return mock_bedrock

        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response({
            "path": "long_running",
            "persona": "developer",
            "thread_action": "new",  # value irrelevant; we're checking input
            "reasoning": "",
        })
        mock_bedrock = wrap(mock_bedrock)

        handler = _import_handler(mock_bedrock=mock_bedrock)
        event = mock_apigw_event(
            route_key="$default",
            body={"action": "message", "text": "another question", "session_id": "sess-follow"},
            connection_id="conn-3",
            authorizer_claims={"sub": "user-3"},
        )
        handler.lambda_handler(event, None)

        # The prompt must include the idle thread so the classifier can pick it.
        assert captured["active_threads"] is not None
        assert "thread-1" in captured["active_threads"]
        assert "status=idle" in captured["active_threads"]


class TestWebChatActionVariants:
    """Test 8: Both action:"message" and action:"sendMessage" accepted (PR #9 regression)."""

    @pytest.mark.parametrize("action", ["message", "sendMessage"])
    def test_accepted_actions(self, mocked_aws_services, action):
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response({
            "path": "direct_response",
            "persona": "developer",
            "response": "Got it!",
            "thread_action": "none",
            "reasoning": "Ack",
        })

        handler = _import_handler(mock_bedrock=mock_bedrock)
        event = mock_apigw_event(
            route_key="$default",
            body={"action": action, "text": "test message", "session_id": "sess-action"},
            connection_id="conn-action",
            authorizer_claims={"sub": "user-action", "email": "action@example.com"},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200


class TestMalformedPayload:
    """Test 9: Malformed payload returns 200 OK (handler gracefully ignores)."""

    def test_invalid_json_body(self, mocked_aws_services):
        handler = _import_handler()
        event = mock_apigw_event(
            route_key="$default",
            body="this is not json{{{",
            connection_id="conn-bad",
        )
        result = handler.lambda_handler(event, None)
        # The webchat adapter returns None for unparseable bodies -> handler returns 200 OK
        assert result["statusCode"] == 200

    def test_empty_text_ignored(self, mocked_aws_services):
        handler = _import_handler()
        event = mock_apigw_event(
            route_key="$default",
            body={"action": "message", "text": "", "session_id": "sess-empty"},
            connection_id="conn-empty",
            authorizer_claims={"sub": "user-empty"},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200

    def test_unknown_action_ignored(self, mocked_aws_services):
        handler = _import_handler()
        event = mock_apigw_event(
            route_key="$default",
            body={"action": "typing_indicator", "text": "ignored"},
            connection_id="conn-unknown",
            authorizer_claims={"sub": "user-unknown"},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200


class TestClassifierFailure:
    """Test 10: Classifier Bedrock failure falls through to long_running."""

    def test_bedrock_error_defaults_to_long_running(self, mocked_aws_services):
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.side_effect = Exception("Bedrock service error")

        handler = _import_handler(mock_bedrock=mock_bedrock)
        event = mock_apigw_event(
            route_key="$default",
            body={"action": "message", "text": "Do something complex", "session_id": "sess-err"},
            connection_id="conn-err",
            authorizer_claims={"sub": "user-err", "email": "err@example.com"},
        )
        result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        # Should fall through to long_running (enqueue), not crash
        assert body["status"] == "processing"


class TestExtendedClaimsPersistence:
    """Stage A (#184): Extended identity claims persistence and propagation."""

    def test_connect_persists_extended_claims(self, mocked_aws_services):
        """$connect with extended claims stores org_id, team_id, etc in DDB."""
        handler = _import_handler()
        event = mock_apigw_event(
            route_key="$connect",
            connection_id="conn-claims",
            token="fake-jwt",
        )
        # Inject extended authorizer context (as custom Cognito claims)
        event["requestContext"]["authorizer"] = {
            "claims": {
                "sub": "user-ext-1",
                "email": "ext@example.com",
                "custom:tenant_id": "acme",
                "custom:org_id": "org-42",
                "custom:team_id": "team-alpha",
                "custom:department_id": "engineering",
                "custom:account_type": "human",
                "custom:role": "admin",
            }
        }
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200

        # Verify all claims were persisted
        table = mocked_aws_services["table"]
        item = table.get_item(Key={"session_id": "conn#conn-claims"}).get("Item", {})
        assert item["sub"] == "user-ext-1"
        assert item["email"] == "ext@example.com"
        assert item["tenant_id"] == "acme"
        assert item["org_id"] == "org-42"
        assert item["team_id"] == "team-alpha"
        assert item["department_id"] == "engineering"
        assert item["account_type"] == "human"
        assert item["role"] == "admin"

    def test_connect_without_extended_claims_still_works(self, mocked_aws_services):
        """$connect without extended claims only stores sub/email/tenant (backward compat)."""
        handler = _import_handler()
        event = mock_apigw_event(
            route_key="$connect",
            connection_id="conn-legacy",
            token="fake-jwt",
        )
        event["requestContext"]["authorizer"] = {
            "claims": {
                "sub": "user-legacy-1",
                "email": "legacy@example.com",
            }
        }
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200

        table = mocked_aws_services["table"]
        item = table.get_item(Key={"session_id": "conn#conn-legacy"}).get("Item", {})
        assert item["sub"] == "user-legacy-1"
        # Extended fields should be absent (not stored as empty strings)
        assert "org_id" not in item
        assert "team_id" not in item

    def test_restore_injects_extended_claims(self, mocked_aws_services):
        """_restore_connection_claims re-injects extended claims into event."""
        handler = _import_handler()

        # First, persist extended claims via $connect
        connect_event = mock_apigw_event(
            route_key="$connect",
            connection_id="conn-restore",
            token="fake-jwt",
        )
        connect_event["requestContext"]["authorizer"] = {
            "claims": {
                "sub": "user-restore-1",
                "email": "restore@example.com",
                "custom:org_id": "org-99",
                "custom:team_id": "team-beta",
                "custom:department_id": "sales",
                "custom:account_type": "service",
                "custom:role": "viewer",
            }
        }
        handler.lambda_handler(connect_event, None)

        # Now simulate a $default event (no authorizer) and verify restore
        msg_event = mock_apigw_event(
            route_key="$default",
            body={"action": "message", "text": "test", "session_id": "sess-restore"},
            connection_id="conn-restore",
        )
        # Manually call _restore_connection_claims
        handler._restore_connection_claims(msg_event, "conn-restore")
        claims = msg_event["requestContext"]["authorizer"]["claims"]
        assert claims["sub"] == "user-restore-1"
        assert claims["custom:org_id"] == "org-99"
        assert claims["custom:team_id"] == "team-beta"
        assert claims["custom:department_id"] == "sales"
        assert claims["custom:account_type"] == "service"
        assert claims["custom:role"] == "viewer"

    def test_extended_claims_flow_to_sqs_message(self, mocked_aws_services):
        """Extended identity claims are included in the SQS message body."""
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response({
            "path": "long_running",
            "persona": "developer",
            "response": None,
            "thread_action": "new",
            "reasoning": "Needs deep work",
        })

        handler = _import_handler(mock_bedrock=mock_bedrock)

        # Persist extended claims via $connect first
        connect_event = mock_apigw_event(
            route_key="$connect",
            connection_id="conn-sqs-flow",
            token="fake-jwt",
        )
        connect_event["requestContext"]["authorizer"] = {
            "claims": {
                "sub": "user-sqs-1",
                "email": "sqs@example.com",
                "custom:org_id": "org-sqs",
                "custom:team_id": "team-sqs",
                "custom:account_type": "human",
            }
        }
        handler.lambda_handler(connect_event, None)

        # Now send a message
        msg_event = mock_apigw_event(
            route_key="$default",
            body={"action": "message", "text": "Deploy the feature", "session_id": "sess-sqs-flow"},
            connection_id="conn-sqs-flow",
        )
        result = handler.lambda_handler(msg_event, None)
        assert result["statusCode"] == 200

        # Verify SQS message contains identity fields
        sqs = mocked_aws_services["sqs"]
        resp = sqs.receive_message(
            QueueUrl="https://sqs.us-east-1.amazonaws.com/123/adp-dev-agent-gateway-tasks",
            MaxNumberOfMessages=1,
            WaitTimeSeconds=0,
        )
        messages = resp.get("Messages", [])
        assert len(messages) >= 1
        task = json.loads(messages[0]["Body"])
        assert task["user_id"] == "user-sqs-1"
        assert task["org_id"] == "org-sqs"
        assert task["team_id"] == "team-sqs"
        assert task["account_type"] == "human"

    def test_missing_org_id_not_rejected_logging_only(self, mocked_aws_services):
        """Missing custom:org_id is NOT rejected — logging only (Stage A)."""
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response({
            "path": "direct_response",
            "persona": "developer",
            "response": "Hello!",
            "thread_action": "none",
            "reasoning": "Simple",
        })

        handler = _import_handler(mock_bedrock=mock_bedrock)
        event = mock_apigw_event(
            route_key="$default",
            body={"action": "message", "text": "Hello!", "session_id": "sess-no-org"},
            connection_id="conn-no-org",
            authorizer_claims={"sub": "user-no-org"},
        )
        result = handler.lambda_handler(event, None)
        # Should NOT reject — logging-only enforcement in Stage A
        assert result["statusCode"] == 200


class TestNoSubDropsMessage:
    """Issue #88: WebChat messages with no Cognito sub must be dropped, not fall back to connectionId."""

    def test_no_authorizer_claims_drops_message(self, mocked_aws_services):
        """Message with no authorizer claims at all is dropped (returns 200 OK, no side effects)."""
        handler = _import_handler()
        event = mock_apigw_event(
            route_key="$default",
            body={"action": "message", "text": "Hello!", "session_id": "sess-nosub"},
            connection_id="conn-nosub",
            authorizer_claims=None,  # No claims — simulates $default route without authorizer
        )
        result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 200
        assert result["body"] == "OK"

    def test_empty_claims_drops_message(self, mocked_aws_services):
        """Message with authorizer claims but missing 'sub' is dropped."""
        handler = _import_handler()
        event = mock_apigw_event(
            route_key="$default",
            body={"action": "message", "text": "Hello!", "session_id": "sess-emptysub"},
            connection_id="conn-emptysub",
            authorizer_claims={"email": "user@example.com"},  # Has email but no sub
        )
        result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 200
        assert result["body"] == "OK"

    def test_valid_sub_reaches_sqs_as_user_id(self, mocked_aws_services):
        """When sub IS present, it flows through as user_id on the SQS message (not connectionId)."""
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response({
            "path": "long_running",
            "persona": "developer",
            "response": None,
            "thread_action": "new",
            "reasoning": "Needs deep work",
        })

        handler = _import_handler(mock_bedrock=mock_bedrock)
        cognito_sub = "44086498-2091-70e1-bd3a-12c6104c3ebb"
        event = mock_apigw_event(
            route_key="$default",
            body={"action": "message", "text": "Refactor the auth module", "session_id": "sess-sub"},
            connection_id="cMJocfj3IAMCJSQ=",  # This should NOT end up as user_id
            authorizer_claims={"sub": cognito_sub, "email": "user@example.com"},
        )
        result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "processing"

        # Verify the SQS message carries the Cognito sub, not the connectionId
        sqs = mocked_aws_services["sqs"]
        resp = sqs.receive_message(
            QueueUrl="https://sqs.us-east-1.amazonaws.com/123/adp-dev-agent-gateway-tasks",
            MaxNumberOfMessages=1,
            WaitTimeSeconds=0,
        )
        messages = resp.get("Messages", [])
        assert len(messages) >= 1
        task = json.loads(messages[0]["Body"])
        assert task["user_id"] == cognito_sub
        assert task["user_id"] != "cMJocfj3IAMCJSQ="
