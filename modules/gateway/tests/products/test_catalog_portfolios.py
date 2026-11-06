"""Local gateway readback for optional, overlapping Portfolio membership."""

import copy
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from src.auth.dependencies import get_current_user
from src.catalog.models import CatalogChange, CatalogReceipt, MembershipHead, MembershipVersion, PortfolioHead, PortfolioVersion
from src.products.models import Product, ProductProjectBinding, Project
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from tests.products.membership import add_team_member

PROFILE_REQUEST = {
    "name": "Example Product",
    "purpose": "A small product",
    "intended_users": ["Maintainers"],
    "starting_point": "idea",
    "project": {"mode": "create"},
}
CATALOG = "/v1/catalog/portfolios"


@pytest.fixture
async def catalog_editor(db_session, app, valid_token_context, monkeypatch):
    actor = valid_token_context
    db_session.add(Organization(id=actor.org_id, name="Example workspace"))
    db_session.add(User(id=actor.user_id, org_id=actor.org_id, team_id=actor.team_id, email="editor@example.test", user_kind="human"))
    await db_session.flush()
    db_session.add(TenantMembership(user_id=actor.user_id, tenant_id=actor.org_id, role="org_admin", is_active=True))
    await add_team_member(db_session, actor)
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: actor
    monkeypatch.setenv("BG_CATALOG_ENABLED", "true")
    return actor


async def create_product(client, key):
    response = await client.post("/v1/products", json=PROFILE_REQUEST, headers={"Idempotency-Key": key})
    assert response.status_code == 201, response.text
    return response.json()


async def create_portfolio(client, name, actor):
    data = {"name": name, "owner_ref": {"kind": "user", "id": actor.user_id}, "lifecycle_status": "active"}
    response = await client.post(CATALOG, json=data, headers={"Idempotency-Key": name})
    assert response.status_code == 201, response.text
    assert (await client.post(CATALOG, json=data, headers={"Idempotency-Key": name})).json() == response.json()
    return response.json()


def reference(actor, project_id, source, kind, record_id, revision="1"):
    return {
        "tenant_id": actor.org_id,
        "project_id": project_id,
        "source_connection_id": source,
        "record_type": kind,
        "record_id": record_id,
        "record_revision": revision,
    }


async def add_product(client, actor, portfolio, product, key):
    project_id = product["project_id"]
    payload = {
        "portfolio_ref": reference(actor, project_id, "adp.catalog", "portfolio", portfolio["id"]),
        "product_ref": reference(actor, project_id, "adp.profile", "product", product["product_id"]),
        "source_refs": [reference(actor, project_id, "adp.profile", "product", product["product_id"])],
        "valid_from": "2026-10-19T00:00:00Z",
    }
    response = await client.post(f"{CATALOG}/{portfolio['id']}/memberships", json=payload, headers={"Idempotency-Key": key})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("record_kind", ["portfolio", "membership"])
async def test_catalog_creation_with_foreign_keys(test_client, db_session, test_engine, catalog_editor, record_kind):
    product = await create_product(test_client, "foreign_keys_product")
    portfolio = await create_portfolio(test_client, "Foreign keys Portfolio", catalog_editor) if record_kind == "membership" else None
    async with test_engine.connect() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        foreign_keys = await connection.exec_driver_sql("PRAGMA foreign_keys")
        assert foreign_keys.scalar_one() == 1

    if portfolio is None:
        result = await create_portfolio(test_client, "Foreign keys Portfolio", catalog_editor)
        head = await db_session.get(PortfolioHead, (catalog_editor.org_id, "adp.catalog", result["id"]))
        version = await db_session.get(PortfolioVersion, (catalog_editor.org_id, "adp.catalog", result["id"], 1))
        assert head.revision == version.revision == 1
    else:
        result = await add_product(test_client, catalog_editor, portfolio, product, "foreign_keys_membership")
        head = await db_session.get(MembershipHead, (catalog_editor.org_id, result["id"]))
        version = await db_session.get(MembershipVersion, (catalog_editor.org_id, result["id"], 1))
        assert head.relationship_revision == version.relationship_revision == 1
        assert version.portfolio_id == portfolio["id"] and version.product_id == product["product_id"]

    change = await db_session.get(CatalogChange, (catalog_editor.org_id, result["change_id"]))
    assert change.record_id == result["id"]
    assert await db_session.scalar(select(CatalogReceipt).where(CatalogReceipt.outcome == result)) is not None


