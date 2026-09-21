"""Unit tests for the gateway-api channel adapter in the Ingest Lambda (#5331).

EPIC #4191. `adp flow start` reaches the intent-refinement persona by invoking this
Lambda directly, so a terminal-started planning conversation IS the conversation the
browser would show. The alternative — the gateway writing onto the input queue itself
— is what these tests exist to make impossible to regress to, because it skips every
side effect `handle_unified_message` performs: no session row, no thread, no
transcript, no registered run.

Two groups matter most, and they are the adversarial ones:

- **Channel detection order.** This envelope carries its own `user_id`/`org_id`,
  because its transport is `lambda:InvokeFunction` and the IAM grant is what
  authenticates it. That makes it the one envelope a WebSocket client must never
  reach: a browser sending `{"source": "gateway-api", "user_id": "someone-else"}`
  would otherwise be handed another person's identity. Every API Gateway WebSocket
  event carries a `connectionId`, and that check is first and unconditional.

- **The pin is still validated.** Being IAM-gated makes the CALLER trusted, not the
  value correct. A gateway bug that sent a non-pinnable persona must be rejected.
"""

from __future__ import annotations

import io
import json
import os
import sys
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from tests.conftest import mock_apigw_event

HANDLER_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "gateway", "lambdas", "ingest")

TASKS_QUEUE = "https://sqs.us-east-1.amazonaws.com/123/adp-dev-agent-gateway-tasks"
SESSION = "sess-cli-abc123"
USER = "user-operator"
ORG = "org-acme"


@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("INPUT_QUEUE_URL", TASKS_QUEUE)
    monkeypatch.setenv("RESPONSE_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/adp-dev-agent-gateway-responses.fifo")
    monkeypatch.setenv("SESSIONS_TABLE_NAME", "adp-dev-agent-gateway-sessions")
    monkeypatch.setenv("AWS_REGION_NAME", "us-east-1")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "")
    monkeypatch.setenv("SLACK_BOT_USER_ID", "")
    monkeypatch.setenv("WEBHOOK_EVENTS_TABLE", "adp-dev-webhook-events")


@pytest.fixture(autouse=True)
def _importable_handler(mock_env):
    """The handler reads required env at import time, so every test needs it.

    Autouse and env-dependent together: a test that only exercises `parse_event` still
    has to import the module the adapter is registered in, and `INPUT_QUEUE_URL` is a
    hard `os.environ[...]` at module scope.
    """
    original = sys.path.copy()
    sys.path.insert(0, HANDLER_DIR)
    yield
    sys.path = original


