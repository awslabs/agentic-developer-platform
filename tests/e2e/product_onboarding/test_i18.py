"""Local I18 API scenarios; these checks do not claim deployed acceptance."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from src.catalog.models import CatalogChange, CatalogReceipt, MembershipHead, MembershipVersion, PortfolioHead, PortfolioVersion
from src.products.models import Product, ProductProjectBinding, Project

pytest_plugins = ("modules.gateway.tests.conftest",)

CATALOG = "/v1/catalog/portfolios"
PRODUCT = "/v1/products"
PRODUCT_REQUEST = {
    "name": "Example Product", "purpose": "Independent example", "intended_users": ["Maintainers"],
    "starting_point": "idea", "project": {"mode": "create"},
}


@pytest.fixture
async def catalog_actor(db_session, app, valid_token_context, monkeypatch):
    from src.auth.dependencies import get_current_user
    from src.shared.models.onboarding import TenantMembership
    from src.shared.models.organization import Organization, User

    actor = valid_token_context
    db_session.add(Organization(id=actor.org_id, name="Example workspace"))
    db_session.add(User(id=actor.user_id, org_id=actor.org_id, team_id=actor.team_id,
                        email="portfolio@example.test", user_kind="human"))
    await db_session.flush()
    db_session.add(TenantMembership(user_id=actor.user_id, tenant_id=actor.org_id,
                                    role="org_admin", is_active=True))
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: actor
    monkeypatch.setenv("BG_CATALOG_ENABLED", "true")
    monkeypatch.setenv("BG_PRODUCT_PROFILE_ENABLED", "true")
    return actor


def record_ref(actor, project_id, source, kind, record_id, revision="1"):
    return {"tenant_id": actor.org_id, "project_id": project_id, "source_connection_id": source,
            "record_type": kind, "record_id": record_id, "record_revision": revision}


async def create_product(client, name, key):
    response = await client.post(PRODUCT, json={**PRODUCT_REQUEST, "name": name},
                                 headers={"Idempotency-Key": key})
    assert response.status_code == 201, response.text
    return response.json()


async def create_portfolio(client, actor, name, key):
    response = await client.post(CATALOG,
        json={"name": name, "lifecycle_status": "active",
              "owner_ref": {"kind": "user", "id": actor.user_id}},
        headers={"Idempotency-Key": key})
    assert response.status_code == 201, response.text
    return response.json()


async def link_product(client, actor, portfolio, product, key):
    portfolio_ref = record_ref(actor, product["project_id"], "adp.catalog", "portfolio", portfolio["id"])
    product_ref = record_ref(actor, product["project_id"], "adp.profile", "product", product["product_id"])
    response = await client.post(f"{CATALOG}/{portfolio['id']}/memberships",
        json={"portfolio_ref": portfolio_ref, "product_ref": product_ref,
              "source_refs": [product_ref], "valid_from": "2026-10-19T00:00:00Z"},
        headers={"Idempotency-Key": key})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def portfolio_many_to_many(test_client, db_session, catalog_actor):
    """SCN-01: exact endpoints, revisions and optional overlapping collections."""
    checkout = await create_product(test_client, "Checkout", "i18_checkout_create")
    accounts = await create_product(test_client, "Accounts", "i18_accounts_create")
    commerce = await create_portfolio(test_client, catalog_actor, "Commerce", "i18_commerce_create")
    shared = await create_portfolio(test_client, catalog_actor, "Shared services", "i18_shared_create")

    for portfolio, product, key in (
        (commerce, checkout, "i18_commerce_checkout"),
        (commerce, accounts, "i18_commerce_accounts"),
        (shared, accounts, "i18_shared_accounts"),
    ):
        await link_product(test_client, catalog_actor, portfolio, product, key)

    observed = {}
    for portfolio in (commerce, shared):
        readback = await test_client.get(f"{CATALOG}/{portfolio['id']}/memberships")
        assert readback.status_code == 200, readback.text
        assert readback.headers["X-Catalog-View"] == "current"
        observed[portfolio["id"]] = {
            (item["product_ref"]["record_id"], item["product_ref"]["project_id"],
             item["product_ref"]["record_revision"]) for item in readback.json()["items"]}
        assert all(item["portfolio_ref"]["record_id"] == portfolio["id"]
                   and item["portfolio_ref"]["source_connection_id"] == "adp.catalog"
                   for item in readback.json()["items"])
    assert observed == {
        commerce["id"]: {(checkout["product_id"], checkout["project_id"], "1"),
                         (accounts["product_id"], accounts["project_id"], "1")},
        shared["id"]: {(accounts["product_id"], accounts["project_id"], "1")},
    }
    persisted = (await db_session.scalars(select(MembershipVersion))).all()
    assert {(row.portfolio_id, row.product_id, row.project_id, row.product_revision) for row in persisted} == {
        (commerce["id"], checkout["product_id"], checkout["project_id"], 1),
        (commerce["id"], accounts["product_id"], accounts["project_id"], 1),
        (shared["id"], accounts["product_id"], accounts["project_id"], 1),
    }
    assert {row.id for row in (await db_session.scalars(select(PortfolioVersion))).all()} == {
        commerce["id"], shared["id"]}
    assert {row.id for row in (await db_session.scalars(select(Product))).all()} == {
        checkout["product_id"], accounts["product_id"]}
    assert len((await db_session.scalars(select(ProductProjectBinding))).all()) == 2
    changes = (await db_session.scalars(select(CatalogChange).where(
        CatalogChange.record_type == "portfolio_product"))).all()
    assert {change.record_id for change in changes} == {row.relationship_id for row in persisted}
    assert all(change.source_connection_id == "adp.catalog" and change.native_record_revision == "1"
               and change.source_refs for change in changes)


@pytest.mark.asyncio
async def portfolio_history(test_client, db_session, catalog_actor, monkeypatch):
    """SCN-02: correction changes current grouping but not prior decisions."""
    import src.catalog.routes as routes

    clock = datetime(2026, 10, 20, 12, tzinfo=UTC)
    monkeypatch.setattr(routes, "utcnow", lambda: clock)
    product = await create_product(test_client, "Dated Product", "i18_history_product")
    portfolio = await create_portfolio(test_client, catalog_actor, "Dated Portfolio", "i18_history_portfolio")
    first = await link_product(test_client, catalog_actor, portfolio, product, "i18_history_link")
    membership_path = f"{CATALOG}/{portfolio['id']}/memberships"
    original = (await test_client.get(membership_path)).json()["items"][0]
    assert original["relationship_revision"] == "1" and "valid_to" not in original
    initial_row = await db_session.scalar(select(MembershipVersion)
        .where(MembershipVersion.relationship_id == first["id"]))
    original_fields = {column.key: getattr(initial_row, column.key) for column in MembershipVersion.__table__.columns}

    clock = datetime(2026, 10, 21, 12, tzinfo=UTC)
    first_close = await test_client.patch(f"{membership_path}/{first['id']}",
        json={"valid_to": "2026-11-10T00:00:00Z"},
        headers={"Idempotency-Key": "i18_close_first", "If-Match": "1"})
    assert first_close.status_code == 200, first_close.text
    assert first_close.json()["revision"] == "2"

    clock = datetime(2026, 10, 22, 12, tzinfo=UTC)
    correction = await test_client.patch(f"{membership_path}/{first['id']}",
        json={"valid_to": "2026-10-21T12:00:00Z"},
        headers={"Idempotency-Key": "i18_close_correction", "If-Match": "2"})
    assert correction.status_code == 200, correction.text
    assert correction.json()["revision"] == "3"

    current = await test_client.get(membership_path)
    assert current.status_code == 200 and current.json()["items"] == []
    assert current.headers["X-Catalog-View"] == "current"
    selected = {"effective_at": "2026-10-22T00:00:00Z"}
    original_read = await test_client.get(membership_path,
        params={**selected, "decision_at": "2026-10-20T12:30:00Z"})
    first_close_read = await test_client.get(membership_path,
        params={**selected, "decision_at": "2026-10-21T12:30:00Z"})
    after_correction = await test_client.get(membership_path,
        params={**selected, "decision_at": "2026-10-22T12:00:00Z"})
    assert original_read.status_code == first_close_read.status_code == after_correction.status_code == 200
    assert original_read.headers["X-Catalog-View"] == "historical"
    assert original_read.json()["items"] == [original]
    assert first_close_read.json()["items"][0]["relationship_revision"] == "2"
    assert after_correction.json()["items"] == []
    earlier_effective = await test_client.get(membership_path,
        params={"effective_at": "2026-10-20T18:00:00Z", "decision_at": "2026-10-22T12:00:00Z"})
    assert earlier_effective.json()["items"][0]["relationship_revision"] == "3"
    assert earlier_effective.json()["items"][0]["supersedes_revision"] == "2"

    rows = (await db_session.scalars(select(MembershipVersion)
        .order_by(MembershipVersion.relationship_revision).execution_options(populate_existing=True))).all()
    assert [row.relationship_revision for row in rows] == [1, 2, 3]
    assert {column.key: getattr(rows[0], column.key) for column in MembershipVersion.__table__.columns} == original_fields
    assert rows[0].valid_to is None and rows[0].source_refs == original["source_refs"]
    assert rows[0].recorded_at.replace(tzinfo=UTC) == datetime(2026, 10, 20, 12, tzinfo=UTC)
    assert [row.supersedes_revision for row in rows] == [None, 1, 2]
    assert rows[1].valid_to.replace(tzinfo=UTC) == datetime(2026, 11, 10, tzinfo=UTC)
    assert rows[2].valid_to.replace(tzinfo=UTC) == datetime(2026, 10, 21, 12, tzinfo=UTC)
    assert len((await db_session.scalars(select(CatalogReceipt))).all()) == 4
    changes = (await db_session.scalars(select(CatalogChange).where(
        CatalogChange.record_type == "portfolio_product").order_by(CatalogChange.relationship_revision))).all()
    assert [change.relationship_revision for change in changes] == [1, 2, 3]
    assert [change.valid_to for change in changes] == [row.valid_to for row in rows]
    assert (await db_session.scalar(select(Product))).revision == 1


@pytest.mark.asyncio
async def portfolio_denials(test_client, db_session, catalog_actor):
    """SCN-03: denied scope and unsupported edges leave the saved catalog unchanged."""
    from src.shared.models.onboarding import TenantMembership

    actor = catalog_actor
    product = await create_product(test_client, "Owned Product", "i18_denial_owned")
    portfolio = await create_portfolio(test_client, actor, "Visible Portfolio", "i18_denial_portfolio")
    await link_product(test_client, actor, portfolio, product, "i18_denial_link")
    owned_project = product["project_id"]
    db_session.add(Project(tenant_id=actor.org_id, id="foreign-project", name="Foreign Project",
                           owner_user_id="other-user"))
    db_session.add(Product(tenant_id=actor.org_id, id="foreign-product", name="Foreign Product",
                           purpose="Private", intended_users=["Other"], starting_point="idea",
                           owner_user_id="other-user", created_by="other-user"))
    await db_session.flush()
    db_session.add(ProductProjectBinding(tenant_id=actor.org_id, id="foreign-binding",
                                         product_id="foreign-product", project_id="foreign-project",
                                         provenance={"source": "run-owned-fixture"}))
    db_session.add(PortfolioVersion(tenant_id=actor.org_id, source_connection_id="adp.catalog",
                                    id="foreign-portfolio", revision=1,
                                    data={"name": "Private Portfolio", "lifecycle_status": "active",
                                          "owner_ref": {"kind": "user", "id": "other-user"}}))
    db_session.add(PortfolioHead(tenant_id=actor.org_id, source_connection_id="adp.catalog",
                                 id="foreign-portfolio", revision=1))
    await db_session.flush()
    foreign_ref = record_ref(actor, "foreign-project", "adp.profile", "product", "foreign-product")
    db_session.add(MembershipVersion(tenant_id=actor.org_id, relationship_id="foreign-edge",
        relationship_revision=1, portfolio_source="adp.catalog", portfolio_id="foreign-portfolio",
        portfolio_revision=1, product_source="adp.profile", product_id="foreign-product",
        product_revision=1, project_id="foreign-project", source_connection_id="adp.catalog",
        mapping_version="catalog-1", source_refs=[foreign_ref],
        valid_from=datetime(2026, 10, 19, tzinfo=UTC)))
    db_session.add(MembershipHead(tenant_id=actor.org_id, relationship_id="foreign-edge", relationship_revision=1,
                                  portfolio_source="adp.catalog", portfolio_id="foreign-portfolio",
                                  product_source="adp.profile", product_id="foreign-product", project_id="foreign-project"))
    await db_session.commit()

    baseline = {
        "portfolios": len((await db_session.scalars(select(PortfolioVersion))).all()),
        "memberships": len((await db_session.scalars(select(MembershipVersion))).all()),
        "receipts": len((await db_session.scalars(select(CatalogReceipt))).all()),
        "changes": len((await db_session.scalars(select(CatalogChange))).all()),
        "products": len((await db_session.scalars(select(Product))).all()),
    }
    project_configuration = (await db_session.scalar(select(Project).where(Project.id == owned_project))).configuration
    await db_session.execute(update(TenantMembership).where(TenantMembership.user_id == actor.user_id)
                             .values(role="member"))
    await db_session.commit()

    visible = await test_client.get(CATALOG, params={"limit": 1})
    foreign = await test_client.get(f"{CATALOG}/foreign-portfolio")
    absent = await test_client.get(f"{CATALOG}/never-existed")
    assert visible.status_code == 200, visible.text
    assert [item["id"] for item in visible.json()["items"]] == [portfolio["id"]]
    assert "total" not in visible.json() and "next_cursor" not in visible.json()
    assert foreign.status_code == absent.status_code == 404
    assert foreign.json() == absent.json() == {"code": "not_found", "message": "not found"}
    assert (await test_client.get(f"{CATALOG}/foreign-portfolio/memberships")).json() == foreign.json()

    await db_session.execute(update(TenantMembership).where(TenantMembership.user_id == actor.user_id)
                             .values(role="org_admin"))
    await db_session.commit()
    membership_path = f"{CATALOG}/{portfolio['id']}/memberships"
    foreign_membership = {
        "portfolio_ref": record_ref(actor, "foreign-project", "adp.catalog", "portfolio", portfolio["id"]),
        "product_ref": foreign_ref, "source_refs": [foreign_ref],
        "valid_from": "2026-10-19T00:00:00Z",
    }
    denied = await test_client.post(membership_path, json=foreign_membership,
                                    headers={"Idempotency-Key": "i18_denial_foreign"})
    assert denied.status_code == 404 and denied.json() == absent.json()
    assert (await db_session.scalar(select(Product).where(Product.id == "foreign-product"))) is not None

    nested = await test_client.post(membership_path,
        json={"portfolio_ref": record_ref(actor, owned_project, "adp.catalog", "portfolio", portfolio["id"]),
              "product_ref": record_ref(actor, owned_project, "adp.catalog", "portfolio", portfolio["id"]),
              "source_refs": [record_ref(actor, owned_project, "adp.catalog", "portfolio", portfolio["id"])],
              "valid_from": "2026-10-19T00:00:00Z"},
        headers={"Idempotency-Key": "i18_denial_nested"})
    allocation = await test_client.patch(f"{CATALOG}/{portfolio['id']}",
        json={"name": "Modified", "lifecycle_status": "active",
              "owner_ref": {"kind": "user", "id": actor.user_id},
              "allocation_target": {"amount": "50.00", "currency": "USD"}},
        headers={"Idempotency-Key": "i18_denial_allocation", "If-Match": "1"})
    assert nested.status_code == allocation.status_code == 422
    assert nested.json()["code"] == allocation.json()["code"] == "invalid_request"

    await db_session.execute(update(TenantMembership).where(TenantMembership.user_id == actor.user_id)
                             .values(revoked_at=datetime.now(UTC)))
    await db_session.commit()
    revoked_read = await test_client.get(f"{CATALOG}/{portfolio['id']}")
    revoked_write = await test_client.post(membership_path, json=foreign_membership,
        headers={"Idempotency-Key": "i18_denial_revoked"})
    assert revoked_read.status_code == 404 and revoked_read.json() == absent.json()
    assert revoked_write.status_code == 403 and revoked_write.json()["code"] == "permission_denied"

    assert len((await db_session.scalars(select(PortfolioVersion))).all()) == baseline["portfolios"]
    assert len((await db_session.scalars(select(MembershipVersion))).all()) == baseline["memberships"]
    assert len((await db_session.scalars(select(CatalogReceipt))).all()) == baseline["receipts"]
    assert len((await db_session.scalars(select(CatalogChange))).all()) == baseline["changes"]
    assert len((await db_session.scalars(select(Product))).all()) == baseline["products"]
    assert (await db_session.scalar(select(Project).where(Project.id == owned_project))).configuration == project_configuration
