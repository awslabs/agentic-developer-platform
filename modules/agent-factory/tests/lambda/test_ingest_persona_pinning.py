"""
Unit tests for client-pinned persona in the Ingest Lambda (#4208).

The intent-intake chat needs its turns to land on the intent-refinement
persona rather than whatever the Bedrock classifier infers from the message
text, so the client may pin a persona in the send payload.

That field arrives from a browser and is therefore untrusted. The load-bearing
tests here are the adversarial ones: a persona outside the allowlist, or one
failing the name pattern, must be REJECTED and must never reach SQS.
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

HANDLER_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "gateway", "lambdas", "ingest")

TASKS_QUEUE = "https://sqs.us-east-1.amazonaws.com/123/adp-dev-agent-gateway-tasks"


@pytest.fixture(autouse=True)
def _patch_sys_path():
    original = sys.path.copy()
    sys.path.insert(0, HANDLER_DIR)
    yield
    sys.path = original


@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("INPUT_QUEUE_URL", TASKS_QUEUE)
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
    body_bytes = json.dumps(
        {
            "content": [{"type": "text", "text": json.dumps(classification)}],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
    ).encode()
    return {"body": io.BytesIO(body_bytes)}


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
        ddb.create_table(
            TableName="adp-dev-webhook-events",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}, {"AttributeName": "arrived_at", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        sqs_client = boto3.client("sqs", region_name="us-east-1")
        sqs_client.create_queue(QueueName="adp-dev-agent-gateway-tasks")
        sqs_client.create_queue(
            QueueName="adp-dev-agent-gateway-responses.fifo",
            Attributes={"FifoQueue": "true"},
        )
        yield {"ddb": ddb, "table": table, "sqs": sqs_client}


def _import_handler(mock_bedrock=None):
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
        ):
            del sys.modules[mod_name]

    import handler

    if mock_bedrock is not None:
        import classifier

        classifier._bedrock_client = mock_bedrock

    return handler


def _send(handler, text="I want a nightly cost report", session_id="sess-pin", persona=None):
    body = {"action": "sendMessage", "text": text, "session_id": session_id}
    if persona is not None:
        body["persona"] = persona
    event = mock_apigw_event(
        route_key="$default",
        body=body,
        connection_id="conn-pin",
        authorizer_claims={"sub": "user-pin", "email": "pin@example.com", "custom:tenant_id": "test-tenant"},
    )
    return handler.lambda_handler(event, None)


def _drain_queue(sqs) -> list[dict]:
    """Return every task currently on the input queue."""
    tasks = []
    while True:
        resp = sqs.receive_message(QueueUrl=TASKS_QUEUE, MaxNumberOfMessages=10, WaitTimeSeconds=0)
        messages = resp.get("Messages", [])
        if not messages:
            return tasks
        for m in messages:
            tasks.append(json.loads(m["Body"]))
            sqs.delete_message(QueueUrl=TASKS_QUEUE, ReceiptHandle=m["ReceiptHandle"])


# ---------------------------------------------------------------------------
# Adversarial: rejection of untrusted persona values
# ---------------------------------------------------------------------------


class TestPersonaRejection:
    """A persona the client is not allowed to pin must never reach SQS."""

    @pytest.mark.parametrize(
        "bad_persona",
        [
            # Not in the pinnable allowlist — a real persona, but not one a client
            # may select. This is the privilege-escalation case: without the
            # allowlist, the chat box picks any agent type.
            "developer",
            "reviewer",
            "operations",
            # Path traversal shapes — must fail the name pattern.
            "../../../etc/passwd",
            "../developer",
            "intent-refinement/../developer",
            "/etc/passwd",
            # Pattern violations.
            "Intent-Refinement",  # uppercase
            "1intent",  # leading digit
            "-intent",  # leading hyphen
            "intent refinement",  # space
            "intent;rm -rf /",  # shell metacharacters
            "intent\nrefinement",  # newline injection
            "a" * 65,  # over length cap
        ],
    )
    def test_disallowed_persona_is_rejected_and_never_enqueued(
        self, mocked_aws_services, bad_persona
    ):
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response(
            {
                "path": "long_running",
                "persona": "developer",
                "response": None,
                "thread_action": "new",
                "reasoning": "x",
            }
        )
        handler = _import_handler(mock_bedrock=mock_bedrock)

        result = _send(handler, persona=bad_persona)

        assert result["statusCode"] == 400
        assert "invalid persona" in result["body"]

        # The load-bearing assertion: nothing was forwarded to the worker.
        assert _drain_queue(mocked_aws_services["sqs"]) == []

    def test_rejection_does_not_invoke_the_classifier(self, mocked_aws_services):
        """A rejected pin must cost nothing — no Bedrock call, no side effects."""
        mock_bedrock = MagicMock()
        handler = _import_handler(mock_bedrock=mock_bedrock)

        result = _send(handler, persona="developer")

        assert result["statusCode"] == 400
        mock_bedrock.invoke_model.assert_not_called()

    def test_non_string_persona_is_ignored_not_crashed(self, mocked_aws_services):
        """A non-string persona is treated as absent (adapter coerces to "")."""
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response(
            {
                "path": "long_running",
                "persona": "developer",
                "response": None,
                "thread_action": "new",
                "reasoning": "x",
            }
        )
        handler = _import_handler(mock_bedrock=mock_bedrock)

        event = mock_apigw_event(
            route_key="$default",
            body={
                "action": "sendMessage",
                "text": "hi",
                "session_id": "s1",
                "persona": {"evil": True},
            },
            connection_id="conn-pin",
            authorizer_claims={"sub": "user-pin", "email": "pin@example.com", "custom:tenant_id": "test-tenant"},
        )
        result = handler.lambda_handler(event, None)

        # Falls back to normal classifier behaviour rather than 400/500.
        assert result["statusCode"] == 200
        tasks = _drain_queue(mocked_aws_services["sqs"])
        assert len(tasks) == 1
        assert tasks[0]["agent_type"] == "developer"


# ---------------------------------------------------------------------------
# Happy path: an allowlisted pin bypasses the classifier
# ---------------------------------------------------------------------------


class TestPersonaPinning:
    def test_pinned_persona_reaches_sqs_and_bypasses_classifier(self, mocked_aws_services):
        """With the field set, the classifier's persona choice is not used."""
        mock_bedrock = MagicMock()
        # The classifier, if consulted, would say "developer".
        mock_bedrock.invoke_model.return_value = _make_bedrock_response(
            {
                "path": "direct_response",
                "persona": "developer",
                "response": "Sure thing.",
                "thread_action": "new",
                "reasoning": "x",
            }
        )
        handler = _import_handler(mock_bedrock=mock_bedrock)

        result = _send(handler, persona="intent-refinement")

        assert result["statusCode"] == 200
        tasks = _drain_queue(mocked_aws_services["sqs"])
        assert len(tasks) == 1
        assert tasks[0]["agent_type"] == "intent-refinement"

        # The classifier is skipped entirely — it has nothing left to decide and
        # would add Bedrock latency to an already cold-start-heavy path.
        mock_bedrock.invoke_model.assert_not_called()

    def test_pinned_persona_never_takes_the_direct_response_path(self, mocked_aws_services):
        """direct_response is tool-less and capped at 1-2 sentences, so it can
        neither run the interviewer nor call update_draft. A pinned turn must
        always go long_running."""
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response(
            {
                "path": "direct_response",
                "persona": "developer",
                "response": "Short answer.",
                "thread_action": "new",
                "reasoning": "x",
            }
        )
        handler = _import_handler(mock_bedrock=mock_bedrock)

        result = _send(handler, persona="intent-refinement")

        body = json.loads(result["body"])
        assert body["status"] == "processing"  # long_running, not "completed"
        assert "thread_id" in body
        assert len(_drain_queue(mocked_aws_services["sqs"])) == 1

    def test_leading_trailing_whitespace_is_tolerated(self, mocked_aws_services):
        handler = _import_handler(mock_bedrock=MagicMock())

        result = _send(handler, persona="  intent-refinement  ")

        assert result["statusCode"] == 200
        tasks = _drain_queue(mocked_aws_services["sqs"])
        assert tasks[0]["agent_type"] == "intent-refinement"

    def test_multi_turn_intake_reuses_the_same_thread(self, mocked_aws_services):
        """A pinned intake conversation must serialize as ONE thread, not fork a
        new thread per turn — otherwise each turn loses the prior context.

        Turn 2 arrives while turn 1 is still processing, so it takes the
        pre-existing busy-thread path: buffered onto the SAME thread and
        delivered when turn 1 finishes. What matters here is the thread identity,
        not that a second SQS message appears immediately.
        """
        handler = _import_handler(mock_bedrock=MagicMock())

        first = _send(handler, text="I want a nightly cost report", persona="intent-refinement")
        second = _send(handler, text="For the prod account", persona="intent-refinement")

        first_thread = json.loads(first["body"])["thread_id"]
        second_body = json.loads(second["body"])
        assert second_body["thread_id"] == first_thread
        # Serialized rather than forked into a parallel thread.
        assert second_body["status"] == "queued"

        tasks = _drain_queue(mocked_aws_services["sqs"])
        assert len(tasks) == 1
        assert tasks[0]["agent_type"] == "intent-refinement"

    def test_second_turn_on_an_idle_thread_enqueues_with_the_pin(self, mocked_aws_services):
        """Once turn 1 finishes, the next pinned turn reuses the idle thread AND
        still carries the pinned persona to the worker."""
        mocked_aws_services["table"].put_item(
            Item={
                "session_id": "sess-pin",
                "user_workspace": "user-pin#webchat",
                "connection_id": "conn-pin",
                "channel": "webchat",
                "created_at": 1000,
                "updated_at": 1000,
                "messages": [],
                "threads": {
                    "thread-intake": {
                        "topic": "nightly cost report intake",
                        "path": "long_running",
                        "persona": "intent-refinement",
                        "processing_task_id": "",  # idle — prior turn finished
                        "messages": [],
                        "created_at": 1000,
                    },
                },
                "expires_at": 9999999999,
            }
        )
        handler = _import_handler(mock_bedrock=MagicMock())

        result = _send(handler, text="For the prod account", persona="intent-refinement")

        assert json.loads(result["body"])["thread_id"] == "thread-intake"
        tasks = _drain_queue(mocked_aws_services["sqs"])
        assert len(tasks) == 1
        assert tasks[0]["agent_type"] == "intent-refinement"
        assert tasks[0]["thread_id"] == "thread-intake"


