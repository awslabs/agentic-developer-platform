"""Connection reuse must retain current provider reads and SDK credentials."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from unittest.mock import MagicMock

import boto3
import botocore.session
import pytest
from botocore.credentials import RefreshableCredentials
from fastapi import HTTPException

from src.internal import domain_current_identity as identity
from src.internal import domain_operation_store as store


@pytest.fixture(autouse=True)
def fresh_transport_cache():
    store._aws_client.cache_clear()
    yield
    store._aws_client.cache_clear()


def test_concurrent_transport_construction_preserves_region_and_timeouts(monkeypatch):
    factory = MagicMock(side_effect=lambda *args, **kwargs: object())
    monkeypatch.setattr(store.boto3, "client", factory)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    barrier = Barrier(8)

    def acquire():
        barrier.wait(timeout=5)
        return store.aws_client("dynamodb")

    with ThreadPoolExecutor(max_workers=8) as pool:
        clients = list(pool.map(lambda _: acquire(), range(8)))
    assert all(client is clients[0] for client in clients)
    assert factory.call_count == 1
    options = factory.call_args.kwargs
    assert options["region_name"] == "us-east-1"
    assert options["config"].connect_timeout == 3
    assert options["config"].read_timeout == 15
    assert options["config"].retries == {"total_max_attempts": 2}
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    assert store.aws_client("dynamodb") is not clients[0]
    assert factory.call_args.kwargs["region_name"] == "us-west-2"
    assert store.aws_client("iam") is not store.aws_client("dynamodb")


def test_transport_cache_is_bounded(monkeypatch):
    factory = MagicMock(side_effect=lambda *args, **kwargs: object())
    monkeypatch.setattr(store.boto3, "client", factory)
    for index in range(40):
        store.aws_client(f"service-{index}")
    assert store._aws_client.cache_info().currsize == 32
    store.aws_client("service-0")
    assert factory.call_count == 41


async def test_secret_rotation_and_read_failure_are_observed_on_reused_transport(monkeypatch):
    client = MagicMock()
    client.get_secret_value.side_effect = [
        {"SecretString": "original"},
        {"SecretString": "rotated"},
        RuntimeError("provider unavailable"),
    ]
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(store.boto3, "client", factory)
    assert await store.secret("owned-database") == "original"
    assert await store.secret("owned-database") == "rotated"
    with pytest.raises(HTTPException) as refused:
        await store.secret("owned-database")
    assert refused.value.status_code == 503
    assert client.get_secret_value.call_count == 3
    assert factory.call_count == 1


def test_disabled_human_is_observed_on_reused_transport(monkeypatch):
    client = MagicMock()
    client.list_users.return_value = {"Users": [{"Username": "login"}]}
    attributes = [{"Name": "sub", "Value": "subject"}, {"Name": "custom:org_id", "Value": "tenant"}]
    client.admin_get_user.side_effect = [
        {"Enabled": True, "UserAttributes": attributes},
        {"Enabled": False, "UserAttributes": attributes},
    ]
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(store.boto3, "client", factory)
    monkeypatch.setattr(identity, "cognito_user_pool_id", lambda: "pool")
    assert identity._cognito_identity("subject") == (True, "tenant")
    assert identity._cognito_identity("subject") == (False, "tenant")
    assert client.list_users.call_count == client.admin_get_user.call_count == 2
    assert factory.call_count == 1


def test_reused_transport_keeps_refreshable_sdk_credentials(monkeypatch):
    def material(key, expiry):
        return {"access_key": key, "secret_key": "test-secret", "token": "test-token", "expiry_time": expiry.isoformat()}

    now = datetime.now(UTC)
    refresh = MagicMock(return_value=material("refreshed-key", now + timedelta(hours=1)))
    credentials = RefreshableCredentials.create_from_metadata(
        metadata=material("expired-key", now - timedelta(minutes=1)), refresh_using=refresh, method="test-refreshable"
    )
    session = botocore.session.get_session()
    session._credentials = credentials
    factory = MagicMock(side_effect=boto3.Session(botocore_session=session).client)
    monkeypatch.setattr(store.boto3, "client", factory)
    client = store.aws_client("dynamodb")
    assert store.aws_client("dynamodb") is client
    assert client._request_signer._credentials is credentials
    assert client._request_signer._credentials.get_frozen_credentials().access_key == "refreshed-key"
    assert refresh.call_count == factory.call_count == 1
