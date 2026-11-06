"""Local V12 contract tests; deployed catalog and browser acceptance is separate."""

import json
import os
from copy import deepcopy

import pytest

from tests.e2e.product_onboarding.product_catalog_fixture import load_membership_fixture, validate_membership_fixture
from tests.e2e.product_onboarding.product_catalog_live_membership import verify_live_membership
from tests.e2e.product_onboarding.product_catalog_live_history import verify_live_history
from tests.e2e.product_onboarding.product_catalog_live_recovery import run_live_recovery
from tests.e2e.product_onboarding.test_i18 import (
    CATALOG,
    create_portfolio,
    create_product,
    link_product,
)

pytest_plugins = ("modules.gateway.tests.conftest", "tests.e2e.product_onboarding.test_i18")


@pytest.fixture
def catalog_live_provider(request):
    if os.environ.get("PRODUCT_CATALOG_LIVE_ENABLED") != "1":
        pytest.skip("Live catalog needs explicit authorization and a verified selected release")
    required = ("PRODUCT_EXPECTED_REVISION", "E2E_CLOUDFRONT_URL", "PRODUCT_RECOVERY_RUN_ID",
                "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")
    if request.node.name == "live_catalog_membership":
        required += ("PRODUCT_CATALOG_FIXTURE_PATH",)
    if request.node.name == "live_catalog_history_access":
        required += ("PRODUCT_CATALOG_AUTHORIZED_TOKEN", "PRODUCT_CATALOG_REVOKED_TOKEN")
    if request.node.name == "live_catalog_recovery":
        required += ("PRODUCT_CATALOG_CLEANUP_AT",)
    for name in required:
        if not os.environ.get(name):
            pytest.fail(f"Live catalog requires {name} and protected run-owned fixtures")
    from playwright.sync_api import sync_playwright

    from tests.e2e.chat.helpers import fetch_test_credentials, get_cognito_tokens
    from tests.e2e.new_ui.product_fixture_provider import build_provider

    with sync_playwright() as playwright:
        context = playwright.request.new_context()
        try:
            provider = build_provider(os.environ, os.environ["E2E_CLOUDFRONT_URL"].rstrip("/"), context,
                                      get_cognito_tokens(fetch_test_credentials()),
                                      recovery_run_id=os.environ["PRODUCT_RECOVERY_RUN_ID"])
            yield provider
        finally:
            context.dispose()


@pytest.mark.live_only
def live_catalog_membership(catalog_live_provider):
    """Check served API membership against pre-write Product and owned relationship receipts."""
    assert verify_live_membership(catalog_live_provider, catalog_live_provider.base_url,
                                  os.environ["PRODUCT_CATALOG_FIXTURE_PATH"], os.environ["PRODUCT_EXPECTED_REVISION"])


@pytest.mark.live_only
def live_catalog_history_access(catalog_live_provider):
    """Compare dated membership and Contribution reads under three authenticated actors."""
    sessions = {"editor": catalog_live_provider.owner,
                "authorized": {"access_token": os.environ["PRODUCT_CATALOG_AUTHORIZED_TOKEN"]},
                "revoked": {"access_token": os.environ["PRODUCT_CATALOG_REVOKED_TOKEN"]}}
    assert verify_live_history(catalog_live_provider, sessions, os.environ["PRODUCT_EXPECTED_REVISION"],
                               getattr(catalog_live_provider, "native_work_reader", None))


@pytest.mark.live_only
def live_catalog_recovery(catalog_live_provider):
    """Retry one owned cleanup write, then independently verify history and peer preservation."""
    intent = catalog_live_provider.state.document.get("catalog_recovery", {})
    assert run_live_recovery(catalog_live_provider, getattr(catalog_live_provider, "catalog_release_reader", None),
                             os.environ["PRODUCT_CATALOG_CLEANUP_AT"], intent.get("peer"),
                             os.environ["PRODUCT_EXPECTED_REVISION"])


