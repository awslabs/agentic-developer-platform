"""Delivery-home profile changes follow the Product revision contract."""

from datetime import timedelta

import pytest
from sqlalchemy import inspect, select

from src.admin.connections.github_client import GitHubAppClient
from src.auth.dependencies import get_current_user
from src.products.models import Product, ProductChange
from src.products.routes import implied_delivery_home
from src.shared.models.base import utcnow
from src.shared.models.vault import ChannelTenantMap, CredentialServiceDelegation
from tests.products.test_i01 import REQUEST
from tests.products.test_i08_api import mutation, setup_source


@pytest.mark.asyncio
async def test_explicit_home_redacted_after_connection_revocation_in_reads_and_replays(test_client, db_session, source_member):
    bindings_path, selection = await setup_source(test_client, db_session, source_member)
    bound = await test_client.post(bindings_path, json=selection, headers=mutation(1, "revoked_home_binding_123"))
    assert bound.status_code == 201, bound.text
    profile_path = bindings_path.removesuffix("/bindings")
    home = {"provider": "github", "repository": "example/planning", "repository_id": 35}
    patch_headers = mutation(2, "revoked_home_patch_12345")
    saved = await test_client.patch(profile_path, json={"delivery_home_repository": home}, headers=patch_headers)
    assert saved.status_code == 200 and saved.json()["delivery_home_repository"] == home
    edited = await test_client.patch(profile_path, json={"purpose": "Another edit"}, headers=mutation(3, "revoked_home_other_12345"))
    assert edited.status_code == 200, edited.text
    connection = await db_session.get(ChannelTenantMap, selection["connection_ref"]["id"])
    connection.ownership_disputed = True
    await db_session.commit()
    bindings = await test_client.get(bindings_path)
    assert bindings.json()["items"][0]["status"] == "restricted"
    for result in (
        await test_client.get(profile_path),
        await test_client.get(f"{profile_path}/revisions/3"),
        await test_client.patch(profile_path, json={"delivery_home_repository": home}, headers=patch_headers),
    ):
        assert result.status_code == 200, result.text
        assert result.json()["delivery_home_repository"] is None
        assert result.json()["delivery_home_implied"] is False
        assert "example/planning" not in result.text
    product = await db_session.get(Product, (source_member.org_id, profile_path.split("/")[3]))
    await db_session.refresh(product)
    assert product.delivery_home_repository == home and product.revision == 4
    change = await db_session.scalar(select(ProductChange).where(ProductChange.product_id == product.id, ProductChange.revision == 3))
    assert change.snapshot["delivery_home_repository"] == home
    connection.ownership_disputed = False
    await db_session.commit()
    restored = await test_client.get(f"{profile_path}/revisions/3")
    assert restored.json()["delivery_home_repository"] == home


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize(
    ("source_access", "duplicate"),
    [("allowed", False), ("allowed", True), ("product_only", False), ("other_repository", False), ("other_revision", False)],
)
async def test_service_delivery_home_respects_source_grant(test_client, db_session, app, source_member, explicit, source_access, duplicate):
    bindings_path, selection = await setup_source(test_client, db_session, source_member)
    selection = {**selection, "source_revision": "a" * 40}
    bound = await test_client.post(bindings_path, json=selection, headers=mutation(1, "service_home_binding_123"))
    assert bound.status_code == 201, bound.text
    profile_path = bindings_path.removesuffix("/bindings")
    home = {"provider": "github", "repository": "example/planning", "repository_id": 35}
    if explicit:
        saved = await test_client.patch(profile_path, json={"delivery_home_repository": home}, headers=mutation(2, "service_home_patch_12345"))
        assert saved.status_code == 200, saved.text
    human = await test_client.get(profile_path)
    assert human.json()["delivery_home_repository"] == home
    if duplicate:
        linked = await test_client.post(
            f"/v1/products/{human.json()['product_id']}/projects",
            json={"mode": "create", "name": "Other Project"},
            headers=mutation(human.json()["revision"], "service_duplicate_project_123"),
        )
        assert linked.status_code == 201, linked.text
        duplicate_path = f"/v1/products/{human.json()['product_id']}/projects/{linked.json()['project_id']}/bindings"
        added = await test_client.post(duplicate_path, json=selection, headers=mutation(linked.json()["revision"], "service_duplicate_bind_12345"))
        assert added.status_code == 201, added.text
    source_ref = {
        "tenant_id": source_member.org_id,
        "project_id": human.json()["project_id"],
        "source_connection_id": selection["connection_ref"]["id"],
        "record_type": "repository_commit",
        "record_id": "0036" if source_access == "other_repository" else selection["native_id"],
        "record_revision": "b" * 40 if source_access == "other_revision" else selection["source_revision"],
    }
    grant = CredentialServiceDelegation(
        tenant_id=source_member.org_id,
        installation_id=selection["connection_ref"]["id"],
        service_id="registered-l5-worker",
        product_id=human.json()["product_id"],
        project_id=human.json()["project_id"],
        created_by=source_member.user_id,
        operations=["product:read"] if source_access == "product_only" else ["product:read", "source:read"],
        sources=[] if source_access == "product_only" else [source_ref],
        capabilities=[],
        visibility="restricted",
        expires_at=utcnow() + timedelta(hours=1),
    )
    db_session.add(grant)
    await db_session.commit()
    service = source_member.model_copy(
        update={
            "user_id": "iam-agent:registered-l5-worker",
            "agent_registry_id": "registered-l5-worker",
            "org_id": "platform-service",
            "account_type": "service",
            "auth_source": "iam",
            "scope": "internal",
        }
    )
    app.dependency_overrides[get_current_user] = lambda: service
    headers = {"X-L5-Delegation-ID": grant.id}
    assert (await test_client.get(profile_path)).status_code == 404
    bindings = await test_client.get(bindings_path, headers=headers)
    assert bindings.status_code == 200, bindings.text
    assert bool(bindings.json()["items"]) == (source_access == "allowed")
    paths = [profile_path]
    if explicit:
        paths.append(f"{profile_path}/revisions/3")
    for path in paths:
        result = await test_client.get(path, headers=headers)
        assert result.status_code == 200, result.text
        assert bool(result.json()["shared_profile"]["resources"]) == (source_access == "allowed")
        for resource in result.json()["shared_profile"]["resources"]:
            assert all(binding["project_id"] == human.json()["project_id"] for binding in resource["bindings"])
        assert result.json()["delivery_home_repository"] == (home if source_access == "allowed" else None)
        assert result.json()["delivery_home_implied"] == (not explicit and source_access == "allowed")