# ---------------------------------------------------------------------------
# Regression: absent persona must behave exactly as before
# ---------------------------------------------------------------------------


class TestNoPersonaPinnedIsUnchanged:
    def test_absent_persona_uses_classifier_choice(self, mocked_aws_services):
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response(
            {
                "path": "long_running",
                "persona": "reviewer",
                "response": None,
                "thread_action": "new",
                "reasoning": "x",
            }
        )
        handler = _import_handler(mock_bedrock=mock_bedrock)

        result = _send(handler, persona=None)

        assert result["statusCode"] == 200
        tasks = _drain_queue(mocked_aws_services["sqs"])
        assert len(tasks) == 1
        # The classifier still decides when nothing is pinned.
        assert tasks[0]["agent_type"] == "reviewer"
        mock_bedrock.invoke_model.assert_called()

    def test_empty_string_persona_is_treated_as_absent(self, mocked_aws_services):
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response(
            {
                "path": "long_running",
                "persona": "developer",
                "response": None,
                "thread_action": "new",
                "reasoning": "x",
            }
        )
        handler = _import_handler(mock_bedrock=mock_bedrock)

        result = _send(handler, persona="")

        assert result["statusCode"] == 200
        tasks = _drain_queue(mocked_aws_services["sqs"])
        assert tasks[0]["agent_type"] == "developer"
        mock_bedrock.invoke_model.assert_called()

    def test_direct_response_still_works_with_no_pin(self, mocked_aws_services):
        """The classifier's direct_response path is untouched when nothing is pinned."""
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = _make_bedrock_response(
            {
                "path": "direct_response",
                "persona": "developer",
                "response": "Hello there.",
                "thread_action": "none",
                "reasoning": "greeting",
            }
        )
        handler = _import_handler(mock_bedrock=mock_bedrock)

        result = _send(handler, text="hi", persona=None)

        assert result["statusCode"] == 200
        assert json.loads(result["body"])["status"] == "completed"
        # direct_response never enqueues.
        assert _drain_queue(mocked_aws_services["sqs"]) == []