@pytest.mark.asyncio
async def test_overlapping_membership_matches_receipts_and_preserves_product(test_client, db_session, catalog_actor):
    products = [await create_product(test_client, name, f"v12_catalog_product_{index}")
                for index, name in enumerate(("Checkout", "Accounts"))]
    portfolios = [await create_portfolio(test_client, catalog_actor, name, f"v12_catalog_portfolio_{index}")
                  for index, name in enumerate(("Commerce", "Shared services"))]
    original_profiles = {}
    for name, product in zip(("Checkout", "Accounts"), products):
        path = f"/v1/products/{product['product_id']}/projects/{product['project_id']}"
        response = await test_client.get(path)
        assert response.status_code == 200, response.text
        original_profiles[path] = response.json()
        assert response.json()["product"]["name"] == name
        assert response.json()["product"]["purpose"] == "Independent example"
        assert response.json()["product"]["intended_users"] == ["Maintainers"]

    expected = {}
    for portfolio, product, index in (
        (portfolios[0], products[0], 0),
        (portfolios[0], products[1], 1),
        (portfolios[1], products[1], 2),
    ):
        receipt = await link_product(test_client, catalog_actor, portfolio, product, f"v12_membership_{index}")
        expected.setdefault(portfolio["id"], []).append((receipt, product))

    for portfolio in portfolios:
        response = await test_client.get(f"{CATALOG}/{portfolio['id']}/memberships",
                                         params={"effective_at": "2026-10-20T00:00:00Z"})
        assert response.status_code == 200, response.text
        rows = response.json()["items"]
        assert len(rows) == len(expected[portfolio["id"]])
        assert {
            (row["relationship_id"], row["relationship_revision"],
             row["portfolio_ref"]["record_id"], row["product_ref"]["record_id"],
             row["product_ref"]["project_id"], row["product_ref"]["record_revision"])
            for row in rows
        } == {
            (receipt["id"], receipt["revision"], portfolio["id"],
             product["product_id"], product["project_id"], "1")
            for receipt, product in expected[portfolio["id"]]
        }
    assert len({receipt["id"] for memberships in expected.values() for receipt, _ in memberships}) == 3
    for path, original in original_profiles.items():
        response = await test_client.get(path)
        assert response.status_code == 200, response.text
        assert response.json() == original


def test_served_catalog_fixture_rejects_other_run_stale_source_and_public_files(tmp_path):
    binding = {"run_id": "example-run", "source_revision": "a" * 40,
               "tenant_id": "example-tenant", "actor_id": "example-actor"}
    fixture = {**binding, "owned_fixture": True, "product_id": "example-product",
               "project_id": "example-project", "product_name": "Example Product",
               "portfolios": [
                   {"id": "commerce", "name": "Commerce", "revision": "1", "relationship_id": "first",
                    "relationship_revision": "1", "valid_from": "2026-10-01T00:00:00Z"},
                   {"id": "shared", "name": "Shared services", "revision": "1", "relationship_id": "second",
                    "relationship_revision": "1", "valid_from": "2026-10-01T00:00:00Z"},
               ]}
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(fixture))
    path.chmod(0o600)
    assert load_membership_fixture(path, binding, binding["source_revision"]) == fixture
    for changed in ({"run_id": "other-run"}, {"tenant_id": "other-tenant"},
                    {"source_revision": "b" * 40}, {"owned_fixture": False}):
        with pytest.raises(ValueError):
            validate_membership_fixture({**fixture, **changed}, binding, binding["source_revision"])
    for changed in ({"id": "commerce"}, {"relationship_id": "first"}, {"relationship_revision": None}):
        bad = deepcopy(fixture)
        bad["portfolios"][1].update(changed)
        with pytest.raises(ValueError):
            validate_membership_fixture(bad, binding, binding["source_revision"])
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private file"):
        load_membership_fixture(path, binding, binding["source_revision"])
    alias = tmp_path / "linked.json"
    alias.symlink_to(path)
    with pytest.raises(ValueError, match="absolute file"):
        load_membership_fixture(alias, binding, binding["source_revision"])


