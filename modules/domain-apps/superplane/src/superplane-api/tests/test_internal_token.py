"""HTTP regression coverage for the shared-token internal routes."""

import uuid
from unittest.mock import AsyncMock

import pytest
from app.config import settings
from app.main import app, vault_sync_reconciler


@pytest.mark.parametrize(
    "method,path,route_path,body",
    [
        (
            "patch",
            f"/internal/clusters/{uuid.uuid4()}/resources",
            "/internal/clusters/{cluster_id}/resources",
            {},
        ),
        (
            "post",
            "/internal/heartbeat",
            "/internal/heartbeat",
            {"cluster_id": str(uuid.uuid4()), "health_status": "Healthy"},
        ),
        ("post", "/internal/cost-reconcile", "/internal/cost-reconcile", {}),
        ("post", "/internal/vault-sync/trigger", "/internal/vault-sync/trigger", {}),
    ],
)
@pytest.mark.parametrize(
    "headers",
    [
        None,
        [(b"authorization", b"Basic wrong")],
        [(b"authorization", b"Bearer ")],
        [(b"authorization", b"Bearer wrong")],
        [(b"authorization", b"Bearer \xfc")],
    ],
    ids=["missing", "wrong-scheme", "empty-bearer", "wrong-ascii", "raw-non-ascii"],
)
async def test_invalid_internal_token_never_reaches_handler(
    client, internal_token_header, monkeypatch, method, path, route_path, body, headers
):
    route = next(route for route in app.routes if route.path == route_path)
    protected_handler = AsyncMock(side_effect=AssertionError("protected handler ran"))
    monkeypatch.setattr(route.dependant, "call", protected_handler)

    response = await getattr(client, method)(path, json=body, headers=headers)

    assert response.status_code == 401
    protected_handler.assert_not_awaited()


async def test_unconfigured_internal_token_refuses_valid_header(
    client, internal_token_header, monkeypatch
):
    trigger_sync = AsyncMock()
    monkeypatch.setattr(vault_sync_reconciler, "trigger_sync", trigger_sync)
    monkeypatch.setattr(settings, "internal_api_token", "")

    response = await client.post(
        "/internal/vault-sync/trigger", json={}, headers=internal_token_header
    )

    assert response.status_code == 401
    trigger_sync.assert_not_awaited()


async def test_exact_internal_token_invokes_protected_handler(
    client, internal_token_header, monkeypatch
):
    trigger_sync = AsyncMock(return_value={})
    monkeypatch.setattr(vault_sync_reconciler, "trigger_sync", trigger_sync)

    response = await client.post(
        "/internal/vault-sync/trigger", json={}, headers=internal_token_header
    )

    assert response.status_code == 200
    trigger_sync.assert_awaited_once_with(credential_id=None, cluster_id=None)
