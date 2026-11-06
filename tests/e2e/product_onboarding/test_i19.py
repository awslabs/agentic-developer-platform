"""Local I19 API scenarios; native-provider and deployed acceptance remain separate."""

import pytest
from sqlalchemy import select

from src.auth.dependencies import get_current_user
from src.catalog.models import ContributionHead, ContributionVersion, InitiativeHead, InitiativeVersion
from src.products.models import Product, Project
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User

pytest_plugins = ("modules.gateway.tests.conftest",)


@pytest.fixture
async def catalog_actor(db_session, app, valid_token_context, monkeypatch):
    actor = valid_token_context
    db_session.add(Organization(id=actor.org_id, name="Example workspace"))
    db_session.add(User(id=actor.user_id, org_id=actor.org_id, team_id=actor.team_id, email="initiative@example.test", user_kind="human"))
    await db_session.flush()
    db_session.add(TenantMembership(user_id=actor.user_id, tenant_id=actor.org_id, role="org_admin", is_active=True))
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: actor
    monkeypatch.setenv("BG_CATALOG_ENABLED", "true")
    from src.catalog import initiative_routes

    async def fixture_native_work(caller, reference):
        return caller.user_id == actor.user_id and caller.org_id == actor.org_id and reference.record_id not in {"missing-work", "inaccessible-work"}

    monkeypatch.setattr(initiative_routes, "native_work_verified", fixture_native_work)
    monkeypatch.setenv("BG_PRODUCT_PROFILE_ENABLED", "true")
    return actor


def record_ref(actor, project_id, source, kind, record_id):
    return {
        "tenant_id": actor.org_id,
        "project_id": project_id,
        "source_connection_id": source,
        "record_type": kind,
        "record_id": record_id,
        "record_revision": "1",
    }


async def create_product(client, db_session, actor, name):
    response = await client.post(
        "/v1/products",
        json={
            "name": name,
            "purpose": "Example scenario",
            "intended_users": ["Maintainers"],
            "starting_point": "idea",
            "project": {"mode": "create"},
        },
        headers={"Idempotency-Key": f"i19-scn1-{name.replace(' ', '-')}"},
    )
    assert response.status_code == 201, response.text
    product = response.json()
    project = await db_session.get(Project, (actor.org_id, product["project_id"]))
    project.configuration = {"basic": {"trackers": [record_ref(actor, project.id, "adp.profile", "tracker", "example.native-tracker")]}}
    await db_session.commit()
    return product


