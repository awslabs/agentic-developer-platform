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
            status="accepted",
        )

        assert item["event_id"] == "delivery-123"
        assert item["tenant_id"] == "acme-corp"
        assert item["GSI1PK"] == "acme-corp"
        assert item["channel"] == "github"
        assert item["event_type"] == "issues"
        assert item["action"] == "labeled"
        assert item["status"] == "accepted"
        assert item["installation_id"] == "99887766"
        assert item["repo"] == "acme-corp/app"
        assert "arrived_at" in item
        assert "expires_at" in item

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
            status="accepted",
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
            status="accepted",
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
            status="accepted",
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
            status="accepted",
        )
        logger.log_event(
            event_id="t2-ev1",
            tenant_id="tenant-b",
            channel="github",
            event_type="issues",
            action="labeled",
            status="accepted",
        )
        logger.log_event(
            event_id="t1-ev2",
            tenant_id="tenant-a",
            channel="github",
            event_type="pull_request",
            action="opened",
            status="accepted",
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
            status="accepted",
        )

        # Query with a future timestamp — should return nothing
        results = logger.query_by_tenant(
            tenant_id="tenant-x",
            since="2099-01-01T00:00:00Z",
        )

        assert len(results) == 0

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
            ],
            BillingMode="PAY_PER_REQUEST",
        )