@pytest.mark.asyncio
async def test_source_unlink_keeps_home_supplied_by_another_project(test_client, db_session, source_member):
    bindings_path, selection = await setup_source(test_client, db_session, source_member)
    profile_path = bindings_path.removesuffix("/bindings")
    product_id = profile_path.split("/")[3]
    linked = await test_client.post(
        f"/v1/products/{product_id}/projects",
        json={"mode": "create", "name": "Delivery Project"},
        headers=mutation(1, "duplicate_home_project_123"),
    )
    assert linked.status_code == 201, linked.text
    linked_path = f"/v1/products/{product_id}/projects/{linked.json()['project_id']}"
    first = await test_client.post(bindings_path, json=selection, headers=mutation(2, "duplicate_home_base_12345"))
    assert first.status_code == 201, first.text
    second = await test_client.post(f"{linked_path}/bindings", json=selection, headers=mutation(3, "duplicate_home_linked_123"))
    assert second.status_code == 201, second.text
    other = await test_client.post(
        f"{linked_path}/bindings",
        json={**selection, "native_id": "0036"},
        headers=mutation(4, "duplicate_home_other_1234"),
    )
    assert other.status_code == 201, other.text
    home = {"provider": "github", "repository": "example/planning", "repository_id": 35}
    saved = await test_client.patch(profile_path, json={"delivery_home_repository": home}, headers=mutation(5, "duplicate_home_patch_1234"))
    assert saved.status_code == 200, saved.text
    removed = await test_client.delete(f"{bindings_path}/{first.json()['id']}", headers=mutation(6, "duplicate_home_unlink_123"))
    assert removed.status_code == 200, removed.text
    for path in (profile_path, linked_path):
        result = (await test_client.get(path)).json()
        assert result["delivery_home_repository"] == home and result["delivery_home_implied"] is False
        assert result["revision"] == 7 and len(result["product_provenance"]) == 1
    change = await db_session.scalar(select(ProductChange).where(ProductChange.product_id == product_id, ProductChange.revision == 7))
    assert change.changed_paths == ["/bindings"]
    removed_last = await test_client.delete(f"{linked_path}/bindings/{second.json()['id']}", headers=mutation(7, "duplicate_home_last_12345"))
    assert removed_last.status_code == 200, removed_last.text
    result = (await test_client.get(profile_path)).json()
    assert result["delivery_home_repository"]["repository_id"] == 36 and result["delivery_home_implied"] is True
    assert result["revision"] == 8 and len(result["product_provenance"]) == 2
    assert result["product_provenance"][-1]["reason"] == "source_unbound"