@pytest.fixture
def mocked_aws_services(mock_env):
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        sessions = ddb.create_table(
            TableName="adp-dev-agent-gateway-sessions",
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        events = ddb.create_table(
            TableName="adp-dev-webhook-events",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "event_id", "AttributeType": "S"},
                {"AttributeName": "arrived_at", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        sqs_client = boto3.client("sqs", region_name="us-east-1")
        sqs_client.create_queue(QueueName="adp-dev-agent-gateway-tasks")
        sqs_client.create_queue(QueueName="adp-dev-agent-gateway-responses.fifo", Attributes={"FifoQueue": "true"})
        yield {"sessions": sessions, "events": events, "sqs": sqs_client}


def _make_bedrock_response(classification: dict) -> dict:
    body_bytes = json.dumps(
        {"content": [{"type": "text", "text": json.dumps(classification)}], "usage": {"input_tokens": 10, "output_tokens": 5}}
    ).encode()
    return {"body": io.BytesIO(body_bytes)}


def _import_handler(mock_bedrock=None):
    for mod_name in list(sys.modules.keys()):
        if mod_name in (
            "handler",
            "classifier",
            "channels",
            "channels.base",
            "channels.webchat",
            "channels.slack",
            "channels.gateway_api",
            "github_dispatch",
            "invocation_logger",
        ):
            del sys.modules[mod_name]

    import handler

    if mock_bedrock is not None:
        import classifier

        classifier._bedrock_client = mock_bedrock

    return handler


def envelope(**overrides) -> dict:
    """The payload `IntakeDispatcher.send` builds, as the Lambda receives it.

    Kept in one place and asserted against the gateway's own constant in
    `test_the_two_sides_agree_on_the_discriminator`, because a mismatch fails in the
    worst possible way: the event falls through to the webchat adapter, which finds no
    claims, drops the message and returns 200 — a silent success for a turn that never
    happened.
    """
    payload = {
        "source": "gateway-api",
        "session_id": SESSION,
        "message": "Add per-tenant rate limiting to the public API",
        "user_id": USER,
        "org_id": ORG,
        "tenant_id": ORG,
        "team_id": "",
        "department_id": "",
        "account_type": "human",
        "requested_persona": "intent-refinement",
        "channel": "webchat",
        "message_id": "regkey-0001",
    }
    payload.update(overrides)
    return payload


def _drain_queue(sqs) -> list[dict]:
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
# Adversarial: this envelope must be unforgeable from the WebSocket
# ---------------------------------------------------------------------------


class TestTheTrustedEnvelopeIsUnreachableFromABrowser:
    """The envelope carries its own identity, so reaching it is impersonation."""

    def test_a_websocket_event_claiming_to_be_the_gateway_is_still_webchat(self, mocked_aws_services):
        """The load-bearing test of this module.

        A browser that put `source: gateway-api` and somebody else's `user_id` into
        its event must not be routed to the adapter that believes those fields. Every
        API Gateway WebSocket event carries a `connectionId`, and that check runs
        first and unconditionally.
        """
        handler = _import_handler()

        event = mock_apigw_event(
            route_key="$default",
            body={"action": "sendMessage", "text": "hi", "session_id": SESSION, "source": "gateway-api", "user_id": "victim-user"},
            connection_id="conn-browser",
            authorizer_claims={"sub": "attacker-user", "email": "a@example.com", "custom:tenant_id": "org-attacker"},
        )
        event["source"] = "gateway-api"

        channel_name, _ = handler.detect_channel(event)
        assert channel_name == "webchat", "a connection-bearing event is a browser's, whatever its body claims"

    def test_the_impersonated_identity_never_reaches_the_session_row(self, mocked_aws_services):
        """Routing is a means; this is the consequence that matters.

        The turn is attributed to the attacker's own authenticated sub, so the forged
        `user_id` buys nothing.
        """
        handler = _import_handler(mock_bedrock=MagicMock())

        event = mock_apigw_event(
            route_key="$default",
            body={"action": "sendMessage", "text": "hi", "session_id": SESSION, "user_id": "victim-user", "persona": "intent-refinement"},
            connection_id="conn-browser",
            authorizer_claims={"sub": "attacker-user", "email": "a@example.com", "custom:tenant_id": "org-attacker"},
        )
        event["source"] = "gateway-api"
        handler.lambda_handler(event, None)

        row = mocked_aws_services["sessions"].get_item(Key={"session_id": SESSION}).get("Item", {})
        assert row.get("user_workspace", "").startswith("attacker-user#")
        assert "victim-user" not in row.get("user_workspace", "")

    def test_a_signed_slack_event_stays_slack(self):
        """The same reasoning in the other direction: a signed event is Slack's
        regardless of what its body says, so the discriminator cannot be used to
        smuggle a Slack payload onto the trusted path either."""
        handler = _import_handler()

        event = {
            "headers": {"x-slack-signature": "v0=deadbeef", "x-slack-request-timestamp": "1700000000"},
            "body": json.dumps({"source": "gateway-api", "user_id": "victim-user"}),
        }
        channel_name, _ = handler.detect_channel(event)
        assert channel_name == "slack"

    def test_a_malformed_webchat_event_is_not_promoted_to_trusted(self):
        """An event with no connectionId and no signature is NOT the trusted path.

        Matching on the absence of other markers rather than on the explicit
        discriminator would make every malformed webchat event an identity bypass.
        """
        handler = _import_handler()

        channel_name, _ = handler.detect_channel({"body": json.dumps({"text": "hi", "user_id": "victim-user"})})
        assert channel_name == "webchat"

    def test_the_discriminator_does_route_the_real_thing(self):
        """The positive case, so the tests above are not passing by never matching."""
        handler = _import_handler()

        channel_name, adapter = handler.detect_channel(envelope())
        assert channel_name == "gateway-api"
        assert adapter.__class__.__name__ == "GatewayApiAdapter"

    def test_the_two_sides_agree_on_the_discriminator(self):
        """The gateway and this Lambda name the same literal in two repositories'
        worth of code. Asserted rather than assumed, because a mismatch is a silent
        200 for a turn that never happened."""
        handler = _import_handler()
        gateway_side = (
            open(
                os.path.join(os.path.dirname(__file__), "..", "..", "..", "gateway", "src", "orchestration", "intake_dispatch.py"),
                encoding="utf-8",
            )
            .read()
            .count('GATEWAY_API_SOURCE = "gateway-api"')
        )
        assert handler.GATEWAY_API_SOURCE == "gateway-api"
        assert gateway_side == 1, "the gateway's copy of the discriminator must match this one"


# ---------------------------------------------------------------------------
# The adapter's own contract
# ---------------------------------------------------------------------------


class TestTheEnvelopeBecomesTheSharedMessageShape:
    """Everything after `parse_event` is the one existing implementation."""

    def adapter(self):
        _import_handler()
        from channels.gateway_api import GatewayApiAdapter

        return GatewayApiAdapter()

    def test_the_channel_is_webchat_so_resume_finds_the_conversation(self):
        """`user_workspace` is `f"{user_id}#{channel}"` and is the hash key of the
        `user-workspace-index` GSI that `--resume` queries. A new channel value would
        put terminal-started conversations in a different partition from browser ones,
        so the same person's conversation would be invisible from the other client —
        exactly the split this EPIC exists to prevent."""
        from channels.base import ChannelType

        assert self.adapter().channel_type == ChannelType.WEBCHAT

    def test_the_session_id_lands_where_the_handler_reads_it(self):
        """`handle_unified_message` reads `message.thread_id or message.session_key`
        as the session id. The gateway mints the id and hands it to the caller BEFORE
        a reply exists, so a server-side substitution here would hand back an id that
        resolves to nothing."""
        message = self.adapter().parse_event(envelope())
        assert message.thread_id == SESSION

    def test_the_identity_comes_from_the_envelope(self):
        message = self.adapter().parse_event(envelope(team_id="team-7", department_id="dept-3", account_type="service"))
        assert message.user_id == USER
        assert message.platform_data["org_id"] == ORG
        assert message.platform_data["tenant_id"] == ORG
        assert message.platform_data["team_id"] == "team-7"
        assert message.platform_data["department_id"] == "dept-3"
        assert message.platform_data["account_type"] == "service"

    def test_the_tenant_falls_back_to_the_org(self):
        message = self.adapter().parse_event(envelope(tenant_id=""))
        assert message.platform_data["tenant_id"] == ORG

    def test_the_supplied_message_id_is_carried_as_the_registration_key(self):
        """`log_invocation` writes `event_id = message_id` and the worker advances
        status by that same pair, so regenerating it here would orphan the row the
        worker later tries to update — and make one retried turn two registered
        runs."""
        message = self.adapter().parse_event(envelope(message_id="regkey-0001"))
        assert message.message_id == "regkey-0001"

    def test_an_absent_message_id_still_produces_one(self):
        """The dataclass default. A turn with no registration key at all could not be
        registered, and `handle_long_running` would 503 it."""
        payload = envelope()
        del payload["message_id"]
        message = self.adapter().parse_event(payload)
        assert message.message_id

    def test_the_users_words_are_passed_through_unmodified(self):
        """It is the agent's input, not a command. Nothing here interprets it."""
        text = "Rate-limit /v1/chat to 100 rpm per tenant; do NOT touch /v1/health"
        message = self.adapter().parse_event(envelope(message=text))
        assert message.text == text

    def test_there_is_no_connection_to_post_back_to(self):
        """A terminal client polls the readback instead. The response Lambda already
        treats an absent `connection_id` as "no live client", which is correct here —
        inventing one would have it post to a socket that does not exist."""
        message = self.adapter().parse_event(envelope())
        assert message.platform_data["connection_id"] == ""
        assert message.channel_id == ""

    def test_the_ingress_is_recorded_as_provenance(self):
        """Nothing downstream branches on it — the two clients must not be able to
        diverge — but an operator reading a session row can tell where a turn came
        from."""
        message = self.adapter().parse_event(envelope())
        assert message.platform_data["ingress"] == "gateway-api"

    def test_verification_is_the_iam_grant(self):
        """There is no HTTP transport and no shared secret. An event that arrives at
        all was invoked by a principal the resource policy permits."""
        assert self.adapter().verify_request({}, b"") is True

    @pytest.mark.parametrize(
        "unusable,reason",
        [
            ({"message": "   "}, "an empty turn would produce a reply to nothing"),
            ({"user_id": ""}, "an unattributed turn's spend authority cannot be resolved"),
            ({"session_id": ""}, "the caller already holds this id; substituting one hands back a dead handle"),
        ],
    )
    def test_an_unusable_envelope_is_dropped_with_no_side_effects(self, unusable, reason):
        """None, which the handler turns into a 200 with nothing done — the same
        quietly-ignore contract the other adapters have for non-messages."""
        assert self.adapter().parse_event(envelope(**unusable)) is None, reason


# ---------------------------------------------------------------------------
# End to end: the nine side effects the queue shortcut skipped
# ---------------------------------------------------------------------------


class TestTheTurnGetsTheRealIngestContract:
    """A direct queue write performs NONE of these, which is the whole defect."""

    def test_a_dispatched_turn_creates_the_session_row_with_its_tenant(self, mocked_aws_services):
        """`org_id` on the row is what the gateway's readback compares against, so a
        conversation created without it is unreachable from the operator plane."""
        handler = _import_handler(mock_bedrock=MagicMock())
        handler.lambda_handler(envelope(), None)

        row = mocked_aws_services["sessions"].get_item(Key={"session_id": SESSION}).get("Item", {})
        assert row.get("org_id") == ORG
        assert row.get("user_workspace") == f"{USER}#webchat"

    def test_a_dispatched_turn_creates_a_thread_the_readback_can_see(self, mocked_aws_services):
        """In-flight state lives at `threads.<tid>.processing_task_id` and the issue
        reference at `threads.<tid>.github_issue_number`. Neither exists unless
        `create_thread` ran, so a queue write leaves both structurally empty."""
        handler = _import_handler(mock_bedrock=MagicMock())
        result = handler.lambda_handler(envelope(), None)

        thread_id = json.loads(result["body"])["thread_id"]
        row = mocked_aws_services["sessions"].get_item(Key={"session_id": SESSION}).get("Item", {})
        assert thread_id in row.get("threads", {})
        assert row["threads"][thread_id].get("processing_task_id")

    def test_a_dispatched_turn_registers_the_run_under_the_gateways_key(self, mocked_aws_services):
        """The row that authorizes the worker to inherit its owner's Bedrock
        destination. Keyed by `event_id = message_id`, which is the gateway's retry
        token — so a retry names this row instead of registering a second one."""
        handler = _import_handler(mock_bedrock=MagicMock())
        handler.lambda_handler(envelope(message_id="regkey-0001"), None)

        rows = mocked_aws_services["events"].scan().get("Items", [])
        assert [r["event_id"] for r in rows] == ["regkey-0001"]
        assert rows[0]["tenant_id"] == ORG
        assert rows[0]["user_id"] == USER
        assert rows[0]["is_human_rooted"] is True

    def test_a_service_account_turn_is_not_human_rooted(self, mocked_aws_services):
        handler = _import_handler(mock_bedrock=MagicMock())
        handler.lambda_handler(envelope(account_type="service"), None)

        rows = mocked_aws_services["events"].scan().get("Items", [])
        assert rows[0]["is_human_rooted"] is False

    def test_a_retried_turn_does_not_enqueue_or_register_again(self, mocked_aws_services):
        handler = _import_handler(mock_bedrock=MagicMock())
        first = handler.lambda_handler(envelope(message_id="regkey-0001"), None)
        detail = json.loads(first["body"])
        handler.set_thread_processing(SESSION, detail["thread_id"], None)
        with patch.object(handler.time, "time", return_value=2000000000):
            second = handler.lambda_handler(envelope(message_id="regkey-0001"), None)
        assert second == first
        assert len(_drain_queue(mocked_aws_services["sqs"])) == 1
        rows = mocked_aws_services["events"].scan().get("Items", [])
        assert len(rows) == 1
        assert rows[0]["event_id"] == "regkey-0001"

    def test_retry_token_cannot_be_reused_for_different_content(self, mocked_aws_services):
        handler = _import_handler(mock_bedrock=MagicMock())
        handler.lambda_handler(envelope(message_id="same"), None)
        result = handler.lambda_handler(envelope(message_id="same", message="Different work"), None)
        assert result["statusCode"] == 409
        assert "retry_token_reused" in result["body"]
        assert len(_drain_queue(mocked_aws_services["sqs"])) == 1

    def test_lost_acknowledgement_does_not_resend_the_task(self, mocked_aws_services):
        handler = _import_handler(mock_bedrock=MagicMock())
        original = handler.sessions_table.update_item
        def fail_receipt(**kwargs):
            if kwargs.get("ExpressionAttributeNames", {}).get("#result") == "result":
                raise RuntimeError("lost acknowledgement")
            return original(**kwargs)
        with patch.object(handler.sessions_table, "update_item", side_effect=fail_receipt):
            with pytest.raises(RuntimeError, match="lost acknowledgement"):
                handler.lambda_handler(envelope(message_id="lost"), None)
        retry = handler.lambda_handler(envelope(message_id="lost"), None)
        assert retry["statusCode"] == 409
        assert "turn_delivery_uncertain" in retry["body"]
        assert len(_drain_queue(mocked_aws_services["sqs"])) == 1

    def test_repository_and_issue_survive_as_structured_session_context(self, mocked_aws_services):
        handler = _import_handler(mock_bedrock=MagicMock())
        handler.lambda_handler(envelope(repository="acme/web", issue="42"), None)
        row = mocked_aws_services["sessions"].get_item(Key={"session_id": SESSION})["Item"]
        assert row["intake_repository"] == "acme/web"
        assert row["intake_issue"] == "42"
        task = _drain_queue(mocked_aws_services["sqs"])[0]
        assert (task["repo_owner"], task["repo_name"]) == ("acme", "web")

    def test_the_turn_reaches_the_worker_on_the_pinned_persona(self, mocked_aws_services):
        """The queue write is the LAST of the nine steps, not a substitute for them."""
        handler = _import_handler(mock_bedrock=MagicMock())
        handler.lambda_handler(envelope(), None)

        tasks = _drain_queue(mocked_aws_services["sqs"])
        assert len(tasks) == 1
        assert tasks[0]["agent_type"] == "intent-refinement"
        assert tasks[0]["session_id"] == SESSION
        assert tasks[0]["message_id"] == "regkey-0001"

    def test_the_classifier_is_skipped(self, mocked_aws_services):
        """The pin means there is nothing left to decide, and skipping it is what
        keeps a SYNCHRONOUS dispatch from paying a Bedrock call on every turn."""
        mock_bedrock = MagicMock()
        handler = _import_handler(mock_bedrock=mock_bedrock)
        handler.lambda_handler(envelope(), None)

        mock_bedrock.invoke_model.assert_not_called()

    def test_the_reply_handle_is_returned_so_the_caller_can_correlate(self, mocked_aws_services):
        """The gateway invokes this synchronously precisely because these values exist
        nowhere else — a fire-and-forget dispatch could not report them."""
        handler = _import_handler(mock_bedrock=MagicMock())
        body = json.loads(handler.lambda_handler(envelope(), None)["body"])

        assert body["status"] == "processing"
        assert body["session_id"] == SESSION
        assert body["task_id"]
        assert body["thread_id"]

    def test_a_second_turn_continues_the_same_thread(self, mocked_aws_services):
        """A multi-turn intake must serialize as ONE conversation. Turn 2 arrives while
        turn 1 holds the thread, so it is buffered onto the same thread rather than
        forking — which is also the `queued` status the gateway maps to 409."""
        handler = _import_handler(mock_bedrock=MagicMock())
        first = json.loads(handler.lambda_handler(envelope(message_id="k1"), None)["body"])
        second = json.loads(handler.lambda_handler(envelope(message="Only the public API", message_id="k2"), None)["body"])

        assert second["thread_id"] == first["thread_id"]
        assert second["status"] == "queued"

    def test_a_turn_with_no_tenant_is_refused_without_dispatching(self, mocked_aws_services):
        """`handle_long_running` refuses to register a run without a tenant, and
        un-marks the thread rather than enqueueing. Nothing must reach the worker."""
        handler = _import_handler(mock_bedrock=MagicMock())
        result = handler.lambda_handler(envelope(org_id="", tenant_id=""), None)

        assert result["statusCode"] == 503
        assert _drain_queue(mocked_aws_services["sqs"]) == []

    @pytest.mark.parametrize("bad_persona", ["developer", "../../developer", "Intent-Refinement", "intent;rm -rf /"])
    def test_a_non_pinnable_persona_is_refused_even_from_the_trusted_caller(self, mocked_aws_services, bad_persona):
        """Being IAM-gated makes the CALLER trusted, not the VALUE correct.

        The gateway pins this from a module constant, so a bad value here means a
        gateway bug — which must be rejected, not obeyed. Nothing may reach the worker.
        """
        handler = _import_handler(mock_bedrock=MagicMock())
        result = handler.lambda_handler(envelope(requested_persona=bad_persona), None)

        assert result["statusCode"] == 400
        assert "invalid persona" in result["body"]
        assert _drain_queue(mocked_aws_services["sqs"]) == []

    def test_an_unusable_envelope_changes_nothing(self, mocked_aws_services):
        """200 with no side effects, rather than an error: the handler's contract for
        an event that is not a message."""
        handler = _import_handler(mock_bedrock=MagicMock())
        result = handler.lambda_handler(envelope(message="  "), None)

        assert result["statusCode"] == 200
        assert mocked_aws_services["sessions"].get_item(Key={"session_id": SESSION}).get("Item") is None
        assert _drain_queue(mocked_aws_services["sqs"]) == []
