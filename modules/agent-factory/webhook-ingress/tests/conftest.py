"""Shared fixtures for webhook-ingress tests."""

from __future__ import annotations

import os
import sys
import uuid

import boto3
import pytest
from moto import mock_aws

# Add lambda source to path for imports
LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda")
sys.path.insert(0, LAMBDA_DIR)


# =============================================================================
# Unit test fixtures (mocked AWS)
# =============================================================================


@pytest.fixture
def aws_credentials(monkeypatch):
    """Mock AWS credentials for moto."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def ddb_webhook_events_table(aws_credentials):
    """Create a mocked webhook-events DynamoDB table."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
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
        table.meta.client.get_waiter("table_exists").wait(TableName="adp-dev-webhook-events")
        yield table


@pytest.fixture
def ddb_rate_limits_table(aws_credentials):
    """Create a mocked rate-limits DynamoDB table."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="adp-dev-rate-limits",
            KeySchema=[
                {"AttributeName": "tenant_id", "KeyType": "HASH"},
                {"AttributeName": "window", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "tenant_id", "AttributeType": "S"},
                {"AttributeName": "window", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="adp-dev-rate-limits")
        yield table


# =============================================================================
# Integration test fixtures (live AWS environment)
# =============================================================================

_INTEGRATION_SKIP_MSG = (
    "Integration fixtures require WEBHOOK_ENDPOINT, WEBHOOK_SECRET, and "
    "WEBHOOK_SQS_QUEUE_URL environment variables. Set them or run via CI."
)


def _require_env(name: str) -> str:
    """Return env var value or skip the test."""
    val = os.environ.get(name)
    if not val:
        pytest.skip(f"Missing required env var: {name}")
    return val


@pytest.fixture
def webhook_endpoint() -> str:
    """API Gateway endpoint URL for the webhook ingress.

    Resolved from WEBHOOK_ENDPOINT env var, set by CI workflow from
    SSM parameter /adp/<env>/webhook-ingress/endpoint.
    """
    return _require_env("WEBHOOK_ENDPOINT")


@pytest.fixture
def webhook_secret() -> str:
    """HMAC secret for signing webhook payloads.

    Resolved from WEBHOOK_SECRET env var. In CI the workflow fetches the
    secret value from Secrets Manager using the ARN in SSM.
    """
    return _require_env("WEBHOOK_SECRET")


@pytest.fixture
def webhook_sqs_queue_url() -> str:
    """SQS queue URL for webhook dispatch assertions.

    Resolved from WEBHOOK_SQS_QUEUE_URL env var, set by CI workflow from
    SSM parameter /adp/<env>/webhook-ingress/sqs-queue-url.
    """
    return _require_env("WEBHOOK_SQS_QUEUE_URL")


@pytest.fixture
def sqs_client(webhook_sqs_queue_url):
    """Live SQS client + queue URL for integration tests.

    Returns dict with 'client' (boto3 SQS client) and 'queue_url'.
    """
    region = os.environ.get("AWS_REGION", "us-east-1")
    client = boto3.client("sqs", region_name=region)
    return {"client": client, "queue_url": webhook_sqs_queue_url}


@pytest.fixture
def unique_delivery_id() -> str:
    """Generate a unique delivery ID for each test to avoid collision."""
    return f"test-{uuid.uuid4().hex[:16]}"


@pytest.fixture
def test_tenant():
    """Tenant registration details for integration tests.

    Resolved from WEBHOOK_TEST_INSTALLATION_ID and WEBHOOK_TEST_TENANT_ID
    env vars. These correspond to a row in the tenant-registry DynamoDB table
    pre-seeded by the deploy workflow.
    """
    installation_id = os.environ.get("WEBHOOK_TEST_INSTALLATION_ID", "12345678")
    tenant_id = os.environ.get("WEBHOOK_TEST_TENANT_ID", "test-tenant-e2e")
    return {
        "installation_id": installation_id,
        "tenant_id": tenant_id,
    }


@pytest.fixture
def rate_limited_tenant():
    """Tenant that has exceeded its rate limit for integration tests.

    Resolved from WEBHOOK_RATE_LIMITED_INSTALLATION_ID env var.
    Pre-seeded with exhausted rate-limit counters by the deploy workflow.
    """
    installation_id = os.environ.get("WEBHOOK_RATE_LIMITED_INSTALLATION_ID", "99999999")
    tenant_id = os.environ.get("WEBHOOK_RATE_LIMITED_TENANT_ID", "rate-limited-tenant-e2e")
    return {
        "installation_id": installation_id,
        "tenant_id": tenant_id,
    }


@pytest.fixture
def ddb_client():
    """Live DynamoDB resource for integration test assertions.

    Returns dict with 'events_table' (DynamoDB Table resource for webhook-events).
    Table name resolved from WEBHOOK_EVENTS_TABLE env var or SSM parameter.
    """
    region = os.environ.get("AWS_REGION", "us-east-1")
    table_name = os.environ.get("WEBHOOK_EVENTS_TABLE", "adp-dev-webhook-events")
    ddb = boto3.resource("dynamodb", region_name=region)
    events_table = ddb.Table(table_name)
    return {"events_table": events_table}


class _DdbEventCleanup:
    """Tracks delivery IDs and cleans up webhook-events DDB entries after tests."""

    def __init__(self, table_name: str, region: str):
        self._table_name = table_name
        self._region = region
        self._delivery_ids: list[str] = []

    def track(self, delivery_id: str) -> None:
        """Register a delivery_id for post-test cleanup."""
        self._delivery_ids.append(delivery_id)

    def cleanup(self) -> None:
        """Best-effort delete of tracked event records."""
        if not self._delivery_ids:
            return
        try:
            ddb = boto3.resource("dynamodb", region_name=self._region)
            table = ddb.Table(self._table_name)
            for did in self._delivery_ids:
                # Scan for items matching delivery_id (may be stored as event_id or attribute)
                resp = table.scan(
                    FilterExpression="delivery_id = :did OR event_id = :did",
                    ExpressionAttributeValues={":did": did},
                    ProjectionExpression="event_id, arrived_at",
                )
                for item in resp.get("Items", []):
                    table.delete_item(
                        Key={
                            "event_id": item["event_id"],
                            "arrived_at": item["arrived_at"],
                        }
                    )
        except Exception:
            pass  # Best-effort cleanup


@pytest.fixture
def cleanup_ddb_events():
    """Fixture to clean up webhook-events DDB entries after integration tests."""
    region = os.environ.get("AWS_REGION", "us-east-1")
    table_name = os.environ.get("WEBHOOK_EVENTS_TABLE", "adp-dev-webhook-events")
    cleaner = _DdbEventCleanup(table_name, region)
    yield cleaner
    cleaner.cleanup()
