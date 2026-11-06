"""Served two-scope readback; live execution needs protected, run-owned fixtures."""

from __future__ import annotations

import os
from urllib.parse import quote

import pytest

from tests.e2e.chat.helpers import inject_tokens_and_navigate
from tests.e2e.new_ui.product_navigation import open_product_section
from tests.e2e.new_ui.product_release import check_served_assets


def assert_scope_fixture_inputs(fixture):
    scopes = fixture["scopes"]
    assert len(scopes) == 2 and scopes[0]["project_id"] != scopes[1]["project_id"]
    references = []
    plan_dates = []
    for scope in scopes:
        configuration = scope["configuration"]
        repositories = configuration["basic"]["repositories"]
        assert len(repositories) == 1
        reference = repositories[0]
        assert reference["tenant_id"] == fixture["tenant_id"]
        assert reference["project_id"] == scope["project_id"]
        assert reference["source_connection_id"] and reference["record_id"] and reference["record_revision"]
        references.append((reference["source_connection_id"], reference["record_id"]))
        plan_dates.append(configuration["financials"]["release_target_date"])
        assert isinstance(scope["progress"], dict)
    assert len(set(references)) == len(set(plan_dates)) == 2
    assert scopes[0]["progress"] != scopes[1]["progress"]
    return scopes


def assert_scope_profile(profile, fixture, scope):
    project_id = scope["project_id"]
    assert profile["product_id"] == fixture["product_id"]
    assert profile["project_id"] == project_id
    assert profile["tenant_id"] == fixture["tenant_id"]
    assert profile["project"]["id"] == project_id
    assert profile["project"]["name"] == scope["name"]
    assert profile["product"]["owner_ref"] == {"kind": "user", "id": fixture["actor_id"]}
    assert profile["configuration"] == scope["configuration"]
    assert profile["progress"] == scope["progress"]
    return profile["product"]


def assert_scope_denial_actors(fixture, owner, outsider):
    assert owner["actor_id"] == fixture["actor_id"] and owner["tenant_id"] == fixture["tenant_id"]
    assert outsider["actor_id"] != owner["actor_id"] and outsider["tenant_id"] != fixture["tenant_id"]
    assert owner["access_token"] and outsider["access_token"] != owner["access_token"]


@pytest.mark.preview_on
def test_scope_isolation_api(base_url, deployed_features, product_scope_fixture, product_provider, product_scope_case):
    assert deployed_features.get("product_profile") is True
    fixture = product_scope_fixture
    scopes = assert_scope_fixture_inputs(fixture)
    owner = product_provider.owner
    assert owner["actor_id"] == fixture["actor_id"] and owner["tenant_id"] == fixture["tenant_id"]
    headers = {"Authorization": "Bearer " + owner["access_token"]}
    listing = product_provider.request.get(base_url + "/api/v1/products", headers=headers, timeout=30_000)
    assert listing.status == 200
    bound = [item for item in listing.json()["items"] if item["id"] == fixture["product_id"]]
    assert len(bound) == 1
    assert set(bound[0]["project_ids"]) == {scope["project_id"] for scope in scopes}
    identities = []
    for scope in scopes:
        route = f"/products/{quote(fixture['product_id'], safe='')}/projects/{quote(scope['project_id'], safe='')}"
        response = product_provider.request.get(base_url + "/api/v1" + route, headers=headers, timeout=30_000)
        assert response.status == 200
        identities.append(assert_scope_profile(response.json(), fixture, scope))
    assert identities[0] == identities[1]


