"""Actual Product unlink evidence is bounded to an earlier WA owner selection."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from l5_services.models.evidence import AssessmentResult
from l5_services.models.result import ResultDetail
from l5_services.models.wa_qualification import WellArchitectedQualification
from sqlalchemy import delete, select

from src.auth.aws_connection_authority import connection_binding
from src.auth.dependencies import get_current_user
from src.l5_integration import GatewayConfigurationBackend
from src.products.models import Product, ProductChange, ProductOperation, ProductSourceBinding
from src.shared.models.organization import TeamMembership
from src.shared.models.vault import ChannelTenantMap
from tests.products import test_l5_wa_qualification as fixtures

consent_fixture = fixtures.consent_fixture
l5_owner = fixtures.l5_owner
selected = fixtures.selected
REAL_AUTHORIZE_SETTINGS = fixtures.REAL_AUTHORIZE_SETTINGS
record = fixtures.record


@pytest.fixture
async def source_selection(selected, test_client, db_session, l5_owner, monkeypatch, request):
    monkeypatch.setattr(GatewayConfigurationBackend, "authorize_settings", REAL_AUTHORIZE_SETTINGS)
    # Only installation ownership resolution is external. Source selection,
    # connection visibility and credential verification use their real readers.
    for module in ("src.products.source_routes", "src.product_onboarding.source_discovery"):
        monkeypatch.setattr(module + ".assert_installation_owned_by", AsyncMock())
    credential = selected["credential"]
    credential.agent_consent = True
    credential.scopes = {**credential.scopes, "source": "imported_role"}
    credential.aws_verified_binding = connection_binding(credential)
    mapping = ChannelTenantMap(
        org_id=l5_owner.org_id, provider="github", provider_scope_id="fixture-org", installation_id="17", ownership_disputed=False
    )
    db_session.add(mapping)
    await db_session.flush()
    source = ProductSourceBinding(
        tenant_id=l5_owner.org_id,
        project_id=selected["project_id"],
        kind="repository",
        provider="github",
        installation_id=mapping.id,
        namespace="fixture-org",
        native_id="27",
        desired={"source_revision": "a" * 40},
        observed={"source_revision": "a" * 40},
        status="healthy",
    )
    environment = ProductSourceBinding(
        tenant_id=l5_owner.org_id,
        project_id=selected["project_id"],
        kind="environment",
        provider="aws",
        credential_id=credential.id,
        namespace="fixture-environment",
        native_id="123456789012",
        desired={},
        status="healthy",
    )
    reference = {
        **selected["reference"],
        "source_connection_id": mapping.id,
        "record_type": "repository_commit",
        "record_id": "27",
        "record_revision": "a" * 40,
    }
    version = selected["configurations"]["well_architected"][1]
    version.settings = {**version.settings, "source_refs": [reference]}
    selected["run"].input = {**selected["run"].input, "sources": [reference]}
    db_session.add_all((source, environment))
    if getattr(request, "param", False):
        run = selected["run"]
        run.state = "succeeded"
        result_id = str(uuid4())
        payload = {
            "result_id": result_id,
            "run_id": run.run_id,
            "revision": 1,
            "kind": "well_architected_result",
            "scope": run.scope,
            "recommendations": ["private-content-marker"],
        }
        result = AssessmentResult(tenant_id=l5_owner.org_id, run_id=run.run_id, result_id=result_id, version=1, payload={"state": "succeeded"})
        detail = ResultDetail(tenant_id=l5_owner.org_id, result_id=result_id, payload=payload)
        db_session.add(result)
        await db_session.flush()
        db_session.add(detail)
        if request.param == "history":
            result.version = 2
            detail.payload = {**payload, "revision": 2}
            earlier_id = str(uuid4())
            earlier = AssessmentResult(tenant_id=l5_owner.org_id, run_id=run.run_id, result_id=earlier_id, version=1, payload={"state": "succeeded"})
            db_session.add(earlier)
            await db_session.flush()
            db_session.add(ResultDetail(tenant_id=l5_owner.org_id, result_id=earlier_id, payload={**payload, "result_id": earlier_id}))
        selected.update(result=result, detail=detail)
    await db_session.commit()
    selected["selection"]["source_revocation_binding_id"] = source.id
    selected["source"] = source
    selected["source_reference"] = reference
    selected["product"] = await db_session.get(Product, (l5_owner.org_id, selected["product_id"]))
    created, _, _ = await record(test_client, selected)
    selected["experiment"] = created.json()
    selected["experiment_path"] = selected["path"] + "/" + created.json()["experiment_id"]
    return selected


async def unlink(client, fixture, db):
    response = await client.delete(
        f"/v1/products/{fixture['product_id']}/projects/{fixture['project_id']}/bindings/{fixture['source'].id}",
        headers={"Idempotency-Key": "selected_source_unlink_01", "If-Profile-Revision": str(fixture["product"].revision)},
    )
    assert response.status_code == 200, response.text
    operation = await db.scalar(select(ProductOperation).where(ProductOperation.operation == "unlinkBinding"))
    change = await db.scalar(select(ProductChange).where(ProductChange.operation_id == operation.id))
    await db.refresh(fixture["source"])
    fixture.update(operation=operation, change=change)
    return fixture["experiment_path"] + "/source-revocation-observations/" + operation.id


async def test_actual_unlink_proves_transition_without_provider_or_content(source_selection, test_client, db_session):
    path = await unlink(test_client, source_selection, db_session)
    denied = await test_client.get(source_selection["experiment_path"] + "/observations")
    assert denied.status_code == 404, denied.text
    response = await test_client.get(path)
    assert response.status_code == 200, response.text
    proof = response.json()
    assert proof["source"] == "durable-product-source-transition"
    assert proof["selection_digest"] == source_selection["experiment"]["selection_digest"]
    assert proof["current_source_access"] == "denied"
    assert proof["generation_before"] + 1 == proof["generation_after"]
    assert proof["operation_id"] == source_selection["operation"].id
    assert proof["change_id"] == source_selection["change"].id
    assert proof["execution_authorized"] is False and proof["provider_effect_count"] is None
    assert "source_ref" not in proof and "snapshot" not in proof and "content" not in proof
    assert response.headers["cache-control"] == "no-store"
    source_selection["source_authority"].assert_not_awaited()
    source_selection["provider"].assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        "pending",
        "failed",
        "unknown",
        "wrong-operation",
        "wrong-actor",
        "wrong-binding",
        "wrong-generation",
        "late-selection",
        "reused-binding",
        "relinked",
        "replacement",
        "missing-change",
        "wrong-change",
        "owner",
        "membership",
        "configuration",
        "run",
        "native-binding",
        "creation",
        "credential",
        "expired",
        "revoked",
    ],
)
async def test_unrelated_or_changed_transition_cannot_borrow_proof(source_selection, test_client, db_session, l5_owner, app, change):
    fixture = source_selection
    path = await unlink(test_client, fixture, db_session)
    operation, source = fixture["operation"], fixture["source"]
    selection = await db_session.get(WellArchitectedQualification, (l5_owner.org_id, fixture["experiment"]["experiment_id"]))
    if change in {"pending", "failed", "unknown"}:
        operation.state = change
    elif change == "wrong-operation":
        operation.operation = "verifyBinding"
    elif change == "wrong-actor":
        operation.actor_id = "foreign"
    elif change == "wrong-binding":
        operation.binding_id = str(uuid4())
    elif change == "wrong-generation":
        operation.binding_generation += 1
    elif change == "late-selection":
        selection.created_at = datetime.now(UTC) + timedelta(seconds=1)
    elif change == "reused-binding":
        source.created_at = datetime.now(UTC)
    elif change == "relinked":
        source.status, source.unlinked_at = "healthy", None
    elif change == "replacement":
        db_session.add(
            ProductSourceBinding(
                tenant_id=source.tenant_id,
                project_id=source.project_id,
                kind=source.kind,
                provider=source.provider,
                installation_id=source.installation_id,
                namespace=source.namespace,
                native_id=source.native_id,
                desired=source.desired,
                observed=source.observed,
                status="healthy",
            )
        )
    elif change == "missing-change":
        await db_session.delete(fixture["change"])
    elif change == "wrong-change":
        fixture["change"].changed_paths = ["/title"]
    elif change == "owner":
        fixture["configurations"]["well_architected"][0].owner_id = "foreign"
    elif change == "membership":
        await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == l5_owner.user_id))
        app.dependency_overrides[get_current_user] = lambda: l5_owner.model_copy(update={"is_platform_admin": True})
    elif change == "configuration":
        fixture["configurations"]["well_architected"][1].hash = "f" * 64
    elif change == "run":
        fixture["run"].input = {"sources": []}
    elif change == "native-binding":
        fixture["binding"].generation += 1
    elif change == "creation":
        fixture["intent"].intent_hash = "f" * 64
    elif change == "credential":
        fixture["credential"].aws_verified_version_id = "rotated"
    elif change == "expired":
        selection.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif change == "revoked":
        selection.revoked_at = datetime.now(UTC)
    await db_session.commit()
    response = await test_client.get(path)
    assert response.status_code in {404, 409, 410, 412}, response.text
    assert "operation_digest" not in response.json() and "source_ref_digest" not in response.json()
    fixture["provider"].assert_not_called()


async def test_current_source_cannot_be_reported_removed_and_late_selection_fails(source_selection, test_client, db_session):
    fixture = source_selection
    response = await test_client.get(fixture["experiment_path"] + "/source-revocation-observations/" + str(uuid4()))
    assert response.status_code == 409
    await unlink(test_client, fixture, db_session)
    response = await test_client.post(fixture["path"] + "/preview", json=fixture["selection"])
    assert response.status_code == 404


@pytest.mark.parametrize("source_selection", [True], indirect=True)
async def test_terminal_result_is_frozen_then_denied_after_actual_unlink(source_selection, test_client, db_session):
    fixture = source_selection
    result_path = fixture["path"].removesuffix("/qualification-experiments") + "/results/" + fixture["result"].result_id
    before = await test_client.get(result_path)
    assert before.status_code == 200, before.text
    path = await unlink(test_client, fixture, db_session)
    proof = await test_client.get(path)
    assert proof.status_code == 200, proof.text
    assert proof.json()["result"] == fixture["experiment"]["selection"]["source_revocation"]["result"]
    assert "private-content-marker" not in proof.text
    for suffix in ("", "/publication-status", "/design-usage/previous-task"):
        denied = await test_client.get(result_path + suffix)
        assert denied.status_code == 404 and denied.json() == {"detail": {"code": "not_found"}}
    manifest_path = fixture["path"].removesuffix("/qualification-experiments") + "/runs/" + fixture["run"].run_id + "/source-manifest"
    denied = await test_client.get(manifest_path)
    assert denied.status_code == 404 and denied.json() == {"detail": {"code": "not_found"}}
    fixture["provider"].assert_not_called()


@pytest.mark.parametrize("source_selection", [True], indirect=True)
@pytest.mark.parametrize("change", ["result-content", "result-revision", "native-generation", "run-revision"])
async def test_changed_terminal_evidence_cannot_borrow_transition(source_selection, test_client, db_session, change):
    fixture = source_selection
    path = await unlink(test_client, fixture, db_session)
    if change == "result-content":
        fixture["detail"].payload = {**fixture["detail"].payload, "recommendations": ["changed"]}
    elif change == "result-revision":
        fixture["result"].version = 2
    elif change == "native-generation":
        fixture["run"].native_job_ref = {"generation_id": "changed"}
    else:
        fixture["run"].revision += 1
    await db_session.commit()
    response = await test_client.get(path)
    assert response.status_code == 409, response.text
    assert "result_digest" not in response.json()


@pytest.mark.parametrize("source_selection", ["history"], indirect=True)
async def test_latest_complete_result_is_frozen_without_discarding_earlier_revision(source_selection, test_client, db_session):
    fixture = source_selection
    frozen = fixture["experiment"]["selection"]["source_revocation"]["result"]
    assert frozen["revision"] == 2 and frozen["result_id"] == fixture["result"].result_id
    response = await test_client.get(await unlink(test_client, fixture, db_session))
    assert response.status_code == 200, response.text
    assert response.json()["result"] == frozen


@pytest.mark.parametrize("source_selection", [True], indirect=True)
async def test_newer_incomplete_result_does_not_fall_back_to_frozen_old_result(source_selection, test_client, db_session):
    fixture = source_selection
    path = await unlink(test_client, fixture, db_session)
    db_session.add(
        AssessmentResult(
            tenant_id=fixture["run"].tenant_id, run_id=fixture["run"].run_id, result_id=str(uuid4()), version=2, payload={"state": "succeeded"}
        )
    )
    await db_session.commit()
    response = await test_client.get(path)
    assert response.status_code == 409, response.text
