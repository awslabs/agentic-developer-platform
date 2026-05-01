"""
Pytest configuration and fixtures for webhook ingress integration tests.

Fixtures:
- webhook_endpoint: API Gateway URL for the webhook ingress endpoint
- webhook_secret: Shared secret for HMAC signature validation
- sqs_client: boto3 SQS client configured for the submit queue
- ddb_client: boto3 DynamoDB resource for event log assertions
- test_tenant: Seeds a test tenant in tenant-registry, cleans up after suite
"""

from __future__ import annotations

import os
import time
import uuid

import boto3
import pytest

# ---------------------------------------------------------------------------
# Environment resolution
# ---------------------------------------------------------------------------

_REGION = os.environ.get("AWS_REGION", "us-east-1")
_ENV = os.environ.get("TEST_ENV", "dev")


def _get_env_or_ssm(env_var: str, ssm_key: str, default: str = "") -> str:
    """Resolve value from environment variable, falling back to SSM Parameter Store."""
    value = os.environ.get(env_var, "")
    if value:
        return value
    if not ssm_key:
        return default
    try:
        ssm = boto3.client("ssm", region_name=_REGION)
        resp = ssm.get_parameter(Name=ssm_key, WithDecryption=True)
        return resp["Parameter"]["Value"]
    except Exception:
        return default


def _get_secret(secret_id: str) -> str:
    """Retrieve a secret value from Secrets Manager."""
    try:
        sm = boto3.client("secretsmanager", region_name=_REGION)
        resp = sm.get_secret_value(SecretId=secret_id)
        return resp["SecretString"]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def webhook_endpoint() -> str:
    """API Gateway URL for the webhook ingress endpoint.

    Reads from WEBHOOK_ENDPOINT env var or SSM parameter.
    """
    endpoint = _get_env_or_ssm(
        "WEBHOOK_ENDPOINT",
        f"/adp/{_ENV}/webhook-ingress/endpoint",
    )
    if not endpoint:
        pytest.skip(
            "WEBHOOK_ENDPOINT not set and SSM parameter not found. "
            "Set WEBHOOK_ENDPOINT or deploy the webhook-ingress stack."
        )
    return endpoint.rstrip("/")


@pytest.fixture(scope="session")
def webhook_secret() -> str:
    """Webhook HMAC secret for signing test payloads.

    Reads from WEBHOOK_SECRET env var or Secrets Manager.
    """
    secret = os.environ.get("WEBHOOK_SECRET", "")
    if not secret:
        secret = _get_secret(f"adp/{_ENV}/webhook-ingress/webhook-secret")
    if not secret:
        pytest.skip(
            "WEBHOOK_SECRET not set and Secrets Manager secret not found. "
            "Set WEBHOOK_SECRET or deploy the webhook-ingress stack."
        )
    return secret


@pytest.fixture(scope="session")
def sqs_client():
    """SQS client and queue URL for the webhook submit queue.

    Returns dict with 'client' and 'queue_url' keys.
    """
    queue_url = _get_env_or_ssm(
        "WEBHOOK_SQS_QUEUE_URL",
        f"/adp/{_ENV}/webhook-ingress/submit-queue-url",
    )
    if not queue_url:
        pytest.skip("WEBHOOK_SQS_QUEUE_URL not available.")
    client = boto3.client("sqs", region_name=_REGION)
    return {"client": client, "queue_url": queue_url}


@pytest.fixture(scope="session")
def ddb_client():
    """DynamoDB resource and table names for webhook event log assertions.

    Returns dict with 'resource', 'events_table', 'tenant_table' keys.
    """
    events_table = _get_env_or_ssm(
        "WEBHOOK_EVENTS_TABLE",
        f"/adp/{_ENV}/webhook-ingress/events-table",
        default=f"adp-{_ENV}-webhook-events",
    )
    tenant_table = _get_env_or_ssm(
        "WEBHOOK_TENANT_TABLE",
        f"/adp/{_ENV}/webhook-ingress/tenant-registry-table",
        default=f"adp-{_ENV}-tenant-registry",
    )
    resource = boto3.resource("dynamodb", region_name=_REGION)
    return {
        "resource": resource,
        "events_table": resource.Table(events_table),
        "tenant_table": resource.Table(tenant_table),
    }


@pytest.fixture(scope="session")
def test_tenant(ddb_client):
    """Seed a test tenant in the tenant-registry table.

    Uses installation_id 99999999 to avoid conflicts with real installs.
    Cleans up after the test session.
    """
    tenant_table = ddb_client["tenant_table"]
    installation_id = "99999999"
    tenant_id = f"test-tenant-{uuid.uuid4().hex[:8]}"

    tenant_item = {
        "installation_id": installation_id,
        "tenant_id": tenant_id,
        "org_name": "test-org-e2e",
        "status": "active",
        "plan": "pro",
        "rate_limit_rpm": 60,
        "created_at": int(time.time()),
        "ttl": int(time.time()) + 3600,  # Auto-expire in 1 hour
    }

    try:
        tenant_table.put_item(Item=tenant_item)
    except Exception as e:
        pytest.skip(f"Could not seed test tenant: {e}")

    yield {
        "installation_id": installation_id,
        "tenant_id": tenant_id,
        "org_name": "test-org-e2e",
        "item": tenant_item,
    }

    # Cleanup
    try:
        tenant_table.delete_item(Key={"installation_id": installation_id})
    except Exception:
        pass


@pytest.fixture(scope="session")
def rate_limited_tenant(ddb_client):
    """Seed a tenant with an exhausted rate limit for rate-limit testing.

    Uses installation_id 99999998 with rate_limit_rpm=0.
    """
    tenant_table = ddb_client["tenant_table"]
    installation_id = "99999998"
    tenant_id = f"test-tenant-ratelimited-{uuid.uuid4().hex[:8]}"

    tenant_item = {
        "installation_id": installation_id,
        "tenant_id": tenant_id,
        "org_name": "test-org-ratelimited",
        "status": "active",
        "plan": "free",
        "rate_limit_rpm": 0,  # Exhausted
        "created_at": int(time.time()),
        "ttl": int(time.time()) + 3600,
    }

    try:
        tenant_table.put_item(Item=tenant_item)
    except Exception as e:
        pytest.skip(f"Could not seed rate-limited tenant: {e}")

    yield {
        "installation_id": installation_id,
        "tenant_id": tenant_id,
        "org_name": "test-org-ratelimited",
        "item": tenant_item,
    }

    # Cleanup
    try:
        tenant_table.delete_item(Key={"installation_id": installation_id})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def cleanup_ddb_events(ddb_client, request):
    """Track and clean up DDB event log entries created during tests."""
    delivery_ids: list[str] = []

    class _Tracker:
        def track(self, delivery_id: str) -> None:
            delivery_ids.append(delivery_id)

    tracker = _Tracker()
    request.node._webhook_tracker = tracker
    yield tracker

    # Cleanup after test
    events_table = ddb_client["events_table"]
    for delivery_id in delivery_ids:
        try:
            events_table.delete_item(Key={"delivery_id": delivery_id})
        except Exception:
            pass


@pytest.fixture
def unique_delivery_id():
    """Generate a unique delivery ID for each test invocation."""
    return f"test-{uuid.uuid4().hex[:12]}"
