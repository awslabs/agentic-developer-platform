"""Served Product-to-catalog readback against independently observed owned records."""

import os
from urllib.parse import quote, urlsplit

import pytest

from tests.e2e.chat.helpers import inject_tokens_and_navigate
from tests.e2e.new_ui.product_navigation import open_product_section
from tests.e2e.new_ui.product_release import check_served_assets
from tests.e2e.product_onboarding.product_catalog_fixture import load_membership_fixture


def membership_rows(request, base_url, headers, portfolio_id):
    rows = []
    cursor = None
    for _ in range(10):
        path = f"/api/v1/catalog/portfolios/{quote(portfolio_id, safe='')}/memberships"
        if cursor:
            path += f"?cursor={quote(cursor, safe='')}"
        response = request.get(base_url + path, headers=headers, timeout=30_000)
        assert response.status == 200, f"Independent catalog membership read failed: HTTP {response.status}"
        body = response.json()
        rows.extend(body["items"])
        cursor = body.get("next_cursor")
        if not cursor:
            return rows
    raise AssertionError("Catalog membership pagination exceeded its bounded fixture")


def matching_rows(rows, fixture, portfolio):
    return [row for row in rows if row["portfolio_ref"]["record_id"] == portfolio["id"]
            and row["product_ref"]["record_id"] == fixture["product_id"]
            and row["product_ref"]["project_id"] == fixture["project_id"]]


@pytest.mark.preview_on
def test_catalog_membership(page, base_url, deployed_features, product_catalog_provider):
    from playwright.sync_api import expect

    assert deployed_features.get("product_profile") is True
    fixture = load_membership_fixture(os.environ["PRODUCT_CATALOG_FIXTURE_PATH"], product_catalog_provider.binding,
                                      os.environ["PRODUCT_EXPECTED_REVISION"])
    check_served_assets(product_catalog_provider.request, base_url, product_catalog_provider.frontend,
                        fixture["source_revision"])
    owner = product_catalog_provider.owner
    headers = {"Authorization": "Bearer " + owner["access_token"]}
    product_path = f"/products/{quote(fixture['product_id'], safe='')}/projects/{quote(fixture['project_id'], safe='')}"
    profile = product_catalog_provider.request.get(base_url + "/api/v1" + product_path,
                                           headers=headers, timeout=30_000)
    assert profile.status == 200, f"Independent Product read failed: HTTP {profile.status}"
    if (profile.json()["product_id"], profile.json()["project_id"], profile.json()["product"]["name"]) != (
        fixture["product_id"], fixture["project_id"], fixture["product_name"]):
        raise AssertionError("Protected Product identity changed")
    expected = {}
    for portfolio in fixture["portfolios"]:
        response = product_catalog_provider.request.get(
            base_url + f"/api/v1/catalog/portfolios/{quote(portfolio['id'], safe='')}",
            headers=headers, timeout=30_000,
        )
        assert response.status == 200, f"Independent Portfolio read failed: HTTP {response.status}"
        data = response.json()
        if (data["id"], data["revision"], data["data"]["name"]) != (
            portfolio["id"], portfolio["revision"], portfolio["name"]):
            raise AssertionError("Portfolio receipt differs from current catalog")
        rows = matching_rows(membership_rows(product_catalog_provider.request, base_url, headers, portfolio["id"]), fixture, portfolio)
        assert len(rows) == 1, "Expected exactly one current membership for each Portfolio"
        row = rows[0]
        if (row["relationship_id"], row["relationship_revision"], row["valid_from"]) != (
            portfolio["relationship_id"], portfolio["relationship_revision"], portfolio["valid_from"]):
            raise AssertionError("Membership differs from write receipt")
        if (row["product_ref"]["tenant_id"], row["product_ref"]["project_id"]) != (
            fixture["tenant_id"], fixture["project_id"]):
            raise AssertionError("Membership refers to another scope")
        expected[portfolio["id"]] = row

    browser_reads = {portfolio["id"]: [] for portfolio in fixture["portfolios"]}

    def capture(response):
        if response.status != 200:
            return
        path = urlsplit(response.url).path
        for portfolio in fixture["portfolios"]:
            if path == f"/api/v1/catalog/portfolios/{quote(portfolio['id'], safe='')}/memberships":
                browser_reads[portfolio["id"]].extend(response.json()["items"])

    page.on("response", capture)
    inject_tokens_and_navigate(page, owner, path=product_path)
    expect(page.get_by_role("heading", name=fixture["product_name"])).to_be_visible()
    open_product_section(page)
    page.get_by_text("Organization and Portfolios").click()
    expect(page.get_by_role("region", name="Portfolio organization")).to_be_visible()
    for portfolio in fixture["portfolios"]:
        expect(page.get_by_text(f"{portfolio['name']}: {portfolio['valid_from'][:10]} to ongoing")).to_be_visible()
        observed = matching_rows(browser_reads[portfolio["id"]], fixture, portfolio)
        if not observed or any(row != expected[portfolio["id"]] for row in observed):
            raise AssertionError("Served browser membership response differs from independent catalog readback")
