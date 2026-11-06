"""Product binding routes enforce both Product and live connection scope."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from src.auth.dependencies import get_current_user
from src.products.models import Product, ProductOperation, ProductSourceBinding
from src.shared.models.vault import ChannelTenantMap
from tests.products.test_i01 import HEADERS, REQUEST


async def setup_source(test_client, db_session, source_member):
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    path = f"/v1/products/{created.json()['product_id']}/projects/{created.json()['project_id']}/bindings"
    connection = ChannelTenantMap(
        org_id=source_member.org_id,
        id="run-owned-connection",
        provider="github",
        provider_scope_id="27",
        installation_id="17",
        ownership_disputed=False,
    )
    db_session.add(connection)
    await db_session.commit()
    selection = {
        "kind": "repository",
        "provider": "github",
        "namespace": "27",
        "native_id": "0035",
        "connection_ref": {"kind": "github_installation", "id": connection.id},
    }
    return path, selection


def mutation(revision, key):
    return {"If-Profile-Revision": str(revision), "Idempotency-Key": key}


@pytest.mark.asyncio
async def test_route_creates_and_verifies_exact_selected_identity(test_client, db_session, source_member, monkeypatch):
    path, selection = await setup_source(test_client, db_session, source_member)
    created = await test_client.post(path, json=selection, headers=mutation(1, "create_binding_123456"))
    assert created.status_code == 201, created.text
    assert created.json()["selection"] == selection
    binding_id = created.json()["id"]
    assert created.json()["status"] == "unverified" and created.headers["Profile-Revision"] == "2"
    replay = await test_client.post(path, json=selection, headers=mutation(1, "create_binding_123456"))
    assert replay.status_code == 200 and replay.json()["id"] == binding_id
    duplicate = await test_client.post(path, json=selection, headers=mutation(2, "duplicate_binding_1234"))
    assert duplicate.status_code == 200 and duplicate.json()["id"] == binding_id
    assert (await test_client.get(path)).json()["items"][0]["selection"] == selection
    assert (await db_session.scalar(select(func.count()).select_from(ProductSourceBinding))) == 1

    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    calls = []

    async def fake_observe(binding, installation):
        calls.append((binding.native_id, installation.id))
        return {
            "coverage": "complete",
            "status": "healthy",
            "observed": {
                "observed_at": observed_at.isoformat(),
                "stale_after": (observed_at + timedelta(days=1)).isoformat(),
                "source_revision": "fixture-revision",
                "namespace": binding.namespace,
                "native_id": binding.native_id,
            },
        }

    monkeypatch.setattr("src.products.source_routes.observe_binding", fake_observe)
    verify = await test_client.post(f"{path}/{binding_id}/verify", headers=mutation(2, "verify_binding_123456"))
    assert verify.status_code == 202, verify.text
    assert verify.json()["status"] == "succeeded" and verify.json()["coverage"] == "complete"
    assert calls == [("0035", "run-owned-connection")]
    monkeypatch.setattr("src.products.source_routes.utcnow", lambda: observed_at + timedelta(minutes=1))
    verified = (await test_client.get(path)).json()["items"][0]
    assert verified["status"] == "healthy" and verified["source_revision"] == "fixture-revision"
    assert verified["observed_at"] == observed_at.isoformat() and verified["selection"] == selection
    again = await test_client.post(f"{path}/{binding_id}/verify", headers=mutation(2, "verify_binding_123456"))
    assert again.json() == verify.json() and len(calls) == 1
    assert (await db_session.get(Product, (source_member.org_id, path.split("/")[3]))).revision == 2
    assert (await db_session.scalar(select(func.count()).select_from(ProductOperation).where(ProductOperation.operation == "verifyBinding"))) == 1


@pytest.mark.asyncio
async def test_route_denies_unowned_connection_and_unknown_product(test_client, db_session, app, source_member):
    path, selection = await setup_source(test_client, db_session, source_member)
    selection["connection_ref"]["id"] = "another-connection"
    denied = await test_client.post(path, json=selection, headers=mutation(1, "denied_binding_123456"))
    assert denied.status_code == 404
    assert (await db_session.scalar(select(func.count()).select_from(ProductSourceBinding))) == 0
    foreign = source_member.model_copy(update={"org_id": "other-tenant", "user_id": "foreign-user"})
    app.dependency_overrides[get_current_user] = lambda: foreign
    assert (await test_client.get(path)).status_code == 404
    assert (await test_client.post(path, json=selection, headers=mutation(1, "other_denied_123456"))).status_code == 404


@pytest.mark.asyncio
async def test_unsupported_adapter_reports_unavailable_without_false_verification(test_client, db_session, source_member):
    path, selection = await setup_source(test_client, db_session, source_member)
    selection["kind"] = "tracker"
    created = await test_client.post(path, json=selection, headers=mutation(1, "create_tracker_123456"))
    assert created.status_code == 201, created.text
    verified = await test_client.post(f"{path}/{created.json()['id']}/verify", headers=mutation(2, "verify_tracker_123456"))
    assert verified.status_code == 202 and verified.json()["coverage"] == "unavailable"
    binding = await db_session.get(ProductSourceBinding, (source_member.org_id, created.json()["id"]))
    assert binding.observed is None and binding.status == "unverified"