@pytest.mark.asyncio
async def initiative_cross_product(test_client, db_session, catalog_actor):
    """SCN-01: two Project scopes and Portfolios share one Initiative without sharing work refs."""
    actor = catalog_actor
    checkout = await create_product(test_client, db_session, actor, "Checkout")
    accounts = await create_product(test_client, db_session, actor, "Accounts")
    initiative = await test_client.post(
        "/v1/catalog/initiatives", json={"name": "Improve checkout", "status": "planned"}, headers={"Idempotency-Key": "i19-scn1-initiative"}
    )
    assert initiative.status_code == 201, initiative.text
    initiative_id = initiative.json()["id"]
    recorded = {}
    for name, product in (("Checkout", checkout), ("Accounts", accounts)):
        project_id = product["project_id"]
        portfolio = await test_client.post(
            "/v1/catalog/portfolios",
            json={"name": f"{name} portfolio", "lifecycle_status": "active", "owner_ref": {"kind": "user", "id": actor.user_id}},
            headers={"Idempotency-Key": f"i19-scn1-portfolio-{name}"},
        )
        assert portfolio.status_code == 201, portfolio.text
        product_ref = record_ref(actor, project_id, "adp.profile", "product", product["product_id"])
        member = await test_client.post(
            f"/v1/catalog/portfolios/{portfolio.json()['id']}/memberships",
            json={
                "portfolio_ref": record_ref(actor, project_id, "adp.catalog", "portfolio", portfolio.json()["id"]),
                "product_ref": product_ref,
                "source_refs": [product_ref],
                "valid_from": "2026-10-19T00:00:00Z",
            },
            headers={"Idempotency-Key": f"i19-scn1-membership-{name}"},
        )
        assert member.status_code == 201, member.text
        work_links = [
            {"work_ref": record_ref(actor, project_id, "example.native-tracker", kind, f"{name}-{kind}"), "valid_from": "2026-10-19T00:00:00Z"}
            for kind in ("epic", "story", "release")
        ]
        work_links[0]["work_ref"]["record_revision"] = f"source-etag:{name}"
        result = await test_client.post(
            f"/v1/catalog/projects/{project_id}/contributions",
            json={
                "initiative_ref": record_ref(actor, project_id, "adp.catalog", "initiative", initiative_id),
                "product_ref": product_ref,
                "objective": f"Deliver {name} outcome",
                "owner_ref": {"kind": "user", "id": actor.user_id},
                "valid_from": "2026-10-19T00:00:00Z",
                "work_links": work_links,
            },
            headers={"Idempotency-Key": f"i19-scn1-contribution-{name}"},
        )
        assert result.status_code == 201, result.text
        recorded[name] = (product, result.json()["id"], portfolio.json()["id"])
    assert len({identity[1] for identity in recorded.values()}) == 2
    assert len({identity[2] for identity in recorded.values()}) == 2
    assert len((await db_session.scalars(select(InitiativeHead))).all()) == 1
    assert len((await db_session.scalars(select(InitiativeVersion))).all()) == 1
    assert len((await db_session.scalars(select(Product))).all()) == 2
    heads = (await db_session.scalars(select(ContributionHead))).all()
    assert {head.project_id for head in heads} == {checkout["project_id"], accounts["project_id"]}
    for name, (product, contribution_id, _) in recorded.items():
        row = await db_session.get(ContributionVersion, (actor.org_id, "adp.catalog", contribution_id, 1))
        assert row.initiative_id == initiative_id and row.project_id == product["project_id"]
        readback = await test_client.get(f"/v1/catalog/projects/{product['project_id']}/contributions")
        assert readback.status_code == 200 and len(readback.json()["items"]) == 1
        item = readback.json()["items"][0]
        assert item["data"]["product_ref"]["record_id"] == product["product_id"]
        assert item["data"]["work_links"][0]["work_ref"]["record_revision"] == f"source-etag:{name}"
        assert {(link["work_ref"]["record_type"], link["work_ref"]["record_id"]) for link in item["data"]["work_links"]} == {
            (kind, f"{name}-{kind}") for kind in ("epic", "story", "release")
        }


