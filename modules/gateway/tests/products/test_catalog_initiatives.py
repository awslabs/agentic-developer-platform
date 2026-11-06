"""Catalog Initiative/Contribution API against the in-process gateway."""

import pytest
from sqlalchemy import select, update

from src.auth.dependencies import get_current_user
from src.catalog.models import CatalogChange, CatalogReceipt, ContributionHead, ContributionVersion, InitiativeHead, InitiativeVersion
from src.products.models import Project
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from tests.products.membership import add_team_member


@pytest.fixture
async def initiative_actor(db_session, app, valid_token_context, monkeypatch):
    actor = valid_token_context
    db_session.add(Organization(id=actor.org_id, name="Example workspace"))
    db_session.add(User(id=actor.user_id, org_id=actor.org_id, team_id=actor.team_id, email="initiative@example.test", user_kind="human"))
    await db_session.flush()
    db_session.add(TenantMembership(user_id=actor.user_id, tenant_id=actor.org_id, role="org_admin", is_active=True))
    await add_team_member(db_session, actor)
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: actor
    monkeypatch.setenv("BG_CATALOG_ENABLED", "true")
    from src.catalog import initiative_routes

    async def fixture_native_work(caller, reference):
        return caller.user_id == actor.user_id and caller.org_id == actor.org_id and reference.record_id not in {"missing-work", "inaccessible-work"}

    monkeypatch.setattr(initiative_routes, "native_work_verified", fixture_native_work)
    return actor


def ref(actor, project_id, source, kind, record_id, revision="1"):
    return {
        "tenant_id": actor.org_id,
        "project_id": project_id,
        "source_connection_id": source,
        "record_type": kind,
        "record_id": record_id,
        "record_revision": revision,
    }


async def product(client, name, db_session=None, actor=None):
    response = await client.post(
        "/v1/products",
        json={"name": name, "purpose": "Example", "intended_users": ["Maintainers"], "starting_point": "idea", "project": {"mode": "create"}},
        headers={"Idempotency-Key": f"i19-product-{name.replace(' ', '-')}"},
    )
    assert response.status_code == 201, response.text
    record = response.json()
    if db_session is not None:
        project = await db_session.get(Project, (actor.org_id, record["project_id"]))
        project.configuration = {"basic": {"trackers": [ref(actor, project.id, "adp.profile", "tracker", "example.native-tracker")]}}
        await db_session.commit()
    return record


