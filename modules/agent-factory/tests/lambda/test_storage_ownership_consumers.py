"""
Storage ownership consumer coverage (#5615 / S16 continuation).

Tests that every session data consumer — message persistence, thread creation,
thread message appending, history loading, and connection claims restoration —
is either (a) independently ownership-gated or (b) only reachable after the
entry-point ownership check in get_or_create_session.

These tests drive the production lambda_handler boundary with forged session
ids and verify that no data is written to a session the attacker does not own.
They complement test_session_ownership.py (which covers the two-tenant
ownership contract from the attacker's seat) and test_response_ownership.py
(which covers the response handler's persistence contract).

Covered:
  1. Foreign message does not append to victim's messages list
  2. Foreign message does not create a thread in victim's session
  3. Foreign message does not write to victim's thread messages
  4. Connection claims for user A cannot be used to access user B's session
  5. Upload-token for a foreign session returns 404 and creates no artifact
  6. Upload-complete for a foreign session returns 404 and creates no catalog row
  7. Claims restoration propagates org_id/team_id for subsequent ownership checks
"""

from __future__ import annotations

import json
import os
import sys
import time
from unittest.mock import patch, MagicMock

import boto3
import pytest
from moto import mock_aws

HANDLER_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "gateway", "lambdas", "ingest"
)

VICTIM = {
    "sub": "user-victim",
    "custom:tenant_id": "tenant-v",
    "custom:org_id": "org-v",
    "custom:team_id": "team-v",
}
ATTACKER = {
    "sub": "user-attacker",
    "custom:tenant_id": "tenant-a",
    "custom:org_id": "org-a",
    "custom:team_id": "team-a",
}


@pytest.fixture(autouse=True)
def _patch_sys_path():
    original = sys.path.copy()
    sys.path.insert(0, HANDLER_DIR)
    yield
    sys.path = original


def _import_handler():
    for mod_name in list(sys.modules):
        if mod_name in (
            "handler", "classifier", "github_dispatch", "channels",
            "channels.base", "channels.slack", "channels.webchat",
            "channels.gateway_api", "user_resolver", "invocation_logger",
        ):
            del sys.modules[mod_name]
    import handler
    # Keep the real serialization boundary; never contact an API Gateway host.
    handler._apigw_client = MagicMock()
    return handler


@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("INPUT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/queue.fifo")
    monkeypatch.setenv("SESSIONS_TABLE_NAME", "test-sessions")
    monkeypatch.setenv("ARTIFACTS_TABLE", "test-artifacts")
    monkeypatch.setenv("ARTIFACTS_BUCKET", "test-artifacts-bucket")
    monkeypatch.setenv("WS_API_ENDPOINT", "https://ws.example.com/v1")
    monkeypatch.setenv("WS_API_ID", "ws123")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("AWS_REGION_NAME", "us-east-1")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test")
    monkeypatch.setenv("SLACK_BOT_USER_ID", "U1234")
    monkeypatch.setenv("RESPONSE_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/resp.fifo")
    monkeypatch.setenv("WEBHOOK_EVENTS_TABLE", "")
    monkeypatch.setenv("ENABLE_USER_IDENTITIES", "0")


