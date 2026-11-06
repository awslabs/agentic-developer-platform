"""Shared Profile scope, durable completion, authorization and mutation contracts."""

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from src.auth.dependencies import get_current_user
from src.products.models import Product, ProductSourceBinding
from src.products.shared_profile import apply_patch, completion, environments, resource_id
from src.shared.database_agent_context import get_agent_context_db
from src.shared.models.vault import ChannelTenantMap
from tests.products.test_i08_api import mutation, setup_source


@pytest.fixture
async def knowledge_store(app, db_session):
    await db_session.execute(text("CREATE TABLE knowledge_assets (tenant_id TEXT, product_id TEXT, status TEXT)"))
    await db_session.commit()

    async def session():
        yield db_session

    app.dependency_overrides[get_agent_context_db] = session


@pytest.mark.asyncio
async def test_shared_profile_across_projects_replay_conflict_and_redaction(test_client, db_session, app, source_member, knowledge_store):
    path, selection = await setup_source(test_client, db_session, source_member)
    product_id = path.split("/")[3]
    shared = f"/v1/products/{product_id}/profile"
    bound = await test_client.post(path, json={**selection, "display_name": "example/product"}, headers=mutation(1, "shared_profile_bind_1234"))
    assert bound.status_code == 201, bound.text
    initial = (await test_client.get(shared)).json()
    rid = initial["resources"][0]["id"]
    assert initial["completion"]["completed"] == 2 and initial["completion"]["applicable"] == 5
    assert initial["resources"][0]["label"] == "example/product"
    patch = {
        "profile_configuration": {
            "repositories": {rid: {"purpose": "product_code", "label": "Main code"}},
            "default_repository": rid,
            "not_applicable": ["environments"],
        }
    }
    headers = mutation(2, "shared_profile_patch_1234")
    saved = await test_client.patch(shared, json=patch, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["shared_profile"]["resources"][0]["default"] is True
    linked = await test_client.post(
        f"/v1/products/{product_id}/projects", json={"mode": "create", "name": "Launch"}, headers=mutation(3, "shared_profile_project_1234")
    )
    assert linked.status_code == 201, linked.text
    other = f"/v1/products/{product_id}/projects/{linked.json()['project_id']}"
    duplicate = await test_client.post(f"{other}/bindings", json=selection, headers=mutation(4, "shared_profile_duplicate_1234"))
    assert duplicate.status_code == 201, duplicate.text
    for scope in (path.removesuffix("/bindings"), other):
        profile = (await test_client.get(scope)).json()
        assert "profile_configuration" not in profile
        assert profile["shared_profile"]["resources"][0]["label"] == "Main code"
        assert len(profile["shared_profile"]["resources"]) == 1
        assert len(profile["shared_profile"]["resources"][0]["bindings"]) == 2
        assert all(binding["relationship_revision"] >= 1 for binding in profile["shared_profile"]["resources"][0]["bindings"])
    assert (await test_client.patch(shared, json=patch, headers=headers)).json()["revision"] == 3
    assert (await test_client.patch(shared, json={"purpose": "stale"}, headers=mutation(3, "shared_profile_stale_1234"))).status_code == 412
    assert (await test_client.patch(shared, json={"purpose": "different"}, headers=headers)).status_code == 409
    prior = (await test_client.get(shared)).json()["completion"]
    connection = await db_session.get(ChannelTenantMap, selection["connection_ref"]["id"])
    connection.ownership_disputed = True
    await db_session.commit()
    redacted = (await test_client.get(shared)).json()
    assert redacted["resources"] == []
    assert redacted["completion"] == prior
    assert "Main code" not in str(redacted)
    replay = await test_client.patch(shared, json=patch, headers=headers)
    assert replay.json()["shared_profile"]["resources"] == []
    stranger = source_member.model_copy(update={"user_id": "other-user", "org_id": "other-tenant"})
    app.dependency_overrides[get_current_user] = lambda: stranger
    assert (await test_client.get(shared)).status_code == 404
    assert (await test_client.patch(shared, json=patch, headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_product_knowledge_counts_only_saved_product_sources(test_client, db_session, source_member, knowledge_store):
    path, _ = await setup_source(test_client, db_session, source_member)
    product_id = path.split("/")[3]
    for pid, status in [(product_id, "failed"), (product_id, "removed"), ("another-product", "ready"), (None, "ready")]:
        await db_session.execute(
            text("INSERT INTO knowledge_assets VALUES (:tenant, :product, :status)"),
            {"tenant": source_member.org_id, "product": pid, "status": status},
        )
    await db_session.commit()
    result = (await test_client.get(f"/v1/products/{product_id}/profile")).json()
    assert result["knowledge_count"] == 1
    assert next(s for s in result["completion"]["sections"] if s["id"] == "knowledge")["status"] == "complete"


@pytest.mark.asyncio
async def test_named_environments_share_account_and_defaults_require_code(monkeypatch):
    product = Product(name="Example", purpose="Example purpose", owner_user_id="owner", profile_configuration={})
    repo = {"id": "a" * 32, "kind": "repository"}
    account = {"id": "b" * 32, "kind": "environment", "environment": {"name": "Dev", "environment_class": "development"}}

    async def resources(*args):
        return [repo, account]

    monkeypatch.setattr("src.products.shared_profile.catalogue", resources)
    legacy = next(iter(environments({}, [account])))
    product.profile_configuration = await apply_patch(
        None,
        None,
        product,
        {
            "environments": {
                legacy: None,
                "dev": {"name": "Dev", "environment_class": "development", "resource_id": account["id"], "deployment_target": "cluster/dev"},
                "prod": {"name": "Prod", "environment_class": "production", "resource_id": account["id"], "deployment_target": "cluster/prod"},
            },
            "repositories": {repo["id"]: {"purpose": "reference"}},
        },
    )
    named = environments(product.profile_configuration, [account])
    assert set(named) == {"dev", "prod"}
    assert {value["resource_id"] for value in named.values()} == {account["id"]}
    assert completion(product, [repo, account], product.profile_configuration, named, 0)["completed"] == 2
    with pytest.raises(HTTPException) as error:
        await apply_patch(None, None, product, {"default_repository": repo["id"]})
    assert error.value.status_code == 422
    with pytest.raises(HTTPException):
        await apply_patch(None, None, product, {"repositories": {"c" * 32: {"purpose": "product_code"}}})


def test_resource_identity_groups_legacy_numeric_repository_ids():
    def binding(namespace, native_id):
        return ProductSourceBinding(kind="repository", provider="github", namespace=namespace, native_id=native_id)

    assert resource_id(binding("27", "0035")) == resource_id(binding("027", "35"))
    assert resource_id(binding("27", "35")) != resource_id(binding("28", "35"))


@pytest.mark.asyncio
async def test_hidden_environment_cannot_be_removed_by_guessing_its_id(monkeypatch):
    product = Product(profile_configuration={})

    async def no_resources(*args):
        return []

    monkeypatch.setattr("src.products.shared_profile.catalogue", no_resources)
    with pytest.raises(HTTPException) as error:
        await apply_patch(None, None, product, {"environments": {"source-guessed": None}})
    assert error.value.status_code == 404
