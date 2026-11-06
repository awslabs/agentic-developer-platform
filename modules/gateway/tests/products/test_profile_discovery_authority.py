"""Profile discovery work is resolved from persisted work, not a service claim."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Request

from src.auth.atlassian_system_routes import SERVICE, current_system
from src.internal import profile_discovery_authority as profile_authority
from src.internal.auth_deps import verify_internal_or_irsa
from src.product_onboarding.discovery import connection_fence
from src.products.models import ConnectorModuleAdmission, ProductOperation, ProductSourceBinding
from src.products.source_routes import collection_connection_access
from src.shared.models.atlassian_system import AtlassianSystemConnection
from src.shared.models.vault import ChannelTenantMap, UserCredential, UserIdentity
from tests.products.test_i08_api import mutation, setup_source


@pytest.fixture
async def profile_discovery_work(test_client, db_session, app, source_member, monkeypatch):
    monkeypatch.setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
    path, selection = await setup_source(test_client, db_session, source_member)
    root = path.removesuffix("/bindings")
    created = await test_client.post(path, json=selection, headers=mutation(1, "profile_work_binding_123"))
    started = await test_client.post(root + "/onboarding-sessions", json={}, headers=mutation(2, "profile_work_session_123"))
    assert created.status_code == 201 and started.status_code == 201
    binding = await db_session.get(ProductSourceBinding, (source_member.org_id, created.json()["id"]))
    binding.status = "healthy"
    binding.observed = {
        "observed_at": datetime.now(UTC).isoformat(),
        "stale_after": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "source_revision": "observed-revision",
    }
    db_session.add(ConnectorModuleAdmission(tenant_id=source_member.org_id, enabled=True))
    connection = await db_session.get(ChannelTenantMap, binding.installation_id)
    revision = hashlib.sha256(json.dumps([str(value) for value in connection_fence(connection)], separators=(",", ":")).encode()).hexdigest()
    operation = ProductOperation(
        tenant_id=source_member.org_id,
        product_id=root.split("/")[3],
        project_id=root.split("/")[5],
        session_id=started.json()["id"],
        actor_id=source_member.user_id,
        operation="discoverSources",
        key_hash="a" * 64,
        request_hash="b" * 64,
        state="running",
        expected_revision=2,
        lease_owner="attempt-one",
        attempt_count=1,
        lease_until=datetime.now(UTC) + timedelta(minutes=1),
        request={
            "binding_ids": [binding.id],
            "generations": [binding.generation],
            "adapter": "devlake",
            "consumer": "registered-connector-api",
            "connection_fences": [revision],
            "source_revisions": [binding.observed["source_revision"]],
        },
        receipt={"index": 0, "page": 1, "seen": [], "seen_cursors": [], "coverage": "partial"},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(operation)
    await db_session.commit()
    service = source_member.model_copy(
        update={
            "user_id": "iam-agent:registered-connector-api",
            "agent_registry_id": "registered-connector-api",
            "account_type": "service",
            "auth_source": "iam",
            "scope": "internal",
            "credential_scopes": ["connector:profile:read"],
        }
    )
    identity = {"service": service}

    async def verified(request: Request):
        request.state.token_context = identity["service"]

    app.dependency_overrides[verify_internal_or_irsa] = verified
    return {
        "body": {
            "tenant_id": source_member.org_id,
            "product_id": operation.product_id,
            "project_id": operation.project_id,
            "operation_id": operation.id,
            "attempt_id": "attempt-one",
            "binding_id": binding.id,
            "cursor": "",
            "limit": 100,
        },
        "operation": operation,
        "binding": binding,
        "connection": connection,
        "identity": identity,
    }


@pytest.mark.asyncio
async def test_profile_work_resolves_live_owned_source_without_credentials(test_client, profile_discovery_work):
    fixture = profile_discovery_work
    response = await test_client.post("/internal/v1/profile-discovery/resolve", json=fixture["body"])
    assert response.status_code == 200, response.text
    work = response.json()
    assert work["operation_id"] == fixture["operation"].id
    assert work["binding_generation"] == fixture["binding"].generation
    assert work["provider"] == "github" and work["installation"] is True
    assert work["limit"] == 100 and work["cursor"] == ""
    assert len(work["connection_revision"]) == 64
    assert set(work) == {
        "tenant_id",
        "product_id",
        "project_id",
        "operation_id",
        "attempt_id",
        "binding_id",
        "binding_generation",
        "connection_id",
        "connection_revision",
        "provider",
        "installation",
        "namespace",
        "cursor",
        "limit",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"tenant_id": "foreign"},
        {"operation_id": "foreign"},
        {"attempt_id": "foreign"},
        {"binding_id": "foreign"},
        {"cursor": "v1:github:100:2"},
        {"limit": 1},
    ],
)
async def test_profile_work_refuses_foreign_and_out_of_cursor_work(test_client, profile_discovery_work, changed):
    body = {**profile_discovery_work["body"], **changed}
    response = await test_client.post("/internal/v1/profile-discovery/resolve", json=body)
    assert response.status_code in (403, 404)
    assert "connection_revision" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        "native",
        "wrong_consumer",
        "expired",
        "rotated",
        "changed_connection",
        "missing_pin",
        "unclaimed",
        "stopped",
        "revoked_binding",
        "disabled",
        "unentitled",
        "missing_source_pin",
        "changed_source_revision",
    ],
)
async def test_profile_work_refuses_stale_or_unentitled_service(test_client, db_session, profile_discovery_work, monkeypatch, changed):
    fixture = profile_discovery_work
    operation = fixture["operation"]
    if changed == "native":
        operation.request = {**operation.request, "adapter": "native"}
    elif changed == "wrong_consumer":
        operation.request = {**operation.request, "consumer": "someone-else"}
    elif changed == "expired":
        operation.lease_until = datetime.now(UTC) - timedelta(minutes=1)
    elif changed == "rotated":
        fixture["binding"].generation += 1
    elif changed == "changed_connection":
        fixture["connection"].provider_scope_id = "changed-owner-scope"
    elif changed == "missing_pin":
        operation.request = {**operation.request, "connection_fences": []}
    elif changed == "missing_source_pin":
        operation.request = {**operation.request, "source_revisions": []}
    elif changed == "changed_source_revision":
        fixture["binding"].observed = {**fixture["binding"].observed, "source_revision": "different"}
    elif changed == "unclaimed":
        operation.attempt_count = 0
    elif changed == "stopped":
        operation.state = "cancelled"
    elif changed == "revoked_binding":
        fixture["binding"].status = "revoked"
    elif changed == "disabled":
        monkeypatch.setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "false")
    else:
        service = fixture["identity"]["service"]
        fixture["identity"]["service"] = service.model_copy(update={"credential_scopes": []})
    await db_session.commit()
    response = await test_client.post("/internal/v1/profile-discovery/resolve", json=fixture["body"])
    assert response.status_code in (403, 404)
    assert "connection_revision" not in response.text


@pytest.mark.asyncio
async def test_profile_github_page_uses_existing_installation_and_binds_continuation(
    test_client,
    db_session,
    profile_discovery_work,
    monkeypatch,
):
    fixture = profile_discovery_work
    seen = []

    async def read(mapping, *, page, limit):
        seen.append((mapping.id, page, limit))
        return (
            [
                {
                    "kind": "repository",
                    "namespace": fixture["binding"].namespace,
                    "native_id": str(page),
                    "name": "repo",
                    "source_revision": "revision",
                    "observed_at": datetime.now(UTC).isoformat(),
                }
            ],
            "2" if page == 1 else None,
            "partial" if page == 1 else "complete",
        )

    monkeypatch.setattr(profile_authority, "github_resources", read)
    path = "/internal/v1/profile-discovery/github-page"
    first = await test_client.post(path, json=fixture["body"])
    assert first.status_code == 200, first.text
    assert first.json()["coverage"] == "partial"
    resolved = await test_client.post("/internal/v1/profile-discovery/resolve", json=fixture["body"])
    assert resolved.status_code == 200
    assert first.json()["work"] == resolved.json()
    assert first.json()["work"]["connection_id"] == fixture["connection"].id
    assert first.json()["next_cursor"] == "v1:github:100:2"
    assert "credential" not in first.text
    fixture["operation"].receipt = {
        **fixture["operation"].receipt,
        "page": {
            "contract_version": 1,
            "cursor": first.json()["next_cursor"],
            "binding_id": fixture["binding"].id,
            "generation": fixture["binding"].generation,
            "namespace": fixture["binding"].namespace,
        },
    }
    await db_session.commit()
    second = await test_client.post(path, json={**fixture["body"], "cursor": first.json()["next_cursor"]})
    assert second.status_code == 200, second.text
    assert second.json()["coverage"] == "complete"
    assert second.json()["next_cursor"] == ""
    assert second.json()["work"] == {**resolved.json(), "cursor": first.json()["next_cursor"]}
    assert seen == [(fixture["connection"].id, 1, 100), (fixture["connection"].id, 2, 100)]


@pytest.mark.asyncio
@pytest.mark.parametrize("next_page,coverage", [("3", "partial"), ("2", "complete"), (None, "unavailable")])
async def test_profile_github_page_refuses_incomplete_provider_pages(
    test_client,
    profile_discovery_work,
    monkeypatch,
    next_page,
    coverage,
):
    async def read(*args, **kwargs):
        return [], next_page, coverage

    monkeypatch.setattr(profile_authority, "github_resources", read)
    response = await test_client.post("/internal/v1/profile-discovery/github-page", json=profile_discovery_work["body"])
    assert response.status_code == 404
    assert "connection_revision" not in response.text


@pytest.mark.asyncio
async def test_profile_github_page_refuses_revocation_during_provider_read(
    test_client,
    db_session,
    profile_discovery_work,
    monkeypatch,
):
    fixture = profile_discovery_work

    async def read(*args, **kwargs):
        fixture["operation"].state = "cancelled"
        await db_session.commit()
        return [], None, "complete"

    monkeypatch.setattr(profile_authority, "github_resources", read)
    response = await test_client.post("/internal/v1/profile-discovery/github-page", json=fixture["body"])
    assert response.status_code == 404
    assert "connection_revision" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["connection", "binding", "cursor", "source_id", "observed_revision"])
async def test_profile_github_page_refuses_changed_pins_after_read(
    test_client,
    db_session,
    profile_discovery_work,
    monkeypatch,
    changed,
):
    fixture = profile_discovery_work

    async def read(*args, **kwargs):
        if changed == "connection":
            fixture["connection"].provider_scope_id = "rotated-owner"
        elif changed == "binding":
            fixture["binding"].generation += 1
        elif changed == "source_id":
            fixture["binding"].native_id = "different-repository"
        elif changed == "observed_revision":
            fixture["binding"].observed = {**fixture["binding"].observed, "source_revision": "different"}
        else:
            fixture["operation"].receipt = {**fixture["operation"].receipt, "page": 2}
        await db_session.commit()
        return [], None, "complete"

    monkeypatch.setattr(profile_authority, "github_resources", read)
    response = await test_client.post("/internal/v1/profile-discovery/github-page", json=fixture["body"])
    assert response.status_code == 404
    assert "connection_revision" not in response.text


@pytest.mark.asyncio
async def test_profile_github_page_never_reads_for_unadmitted_work(test_client, profile_discovery_work, monkeypatch):
    async def read(*args, **kwargs):
        pytest.fail("unadmitted work reached provider")

    monkeypatch.setattr(profile_authority, "github_resources", read)
    response = await test_client.post(
        "/internal/v1/profile-discovery/github-page",
        json={
            **profile_discovery_work["body"],
            "attempt_id": "foreign",
        },
    )
    assert response.status_code == 404


@pytest.fixture
async def profile_jira_work(profile_discovery_work, db_session):
    fixture = profile_discovery_work
    site_id = "12345678-1234-1234-1234-123456789abc"
    credential = UserCredential(
        id="shared-jira-credential",
        org_id=fixture["body"]["tenant_id"],
        service=SERVICE,
        provider="agentcore-identity",
        credential_type="api_key",
        label="Synthetic shared Jira",
        secret_arn="agentcore-identity:api-key:synthetic-provider",
        strict=True,
        agent_consent=True,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    connection = AtlassianSystemConnection(
        org_id=fixture["body"]["tenant_id"],
        credential_id=credential.id,
        revision="system-one",
        site_id=site_id,
        workload_name="synthetic-workload",
        provider_name="synthetic-provider",
        allowed_product_ids=[fixture["body"]["product_id"]],
        schemas={"listJiraBoards": {"type": "object", "properties": {}}},
        status="ready",
        created_by=fixture["operation"].actor_id,
        verified_at=datetime.now(UTC),
    )
    db_session.add_all([credential, connection])
    binding = fixture["binding"]
    binding.provider = "atlassian-mcp"
    binding.kind = "tracker"
    binding.installation_id = None
    binding.credential_id = credential.id
    binding.namespace = site_id
    binding.native_id = "100:all"
    binding.desired = {
        "share_with_product": True,
        "product_id": fixture["body"]["product_id"],
        "jira_project_id": "100",
        "jira_project_key": "APP",
        "board_id": None,
    }
    binding.observed = {**binding.observed, "connection_revision": connection.revision}
    await db_session.flush()
    await db_session.refresh(credential)
    revision = hashlib.sha256(json.dumps([str(value) for value in connection_fence(credential)], separators=(",", ":")).encode()).hexdigest()
    operation = fixture["operation"]
    operation.request = {**operation.request, "connection_fences": [revision]}
    await db_session.commit()
    assert revision == hashlib.sha256(json.dumps([str(value) for value in connection_fence(credential)], separators=(",", ":")).encode()).hexdigest()
    _, actual = await current_system(db_session, credential.id, fixture["body"]["tenant_id"], fixture["body"]["product_id"])
    assert actual.revision == connection.revision
    owner = fixture["identity"]["service"].model_copy(update={"user_id": operation.actor_id, "account_type": "human"})
    assert await collection_connection_access(db_session, owner, binding, fixture["body"]["product_id"]) is credential
    return {**fixture, "credential": credential, "system": connection}


@pytest.mark.asyncio
async def test_shared_jira_discovery_applies_with_separate_provider_and_observation_revisions(
    test_client,
    db_session,
    profile_jira_work,
):
    from src.products.models import Project
    from tests.products.test_devlake_enrichment import observed
    from tests.products.test_i08_api import mutation

    fixture = profile_jira_work
    binding = fixture["binding"]
    operation = fixture["operation"]
    binding.native_id = "100:12"
    binding.desired = {**binding.desired, "board_id": 12, "board_type": "scrum"}
    item = observed("tracker", binding.namespace, "12", "Selected board")
    operation.state = "succeeded"
    operation.lease_owner = None
    operation.lease_until = None
    operation.receipt = {"coverage": "complete", "resources": [{**item, "binding_id": binding.id}]}
    await db_session.commit()
    root = f"/v1/products/{operation.product_id}/projects/{operation.project_id}"
    readback = await test_client.get(root + "/operations/" + operation.id)
    assert readback.status_code == 200 and readback.json()["coverage"] == "complete"
    response = await test_client.post(
        root + f"/onboarding-sessions/{operation.session_id}/source-enrichment",
        json={"operation_id": operation.id},
        headers=mutation(2, "shared_jira_enrichment_apply_123"),
    )
    assert response.status_code == 200, response.text
    replay = await test_client.post(
        root + f"/onboarding-sessions/{operation.session_id}/source-enrichment",
        json={"operation_id": operation.id},
        headers=mutation(2, "shared_jira_enrichment_apply_123"),
    )
    assert replay.status_code == 200 and replay.json() == response.json()
    project = await db_session.get(Project, (operation.tenant_id, operation.project_id))
    await db_session.refresh(project)
    assert project.configuration["basic"]["trackers"][0]["record_revision"] == item["source_revision"]
    profile_readback = await test_client.get(root)
    assert profile_readback.status_code == 200, profile_readback.text
    assert profile_readback.json()["configuration"]["basic"]["trackers"][0]["record_revision"] == item["source_revision"]
    binding_readback = await test_client.get(root + "/bindings")
    assert binding_readback.status_code == 200 and binding_readback.json()["items"][0]["status"] == "healthy"
    binding.observed = {**binding.observed, "source_revision": "changed-provider-revision"}
    await db_session.commit()
    changed = await test_client.get(root + "/operations/" + operation.id)
    assert changed.status_code == 200 and changed.json()["coverage"] == "denied"
    fixture["system"].revision = "rotated"
    await db_session.commit()
    restricted_profile = await test_client.get(root)
    assert restricted_profile.status_code == 200, restricted_profile.text
    assert not (restricted_profile.json()["configuration"].get("basic") or {}).get("trackers")
    assert not any(proof.get("binding_id") == binding.id for proof in restricted_profile.json()["provenance"])
    restricted_binding = await test_client.get(root + "/bindings")
    assert restricted_binding.status_code == 200
    assert restricted_binding.json()["items"][0]["status"] == "restricted"


@pytest.mark.asyncio
@pytest.mark.parametrize("rotate", [False, True])
async def test_selected_shared_jira_discovery_checkpoints_protected_page_and_applies(
    test_client,
    db_session,
    profile_jira_work,
    monkeypatch,
    rotate,
):
    from src.products.models import Project
    from tests.products.test_i08_api import mutation

    fixture = profile_jira_work
    binding = fixture["binding"]
    binding.native_id = "100:12"
    binding.desired = {**binding.desired, "board_id": 12, "board_type": "scrum"}
    fixture["system"].schemas = {"getJiraBoardConfig": {}}
    await db_session.commit()
    monkeypatch.setenv("CONNECTOR_PROFILE_DISCOVERY_URL", "http://connector-api.insights.svc.cluster.local:8080/v1/profile-discovery")
    monkeypatch.setenv("CONNECTOR_PROFILE_DISCOVERY_CONSUMER", "registered-connector-api")
    reads = []

    async def read(connection, operation, arguments, **kwargs):
        reads.append((connection.credential_id, operation, arguments))
        if rotate:
            fixture["system"].revision = "rotated"
            await db_session.commit()
        return {
            "data": {
                "id": 12,
                "name": "Selected board",
                "type": "scrum",
                "location": {"type": "project", "id": "100", "key": "APP"},
            }
        }

    class ConnectorTransport:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json):
            assert url == "http://connector-api.insights.svc.cluster.local:8080/v1/profile-discovery"
            assert json["provider"] == "jira" and json["binding_id"] == binding.id
            response = await test_client.post(
                "/internal/v1/profile-discovery/jira-boards-page", json={key: value for key, value in json.items() if key != "provider"}
            )
            from httpx import Response

            if rotate:
                assert response.status_code == 404
                return Response(503)
            assert response.status_code == 200, response.text

            page = response.json()
            items = []
            for item in page["items"]:
                revision = hashlib.sha256("\x00".join(item[field] for field in ("kind", "namespace", "native_id", "name")).encode()).hexdigest()
                items.append({**item, "source_revision": "observation-sha256:" + revision, "provenance": "observation_only"})
            return Response(
                200,
                json={"contract_version": page["contract_version"], "items": items, "next_cursor": page["next_cursor"], "coverage": page["coverage"]},
            )

    monkeypatch.setattr(profile_authority, "adapter_read", read)
    monkeypatch.setattr("src.product_onboarding.discovery.httpx.AsyncClient", ConnectorTransport)
    operation = fixture["operation"]
    root = f"/v1/products/{operation.product_id}/projects/{operation.project_id}"
    discovery = await test_client.post(
        root + f"/onboarding-sessions/{operation.session_id}/discoveries",
        json={"binding_ids": [binding.id], "adapter": "devlake"},
        headers=mutation(2, "selected_shared_jira_discovery_123"),
    )
    assert discovery.status_code == 202, discovery.text
    discovered = await db_session.get(ProductOperation, (operation.tenant_id, discovery.json()["id"]))
    await db_session.refresh(discovered)
    if rotate:
        assert discovered.receipt["coverage"] == "unavailable" and not discovered.receipt["resources"]
        refused = await test_client.post(
            root + f"/onboarding-sessions/{operation.session_id}/source-enrichment",
            json={"operation_id": discovered.id},
            headers=mutation(2, "rotated_shared_jira_apply_123"),
        )
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "discovery_incomplete"
        project = await db_session.get(Project, (operation.tenant_id, operation.project_id))
        await db_session.refresh(project)
        assert not (project.configuration.get("basic") or {}).get("trackers")
        return
    assert discovered.state == "succeeded" and discovered.receipt["coverage"] == "complete"
    assert discovered.receipt["index"] == 1 and discovered.receipt["page"] == 1
    assert len(discovered.receipt["resources"]) == 1
    item = discovered.receipt["resources"][0]
    assert (item["kind"], item["namespace"], item["native_id"], item["binding_id"]) == (
        "tracker",
        binding.namespace,
        "12",
        binding.id,
    )
    assert item["provenance"] == "observation_only"
    assert item["source_revision"].startswith("observation-sha256:")
    assert reads == [(fixture["credential"].id, "board", {"boardId": 12})]
    response = await test_client.post(
        root + f"/onboarding-sessions/{operation.session_id}/source-enrichment",
        json={"operation_id": discovered.id},
        headers=mutation(2, "selected_shared_jira_apply_123"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 3 and len(response.json()["applied"]) == 1
    project = await db_session.get(Project, (operation.tenant_id, operation.project_id))
    await db_session.refresh(project)
    assert project.configuration["basic"]["trackers"][0]["record_revision"] == item["source_revision"]


@pytest.mark.asyncio
async def test_project_wide_jira_discovery_does_not_apply_an_unselected_board(test_client, db_session, profile_jira_work):
    from src.products.models import Project
    from tests.products.test_devlake_enrichment import observed
    from tests.products.test_i08_api import mutation

    operation = profile_jira_work["operation"]
    binding = profile_jira_work["binding"]
    operation.state = "succeeded"
    operation.lease_owner = None
    operation.lease_until = None
    operation.receipt = {"coverage": "complete", "resources": [{**observed("tracker", binding.namespace, "12", "Board"), "binding_id": binding.id}]}
    await db_session.commit()
    root = f"/v1/products/{operation.product_id}/projects/{operation.project_id}"
    response = await test_client.post(
        root + f"/onboarding-sessions/{operation.session_id}/source-enrichment",
        json={"operation_id": operation.id},
        headers=mutation(2, "project_wide_jira_apply_123"),
    )
    assert response.status_code == 200 and response.json()["applied"] == [] and response.json()["revision"] == 2
    project = await db_session.get(Project, (operation.tenant_id, operation.project_id))
    await db_session.refresh(project)
    assert not (project.configuration.get("basic") or {}).get("trackers")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "wrong_board", "wrong_project", "wrong_type", "missing_capability", "cursor", "rotated"])
@pytest.mark.parametrize("location", [{"type": "project", "projectKeyOrId": "100"}, {"type": "project", "id": "100", "key": "APP"}])
async def test_profile_selected_jira_board_reads_only_pinned_board(
    test_client,
    db_session,
    profile_jira_work,
    monkeypatch,
    failure,
    location,
):
    fixture = profile_jira_work
    binding = fixture["binding"]
    binding.native_id = "100:12"
    binding.desired = {**binding.desired, "board_id": 12, "board_type": "scrum"}
    fixture["system"].schemas = {"getJiraBoardConfig": {}}
    if failure == "missing_capability":
        fixture["system"].schemas = {}
    await db_session.commit()
    reads = []

    async def read(connection, operation, arguments, **kwargs):
        reads.append((connection.credential_id, operation, arguments))
        board = {"id": 12, "name": "Selected board", "type": "scrum", "location": dict(location)}
        if failure == "wrong_board":
            board["id"] = 13
        elif failure == "wrong_project":
            board["location"]["projectKeyOrId" if "projectKeyOrId" in location else "id"] = "200"
        elif failure == "wrong_type":
            board["type"] = "kanban"
        elif failure == "rotated":
            fixture["system"].revision = "rotated"
            await db_session.commit()
        return {"data": board}

    monkeypatch.setattr(profile_authority, "adapter_read", read)
    request = {**fixture["body"], "cursor": "v1:jira:100:1"} if failure == "cursor" else fixture["body"]
    result = await test_client.post("/internal/v1/profile-discovery/jira-boards-page", json=request)
    if failure is None:
        assert result.status_code == 200, result.text
        assert result.json()["coverage"] == "complete" and result.json()["next_cursor"] == ""
        assert [(item["kind"], item["native_id"]) for item in result.json()["items"]] == [("tracker", "12")]
    else:
        assert result.status_code == 404 and "connection_revision" not in result.text
    assert reads == ([] if failure in ("missing_capability", "cursor") else [(fixture["system"].credential_id, "board", {"boardId": 12})])


@pytest.mark.asyncio
async def test_profile_jira_boards_page_reads_only_selected_project_with_pinned_cursor(
    test_client,
    db_session,
    profile_jira_work,
    monkeypatch,
):
    fixture = profile_jira_work
    arguments = []

    async def read(connection, operation, args, **kwargs):
        arguments.append((connection.credential_id, operation, args.copy()))
        start_at = args["startAt"]
        return {
            "data": {
                "startAt": start_at,
                "maxResults": 100,
                "total": 2,
                "isLast": start_at == 1,
                "values": [{"id": start_at + 1, "name": "APP Board", "location": {"projectId": 100, "projectKey": "APP"}}],
            }
        }

    monkeypatch.setattr(profile_authority, "adapter_read", read)
    path = "/internal/v1/profile-discovery/jira-boards-page"
    resolved = await test_client.post("/internal/v1/profile-discovery/resolve", json=fixture["body"])
    assert resolved.status_code == 200, resolved.text
    first = await test_client.post(path, json=fixture["body"])
    assert first.status_code == 200, first.text
    assert first.json()["coverage"] == "partial" and first.json()["next_cursor"] == "v1:jira:100:1"
    assert first.json()["items"][0]["namespace"] == fixture["binding"].namespace
    assert first.json()["work"] == resolved.json()
    assert first.json()["work"]["connection_id"] == fixture["credential"].id
    assert "synthetic-provider" not in first.text
    fixture["operation"].receipt = {
        **fixture["operation"].receipt,
        "page": {
            "contract_version": 1,
            "cursor": first.json()["next_cursor"],
            "binding_id": fixture["binding"].id,
            "generation": fixture["binding"].generation,
            "namespace": fixture["binding"].namespace,
        },
    }
    await db_session.commit()
    second = await test_client.post(path, json={**fixture["body"], "cursor": first.json()["next_cursor"]})
    assert second.status_code == 200, second.text
    assert second.json()["coverage"] == "complete" and second.json()["next_cursor"] == ""
    assert [item["native_id"] for item in first.json()["items"] + second.json()["items"]] == ["1", "2"]
    assert second.json()["work"] == {**resolved.json(), "cursor": first.json()["next_cursor"]}
    assert arguments == [(fixture["credential"].id, "boards", {"projectKeyOrId": "100", "maxResults": 100, "startAt": start}) for start in (0, 1)]


@pytest.mark.asyncio
async def test_profile_jira_boards_page_preserves_pinned_provider_token(
    test_client,
    db_session,
    profile_jira_work,
    monkeypatch,
):
    from src.product_onboarding.discovery import next_cursor, valid_page

    fixture = profile_jira_work
    arguments = []

    async def read(connection, operation, args, **kwargs):
        arguments.append(args.copy())
        if args.get("startAt") == 0:
            return {
                "data": {
                    "startAt": 0,
                    "maxResults": 100,
                    "total": 2,
                    "isLast": False,
                    "nextPageToken": "private-page-two",
                    "values": [{"id": 1, "name": "APP Board", "location": {"projectId": 100, "projectKey": "APP"}}],
                }
            }
        if args.get("nextPageToken") == "private-page-two":
            return {
                "data": {
                    "startAt": 1,
                    "maxResults": 100,
                    "total": 2,
                    "isLast": True,
                    "values": [{"id": 2, "name": "APP Board Two", "location": {"projectId": 100, "projectKey": "APP"}}],
                }
            }
        raise AssertionError("unexpected Jira cursor")

    monkeypatch.setattr(profile_authority, "adapter_read", read)
    path = "/internal/v1/profile-discovery/jira-boards-page"
    first = await test_client.post(path, json=fixture["body"])
    assert first.status_code == 200, first.text
    cursor = first.json()["next_cursor"]
    assert cursor.startswith("v1:jira:100:t1.") and "private-page-two" not in first.text
    page = next_cursor(fixture["operation"].receipt, {"contract_version": 1, "cursor": cursor}, fixture["binding"])
    assert page is not None and valid_page(page[0], fixture["binding"])
    fixture["operation"].receipt = {**fixture["operation"].receipt, "page": page[0], "seen_cursors": page[1]}
    await db_session.commit()
    forged = await test_client.post(path, json={**fixture["body"], "cursor": cursor + "x"})
    assert forged.status_code == 404 and len(arguments) == 1
    second = await test_client.post(path, json={**fixture["body"], "cursor": cursor})
    assert second.status_code == 200, second.text
    assert second.json()["coverage"] == "complete" and second.json()["next_cursor"] == ""
    assert [item["native_id"] for item in first.json()["items"] + second.json()["items"]] == ["1", "2"]
    assert arguments == [
        {"projectKeyOrId": "100", "maxResults": 100, "startAt": 0},
        {"projectKeyOrId": "100", "maxResults": 100, "nextPageToken": "private-page-two"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        "wrong_start",
        "missing_total",
        "duplicate",
        "unbounded",
        "foreign_token",
        "leading_zero",
        "nan",
        "foreign_project",
        "foreign_key",
        "missing_location",
        "boolean_project",
    ],
)
async def test_profile_jira_boards_page_rejects_incomplete_or_forged_pages(
    test_client,
    profile_jira_work,
    monkeypatch,
    invalid,
):
    async def read(*args, **kwargs):
        data = {
            "startAt": 0,
            "maxResults": 100,
            "total": 2,
            "isLast": False,
            "values": [{"id": 1, "name": "APP Board", "location": {"projectId": 100, "projectKey": "APP"}}],
        }
        if invalid == "wrong_start":
            data["startAt"] = 1
        elif invalid == "missing_total":
            data.pop("total")
        elif invalid == "duplicate":
            data["values"].append({"id": 1, "name": "APP Board"})
            data["total"] = 3
        elif invalid == "unbounded":
            data["total"] = 1_000_001
        elif invalid == "leading_zero":
            data["values"][0]["id"] = "01"
        elif invalid == "nan":
            data["values"][0]["size"] = float("nan")
        elif invalid == "foreign_project":
            data["values"][0]["location"]["projectId"] = 200
        elif invalid == "foreign_key":
            data["values"][0]["location"]["projectKey"] = "OTHER"
        elif invalid == "missing_location":
            data["values"][0].pop("location")
        elif invalid == "boolean_project":
            data["values"][0]["location"]["projectId"] = True
        else:
            data["nextPageToken"] = "provider-private-cursor" * 10
        return {"data": data}

    monkeypatch.setattr(profile_authority, "adapter_read", read)
    result = await test_client.post("/internal/v1/profile-discovery/jira-boards-page", json=profile_jira_work["body"])
    assert result.status_code == 404
    assert "provider-private-cursor" not in result.text


@pytest.mark.asyncio
async def test_profile_jira_boards_page_refuses_rotation_after_read(test_client, db_session, profile_jira_work, monkeypatch):
    fixture = profile_jira_work

    async def read(*args, **kwargs):
        fixture["system"].revision = "system-rotated"
        await db_session.commit()
        return {"data": {"startAt": 0, "maxResults": 100, "total": 0, "isLast": True, "values": []}}

    monkeypatch.setattr(profile_authority, "adapter_read", read)
    result = await test_client.post("/internal/v1/profile-discovery/jira-boards-page", json=fixture["body"])
    assert result.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["project_key", "observed_revision"])
async def test_profile_jira_boards_page_refuses_selection_changed_during_read(
    test_client,
    db_session,
    profile_jira_work,
    monkeypatch,
    changed,
):
    fixture = profile_jira_work

    async def read(*args, **kwargs):
        if changed == "project_key":
            fixture["binding"].desired = {**fixture["binding"].desired, "jira_project_key": "OTHER"}
        else:
            fixture["binding"].observed = {**fixture["binding"].observed, "source_revision": "different"}
        await db_session.commit()
        return {
            "data": {
                "startAt": 0,
                "maxResults": 100,
                "total": 1,
                "isLast": True,
                "values": [{"id": 1, "name": "APP Board", "location": {"projectId": 100, "projectKey": "APP"}}],
            }
        }

    monkeypatch.setattr(profile_authority, "adapter_read", read)
    response = await test_client.post("/internal/v1/profile-discovery/jira-boards-page", json=fixture["body"])
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["unshared", "rotated", "missing_tool", "selected_board"])
async def test_profile_jira_boards_page_never_reads_unadmitted_shared_work(
    test_client,
    db_session,
    profile_jira_work,
    monkeypatch,
    changed,
):
    fixture = profile_jira_work
    if changed == "unshared":
        fixture["binding"].desired = {**fixture["binding"].desired, "share_with_product": False}
    elif changed == "rotated":
        fixture["system"].revision = "system-rotated"
    elif changed == "missing_tool":
        fixture["system"].schemas = {}
    else:
        fixture["binding"].desired = {**fixture["binding"].desired, "board_id": 2}
    await db_session.commit()
    called = []

    async def read(*args, **kwargs):
        called.append(True)
        return {"data": {"startAt": 0, "maxResults": 100, "total": 0, "isLast": True, "values": []}}

    monkeypatch.setattr(profile_authority, "adapter_read", read)
    result = await test_client.post("/internal/v1/profile-discovery/jira-boards-page", json=fixture["body"])
    assert result.status_code == 404
    assert not called


@pytest.mark.asyncio
async def test_gitlab_personal_discovery_requires_current_verified_instance_identity(
    test_client, db_session, profile_discovery_work, source_member, monkeypatch
):
    from src.product_onboarding import expanded_provider

    fixture = profile_discovery_work
    credential = UserCredential(
        org_id=source_member.org_id,
        user_id=source_member.user_id,
        service="gitlab",
        credential_type="api_key",
        label="Controlled GitLab",
        secret_arn="fixture-gitlab-personal",
    )
    db_session.add(credential)
    await db_session.flush()
    binding = fixture["binding"]
    binding.installation_id = None
    binding.credential_id = credential.id
    binding.provider = "gitlab"
    binding.kind = "repository"
    binding.namespace = "user:77"
    binding.native_id = "901"
    binding.observed = {**binding.observed, "credential_version": "fixture-version"}
    operation = fixture["operation"]
    revision = hashlib.sha256(json.dumps([str(value) for value in connection_fence(credential)], separators=(",", ":")).encode()).hexdigest()
    operation.request = {**operation.request, "connection_fences": [revision]}
    await db_session.commit()

    calls = []

    async def material(_credential, _secrets, *, expected_version):
        assert expected_version == "fixture-version"
        return "https://gitlab.example.test/api/v4", "fixture-only", "fixture-version"

    async def read(*args, **kwargs):
        calls.append(args)
        return [], {}, "fixture-version"

    monkeypatch.setattr("src.auth.vault_routes.get_secrets_manager", lambda: object())
    monkeypatch.setattr(expanded_provider, "material", material)
    monkeypatch.setattr(expanded_provider, "read", read)
    method, path, query, payload = expanded_provider.expected("gitlab", "user:77", "", 100)
    request = {**fixture["body"], "method": method, "path": path, "query": query}
    assert payload is None
    endpoint = "/internal/v1/profile-discovery/provider-page"

    missing = await test_client.post(endpoint, json=request)
    assert missing.status_code == 404 and calls == []
    identity = UserIdentity(
        org_id=source_member.org_id,
        user_id=source_member.user_id,
        team_id=source_member.team_id,
        provider="gitlab",
        provider_user_id="https://other.example.test#77",
        verification_method="oauth",
        verified_at=datetime.now(UTC),
    )
    db_session.add(identity)
    await db_session.commit()
    foreign_instance = await test_client.post(endpoint, json=request)
    assert foreign_instance.status_code == 404 and calls == []

    identity.provider_user_id = "https://gitlab.example.test#77"
    identity.verified_at = None
    await db_session.commit()
    unverified = await test_client.post(endpoint, json=request)
    assert unverified.status_code == 404 and calls == []

    identity.verified_at = datetime.now(UTC)
    await db_session.commit()
    allowed = await test_client.post(endpoint, json=request)
    assert allowed.status_code == 200 and allowed.json()["work"]["namespace"] == "user:77", allowed.text
    assert len(calls) == 1

    identity.verified_at = None
    await db_session.commit()
    revoked = await test_client.post(endpoint, json=request)
    assert revoked.status_code == 404 and len(calls) == 1

    credential.credential_type = "basic_auth"
    operation.request = {
        **operation.request,
        "connection_fences": [
            hashlib.sha256(json.dumps([str(value) for value in connection_fence(credential)], separators=(",", ":")).encode()).hexdigest()
        ],
    }
    await db_session.commit()
    ineligible = await test_client.post("/internal/v1/profile-discovery/resolve", json=fixture["body"])
    assert ineligible.status_code == 404 and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "denial",
    [
        "foreign_tenant",
        "foreign_service",
        "foreign_attempt",
        "foreign_binding",
        "revoked_binding",
        "revoked_connection",
        "rotated",
        "disabled",
    ],
)
async def test_denied_profile_page_never_calls_provider_or_exposes_checkpoint(test_client, db_session, profile_discovery_work, monkeypatch, denial):
    fixture = profile_discovery_work
    operation = fixture["operation"]
    cursor = "v1:github:100:2"
    operation.receipt = {
        **operation.receipt,
        "cursor": True,
        "page": {
            "contract_version": 1,
            "cursor": cursor,
            "binding_id": fixture["binding"].id,
            "generation": fixture["binding"].generation,
            "namespace": fixture["binding"].namespace,
        },
        "resources": [{"binding_id": fixture["binding"].id, "native_id": "private-record", "name": "private-name"}],
    }
    body = {**fixture["body"], "cursor": cursor}
    if denial == "foreign_tenant":
        body["tenant_id"] = "foreign-tenant"
    elif denial == "foreign_service":
        service = fixture["identity"]["service"]
        fixture["identity"]["service"] = service.model_copy(update={"org_id": "foreign-tenant"})
    elif denial == "foreign_attempt":
        body["attempt_id"] = "another-attempt"
    elif denial == "foreign_binding":
        body["binding_id"] = "another-binding"
    elif denial == "revoked_binding":
        fixture["binding"].status = "revoked"
    elif denial == "revoked_connection":
        fixture["connection"].ownership_disputed = True
    elif denial == "rotated":
        fixture["binding"].generation += 1
    else:
        monkeypatch.setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "false")
    await db_session.commit()
    calls = []

    async def provider(*args, **kwargs):
        calls.append((args, kwargs))
        return [], None, "complete"

    monkeypatch.setattr(profile_authority, "github_resources", provider)
    refused = await test_client.post("/internal/v1/profile-discovery/github-page", json=body)
    assert refused.status_code in (403, 404) and calls == []
    assert "private-name" not in refused.text and "private-record" not in refused.text
    if denial in ("revoked_binding", "revoked_connection", "rotated", "disabled"):
        readback = await test_client.get(f"/v1/products/{operation.product_id}/projects/{operation.project_id}/operations/{operation.id}")
        assert readback.status_code == 200, readback.text
        assert readback.json()["coverage"] == "denied"
        assert not readback.json().get("resources") and "cursor" not in readback.json()
        assert "checkpoint_sha256" not in readback.json()
        assert "private-name" not in readback.text and "private-record" not in readback.text


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["revoked_binding", "revoked_connection"])
async def test_revocation_during_provider_read_discards_late_profile_page(test_client, db_session, profile_discovery_work, monkeypatch, denial):
    fixture = profile_discovery_work
    calls = []

    async def provider(*args, **kwargs):
        calls.append((args, kwargs))
        if denial == "revoked_binding":
            fixture["binding"].status = "revoked"
        else:
            fixture["connection"].ownership_disputed = True
        await db_session.commit()
        return (
            [
                {
                    "kind": "repository",
                    "namespace": fixture["binding"].namespace,
                    "native_id": "private-record",
                    "name": "private-name",
                    "source_revision": "private-revision",
                    "observed_at": datetime.now(UTC).isoformat(),
                }
            ],
            None,
            "complete",
        )

    monkeypatch.setattr(profile_authority, "github_resources", provider)
    refused = await test_client.post("/internal/v1/profile-discovery/github-page", json=fixture["body"])
    assert refused.status_code == 404 and len(calls) == 1
    assert "private-name" not in refused.text and "private-record" not in refused.text
    operation = fixture["operation"]
    readback = await test_client.get(f"/v1/products/{operation.product_id}/projects/{operation.project_id}/operations/{operation.id}")
    assert readback.status_code == 200, readback.text
    assert readback.json()["coverage"] == "denied" and not readback.json().get("resources")