@pytest.mark.asyncio
async def test_portfolio_many_to_many(test_client, db_session, catalog_editor):
    checkout = await create_product(test_client, "checkout_product_key")
    accounts = await create_product(test_client, "accounts_product_key")
    commerce = await create_portfolio(test_client, "Commerce", catalog_editor)
    shared = await create_portfolio(test_client, "Shared services", catalog_editor)
    for portfolio, product, key in (
        (commerce, checkout, "commerce_checkout"),
        (commerce, accounts, "commerce_accounts"),
        (shared, accounts, "shared_accounts"),
    ):
        await add_product(test_client, catalog_editor, portfolio, product, key)

    page = await test_client.get(CATALOG)
    assert page.status_code == 200, page.text
    assert {item["id"] for item in page.json()["items"]} == {commerce["id"], shared["id"]}
    assert all(item["recorded_at"].endswith("Z") for item in page.json()["items"])
    for portfolio, expected in ((commerce, {checkout["product_id"], accounts["product_id"]}), (shared, {accounts["product_id"]})):
        result = await test_client.get(f"{CATALOG}/{portfolio['id']}/memberships")
        assert result.status_code == 200, result.text
        assert {item["product_ref"]["record_id"] for item in result.json()["items"]} == expected
        assert all(
            item["portfolio_ref"]["record_id"] == portfolio["id"]
            and item["source_connection_id"] == "adp.catalog"
            and item["valid_from"].endswith("Z")
            and item["recorded_at"].endswith("Z")
            for item in result.json()["items"]
        )
    first_page = await test_client.get(f"{CATALOG}/{commerce['id']}/memberships", params={"limit": 1})
    assert len(first_page.json()["items"]) == 1 and first_page.json()["next_cursor"]
    second_page = await test_client.get(f"{CATALOG}/{commerce['id']}/memberships", params={"limit": 1, "cursor": first_page.json()["next_cursor"]})
    assert second_page.status_code == 200, second_page.text
    assert len(second_page.json()["items"]) == 1 and "next_cursor" not in second_page.json()
    assert {first_page.json()["items"][0]["product_ref"]["record_id"], second_page.json()["items"][0]["product_ref"]["record_id"]} == {
        checkout["product_id"],
        accounts["product_id"],
    }

    versions = (await db_session.scalars(select(MembershipVersion))).all()
    assert len(versions) == 3
    assert {row.portfolio_id for row in versions} == {commerce["id"], shared["id"]}
    assert len((await db_session.scalars(select(PortfolioVersion))).all()) == 2
    assert len((await db_session.scalars(select(Product))).all()) == 2
    assert len((await db_session.scalars(select(ProductProjectBinding))).all()) == 2
    assert len((await db_session.scalars(select(CatalogReceipt))).all()) == 5
    assert len((await db_session.scalars(select(CatalogChange))).all()) == 5
    assert {
        change.record_id for change in (await db_session.scalars(select(CatalogChange).where(CatalogChange.record_type == "portfolio_product"))).all()
    } == {row.relationship_id for row in versions}