@pytest.mark.asyncio
async def test_dated_membership_and_cross_product_contribution_history(test_client, db_session, catalog_actor, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from src.catalog import initiative_routes, routes
    from src.catalog.models import CatalogChange, CatalogReceipt, ContributionVersion, MembershipVersion
    from src.products.models import Product, ProductProjectBinding, Project
    from tests.e2e.product_onboarding.test_i18 import record_ref

    actor = catalog_actor
    products = [await create_product(test_client, name, f"v12_history_product_{index}")
                for index, name in enumerate(("Checkout", "Accounts"))]
    routine = await create_product(test_client, "Routine Product", "v12_history_routine_product")
    routine_path = f"/v1/products/{routine['product_id']}/projects/{routine['project_id']}"
    routine_profile = await test_client.get(routine_path)
    assert routine_profile.status_code == 200 and routine_profile.json()["product"]["name"] == "Routine Product"
    unlinked = await test_client.get(f"/v1/catalog/projects/{routine['project_id']}/contributions")
    assert unlinked.status_code == 200 and unlinked.json() == {"items": []}

    project_ids = {product["project_id"] for product in products}
    for project_id in project_ids:
        project = await db_session.get(Project, (actor.org_id, project_id))
        project.configuration = {"basic": {"trackers": [record_ref(actor, project_id, "adp.profile",
                                                                   "tracker", "example.native-tracker")]}}
    await db_session.commit()

    async def local_native_work(caller, reference):
        return caller.user_id == actor.user_id and caller.org_id == actor.org_id and reference.project_id in project_ids

    monkeypatch.setattr(initiative_routes, "native_work_verified", local_native_work)
    now = datetime.now(UTC)
    valid_from = now - timedelta(days=5)
    switch = now - timedelta(days=3)
    first_recorded = now - timedelta(days=2)
    revised_recorded = now - timedelta(days=1)
    decision_before_revision = first_recorded + timedelta(hours=1)

    def wire(instant):
        return instant.isoformat().replace("+00:00", "Z")

    initiative = await test_client.post("/v1/catalog/initiatives",
                                       json={"name": "Shared outcome", "status": "planned"},
                                       headers={"Idempotency-Key": "v12_history_initiative"})
    assert initiative.status_code == 201, initiative.text
    first_product = products[0]
    portfolio = await create_portfolio(test_client, actor, "Commerce", "v12_history_portfolio")
    first_ref = record_ref(actor, first_product["project_id"], "adp.profile", "product", first_product["product_id"])
    monkeypatch.setattr(routes, "utcnow", lambda: first_recorded)
    membership = await test_client.post(f"{CATALOG}/{portfolio['id']}/memberships",
        json={"portfolio_ref": record_ref(actor, first_product["project_id"], "adp.catalog", "portfolio", portfolio["id"]),
              "product_ref": first_ref, "source_refs": [first_ref], "valid_from": wire(valid_from)},
        headers={"Idempotency-Key": "v12_history_membership"})
    assert membership.status_code == 201, membership.text
    monkeypatch.setattr(routes, "utcnow", lambda: revised_recorded)
    closed = await test_client.patch(f"{CATALOG}/{portfolio['id']}/memberships/{membership.json()['id']}",
        json={"valid_to": wire(switch)},
        headers={"Idempotency-Key": "v12_history_close", "If-Match": membership.json()["revision"]})
    assert closed.status_code == 200 and closed.json()["revision"] == "2", closed.text

    contributions = {}
    for index, product in enumerate(products):
        project_id = product["project_id"]
        original = record_ref(actor, project_id, "example.native-tracker", "story", f"story-{index}")
        payload = {"initiative_ref": record_ref(actor, project_id, "adp.catalog", "initiative", initiative.json()["id"]),
                   "product_ref": record_ref(actor, project_id, "adp.profile", "product", product["product_id"]),
                   "objective": f"Scope {index} outcome", "owner_ref": {"kind": "user", "id": actor.user_id},
                   "valid_from": wire(valid_from), "work_links": [{"work_ref": original, "valid_from": wire(valid_from)}]}
        monkeypatch.setattr(initiative_routes, "utcnow", lambda: first_recorded)
        response = await test_client.post(f"/v1/catalog/projects/{project_id}/contributions", json=payload,
                                          headers={"Idempotency-Key": f"v12_history_contribution_{index}"})
        assert response.status_code == 201, response.text
        contributions[project_id] = (response.json(), payload)

    first_project = first_product["project_id"]
    first_receipt, first_payload = contributions[first_project]
    revised = {**first_payload, "objective": "Revised scope 0 outcome", "work_links": [
        {"work_ref": first_payload["work_links"][0]["work_ref"], "valid_from": wire(valid_from), "valid_to": wire(switch)},
        {"work_ref": record_ref(actor, first_project, "example.native-tracker", "release", "first-release"),
         "valid_from": wire(switch)},
    ]}
    monkeypatch.setattr(initiative_routes, "utcnow", lambda: revised_recorded)
    changed = await test_client.patch(f"/v1/catalog/projects/{first_project}/contributions/{first_receipt['id']}",
        json=revised, headers={"Idempotency-Key": "v12_history_revision", "If-Match": first_receipt["revision"]})
    assert changed.status_code == 200 and changed.json()["revision"] == "2", changed.text

    prior_membership = await test_client.get(f"{CATALOG}/{portfolio['id']}/memberships",
        params={"effective_at": wire(valid_from + timedelta(hours=1)), "decision_at": wire(decision_before_revision)})
    ended_membership = await test_client.get(f"{CATALOG}/{portfolio['id']}/memberships",
        params={"effective_at": wire(switch + timedelta(hours=1))})
    assert prior_membership.status_code == 200 and prior_membership.headers["X-Catalog-View"] == "historical"
    assert [(row["relationship_id"], row["relationship_revision"], row["valid_from"], row["product_ref"]["record_id"])
            for row in prior_membership.json()["items"]] == [
                (membership.json()["id"], "1", wire(valid_from), first_product["product_id"])]
    assert ended_membership.status_code == 200 and ended_membership.json()["items"] == []
    for product in products:
        project_id = product["project_id"]
        receipt, payload = contributions[project_id]
        path = f"/v1/catalog/projects/{project_id}/contributions"
        before = await test_client.get(path, params={"effective_at": wire(valid_from + timedelta(hours=1)),
                                                     "decision_at": wire(decision_before_revision)})
        after = await test_client.get(path, params={"effective_at": wire(switch + timedelta(hours=1))})
        assert before.status_code == after.status_code == 200
        assert before.headers["X-Catalog-View"] == after.headers["X-Catalog-View"] == "historical"
        assert len(before.json()["items"]) == len(after.json()["items"]) == 1
        original, current = before.json()["items"][0], after.json()["items"][0]
        assert (original["id"], original["revision"], original["data"]["objective"]) == (
            receipt["id"], "1", payload["objective"])
        assert [(link["work_ref"]["record_id"], link["valid_from"]) for link in original["data"]["work_links"]] == [
            (payload["work_links"][0]["work_ref"]["record_id"], wire(valid_from))]
        assert (current["id"], current["revision"], current["data"]["product_ref"]["record_id"]) == (
            receipt["id"], "2" if product == first_product else "1", product["product_id"])
        assert [link["work_ref"]["record_id"] for link in current["data"]["work_links"]] == (
            ["first-release"] if product == first_product else [payload["work_links"][0]["work_ref"]["record_id"]])
    assert len((await db_session.scalars(select(ContributionVersion))).all()) == 3
    assert len((await db_session.scalars(select(MembershipVersion))).all()) == 2
    membership_versions = sorted((await db_session.scalars(select(MembershipVersion))).all(),
                                 key=lambda row: row.relationship_revision)
    assert [row.relationship_revision for row in membership_versions] == [1, 2]
    assert [row.recorded_at.replace(tzinfo=UTC) for row in membership_versions] == [first_recorded, revised_recorded]
    assert membership_versions[0].valid_from.replace(tzinfo=UTC) == valid_from
    assert membership_versions[1].valid_to.replace(tzinfo=UTC) == switch
    contribution_versions = sorted((await db_session.scalars(select(ContributionVersion))).all(),
                                   key=lambda row: (row.project_id, row.revision))
    first_versions = [row for row in contribution_versions if row.project_id == first_project]
    assert [row.revision for row in first_versions] == [1, 2]
    assert [row.recorded_at.replace(tzinfo=UTC) for row in first_versions] == [first_recorded, revised_recorded]
    assert datetime.fromisoformat(first_versions[1].work_links[0]["valid_to"].replace("Z", "+00:00")) == switch
    assert datetime.fromisoformat(first_versions[1].work_links[1]["valid_from"].replace("Z", "+00:00")) == switch


    native_identity = {
        (product["product_id"], product["project_id"]): (
            (await db_session.get(Product, (actor.org_id, product["product_id"]))).revision,
            (await db_session.get(Project, (actor.org_id, product["project_id"]))).configuration,
        )
        for product in products
    }
    bindings = {(row.product_id, row.project_id, row.id) for row in
                (await db_session.scalars(select(ProductProjectBinding))).all()}
    async def catalog_counts():
        counts = []
        for model in (MembershipVersion, ContributionVersion, CatalogChange, CatalogReceipt):
            counts.append(len((await db_session.scalars(select(model))).all()))
        return tuple(counts)

    counts = await catalog_counts()
    membership_path = f"{CATALOG}/{portfolio['id']}/memberships/{membership.json()['id']}"
    contribution_path = f"/v1/catalog/projects/{first_project}/contributions/{first_receipt['id']}"
    membership_headers = {"Idempotency-Key": "v12_history_close", "If-Match": membership.json()["revision"]}
    contribution_headers = {"Idempotency-Key": "v12_history_revision", "If-Match": first_receipt["revision"]}
    replayed_membership = await test_client.patch(membership_path, json={"valid_to": wire(switch)},
                                                  headers=membership_headers)
    replayed_contribution = await test_client.patch(contribution_path, json=revised,
                                                    headers=contribution_headers)
    assert replayed_membership.status_code == 200 and replayed_membership.json() == closed.json()
    assert replayed_contribution.status_code == 200 and replayed_contribution.json() == changed.json()
    conflicting = await test_client.patch(contribution_path, json={**revised, "objective": "Other outcome"},
                                          headers=contribution_headers)
    stale = await test_client.patch(contribution_path, json=revised,
                                    headers={**contribution_headers, "Idempotency-Key": "v12_history_stale"})
    assert conflicting.status_code == 409 and conflicting.json()["code"] == "conflict"
    assert stale.status_code == 412 and stale.json()["code"] == "stale_revision"
    assert await catalog_counts() == counts
    assert {(row.product_id, row.project_id, row.id) for row in
            (await db_session.scalars(select(ProductProjectBinding))).all()} == bindings
    for product in products:
        assert ((await db_session.get(Product, (actor.org_id, product["product_id"]))).revision,
                (await db_session.get(Project, (actor.org_id, product["project_id"]))).configuration) == native_identity[
                    (product["product_id"], product["project_id"])]
    current = await test_client.get(f"/v1/catalog/projects/{first_project}/contributions",
                                    params={"effective_at": wire(switch + timedelta(hours=1))})
    assert current.status_code == 200 and len(current.json()["items"]) == 1
    assert [link["work_ref"]["record_id"] for link in current.json()["items"][0]["data"]["work_links"]] == [
        "first-release"]
