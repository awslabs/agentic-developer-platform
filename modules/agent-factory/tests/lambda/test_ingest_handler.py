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
from botocore.exceptions import ClientError

from tests.conftest import mock_apigw_event as _mock_apigw_event


def mock_apigw_event(**kwargs):
    """Valid signed-in chat fixtures include the user's tenant claim."""
    claims = dict(kwargs.get("authorizer_claims") or {})
    if claims.get("sub"):
        claims.setdefault("custom:tenant_id", "test-tenant")
    return _mock_apigw_event(**{**kwargs, "authorizer_claims": claims})


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
    monkeypatch.setenv("WEBHOOK_EVENTS_TABLE", "adp-dev-webhook-events")


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

        ddb.create_table(
            TableName="adp-dev-webhook-events",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}, {"AttributeName": "arrived_at", "AttributeType": "S"}],
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
                        "channels.webchat", "channels.slack", "github_dispatch", "invocation_logger"):
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
            authorizer_claims={"sub": "user-connect"},
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
            authorizer_claims={"sub": "user-bad-json"},
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
                "custom:tenant_id": "test-tenant",
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

    def test_missing_org_id_allowed_off_the_dispatch_path(self, mocked_aws_services):
        """Missing custom:org_id does not block non-dispatch paths.

        Issue #4233 made a missing org_id fail CLOSED on the github_actions
        dispatch path (see test_ingest_tenant_gate.py). It deliberately did not
        touch the other paths: chatting without an org claim must keep working,
        because no repository is being targeted. This is the regression guard
        for that boundary — it replaces the Stage A
        `test_missing_org_id_not_rejected_logging_only` contract, which asserted
        logging-only enforcement everywhere.
        """
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
        assert result["statusCode"] == 200
        assert json.loads(result["body"])["status"] == "completed"