@pytest.mark.asyncio
async def contribution_versioning(test_client, db_session, catalog_actor, monkeypatch):
    """SCN-02: clock-pinned corrections, historical readback, duplicate rejection; synthetic work refs."""
    from datetime import UTC, datetime, timedelta

    from src.catalog import initiative_routes
    from src.catalog.models import CatalogChange, CatalogReceipt

    actor = catalog_actor
    product = await create_product(test_client, db_session, actor, "Versioned")
    project_id = product["project_id"]
    initiative = await test_client.post(
        "/v1/catalog/initiatives", json={"name": "Deliver outcome", "status": "planned"}, headers={"Idempotency-Key": "i19-scn2-initiative"}
    )
    assert initiative.status_code == 201, initiative.text
    path = f"/v1/catalog/projects/{project_id}/contributions"
    original = record_ref(actor, project_id, "example.native-tracker", "story", "original-story")
    replacement = record_ref(actor, project_id, "example.native-tracker", "release", "selected-release")
    now = datetime.now(UTC)
    first_effective = now - timedelta(days=3)
    switch = now - timedelta(days=1)
    old_recorded = now - timedelta(hours=2)
    new_recorded = now - timedelta(hours=1)
    payload = {
        "initiative_ref": record_ref(actor, project_id, "adp.catalog", "initiative", initiative.json()["id"]),
        "product_ref": record_ref(actor, project_id, "adp.profile", "product", product["product_id"]),
        "objective": "Original outcome",
        "owner_ref": {"kind": "user", "id": actor.user_id},
        "valid_from": first_effective.isoformat(),
        "work_links": [{"work_ref": original, "valid_from": first_effective.isoformat()}],
    }
    monkeypatch.setattr(initiative_routes, "utcnow", lambda: old_recorded)
    created = await test_client.post(path, json=payload, headers={"Idempotency-Key": "i19-scn2-first"})
    assert created.status_code == 201, created.text
    contribution_id = created.json()["id"]
    revised = {
        **payload,
        "objective": "Revised outcome",
        "work_links": [
            {"work_ref": original, "valid_from": first_effective.isoformat(), "valid_to": switch.isoformat()},
            {"work_ref": replacement, "valid_from": switch.isoformat()},
        ],
    }
    monkeypatch.setattr(initiative_routes, "utcnow", lambda: new_recorded)
    correction = await test_client.patch(
        f"{path}/{contribution_id}", json=revised, headers={"If-Match": "1", "Idempotency-Key": "i19-scn2-correction"}
    )
    assert correction.status_code == 200, correction.text
    assert correction.json()["revision"] == "2"
    assert (
        await test_client.patch(f"{path}/{contribution_id}", json=revised, headers={"If-Match": "1", "Idempotency-Key": "i19-scn2-correction"})
    ).json() == correction.json()
    prior = await test_client.get(
        path, params={"effective_at": (switch - timedelta(hours=1)).isoformat(), "decision_at": (old_recorded + timedelta(minutes=30)).isoformat()}
    )
    assert prior.status_code == 200, prior.text
    assert prior.headers["X-Catalog-View"] == "historical"
    assert [
        (item["revision"], item["data"]["objective"], [link["work_ref"]["record_id"] for link in item["data"]["work_links"]])
        for item in prior.json()["items"]
    ] == [("1", "Original outcome", ["original-story"])]
    current = await test_client.get(path)
    assert current.status_code == 200, current.text
    assert [
        (item["revision"], item["data"]["objective"], [link["work_ref"]["record_id"] for link in item["data"]["work_links"]])
        for item in current.json()["items"]
    ] == [("2", "Revised outcome", ["selected-release"])]
    first = await db_session.get(ContributionVersion, (actor.org_id, "adp.catalog", contribution_id, 1))
    second = await db_session.get(ContributionVersion, (actor.org_id, "adp.catalog", contribution_id, 2))
    assert (first.supersedes_revision, second.supersedes_revision) == (None, 1)
    assert (first.data["objective"], second.data["objective"]) == ("Original outcome", "Revised outcome")
    assert [item["work_ref"]["record_id"] for item in first.work_links] == ["original-story"]
    before = (
        len((await db_session.scalars(select(ContributionVersion))).all()),
        len((await db_session.scalars(select(CatalogChange))).all()),
        len((await db_session.scalars(select(CatalogReceipt))).all()),
    )
    duplicate = await test_client.post(path, json=payload, headers={"Idempotency-Key": "i19-scn2-duplicate"})
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "conflict"
    assert (
        len((await db_session.scalars(select(ContributionVersion))).all()),
        len((await db_session.scalars(select(CatalogChange))).all()),
        len((await db_session.scalars(select(CatalogReceipt))).all()),
    ) == before


