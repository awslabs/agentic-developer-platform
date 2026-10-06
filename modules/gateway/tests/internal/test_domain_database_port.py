"""Domain trust reads use a separately selected credential and schema."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from src.internal import domain_operation_store as store
from src.internal.domain_operation_store import DomainBinding


@pytest.fixture
def binding():
    return DomainBinding(
        domain="superplane",
        org_id="domain",
        adp_org_id="adp",
        producer_registry_id="producer",
        worker_registry_id="worker",
        database_secret_id="shared-secret",
        database_schema="operations",
        queue_url="https://sqs.us-east-1.amazonaws.com/123456789012/paid",
        worker_namespace="domain",
        worker_service_account="worker",
        worker_container="worker",
        worker_image_digests=("sha256:" + "a" * 64,),
        repo="owner/repo",
        observation_url="https://observe.example",
        observation_credential_secret_id="observation",
        domain_database_secret_id="domain-secret",
        domain_database_schema="superplane",
    )


@pytest.mark.asyncio
async def test_domain_connection_resolves_only_domain_secret_and_never_harness_schema(binding, monkeypatch):
    secret = AsyncMock(return_value='{"dsn":"postgresql://domain-host/domain"}')
    monkeypatch.setattr(store, "secret", secret)
    monkeypatch.setattr(store, "database_ssl", lambda: object())
    connection = SimpleNamespace(fetchval=AsyncMock(return_value="superplane"), close=AsyncMock())
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr(store.asyncpg, "connect", connect)
    harness = Mock(side_effect=AssertionError("domain does not own Harness schema"))
    monkeypatch.setattr(store, "harness", harness)
    async with store.domain_connect(binding) as value:
        assert value is connection
    secret.assert_awaited_once_with("domain-secret")
    assert connect.call_args.kwargs["server_settings"] == {"search_path": "superplane,public"}
    connection.close.assert_awaited_once()
    harness.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"domain_database_secret_id": ""},
        {"domain_database_secret_id": "shared-secret"},
        {"domain_database_schema": "operations"},
        {"domain_database_schema": "public"},
        {"domain_database_schema": "superplane;public"},
    ],
)
async def test_domain_port_never_falls_back_to_shared_credentials(binding, monkeypatch, fields):
    secret = AsyncMock()
    monkeypatch.setattr(store, "secret", secret)
    with pytest.raises(HTTPException) as refused:
        await store.domain_database_dsn(replace(binding, **fields))
    assert refused.value.status_code == 503
    secret.assert_not_called()