@pytest.fixture(autouse=True)
def github_repositories(monkeypatch):
    calls = []

    class GitHubFixture:
        def __init__(self, app_id, private_key):
            assert (app_id, private_key) == ("fixture-app", "fixture-key")

        async def get_installation_repository(self, installation_id, repository_id):
            calls.append((installation_id, repository_id))
            name = {35: "planning", 36: "remaining", 41: "foreign"}[repository_id]
            return {"id": repository_id, "owner": {"id": 27, "login": "example"}, "name": name, "full_name": f"example/{name}"}

        async def aclose(self):
            pass

    monkeypatch.setattr("src.admin.connections.service._get_github_app_credentials", lambda: ("fixture-app", "fixture-key"))
    monkeypatch.setattr("src.admin.connections.github_client.GitHubAppClient", GitHubFixture)
    return calls


@pytest.mark.asyncio
async def test_explicit_home_round_trip_with_two_bound_repositories(test_client, db_session, test_engine, source_member):
    async with test_engine.connect() as connection:
        columns = await connection.run_sync(lambda sync_connection: inspect(sync_connection).get_columns("products"))
        assert "delivery_home_repository" in {column["name"] for column in columns}

    bindings_path, selection = await setup_source(test_client, db_session, source_member)
    first = await test_client.post(bindings_path, json=selection, headers=mutation(1, "home_bind_first_12345"))
    assert first.status_code == 201, first.text
    second = await test_client.post(bindings_path, json={**selection, "native_id": "0036"}, headers=mutation(2, "home_bind_second_12345"))
    assert second.status_code == 201, second.text
    profile_path = bindings_path.removesuffix("/bindings")
    home = {"provider": "github", "repository": "example/planning", "repository_id": 35}
    updated = await test_client.patch(
        profile_path,
        json={"delivery_home_repository": home},
        headers=mutation(3, "home_profile_update_12345"),
    )
    assert updated.status_code == 200, updated.text
    assert updated.headers["Profile-Revision"] == "4"
    assert updated.json()["delivery_home_repository"] == home
    assert updated.json()["delivery_home_implied"] is False
    assert (await test_client.get(profile_path)).json()["delivery_home_repository"] == home
    product = await db_session.get(Product, (source_member.org_id, profile_path.split("/")[3]))
    assert product.delivery_home_repository == home
    assert len(product.provenance) == 1
    assert product.provenance[0]["path"] == "/product/delivery_home_repository"
    change = await db_session.scalar(select(ProductChange).where(ProductChange.product_id == product.id, ProductChange.revision == 4))
    assert change.changed_paths == ["/product/delivery_home_repository"]