@pytest.mark.asyncio
async def test_portfolio_revision_and_replay(test_client, db_session, catalog_editor):
    portfolio = await create_portfolio(test_client, "Commerce", catalog_editor)
    update = {"name": "Commerce edited", "owner_ref": {"kind": "user", "id": catalog_editor.user_id}, "lifecycle_status": "retired"}
    headers = {"Idempotency-Key": "commerce_revision", "If-Match": "1"}
    response = await test_client.patch(f"{CATALOG}/{portfolio['id']}", json=update, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == "2"
    replay = await test_client.patch(f"{CATALOG}/{portfolio['id']}", json=update, headers=headers)
    assert replay.json() == response.json()
    current = await test_client.get(f"{CATALOG}/{portfolio['id']}")
    assert current.json()["data"]["lifecycle_status"] == "retired"
    assert len((await db_session.scalars(select(PortfolioVersion))).all()) == 2


@pytest.mark.asyncio
async def test_portfolio_history(test_client, db_session, catalog_editor, monkeypatch):
    import src.catalog.routes as catalog_routes

    observed_at = datetime(2026, 10, 20, 12, tzinfo=UTC)
    monkeypatch.setattr(catalog_routes, "utcnow", lambda: observed_at)
    product = await create_product(test_client, "history_product_key")
    portfolio = await create_portfolio(test_client, "History collection", catalog_editor)
    edge = await add_product(test_client, catalog_editor, portfolio, product, "history_member")
    path = f"{CATALOG}/{portfolio['id']}/memberships"
    original = (await test_client.get(path)).json()["items"][0]
    assert original["relationship_revision"] == "1" and "valid_to" not in original
    original_version = await db_session.scalar(select(MembershipVersion).where(MembershipVersion.relationship_id == edge["id"]))
    unchanged = {column.key: getattr(original_version, column.key) for column in MembershipVersion.__table__.columns}

    observed_at = datetime(2026, 10, 21, 12, tzinfo=UTC)
    first_close = await test_client.patch(
        f"{path}/{edge['id']}",
        json={"valid_to": "2026-11-10T00:00:00Z"},
        headers={"Idempotency-Key": "close_membership_1", "If-Match": "1"},
    )
    assert first_close.status_code == 200, first_close.text
    assert first_close.json()["revision"] == "2"
    assert (
        await test_client.patch(
            f"{path}/{edge['id']}",
            json={"valid_to": "2026-11-10T00:00:00Z"},
            headers={"Idempotency-Key": "close_membership_1", "If-Match": "1"},
        )
    ).json() == first_close.json()

    observed_at = datetime(2026, 10, 22, 12, tzinfo=UTC)
    corrected = await test_client.patch(
        f"{path}/{edge['id']}",
        json={"valid_to": "2026-10-21T12:00:00Z"},
        headers={"Idempotency-Key": "close_membership_2", "If-Match": "2"},
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["revision"] == "3"
    current = await test_client.get(path)
    assert current.status_code == 200 and current.json()["items"] == []
    assert current.headers["X-Catalog-View"] == "current"

    def history(effective_at, decision_at):
        return {"effective_at": effective_at, "decision_at": decision_at}

    at_creation = await test_client.get(path, params=history("2026-10-22T00:00:00Z", "2026-10-20T12:30:00Z"))
    assert at_creation.status_code == 200, at_creation.text
    assert at_creation.headers["X-Catalog-View"] == "historical"
    assert at_creation.json()["items"] == [original]
    after_first_close = await test_client.get(path, params=history("2026-10-22T00:00:00Z", "2026-10-21T12:30:00Z"))
    assert after_first_close.json()["items"][0]["relationship_revision"] == "2"
    assert datetime.fromisoformat(after_first_close.json()["items"][0]["valid_to"].replace("Z", "+00:00")) == datetime(2026, 11, 10, tzinfo=UTC)
    after_correction = await test_client.get(path, params=history("2026-10-22T00:00:00Z", "2026-10-22T12:00:00Z"))
    assert after_correction.json()["items"] == []
    earlier_effective = await test_client.get(path, params=history("2026-10-20T12:00:00Z", "2026-10-22T12:00:00Z"))
    assert earlier_effective.json()["items"][0]["supersedes_revision"] == "2"
    versions = (await db_session.scalars(select(MembershipVersion).order_by(MembershipVersion.relationship_revision))).all()
    assert len(versions) == 3
    refreshed = await db_session.scalar(
        select(MembershipVersion)
        .where(MembershipVersion.relationship_id == edge["id"], MembershipVersion.relationship_revision == 1)
        .execution_options(populate_existing=True)
    )
    assert {column.key: getattr(refreshed, column.key) for column in MembershipVersion.__table__.columns} == unchanged
    assert versions[0].valid_to is None and versions[0].source_refs == original["source_refs"]
    assert versions[1].supersedes_revision == 1 and versions[2].supersedes_revision == 2
    assert len((await db_session.scalars(select(CatalogReceipt))).all()) == 4
    changes = (
        await db_session.scalars(
            select(CatalogChange).where(CatalogChange.record_type == "portfolio_product").order_by(CatalogChange.relationship_revision)
        )
    ).all()
    assert [change.relationship_revision for change in changes] == [1, 2, 3]
    assert [change.valid_to for change in changes] == [row.valid_to for row in versions]
    assert {change.id for change in changes}.issubset(
        {receipt.outcome["change_id"] for receipt in (await db_session.scalars(select(CatalogReceipt))).all()}
    )
    product_reference = reference(catalog_editor, product["project_id"], "adp.profile", "product", product["product_id"])
    reopened = await test_client.post(
        path,
        json={
            "portfolio_ref": reference(catalog_editor, product["project_id"], "adp.catalog", "portfolio", portfolio["id"]),
            "product_ref": product_reference,
            "source_refs": [product_reference],
            "valid_from": "2026-10-22T12:00:00Z",
        },
        headers={"Idempotency-Key": "history_reopen_new_edge"},
    )
    assert reopened.status_code == 201, reopened.text
    assert reopened.json()["id"] != edge["id"]
    current_after_reopen = await test_client.get(path)
    assert [item["relationship_id"] for item in current_after_reopen.json()["items"]] == [reopened.json()["id"]]
    active_heads = (await db_session.scalars(select(MembershipHead))).all()
    assert [(head.relationship_id, head.relationship_revision) for head in active_heads] == [(reopened.json()["id"], 1)]
    unchanged_after_reopen = await db_session.scalar(
        select(MembershipVersion)
        .where(MembershipVersion.relationship_id == edge["id"], MembershipVersion.relationship_revision == 1)
        .execution_options(populate_existing=True)
    )
    assert {column.key: getattr(unchanged_after_reopen, column.key) for column in MembershipVersion.__table__.columns} == unchanged


@pytest.mark.asyncio
async def test_denied_membership_writes_leave_no_versions_or_receipts(test_client, db_session, catalog_editor):
    product = await create_product(test_client, "denied_product_key")
    portfolio = await create_portfolio(test_client, "Denials", catalog_editor)
    project_id = product["project_id"]
    path = f"{CATALOG}/{portfolio['id']}/memberships"
    payload = {
        "portfolio_ref": reference(catalog_editor, project_id, "adp.catalog", "portfolio", portfolio["id"]),
        "product_ref": reference(catalog_editor, project_id, "adp.profile", "product", product["product_id"]),
        "valid_from": "2026-10-19T00:00:00Z",
    }
    payload["source_refs"] = [payload["product_ref"]]
    baseline_receipts = len((await db_session.scalars(select(CatalogReceipt))).all())
    for index, mutation in enumerate(
        (
            {"product_ref": {**payload["product_ref"], "record_type": "portfolio"}},
            {"product_ref": {**payload["product_ref"], "record_id": "foreign-product"}},
            {"portfolio_ref": {**payload["portfolio_ref"], "tenant_id": "foreign-tenant"}},
            {"source_refs": [reference(catalog_editor, project_id, "foreign.source", "change", "made-up")]},
            {"valid_to": "2026-10-18T00:00:00Z"},
        )
    ):
        altered = {**copy.deepcopy(payload), **mutation}
        response = await test_client.post(path, json=altered, headers={"Idempotency-Key": f"denied_{index}"})
        assert response.status_code in {404, 422}, (mutation, response.text)
        assert set(response.json()) == {"code", "message"}
    assert (await db_session.scalars(select(MembershipVersion))).all() == []
    assert len((await db_session.scalars(select(CatalogReceipt))).all()) == baseline_receipts

    accepted = await test_client.post(path, json=payload, headers={"Idempotency-Key": "accepted_1"})
    assert accepted.status_code == 201, accepted.text
    duplicate = await test_client.post(path, json=payload, headers={"Idempotency-Key": "accepted_2"})
    assert duplicate.status_code == 409
    changed_retry = await test_client.post(path, json={**payload, "valid_from": "2026-10-18T00:00:00Z"}, headers={"Idempotency-Key": "accepted_1"})
    assert changed_retry.status_code == 409
    assert len((await db_session.scalars(select(MembershipVersion))).all()) == 1
    assert len((await db_session.scalars(select(CatalogReceipt))).all()) == baseline_receipts + 1
    invalid_close = await test_client.patch(
        f"{path}/{accepted.json()['id']}", json={"valid_to": "2026-10-18T00:00:00Z"}, headers={"Idempotency-Key": "bad_close", "If-Match": "1"}
    )
    assert invalid_close.status_code == 422
    assert len((await db_session.scalars(select(CatalogReceipt))).all()) == baseline_receipts + 1
    stale = await test_client.patch(
        f"{CATALOG}/{portfolio['id']}",
        json={"name": "Stale", "owner_ref": {"kind": "user", "id": catalog_editor.user_id}, "lifecycle_status": "active"},
        headers={"Idempotency-Key": "stale_portfolio", "If-Match": "2"},
    )
    missing_precondition = await test_client.patch(
        f"{CATALOG}/{portfolio['id']}",
        json={"name": "Stale", "owner_ref": {"kind": "user", "id": catalog_editor.user_id}, "lifecycle_status": "active"},
        headers={"Idempotency-Key": "no_precondition"},
    )
    assert stale.status_code == 412 and stale.json()["code"] == "stale_revision"
    assert missing_precondition.status_code == 428 and missing_precondition.json()["code"] == "precondition_required"
    assert len((await db_session.scalars(select(CatalogReceipt))).all()) == baseline_receipts + 1


@pytest.mark.asyncio
async def test_scope_cursor_and_revocation(test_client, db_session, catalog_editor):
    product = await create_product(test_client, "cursor_product_key")
    first = await create_portfolio(test_client, "A first", catalog_editor)
    second = await create_portfolio(test_client, "B second", catalog_editor)
    for index, portfolio in enumerate((first, second)):
        await add_product(test_client, catalog_editor, portfolio, product, f"cursor_member_{index}")

    page = await test_client.get(CATALOG, params={"limit": 1})
    assert page.status_code == 200, page.text
    assert page.json()["can_edit"] is True
    assert len(page.json()["items"]) == 1
    assert "next_cursor" in page.json() and "total" not in page.json()
    next_page = await test_client.get(CATALOG, params={"limit": 1, "cursor": page.json()["next_cursor"]})
    assert next_page.status_code == 200, next_page.text
    assert {page.json()["items"][0]["id"], next_page.json()["items"][0]["id"]} == {first["id"], second["id"]}
    assert "next_cursor" not in next_page.json()
    tampered = await test_client.get(CATALOG, params={"limit": 1, "cursor": page.json()["next_cursor"] + "x"})
    assert tampered.status_code == 400 and tampered.json()["code"] == "invalid_cursor"

    await db_session.execute(update(TenantMembership).where(TenantMembership.user_id == catalog_editor.user_id).values(role="member"))
    await db_session.commit()
    stale_cursor = await test_client.get(CATALOG, params={"limit": 1, "cursor": page.json()["next_cursor"]})
    assert stale_cursor.status_code == 400 and stale_cursor.json()["code"] == "invalid_cursor"
    assert {row["id"] for row in (await test_client.get(CATALOG)).json()["items"]} == {first["id"], second["id"]}
    forbidden = await test_client.post(
        CATALOG,
        json={"name": "Not allowed", "owner_ref": {"kind": "user", "id": catalog_editor.user_id}, "lifecycle_status": "active"},
        headers={"Idempotency-Key": "forbidden_1"},
    )
    assert forbidden.status_code == 403

    await db_session.execute(update(TenantMembership).where(TenantMembership.user_id == catalog_editor.user_id).values(revoked_at=datetime.now(UTC)))
    await db_session.commit()
    assert (await test_client.get(CATALOG)).status_code == 403
    assert (await test_client.get(f"{CATALOG}/{first['id']}")).json()["code"] == "not_found"
    revoked_cursor = await test_client.get(CATALOG, params={"limit": 1, "cursor": page.json()["next_cursor"]})
    assert revoked_cursor.status_code == 400 and revoked_cursor.json()["code"] == "invalid_cursor"
    assert (
        await test_client.post(
            CATALOG,
            json={"name": "Revoked", "owner_ref": {"kind": "user", "id": catalog_editor.user_id}, "lifecycle_status": "active"},
            headers={"Idempotency-Key": "revoked_1"},
        )
    ).status_code == 403


@pytest.mark.asyncio
async def test_foreign_record_hidden_from_viewer_without_count(test_client, db_session, catalog_editor):
    product = await create_product(test_client, "visible_product_key")
    portfolio = await create_portfolio(test_client, "Visible", catalog_editor)
    await add_product(test_client, catalog_editor, portfolio, product, "visible_member")
    tenant = catalog_editor.org_id
    db_session.add(Project(tenant_id=tenant, id="foreign-project", name="Foreign", owner_user_id="other-user"))
    db_session.add(
        Product(
            tenant_id=tenant,
            id="foreign-product",
            name="Foreign",
            purpose="Private",
            intended_users=["Other"],
            starting_point="idea",
            owner_user_id="other-user",
            created_by="other-user",
        )
    )
    await db_session.flush()
    db_session.add(
        ProductProjectBinding(
            tenant_id=tenant, id="foreign-binding", product_id="foreign-product", project_id="foreign-project", provenance={"source": "test-fixture"}
        )
    )
    db_session.add(
        PortfolioVersion(
            tenant_id=tenant,
            source_connection_id="adp.catalog",
            id="foreign-portfolio",
            revision=1,
            data={"name": "Private", "owner_ref": {"kind": "user", "id": "other-user"}, "lifecycle_status": "active"},
        )
    )
    db_session.add(PortfolioHead(tenant_id=tenant, source_connection_id="adp.catalog", id="foreign-portfolio", revision=1))
    await db_session.flush()
    db_session.add(
        MembershipVersion(
            tenant_id=tenant,
            relationship_id="foreign-edge",
            relationship_revision=1,
            portfolio_source="adp.catalog",
            portfolio_id="foreign-portfolio",
            portfolio_revision=1,
            product_source="adp.profile",
            product_id="foreign-product",
            product_revision=1,
            project_id="foreign-project",
            source_connection_id="adp.catalog",
            mapping_version="catalog-1",
            source_refs=[reference(catalog_editor, "foreign-project", "adp.profile", "product", "foreign-product")],
            valid_from=datetime(2026, 10, 19, tzinfo=UTC),
        )
    )
    db_session.add(
        MembershipHead(
            tenant_id=tenant,
            relationship_id="foreign-edge",
            relationship_revision=1,
            portfolio_source="adp.catalog",
            portfolio_id="foreign-portfolio",
            product_source="adp.profile",
            product_id="foreign-product",
            project_id="foreign-project",
        )
    )
    await db_session.commit()
    foreign_reference = reference(catalog_editor, "foreign-project", "adp.profile", "product", "foreign-product")
    payload = {
        "portfolio_ref": reference(catalog_editor, "foreign-project", "adp.catalog", "portfolio", portfolio["id"]),
        "product_ref": foreign_reference,
        "source_refs": [foreign_reference],
        "valid_from": "2026-10-19T00:00:00Z",
    }
    denied = await test_client.post(f"{CATALOG}/{portfolio['id']}/memberships", json=payload, headers={"Idempotency-Key": "foreign_product_attempt"})
    assert denied.status_code == 404 and denied.json()["code"] == "not_found"
    assert len((await db_session.scalars(select(MembershipVersion))).all()) == 2
    await db_session.execute(update(TenantMembership).where(TenantMembership.user_id == catalog_editor.user_id).values(role="member"))
    await db_session.commit()

    page = await test_client.get(CATALOG, params={"limit": 1})
    assert page.status_code == 200, page.text
    assert [item["id"] for item in page.json()["items"]] == [portfolio["id"]]
    assert page.json()["can_edit"] is False
    assert "total" not in page.json() and "next_cursor" not in page.json()
    hidden = await test_client.get(f"{CATALOG}/foreign-portfolio")
    unknown = await test_client.get(f"{CATALOG}/unknown-portfolio")
    assert hidden.status_code == unknown.status_code == 404
    assert hidden.json() == unknown.json() == {"code": "not_found", "message": "not found"}
    assert (await test_client.get(f"{CATALOG}/foreign-portfolio/memberships")).json() == hidden.json()
    assert {row["product_ref"]["record_id"] for row in (await test_client.get(f"{CATALOG}/{portfolio['id']}/memberships")).json()["items"]} == {
        product["product_id"]
    }


@pytest.mark.asyncio
async def test_reject_nested_and_allocation_target(test_client, db_session, catalog_editor):
    portfolio = await create_portfolio(test_client, "No nesting", catalog_editor)
    before = len((await db_session.scalars(select(CatalogReceipt))).all())
    response = await test_client.patch(
        f"{CATALOG}/{portfolio['id']}",
        json={
            "name": "Changed",
            "owner_ref": {"kind": "user", "id": catalog_editor.user_id},
            "lifecycle_status": "active",
            "allocation_target": {"currency": "USD"},
        },
        headers={"Idempotency-Key": "allocate_wrong", "If-Match": "1"},
    )
    assert response.status_code == 422 and response.json()["code"] == "invalid_request"
    nested = await test_client.post(
        f"{CATALOG}/{portfolio['id']}/memberships",
        json={
            "portfolio_ref": reference(catalog_editor, "some-project", "adp.catalog", "portfolio", portfolio["id"]),
            "product_ref": reference(catalog_editor, "some-project", "adp.catalog", "portfolio", portfolio["id"]),
            "valid_from": "2026-10-19T00:00:00Z",
            "source_refs": [reference(catalog_editor, "some-project", "adp.catalog", "portfolio", portfolio["id"])],
        },
        headers={"Idempotency-Key": "nested_wrong"},
    )
    assert nested.status_code == 422 and nested.json()["code"] == "invalid_request"
    assert len((await db_session.scalars(select(CatalogReceipt))).all()) == before
