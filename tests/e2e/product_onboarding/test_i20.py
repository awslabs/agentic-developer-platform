"""Local I20 Product/UI contract scenarios; deployed browser acceptance is separate."""

import pytest
from sqlalchemy import select

from src.catalog.models import ContributionHead, InitiativeHead, MembershipVersion
from tests.e2e.product_onboarding.test_i18 import CATALOG, catalog_actor, create_portfolio, create_product, record_ref

pytest_plugins = ("modules.gateway.tests.conftest",)


@pytest.mark.asyncio
async def test_future_portfolio_membership_readback(test_client, db_session, catalog_actor):
    actor = catalog_actor
    product = await create_product(test_client, "Scheduled Portfolio", "i20-future-product")
    portfolio = await create_portfolio(test_client, actor, "Future collection", "i20-future-portfolio")
    product_ref = record_ref(actor, product["project_id"], "adp.profile", "product", product["product_id"], "1")
    portfolio_ref = record_ref(actor, product["project_id"], "adp.catalog", "portfolio", portfolio["id"], portfolio["revision"])
    path = f"{CATALOG}/{portfolio['id']}/memberships"
    starts_at = "2099-01-01T00:00:00Z"
    saved = await test_client.post(path, json={"portfolio_ref": portfolio_ref, "product_ref": product_ref,
        "source_refs": [portfolio_ref, product_ref], "valid_from": starts_at},
        headers={"Idempotency-Key": "i20-future-membership"})
    assert saved.status_code == 201, saved.text
    current = await test_client.get(path, params={"effective_at": "2026-10-20T00:00:00Z"})
    assert current.status_code == 200 and current.json()["items"] == []
    confirmed = await test_client.get(path, params={"effective_at": starts_at})
    assert confirmed.status_code == 200, confirmed.text
    assert len(confirmed.json()["items"]) == 1
    membership = confirmed.json()["items"][0]
    assert membership["relationship_id"] == saved.json()["id"]
    assert membership["relationship_revision"] == saved.json()["revision"]
    assert membership["product_ref"] == product_ref
    assert len((await db_session.scalars(select(MembershipVersion))).all()) == 1
    observed = await test_client.get(f"/v1/products/{product['product_id']}/projects/{product['project_id']}")
    assert observed.status_code == 200 and observed.json()["revision"] == 1


@pytest.mark.asyncio
async def optional_portfolio_ui(test_client, db_session, catalog_actor):
    actor = catalog_actor
    product = await create_product(test_client, "Optional Portfolio", "i20-portfolio-product")
    path = f"/v1/products/{product['product_id']}/projects/{product['project_id']}"
    original = await test_client.get(path)
    assert original.status_code == 200, original.text
    assert original.json()["product"]["name"] == "Optional Portfolio"
    assert original.json()["revision"] == 1
    assert (await db_session.scalars(select(MembershipVersion))).all() == []

    portfolios = [await create_portfolio(test_client, actor, name, f"i20-portfolio-{index}")
                  for index, name in enumerate(("Commerce", "Shared services"))]
    catalog = await test_client.get(CATALOG)
    assert catalog.status_code == 200 and catalog.json()["can_edit"] is True
    assert {item["id"] for item in catalog.json()["items"]} == {item["id"] for item in portfolios}
    product_ref = record_ref(actor, product["project_id"], "adp.profile", "product", product["product_id"],
                             str(original.json()["revision"]))
    receipts = []
    for index, portfolio in enumerate(portfolios):
        portfolio_ref = record_ref(actor, product["project_id"], "adp.catalog", "portfolio", portfolio["id"], portfolio["revision"])
        saved = await test_client.post(f"{CATALOG}/{portfolio['id']}/memberships",
            json={"portfolio_ref": portfolio_ref, "product_ref": product_ref,
                  "source_refs": [portfolio_ref, product_ref], "valid_from": "2020-01-01T00:00:00Z"},
            headers={"Idempotency-Key": f"i20-portfolio-add-{index}"})
        assert saved.status_code == 201, saved.text
        receipts.append(saved.json())

    for portfolio, receipt in zip(portfolios, receipts):
        observed = await test_client.get(f"{CATALOG}/{portfolio['id']}/memberships",
                                         params={"effective_at": "2026-10-20T00:00:00Z"})
        assert observed.status_code == 200, observed.text
        assert [(row["relationship_id"], row["relationship_revision"], row["valid_from"], row["product_ref"])
                for row in observed.json()["items"]] == [
                    (receipt["id"], receipt["revision"], "2020-01-01T00:00:00Z", product_ref)]
    persisted = (await db_session.scalars(select(MembershipVersion))).all()
    assert {(row.portfolio_id, row.product_id, row.project_id) for row in persisted} == {
        (portfolio["id"], product["product_id"], product["project_id"]) for portfolio in portfolios
    }
    ended = await test_client.patch(f"{CATALOG}/{portfolios[0]['id']}/memberships/{receipts[0]['id']}",
        json={"valid_to": "2021-01-01T00:00:00Z"},
        headers={"Idempotency-Key": "i20-portfolio-end", "If-Match": receipts[0]["revision"]})
    assert ended.status_code == 200, ended.text
    closed = await test_client.get(f"{CATALOG}/{portfolios[0]['id']}/memberships",
                                   params={"effective_at": "2026-10-20T00:00:00Z"})
    assert closed.status_code == 200 and closed.json()["items"] == []
    remaining = await test_client.get(f"{CATALOG}/{portfolios[1]['id']}/memberships",
                                      params={"effective_at": "2026-10-20T00:00:00Z"})
    assert {item["relationship_id"] for item in remaining.json()["items"]} == {receipts[1]["id"]}
    unchanged = await test_client.get(path)
    assert unchanged.json()["product"] == original.json()["product"]
    assert unchanged.json()["revision"] == original.json()["revision"]