# ---------------------------------------------------------------------------
# Validator unit tests
# ---------------------------------------------------------------------------


class TestValidatorUnit:
    def test_allowlist_and_pattern_are_defined(self, mocked_aws_services):
        handler = _import_handler()
        assert "intent-refinement" in handler.PINNABLE_PERSONAS
        # The pattern must mirror persona-loader.ts's PERSONA_NAME_PATTERN.
        assert handler.PERSONA_NAME_PATTERN.match("intent-refinement")
        assert not handler.PERSONA_NAME_PATTERN.match("../developer")

    def test_adapter_carries_persona_into_platform_data(self, mocked_aws_services):
        """The adapter must pass the raw value through without judging it —
        validation is the handler's job."""
        _import_handler()
        from channels.webchat import WebChatAdapter

        event = mock_apigw_event(
            route_key="$default",
            body={
                "action": "sendMessage",
                "text": "hi",
                "session_id": "s",
                "persona": "anything-at-all",
            },
            connection_id="c",
            authorizer_claims={"sub": "u", "email": "e@example.com"},
        )
        message = WebChatAdapter().parse_event(event)
        assert message.platform_data["requested_persona"] == "anything-at-all"

    def test_adapter_defaults_persona_to_empty_string(self, mocked_aws_services):
        _import_handler()
        from channels.webchat import WebChatAdapter

        event = mock_apigw_event(
            route_key="$default",
            body={"action": "sendMessage", "text": "hi", "session_id": "s"},
            connection_id="c",
            authorizer_claims={"sub": "u", "email": "e@example.com"},
        )
        message = WebChatAdapter().parse_event(event)
        assert message.platform_data["requested_persona"] == ""

