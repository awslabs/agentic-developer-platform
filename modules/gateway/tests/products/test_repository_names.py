"""Legacy repository names stay readable without changing source identity or authority."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.product_onboarding.source_discovery import repository_resource
from src.products import repository_names as names
from src.shared.models.base import utcnow


def selected(*, tenant="tenant", binding_id="binding"):
    binding = SimpleNamespace(id=binding_id, tenant_id=tenant, installation_id="connection", namespace="027", native_id="0035")
    connection = SimpleNamespace(
        id="connection", org_id=tenant, provider="github", ownership_disputed=False, installation_id="17", provider_scope_id="27"
    )
    return binding, connection


@pytest.fixture
def github(monkeypatch):
    names._names.clear()
    document = {"id": 35, "name": "service", "full_name": "example/service", "owner": {"id": 27, "login": "example"}}
    state = SimpleNamespace(document=document, calls=[], closed=0, error=None, blocked=False)

    class Client:
        def __init__(self, *args):
            pass

        async def get_installation_repository(self, installation, repository):
            state.calls.append((installation, repository))
            if state.blocked:
                await asyncio.Event().wait()
            if state.error:
                raise state.error
            return state.document

        async def aclose(self):
            state.closed += 1

    monkeypatch.setattr(names, "GitHubAppClient", Client)
    monkeypatch.setattr("src.admin.connections.service._get_github_app_credentials", lambda: ("app", "key"))
    yield state
    names._names.clear()


@pytest.mark.asyncio
async def test_reads_and_caches_names_by_authorized_connection_without_changing_ids(github):
    first, connection = selected()
    second, _ = selected(binding_id="second-project-binding")
    assert await names.repository_names([(first, connection), (second, connection)]) == {first.id: "example/service", second.id: "example/service"}
    assert github.calls == [(17, 35)]
    assert await names.repository_names([(first, connection)]) == {first.id: "example/service"}
    assert github.calls == [(17, 35)]
    assert (first.namespace, first.native_id) == ("027", "0035")
    connection.ownership_disputed = True
    assert await names.repository_names([(first, connection)]) == {}
    foreign, other_connection = selected(tenant="other-tenant")
    assert await names.repository_names([(foreign, other_connection)]) == {foreign.id: "example/service"}
    assert len(github.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", ["repository", "owner", "name", "invalid_name", "scope", "tenant"])
async def test_refuses_names_from_other_sources_or_inconsistent_metadata(github, wrong):
    binding, connection = selected()
    if wrong == "repository":
        github.document["id"] = 36
    if wrong == "owner":
        github.document["owner"]["id"] = 28
    if wrong == "name":
        github.document["full_name"] = "other/service"
    if wrong == "invalid_name":
        github.document.update(name="..", full_name="example/..")
    if wrong == "scope":
        connection.provider_scope_id = "other"
    if wrong == "tenant":
        connection.org_id = "other"
    assert not (await names.repository_names([(binding, connection)])).get(binding.id)


@pytest.mark.asyncio
async def test_provider_denial_is_a_bounded_name_failure(github):
    binding, connection = selected()
    request = httpx.Request("GET", "https://api.github.com/repositories/35")
    github.error = httpx.HTTPStatusError("denied", request=request, response=httpx.Response(403, request=request))
    assert await names.repository_names([(binding, connection)]) == {binding.id: None}
    assert await names.repository_names([(binding, connection)]) == {binding.id: None}
    assert len(github.calls) == 1 and github.closed == 1


@pytest.mark.asyncio
async def test_slow_github_does_not_block_profile(github, monkeypatch):
    github.blocked = True
    monkeypatch.setattr(names, "LOOKUP_TIMEOUT", 0.02)
    async with asyncio.timeout(1):
        assert await names.repository_names([selected()]) == {}
    assert github.closed == 1


def test_repository_discovery_includes_org_and_name_but_preserves_native_identity():
    resource = repository_resource(
        {"id": 35, "name": "service", "owner": {"id": 27, "login": "example"}, "updated_at": "2026-11-05T12:00:00Z"}, "27", utcnow()
    )
    assert resource["full_name"] == "example/service"
    assert (resource["namespace"], resource["native_id"]) == ("27", "35")


@pytest.mark.asyncio
async def test_legacy_profile_name_resolves_without_mutation_and_is_hidden_after_revocation(test_client, db_session, app, source_member, github):
    from src.products.models import ProductSourceBinding
    from src.shared.database_agent_context import get_agent_context_db
    from src.shared.models.vault import ChannelTenantMap
    from tests.products.test_i08_api import mutation, setup_source

    async def knowledge_db():
        yield SimpleNamespace(scalar=AsyncMock(return_value=0))

    app.dependency_overrides[get_agent_context_db] = knowledge_db
    path, selection = await setup_source(test_client, db_session, source_member)
    created = await test_client.post(path, json=selection, headers=mutation(1, "legacy_name_binding_1234"))
    assert created.status_code == 201, created.text
    shared = f"/v1/products/{path.split('/')[3]}/profile"
    response = await test_client.get(shared)
    assert response.status_code == 200, response.text
    resource = response.json()["resources"][0]
    assert resource["label"] == "example/service"
    assert (resource["namespace"], resource["native_id"]) == ("27", "0035")
    assert response.json()["revision"] == 2
    binding = await db_session.get(ProductSourceBinding, (source_member.org_id, created.json()["id"]))
    assert binding.desired == selection and not binding.observed and binding.status == "unverified"
    assert len(github.calls) == 1
    saved = await test_client.patch(
        shared,
        json={"profile_configuration": {"repositories": {resource["id"]: {"purpose": "reference", "label": "27/35"}}}},
        headers=mutation(2, "legacy_numeric_label_1234"),
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["shared_profile"]["resources"][0]["label"] == "example/service"
    connection = await db_session.get(ChannelTenantMap, selection["connection_ref"]["id"])
    connection.ownership_disputed = True
    await db_session.commit()
    revoked = await test_client.get(shared)
    assert revoked.json()["resources"] == [] and "example/service" not in revoked.text
    assert len(github.calls) == 1