async def initiative(client, actor):
    response = await client.post(
        "/v1/catalog/initiatives", json={"name": "Improve checkout", "status": "planned"}, headers={"Idempotency-Key": "i19-initiative"}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def portfolio_with_product(client, actor, product_record, name):
    response = await client.post(
        "/v1/catalog/portfolios",
        json={"name": name, "owner_ref": {"kind": "user", "id": actor.user_id}, "lifecycle_status": "active"},
        headers={"Idempotency-Key": f"i19-portfolio-{name}"},
    )
    assert response.status_code == 201, response.text
    project_id = product_record["project_id"]
    portfolio_id = response.json()["id"]
    product_ref = ref(actor, project_id, "adp.profile", "product", product_record["product_id"])
    link = await client.post(
        f"/v1/catalog/portfolios/{portfolio_id}/memberships",
        json={
            "portfolio_ref": ref(actor, project_id, "adp.catalog", "portfolio", portfolio_id),
            "product_ref": product_ref,
            "source_refs": [product_ref],
            "valid_from": "2026-10-19T00:00:00Z",
        },
        headers={"Idempotency-Key": f"i19-portfolio-link-{name}"},
    )
    assert link.status_code == 201, link.text
    return portfolio_id


def contribution_data(actor, initiative_id, product_record, work_id="epic-1"):
    project_id = product_record["project_id"]
    return {
        "initiative_ref": ref(actor, project_id, "adp.catalog", "initiative", initiative_id),
        "product_ref": ref(actor, project_id, "adp.profile", "product", product_record["product_id"]),
        "owner_ref": {"kind": "user", "id": actor.user_id},
        "objective": "Deliver a measurable improvement",
        "valid_from": "2026-10-19T00:00:00Z",
        "work_links": [
            {"work_ref": ref(actor, project_id, "example.native-tracker", kind, f"{work_id}-{kind}"), "valid_from": "2026-10-19T00:00:00Z"}
            for kind in ("epic", "story", "release")
        ],
    }


@pytest.mark.asyncio
async def test_catalog_initiative_selection_pins_revision_without_creating_contribution(test_client, db_session, initiative_actor):
    actor = initiative_actor
    created = await product(test_client, "Initiative Selection")
    selected = await initiative(test_client, actor)
    base = f"/v1/products/{created['product_id']}/projects/{created['project_id']}"
    started = await test_client.post(
        f"{base}/onboarding-sessions",
        json={},
        headers={
            "Idempotency-Key": "initiative-selection-session",
            "If-Profile-Revision": "1",
        },
    )
    assert started.status_code == 201, started.text
    session_path = f"{base}/onboarding-sessions/{started.json()['id']}"
    reference = ref(actor, created["project_id"], "adp.catalog", "initiative", selected["id"], selected["revision"])
    payload = {"action": "save", "configuration": {"financials": {"initiatives": [reference]}}}
    saved = await test_client.put(
        f"{session_path}/steps/financials",
        json=payload,
        headers={
            "Idempotency-Key": "initiative-selection-save",
            "If-Profile-Revision": "1",
        },
    )
    assert saved.status_code == 200, saved.text
    readback = await test_client.get(base)
    assert readback.status_code == 200
    assert readback.json()["configuration"]["financials"]["initiatives"] == [reference]
    assert (await db_session.scalars(select(ContributionHead))).all() == []
    assert len((await db_session.scalars(select(InitiativeHead))).all()) == 1

    changed = await test_client.patch(
        f"/v1/catalog/initiatives/{selected['id']}",
        json={"name": "Improved checkout", "status": "planned"},
        headers={
            "Idempotency-Key": "initiative-selection-change",
            "If-Match": selected["revision"],
        },
    )
    assert changed.status_code == 200, changed.text
    stale = await test_client.put(
        f"{session_path}/steps/financials",
        json=payload,
        headers={
            "Idempotency-Key": "initiative-selection-stale",
            "If-Profile-Revision": str(readback.json()["revision"]),
        },
    )
    assert stale.status_code == 412 and stale.json()["detail"]["code"] == "stale_revision"
    await db_session.execute(update(TenantMembership).where(TenantMembership.user_id == actor.user_id).values(role="member"))
    await db_session.commit()
    inaccessible = await test_client.get(f"/v1/catalog/initiatives/{selected['id']}")
    assert inaccessible.status_code == 404
    denied = await test_client.put(
        f"{session_path}/steps/financials",
        json=payload,
        headers={
            "Idempotency-Key": "initiative-selection-revoked",
            "If-Profile-Revision": str(readback.json()["revision"]),
        },
    )
    assert denied.status_code == 404
    assert (await db_session.scalars(select(InitiativeHead))).all() != []
    assert (await db_session.scalars(select(ContributionHead))).all() == []
    assert (await db_session.scalars(select(ContributionHead))).all() == []


@pytest.mark.asyncio
async def test_two_products_keep_independent_contributions_and_native_work_refs(test_client, db_session, initiative_actor):
    actor = initiative_actor
    checkout = await product(test_client, "Checkout", db_session, actor)
    accounts = await product(test_client, "Accounts", db_session, actor)
    checkout_portfolio = await portfolio_with_product(test_client, actor, checkout, "Commerce")
    accounts_portfolio = await portfolio_with_product(test_client, actor, accounts, "Shared Services")
    assert checkout_portfolio != accounts_portfolio
    initiative_record = await initiative(test_client, actor)
    results = []
    for name, product_record in (("checkout", checkout), ("accounts", accounts)):
        payload = contribution_data(actor, initiative_record["id"], product_record, f"{name}-epic")
        payload["work_links"][0]["work_ref"]["record_revision"] = f"etag:{name}"
        response = await test_client.post(
            f"/v1/catalog/projects/{product_record['project_id']}/contributions",
            json=payload,
            headers={"Idempotency-Key": f"i19-{name}-contribution"},
        )
        assert response.status_code == 201, response.text
        results.append((product_record, response.json()))
    assert results[0][1]["id"] != results[1][1]["id"]
    for (name, _), (product_record, result) in zip((("checkout", checkout), ("accounts", accounts)), results):
        readback = await test_client.get(f"/v1/catalog/projects/{product_record['project_id']}/contributions")
        assert readback.status_code == 200, readback.text
        items = readback.json()["items"]
        assert len(items) == 1
        assert items[0]["id"] == result["id"]
        assert items[0]["data"]["initiative_ref"]["record_id"] == initiative_record["id"]
        assert items[0]["data"]["product_ref"]["record_id"] == product_record["product_id"]
        work = {(link["work_ref"]["record_type"], link["work_ref"]["record_id"]) for link in items[0]["data"]["work_links"]}
        assert work == {(kind, f"{name}-epic-{kind}") for kind in ("epic", "story", "release")}
        assert items[0]["data"]["work_links"][0]["work_ref"]["record_revision"] == f"etag:{name}"
    assert len((await db_session.scalars(select(ContributionHead))).all()) == 2
    assert len((await db_session.scalars(select(ContributionVersion))).all()) == 2
    assert len((await db_session.scalars(select(InitiativeHead))).all()) == 1
    assert len((await db_session.scalars(select(InitiativeVersion))).all()) == 1


@pytest.mark.asyncio
async def test_contribution_revisions_preserve_prior_outcome_and_dated_links(test_client, db_session, initiative_actor, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from src.catalog import initiative_routes

    actor = initiative_actor
    product_record = await product(test_client, "Versioned Product", db_session, actor)
    initiative_record = await initiative(test_client, actor)
    project_id = product_record["project_id"]
    path = f"/v1/catalog/projects/{project_id}/contributions"
    now = datetime.now(UTC)
    start = now - timedelta(days=3)
    switch = now - timedelta(days=1)
    first_payload = contribution_data(actor, initiative_record["id"], product_record)
    first_payload["valid_from"] = start.isoformat()
    first_payload["work_links"] = [
        {"work_ref": ref(actor, project_id, "example.native-tracker", "story", "original"), "valid_from": start.isoformat()}
    ]
    initial_recorded = now - timedelta(hours=2)
    monkeypatch.setattr(initiative_routes, "utcnow", lambda: initial_recorded)
    created = await test_client.post(path, json=first_payload, headers={"Idempotency-Key": "i19-first"})
    assert created.status_code == 201, created.text
    contribution_id = created.json()["id"]

    updated_payload = {
        **first_payload,
        "objective": "Revised commitment",
        "work_links": [
            {"work_ref": first_payload["work_links"][0]["work_ref"], "valid_from": start.isoformat(), "valid_to": switch.isoformat()},
            {"work_ref": ref(actor, project_id, "example.native-tracker", "release", "current"), "valid_from": switch.isoformat()},
        ],
    }
    monkeypatch.setattr(initiative_routes, "utcnow", lambda: now - timedelta(hours=1))
    updated = await test_client.patch(f"{path}/{contribution_id}", json=updated_payload, headers={"Idempotency-Key": "i19-revision", "If-Match": "1"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] == "2"
    current = await test_client.get(f"{path}/{contribution_id}")
    assert current.status_code == 200, current.text
    assert current.json()["data"]["objective"] == "Revised commitment"
    assert [item["work_ref"]["record_id"] for item in current.json()["data"]["work_links"]] == ["current"]
    before_switch = await test_client.get(path, params={"effective_at": (switch - timedelta(hours=1)).isoformat()})
    assert before_switch.status_code == 200, before_switch.text
    assert before_switch.headers["X-Catalog-View"] == "historical"
    assert [item["work_ref"]["record_id"] for item in before_switch.json()["items"][0]["data"]["work_links"]] == ["original"]
    before_decision = await test_client.get(
        path, params={"effective_at": (switch - timedelta(hours=1)).isoformat(), "decision_at": (now - timedelta(hours=1, minutes=30)).isoformat()}
    )
    assert before_decision.status_code == 200, before_decision.text
    assert before_decision.json()["items"][0]["revision"] == "1"
    assert before_decision.json()["items"][0]["data"]["objective"] == first_payload["objective"]
    rows = (await db_session.scalars(select(ContributionVersion).order_by(ContributionVersion.revision))).all()
    assert [row.revision for row in rows] == [1, 2]
    assert [row.supersedes_revision for row in rows] == [None, 1]
    assert rows[0].data["objective"] == first_payload["objective"]
    assert rows[0].work_links[0]["work_ref"]["record_id"] == "original"
    assert (await db_session.scalar(select(ContributionHead))).revision == 2

    duplicate = await test_client.post(path, json=first_payload, headers={"Idempotency-Key": "i19-duplicate"})
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "conflict"
    stale = await test_client.patch(f"{path}/{contribution_id}", json=updated_payload, headers={"Idempotency-Key": "i19-stale", "If-Match": "1"})
    assert stale.status_code == 412
    assert (await db_session.scalar(select(ContributionHead))).revision == 2
    assert len((await db_session.scalars(select(ContributionVersion))).all()) == 2


@pytest.mark.asyncio
async def test_contribution_does_not_infer_other_product_stories(test_client, db_session, initiative_actor):
    actor = initiative_actor
    first = await product(test_client, "First Work Product", db_session, actor)
    second = await product(test_client, "Second Work Product", db_session, actor)
    initiative_record = await initiative(test_client, actor)
    for name, product_record in (("first", first), ("second", second)):
        payload = contribution_data(actor, initiative_record["id"], product_record, name)
        payload["work_links"] = [link for link in payload["work_links"] if link["work_ref"]["record_type"] == "story"]
        result = await test_client.post(
            f"/v1/catalog/projects/{product_record['project_id']}/contributions", json=payload, headers={"Idempotency-Key": f"i19-{name}-work"}
        )
        assert result.status_code == 201, result.text
    for name, product_record in (("first", first), ("second", second)):
        result = await test_client.get(f"/v1/catalog/projects/{product_record['project_id']}/contributions")
        assert result.status_code == 200, result.text
        assert len(result.json()["items"]) == 1
        assert [link["work_ref"]["record_id"] for link in result.json()["items"][0]["data"]["work_links"]] == [f"{name}-story"]


@pytest.mark.asyncio
async def test_source_grants_are_rechecked_on_reads_and_missing_initiative_is_optional(test_client, db_session, initiative_actor):
    actor = initiative_actor
    no_initiative = await product(test_client, "Routine Work")
    project_id = no_initiative["project_id"]
    assert (await test_client.get(f"/v1/products/{no_initiative['product_id']}/projects/{project_id}")).status_code == 200
    empty = await test_client.get(f"/v1/catalog/projects/{project_id}/contributions")
    assert empty.status_code == 200 and empty.json() == {"items": []}
    assert (await test_client.get("/v1/catalog/initiatives")).json() == {"items": []}

    product_record = await product(test_client, "Authorized Work", db_session, actor)
    initiative_record = await initiative(test_client, actor)
    project_id = product_record["project_id"]
    path = f"/v1/catalog/projects/{project_id}/contributions"
    payload = contribution_data(actor, initiative_record["id"], product_record)
    created = await test_client.post(path, json=payload, headers={"Idempotency-Key": "i19-registered-source"})
    assert created.status_code == 201, created.text
    row_id = created.json()["id"]
    initial = await test_client.get(f"{path}/{row_id}")
    assert len(initial.json()["data"]["work_links"]) == 3
    project = await db_session.get(Project, (actor.org_id, project_id))
    project.configuration = {}
    await db_session.commit()
    revoked = await test_client.get(f"{path}/{row_id}")
    assert revoked.status_code == 200
    assert revoked.json()["data"]["work_links"] == []
    assert (await test_client.get(path)).json()["items"][0]["data"]["work_links"] == []
    denied = await test_client.patch(f"{path}/{row_id}", json=payload, headers={"If-Match": "1", "Idempotency-Key": "i19-revoked-source"})
    assert denied.status_code == 404 and denied.json()["code"] == "not_found"
    assert (await db_session.scalar(select(ContributionHead).where(ContributionHead.id == row_id))).revision == 1


@pytest.mark.asyncio
async def test_foreign_project_and_work_sources_are_denied_without_writes(test_client, db_session, initiative_actor):
    actor = initiative_actor
    product_record = await product(test_client, "Scoped Work", db_session, actor)
    initiative_record = await initiative(test_client, actor)
    project_id = product_record["project_id"]
    payload = contribution_data(actor, initiative_record["id"], product_record)
    path = f"/v1/catalog/projects/{project_id}/contributions"
    baseline = (
        len((await db_session.scalars(select(ContributionVersion))).all()),
        len((await db_session.scalars(select(CatalogReceipt))).all()),
        len((await db_session.scalars(select(CatalogChange))).all()),
    )
    foreign = {
        **payload,
        "work_links": [{**payload["work_links"][0], "work_ref": {**payload["work_links"][0]["work_ref"], "project_id": "foreign-project"}}],
    }
    denied = await test_client.post(path, json=foreign, headers={"Idempotency-Key": "i19-foreign-work"})
    assert denied.status_code == 404 and denied.json()["code"] == "not_found"
    unknown_source = {
        **payload,
        "work_links": [
            {**payload["work_links"][0], "work_ref": {**payload["work_links"][0]["work_ref"], "source_connection_id": "unregistered-tracker"}}
        ],
    }
    denied = await test_client.post(path, json=unknown_source, headers={"Idempotency-Key": "i19-foreign-source"})
    assert denied.status_code == 404 and denied.json()["code"] == "not_found"
    denied = await test_client.post(
        "/v1/catalog/projects/foreign-project/contributions", json=payload, headers={"Idempotency-Key": "i19-foreign-project"}
    )
    assert denied.status_code == 404 and denied.json()["code"] == "not_found"
    assert (
        len((await db_session.scalars(select(ContributionVersion))).all()),
        len((await db_session.scalars(select(CatalogReceipt))).all()),
        len((await db_session.scalars(select(CatalogChange))).all()),
    ) == baseline
    assert (await test_client.get("/v1/catalog/projects/foreign-project/contributions")).status_code == 404
    assert (await test_client.get("/v1/catalog/projects/absent/contributions")).json() == (
        await test_client.get("/v1/catalog/projects/foreign-project/contributions")
    ).json()


@pytest.mark.asyncio
async def test_business_links_do_not_grant_another_user_product_or_project_access(test_client, db_session, app, initiative_actor):
    actor = initiative_actor
    product_record = await product(test_client, "Restricted Work", db_session, actor)
    initiative_record = await initiative(test_client, actor)
    project_id = product_record["project_id"]
    path = f"/v1/catalog/projects/{project_id}/contributions"
    created = await test_client.post(
        path, json=contribution_data(actor, initiative_record["id"], product_record), headers={"Idempotency-Key": "i19-restricted-link"}
    )
    assert created.status_code == 201, created.text
    outsider = actor.model_copy(update={"user_id": "different-user", "team_id": "different-team"})
    db_session.add(User(id=outsider.user_id, org_id=actor.org_id, team_id=outsider.team_id, email="outsider@example.test", user_kind="human"))
    await db_session.flush()
    db_session.add(TenantMembership(user_id=outsider.user_id, tenant_id=actor.org_id, role="member", is_active=True))
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: outsider
    missing = await test_client.get("/v1/catalog/initiatives/not-present")
    invisible = await test_client.get(f"/v1/catalog/initiatives/{initiative_record['id']}")
    assert invisible.status_code == missing.status_code == 404
    assert invisible.json() == missing.json()
    assert (await test_client.get("/v1/catalog/initiatives")).json() == {"items": []}
    assert (await test_client.get(path)).status_code == 404
    assert (await test_client.get(f"{path}/{created.json()['id']}")).json() == (await test_client.get(f"{path}/not-present")).json()
    assert (await test_client.get(f"/v1/products/{product_record['product_id']}/projects/{project_id}")).status_code == 404


@pytest.mark.asyncio
async def test_revoked_source_invalidates_project_cursor(test_client, db_session, initiative_actor):
    actor = initiative_actor
    product_record = await product(test_client, "Cursor Scope Product", db_session, actor)
    project_id = product_record["project_id"]
    path = f"/v1/catalog/projects/{project_id}/contributions"
    for index in range(2):
        created_initiative = await test_client.post(
            "/v1/catalog/initiatives",
            json={"name": f"Objective {index}", "status": "planned"},
            headers={"Idempotency-Key": f"i19-cursor-initiative-{index}"},
        )
        assert created_initiative.status_code == 201, created_initiative.text
        created = await test_client.post(
            path,
            json=contribution_data(actor, created_initiative.json()["id"], product_record, f"cursor-{index}"),
            headers={"Idempotency-Key": f"i19-cursor-contribution-{index}"},
        )
        assert created.status_code == 201, created.text
    first = await test_client.get(path, params={"limit": 1})
    assert first.status_code == 200
    assert len(first.json()["items"]) == 1
    assert first.json()["next_cursor"]
    project = await db_session.get(Project, (actor.org_id, project_id))
    project.configuration = {}
    await db_session.commit()
    second = await test_client.get(path, params={"limit": 1, "cursor": first.json()["next_cursor"]})
    assert second.status_code == 400 and second.json()["code"] == "invalid_cursor"
    visible = await test_client.get(path)
    assert visible.status_code == 200 and len(visible.json()["items"]) == 2
    assert all(not item["data"]["work_links"] for item in visible.json()["items"])
    assert "total" not in visible.json()


@pytest.mark.asyncio
async def test_unresolved_native_work_fails_closed_and_hides_previously_linked_refs(test_client, db_session, initiative_actor, monkeypatch):
    from src.catalog import initiative_routes

    actor = initiative_actor
    product_record = await product(test_client, "Fail Closed Work", db_session, actor)
    initiative_record = await initiative(test_client, actor)
    project_id = product_record["project_id"]
    path = f"/v1/catalog/projects/{project_id}/contributions"
    payload = contribution_data(actor, initiative_record["id"], product_record)
    created = await test_client.post(path, json=payload, headers={"Idempotency-Key": "i19-verified-fixture"})
    assert created.status_code == 201, created.text
    assert len((await test_client.get(f"{path}/{created.json()['id']}")).json()["data"]["work_links"]) == 3

    async def no_resolver(caller, reference):
        return None

    monkeypatch.setattr(initiative_routes, "native_work_verified", no_resolver)
    current = await test_client.get(f"{path}/{created.json()['id']}")
    assert current.status_code == 200 and current.json()["data"]["work_links"] == []
    rejected = await test_client.patch(f"{path}/{created.json()['id']}", json=payload, headers={"If-Match": "1", "Idempotency-Key": "i19-unresolved"})
    assert rejected.status_code == 503 and rejected.json()["code"] == "dependency_unavailable"
    assert len((await db_session.scalars(select(ContributionVersion))).all()) == 1
    assert (await db_session.scalar(select(ContributionHead))).revision == 1
    assert len((await db_session.scalars(select(CatalogReceipt))).all()) == 2


@pytest.mark.asyncio
async def test_missing_or_inaccessible_work_id_rejected_before_writing(test_client, db_session, initiative_actor):
    actor = initiative_actor
    product_record = await product(test_client, "Missing Native Work", db_session, actor)
    initiative_record = await initiative(test_client, actor)
    project_id = product_record["project_id"]
    path = f"/v1/catalog/projects/{project_id}/contributions"
    baseline = (len((await db_session.scalars(select(CatalogReceipt))).all()), len((await db_session.scalars(select(CatalogChange))).all()))
    for kind in ("missing-work", "inaccessible-work"):
        payload = contribution_data(actor, initiative_record["id"], product_record)
        payload["work_links"][0]["work_ref"]["record_id"] = kind
        result = await test_client.post(path, json=payload, headers={"Idempotency-Key": f"i19-{kind}"})
        assert result.status_code == 404 and result.json()["code"] == "not_found"
    assert (len((await db_session.scalars(select(CatalogReceipt))).all()), len((await db_session.scalars(select(CatalogChange))).all())) == baseline
    assert (await db_session.scalars(select(ContributionVersion))).all() == []


@pytest.mark.asyncio
async def test_work_revisions_are_opaque_but_duplicate_native_identity_is_rejected(test_client, db_session, initiative_actor):
    actor = initiative_actor
    product_record = await product(test_client, "Native Revision Product", db_session, actor)
    initiative_record = await initiative(test_client, actor)
    path = f"/v1/catalog/projects/{product_record['project_id']}/contributions"
    payload = contribution_data(actor, initiative_record["id"], product_record)
    first = payload["work_links"][0]
    first["work_ref"]["record_revision"] = "etag:opaque-source-revision"
    payload["work_links"].append({**first, "work_ref": {**first["work_ref"], "record_revision": "etag:newer-revision"}})
    rejected = await test_client.post(path, json=payload, headers={"Idempotency-Key": "i19-overlap-revisions"})
    assert rejected.status_code == 422 and rejected.json()["code"] == "invalid_request"
    payload["work_links"].pop()
    first["work_ref"]["record_revision"] = "latest"
    unpinned = await test_client.post(path, json=payload, headers={"Idempotency-Key": "i19-latest-revision"})
    assert unpinned.status_code == 422 and unpinned.json()["code"] == "invalid_request"
    assert (await db_session.scalars(select(ContributionVersion))).all() == []
    assert (await db_session.scalars(select(CatalogReceipt).where(CatalogReceipt.operation.like("create_contribution:%")))).all() == []