@pytest.mark.asyncio
async def test_invalid_github_signing_key_keeps_profile_readable_and_rejects_home(test_client, db_session, source_member, monkeypatch):
    bindings_path, selection = await setup_source(test_client, db_session, source_member)
    bound = await test_client.post(bindings_path, json=selection, headers=mutation(1, "invalid_key_binding_12345"))
    assert bound.status_code == 201, bound.text
    profile_path = bindings_path.removesuffix("/bindings")
    monkeypatch.setattr("src.admin.connections.github_client.GitHubAppClient", GitHubAppClient)

    current = await test_client.get(profile_path)
    assert current.status_code == 200, current.text
    assert current.json()["delivery_home_repository"] is None
    assert current.json()["delivery_home_implied"] is False
    rejected = await test_client.patch(
        profile_path,
        json={"delivery_home_repository": {"provider": "github", "repository": "example/planning", "repository_id": 35}},
        headers=mutation(current.json()["revision"], "invalid_key_home_12345"),
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["detail"]["code"] == "delivery_home_not_bound"
    unchanged = await test_client.get(profile_path)
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["revision"] == current.json()["revision"]
    assert unchanged.json()["product_provenance"] == current.json()["product_provenance"]
    assert unchanged.json()["delivery_home_repository"] is None


@pytest.mark.asyncio
async def test_unbound_home_does_not_change_profile_or_history(test_client, db_session, source_member):
    bindings_path, selection = await setup_source(test_client, db_session, source_member)
    first = await test_client.post(bindings_path, json=selection, headers=mutation(1, "unbound_first_binding_123"))
    assert first.status_code == 201, first.text
    second = await test_client.post(bindings_path, json={**selection, "native_id": "0036"}, headers=mutation(2, "unbound_second_binding_123"))
    assert second.status_code == 201, second.text
    profile_path = bindings_path.removesuffix("/bindings")
    before = (await test_client.get(profile_path)).json()
    invalid = await test_client.patch(
        profile_path,
        json={"delivery_home_repository": {"provider": "github", "repository": "example/unbound", "repository_id": 99}},
        headers=mutation(3, "unbound_home_patch_12345"),
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "delivery_home_not_bound"
    assert (await test_client.get(profile_path)).json() == before
    product_id = profile_path.split("/")[3]
    product = await db_session.get(Product, (source_member.org_id, product_id))
    assert product.provenance == []
    assert await db_session.scalar(select(ProductChange).where(ProductChange.product_id == product_id, ProductChange.revision == 4)) is None


@pytest.mark.asyncio
async def test_other_product_binding_cannot_authorize_home(test_client, db_session, source_member):
    bindings_path, selection = await setup_source(test_client, db_session, source_member)
    own = await test_client.post(bindings_path, json=selection, headers=mutation(1, "own_first_binding_123456"))
    assert own.status_code == 201, own.text
    second = await test_client.post(
        "/v1/products", json={**REQUEST, "name": "Other Product"}, headers={"Idempotency-Key": "other_home_product_12345"}
    )
    assert second.status_code == 201, second.text
    other_bindings = f"/v1/products/{second.json()['product_id']}/projects/{second.json()['project_id']}/bindings"
    foreign = await test_client.post(other_bindings, json={**selection, "native_id": "0041"}, headers=mutation(1, "other_home_binding_12345"))
    assert foreign.status_code == 201, foreign.text
    profile_path = bindings_path.removesuffix("/bindings")
    before = (await test_client.get(profile_path)).json()
    invalid = await test_client.patch(
        profile_path,
        json={"delivery_home_repository": {"provider": "github", "repository": "example/other", "repository_id": 41}},
        headers=mutation(2, "other_home_patch_12345"),
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "delivery_home_not_bound"
    assert (await test_client.get(profile_path)).json() == before


@pytest.mark.asyncio
async def test_two_source_home_journey_rejects_foreign_identity_then_implies_remaining(
    test_client,
    db_session,
    source_member,
    github_repositories,
):
    bindings_path, selection = await setup_source(test_client, db_session, source_member)
    first = await test_client.post(bindings_path, json=selection, headers=mutation(1, "journey_first_binding_1234"))
    assert first.status_code == 201, first.text
    second = await test_client.post(bindings_path, json={**selection, "native_id": "0036"}, headers=mutation(2, "journey_second_binding_1234"))
    assert second.status_code == 201, second.text
    other = await test_client.post(
        "/v1/products", json={**REQUEST, "name": "Another Product"}, headers={"Idempotency-Key": "journey_other_product_1234"}
    )
    assert other.status_code == 201, other.text
    foreign_path = f"/v1/products/{other.json()['product_id']}/projects/{other.json()['project_id']}/bindings"
    foreign = await test_client.post(foreign_path, json={**selection, "native_id": "0041"}, headers=mutation(1, "journey_foreign_binding_1234"))
    assert foreign.status_code == 201, foreign.text

    profile_path = bindings_path.removesuffix("/bindings")
    before = (await test_client.get(profile_path)).json()
    for home, key in (
        ({"provider": "github", "repository": "example/foreign", "repository_id": 41}, "journey_foreign_home_1234"),
        ({"provider": "github", "repository": "example/foreign", "repository_id": 35}, "journey_spoofed_home_1234"),
    ):
        denied = await test_client.patch(profile_path, json={"delivery_home_repository": home}, headers=mutation(3, key))
        assert denied.status_code == 422 and denied.json()["detail"]["code"] == "delivery_home_not_bound"
    assert (await test_client.get(profile_path)).json() == before
    product = await db_session.get(Product, (source_member.org_id, before["product_id"]))
    assert product.provenance == []
    assert await db_session.scalar(select(ProductChange).where(ProductChange.product_id == product.id, ProductChange.revision == 4)) is None

    home = {"provider": "github", "repository": "example/planning", "repository_id": 35}
    saved = await test_client.patch(profile_path, json={"delivery_home_repository": home}, headers=mutation(3, "journey_valid_home_1234"))
    assert saved.status_code == 200 and saved.json()["delivery_home_repository"] == home
    assert saved.json()["delivery_home_implied"] is False
    removed = await test_client.delete(f"{bindings_path}/{first.json()['id']}", headers=mutation(4, "journey_unbind_home_1234"))
    assert removed.status_code == 200 and removed.headers["Profile-Revision"] == "5"
    profile = (await test_client.get(profile_path)).json()
    assert profile["delivery_home_repository"] == {"provider": "github", "repository": "example/remaining", "repository_id": 36}
    assert profile["delivery_home_implied"] is True and profile["revision"] == 5
    await db_session.refresh(product)
    assert product.delivery_home_repository is None
    assert [entry["path"] for entry in product.provenance] == ["/product/delivery_home_repository"] * 2
    assert product.provenance[-1]["reason"] == "source_unbound"
    change = await db_session.scalar(select(ProductChange).where(ProductChange.product_id == product.id, ProductChange.revision == 5))
    assert change.changed_paths == ["/bindings", "/product/delivery_home_repository"]
    assert github_repositories == [(17, 35), (17, 35), (17, 36)]


@pytest.mark.asyncio
async def test_unlink_last_home_clears_it_and_rejects_stale_home(test_client, db_session, source_member):
    bindings_path, selection = await setup_source(test_client, db_session, source_member)
    bound = await test_client.post(bindings_path, json=selection, headers=mutation(1, "unlink_home_binding_1234"))
    assert bound.status_code == 201, bound.text
    profile_path = bindings_path.removesuffix("/bindings")
    home = {"provider": "github", "repository": "example/planning", "repository_id": 35}
    set_home = await test_client.patch(profile_path, json={"delivery_home_repository": home}, headers=mutation(2, "unlink_home_patch_12345"))
    assert set_home.status_code == 200, set_home.text
    removed = await test_client.delete(f"{bindings_path}/{bound.json()['id']}", headers=mutation(3, "unlink_home_source_12345"))
    assert removed.status_code == 200, removed.text
    assert removed.headers["Profile-Revision"] == "4"
    profile = (await test_client.get(profile_path)).json()
    assert profile["delivery_home_repository"] is None
    assert profile["delivery_home_implied"] is False
    assert profile["revision"] == 4
    assert len(profile["product_provenance"]) == 2
    assert profile["product_provenance"][-1]["reason"] == "source_unbound"
    product_id = profile["product_id"]
    change = await db_session.scalar(select(ProductChange).where(ProductChange.product_id == product_id, ProductChange.revision == 4))
    assert change.changed_paths == ["/bindings", "/product/delivery_home_repository"]
    stale = await test_client.patch(profile_path, json={"delivery_home_repository": home}, headers=mutation(4, "stale_home_rebind_12345"))
    assert stale.status_code == 422 and stale.json()["detail"]["code"] == "delivery_home_not_bound"
    replay = await test_client.delete(f"{bindings_path}/{bound.json()['id']}", headers=mutation(3, "unlink_home_source_12345"))
    assert replay.status_code == 200
    assert (await test_client.get(profile_path)).json()["revision"] == 4


@pytest.mark.asyncio
async def test_unlink_home_implies_only_remaining_repository(test_client, db_session, source_member, monkeypatch):
    bindings_path, selection = await setup_source(test_client, db_session, source_member)
    first = await test_client.post(bindings_path, json=selection, headers=mutation(1, "remaining_first_bind_12345"))
    assert first.status_code == 201, first.text
    second = await test_client.post(bindings_path, json={**selection, "native_id": "0036"}, headers=mutation(2, "remaining_second_bind_12345"))
    assert second.status_code == 201, second.text
    profile_path = bindings_path.removesuffix("/bindings")
    explicit = {"provider": "github", "repository": "example/planning", "repository_id": 35}
    saved = await test_client.patch(profile_path, json={"delivery_home_repository": explicit}, headers=mutation(3, "remaining_home_patch_12345"))
    assert saved.status_code == 200, saved.text

    calls = []

    class GitHubFixture:
        def __init__(self, app_id, private_key):
            assert (app_id, private_key) == ("fixture-app", "fixture-key")

        async def get_installation_repository(self, installation_id, repository_id):
            calls.append((installation_id, repository_id))
            return {"id": 36, "owner": {"id": 27, "login": "example"}, "name": "remaining", "full_name": "example/remaining"}

        async def aclose(self):
            pass

    monkeypatch.setattr("src.admin.connections.service._get_github_app_credentials", lambda: ("fixture-app", "fixture-key"))
    monkeypatch.setattr("src.admin.connections.github_client.GitHubAppClient", GitHubFixture)
    removed = await test_client.delete(f"{bindings_path}/{first.json()['id']}", headers=mutation(4, "remaining_unlink_home_12345"))
    assert removed.status_code == 200, removed.text
    result = (await test_client.get(profile_path)).json()
    assert result["revision"] == 5
    assert result["delivery_home_repository"] == {"provider": "github", "repository": "example/remaining", "repository_id": 36}
    assert result["delivery_home_implied"] is True
    assert result["product_provenance"][-1]["reason"] == "source_unbound"
    product = await db_session.get(Product, (source_member.org_id, result["product_id"]))
    assert product.delivery_home_repository is None
    assert calls == [(17, 36)]
    event = await db_session.scalar(select(ProductChange).where(ProductChange.product_id == product.id, ProductChange.revision == 5))
    assert event.changed_paths == ["/bindings", "/product/delivery_home_repository"]


@pytest.mark.asyncio
async def test_single_bound_repository_is_implied_without_storing_home(test_client, db_session, source_member, monkeypatch):
    bindings_path, selection = await setup_source(test_client, db_session, source_member)
    profile_path = bindings_path.removesuffix("/bindings")
    product = await db_session.get(Product, (source_member.org_id, profile_path.split("/")[3]))
    calls = []

    class RepositoryClient:
        def __init__(self, app_id, private_key):
            assert (app_id, private_key) == ("fixture-app", "fixture-key")

        async def get_installation_repository(self, installation_id, repository_id):
            calls.append((installation_id, repository_id))
            return {"id": 35, "owner": {"id": 27, "login": "example"}, "name": "planning", "full_name": "example/planning"}

        async def aclose(self):
            pass

    monkeypatch.setattr("src.admin.connections.service._get_github_app_credentials", lambda: ("fixture-app", "fixture-key"))
    monkeypatch.setattr("src.admin.connections.github_client.GitHubAppClient", RepositoryClient)
    assert await implied_delivery_home(db_session, source_member, product) is None
    created = await test_client.post(bindings_path, json=selection, headers=mutation(1, "sole_home_binding_12345"))
    assert created.status_code == 201, created.text
    expected = {"provider": "github", "repository": "example/planning", "repository_id": 35}
    assert await implied_delivery_home(db_session, source_member, product) == expected
    profile = (await test_client.get(profile_path)).json()
    assert profile["delivery_home_repository"] == expected
    assert profile["delivery_home_implied"] is True
    product = await db_session.get(Product, (source_member.org_id, profile["product_id"]))
    assert product.delivery_home_repository is None and product.provenance == []
    assert calls == [(17, 35), (17, 35)]
    second = await test_client.post(bindings_path, json={**selection, "native_id": "0036"}, headers=mutation(2, "sole_home_second_12345"))
    assert second.status_code == 201, second.text
    assert await implied_delivery_home(db_session, source_member, product) is None
    profile = (await test_client.get(profile_path)).json()
    assert profile["delivery_home_repository"] is None
    assert profile["delivery_home_implied"] is False
    assert calls == [(17, 35), (17, 35)]


@pytest.mark.asyncio
async def test_clearing_home_projects_sole_source_on_read_and_current_replay(test_client, db_session, source_member, monkeypatch):
    bindings_path, selection = await setup_source(test_client, db_session, source_member)
    bound = await test_client.post(bindings_path, json=selection, headers=mutation(1, "projected_home_source_1234"))
    assert bound.status_code == 201, bound.text
    profile_path = bindings_path.removesuffix("/bindings")
    product = await db_session.get(Product, (source_member.org_id, profile_path.split("/")[3]))
    home = {"provider": "github", "repository": "example/planning", "repository_id": 35}
    assert await implied_delivery_home(db_session, source_member, product) == home

    set_home = await test_client.patch(profile_path, json={"delivery_home_repository": home}, headers=mutation(2, "projected_home_set_1234"))
    assert set_home.status_code == 200 and set_home.json()["delivery_home_implied"] is False
    cleared = await test_client.patch(profile_path, json={"delivery_home_repository": None}, headers=mutation(3, "projected_home_clear_1234"))
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["delivery_home_repository"] == home
    assert cleared.json()["delivery_home_implied"] is True
    assert cleared.json()["revision"] == 4
    current = (await test_client.get(profile_path)).json()
    assert current["delivery_home_repository"] == home and current["delivery_home_implied"] is True
    replay = await test_client.patch(profile_path, json={"delivery_home_repository": None}, headers=mutation(3, "projected_home_clear_1234"))
    assert replay.status_code == 200 and replay.json()["delivery_home_repository"] == home
    assert replay.json()["delivery_home_implied"] is True
    historical = await test_client.patch(profile_path, json={"delivery_home_repository": home}, headers=mutation(2, "projected_home_set_1234"))
    assert historical.status_code == 200 and historical.json()["delivery_home_implied"] is False
    assert historical.json()["revision"] == 3
    product = await db_session.get(Product, (source_member.org_id, current["product_id"]))
    await db_session.refresh(product)
    assert product.delivery_home_repository is None
    assert len(product.provenance) == 2

    monkeypatch.setattr("src.admin.connections.service._get_github_app_credentials", lambda: (None, None))
    unavailable = (await test_client.get(profile_path)).json()
    assert unavailable["delivery_home_repository"] is None and unavailable["delivery_home_implied"] is False
    assert await implied_delivery_home(db_session, source_member, product) is None
    assert product.delivery_home_repository is None


@pytest.mark.asyncio
async def test_stale_home_edit_does_not_overwrite_concurrent_profile_edit(test_client, db_session, source_member):
    bindings_path, selection = await setup_source(test_client, db_session, source_member)
    first = await test_client.post(bindings_path, json=selection, headers=mutation(1, "race_home_binding_12345"))
    assert first.status_code == 201, first.text
    second = await test_client.post(bindings_path, json={**selection, "native_id": "0036"}, headers=mutation(2, "race_home_second_12345"))
    assert second.status_code == 201, second.text
    profile_path = bindings_path.removesuffix("/bindings")
    renamed = await test_client.patch(profile_path, json={"purpose": "Concurrent purpose"}, headers=mutation(3, "race_home_purpose_12345"))
    assert renamed.status_code == 200, renamed.text
    home = {"provider": "github", "repository": "example/planning", "repository_id": 35}
    stale = await test_client.patch(profile_path, json={"delivery_home_repository": home}, headers=mutation(3, "race_home_stale_123456"))
    assert stale.status_code == 412
    assert stale.json()["detail"]["code"] == "stale_revision"
    current = (await test_client.get(profile_path)).json()
    assert current["product"]["purpose"] == "Concurrent purpose"
    assert current["delivery_home_repository"] is None
    saved = await test_client.patch(profile_path, json={"delivery_home_repository": home}, headers=mutation(4, "race_home_retry_123456"))
    assert saved.status_code == 200, saved.text
    assert saved.json()["product"]["purpose"] == "Concurrent purpose"
    assert saved.json()["delivery_home_repository"] == home
    assert saved.json()["revision"] == 5


@pytest.mark.asyncio
async def test_cross_project_home_patch_and_project_unlink(test_client, db_session, source_member):
    bindings_path, selection = await setup_source(test_client, db_session, source_member)
    profile_path = bindings_path.removesuffix("/bindings")
    product_id = profile_path.split("/")[3]
    linked = await test_client.post(
        f"/v1/products/{product_id}/projects",
        json={"mode": "create", "name": "Delivery Project"},
        headers=mutation(1, "cross_project_create_123"),
    )
    assert linked.status_code == 201, linked.text
    linked_path = f"/v1/products/{product_id}/projects/{linked.json()['project_id']}"
    linked_source = await test_client.post(
        f"{linked_path}/bindings",
        json=selection,
        headers=mutation(2, "cross_project_source_123"),
    )
    assert linked_source.status_code == 201, linked_source.text
    base_source = await test_client.post(
        bindings_path,
        json={**selection, "native_id": "0036"},
        headers=mutation(3, "cross_project_base_12345"),
    )
    assert base_source.status_code == 201, base_source.text
    other = await test_client.post(
        "/v1/products",
        json={**REQUEST, "name": "Other Product"},
        headers={"Idempotency-Key": "cross_project_other_123"},
    )
    assert other.status_code == 201, other.text
    other_path = f"/v1/products/{other.json()['product_id']}/projects/{other.json()['project_id']}/bindings"
    foreign = await test_client.post(other_path, json={**selection, "native_id": "0041"}, headers=mutation(1, "cross_project_foreign_12"))
    assert foreign.status_code == 201, foreign.text
    denied = await test_client.patch(
        profile_path,
        json={"delivery_home_repository": {"provider": "github", "repository": "example/foreign", "repository_id": 41}},
        headers=mutation(4, "cross_project_denied_123"),
    )
    assert denied.status_code == 422 and denied.json()["detail"]["code"] == "delivery_home_not_bound"

    home = {"provider": "github", "repository": "example/planning", "repository_id": 35}
    saved = await test_client.patch(profile_path, json={"delivery_home_repository": home}, headers=mutation(4, "cross_project_home_12345"))
    assert saved.status_code == 200, saved.text
    assert saved.json()["delivery_home_repository"] == home and saved.json()["delivery_home_implied"] is False
    assert (await test_client.get(linked_path)).json()["delivery_home_repository"] == home
    removed = await test_client.delete(linked_path, headers=mutation(5, "cross_project_unlink_123"))
    assert removed.status_code == 200, removed.text
    assert removed.json()["revision"] == 6
    assert removed.json()["delivery_home_repository"] == {
        "provider": "github",
        "repository": "example/remaining",
        "repository_id": 36,
    }
    assert removed.json()["delivery_home_implied"] is True
    product = await db_session.get(Product, (source_member.org_id, product_id))
    await db_session.refresh(product)
    assert product.delivery_home_repository is None
    assert [item["revision"] for item in product.provenance] == [5, 6]
    assert [item["path"] for item in product.provenance] == ["/product/delivery_home_repository"] * 2
    change = await db_session.scalar(select(ProductChange).where(ProductChange.product_id == product_id, ProductChange.revision == 6))
    assert change.changed_paths == ["/project/binding", "/product/delivery_home_repository"]
    assert change.snapshot["delivery_home_repository"] is None
    replay = await test_client.delete(linked_path, headers=mutation(5, "cross_project_unlink_123"))
    assert replay.status_code == 200 and replay.json() == removed.json()
    stale = await test_client.patch(profile_path, json={"delivery_home_repository": home}, headers=mutation(6, "cross_project_stale_123"))
    assert stale.status_code == 422 and stale.json()["detail"]["code"] == "delivery_home_not_bound"


@pytest.mark.asyncio
async def test_project_unlink_keeps_home_when_repository_still_linked(test_client, db_session, source_member):
    bindings_path, selection = await setup_source(test_client, db_session, source_member)
    profile_path = bindings_path.removesuffix("/bindings")
    product_id = profile_path.split("/")[3]
    linked = await test_client.post(
        f"/v1/products/{product_id}/projects",
        json={"mode": "create", "name": "Delivery Project"},
        headers=mutation(1, "shared_project_create_123"),
    )
    assert linked.status_code == 201, linked.text
    linked_path = f"/v1/products/{product_id}/projects/{linked.json()['project_id']}"
    for path, revision, key in ((bindings_path, 2, "shared_base_source_12345"), (f"{linked_path}/bindings", 3, "shared_linked_source_123")):
        added = await test_client.post(path, json=selection, headers=mutation(revision, key))
        assert added.status_code == 201, added.text
    home = {"provider": "github", "repository": "example/planning", "repository_id": 35}
    saved = await test_client.patch(profile_path, json={"delivery_home_repository": home}, headers=mutation(4, "shared_home_patch_12345"))
    assert saved.status_code == 200, saved.text
    removed = await test_client.delete(linked_path, headers=mutation(5, "shared_project_unlink_12"))
    assert removed.status_code == 200, removed.text
    assert removed.json()["delivery_home_repository"] == home
    assert removed.json()["delivery_home_implied"] is False
    product = await db_session.get(Product, (source_member.org_id, product_id))
    await db_session.refresh(product)
    assert product.delivery_home_repository == home and len(product.provenance) == 1
    change = await db_session.scalar(select(ProductChange).where(ProductChange.product_id == product_id, ProductChange.revision == 6))
    assert change.changed_paths == ["/project/binding"]


@pytest.mark.asyncio
async def test_sole_source_in_linked_project_implies_home_on_each_project(test_client, db_session, source_member):
    bindings_path, selection = await setup_source(test_client, db_session, source_member)
    base_path = bindings_path.removesuffix("/bindings")
    product_id = base_path.split("/")[3]
    linked = await test_client.post(
        f"/v1/products/{product_id}/projects",
        json={"mode": "create", "name": "Delivery Project"},
        headers=mutation(1, "implied_linked_create_123"),
    )
    assert linked.status_code == 201, linked.text
    linked_path = f"/v1/products/{product_id}/projects/{linked.json()['project_id']}"
    bound = await test_client.post(
        f"{linked_path}/bindings",
        json=selection,
        headers=mutation(2, "implied_linked_source_123"),
    )
    assert bound.status_code == 201, bound.text
    home = {"provider": "github", "repository": "example/planning", "repository_id": 35}
    for path in (base_path, linked_path):
        profile = (await test_client.get(path)).json()
        assert profile["delivery_home_repository"] == home
        assert profile["delivery_home_implied"] is True
        assert profile["revision"] == 3
    product = await db_session.get(Product, (source_member.org_id, product_id))
    assert product.delivery_home_repository is None and product.provenance == []


@pytest.mark.asyncio
async def test_two_repositories_in_different_projects_do_not_imply_home(test_client, db_session, source_member, github_repositories):
    bindings_path, selection = await setup_source(test_client, db_session, source_member)
    base_path = bindings_path.removesuffix("/bindings")
    product_id = base_path.split("/")[3]
    linked = await test_client.post(
        f"/v1/products/{product_id}/projects",
        json={"mode": "create", "name": "Delivery Project"},
        headers=mutation(1, "implied_two_create_12345"),
    )
    assert linked.status_code == 201, linked.text
    linked_path = f"/v1/products/{product_id}/projects/{linked.json()['project_id']}"
    for path, source, revision, key in (
        (bindings_path, selection, 2, "implied_two_base_12345"),
        (f"{linked_path}/bindings", {**selection, "native_id": "0036"}, 3, "implied_two_linked_1234"),
    ):
        added = await test_client.post(path, json=source, headers=mutation(revision, key))
        assert added.status_code == 201, added.text
    calls_before = len(github_repositories)
    for path in (base_path, linked_path):
        profile = (await test_client.get(path)).json()
        assert profile["delivery_home_repository"] is None
        assert profile["delivery_home_implied"] is False
        assert profile["revision"] == 4
    assert len(github_repositories) == calls_before
    removed = await test_client.delete(
        f"{linked_path}/bindings/{added.json()['id']}",
        headers=mutation(4, "implied_two_unlink_12345"),
    )
    assert removed.status_code == 200, removed.text
    duplicate = await test_client.post(
        f"{linked_path}/bindings",
        json=selection,
        headers=mutation(5, "implied_duplicate_12345"),
    )
    assert duplicate.status_code == 201, duplicate.text
    profile = (await test_client.get(base_path)).json()
    assert profile["delivery_home_repository"] == {"provider": "github", "repository": "example/planning", "repository_id": 35}
    assert profile["delivery_home_implied"] is True
    assert profile["revision"] == 6