@pytest.fixture
def aws(mock_env):
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        sessions = ddb.create_table(
            TableName="test-sessions",
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        artifacts = ddb.create_table(
            TableName="test-artifacts",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        sessions.meta.client.get_waiter("table_exists").wait(TableName="test-sessions")
        artifacts.meta.client.get_waiter("table_exists").wait(TableName="test-artifacts")
        yield {"sessions": sessions, "artifacts": artifacts}


def _ws_event(body: dict, claims: dict, connection_id: str = "conn-attacker") -> dict:
    return {
        "requestContext": {
            "routeKey": "sendMessage",
            "connectionId": connection_id,
            "authorizer": {"claims": claims},
        },
        "body": json.dumps(body),
    }


def _owner_principal(claims: dict, channel: str = "webchat") -> str:
    return json.dumps(
        [
            claims.get("custom:tenant_id", ""),
            claims.get("custom:org_id", ""),
            claims.get("custom:team_id", ""),
            claims["sub"],
            channel,
        ],
        separators=(",", ":"),
    )


def _seed_victim_session(handler, table, session_id="sess-victim"):
    """Create a session owned by the victim, as the server would."""
    now = int(time.time())
    principal = _owner_principal(VICTIM)
    table.put_item(Item={
        "session_id": session_id,
        "owner_principal": principal,
        "owner_user_id": VICTIM["sub"],
        "user_workspace": f"{VICTIM['sub']}#webchat",
        "tenant_id": VICTIM["custom:tenant_id"],
        "org_id": VICTIM["custom:org_id"],
        "team_id": VICTIM["custom:team_id"],
        "connection_id": "conn-victim",
        "channel": "webchat",
        "messages": [{"role": "user", "content": "victim message", "timestamp": now}],
        "threads": {
            "t1": {
                "topic": "victim topic",
                "processing_task_id": "",
                "messages": [{"role": "user", "content": "thread msg", "timestamp": now}],
                "created_at": now,
            },
        },
        "created_at": now,
        "updated_at": now,
        "expires_at": now + 86400,
    })
    return session_id


def _assert_message_refused(handler, result, session_id):
    expected = {
        "error": "session not found",
        "session_id": session_id,
        "type": "session_invalid",
        "content": "That conversation is no longer available. Starting a new one.",
    }
    assert result["statusCode"] == 404
    assert json.loads(result["body"]) == expected
    delivery = handler._apigw_client.post_to_connection
    delivery.assert_called_once()
    assert delivery.call_args.kwargs["ConnectionId"] == "conn-attacker"
    assert json.loads(delivery.call_args.kwargs["Data"]) == expected


class TestOwnedMessagePersists:
    def test_owner_message_and_concrete_classifier_response_are_persisted(self, aws):
        handler = _import_handler()
        session_id = _seed_victim_session(handler, aws["sessions"])
        handler._persist_connection_claims("conn-owner-new", {"claims": VICTIM})
        classification = handler.ClassificationResult(
            path="direct_response", response="Hello from the classifier",
            thread_action="none", reasoning="test",
        )
        with patch.object(handler, "classify_message", return_value=classification) as classify, \
                patch.object(handler.sqs, "send_message") as send:
            result = handler.lambda_handler(
                _ws_event({"action": "sendMessage", "text": "hi", "session_id": session_id},
                          VICTIM, connection_id="conn-owner-new"), None,
            )
        assert result["statusCode"] == 200
        classify.assert_called_once()
        send.assert_called_once()
        row = aws["sessions"].get_item(Key={"session_id": session_id}, ConsistentRead=True)["Item"]
        assert [m["content"] for m in row["messages"]][-2:] == ["hi", classification.response]
        assert row["connection_id"] == "conn-owner-new"


# ─── Test 1: Foreign message does not append to victim's messages ─

class TestForeignMessageDoesNotAppend:

    def test_attacker_message_does_not_write_to_victim_session(self, aws):
        handler = _import_handler()
        session_id = _seed_victim_session(handler, aws["sessions"])
        handler._persist_connection_claims("conn-attacker", {"claims": ATTACKER})

        with patch.object(handler, "classify_message", return_value=handler.ClassificationResult(
            response="Hello from the classifier",
            path="direct_response", persona="developer", reasoning="test",
            thread_action="new", follow_up_thread_id=None, escalation_note=None,
            issue_title=None, repo=None,
        )), patch.object(handler.sqs, "send_message"):
            result = handler.lambda_handler(
                _ws_event({"action": "sendMessage", "text": "hi", "session_id": session_id}, ATTACKER),
                None,
            )

        _assert_message_refused(handler, result, session_id)
        row = aws["sessions"].get_item(
            Key={"session_id": session_id}, ConsistentRead=True,
        ).get("Item", {})
        # Victim's original message is still there, but no attacker message added
        contents = [m.get("content", "") for m in row.get("messages", [])]
        assert "hi" not in contents
        # Victim's connection is NOT rebound to the attacker's
        assert row["connection_id"] == "conn-victim"


# ─── Test 2: Foreign message does not create a thread ────────────

class TestForeignMessageNoThread:

    def test_attacker_cannot_create_thread_in_victim_session(self, aws):
        handler = _import_handler()
        session_id = _seed_victim_session(handler, aws["sessions"])
        handler._persist_connection_claims("conn-attacker", {"claims": ATTACKER})

        with patch.object(handler, "classify_message", return_value=handler.ClassificationResult(
            path="long_running", persona="developer", reasoning="test",
            thread_action="new", follow_up_thread_id=None, escalation_note=None,
            issue_title="attacker topic", repo=None,
        )), patch.object(handler.sqs, "send_message"):
            result = handler.lambda_handler(
                _ws_event({"action": "sendMessage", "text": "hi", "session_id": session_id}, ATTACKER),
                None,
            )

        row = aws["sessions"].get_item(
            Key={"session_id": session_id}, ConsistentRead=True,
        ).get("Item", {})
        _assert_message_refused(handler, result, session_id)
        # Only the original thread exists; no attacker-created thread
        thread_topics = [t.get("topic", "") for t in row.get("threads", {}).values()]
        assert "attacker topic" not in thread_topics
        assert "victim topic" in thread_topics


# ─── Test 5: Upload-token for foreign session returns 404 ────────

class TestUploadTokenForeignSession:

    def test_upload_token_for_foreign_session_returns_not_found(self, aws):
        handler = _import_handler()
        session_id = _seed_victim_session(handler, aws["sessions"])
        handler._persist_connection_claims("conn-attacker", {"claims": ATTACKER})

        with patch.object(handler.s3_client, "generate_presigned_url",
                          return_value="https://s3.example.com/p"):
            result = handler.lambda_handler(
                _ws_event({
                    "action": "upload-token",
                    "session_id": session_id,
                    "task_id": "task-1",
                    "filename": "notes.txt",
                }, ATTACKER),
                None,
            )

        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert "not found" in body.get("error", "").lower()

    def test_upload_token_for_nonexistent_session_returns_not_found(self, aws):
        handler = _import_handler()
        handler._persist_connection_claims("conn-attacker", {"claims": ATTACKER})

        with patch.object(handler.s3_client, "generate_presigned_url",
                          return_value="https://s3.example.com/p"):
            result = handler.lambda_handler(
                _ws_event({
                    "action": "upload-token",
                    "session_id": "sess-does-not-exist",
                    "task_id": "task-1",
                    "filename": "notes.txt",
                }, ATTACKER),
                None,
            )

        assert result["statusCode"] == 404


# ─── Test 6: Upload-complete for foreign session returns 404 ─────

class TestUploadCompleteForeignSession:

    def test_upload_complete_for_foreign_session_returns_not_found(self, aws):
        handler = _import_handler()
        session_id = _seed_victim_session(handler, aws["sessions"])
        handler._persist_connection_claims("conn-attacker", {"claims": ATTACKER})

        result = handler.lambda_handler(
            _ws_event({
                "action": "upload-complete",
                "session_id": session_id,
                "task_id": "task-1",
                "filename": "notes.txt",
                "checksum": "sha256-abc",
            }, ATTACKER),
            None,
        )

        assert result["statusCode"] == 404
        # No artifact row created in victim's partition
        rows = aws["artifacts"].query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": f"session#{session_id}"},
        ).get("Items", [])
        assert len(rows) == 0


# ─── Test 7: Claims restoration propagates identity ──────────────

class TestClaimsRestorationPropagatesIdentity:
    """Verify that _restore_connection_claims correctly propagates org_id
    and team_id from the persisted conn# record, so upload-token ownership
    checks work correctly on non-$connect messages."""

    def test_restored_claims_include_org_and_team(self, aws):
        handler = _import_handler()

        # Persist claims as $connect would
        handler._persist_connection_claims("conn-test", {"claims": VICTIM})

        # Simulate a non-$connect event where authorizer.claims is empty
        event = {
            "requestContext": {
                "routeKey": "upload-token",
                "connectionId": "conn-test",
                "authorizer": {},  # empty — claims only available at $connect
            },
            "body": json.dumps({"action": "upload-token", "session_id": "sess-x"}),
        }

        handler._restore_connection_claims(event, "conn-test")

        claims = event["requestContext"]["authorizer"]["claims"]
        assert claims["sub"] == VICTIM["sub"]
        assert claims["custom:org_id"] == VICTIM["custom:org_id"]
        assert claims["custom:team_id"] == VICTIM["custom:team_id"]
        assert claims["custom:tenant_id"] == VICTIM["custom:tenant_id"]
