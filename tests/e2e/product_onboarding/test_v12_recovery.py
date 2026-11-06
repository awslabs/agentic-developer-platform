"""Offline V12 interruption and retry checks against real local catalog routes."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.e2e.product_onboarding.product_catalog_fixture import begin_membership_fixture, pinned_membership_scenario
from tests.e2e.product_onboarding.product_catalog_recovery import recover_membership_write
from tests.e2e.product_onboarding.product_catalog_release import pin_catalog_release
from tests.e2e.product_onboarding.product_catalog_recovery_scenario import begin_recovery_fixture, pinned_recovery_scenario
from tests.e2e.product_onboarding.test_product_catalog_release import catalog_release_fixture
from tests.e2e.product_onboarding.test_i18 import CATALOG, create_portfolio, create_product, record_ref

pytest_plugins = ("modules.gateway.tests.conftest", "tests.e2e.product_onboarding.test_i18")


class LocalState:
    def __init__(self, binding, document=None):
        self.binding = binding
        self.document = deepcopy(document) if document is not None else {"cases": {}}

    def save(self, value):
        self.document = deepcopy(value)


@pytest.mark.asyncio
async def test_lost_membership_response_retries_original_key_without_duplicate_or_lost_history(
    test_client, db_session, catalog_actor, monkeypatch,
):
    from src.catalog import routes
    from src.catalog.models import CatalogChange, CatalogReceipt, MembershipHead, MembershipVersion

    actor = catalog_actor
    product = await create_product(test_client, "Retry Product", "v12_recovery_product")
    profile_path = f"/v1/products/{product['product_id']}/projects/{product['project_id']}"
    profile = (await test_client.get(profile_path)).json()
    portfolio = await create_portfolio(test_client, actor, "Commerce", "v12_recovery_portfolio")
    now = datetime.now(UTC)
    valid_from, switch = now - timedelta(days=5), now - timedelta(days=3)
    first_recorded, revised_recorded = now - timedelta(days=2), now - timedelta(days=1)
    def wire(instant):
        return instant.isoformat().replace("+00:00", "Z")
    binding = {"run_id": "v12-recovery", "source_revision": "a" * 40,
               "tenant_id": actor.org_id, "actor_id": actor.user_id}
    plan = pinned_membership_scenario(binding["source_revision"], "b" * 40,
                                      binding["run_id"], wire(valid_from))
    state = LocalState(binding)
    begin_membership_fixture(state, plan, {"product_id": product["product_id"],
                                           "project_id": product["project_id"], "product_name": "Retry Product"})
    selection, store, observe, _, _, _ = catalog_release_fixture(state)
    release = pin_catalog_release(state, selection, store, observe)
    begin_recovery_fixture(state, pinned_recovery_scenario(state))
    portfolio_ref = record_ref(actor, product["project_id"], "adp.catalog", "portfolio", portfolio["id"])
    product_ref = record_ref(actor, product["project_id"], "adp.profile", "product", product["product_id"])
    payload = {"portfolio_ref": portfolio_ref, "product_ref": product_ref, "source_refs": [product_ref],
               "valid_from": plan["clock"]}
    path = f"{CATALOG}/{portfolio['id']}/memberships"
    committed = []
    keys = []

    async def write(key, body):
        keys.append(key)
        response = await test_client.post(path, json=body, headers={"Idempotency-Key": key})
        assert response.status_code == 201, response.text
        return response.json()

    async def lost_response(key, body):
        committed.append(await write(key, body))
        raise ConnectionError("response lost after catalog commit")

    async def readback():
        response = await test_client.get(path, params={"effective_at": wire(valid_from + timedelta(hours=1))})
        assert response.status_code == 200, response.text
        return response.json()

    monkeypatch.setattr(routes, "utcnow", lambda: first_recorded)
    with pytest.raises(ConnectionError, match="response lost"):
        await recover_membership_write(state, "Commerce", portfolio, payload, lost_response, readback, release)
    attempts = state.document["catalog"]["portfolios"][0]["recovery"]["attempts"]
    assert attempts == [{"state": "response_unknown", "observation_ref": release["observation_ref"]}]
    assert len((await db_session.scalars(select(MembershipVersion))).all()) == 1
    foreign = LocalState({**binding, "actor_id": "different-actor"}, state.document)
    with pytest.raises(ValueError, match="pinned scenario ownership"):
        await recover_membership_write(foreign, "Commerce", portfolio, payload, write, readback, release)
    assert len(keys) == 1
    resumed = LocalState(binding, state.document)
    new_selection, new_store, new_observe, _, _, _ = catalog_release_fixture(resumed)
    release = pin_catalog_release(resumed, new_selection, new_store, new_observe)
    recovered = await recover_membership_write(resumed, "Commerce", portfolio, payload, write, readback, release)
    assert recovered == committed[0]
    assert keys == [resumed.document["catalog"]["portfolios"][0]["keys"]["membership"]] * 2
    assert [attempt["state"] for attempt in resumed.document["catalog"]["portfolios"][0]["recovery"]["attempts"]] == [
        "response_unknown", "verified"]

    async def changed_receipt(key, body):
        assert key == keys[0] and body == payload
        return {**recovered, "id": "different-relationship"}

    with pytest.raises(AssertionError, match="original relationship receipt"):
        await recover_membership_write(resumed, "Commerce", portfolio, payload, changed_receipt, readback, release)
    assert [attempt["state"] for attempt in resumed.document["catalog"]["portfolios"][0]["recovery"]["attempts"]] == [
        "response_unknown", "verified", "receipt_mismatch"]
    async def catalog_counts():
        counts = []
        for model in (MembershipVersion, MembershipHead, CatalogChange, CatalogReceipt):
            counts.append(len((await db_session.scalars(select(model))).all()))
        return tuple(counts)

    counts = await catalog_counts()
    replay = await write(keys[0], payload)
    assert replay == recovered
    duplicate = await test_client.post(path, json=payload, headers={"Idempotency-Key": "new-key"})
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "conflict"
    assert await catalog_counts() == counts
    monkeypatch.setattr(routes, "utcnow", lambda: revised_recorded)
    before = await test_client.get(path, params={"effective_at": wire(valid_from + timedelta(hours=1)),
                                                 "decision_at": wire(first_recorded + timedelta(hours=1))})
    assert before.status_code == 200 and len(before.json()["items"]) == 1
    closed = await test_client.patch(path + "/" + recovered["id"], json={"valid_to": wire(switch)},
                                     headers={"Idempotency-Key": "v12_recovery_close", "If-Match": recovered["revision"]})
    assert closed.status_code == 200 and closed.json()["revision"] == "2", closed.text
    historical = await test_client.get(path, params={"effective_at": wire(valid_from + timedelta(hours=1)),
                                                     "decision_at": wire(first_recorded + timedelta(hours=1))})
    current = await test_client.get(path, params={"effective_at": wire(switch + timedelta(hours=1))})
    assert historical.status_code == 200 and historical.json() == before.json()
    assert current.status_code == 200 and current.json()["items"] == []
    assert [(row.relationship_id, row.relationship_revision) for row in
            (await db_session.scalars(select(MembershipVersion).order_by(MembershipVersion.relationship_revision))).all()] == [
        (recovered["id"], 1), (recovered["id"], 2)]
    assert (await test_client.get(profile_path)).json() == profile


@pytest.mark.asyncio
async def test_recovery_refuses_duplicate_readback_and_changed_original_intent():
    binding = {"run_id": "v12-recovery", "source_revision": "a" * 40,
               "tenant_id": "example-tenant", "actor_id": "example-editor"}
    plan = pinned_membership_scenario(binding["source_revision"], "b" * 40,
                                      binding["run_id"], "2026-10-01T00:00:00Z")
    product = {"product_id": "product", "project_id": "project", "product_name": "Example"}
    state = LocalState(binding)
    begin_membership_fixture(state, plan, product)
    selection, store, observe, _, _, _ = catalog_release_fixture(state)
    release = pin_catalog_release(state, selection, store, observe)
    begin_recovery_fixture(state, pinned_recovery_scenario(state))
    portfolio = {"id": "portfolio", "revision": "1", "change_id": "change"}
    product_ref = {"tenant_id": binding["tenant_id"], "project_id": product["project_id"],
                   "record_id": product["product_id"]}
    payload = {"product_ref": product_ref, "portfolio_ref": {"record_id": portfolio["id"]},
               "valid_from": plan["clock"]}
    receipt = {"id": "relationship", "revision": "1", "change_id": "change-2"}
    row = {"relationship_id": receipt["id"], "relationship_revision": receipt["revision"],
           "valid_from": plan["clock"], "portfolio_ref": {"record_id": portfolio["id"]},
           "product_ref": product_ref}

    async def write(key, body):
        assert key == state.document["catalog"]["portfolios"][0]["keys"]["membership"]
        assert body == payload
        return receipt

    async def duplicate():
        return {"items": [row, row]}

    missing_scenario = LocalState(binding, state.document)
    missing_scenario.document.pop("catalog_recovery")
    with pytest.raises(ValueError, match="pinned scenario ownership"):
        await recover_membership_write(missing_scenario, "Commerce", portfolio, payload, write, duplicate, release)
    assert "recovery" not in missing_scenario.document["catalog"]["portfolios"][0]
    missing = LocalState(binding, state.document)
    missing.document.pop("catalog_release")
    with pytest.raises(ValueError, match="release and schema receipt"):
        await recover_membership_write(missing, "Commerce", portfolio, payload, write, duplicate, None)
    assert "recovery" not in missing.document["catalog"]["portfolios"][0]
    with pytest.raises(ValueError, match="release and schema receipt"):
        await recover_membership_write(state, "Commerce", portfolio, payload, write, duplicate,
                                       {**release, "schema_head": "stale_head"})
    assert "recovery" not in state.document["catalog"]["portfolios"][0]

    with pytest.raises(AssertionError, match="duplicate"):
        await recover_membership_write(state, "Commerce", portfolio, payload, write, duplicate, release)
    assert [attempt["state"] for attempt in state.document["catalog"]["portfolios"][0]["recovery"]["attempts"]] == [
        "readback_failed"]
    for _ in range(2):
        with pytest.raises(AssertionError, match="duplicate"):
            await recover_membership_write(state, "Commerce", portfolio, payload, write, duplicate, release)
    with pytest.raises(ValueError, match="bounded attempt"):
        await recover_membership_write(state, "Commerce", portfolio, payload, write, duplicate, release)
    assert len(state.document["catalog"]["portfolios"][0]["recovery"]["attempts"]) == 3
    with pytest.raises(ValueError, match="durable write intent"):
        await recover_membership_write(state, "Commerce", portfolio,
                                       {**payload, "valid_to": "2026-10-02T00:00:00Z"}, write, duplicate, release)


@pytest.mark.asyncio
async def test_cleanup_only_retires_owned_catalog_links_and_keeps_peer_evidence(test_client, db_session, catalog_actor):
    from src.catalog.models import CatalogChange, CatalogReceipt, MembershipHead, MembershipVersion, PortfolioVersion
    from tests.e2e.product_onboarding.product_catalog_fixture import record_membership_cleanup, record_membership_receipts

    actor = catalog_actor
    owned = await create_product(test_client, "Owned Product", "v12_cleanup_product")
    peer = await create_product(test_client, "Existing Product", "v12_cleanup_peer")
    owned_portfolios = [
        await create_portfolio(test_client, actor, name, f"v12_cleanup_portfolio_{index}")
        for index, name in enumerate(("Commerce", "Shared services"))
    ]
    peer_portfolio = await create_portfolio(test_client, actor, "Existing Portfolio", "v12_cleanup_peer_portfolio")
    now = datetime.now(UTC)

    def wire(instant):
        return instant.isoformat().replace("+00:00", "Z")

    valid_from, valid_to = wire(now - timedelta(days=5)), wire(now - timedelta(days=1))
    binding = {"run_id": "v12-owned-cleanup", "source_revision": "a" * 40,
               "tenant_id": actor.org_id, "actor_id": actor.user_id}
    plan = pinned_membership_scenario(binding["source_revision"], "b" * 40, binding["run_id"], valid_from)
    state = LocalState(binding)
    begin_membership_fixture(state, plan, {"product_id": owned["product_id"],
                                           "project_id": owned["project_id"], "product_name": "Owned Product"})
    selection, store, observe, _, _, _ = catalog_release_fixture(state)
    release = pin_catalog_release(state, selection, store, observe)

    async def create_relationship(portfolio, product, key):
        project_id = product["project_id"]
        product_ref = record_ref(actor, project_id, "adp.profile", "product", product["product_id"])
        path = f"{CATALOG}/{portfolio['id']}/memberships"
        response = await test_client.post(path, json={
            "portfolio_ref": record_ref(actor, project_id, "adp.catalog", "portfolio", portfolio["id"]),
            "product_ref": product_ref, "source_refs": [product_ref], "valid_from": valid_from,
        }, headers={"Idempotency-Key": key})
        assert response.status_code == 201, response.text
        return response.json()

    owned_links = {}
    for entry, portfolio in zip(state.document["catalog"]["portfolios"], owned_portfolios):
        link = await create_relationship(portfolio, owned, entry["keys"]["membership"])
        record_membership_receipts(state, entry["name"], portfolio, link)
        owned_links[entry["name"]] = (portfolio, link)
    peer_link = await create_relationship(peer_portfolio, peer, "v12_cleanup_peer_link")
    peer_path = f"{CATALOG}/{peer_portfolio['id']}"
    peer_portfolio_before = (await test_client.get(peer_path)).json()
    peer_rows_before = (await test_client.get(peer_path + "/memberships")).json()
    peer_profile_path = f"/v1/products/{peer['product_id']}/projects/{peer['project_id']}"
    peer_profile_before = (await test_client.get(peer_profile_path)).json()
    peer_receipt = await db_session.scalar(select(CatalogReceipt).where(CatalogReceipt.idempotency_key == "v12_cleanup_peer_link"))
    assert peer_receipt is not None and peer_receipt.outcome["id"] == peer_link["id"]

    unowned_close = await test_client.patch(
        f"{CATALOG}/{owned_portfolios[0]['id']}/memberships/{peer_link['id']}",
        json={"valid_to": valid_to}, headers={"Idempotency-Key": "v12_cleanup_wrong_scope", "If-Match": "1"})
    assert unowned_close.status_code == 404
    assert (await test_client.get(peer_path + "/memberships")).json() == peer_rows_before
    with pytest.raises(ValueError, match="owned records"):
        record_membership_cleanup(state, {"Existing Portfolio": {
            "closed_revision": "2", "retired_revision": "2", "current_memberships": [], "portfolio_status": "retired"}})
    assert state.document["catalog"]["cleanup_pending"] is True

    observations = {}
    mutation_change_ids = set()
    for entry in state.document["catalog"]["portfolios"]:
        portfolio, link = owned_links[entry["name"]]
        path = f"{CATALOG}/{portfolio['id']}"
        closed = await test_client.patch(f"{path}/memberships/{link['id']}", json={"valid_to": valid_to},
            headers={"Idempotency-Key": entry["keys"]["close"], "If-Match": link["revision"]})
        assert closed.status_code == 200 and closed.json()["revision"] == "2", closed.text
        retired = await test_client.patch(path, json={
            "name": entry["name"], "lifecycle_status": "retired", "owner_ref": {"kind": "user", "id": actor.user_id},
        }, headers={"Idempotency-Key": entry["keys"]["retire"], "If-Match": portfolio["revision"]})
        assert retired.status_code == 200 and retired.json()["revision"] == "2", retired.text
        mutation_change_ids.update((closed.json()["change_id"], retired.json()["change_id"]))
        current = await test_client.get(path + "/memberships", params={"effective_at": wire(now)})
        observed_portfolio = await test_client.get(path)
        assert current.status_code == observed_portfolio.status_code == 200
        assert current.json()["items"] == [] and observed_portfolio.json()["data"]["lifecycle_status"] == "retired"
        observations[entry["name"]] = {"closed_revision": closed.json()["revision"],
                                       "retired_revision": retired.json()["revision"],
                                       "current_memberships": current.json()["items"],
                                       "portfolio_status": observed_portfolio.json()["data"]["lifecycle_status"]}
    assert record_membership_cleanup(state, observations) == observations
    assert state.document["catalog"]["cleanup_pending"] is False
    assert state.document["catalog_release"] == release
    assert (await test_client.get(peer_path)).json() == peer_portfolio_before
    assert (await test_client.get(peer_path + "/memberships")).json() == peer_rows_before
    assert (await test_client.get(peer_profile_path)).json() == peer_profile_before
    assert (await db_session.scalar(select(CatalogReceipt).where(CatalogReceipt.idempotency_key == "v12_cleanup_peer_link"))).outcome == peer_receipt.outcome
    assert len((await db_session.scalars(select(PortfolioVersion).where(PortfolioVersion.id == peer_portfolio["id"]))).all()) == 1
    assert len((await db_session.scalars(select(MembershipHead).where(MembershipHead.relationship_id == peer_link["id"]))).all()) == 1
    versions = (await db_session.scalars(select(MembershipVersion))).all()
    assert {(row.relationship_id, row.relationship_revision) for row in versions} == {
        (peer_link["id"], 1), *((link["id"], revision) for _, link in owned_links.values() for revision in (1, 2)),
    }
    receipt_ids = {entry["receipts"][kind]["change_id"] for entry in state.document["catalog"]["portfolios"]
                   for kind in ("portfolio", "relationship")}
    receipt_ids.update((peer_portfolio["change_id"], peer_link["change_id"]))
    receipt_ids.update(mutation_change_ids)
    retained = (await db_session.scalars(select(CatalogReceipt))).all()
    changes = (await db_session.scalars(select(CatalogChange))).all()
    assert len(receipt_ids) == len(retained) == len(changes) == 10
    assert {receipt.outcome["change_id"] for receipt in retained} == receipt_ids
    assert {change.id for change in changes} == receipt_ids
    assert record_membership_cleanup(state, observations) == observations
