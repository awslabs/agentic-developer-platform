"""
Unit tests for upload-token and upload-complete endpoints (Stage C, #186).

Tests:
  1. upload-token returns presigned URL scoped to correct hierarchical key
  2. upload-token rejects missing identity claims
  3. upload-token rejects missing required fields
  4. upload-token rejects files exceeding size limit
  5. upload-complete writes DDB catalog row
  6. upload-complete returns existing artifact for duplicate sha256 (idempotent)
  7. upload-complete rejects missing required fields
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws


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
    monkeypatch.setenv("INPUT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/tasks")
    monkeypatch.setenv("RESPONSE_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/resp.fifo")
    monkeypatch.setenv("SESSIONS_TABLE_NAME", "sessions")
    monkeypatch.setenv("ARTIFACTS_BUCKET", "test-artifacts-bucket")
    monkeypatch.setenv("ARTIFACTS_TABLE", "test-artifacts-table")
    monkeypatch.setenv("AWS_REGION_NAME", "us-east-1")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "")
    monkeypatch.setenv("SLACK_BOT_USER_ID", "")


@pytest.fixture
def mocked_aws(mock_env):
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="sessions",
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        artifacts_table = ddb.create_table(
            TableName="test-artifacts-table",
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
        sqs = boto3.client("sqs", region_name="us-east-1")
        sqs.create_queue(QueueName="tasks")
        sqs.create_queue(QueueName="resp.fifo", Attributes={"FifoQueue": "true"})
        yield {"ddb": ddb, "artifacts_table": artifacts_table}


def _import_handler():
    for mod_name in list(sys.modules.keys()):
        if mod_name in ("handler", "classifier", "channels", "channels.base",
                        "channels.webchat", "channels.slack", "github_dispatch"):
            del sys.modules[mod_name]
    import handler
    return handler


def _make_ws_event(body: dict, claims: dict | None = None) -> dict:
    """Make a WebSocket event with connection claims."""
    if claims is None:
        claims = {
            "sub": "user-1",
            "custom:org_id": "org-1",
            "custom:team_id": "team-A",
        }
    return {
        "requestContext": {
            "connectionId": "conn-123",
            "routeKey": "$default",
            "authorizer": {"claims": claims},
        },
        "body": json.dumps(body),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestUploadToken:
    def test_returns_presigned_url_with_hierarchical_key(self, mocked_aws):
        handler = _import_handler()

        # Persist connection claims first
        handler._persist_connection_claims("conn-123", {
            "claims": {"sub": "user-1", "custom:org_id": "org-1", "custom:team_id": "team-A"},
        })

        event = _make_ws_event({
            "action": "upload-token",
            "session_id": "sess-1",
            "filename": "report.pdf",
            "content_type": "application/pdf",
            "size_bytes": 1024,
        })

        with patch.object(handler.s3_client, "generate_presigned_url",
                          return_value="https://s3.example.com/presigned") as mock_presign:
            result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["upload_url"] == "https://s3.example.com/presigned"
        assert "s3_key" in body
        # Key should follow hierarchical format
        assert body["s3_key"].startswith("o/org-1/t/team-A/u/user-1/s/sess-1/")
        assert body["s3_key"].endswith("/in/report.pdf")
        assert body["expires_in"] == 3600

    def test_rejects_missing_identity(self, mocked_aws):
        handler = _import_handler()

        # Persist connection claims WITHOUT sub
        handler._persist_connection_claims("conn-123", {
            "claims": {},
        })

        event = _make_ws_event(
            {"action": "upload-token", "session_id": "sess-1", "filename": "f.txt"},
            claims={},  # no sub
        )

        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 401

    def test_rejects_missing_fields(self, mocked_aws):
        handler = _import_handler()

        handler._persist_connection_claims("conn-123", {
            "claims": {"sub": "user-1", "custom:org_id": "org-1", "custom:team_id": "team-A"},
        })

        event = _make_ws_event({
            "action": "upload-token",
            # missing session_id and filename
        })

        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 400

    def test_rejects_oversized_file(self, mocked_aws):
        handler = _import_handler()

        handler._persist_connection_claims("conn-123", {
            "claims": {"sub": "user-1", "custom:org_id": "org-1", "custom:team_id": "team-A"},
        })

        event = _make_ws_event({
            "action": "upload-token",
            "session_id": "sess-1",
            "filename": "huge.bin",
            "size_bytes": 100 * 1024 * 1024,  # 100 MB
        })

        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 400
        assert "too large" in json.loads(result["body"])["error"]


class TestUploadComplete:
    def test_writes_catalog_row(self, mocked_aws):
        handler = _import_handler()

        handler._persist_connection_claims("conn-123", {
            "claims": {"sub": "user-1", "custom:org_id": "org-1", "custom:team_id": "team-A"},
        })

        event = _make_ws_event({
            "action": "upload-complete",
            "session_id": "sess-1",
            "task_id": "task-1",
            "s3_key": "o/org-1/t/team-A/u/user-1/s/sess-1/task-1/in/doc.pdf",
            "filename": "doc.pdf",
            "content_type": "application/pdf",
            "size_bytes": 2048,
            "checksum": "sha256hex123",
        })

        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["artifact_id"].startswith("art_")
        assert body["deduplicated"] is False

    def test_deduplicates_by_checksum(self, mocked_aws):
        handler = _import_handler()

        handler._persist_connection_claims("conn-123", {
            "claims": {"sub": "user-1", "custom:org_id": "org-1", "custom:team_id": "team-A"},
        })

        # First upload
        event1 = _make_ws_event({
            "action": "upload-complete",
            "session_id": "sess-1",
            "task_id": "task-1",
            "s3_key": "o/org-1/t/team-A/u/user-1/s/sess-1/task-1/in/doc.pdf",
            "filename": "doc.pdf",
            "content_type": "application/pdf",
            "size_bytes": 2048,
            "checksum": "sha256dup",
        })

        result1 = handler.lambda_handler(event1, None)
        body1 = json.loads(result1["body"])
        first_id = body1["artifact_id"]
        assert body1["deduplicated"] is False

        # Second upload with same checksum
        event2 = _make_ws_event({
            "action": "upload-complete",
            "session_id": "sess-1",
            "task_id": "task-2",
            "s3_key": "o/org-1/t/team-A/u/user-1/s/sess-1/task-2/in/doc.pdf",
            "filename": "doc.pdf",
            "content_type": "application/pdf",
            "size_bytes": 2048,
            "checksum": "sha256dup",
        })

        result2 = handler.lambda_handler(event2, None)
        body2 = json.loads(result2["body"])
        assert body2["artifact_id"] == first_id
        assert body2["deduplicated"] is True

    def test_rejects_missing_fields(self, mocked_aws):
        handler = _import_handler()

        handler._persist_connection_claims("conn-123", {
            "claims": {"sub": "user-1", "custom:org_id": "org-1", "custom:team_id": "team-A"},
        })

        event = _make_ws_event({
            "action": "upload-complete",
            "session_id": "sess-1",
            # missing s3_key, filename, checksum
        })

        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 400
