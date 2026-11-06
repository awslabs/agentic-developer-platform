"""I21 client against actual catalog routes, with local DB and fixture identity."""

import asyncio
import io
from datetime import UTC, datetime
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest
from sqlalchemy import update

from modules.insights.emitter.registry_adapter import CatalogAPI, CatalogRevisionUnavailable
from src.catalog.models import PortfolioVersion
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User


pytest_plugins = ("modules.gateway.tests.conftest",)


@pytest.fixture
async def catalog_reader(db_session, app, valid_token_context, test_client, monkeypatch):
    from src.auth.dependencies import get_current_user

    actor = valid_token_context
    db_session.add(Organization(id=actor.org_id, name="Example workspace"))
    db_session.add(User(id=actor.user_id, org_id=actor.org_id, team_id=actor.team_id,
                        email="reader@example.test", user_kind="human"))
    await db_session.flush()
    db_session.add(TenantMembership(user_id=actor.user_id, tenant_id=actor.org_id,
                                    role="org_admin", is_active=True))
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: actor
    monkeypatch.setenv("BG_CATALOG_ENABLED", "true")
    loop = asyncio.get_running_loop()

    def open_gateway(request, *, timeout):
        response = asyncio.run_coroutine_threadsafe(
            test_client.get(urlsplit(request.full_url).path, headers=dict(request.header_items())), loop
        ).result(timeout=timeout)
        if response.status_code != 200:
            raise HTTPError(request.full_url, response.status_code, "catalog unavailable", {}, None)
        return io.BytesIO(response.content)

    client = CatalogAPI("https://gateway.example/v1/catalog", lambda: {}, open_gateway)
    return actor, client


@pytest.mark.asyncio
async def test_i21_exact_catalog_read_and_changed_revision(test_client, db_session, catalog_reader):
    actor, client = catalog_reader
    data = {"name": "Original portfolio", "lifecycle_status": "active",
            "owner_ref": {"kind": "user", "id": actor.user_id}}
    created = await test_client.post("/v1/catalog/portfolios", json=data,
                                    headers={"Idempotency-Key": "i21-catalog-read"})
    assert created.status_code == 201, created.text
    receipt = created.json()
    reference = {"tenant_id": actor.org_id, "project_id": "example-project",
                 "source_connection_id": "adp.catalog", "record_type": "portfolio",
                 "record_id": receipt["id"], "record_revision": receipt["revision"]}
    record = await asyncio.to_thread(client.get_record, reference)
    stored = await db_session.get(PortfolioVersion, (actor.org_id, "adp.catalog", receipt["id"], 1))
    assert record["data"] == stored.data == data
    assert record["revision"] == str(stored.revision) == reference["record_revision"]
    assert record["tenant_id"] == actor.org_id
    assert "source_event_id" not in record

    changed = await test_client.patch(f"/v1/catalog/portfolios/{receipt['id']}",
                                      json={**data, "name": "Changed portfolio"},
                                      headers={"Idempotency-Key": "i21-catalog-update", "If-Match": receipt["revision"]})
    assert changed.status_code == 200, changed.text
    with pytest.raises(CatalogRevisionUnavailable, match="revision_unresolvable"):
        await asyncio.to_thread(client.get_record, reference)
    current = await asyncio.to_thread(client.get_record, {**reference, "record_revision": changed.json()["revision"]})
    assert current["data"]["name"] == "Changed portfolio"


@pytest.mark.asyncio
async def test_i21_catalog_revocation_is_rechecked(test_client, db_session, catalog_reader):
    actor, client = catalog_reader
    created = await test_client.post("/v1/catalog/portfolios",
        json={"name": "Revocation example", "lifecycle_status": "active",
              "owner_ref": {"kind": "user", "id": actor.user_id}},
        headers={"Idempotency-Key": "i21-catalog-revoke"})
    assert created.status_code == 201, created.text
    receipt = created.json()
    reference = {"tenant_id": actor.org_id, "project_id": "example-project",
                 "source_connection_id": "adp.catalog", "record_type": "portfolio",
                 "record_id": receipt["id"], "record_revision": receipt["revision"]}
    assert (await asyncio.to_thread(client.get_record, reference))["id"] == receipt["id"]
    await db_session.execute(update(TenantMembership).where(
        TenantMembership.user_id == actor.user_id, TenantMembership.tenant_id == actor.org_id
    ).values(revoked_at=datetime.now(UTC)))
    await db_session.commit()
    with pytest.raises(CatalogRevisionUnavailable, match="revision_unresolvable"):
        await asyncio.to_thread(client.get_record, reference)