@pytest.mark.asyncio
async def initiative_selection_ui(test_client, db_session, catalog_actor):
    actor = catalog_actor
    products = [await create_product(test_client, name, f"i20-initiative-product-{index}")
                for index, name in enumerate(("Checkout", "Accounts"))]
    created = await test_client.post("/v1/catalog/initiatives",
        json={"name": "Improve signup", "status": "planned"},
        headers={"Idempotency-Key": "i20-cross-product-initiative"})
    assert created.status_code == 201, created.text
    selected = created.json()
    catalog = await test_client.get("/v1/catalog/initiatives")
    assert catalog.status_code == 200 and {item["id"] for item in catalog.json()["items"]} == {selected["id"]}
    assert catalog.json()["items"][0]["revision"] == selected["revision"]

    for index, product in enumerate(products):
        base = f"/v1/products/{product['product_id']}/projects/{product['project_id']}"
        started = await test_client.post(f"{base}/onboarding-sessions", json={}, headers={
            "Idempotency-Key": f"i20-initiative-session-{index}", "If-Profile-Revision": "1"})
        assert started.status_code == 201, started.text
        reference = record_ref(actor, product["project_id"], "adp.catalog", "initiative", selected["id"], selected["revision"])
        saved = await test_client.put(f"{base}/onboarding-sessions/{started.json()['id']}/steps/financials",
            json={"action": "save", "configuration": {"financials": {"initiatives": [reference]}}},
            headers={"Idempotency-Key": f"i20-initiative-select-{index}", "If-Profile-Revision": "1"})
        assert saved.status_code == 200, saved.text
        profile = await test_client.get(base)
        assert profile.status_code == 200, profile.text
        assert profile.json()["product_id"] == product["product_id"]
        assert profile.json()["project_id"] == product["project_id"]
        assert profile.json()["configuration"]["financials"]["initiatives"] == [reference]
    assert (await db_session.scalars(select(ContributionHead))).all() == []
    assert len((await db_session.scalars(select(InitiativeHead))).all()) == 1

    for index, (product, objective) in enumerate(zip(products, ("Improve checkout", "Improve account setup"))):
        project_id = product["project_id"]
        current = await test_client.get(f"/v1/products/{product['product_id']}/projects/{project_id}")
        payload = {"initiative_ref": record_ref(actor, project_id, "adp.catalog", "initiative", selected["id"], selected["revision"]),
                   "product_ref": record_ref(actor, project_id, "adp.profile", "product", product["product_id"],
                                             str(current.json()["revision"])),
                   "owner_ref": {"kind": "user", "id": actor.user_id},
                   "objective": objective, "valid_from": "2020-01-01T00:00:00Z", "work_links": []}
        committed = await test_client.post(f"/v1/catalog/projects/{project_id}/contributions", json=payload,
            headers={"Idempotency-Key": f"i20-initiative-contribution-{index}"})
        assert committed.status_code == 201, committed.text
        contribution = await test_client.get(f"/v1/catalog/projects/{project_id}/contributions")
        assert contribution.status_code == 200, contribution.text
        assert [(item["id"], item["data"]["initiative_ref"], item["data"]["product_ref"], item["data"]["objective"])
                for item in contribution.json()["items"]] == [
                    (committed.json()["id"], payload["initiative_ref"], payload["product_ref"], objective)]
    assert len((await db_session.scalars(select(ContributionHead))).all()) == 2
    assert len((await db_session.scalars(select(InitiativeHead))).all()) == 1

