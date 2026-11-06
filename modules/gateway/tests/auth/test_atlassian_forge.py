"""Pairing, current scope, token replacement and revoked installation behavior."""

import base64
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.requests import Request

from src.auth import atlassian_forge_routes as forge
from src.auth import atlassian_system_routes as system
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.models.atlassian_system import AtlassianSystemConnection
from src.shared.models.base import utcnow
from tests.auth.test_connections_routes import caller
from tests.products.test_jira_context import session as connection_session
from tests.products.test_jira_context import setup as connection_setup

session = connection_session
service_setup = connection_setup

SITE = "12345678-1234-1234-1234-123456789abc"
INSTALL = "ari:cloud:ecosystem::installation/22222222-2222-2222-2222-222222222222"


@pytest.fixture
async def configured(session, service_setup, monkeypatch):
    monkeypatch.setenv("ADP_ATLASSIAN_FORGE_APP_ID", "ari:cloud:ecosystem::app/11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("ADP_ATLASSIAN_FORGE_ENVIRONMENT_ID", "33333333-3333-3333-3333-333333333333")
    monkeypatch.setenv("ADP_ATLASSIAN_FORGE_INSTALL_URL", "https://developer.atlassian.com/console/install/example")
    monkeypatch.setattr(forge, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-test-oauth-user"))
    monkeypatch.setattr(forge, "provision_key", AsyncMock())
    monkeypatch.setattr(forge, "update_token", AsyncMock())
    monkeypatch.setattr(
        forge,
        "adapter_read",
        AsyncMock(
            return_value={
                "account_id": "forge-app-account",
                "schemas": {"jira:issues": {}},
                "installation_id": INSTALL,
                "site_id": SITE,
                "expires_at": int((utcnow() + timedelta(hours=2)).timestamp()),
            }
        ),
    )
    actor = service_setup[0]
    pairing = await forge.start_pairing(forge.StartPairing(label="App", product_ids=["product-a"]), actor, session)
    return actor, pairing, service_setup[2]


def delivery(token="never-store-this-token-in-db", installation=INSTALL, site=SITE):
    # Only lookup hints: authenticating this FIT is separately exercised in the
    # adapter's real RSA tests. This suite verifies core cannot act before it.
    claims = {"app": {"installationId": installation, "apiBaseUrl": "https://api.atlassian.com/ex/jira/" + site}}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return Request(
        {"type": "http", "headers": [(b"authorization", f"Bearer header.{payload}.signature".encode()), (b"x-forge-oauth-system", token.encode())]}
    )


async def test_pairing_keeps_service_account_and_activates_only_after_app_verification(session, configured):
    actor, pairing, service = configured
    row = await session.get(AtlassianSystemConnection, pairing["connection"]["id"])
    assert row.pairing_hash != pairing["pairing_code"] and row.status == "unverified"
    with pytest.raises(HTTPException):
        await system.current_system(session, row.credential_id, actor.org_id, "product-a")
    await forge.accept_delivery(delivery(), session, pairing["pairing_code"])
    credential, row = await system.current_system(session, row.credential_id, actor.org_id, "product-a")
    assert row.connection_method == "forge_app" and row.provider_account_id == "forge-app-account"
    assert row.pairing_hash is None
    assert "never-store-this-token-in-db" not in repr(row.__dict__) + repr(credential.__dict__)
    assert forge.provision_key.await_count == 1
    await system.current_system(session, service["id"], actor.org_id, "product-a")
    with pytest.raises(HTTPException):
        await forge.accept_delivery(delivery(), session, pairing["pairing_code"])
    assert forge.provision_key.await_count == 1


async def test_failed_or_expired_pairing_never_stores_tokens(session, configured, monkeypatch):
    _, pairing, _ = configured
    monkeypatch.setattr(forge, "adapter_read", AsyncMock(side_effect=HTTPException(403)))
    with pytest.raises(HTTPException):
        await forge.accept_delivery(delivery(), session, pairing["pairing_code"])
    forge.provision_key.assert_not_awaited()
    row = await session.get(AtlassianSystemConnection, pairing["connection"]["id"])
    row.pairing_expires_at = utcnow() - timedelta(seconds=1)
    await session.commit()
    forge.adapter_read.reset_mock()
    with pytest.raises(HTTPException):
        await forge.accept_delivery(delivery(), session, pairing["pairing_code"])
    forge.adapter_read.assert_not_awaited()


async def test_refresh_preserves_cursors_and_revocation_stops_late_callbacks(session, configured):
    actor, pairing, _ = configured
    await forge.accept_delivery(delivery(), session, pairing["pairing_code"])
    credential, row = await system.current_system(session, pairing["connection"]["id"], actor.org_id, "product-a")
    revision = row.revision
    await forge.accept_delivery(delivery(), session)
    forge.update_token.assert_not_awaited()  # cached delivery, no rotation backwards
    forge.adapter_read.return_value["expires_at"] += 1200
    await forge.accept_delivery(delivery(token="next-token"), session)
    assert forge.update_token.await_count == 1
    assert row.revision == revision
    with pytest.raises(HTTPException):
        await system.current_system(session, row.credential_id, actor.org_id, "unassigned-product")
    await system.disconnect_system_connection(row.credential_id, actor, session)
    forge.adapter_read.reset_mock()
    with pytest.raises(HTTPException):
        await forge.accept_delivery(delivery(token="late-token"), session)
    forge.adapter_read.assert_not_awaited()
    assert forge.update_token.await_count == 1


async def test_user_and_tenant_cannot_claim_app_setup_or_change_sharing(session, configured):
    actor, pairing, _ = configured
    with pytest.raises(HTTPException) as error:
        await forge.start_pairing(forge.StartPairing(label="bad", product_ids=["product-a"]), caller(), session)
    assert error.value.status_code == 403
    alien = actor.model_copy(update={"org_id": "other-tenant"})
    with pytest.raises(HTTPException):
        await forge.update_access(
            pairing["connection"]["id"],
            forge.AccessUpdate(label="bad", product_ids=["product-a"], expected_revision=pairing["connection"]["revision"]),
            alien,
            session,
        )


async def test_repairing_cannot_move_existing_product_sources_to_another_site(session, configured):
    actor, pairing, _ = configured
    await forge.accept_delivery(delivery(), session, pairing["pairing_code"])
    connection_id = pairing["connection"]["id"]
    await system.disconnect_system_connection(connection_id, actor, session)
    row = await session.get(AtlassianSystemConnection, connection_id)
    repair = await forge.start_pairing(
        forge.StartPairing(label="App", product_ids=["product-a"], connection_id=connection_id, expected_revision=row.revision), actor, session
    )
    forge.adapter_read.reset_mock()
    forge.provision_key.reset_mock()
    with pytest.raises(HTTPException) as error:
        await forge.accept_delivery(delivery(site="44444444-4444-4444-4444-444444444444"), session, repair["pairing_code"])
    assert error.value.status_code == 409
    forge.adapter_read.assert_not_awaited()
    forge.provision_key.assert_not_awaited()
    await forge.accept_delivery(delivery(), session, repair["pairing_code"])
    _, row = await system.current_system(session, connection_id, actor.org_id, "product-a")
    assert row.site_id == SITE and row.status == "ready"


async def test_public_pairing_validation_never_echoes_tokens_and_bounds_body(session, configured):
    app = FastAPI()
    app.include_router(forge.router)

    async def db():
        yield session

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_current_user] = lambda: configured[0]
    with TestClient(app) as client:
        response = client.post(
            "/auth/connections/atlassian-mcp/forge/pair", json={"pairing_code": "secret-invalid"}, headers={"x-forge-oauth-system": "private-token"}
        )
        assert response.status_code == 422
        assert "secret-invalid" not in response.text and "private-token" not in response.text
        response = client.post("/auth/connections/atlassian-mcp/forge/pair", content=b"x" * 65537)
        assert response.status_code == 413
    assert len((await session.scalars(select(AtlassianSystemConnection))).all()) == 2
