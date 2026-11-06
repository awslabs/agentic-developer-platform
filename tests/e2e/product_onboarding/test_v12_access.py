"""Local V12 actor isolation; actual provider sessions require deployed verification."""

import pytest
from sqlalchemy import update


pytest_plugins = ("modules.gateway.tests.conftest", "tests.e2e.product_onboarding.test_i18")


@pytest.mark.asyncio
async def test_cross_product_initiative_does_not_grant_other_scope_or_disclose_counts(
    test_client, db_session, app, catalog_actor, monkeypatch,
):
    from datetime import UTC, datetime
    from src.catalog import initiative_routes
    from src.auth.dependencies import get_current_user
    from src.products.models import Product
    from src.shared.models.onboarding import TenantMembership
    from src.shared.models.organization import User
    from tests.e2e.product_onboarding.test_i18 import CATALOG, create_portfolio, link_product
    from tests.e2e.product_onboarding.test_i19 import create_product, record_ref

    actor = catalog_actor
    allowed_user_id = "v12-scoped-member"

    async def local_work(caller, reference):
        return (caller.org_id == actor.org_id and caller.user_id in (actor.user_id, allowed_user_id)
                and reference.source_connection_id == "example.native-tracker")

    monkeypatch.setattr(initiative_routes, "native_work_verified", local_work)
    first = await create_product(test_client, db_session, actor, "Visible Scope")
    second = await create_product(test_client, db_session, actor, "Private Scope")
    visible_product = await db_session.get(Product, (actor.org_id, first["product_id"]))
    visible_product.team_id = actor.team_id
    private_product = await db_session.get(Product, (actor.org_id, second["product_id"]))
    private_product.team_id = "private-team"
    await db_session.commit()

    initiative = await test_client.post("/v1/catalog/initiatives",
        json={"name": "Shared improvement", "status": "planned"},
        headers={"Idempotency-Key": "v12_access_initiative"})
    assert initiative.status_code == 201, initiative.text
    created = {}
    for index, product in enumerate((first, second)):
        project_id = product["project_id"]
        portfolio = await create_portfolio(test_client, actor, f"Scope {index} Portfolio",
                                           f"v12_access_portfolio_{index}")
        await link_product(test_client, actor, portfolio, product, f"v12_access_membership_{index}")
        payload = {"initiative_ref": record_ref(actor, project_id, "adp.catalog", "initiative", initiative.json()["id"]),
                   "product_ref": record_ref(actor, project_id, "adp.profile", "product", product["product_id"]),
                   "objective": f"Scope {index} private outcome", "owner_ref": {"kind": "user", "id": actor.user_id},
                   "valid_from": "2026-10-19T00:00:00Z", "work_links": [
                       {"work_ref": record_ref(actor, project_id, "example.native-tracker", "story", f"story-{index}"),
                        "valid_from": "2026-10-19T00:00:00Z"}]}
        path = f"/v1/catalog/projects/{project_id}/contributions"
        receipt = await test_client.post(path, json=payload, headers={"Idempotency-Key": f"v12_access_contribution_{index}"})
        assert receipt.status_code == 201, receipt.text
        created[project_id] = {"portfolio": portfolio, "receipt": receipt.json(), "path": path,
                               "objective": payload["objective"]}

    authorized = actor.model_copy(update={"user_id": allowed_user_id})
    db_session.add(User(id=authorized.user_id, org_id=actor.org_id, team_id=actor.team_id,
                        email="scoped-member@example.test", user_kind="human"))
    await db_session.flush()
    db_session.add(TenantMembership(user_id=authorized.user_id, tenant_id=actor.org_id,
                                    role="member", is_active=True))
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: authorized

    first_path = created[first["project_id"]]["path"]
    private_path = created[second["project_id"]]["path"]
    visible = await test_client.get(first_path)
    assert visible.status_code == 200 and len(visible.json()["items"]) == 1
    assert visible.json()["items"][0]["data"]["objective"] == created[first["project_id"]]["objective"]
    assert [link["work_ref"]["record_id"] for link in visible.json()["items"][0]["data"]["work_links"]] == ["story-0"]
    listed = await test_client.get("/v1/catalog/initiatives")
    assert listed.status_code == 200 and {item["id"] for item in listed.json()["items"]} == {initiative.json()["id"]}
    first_memberships = await test_client.get(f"{CATALOG}/{created[first['project_id']]['portfolio']['id']}/memberships")
    assert first_memberships.status_code == 200 and len(first_memberships.json()["items"]) == 1

    private = await test_client.get(private_path)
    absent = await test_client.get("/v1/catalog/projects/unknown/contributions")
    assert private.status_code == absent.status_code == 404 and private.json() == absent.json()
    private_detail = await test_client.get(f"{private_path}/{created[second['project_id']]['receipt']['id']}")
    assert private_detail.status_code == 404 and private_detail.json() == absent.json()
    denied_portfolio = await test_client.get(f"{CATALOG}/{created[second['project_id']]['portfolio']['id']}/memberships")
    assert denied_portfolio.status_code == 404
    denied_profile = await test_client.get(f"/v1/products/{second['product_id']}/projects/{second['project_id']}")
    assert denied_profile.status_code == 404
    for response in (listed, visible, first_memberships, private, private_detail, denied_portfolio, denied_profile):
        assert "total" not in response.json()
        if response in (private, private_detail, denied_portfolio, denied_profile):
            assert created[second["project_id"]]["objective"] not in response.text
            assert "story-1" not in response.text

    app.dependency_overrides[get_current_user] = lambda: actor
    second_path = f"{private_path}/{created[second['project_id']]['receipt']['id']}"
    corrected_payload = {
        "initiative_ref": record_ref(actor, second["project_id"], "adp.catalog", "initiative", initiative.json()["id"]),
        "product_ref": record_ref(actor, second["project_id"], "adp.profile", "product", second["product_id"]),
        "objective": "Corrected private outcome", "owner_ref": {"kind": "user", "id": actor.user_id},
        "valid_from": "2026-10-19T00:00:00Z", "work_links": [
            {"work_ref": record_ref(actor, second["project_id"], "example.native-tracker", "story", "story-1"),
             "valid_from": "2026-10-19T00:00:00Z"}],
    }
    corrected = await test_client.patch(second_path, json=corrected_payload,
        headers={"Idempotency-Key": "v12_access_correction", "If-Match": created[second["project_id"]]["receipt"]["revision"]})
    assert corrected.status_code == 200 and corrected.json()["revision"] == "2", corrected.text
    app.dependency_overrides[get_current_user] = lambda: authorized
    visible_after = await test_client.get(first_path)
    assert visible_after.status_code == 200 and len(visible_after.json()["items"]) == 1
    assert [link["work_ref"]["record_id"] for link in visible_after.json()["items"][0]["data"]["work_links"]] == ["story-0"]
    for path in (private_path, second_path):
        denied = await test_client.get(path)
        assert denied.status_code == 404 and denied.json() == absent.json()
        assert "total" not in denied.json() and "story-1" not in denied.text
        assert "Corrected private outcome" not in denied.text
    after_correction = await test_client.get("/v1/catalog/initiatives")
    assert after_correction.status_code == 200 and len(after_correction.json()["items"]) == 1
    assert "total" not in after_correction.json()

    await db_session.execute(update(TenantMembership).where(
        TenantMembership.user_id == authorized.user_id, TenantMembership.tenant_id == actor.org_id,
    ).values(is_active=False, revoked_at=datetime.now(UTC)))
    await db_session.commit()
    for path in (first_path, private_path, "/v1/catalog/initiatives"):
        revoked = await test_client.get(path)
        assert revoked.status_code == 403 and "total" not in revoked.json()
        assert "story-0" not in revoked.text and "story-1" not in revoked.text