@pytest.mark.asyncio
async def catalog_ui_failure_states(test_client, db_session, catalog_actor, monkeypatch):
    from fastapi import HTTPException
    from sqlalchemy import update

    from src.catalog import routes as catalog_routes
    from src.shared.models.onboarding import TenantMembership

    actor = catalog_actor
    product = await create_product(test_client, "Catalog Fallback", "i20-failure-product")
    base = f"/v1/products/{product['product_id']}/projects/{product['project_id']}"
    visible = await create_portfolio(test_client, actor, "Visible collection", "i20-failure-visible")
    hidden = await create_portfolio(test_client, actor, "Restricted collection", "i20-failure-hidden")
    portfolio_ref = record_ref(actor, product["project_id"], "adp.catalog", "portfolio", visible["id"], visible["revision"])
    product_ref = record_ref(actor, product["project_id"], "adp.profile", "product", product["product_id"])
    linked = await test_client.post(f"{CATALOG}/{visible['id']}/memberships", json={
        "portfolio_ref": portfolio_ref, "product_ref": product_ref, "source_refs": [portfolio_ref, product_ref],
        "valid_from": "2020-01-01T00:00:00Z"}, headers={"Idempotency-Key": "i20-failure-link"})
    assert linked.status_code == 201, linked.text
    original_digest = catalog_routes.scope_digest

    async def unavailable(*args, **kwargs):
        raise HTTPException(status_code=503, detail={"code": "dependency_unavailable", "message": "catalog unavailable"})

    monkeypatch.setattr(catalog_routes, "scope_digest", unavailable)
    failed = await test_client.get(CATALOG)
    assert failed.status_code == 503 and failed.json()["code"] == "dependency_unavailable"
    updated = await test_client.patch(base,
        json={"name": "Catalog Fallback", "purpose": "Edited while catalog was down", "intended_users": ["Maintainers"]},
        headers={"Idempotency-Key": "i20-failure-product-edit", "If-Profile-Revision": "1"})
    assert updated.status_code == 200, updated.text
    profile = await test_client.get(base)
    assert profile.status_code == 200 and profile.json()["product"]["purpose"] == "Edited while catalog was down"
    assert profile.json()["revision"] == updated.json()["revision"]
    monkeypatch.setattr(catalog_routes, "scope_digest", original_digest)

    closed = await test_client.patch(f"{CATALOG}/{visible['id']}/memberships/{linked.json()['id']}",
        json={"valid_to": "2030-01-01T00:00:00Z"},
        headers={"Idempotency-Key": "i20-failure-close", "If-Match": linked.json()["revision"]})
    assert closed.status_code == 200 and closed.json()["revision"] == "2"
    versions = (await db_session.scalars(select(MembershipVersion))).all()
    assert len(versions) == 2
    stale = await test_client.patch(f"{CATALOG}/{visible['id']}/memberships/{linked.json()['id']}",
        json={"valid_to": "2031-01-01T00:00:00Z"},
        headers={"Idempotency-Key": "i20-failure-stale", "If-Match": linked.json()["revision"]})
    assert stale.status_code == 412 and stale.json()["code"] == "stale_revision"
    assert len((await db_session.scalars(select(MembershipVersion))).all()) == len(versions)
    latest = await test_client.get(f"{CATALOG}/{visible['id']}/memberships",
                                   params={"effective_at": "2026-10-20T00:00:00Z"})
    assert latest.status_code == 200 and latest.json()["items"][0]["relationship_revision"] == "2"

    await db_session.execute(update(TenantMembership).where(
        TenantMembership.user_id == actor.user_id, TenantMembership.tenant_id == actor.org_id
    ).values(role="member"))
    await db_session.commit()
    authorized = await test_client.get(CATALOG)
    assert authorized.status_code == 200 and authorized.json()["can_edit"] is False
    assert {item["id"] for item in authorized.json()["items"]} == {visible["id"]}
    assert hidden["id"] not in str(authorized.json()) and "Restricted collection" not in str(authorized.json())
    assert "total" not in authorized.json()
    denied = await test_client.patch(f"{CATALOG}/{visible['id']}/memberships/{linked.json()['id']}",
        json={"valid_to": "2032-01-01T00:00:00Z"},
        headers={"Idempotency-Key": "i20-failure-revoked", "If-Match": "2"})
    assert denied.status_code == 403 and denied.json()["code"] == "permission_denied"
    assert len((await db_session.scalars(select(MembershipVersion))).all()) == len(versions)
    ordinary = await test_client.get(base)
    assert ordinary.status_code == 200 and ordinary.json()["product"]["purpose"] == "Edited while catalog was down"