@pytest.mark.asyncio
async def contribution_optional_access(test_client, db_session, app, catalog_actor):
    """SCN-03: routine Profile work is optional; denied links/views have no hidden counts or side effects."""
    from src.catalog.models import CatalogChange, CatalogReceipt

    actor = catalog_actor
    ordinary = await test_client.post(
        "/v1/products",
        json={
            "name": "Routine Product",
            "purpose": "Unlinked work",
            "intended_users": ["Maintainers"],
            "starting_point": "idea",
            "project": {"mode": "create"},
        },
        headers={"Idempotency-Key": "i19-scn3-routine"},
    )
    assert ordinary.status_code == 201, ordinary.text
    ordinary_profile = await test_client.get(f"/v1/products/{ordinary.json()['product_id']}/projects/{ordinary.json()['project_id']}")
    assert ordinary_profile.status_code == 200 and ordinary_profile.json()["product_id"] == ordinary.json()["product_id"]
    ordinary_contributions = await test_client.get(f"/v1/catalog/projects/{ordinary.json()['project_id']}/contributions")
    assert ordinary_contributions.status_code == 200 and ordinary_contributions.json() == {"items": []}

    checkout = await create_product(test_client, db_session, actor, "Access Checkout")
    initiative = await test_client.post(
        "/v1/catalog/initiatives", json={"name": "Improve checkout", "status": "planned"}, headers={"Idempotency-Key": "i19-scn3-initiative"}
    )
    assert initiative.status_code == 201, initiative.text
    project_id = checkout["project_id"]
    path = f"/v1/catalog/projects/{project_id}/contributions"
    payload = {
        "initiative_ref": record_ref(actor, project_id, "adp.catalog", "initiative", initiative.json()["id"]),
        "product_ref": record_ref(actor, project_id, "adp.profile", "product", checkout["product_id"]),
        "objective": "Checkout improvement",
        "owner_ref": {"kind": "user", "id": actor.user_id},
        "valid_from": "2026-10-19T00:00:00Z",
        "work_links": [
            {"work_ref": record_ref(actor, project_id, "example.native-tracker", "story", "checkout-story"), "valid_from": "2026-10-19T00:00:00Z"}
        ],
    }
    created = await test_client.post(path, json=payload, headers={"Idempotency-Key": "i19-scn3-authorized"})
    assert created.status_code == 201, created.text
    counts = (
        len((await db_session.scalars(select(ContributionVersion))).all()),
        len((await db_session.scalars(select(CatalogReceipt))).all()),
        len((await db_session.scalars(select(CatalogChange))).all()),
    )
    forbidden = {
        **payload,
        "work_links": [
            {
                "work_ref": record_ref(actor, ordinary.json()["project_id"], "example.native-tracker", "story", "private-story"),
                "valid_from": "2026-10-19T00:00:00Z",
            }
        ],
    }
    denied = await test_client.post(path, json=forbidden, headers={"Idempotency-Key": "i19-scn3-foreign-work"})
    assert denied.status_code == 404 and denied.json()["code"] == "not_found"
    assert (
        len((await db_session.scalars(select(ContributionVersion))).all()),
        len((await db_session.scalars(select(CatalogReceipt))).all()),
        len((await db_session.scalars(select(CatalogChange))).all()),
    ) == counts

    outsider = actor.model_copy(update={"user_id": "unrelated-user", "team_id": "unrelated-team"})
    db_session.add(User(id=outsider.user_id, org_id=actor.org_id, team_id=outsider.team_id, email="unrelated@example.test", user_kind="human"))
    await db_session.flush()
    db_session.add(TenantMembership(user_id=outsider.user_id, tenant_id=actor.org_id, role="member", is_active=True))
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: outsider
    missing = await test_client.get("/v1/catalog/initiatives/nonexistent")
    hidden = await test_client.get(f"/v1/catalog/initiatives/{initiative.json()['id']}")
    assert hidden.status_code == 404 and hidden.json() == missing.json()
    assert (await test_client.get("/v1/catalog/initiatives")).json() == {"items": []}
    hidden_contributions = await test_client.get(path)
    assert hidden_contributions.status_code == 404 and hidden_contributions.json() == missing.json()
    assert (await test_client.get(f"{path}/{created.json()['id']}")).json() == missing.json()
    assert (await test_client.get(f"/v1/products/{checkout['product_id']}/projects/{project_id}")).status_code == 404
    assert (
        len((await db_session.scalars(select(ContributionVersion))).all()),
        len((await db_session.scalars(select(CatalogReceipt))).all()),
        len((await db_session.scalars(select(CatalogChange))).all()),
    ) == counts
