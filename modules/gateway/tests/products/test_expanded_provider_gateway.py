"""Profile broker constrains the native calls chosen by the Go provider adapters."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException

from src.product_onboarding import discovery, expanded_provider
from src.product_onboarding.discovery import next_cursor, root_scope, valid_cursor


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [None, "https://api.linear.app", "https://api.linear.app/graphql"])
@pytest.mark.parametrize("credential_type,authorization", [("api_key", "fixture-only"), ("bearer", "Bearer fixture-only")])
async def test_linear_discovery_uses_native_graphql_endpoint_and_auth(monkeypatch, url, credential_type, authorization):
    credential = SimpleNamespace(provider="linear", service="linear", credential_type=credential_type, secret_arn="fixture")
    secret = {"token": "fixture-only", **({"url": url} if url else {})}
    secrets = Mock(current_version_id=Mock(return_value="v1"), get_secret_at_version=Mock(return_value=(json.dumps(secret), "v1")))
    query = {"query": "query { teams { nodes { id name } } }"}
    response = {"data": {"teams": {"nodes": [{"id": "team-one", "name": "Engineering"}]}}}
    calls = []

    def transport(request):
        calls.append(request)
        assert request.method == "POST" and str(request.url) == "https://api.linear.app/graphql"
        assert request.headers["Authorization"] == authorization
        assert json.loads(request.content) == query
        return httpx.Response(200, json=response)

    client = httpx.AsyncClient
    monkeypatch.setattr(expanded_provider.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(transport), **kwargs))
    result, _, version = await expanded_provider.read(credential, secrets, "POST", "", {}, query)
    assert result == response and version == "v1" and len(calls) == 1


@pytest.mark.asyncio
async def test_gitlab_navigation_page_retains_repository_continuation(monkeypatch):
    from tests.products.test_devlake_enrichment import observed

    monkeypatch.setattr(
        discovery, "configured_devlake", lambda: ("http://connector-api.fixture.svc.cluster.local:8080/v1/profile-discovery", "consumer")
    )
    subgroup = observed(kind="namespace", namespace="77", native_id="78", name="Subteam")
    repository = observed(kind="repository", namespace="77", native_id="901", name="API")
    pages = [
        {"contract_version": 1, "items": [subgroup], "next_cursor": "v1:gitlab:100:2:projects%3A1", "coverage": "partial"},
        {"contract_version": 1, "items": [repository], "next_cursor": "", "coverage": "complete"},
    ]
    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=pages[len(requests) - 1])

    monkeypatch.setattr(
        discovery,
        "httpx",
        SimpleNamespace(
            AsyncClient=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(transport), **kwargs), HTTPError=httpx.HTTPError
        ),
    )
    binding = SimpleNamespace(id="binding", provider="gitlab", namespace="77", generation=1)
    operation = SimpleNamespace(tenant_id="tenant", product_id="product", project_id="project", id="operation", lease_owner="attempt")
    resources, cursor, coverage = await discovery.discover_devlake_page(None, None, binding, None, 1, 100, operation)
    assert resources == [subgroup] and coverage == "partial" and cursor is not None
    page = {**cursor, "binding_id": binding.id, "generation": binding.generation, "namespace": binding.namespace}
    resources, cursor, coverage = await discovery.discover_devlake_page(None, None, binding, None, page, 100, operation)
    assert resources == [repository] and cursor is None and coverage == "complete"
    assert [request["cursor"] for request in requests] == ["", "v1:gitlab:100:2:projects%3A1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_type,authorization", [("api_key", "fixture-only"), ("bearer", "Bearer fixture-only")])
async def test_linear_registered_setup_credential_verifies_with_matching_auth(db_session, source_member, monkeypatch, credential_type, authorization):
    from uuid import uuid4

    from src.auth.vault_routes import put_credential_endpoint
    from src.auth.vault_schemas import CredentialCreate
    from src.shared.models.vault import UserCredential

    value = json.dumps({"url": "https://api.linear.app/graphql", "token": "fixture-only"})
    secrets = Mock(
        create_secret=Mock(return_value="fixture-secret"),
        current_version_id=Mock(return_value="v1"),
        get_secret_at_version=Mock(return_value=(value, "v1")),
    )
    registered = await put_credential_endpoint(
        uuid4(),
        CredentialCreate(service="linear", label="Team access", credential_type=credential_type, scope_hint="user", value=value),
        source_member,
        db_session,
        secrets,
    )
    credential = await db_session.get(UserCredential, registered.id)
    assert credential.credential_type == credential_type
    binding = SimpleNamespace(provider="linear", native_id="team-one", namespace="/", credential_id=credential.id, tenant_id=source_member.org_id)
    requests = []

    def transport(request):
        requests.append(request)
        assert request.method == "POST" and str(request.url) == "https://api.linear.app/graphql"
        assert request.headers["Authorization"] == authorization
        assert json.loads(request.content) == {"query": 'query { team(id: "team-one") { id name } }'}
        return httpx.Response(200, json={"data": {"team": {"id": "team-one", "name": "Engineering"}}})

    monkeypatch.setattr(
        expanded_provider,
        "httpx",
        SimpleNamespace(
            AsyncClient=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(transport), **kwargs), HTTPError=httpx.HTTPError
        ),
    )
    observation = await expanded_provider.observe(binding, credential, secrets)
    assert observation["status"] == "healthy" and len(requests) == 1


@pytest.mark.asyncio
async def test_campaign_accepts_actual_gateway_verification_and_profile_revisions(monkeypatch):
    from pathlib import Path

    from src.product_onboarding import enrichment, jenkins
    from src.products.source_routes import binding_view
    from src.shared.models.base import utcnow
    from tests.products.test_devlake_enrichment import observed

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "insights" / "poc" / "devlake"))
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4]))
    from modules.insights.tests.devlake_poc.test_s07 import ProviderProfileSourceTests
    from provider_evidence import from_measurement

    fixture = ProviderProfileSourceTests()
    fixture.setUp()
    selection = fixture.expanded()
    bindings = fixture.responses[fixture.config["gateway"]["bindings_url"]]["items"]
    profile = fixture.responses[fixture.config["gateway"]["profile_url"]]
    monkeypatch.setattr(enrichment, "connection_access", AsyncMock(return_value=object()))
    for provider, current in selection["providers"].items():
        upstream = fixture.responses[fixture.config["providers"][provider]["url"]]
        namespace = fixture.config["providers"][provider]["namespace"]
        if provider == "slack":
            upstream["channel"].update(is_channel=True, context_team_id=namespace)
        binding = SimpleNamespace(
            id=current["binding_id"],
            provider=provider,
            tenant_id=current["tenant_id"],
            project_id=current["project_id"],
            namespace=namespace,
            native_id=current["record_id"],
            credential_id=current["connection_id"],
            installation_id=None,
            revision=current["binding_revision"],
            generation=current["source_generation"],
            unlinked_at=None,
            desired={"provider": provider, "kind": current["record_type"], "namespace": namespace, "native_id": current["record_id"]},
        )
        credential = SimpleNamespace(id=binding.credential_id, org_id=binding.tenant_id)
        if provider == "jenkins":
            monkeypatch.setattr(jenkins, "read_jenkins", AsyncMock(return_value=(upstream, "v1")))
            observation = await jenkins.observe_jenkins(binding, credential, None)
        else:
            monkeypatch.setattr(expanded_provider, "read", AsyncMock(return_value=(upstream, {}, "v1")))
            observation = await expanded_provider.observe(binding, credential, None)
        assert observation["status"] == "healthy", provider
        binding.status, binding.observed = observation["status"], observation["observed"]
        native_id, name = fixture.module.upstream_identity(provider, upstream)
        item = observed(kind=current["record_type"], namespace=binding.namespace, native_id=str(native_id), name=name)
        if provider in ("jenkins", "gitlab"):
            item["full_name"] = upstream["fullName" if provider == "jenkins" else "path_with_namespace"]
            fields = (item["kind"], item["namespace"], item["native_id"], item["name"], item["full_name"])
            item["source_revision"] = "observation-sha256:" + hashlib.sha256("\x00".join(fields).encode()).hexdigest()
        fact = await enrichment.selected_source_fact(None, SimpleNamespace(org_id=binding.tenant_id), binding, item, "complete")
        assert fact is not None, provider
        section, field, _ = fixture.module.DESTINATIONS[provider]
        configuration, proofs, conflicts, suggestions = enrichment.source_changes({"basic": {}}, [], [fact])
        assert not conflicts and not suggestions
        existing = profile["configuration"].get(section, {}).get(field, [])
        profile["configuration"].setdefault(section, {})[field] = [
            reference for reference in existing if reference.get("source_connection_id") != binding.credential_id
        ] + configuration[section][field]
        profile["provenance"] = [proof for proof in profile["provenance"] if proof["binding_id"] != binding.id] + proofs
        bindings[:] = [entry for entry in bindings if entry["id"] != binding.id] + [binding_view(binding, True, utcnow())]
        current["record_revision"] = fact["value"]["record_revision"]
        assert bindings[-1]["source_revision"] != current["record_revision"]
    measurement = fixture.module.collect(fixture.config, "a" * 40, fixture.read, selection)
    evidence = from_measurement(json.dumps(measurement), "123", selection, "a" * 40, "123", "2")
    assert set(evidence) == set(selection["providers"])


@pytest.mark.asyncio
@pytest.mark.parametrize("response_namespace", ["workloads", "foreign"])
async def test_argocd_observation_selects_bound_application_namespace(monkeypatch, response_namespace):
    credential = SimpleNamespace(
        id="credential", org_id="tenant", provider="argocd", service="argocd", credential_type="bearer", secret_arn="fixture"
    )
    binding = SimpleNamespace(provider="argocd", native_id="checkout", namespace="team/workloads", credential_id="credential", tenant_id="tenant")
    secret = {"url": "https://argo.example.test", "token": "fixture-only"}
    secrets = Mock(current_version_id=Mock(return_value="v1"), get_secret_at_version=Mock(return_value=(json.dumps(secret), "v1")))
    calls = []

    def transport(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/api/v1/applications/checkout"
        assert request.url.params["project"] == "team" and request.url.params["appNamespace"] == "workloads"
        assert request.headers["Authorization"] == "Bearer fixture-only"
        return httpx.Response(200, json={"metadata": {"name": "checkout", "namespace": response_namespace}, "spec": {"project": "team"}})

    client = httpx.AsyncClient
    monkeypatch.setattr(expanded_provider.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(transport), **kwargs))
    result = await expanded_provider.observe(binding, credential, secrets)
    assert len(calls) == 1
    assert calls[0].url.params["appNamespace"] == "workloads"
    if response_namespace == "workloads":
        assert result["coverage"] == "complete" and result["observed"]["namespace"] == "team/workloads"
    else:
        assert result["coverage"] == "unavailable" and "observed" not in result


@pytest.mark.parametrize(
    "provider,namespace,cursor,method,path,query",
    [
        ("sonarqube", "/", "", "GET", "components/search_projects", {"p": ["1"], "ps": ["100"]}),
        ("argocd", "team/ns", "", "GET", "applications", {}),
        (
            "gitlab",
            "77",
            "v1:gitlab:100:2:projects%3A1",
            "GET",
            "groups/77/projects",
            {
                "page": ["1"],
                "per_page": ["100"],
                "archived": ["false"],
                "min_access_level": ["20"],
                "with_shared": ["false"],
                "include_subgroups": ["false"],
            },
        ),
        ("pagerduty", "/", "", "GET", "services", {"offset": ["0"], "limit": ["100"], "query": [""]}),
        ("linear", "/", "", "POST", "", {}),
        (
            "slack",
            "/",
            "v1:slack:100:2:opaque%2Bnext",
            "GET",
            "conversations.list",
            {"limit": ["100"], "types": ["public_channel,private_channel"], "cursor": ["opaque+next"]},
        ),
    ],
)
def test_broker_request_is_bound_to_provider_namespace_and_page(provider, namespace, cursor, method, path, query):
    assert expanded_provider.expected(provider, namespace, cursor, 100)[:3] == (method, path, query)
    binding = SimpleNamespace(provider=provider)
    assert valid_cursor(cursor, binding) is bool(cursor)
    for invalid in ("v1:foreign:100:2:opaque", f"v1:{provider}:100:6:opaque"):
        with pytest.raises(HTTPException):
            expanded_provider.expected(provider, namespace, invalid, 100)


def test_gateway_root_identity_revision_tracks_normalized_namespace():
    fields = ["quality_project", "", "team:api", "API"]
    item = dict(
        kind=fields[0],
        namespace="",
        native_id=fields[2],
        name=fields[3],
        source_revision="observation-sha256:" + hashlib.sha256("\x00".join(fields).encode()).hexdigest(),
    )
    normalized = root_scope(item)
    assert normalized["namespace"] == "/"
    assert normalized["source_revision"] == "observation-sha256:" + hashlib.sha256("\x00".join([fields[0], "/", *fields[2:]]).encode()).hexdigest()
    assert root_scope({**item, "source_revision": "observation-sha256:invalid"})["namespace"] == ""
    binding = SimpleNamespace(provider="sonarqube", id="binding", generation=1, namespace="/")
    receipt = {"page": 1, "seen_cursors": []}
    first = next_cursor(receipt, {"contract_version": 1, "cursor": "v1:sonarqube:100:2:"}, binding)
    assert first is not None
    assert next_cursor({**receipt, "page": first[0]}, {"contract_version": 1, "cursor": "v1:sonarqube:100:2:"}, binding) is None


@pytest.mark.asyncio
async def test_rotated_credential_never_calls_provider(monkeypatch):
    credential = SimpleNamespace(provider=None, service="sonarqube", credential_type="api_key", secret_arn="fixture-only")
    secrets = Mock(current_version_id=Mock(return_value="rotated"))
    calls = []
    monkeypatch.setattr(expanded_provider, "httpx", SimpleNamespace(AsyncClient=lambda **_: calls.append("called")))
    with pytest.raises(HTTPException):
        await expanded_provider.read(
            credential, secrets, "GET", "components/search_projects", {"p": ["1"], "ps": ["100"]}, expected_version="original"
        )
    assert calls == [] and secrets.get_secret_at_version.call_count == 0