@pytest.mark.parametrize('refuse', [False, True])
def test_ingest_registers_final_root_before_sqs_and_refuses_failed_authority(mocked_aws_services, monkeypatch, refuse):
    handler = _import_handler(mock_bedrock=MagicMock())
    import model_root_client
    monkeypatch.setenv('ADP_CHAT_MODEL_POLICY_ENABLED', 'true')
    registered = []
    def admit(envelope, **identity):
        assert _drain_queue(mocked_aws_services['sqs']) == []
        assert identity == {'source': 'chat', 'subject': envelope['user_id']}
        assert envelope['tenant_id']
        if refuse:
            raise model_root_client.RootRegistrationRefusedError('refused')
        envelope = dict(envelope, persona=envelope['agent_type'], correlation={'root_human_id': 'canonical-human'})
        body = json.dumps(envelope, sort_keys=True, separators=(',', ':'))
        registered.append(body)
        return body
    monkeypatch.setattr(model_root_client, 'register_model_root', admit)
    result = _send(handler, persona='intent-refinement')
    if refuse:
        assert result['statusCode'] == 503
        assert _drain_queue(mocked_aws_services['sqs']) == []
    else:
        assert result['statusCode'] == 200
        tasks = mocked_aws_services['sqs'].receive_message(QueueUrl=TASKS_QUEUE).get('Messages', [])
        assert len(tasks) == 1
        assert tasks[0]['Body'] == registered[0]
