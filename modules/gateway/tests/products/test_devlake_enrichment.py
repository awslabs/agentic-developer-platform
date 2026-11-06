"""S10 observations only map to authorized, selected Profile source references."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.product_onboarding.enrichment import selected_source_fact


def observed(kind="repository", namespace="namespace", native_id="42", name="Selected"):
    revision = hashlib.sha256("\x00".join((kind, namespace, native_id, name)).encode()).hexdigest()
    return {
        "kind": kind,
        "namespace": namespace,
        "native_id": native_id,
        "name": name,
        "source_revision": "observation-sha256:" + revision,
        "observed_at": "2026-10-20T00:00:00Z",
        "provenance": "observation_only",
        "description": "private source content",
        "owner": "inferred owner",
    }


def selection(provider="github", kind="repository"):
    return SimpleNamespace(
        tenant_id="tenant",
        project_id="project",
        id="binding",
        generation=3,
        provider=provider,
        namespace="namespace",
        native_id="42",
        installation_id="registered-connection" if provider == "github" else None,
        credential_id="registered-connection" if provider in ("jira", "atlassian-mcp") else None,
        unlinked_at=None,
        status="healthy",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "kind", "path"),
    [
        ("github", "repository", "/configuration/basic/repositories"),
        ("jira", "tracker", "/configuration/basic/trackers"),
    ],
)
async def test_selected_provider_fact_has_only_typed_reference_and_provenance(monkeypatch, provider, kind, path):
    access = AsyncMock(return_value=object())
    monkeypatch.setattr("src.product_onboarding.enrichment.connection_access", access)
    item = observed(kind=kind)
    result = await selected_source_fact(None, SimpleNamespace(org_id="tenant"), selection(provider), item, "complete")
    assert result == {
        "field_path": path,
        "value": {
            "tenant_id": "tenant",
            "project_id": "project",
            "source_connection_id": "registered-connection",
            "record_type": kind,
            "record_id": "42",
            "record_revision": item["source_revision"],
        },
        "display_name": "Selected",
        "provenance": {
            "binding_id": "binding",
            "binding_generation": 3,
            "field_map_version": "devlake-profile-v1",
            "observed_at": "2026-10-20T00:00:00Z",
            "coverage": "complete",
            "revision_basis": "observation_only",
        },
    }
    access.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,kind,namespace,native_id,path",
    [
        ("sonarqube", "quality_project", "/", "team:api", "/configuration/advanced/observability"),
        ("jenkins", "ci_job", "team", "team/api", "/configuration/advanced/pipelines"),
        ("argocd", "deployment_application", "team/ns", "api", "/configuration/advanced/deployments"),
        ("gitlab", "repository", "88", "901", "/configuration/basic/repositories"),
        ("pagerduty", "incident_service", "/", "P123", "/configuration/advanced/support"),
        ("linear", "tracker_team", "/", "team-a", "/configuration/basic/trackers"),
        ("slack", "communication_channel", "/", "C123", "/configuration/basic/collaboration"),
    ],
)
async def test_expanded_selected_reference_uses_native_identity(monkeypatch, provider, kind, namespace, native_id, path):
    from src.product_onboarding.enrichment import source_changes

    monkeypatch.setattr("src.product_onboarding.enrichment.connection_access", AsyncMock(return_value=object()))
    binding = selection(provider, kind)
    binding.namespace, binding.native_id, binding.credential_id = namespace, native_id, "registered-connection"
    item = observed(kind=kind, namespace=namespace, native_id=native_id)
    if provider in ("jenkins", "gitlab"):
        item["full_name"] = native_id if provider == "jenkins" else "person/api"
        item["source_revision"] = (
            "observation-sha256:" + hashlib.sha256("\x00".join((kind, namespace, native_id, item["name"], item["full_name"])).encode()).hexdigest()
        )
    fact = await selected_source_fact(None, SimpleNamespace(org_id="tenant"), binding, item, "complete")
    assert fact and fact["field_path"] == path and fact["value"]["record_id"] == native_id
    updated, proofs, conflicts, suggestions = source_changes({}, [], [fact])
    section, field = path.removeprefix("/configuration/").split("/")
    assert updated[section][field][0]["record_revision"] == item["source_revision"]
    assert len(proofs) == 1 and not conflicts and not suggestions
    if provider == "gitlab":
        assert await selected_source_fact(None, SimpleNamespace(org_id="tenant"), binding, {**item, "full_name": "other/api"}, "complete") is None


@pytest.mark.asyncio
async def test_shared_jira_fact_requires_current_product_access(monkeypatch):
    access = AsyncMock(return_value=object())
    monkeypatch.setattr("src.product_onboarding.enrichment.collection_connection_access", access)
    monkeypatch.setattr("src.product_onboarding.enrichment.connection_access", AsyncMock(side_effect=AssertionError("wrong access path")))
    binding = selection("atlassian-mcp")
    binding.native_id = "100:42"
    binding.desired = {"share_with_product": True, "product_id": "product", "jira_project_id": "100", "board_id": 42}
    item = observed(kind="tracker", native_id="42")
    result = await selected_source_fact(None, SimpleNamespace(org_id="tenant"), binding, item, "complete")
    assert result["field_path"] == "/configuration/basic/trackers"
    assert result["value"]["record_revision"] == item["source_revision"]
    access.assert_awaited_once_with(None, SimpleNamespace(org_id="tenant"), binding, "product")
    access.reset_mock()
    access.return_value = None
    assert await selected_source_fact(None, SimpleNamespace(org_id="tenant"), binding, item, "complete") is None
    access.assert_awaited_once()
    access.reset_mock()
    assert await selected_source_fact(None, SimpleNamespace(org_id="tenant"), binding, {**item, "name": "changed"}, "complete") is None
    access.assert_not_awaited()
    binding.desired = {**binding.desired, "board_id": None}
    assert await selected_source_fact(None, SimpleNamespace(org_id="tenant"), binding, item, "complete") is None
    access.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"namespace": "other"},
        {"native_id": "43"},
        {"provenance": "source_revision"},
        {"source_revision": "observation-sha256:" + "0" * 64},
        {"kind": "finance"},
        {"name": "https://private.example"},
        {"name": "Changed"},
    ],
)
async def test_unselected_or_untrusted_observation_cannot_become_profile_fact(monkeypatch, change):
    access = AsyncMock(return_value=object())
    monkeypatch.setattr("src.product_onboarding.enrichment.connection_access", access)
    assert (
        await selected_source_fact(
            None,
            SimpleNamespace(org_id="tenant"),
            selection(),
            {
                **observed(),
                **change,
            },
            "complete",
        )
        is None
    )
    access.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["foreign", "revoked", "unlinked", "denied", "unavailable"])
async def test_scoped_source_fact_refuses_denial(monkeypatch, denial):
    access = AsyncMock(return_value=None if denial == "denied" else object())
    monkeypatch.setattr("src.product_onboarding.enrichment.connection_access", access)
    binding = selection("jira")
    if denial == "foreign":
        binding.tenant_id = "other"
    elif denial in ("revoked", "unlinked"):
        binding.status = denial
    coverage = "unavailable" if denial == "unavailable" else "partial"
    assert await selected_source_fact(None, SimpleNamespace(org_id="tenant"), binding, observed(kind="tracker"), coverage) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("basic", [{}, {"repositories": []}])
async def test_unique_complete_fact_fills_empty_field_without_copying_private_data(basic):
    from src.product_onboarding.enrichment import source_changes

    item = observed("repository", "namespace", "42", "Selected")
    access = AsyncMock(return_value=object())
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("src.product_onboarding.enrichment.connection_access", access)
        fact = await selected_source_fact(None, SimpleNamespace(org_id="tenant"), selection(), item, "complete")
    configured, provenance, conflicts, suggestions = source_changes({"basic": basic}, [], [fact])
    assert configured["basic"]["repositories"] == [fact["value"]]
    assert provenance[0] == {
        "paths": ["/configuration/basic/repositories"],
        "state": "accepted",
        "method": "connector",
        "source_refs": [fact["value"]],
        "observed_at": item["observed_at"],
        "coverage": "complete",
        "field_map_version": "devlake-profile-v1",
        "revision_basis": "observation_only",
        "binding_id": "binding",
        "binding_generation": 3,
    }
    assert not conflicts and not suggestions
    assert "private source content" not in str((configured, provenance))


@pytest.mark.asyncio
async def test_existing_manual_field_is_never_replaced_even_when_source_changes(monkeypatch):
    from src.product_onboarding.enrichment import source_changes

    monkeypatch.setattr("src.product_onboarding.enrichment.connection_access", AsyncMock(return_value=object()))
    fact = await selected_source_fact(None, SimpleNamespace(org_id="tenant"), selection(), observed(), "complete")
    manually_confirmed = {"basic": {"repositories": [{**fact["value"], "record_revision": "manual-rev"}]}}
    proof = {"paths": [fact["field_path"]], "state": "accepted", "method": "manual", "confirmed_by": "member"}
    updated, provenance, conflicts, suggestions = source_changes(manually_confirmed, [proof], [fact])
    assert updated == manually_confirmed and provenance == [proof]
    assert conflicts == [{"field_path": fact["field_path"], "reason": "confirmed_or_changed"}]
    assert suggestions == []
    updated, provenance, conflicts, suggestions = source_changes({"basic": {}}, [], [fact, fact])
    assert updated == {"basic": {}} and not provenance and not conflicts and len(suggestions) == 2
    partial = {**fact, "provenance": {**fact["provenance"], "coverage": "partial"}}
    updated, provenance, conflicts, suggestions = source_changes({"basic": {}}, [], [partial])
    assert updated == {"basic": {}} and not provenance and not conflicts and suggestions == [partial]


async def setup_enrichment(test_client, db_session, source_member, monkeypatch):
    from datetime import timedelta

    from src.product_onboarding.discovery import connection_fence
    from src.products.models import ConnectorModuleAdmission, ProductOnboardingSession, ProductOperation, ProductSourceBinding
    from src.shared.models.base import utcnow
    from src.shared.models.vault import ChannelTenantMap
    from tests.products.test_i08_api import mutation, setup_source

    monkeypatch.setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
    path, selected = await setup_source(test_client, db_session, source_member)
    created = await test_client.post(path, json=selected, headers=mutation(1, "enrichment_binding_123"))
    assert created.status_code == 201, created.text
    root = path.removesuffix("/bindings")
    started = await test_client.post(root + "/onboarding-sessions", json={}, headers=mutation(2, "enrichment_session_123"))
    assert started.status_code == 201, started.text
    binding = await db_session.get(ProductSourceBinding, (source_member.org_id, created.json()["id"]))
    connection = await db_session.get(ChannelTenantMap, binding.installation_id)
    connection_pin = hashlib.sha256(json.dumps([str(value) for value in connection_fence(connection)], separators=(",", ":")).encode()).hexdigest()
    item = observed("repository", selected["namespace"], selected["native_id"], "Repo")
    now = utcnow()
    item["observed_at"] = now.isoformat().replace("+00:00", "Z")
    binding.status = "healthy"
    binding.observed = {
        "source_revision": "verified-provider-revision",
        "namespace": binding.namespace,
        "native_id": binding.native_id,
        "observed_at": now.isoformat(),
        "stale_after": (now + timedelta(minutes=15)).isoformat(),
    }
    session = await db_session.get(ProductOnboardingSession, (source_member.org_id, started.json()["id"]))
    operation = ProductOperation(
        tenant_id=source_member.org_id,
        actor_id=session.actor_user_id,
        product_id=root.split("/")[3],
        project_id=root.split("/")[5],
        session_id=session.id,
        operation="discoverSources",
        state="succeeded",
        key_hash="enrichment-discovery-key",
        request_hash="discovery-hash",
        request={
            "adapter": "devlake",
            "binding_ids": [binding.id],
            "generations": [binding.generation],
            "source_revisions": [binding.observed["source_revision"]],
            "connection_fences": [connection_pin],
        },
        receipt={"coverage": "complete", "resources": [{**item, "binding_id": binding.id}]},
        expected_revision=2,
        expires_at=now + timedelta(hours=1),
    )
    db_session.add(ConnectorModuleAdmission(tenant_id=source_member.org_id, enabled=True))
    db_session.add(operation)
    await db_session.commit()
    return root, session.id, operation.id, item


@pytest.mark.asyncio
async def test_connector_discovery_preserves_verified_revision_and_observation_for_apply(
    test_client,
    db_session,
    source_member,
    monkeypatch,
):
    from sqlalchemy import select

    from src.products.models import ProductOperation, ProductSourceBinding
    from tests.products.test_i08_api import mutation

    root, session_id, _, item = await setup_enrichment(test_client, db_session, source_member, monkeypatch)
    binding = await db_session.scalar(select(ProductSourceBinding).where(ProductSourceBinding.tenant_id == source_member.org_id))
    monkeypatch.setenv("CONNECTOR_PROFILE_DISCOVERY_URL", "http://connector-api.insights.svc.cluster.local:8080/v1/profile-discovery")
    monkeypatch.setenv("CONNECTOR_PROFILE_DISCOVERY_CONSUMER", "registered-connector-api")

    async def connector_page(db, actor, selected, connection, page, limit, operation):
        assert selected.id == binding.id and operation.request["source_revisions"] == ["verified-provider-revision"]
        return [item], None, "complete"

    monkeypatch.setattr("src.product_onboarding.discovery.discover_devlake_page", connector_page)
    discovery = await test_client.post(
        root + f"/onboarding-sessions/{session_id}/discoveries",
        json={"binding_ids": [binding.id], "adapter": "devlake"},
        headers=mutation(2, "enrichment_connector_discovery_123"),
    )
    assert discovery.status_code == 202, discovery.text
    operation = await db_session.get(ProductOperation, (source_member.org_id, discovery.json()["id"]))
    await db_session.refresh(operation)
    assert operation.receipt["resources"][0]["provenance"] == "observation_only"
    assert operation.receipt["resources"][0]["source_revision"] == item["source_revision"]
    applied = await test_client.post(
        root + f"/onboarding-sessions/{session_id}/source-enrichment",
        json={"operation_id": operation.id},
        headers=mutation(2, "enrichment_connector_apply_123"),
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["revision"] == 3 and len(applied.json()["applied"]) == 1


@pytest.mark.asyncio
async def test_apply_enrichment_persists_sourced_reference_and_rejects_stale_revision(test_client, db_session, source_member, monkeypatch):
    from src.products.models import Project
    from tests.products.test_i08_api import mutation

    root, session_id, operation_id, item = await setup_enrichment(test_client, db_session, source_member, monkeypatch)
    endpoint = root + f"/onboarding-sessions/{session_id}/source-enrichment"
    response = await test_client.post(endpoint, json={"operation_id": operation_id}, headers=mutation(2, "enrichment_apply_123"))
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 3 and len(response.json()["applied"]) == 1
    assert not response.json()["conflicts"] and not response.json()["suggestions"]
    project = await db_session.get(Project, (source_member.org_id, root.split("/")[5]))
    await db_session.refresh(project)
    assert project.configuration["basic"]["repositories"][0]["record_revision"] == item["source_revision"]
    assert project.provenance[-1]["field_map_version"] == "devlake-profile-v1"
    assert "private source content" not in str(project.configuration)
    readback = await test_client.get(root)
    assert readback.status_code == 200, readback.text
    assert readback.json()["configuration"]["basic"]["repositories"][0]["record_revision"] == item["source_revision"]
    repeated = await test_client.post(endpoint, json={"operation_id": operation_id}, headers=mutation(2, "enrichment_apply_123"))
    assert repeated.status_code == 200 and repeated.json() == response.json()
    stale = await test_client.post(endpoint, json={"operation_id": operation_id}, headers=mutation(2, "enrichment_stale_123"))
    assert stale.status_code == 412 and stale.json()["detail"] == {"code": "stale_revision"}


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_apply_enrichment_reports_conflict_without_exposing_manual_source(test_client, db_session, source_member, monkeypatch, empty):
    from src.products.models import Product, Project
    from tests.products.test_i08_api import mutation

    root, session_id, operation_id, _ = await setup_enrichment(test_client, db_session, source_member, monkeypatch)
    project = await db_session.get(Project, (source_member.org_id, root.split("/")[5]))
    product = await db_session.get(Product, (source_member.org_id, root.split("/")[3]))
    before = {
        **project.configuration,
        "basic": {
            **(project.configuration.get("basic") or {}),
            "repositories": []
            if empty
            else [
                {
                    "tenant_id": source_member.org_id,
                    "project_id": project.id,
                    "source_connection_id": "private-manual-source",
                    "record_type": "repository",
                    "record_id": "private-record",
                    "record_revision": "confirmed-rev",
                }
            ],
        },
    }
    project.configuration = before
    project.provenance = [{"paths": ["/configuration/basic/repositories"], "state": "accepted", "method": "manual"}]
    product.revision = 3
    await db_session.commit()
    endpoint = root + f"/onboarding-sessions/{session_id}/source-enrichment"
    stale = await test_client.post(endpoint, json={"operation_id": operation_id}, headers=mutation(2, "enrichment_conflict_stale_123"))
    assert stale.status_code == 412
    response = await test_client.post(endpoint, json={"operation_id": operation_id}, headers=mutation(3, "enrichment_conflict_read_123"))
    assert response.status_code == 200, response.text
    assert response.json() == {
        "revision": 3,
        "applied": [],
        "conflicts": [
            {
                "field_path": "/configuration/basic/repositories",
                "reason": "confirmed_or_changed",
            }
        ],
        "suggestions": [],
        "coverage": "complete",
    }
    assert "private-record" not in response.text and "private-manual-source" not in response.text
    await db_session.refresh(project)
    assert project.configuration == before and project.provenance[0]["method"] == "manual"
    await db_session.refresh(product)
    assert product.revision == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["step", "project"])
@pytest.mark.parametrize("initial,clear", [("enriched", []), ("enriched", None), ("empty", []), ("missing", [])])
async def test_manual_source_clear_survives_enrichment_refresh(test_client, db_session, source_member, monkeypatch, writer, initial, clear):
    from src.products.models import Project
    from tests.products.test_i08_api import mutation

    root, session_id, operation_id, _ = await setup_enrichment(test_client, db_session, source_member, monkeypatch)
    endpoint = root + f"/onboarding-sessions/{session_id}/source-enrichment"
    project = await db_session.get(Project, (source_member.org_id, root.split("/")[5]))
    revision = 2
    if initial == "enriched":
        applied = await test_client.post(endpoint, json={"operation_id": operation_id}, headers=mutation(revision, "enrichment_before_clear"))
        assert applied.status_code == 200, applied.text
        revision = applied.json()["revision"]
        assert applied.json()["applied"]
    elif initial == "empty":
        project.configuration = {"basic": {"repositories": []}}
        await db_session.commit()

    configuration = {"basic": {"repositories": clear} if clear is not None else {}}
    if writer == "step":
        save = test_client.put
        save_endpoint = root + f"/onboarding-sessions/{session_id}/steps/basic"
        body = {"action": "save", "configuration": configuration}
    else:
        save = test_client.patch
        save_endpoint = root + "/configuration"
        body = {"configuration": configuration}
    saved = await save(save_endpoint, json=body, headers=mutation(revision, "manual_source_clear"))
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == revision + 1
    replay = await save(save_endpoint, json=body, headers=mutation(revision, "manual_source_clear"))
    assert replay.status_code == 200 and replay.json()["revision"] == revision + 1
    revision = saved.json()["revision"]
    repeated = await save(save_endpoint, json=body, headers=mutation(revision, "manual_source_clear_again"))
    assert repeated.status_code == 200 and repeated.json()["revision"] == revision

    await db_session.refresh(project)
    before = project.configuration
    proofs = project.provenance
    path = "/configuration/basic/repositories"
    proof = next(proof for proof in proofs if path in proof.get("paths", []))
    assert proof["state"] == "accepted" and proof["method"] == "manual"
    assert proof["confirmed_by"] == {"kind": "user", "id": source_member.user_id}
    refreshed = await test_client.post(endpoint, json={"operation_id": operation_id}, headers=mutation(revision, "enrichment_after_clear"))
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["revision"] == revision and refreshed.json()["applied"] == []
    assert refreshed.json()["conflicts"] == [{"field_path": path, "reason": "confirmed_or_changed"}]
    await db_session.refresh(project)
    assert project.configuration == before and project.provenance == proofs
    assert not project.configuration["basic"].get("repositories")
    readback = await test_client.get(root)
    assert readback.status_code == 200
    assert not readback.json()["configuration"]["basic"].get("repositories")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,kind,field", [("github", "repository", "repositories"), ("jira", "tracker", "trackers")])
@pytest.mark.parametrize("method,confirmed", [("manual", False), ("connector", True)])
async def test_confirmed_empty_source_fields_are_protected(monkeypatch, provider, kind, field, method, confirmed):
    from src.product_onboarding.enrichment import source_changes

    monkeypatch.setattr("src.product_onboarding.enrichment.connection_access", AsyncMock(return_value=object()))
    fact = await selected_source_fact(None, SimpleNamespace(org_id="tenant"), selection(provider), observed(kind=kind), "complete")
    configuration = {"basic": {field: []}}
    proof = {"paths": [fact["field_path"]], "state": "accepted", "method": method}
    if confirmed:
        proof["confirmed_by"] = {"kind": "user", "id": "member"}
    updated, proofs, conflicts, suggestions = source_changes(configuration, [proof], [fact])
    assert updated == configuration and proofs == [proof]
    assert conflicts == [{"field_path": fact["field_path"], "reason": "confirmed_or_changed"}]
    assert suggestions == []


@pytest.mark.asyncio
async def test_partial_discovery_cannot_autofill_empty_profile(test_client, db_session, source_member, monkeypatch):
    from src.products.models import Product, ProductOperation, Project
    from tests.products.test_i08_api import mutation

    root, session_id, operation_id, _ = await setup_enrichment(test_client, db_session, source_member, monkeypatch)
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    operation.receipt = {**operation.receipt, "coverage": "partial"}
    await db_session.commit()
    endpoint = root + f"/onboarding-sessions/{session_id}/source-enrichment"
    result = await test_client.post(endpoint, json={"operation_id": operation_id}, headers=mutation(2, "enrichment_partial_123"))
    assert result.status_code == 200, result.text
    assert result.json()["revision"] == 2 and result.json()["applied"] == [] and result.json()["conflicts"] == []
    assert result.json()["coverage"] == "partial" and len(result.json()["suggestions"]) == 1
    project = await db_session.get(Project, (source_member.org_id, root.split("/")[5]))
    product = await db_session.get(Product, (source_member.org_id, root.split("/")[3]))
    await db_session.refresh(project)
    await db_session.refresh(product)
    assert not (project.configuration.get("basic") or {}).get("repositories") and product.revision == 2


@pytest.mark.asyncio
async def test_apply_replay_redacts_after_connection_revocation(test_client, db_session, source_member, monkeypatch):
    from src.shared.models.vault import ChannelTenantMap
    from tests.products.test_i08_api import mutation

    root, session_id, operation_id, _ = await setup_enrichment(test_client, db_session, source_member, monkeypatch)
    endpoint = root + f"/onboarding-sessions/{session_id}/source-enrichment"
    first = await test_client.post(endpoint, json={"operation_id": operation_id}, headers=mutation(2, "enrichment_replay_123"))
    assert first.status_code == 200, first.text
    connection = await db_session.get(ChannelTenantMap, "run-owned-connection")
    connection.ownership_disputed = True
    await db_session.commit()
    replay = await test_client.post(endpoint, json={"operation_id": operation_id}, headers=mutation(2, "enrichment_replay_123"))
    assert replay.status_code == 403 and replay.json()["detail"] == {"code": "source_access_denied"}
    assert "record_revision" not in replay.text and "binding_generation" not in replay.text


@pytest.mark.asyncio
async def test_apply_rejects_changed_source_evidence_without_profile_write(test_client, db_session, source_member, monkeypatch):
    from sqlalchemy import select

    from src.products.models import Product, ProductSourceBinding, Project
    from tests.products.test_i08_api import mutation

    root, session_id, operation_id, _ = await setup_enrichment(test_client, db_session, source_member, monkeypatch)
    binding = await db_session.scalar(select(ProductSourceBinding).where(ProductSourceBinding.tenant_id == source_member.org_id))
    binding.observed = {**binding.observed, "source_revision": "new-source-revision"}
    await db_session.commit()
    endpoint = root + f"/onboarding-sessions/{session_id}/source-enrichment"
    response = await test_client.post(endpoint, json={"operation_id": operation_id}, headers=mutation(2, "enrichment_changed_123"))
    assert response.status_code == 409 and response.json()["detail"] == {"code": "source_changed"}
    project = await db_session.get(Project, (source_member.org_id, root.split("/")[5]))
    product = await db_session.get(Product, (source_member.org_id, root.split("/")[3]))
    await db_session.refresh(project)
    await db_session.refresh(product)
    assert not (project.configuration.get("basic") or {}).get("repositories") and product.revision == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_pin", ["missing_revision", "changed_revision", "changed_connection"])
async def test_apply_refuses_missing_or_changed_verified_revision(test_client, db_session, source_member, monkeypatch, invalid_pin):
    from src.products.models import Product, ProductOperation, Project
    from tests.products.test_i08_api import mutation

    root, session_id, operation_id, _ = await setup_enrichment(test_client, db_session, source_member, monkeypatch)
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    if invalid_pin == "changed_connection":
        operation.request = {**operation.request, "connection_fences": ["0" * 64]}
    else:
        operation.request = {**operation.request, "source_revisions": [] if invalid_pin == "missing_revision" else ["different-provider-revision"]}
    await db_session.commit()
    endpoint = root + f"/onboarding-sessions/{session_id}/source-enrichment"
    response = await test_client.post(endpoint, json={"operation_id": operation_id}, headers=mutation(2, "enrichment_invalid_pin_123"))
    assert response.status_code in (409, 403)
    project = await db_session.get(Project, (source_member.org_id, root.split("/")[5]))
    product = await db_session.get(Product, (source_member.org_id, root.split("/")[3]))
    await db_session.refresh(project)
    await db_session.refresh(product)
    assert not (project.configuration.get("basic") or {}).get("repositories") and product.revision == 2


@pytest.mark.asyncio
async def test_resumed_partial_discovery_is_scoped_and_not_complete(test_client, db_session, source_member, monkeypatch):
    from sqlalchemy import func, select

    from src.products.models import ProductOnboardingSession, ProductOperation, ProductSourceBinding
    from src.shared.models.vault import ChannelTenantMap

    root, session_id, operation_id, item = await setup_enrichment(test_client, db_session, source_member, monkeypatch)
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    checkpoint = {
        "contract_version": 1,
        "cursor": "v1:github:100:2",
        "binding_id": operation.request["binding_ids"][0],
        "generation": operation.request["generations"][0],
        "namespace": item["namespace"],
    }
    operation.receipt = {**operation.receipt, "coverage": "partial", "cursor": True, "page": checkpoint, "index": 0}
    assert "checkpoint_sha256" not in operation.receipt
    await db_session.commit()
    session_path = root + f"/onboarding-sessions/{session_id}"
    first = await test_client.get(session_path)
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "open"
    assert first.json()["discoveries"] == [
        {
            "id": operation_id,
            "state": "succeeded",
            "coverage": "partial",
            "complete": False,
            "resource_count": 1,
            "cursor": operation_id,
            "binding_ids": operation.request["binding_ids"],
            "adapter": "devlake",
        }
    ]
    assert item["native_id"] not in first.text and item["name"] not in first.text
    checkpoint_readback = await test_client.get(root + "/operations/" + operation_id)
    assert checkpoint_readback.json()["cursor"] == operation_id
    assert (
        checkpoint_readback.json()["checkpoint_sha256"]
        == hashlib.sha256(json.dumps(checkpoint, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )
    assert checkpoint["cursor"] not in checkpoint_readback.text

    operation.receipt = {**operation.receipt, "consumed": True}
    await db_session.commit()
    consumed_readback = await test_client.get(root + "/operations/" + operation_id)
    assert "cursor" not in consumed_readback.json()
    assert consumed_readback.json()["checkpoint_sha256"] == checkpoint_readback.json()["checkpoint_sha256"]

    monkeypatch.setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "false")
    off = await test_client.get(session_path)
    assert off.json()["discoveries"] == [
        {
            "id": operation_id,
            "state": "cancelled",
            "coverage": "denied",
            "complete": False,
            "resource_count": 0,
        }
    ]
    readback = await test_client.get(root + "/operations/" + operation_id)
    assert readback.json()["state"] == "cancelled" and readback.json()["coverage"] == "denied"
    assert "cursor" not in readback.json() and "checkpoint_sha256" not in readback.json()
    assert readback.json().get("resources") == []
    assert item["native_id"] not in readback.text

    monkeypatch.setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
    mapping = await db_session.get(ChannelTenantMap, "run-owned-connection")
    mapping.ownership_disputed = True
    await db_session.commit()
    revoked = await test_client.get(session_path)
    assert revoked.json()["discoveries"][0]["coverage"] == "denied"
    assert "cursor" not in revoked.json()["discoveries"][0] and item["native_id"] not in revoked.text
    denied = await test_client.get(root + "/operations/" + operation_id)
    assert denied.json()["coverage"] == "denied" and item["native_id"] not in denied.text
    assert "checkpoint_sha256" not in denied.json()
    assert await db_session.scalar(select(func.count(ProductSourceBinding.id))) == 1
    session = await db_session.get(ProductOnboardingSession, (source_member.org_id, session_id))
    session.actor_user_id = "another-member"
    await db_session.commit()
    assert (await test_client.get(session_path)).status_code == 404


@pytest.mark.parametrize(
    "change", ["missing_page", "foreign_binding", "old_generation", "blank_cursor", "oversized_cursor", "native", "complete", "denied"]
)
def test_discovery_checkpoint_fingerprint_refuses_invalid_or_ineligible_page(change):
    from src.product_onboarding.discovery import view

    page = {"contract_version": 1, "cursor": "v1:github:100:2", "binding_id": "owned-binding", "generation": 2, "namespace": "owned-namespace"}
    operation = SimpleNamespace(
        id="owned-operation",
        tenant_id="tenant",
        product_id="product",
        project_id="project",
        operation="discoverSources",
        state="succeeded",
        request={"adapter": "devlake", "binding_ids": ["owned-binding"], "generations": [2]},
        receipt={"coverage": "partial", "cursor": True, "index": 0, "page": page},
    )
    if change == "missing_page":
        del operation.receipt["page"]
    elif change == "foreign_binding":
        page["binding_id"] = "foreign-binding"
    elif change == "old_generation":
        page["generation"] = 1
    elif change == "blank_cursor":
        page["cursor"] = ""
    elif change == "oversized_cursor":
        page["cursor"] = "x" * 513
    elif change == "native":
        operation.request["adapter"] = "native"
    elif change == "complete":
        operation.receipt["coverage"] = "complete"
    else:
        operation.receipt["coverage"] = "denied"

    assert "checkpoint_sha256" not in view(operation, 4)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["consumer", "profile_revision", "scope", "generation", "finished", "cancelled", "unleased", "product"])
async def test_connector_resume_refuses_stale_checkpoint_before_provider_access(test_client, db_session, source_member, monkeypatch, changed):
    from src.products.models import ProductOperation, ProductSourceBinding
    from tests.products.test_i08_api import mutation

    root, session_id, operation_id, item = await setup_enrichment(test_client, db_session, source_member, monkeypatch)
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    binding = await db_session.get(ProductSourceBinding, (source_member.org_id, operation.request["binding_ids"][0]))
    operation.request = {**operation.request, "consumer": "registered-connector-api"}
    page = {
        "contract_version": 1,
        "cursor": "v1:github:100:2",
        "binding_id": binding.id,
        "generation": binding.generation,
        "namespace": binding.namespace,
    }
    operation.receipt = {**operation.receipt, "coverage": "partial", "cursor": True, "index": 0, "page": page}
    if changed == "consumer":
        operation.request = {**operation.request, "consumer": "retired-connector"}
    elif changed == "profile_revision":
        operation.expected_revision = 1
    elif changed == "scope":
        operation.receipt = {**operation.receipt, "page": {**page, "namespace": "foreign-namespace"}}
    elif changed == "generation":
        operation.receipt = {**operation.receipt, "page": {**page, "generation": page["generation"] - 1}}
    elif changed == "finished":
        operation.receipt = {**operation.receipt, "coverage": "complete"}
    elif changed == "cancelled":
        operation.state = "cancelled"
    elif changed == "unleased":
        operation.state = "running"
        operation.lease_until = None
    else:
        operation.product_id = "another-product"
    await db_session.commit()
    monkeypatch.setenv("CONNECTOR_PROFILE_DISCOVERY_URL", "http://connector-api.insights.svc.cluster.local:8080/v1/profile-discovery")
    monkeypatch.setenv("CONNECTOR_PROFILE_DISCOVERY_CONSUMER", "registered-connector-api")
    calls = []

    async def connector_page(*args):
        calls.append(args)
        return [item], None, "complete"

    monkeypatch.setattr("src.product_onboarding.discovery.discover_devlake_page", connector_page)
    resumed = await test_client.post(
        root + f"/onboarding-sessions/{session_id}/discoveries",
        json={"binding_ids": [binding.id], "adapter": "devlake", "cursor": operation_id},
        headers=mutation(2, "stale_connector_resume_123"),
    )
    assert resumed.status_code == 400 and resumed.json()["detail"] == {"code": "invalid_cursor"}
    await db_session.refresh(operation)
    assert operation.receipt.get("consumed") is not True and calls == []


@pytest.mark.asyncio
async def test_connector_resume_uses_bound_checkpoint_after_restart(test_client, db_session, test_engine, source_member, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.app import create_app
    from src.auth.dependencies import get_current_user
    from src.products.models import ProductOperation, ProductSourceBinding
    from src.shared.database import get_db
    from tests.products.test_i08_api import mutation

    root, session_id, operation_id, item = await setup_enrichment(test_client, db_session, source_member, monkeypatch)
    operation = await db_session.get(ProductOperation, (source_member.org_id, operation_id))
    binding = await db_session.get(ProductSourceBinding, (source_member.org_id, operation.request["binding_ids"][0]))
    operation.request = {**operation.request, "consumer": "registered-connector-api"}
    page = {
        "contract_version": 1,
        "cursor": "v1:github:100:2",
        "binding_id": binding.id,
        "generation": binding.generation,
        "namespace": binding.namespace,
    }
    operation.receipt = {"coverage": "partial", "cursor": True, "index": 0, "page": page, "seen": [], "seen_cursors": [], "resources": []}
    await db_session.commit()
    monkeypatch.setenv("CONNECTOR_PROFILE_DISCOVERY_URL", "http://connector-api.insights.svc.cluster.local:8080/v1/profile-discovery")
    monkeypatch.setenv("CONNECTOR_PROFILE_DISCOVERY_CONSUMER", "registered-connector-api")
    calls = []

    async def connector_page(db, actor, selected, connection, current_page, limit, attempt):
        calls.append((current_page, attempt.request["connection_fences"], selected.generation))
        return [item], None, "complete"

    monkeypatch.setattr("src.product_onboarding.discovery.discover_devlake_page", connector_page)
    restarted_app = create_app()
    restarted_app.dependency_overrides[get_current_user] = lambda: source_member

    async def restored_db():
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as session:
            yield session

    restarted_app.dependency_overrides[get_db] = restored_db
    async with AsyncClient(transport=ASGITransport(app=restarted_app), base_url="http://test") as restarted:
        resumed = await restarted.post(
            root + f"/onboarding-sessions/{session_id}/discoveries",
            json={"binding_ids": [binding.id], "adapter": "devlake", "cursor": operation_id},
            headers=mutation(2, "bound_connector_resume_123"),
        )
    assert resumed.status_code == 202, resumed.text
    assert resumed.json()["coverage"] == "complete" and len(calls) == 1
    assert calls[0] == (page, operation.request["connection_fences"], binding.generation)
    await db_session.refresh(operation)
    assert operation.receipt["consumed"] is True