@pytest.mark.preview_on
def test_scope_isolation(page, base_url, deployed_features, product_scope_fixture, product_provider, product_scope_case):
    from playwright.sync_api import expect

    assert deployed_features.get("product_profile") is True
    fixture = product_scope_fixture
    check_served_assets(product_provider.request, base_url, product_provider.frontend,
                        os.environ["PRODUCT_EXPECTED_REVISION"])
    owner = product_provider.owner
    assert owner["actor_id"] == fixture["actor_id"] and owner["tenant_id"] == fixture["tenant_id"]
    first, second = assert_scope_fixture_inputs(fixture)
    product_id = quote(fixture["product_id"], safe="")
    headers = {"Authorization": "Bearer " + owner["access_token"]}
    listing = product_provider.request.get(base_url + "/api/v1/products", headers=headers, timeout=30_000)
    assert listing.status == 200, f"Independent Product bindings read failed: HTTP {listing.status}"
    page_of_products = listing.json()
    bound = [item for item in page_of_products["items"] if item["id"] == fixture["product_id"]]
    assert len(bound) == 1
    assert set(bound[0]["project_ids"]) == {scope["project_id"] for scope in fixture["scopes"]}
    observed_products = []
    for scope in (first, second, first):
        project_id = quote(scope["project_id"], safe="")
        route = f"/products/{product_id}/projects/{project_id}"
        if not observed_products:
            inject_tokens_and_navigate(page, owner, path=route)
        else:
            page.get_by_label("Delivery scope").select_option(scope["project_id"])
            page.wait_for_url(base_url + route, timeout=30_000)
        expect(page.get_by_role("heading", name=fixture["product_name"])).to_be_visible()
        open_product_section(page, "Environments & delivery")
        expect(page.get_by_label("Project name")).to_have_value(scope["name"])
        expect(page.get_by_label("Delivery scope")).to_have_value(scope["project_id"])
        response = product_provider.request.get(base_url + "/api/v1" + route,
                                                headers=headers, timeout=30_000)
        assert response.status == 200, f"Independent Project read failed: HTTP {response.status}"
        product = assert_scope_profile(response.json(), fixture, scope)
        assert product["name"] == fixture["product_name"]
        observed_products.append(product)
    assert observed_products[0] == observed_products[1] == observed_products[2]


@pytest.mark.preview_on
def test_scope_denials_api(base_url, deployed_features, product_scope_fixture, product_scope_editor, product_provider, product_scope_case):
    from tests.e2e.new_ui.test_product_profile import (
        assert_denied_read,
        assert_revoked_read,
    )

    assert deployed_features.get("product_profile") is True
    fixture = product_scope_fixture
    scopes = assert_scope_fixture_inputs(fixture)
    owner, outsider = product_provider.owner, product_provider.nonmember
    editor = product_scope_editor
    assert_scope_denial_actors(fixture, owner, outsider)
    paths = [
        "/products/" + quote(fixture["product_id"], safe="") + "/projects/" + quote(scope["project_id"], safe="")
        for scope in scopes
    ]
    editor_headers = {"Authorization": "Bearer " + editor["access_token"]}
    first = paths[0]
    original = product_provider.request.get(base_url + "/api/v1" + first,
                                             headers=editor_headers, timeout=30_000)
    assert original.status == 200
    updated = product_provider.request.patch(
        base_url + "/api/v1" + first,
        headers={**editor_headers, "If-Profile-Revision": str(original.json()["revision"]),
                 "Idempotency-Key": "scope-editor-" + fixture["run_id"]},
        data={"purpose": "Verify ordinary editor scope access"}, timeout=30_000,
    )
    assert updated.status == 200 and updated.json()["product"]["purpose"] == "Verify ordinary editor scope access"
    assert updated.json()["product"]["owner_ref"] == {"kind": "user", "id": fixture["actor_id"]}
    denied_admin = product_provider.request.patch(
        base_url + "/api/v1" + first,
        headers={**editor_headers, "If-Profile-Revision": str(updated.json()["revision"]),
                 "Idempotency-Key": "scope-admin-denied-" + fixture["run_id"]},
        data={"team_id": None}, timeout=30_000,
    )
    assert denied_admin.status == 403
    denied_bind = product_provider.request.post(
        base_url + "/api/v1/products/" + quote(fixture["product_id"], safe="") + "/projects",
        headers={**editor_headers, "If-Profile-Revision": str(updated.json()["revision"]),
                 "Idempotency-Key": "scope-bind-denied-" + fixture["run_id"]},
        data={"mode": "create", "name": "Unauthorized Project"}, timeout=30_000,
    )
    assert denied_bind.status == 404
    before = []
    for scope, path in zip(scopes, paths):
        owned = product_provider.request.get(
            base_url + "/api/v1" + path,
            headers={"Authorization": "Bearer " + owner["access_token"]}, timeout=30_000,
        )
        assert owned.status == 200
        assert_scope_profile(owned.json(), fixture, scope)
        before.append(owned.json())
        denied = product_provider.request.get(
            base_url + "/api/v1" + path,
            headers={"Authorization": "Bearer " + outsider["access_token"]}, timeout=30_000,
        )
        assert_denied_read(denied, (fixture["product_name"], scope["name"]))
    try:
        product_provider.editor_membership.revoke(product_provider.state)
        for scope, path in zip(scopes, paths):
            revoked = product_provider.request.get(
                base_url + "/api/v1" + path,
                headers=editor_headers, timeout=30_000,
            )
            assert_revoked_read(revoked, (fixture["product_name"], scope["name"]))
            for suffix in ("changes", "project-bindings"):
                history = product_provider.request.get(
                    base_url + "/api/v1" + path + "/" + suffix,
                    headers=editor_headers, timeout=30_000,
                )
                assert history.status in (403, 404)
                assert fixture["product_name"] not in history.text()
        for route in ("products", "projects"):
            listing = product_provider.request.get(base_url + "/api/v1/" + route,
                                                   headers=editor_headers, timeout=30_000)
            assert listing.status == 403
    finally:
        product_provider.editor_membership.restore(product_provider.state)
    for path, original in zip(paths, before):
        restored = product_provider.request.get(
            base_url + "/api/v1" + path,
            headers={"Authorization": "Bearer " + owner["access_token"]}, timeout=30_000,
        )
        assert restored.status == 200 and restored.json() == original


