"""Unit tests for webhook event logging."""

from __future__ import annotations

import time

import boto3
from moto import mock_aws

from common.webhook_events import WebhookEventLogger


class TestWebhookEventLogger:
    """Tests for WebhookEventLogger DDB operations."""

    @mock_aws
    def test_log_event_basic(self, aws_credentials):
        """Log a basic webhook event and verify it's stored."""
        self._create_table()
        logger = WebhookEventLogger(table_name="adp-dev-webhook-events")

        item = logger.log_event(
            event_id="delivery-123",
            tenant_id="acme-corp",
            channel="github",
            event_type="issues",
            action="labeled",
            installation_id="99887766",
            repo="acme-corp/app",
            status="webhook_received",
        )

        assert item["event_id"] == "delivery-123"
        assert item["tenant_id"] == "acme-corp"
        assert item["GSI1PK"] == "acme-corp"
        assert item["channel"] == "github"
        assert item["event_type"] == "issues"
        assert item["action"] == "labeled"
        assert item["status"] == "webhook_received"
        assert item["installation_id"] == "99887766"
        assert item["repo"] == "acme-corp/app"
        assert "arrived_at" in item
        assert "expires_at" in item
        assert "status_updated_at" in item

    @mock_aws
    def test_log_event_generates_uuid_when_no_event_id(self, aws_credentials):
        """event_id is auto-generated UUID when not provided."""
        self._create_table()
        logger = WebhookEventLogger(table_name="adp-dev-webhook-events")

        item = logger.log_event(
            tenant_id="test-tenant",
            channel="github",
            event_type="pull_request",
            action="opened",
            status="webhook_received",
        )

        assert len(item["event_id"]) == 36  # UUID format

    @mock_aws
    def test_log_event_with_error(self, aws_credentials):
        """Error events include error_message."""
        self._create_table()
        logger = WebhookEventLogger(table_name="adp-dev-webhook-events")

        item = logger.log_event(
            event_id="err-456",
            tenant_id="broken-tenant",
            channel="github",
            event_type="issues",
            action="labeled",
            status="error",
            error_message="Tenant not found in registry",
        )

        assert item["status"] == "error"
        assert item["error_message"] == "Tenant not found in registry"

    @mock_aws
    def test_log_event_with_processing_time(self, aws_credentials):
        """Processing time is recorded when provided."""
        self._create_table()
        logger = WebhookEventLogger(table_name="adp-dev-webhook-events")

        item = logger.log_event(
            event_id="perf-789",
            tenant_id="fast-tenant",
            channel="github",
            event_type="issues",
            action="labeled",
            status="webhook_received",
            processing_time_ms=42,
        )

        assert item["processing_time_ms"] == 42

    @mock_aws
    def test_log_event_rate_limited_status(self, aws_credentials):
        """Rate-limited events use status='rate_limited'."""
        self._create_table()
        logger = WebhookEventLogger(table_name="adp-dev-webhook-events")

        item = logger.log_event(
            event_id="rl-001",
            tenant_id="spammy-tenant",
            channel="github",
            event_type="issues",
            action="labeled",
            status="rate_limited",
        )

        assert item["status"] == "rate_limited"

    @mock_aws
    def test_log_event_ttl_is_30_days_from_now(self, aws_credentials):
        """TTL is approximately 30 days from now."""
        self._create_table()
        logger = WebhookEventLogger(table_name="adp-dev-webhook-events")

        now = time.time()
        item = logger.log_event(
            event_id="ttl-test",
            tenant_id="test-tenant",
            channel="github",
            event_type="issues",
            action="labeled",
            status="webhook_received",
        )

        expected_ttl = int(now) + (30 * 24 * 60 * 60)
        # Allow 5 seconds of drift
        assert abs(item["expires_at"] - expected_ttl) < 5

    @mock_aws
    def test_query_by_tenant(self, aws_credentials):
        """Query events for a tenant via GSI1."""
        self._create_table()
        logger = WebhookEventLogger(table_name="adp-dev-webhook-events")

        # Log several events for different tenants
        logger.log_event(
            event_id="t1-ev1",
            tenant_id="tenant-a",
            channel="github",
            event_type="issues",
            action="labeled",
            status="webhook_received",
        )
        logger.log_event(
            event_id="t2-ev1",
            tenant_id="tenant-b",
            channel="github",
            event_type="issues",
            action="labeled",
            status="webhook_received",
        )
        logger.log_event(
            event_id="t1-ev2",
            tenant_id="tenant-a",
            channel="github",
            event_type="pull_request",
            action="opened",
            status="webhook_received",
        )

        # Query for tenant-a
        results = logger.query_by_tenant(
            tenant_id="tenant-a",
            since="2020-01-01T00:00:00Z",
        )

        assert len(results) == 2
        event_ids = {r["event_id"] for r in results}
        assert "t1-ev1" in event_ids
        assert "t1-ev2" in event_ids
        assert "t2-ev1" not in event_ids

    @mock_aws
    def test_query_by_tenant_with_time_filter(self, aws_credentials):
        """Query filters events by arrived_at timestamp."""
        self._create_table()
        logger = WebhookEventLogger(table_name="adp-dev-webhook-events")

        logger.log_event(
            event_id="old-ev",
            tenant_id="tenant-x",
            channel="github",
            event_type="issues",
            action="labeled",
            status="webhook_received",
        )

        # Query with a future timestamp — should return nothing
        results = logger.query_by_tenant(
            tenant_id="tenant-x",
            since="2099-01-01T00:00:00Z",
        )

        assert len(results) == 0

    # =========================================================================
    # Phase 1 (Issue #1455) — enriched fields + key contract
    # =========================================================================

    @mock_aws
    def test_log_event_with_enriched_fields(self, aws_credentials):
        """Enriched event includes user_id, persona, topic, source_url, etc."""
        self._create_table()
        logger = WebhookEventLogger(table_name="adp-dev-webhook-events")

        item = logger.log_event(
            event_id="msg-uuid-123",
            arrived_at="2026-06-13T22:00:00Z",
            tenant_id="acme-corp",
            channel="github",
            event_type="issue_comment",
            action="created",
            installation_id="99887766",
            repo="acme-corp/flagship-app",
            status="webhook_received",
            user_id="usr-42",
            github_login="janedoe",
            persona="developer",
            topic="Fix login bug",
            source_url="https://github.com/acme-corp/flagship-app/issues/99",
            issue_number=99,
            correlation_id="corr-abc-123",
        )

        assert item["event_id"] == "msg-uuid-123"
        assert item["arrived_at"] == "2026-06-13T22:00:00Z"
        assert item["user_id"] == "usr-42"
        assert item["github_login"] == "janedoe"
        assert item["persona"] == "developer"
        assert item["topic"] == "Fix login bug"
        assert item["source_url"] == "https://github.com/acme-corp/flagship-app/issues/99"
        assert item["issue_number"] == 99
        assert item["correlation_id"] == "corr-abc-123"
        assert item["status"] == "webhook_received"
        assert "status_updated_at" in item

    @mock_aws
    def test_log_event_unresolved_user_defaults_to_unattributed(self, aws_credentials):
        """Unresolved sender → row written with user_id='unattributed', not dropped."""
        self._create_table()
        logger = WebhookEventLogger(table_name="adp-dev-webhook-events")

        # Default user_id when not provided
        item = logger.log_event(
            event_id="unresolved-ev",
            tenant_id="some-tenant",
            channel="github",
            event_type="issues",
            action="labeled",
            status="webhook_received",
        )

        assert item["user_id"] == "unattributed"
        # Verify it's actually in DDB
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.Table("adp-dev-webhook-events")
        resp = table.get_item(Key={"event_id": "unresolved-ev", "arrived_at": item["arrived_at"]})
        assert resp["Item"]["user_id"] == "unattributed"

    @mock_aws
    def test_log_event_key_contract_uses_envelope_values(self, aws_credentials):
        """event_id/arrived_at use the provided envelope values (key contract)."""
        self._create_table()
        logger = WebhookEventLogger(table_name="adp-dev-webhook-events")

        # THE KEY CONTRACT: values passed in must be stored as-is
        envelope_message_id = "msg-aaaabbbb-cccc-dddd-eeee-ffffffffffff"
        envelope_arrived_at = "2026-06-13T22:37:58Z"

        item = logger.log_event(
            event_id=envelope_message_id,
            arrived_at=envelope_arrived_at,
            tenant_id="test-tenant",
            channel="github",
            event_type="issue_comment",
            action="created",
            status="webhook_received",
            user_id="usr-99",
        )

        assert item["event_id"] == envelope_message_id
        assert item["arrived_at"] == envelope_arrived_at

        # Verify we can retrieve with the exact keys (what the worker will do)
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.Table("adp-dev-webhook-events")
        resp = table.get_item(
            Key={"event_id": envelope_message_id, "arrived_at": envelope_arrived_at}
        )
        assert "Item" in resp
        assert resp["Item"]["event_id"] == envelope_message_id
        assert resp["Item"]["arrived_at"] == envelope_arrived_at

    @mock_aws
    def test_log_event_topic_truncated_to_120_chars(self, aws_credentials):
        """Topic is truncated to 120 characters."""
        self._create_table()
        logger = WebhookEventLogger(table_name="adp-dev-webhook-events")

        long_topic = "A" * 200
        item = logger.log_event(
            event_id="trunc-test",
            tenant_id="test-tenant",
            channel="github",
            event_type="issues",
            action="labeled",
            status="webhook_received",
            topic=long_topic,
        )

        assert len(item["topic"]) == 120

    @mock_aws
    def test_log_event_exception_does_not_propagate(self, aws_credentials):
        """DDB write failure does NOT raise — best-effort capture."""
        # Use a non-existent table to trigger an error
        logger = WebhookEventLogger(table_name="nonexistent-table", region="us-east-1")

        # Should NOT raise
        item = logger.log_event(
            event_id="fail-test",
            tenant_id="test-tenant",
            channel="github",
            event_type="issues",
            action="labeled",
            status="webhook_received",
        )

        # Item is still returned (the in-memory dict) even if write failed
        assert item["event_id"] == "fail-test"

    def _create_table(self):
        """Helper to create the webhook-events table in moto."""
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="adp-dev-webhook-events",
            KeySchema=[
                {"AttributeName": "event_id", "KeyType": "HASH"},
                {"AttributeName": "arrived_at", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "event_id", "AttributeType": "S"},
                {"AttributeName": "arrived_at", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
                {"AttributeName": "user_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "gsi1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "user-index",
                    "KeySchema": [
                        {"AttributeName": "user_id", "KeyType": "HASH"},
                        {"AttributeName": "arrived_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
