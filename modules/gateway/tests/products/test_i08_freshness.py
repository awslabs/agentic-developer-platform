"""Binding reads preserve the original observation and never disclose revoked sources."""

from datetime import UTC, datetime, timedelta

import pytest

from src.products.models import Product, ProductSourceBinding
from src.shared.models.vault import ChannelTenantMap
from tests.products.test_i08_api import mutation, setup_source


@pytest.mark.asyncio
async def test_expired_observation_degrades_without_rewriting_evidence(test_client, db_session, source_member, monkeypatch):
    path, selection = await setup_source(test_client, db_session, source_member)
    created = await test_client.post(path, json=selection, headers=mutation(1, "create_stale_binding_123"))
    assert created.status_code == 201, created.text
    binding = await db_session.get(ProductSourceBinding, (source_member.org_id, created.json()["id"]))
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    binding.status = "healthy"
    binding.observed = {
        "observed_at": observed_at.isoformat(),
        "stale_after": (observed_at + timedelta(minutes=15)).isoformat(),
        "source_revision": "original-revision",
        "native_id": selection["native_id"],
    }
    await db_session.commit()

    monkeypatch.setattr("src.products.source_routes.utcnow", lambda: observed_at + timedelta(minutes=14))
    current = (await test_client.get(path)).json()["items"][0]
    assert current["status"] == "healthy" and current["observed_at"] == observed_at.isoformat()
    monkeypatch.setattr("src.products.source_routes.utcnow", lambda: observed_at + timedelta(minutes=15))
    expired = (await test_client.get(path)).json()["items"][0]
    assert expired["status"] == "degraded" and expired["observed_at"] == current["observed_at"]
    assert expired["stale_after"] == current["stale_after"] and expired["source_revision"] == "original-revision"
    assert binding.status == "healthy" and binding.observed["observed_at"] == observed_at.isoformat()
    product_id = path.split("/")[3]
    assert (await db_session.get(Product, (source_member.org_id, product_id))).revision == 2


@pytest.mark.asyncio
async def test_revoked_status_and_lost_connection_redact_every_identity(test_client, db_session, source_member):
    path, selection = await setup_source(test_client, db_session, source_member)
    created = await test_client.post(path, json=selection, headers=mutation(1, "create_revoked_binding_123"))
    assert created.status_code == 201
    binding = await db_session.get(ProductSourceBinding, (source_member.org_id, created.json()["id"]))
    expected = {
        "id": binding.id,
        "project_id": binding.project_id,
        "revision": binding.revision,
        "generation": binding.generation,
        "status": "restricted",
    }
    binding.status = "revoked"
    await db_session.commit()
    assert (await test_client.get(path)).json()["items"] == [expected]

    binding.status = "healthy"
    binding.observed = {"observed_at": "2026-01-01T00:00:00Z", "source_revision": "private-revision"}
    mapping = await db_session.get(ChannelTenantMap, selection["connection_ref"]["id"])
    mapping.ownership_disputed = True
    await db_session.commit()
    response = await test_client.get(path)
    assert response.status_code == 200 and response.json()["items"] == [expected]
    assert "0035" not in response.text and "private-revision" not in response.text


@pytest.mark.asyncio
async def test_missing_or_invalid_freshness_never_reports_healthy(test_client, db_session, source_member):
    path, selection = await setup_source(test_client, db_session, source_member)
    created = await test_client.post(path, json=selection, headers=mutation(1, "create_invalid_stale_123"))
    assert created.status_code == 201
    binding = await db_session.get(ProductSourceBinding, (source_member.org_id, created.json()["id"]))
    binding.status = "healthy"
    binding.observed = {"observed_at": "unparseable", "stale_after": "2026-01-01T00:00:00Z"}
    await db_session.commit()
    assert (await test_client.get(path)).json()["items"][0]["status"] == "degraded"