@pytest.mark.preview_on
def test_scope_denials(page, base_url, deployed_features, product_scope_fixture, product_scope_editor, product_provider, product_scope_case):
    from playwright.sync_api import expect

    from tests.e2e.new_ui.test_product_profile import (
        assert_denied_read,
        assert_revoked_read,
    )

    assert deployed_features.get("product_profile") is True
    fixture = product_scope_fixture
    scopes = assert_scope_fixture_inputs(fixture)
    owner = product_provider.owner
    outsider = product_provider.nonmember
    editor = product_scope_editor
    assert_scope_denial_actors(fixture, owner, outsider)
    check_served_assets(product_provider.request, base_url, product_provider.frontend,
                        os.environ["PRODUCT_EXPECTED_REVISION"])
    paths = [
        "/products/" + quote(fixture["product_id"], safe="") + "/projects/" + quote(scope["project_id"], safe="")
        for scope in scopes
    ]
    original = []
    for scope, path in zip(scopes, paths):
        owned = product_provider.request.get(
            base_url + "/api/v1" + path,
            headers={"Authorization": "Bearer " + owner["access_token"]}, timeout=30_000,
        )
        assert owned.status == 200
        assert_scope_profile(owned.json(), fixture, scope)
        original.append(owned.json())
        foreign = product_provider.request.get(
            base_url + "/api/v1" + path,
            headers={"Authorization": "Bearer " + outsider["access_token"]}, timeout=30_000,
        )
        assert_denied_read(foreign, (fixture["product_name"], scope["name"]))
    inject_tokens_and_navigate(page, outsider, path=paths[0])
    expect(page.get_by_role("alert")).to_contain_text("Product unavailable or you do not have access")
    try:
        product_provider.editor_membership.revoke(product_provider.state)
        for scope, path in zip(scopes, paths):
            revoked = product_provider.request.get(
                base_url + "/api/v1" + path,
                headers={"Authorization": "Bearer " + editor["access_token"]}, timeout=30_000,
            )
            assert_revoked_read(revoked, (fixture["product_name"], scope["name"]))
        inject_tokens_and_navigate(page, editor, path=paths[0])
        expect(page.get_by_role("alert")).to_contain_text("Product unavailable or you do not have access")
    finally:
        product_provider.editor_membership.restore(product_provider.state)
    for path, before in zip(paths, original):
        restored = product_provider.request.get(
            base_url + "/api/v1" + path,
            headers={"Authorization": "Bearer " + owner["access_token"]}, timeout=30_000,
        )
        assert restored.status == 200 and restored.json() == before