class TestParseAttachmentsWithStringArtifactIds:
    """Issue #220: _parse_attachments must handle string artifact IDs without crashing."""

    def test_string_only_attachments_returns_empty(self, mocked_aws_services):
        """Attachments list of pure string artifact IDs produces no MediaAttachments."""
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
            body={
                "action": "sendMessage",
                "text": "Please read helloworld.md",
                "session_id": "sess-attach-str",
                "attachments": ["art_9b09c2da5d42"],
            },
            connection_id="conn-attach-str",
            authorizer_claims={"sub": "user-attach-str", "email": "att@example.com"},
        )
        # Should not crash — previously raised AttributeError
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200

    def test_mixed_string_and_dict_attachments(self, mocked_aws_services):
        """Mixed list of string IDs and dict attachments: dicts are parsed, strings are skipped."""
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
            body={
                "action": "sendMessage",
                "text": "Check these files",
                "session_id": "sess-attach-mixed",
                "attachments": [
                    "art_abc123",
                    {"url": "s3://bucket/file.png", "type": "image", "filename": "file.png"},
                    "art_def456",
                ],
            },
            connection_id="conn-attach-mixed",
            authorizer_claims={"sub": "user-attach-mixed", "email": "mix@example.com"},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200

    def test_dict_only_attachments_still_work(self, mocked_aws_services):
        """Pure dict attachments (old format) continue to work after the fix."""
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
            body={
                "action": "sendMessage",
                "text": "See attached",
                "session_id": "sess-attach-dict",
                "attachments": [
                    {"url": "s3://bucket/doc.pdf", "type": "document", "filename": "doc.pdf"},
                ],
            },
            connection_id="conn-attach-dict",
            authorizer_claims={"sub": "user-attach-dict", "email": "dict@example.com"},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200

    def test_string_artifact_ids_flow_to_platform_data(self, mocked_aws_services):
        """String artifact IDs are captured in platform_data.attachment_ids on the SQS message."""
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response({
            "path": "long_running",
            "persona": "developer",
            "response": None,
            "thread_action": "new",
            "reasoning": "Needs work",
        })

        handler = _import_handler(mock_bedrock=mock_bedrock)
        event = mock_apigw_event(
            route_key="$default",
            body={
                "action": "sendMessage",
                "text": "Read helloworld.md",
                "session_id": "sess-attach-sqs",
                "attachments": ["art_aaa111", "art_bbb222"],
            },
            connection_id="conn-attach-sqs",
            authorizer_claims={"sub": "user-attach-sqs", "email": "sqs-att@example.com"},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200

        # Verify SQS message carries attachment_ids
        sqs = mocked_aws_services["sqs"]
        resp = sqs.receive_message(
            QueueUrl="https://sqs.us-east-1.amazonaws.com/123/adp-dev-agent-gateway-tasks",
            MaxNumberOfMessages=1,
            WaitTimeSeconds=0,
        )
        messages = resp.get("Messages", [])
        assert len(messages) >= 1
        task = json.loads(messages[0]["Body"])
        # handler.py maps platform_data.attachment_ids → sqs_body["attachments"]
        att = task.get("attachments", [])
        assert "art_aaa111" in att
        assert "art_bbb222" in att


class TestNoSubRejectsMessage:
    """#88/#5013: reject missing Cognito identity without falling back to connectionId."""

    def test_no_authorizer_claims_rejects_message(self, mocked_aws_services):
        """Message with no authorizer claims at all is rejected explicitly."""
        handler = _import_handler()
        event = mock_apigw_event(
            route_key="$default",
            body={"action": "message", "text": "Hello!", "session_id": "sess-nosub"},
            connection_id="conn-nosub",
            authorizer_claims=None,  # No claims — simulates $default route without authorizer
        )
        result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 401
        assert json.loads(result["body"])["code"] == "connection_identity_expired"

    def test_empty_claims_rejects_message(self, mocked_aws_services):
        """Message with authorizer claims but missing 'sub' is rejected."""
        handler = _import_handler()
        event = mock_apigw_event(
            route_key="$default",
            body={"action": "message", "text": "Hello!", "session_id": "sess-emptysub"},
            connection_id="conn-emptysub",
            authorizer_claims={"email": "user@example.com"},  # Has email but no sub
        )
        result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 401
        assert json.loads(result["body"])["code"] == "connection_identity_expired"

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


class TestCognitoSubPropagation:
    """Issue #1289: cognito_sub must be propagated in SQS dispatch for personal-context identity."""

    def test_cognito_sub_included_in_sqs_message(self, mocked_aws_services):
        """When a user-dispatched task is created, cognito_sub is set from the Cognito JWT sub."""
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response({
            "path": "long_running",
            "persona": "developer",
            "response": None,
            "thread_action": "new",
            "reasoning": "Complex task",
        })

        handler = _import_handler(mock_bedrock=mock_bedrock)
        cognito_sub = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        event = mock_apigw_event(
            route_key="$default",
            body={"action": "message", "text": "Deploy the service", "session_id": "sess-pc"},
            connection_id="conn-pc-test",
            authorizer_claims={
                "sub": cognito_sub,
                "email": "dev@example.com",
                "custom:tenant_id": "org-acme-prod",
                "custom:org_id": "org-acme-prod",
            },
        )
        result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "processing"

        # Verify cognito_sub is present in the SQS message
        sqs = mocked_aws_services["sqs"]
        resp = sqs.receive_message(
            QueueUrl="https://sqs.us-east-1.amazonaws.com/123/adp-dev-agent-gateway-tasks",
            MaxNumberOfMessages=1,
            WaitTimeSeconds=0,
        )
        messages = resp.get("Messages", [])
        assert len(messages) >= 1
        task = json.loads(messages[0]["Body"])
        assert task["cognito_sub"] == cognito_sub
        assert task["tenant_id"] == "org-acme-prod"

    def test_cognito_sub_matches_user_id(self, mocked_aws_services):
        """cognito_sub is the same value as user_id (both sourced from JWT sub)."""
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response({
            "path": "long_running",
            "persona": "architect",
            "response": None,
            "thread_action": "new",
            "reasoning": "Design task",
        })

        handler = _import_handler(mock_bedrock=mock_bedrock)
        cognito_sub = "55086498-3091-80e1-cd4a-23d7215d4fcc"
        event = mock_apigw_event(
            route_key="$default",
            body={"action": "message", "text": "Design the API", "session_id": "sess-match"},
            connection_id="conn-match",
            authorizer_claims={"sub": cognito_sub, "email": "arch@example.com"},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200

        sqs = mocked_aws_services["sqs"]
        resp = sqs.receive_message(
            QueueUrl="https://sqs.us-east-1.amazonaws.com/123/adp-dev-agent-gateway-tasks",
            MaxNumberOfMessages=1,
            WaitTimeSeconds=0,
        )
        messages = resp.get("Messages", [])
        assert len(messages) >= 1
        task = json.loads(messages[0]["Body"])
        # cognito_sub and user_id should be identical (both from JWT sub)
        assert task["cognito_sub"] == task["user_id"]
        assert task["cognito_sub"] == cognito_sub

    def test_existing_dispatch_unaffected_without_personal_context(self, mocked_aws_services):
        """Existing dispatch paths work unchanged when no personal-context consumer reads cognito_sub (regression)."""
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response({
            "path": "long_running",
            "persona": "developer",
            "response": None,
            "thread_action": "new",
            "reasoning": "Standard task",
        })

        handler = _import_handler(mock_bedrock=mock_bedrock)
        cognito_sub = "66086498-4091-90e1-de5b-34e8326e5gdd"
        event = mock_apigw_event(
            route_key="$default",
            body={"action": "message", "text": "Fix the bug", "session_id": "sess-compat"},
            connection_id="conn-compat",
            authorizer_claims={"sub": cognito_sub, "email": "user@example.com"},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200

        sqs = mocked_aws_services["sqs"]
        resp = sqs.receive_message(
            QueueUrl="https://sqs.us-east-1.amazonaws.com/123/adp-dev-agent-gateway-tasks",
            MaxNumberOfMessages=1,
            WaitTimeSeconds=0,
        )
        messages = resp.get("Messages", [])
        assert len(messages) >= 1
        task = json.loads(messages[0]["Body"])
        # All existing fields still present
        assert "task_id" in task
        assert "session_id" in task
        assert "message" in task
        assert task["user_id"] == cognito_sub
        # cognito_sub is additive — it doesn't break existing keys
        assert task["cognito_sub"] == cognito_sub


class TestConnectionIdentityFailures:
    """#5013: encrypted claims storage must fail closed and notify the client."""

    @staticmethod
    def _kms_denial(operation):
        return ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "kms:Decrypt denied on private-key"}},
            operation,
        )

    def test_connect_rejects_kms_denial(self, mocked_aws_services, monkeypatch):
        handler = _import_handler()
        monkeypatch.setattr(handler.sessions_table, "put_item", MagicMock(side_effect=self._kms_denial("PutItem")))
        dispatch = MagicMock()
        monkeypatch.setattr(handler, "handle_unified_message", dispatch)
        event = mock_apigw_event(route_key="$connect", authorizer_claims={"sub": "native-user"})

        result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 503
        payload = json.loads(result["body"])
        assert payload["code"] == "connection_identity_unavailable"
        assert "reconnect" in payload["content"]
        assert "private-key" not in result["body"]
        dispatch.assert_not_called()

    @pytest.mark.parametrize("connection_id,claims", [("conn-missing", None), ("", {"sub": "user"})])
    def test_connect_rejects_missing_identity(self, mocked_aws_services, monkeypatch, connection_id, claims):
        handler = _import_handler()
        put = MagicMock()
        monkeypatch.setattr(handler.sessions_table, "put_item", put)
        result = handler.lambda_handler(
            mock_apigw_event(route_key="$connect", connection_id=connection_id, authorizer_claims=claims), None
        )
        assert result["statusCode"] == 401
        put.assert_not_called()

    @pytest.mark.parametrize("action", ["sendMessage", "upload-token", "upload-complete"])
    @pytest.mark.parametrize("failure", ["kms_denied", "missing", "expired"])
    def test_restore_failure_posts_error_without_dispatch(
        self, mocked_aws_services, monkeypatch, action, failure
    ):
        handler = _import_handler()
        get = MagicMock(return_value={})
        if failure == "kms_denied":
            get.side_effect = self._kms_denial("GetItem")
        elif failure == "expired":
            get.return_value = {"Item": {"sub": "native-user", "expires_at": 1}}
        monkeypatch.setattr(handler.sessions_table, "get_item", get)
        apigw = MagicMock()
        monkeypatch.setattr(handler, "_get_apigw_client", lambda: apigw)
        dispatch = MagicMock()
        upload = MagicMock()
        monkeypatch.setattr(handler, "handle_unified_message", dispatch)
        monkeypatch.setattr(handler, "handle_upload_token", upload)
        monkeypatch.setattr(handler, "handle_upload_complete", upload)
        event = mock_apigw_event(
            connection_id="conn-denied",
            body={"action": action, "text": "test", "session_id": "session-test", "request_id": "request-test"},
        )

        result = handler.lambda_handler(event, None)

        assert result["statusCode"] == (503 if failure == "kms_denied" else 401)
        get.assert_called_once_with(Key={"session_id": "conn#conn-denied"}, ConsistentRead=True)
        dispatch.assert_not_called()
        upload.assert_not_called()
        apigw.post_to_connection.assert_called_once()
        args = apigw.post_to_connection.call_args.kwargs
        assert args["ConnectionId"] == "conn-denied"
        frame = json.loads(args["Data"])
        # Both current AG-UI and legacy chat hooks recognize this failure shape.
        assert frame["type"] == "response"
        assert frame["status"] == "failed"
        assert frame["session_id"] == "session-test"
        assert frame["request_id"] == "request-test"
        assert "reconnect" in frame["content"]
        assert frame["error"] == frame["content"]
        assert "private-key" not in str(frame)

    @pytest.mark.parametrize("partial_context", [False, True])
    def test_signed_authorizer_context_survives_connect_message_round_trip(
        self, mocked_aws_services, monkeypatch, partial_context
    ):
        handler = _import_handler()
        connect = mock_apigw_event(route_key="$connect", connection_id="conn-native")
        connect["requestContext"]["authorizer"] = {
            "X-Agent-UserId": "native-user",
            "X-Agent-Email": "native@example.com",
            "X-Agent-Tenant": "tenant-1",
            "X-Agent-OrgId": "org-1",
            "X-Agent-TeamId": "team-1",
            "X-Agent-DepartmentId": "dept-1",
            "X-Agent-AccountType": "human",
            "X-Agent-Role": "member",
        }
        assert handler.lambda_handler(connect, None)["statusCode"] == 200
        dispatch = MagicMock(return_value={"statusCode": 200})
        monkeypatch.setattr(handler, "handle_unified_message", dispatch)
        event = mock_apigw_event(connection_id="conn-native", body={"action": "sendMessage", "text": "test"})
        assert "authorizer" not in event["requestContext"]
        if partial_context:
            event["requestContext"]["authorizer"] = {"claims": {"sub": "", "custom:org_id": "old-org"}}

        handler.lambda_handler(event, None)

        message = dispatch.call_args.args[0]
        assert message.user_id == "native-user"
        assert message.user_name == "native@example.com"
        for key, value in {"tenant_id": "tenant-1", "org_id": "org-1", "team_id": "team-1",
                           "department_id": "dept-1", "account_type": "human", "role": "member"}.items():
            assert message.platform_data[key] == value
