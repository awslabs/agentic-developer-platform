"""Mounted observation-only selections use real current owner and durable rows."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from l5_services.api.bindings import _intent_hash
from l5_services.api.configuration import view
from l5_services.models.binding import Binding
from l5_services.models.event import EventReceipt
from l5_services.models.run import Run
from l5_services.models.setup import SetupIntent, SetupWork
from l5_services.models.source_retention import SourceRetention
from l5_services.models.wa_qualification import WellArchitectedQualification
from sqlalchemy import delete, func, select, update

from src.auth.aws_connection_authority import connection_binding
from src.auth.dependencies import get_current_user
from src.l5_integration import GatewayConfigurationBackend
from src.products.models import ProductProjectBinding, ProductSourceBinding
from src.shared.models.organization import TeamMembership
from src.shared.models.vault import ChannelTenantMap, CredentialServiceDelegation
from tests.products import test_l5_owner_delegation as owner_fixtures

consent_fixture = owner_fixtures.consent_fixture
l5_owner = owner_fixtures.l5_owner
REAL_AUTHORIZE_SETTINGS = GatewayConfigurationBackend.authorize_settings


@pytest.fixture
async def selected(consent_fixture, db_session, l5_owner, monkeypatch):
    fixture = consent_fixture
    monkeypatch.setenv("L5_WA_QUALIFICATION_OBSERVATIONS_ENABLED", "true")
    configuration, version = fixture["configurations"]["well_architected"]
    scope = view(configuration, version)["scope"]
    binding = Binding(
        tenant_id=l5_owner.org_id,
        binding_id=str(uuid4()),
        configuration_id=configuration.configuration_id,
        product_id=fixture["product_id"],
        project_id=fixture["project_id"],
        service_id="well_architected",
        scope=scope,
        revision=1,
        generation=1,
        state="ready",
        ownership="adp_created",
        connection_ref=fixture["reference"],
        connection_key="a" * 64,
        native_resource_ref={
            **fixture["reference"],
            "record_type": "agent_profile",
            "record_id": "arn:aws:wellarchitected:us-east-1:123456789012:agent-profile/fixture",
        },
        native_key="b" * 64,
        provider_identity={"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Fixture", "region": "us-east-1"},
        readiness=[],
        gaps=[],
    )
    intent = SetupIntent(
        tenant_id=l5_owner.org_id,
        operation_id=str(uuid4()),
        product_id=fixture["product_id"],
        project_id=fixture["project_id"],
        service_id="well_architected",
        configuration_id=configuration.configuration_id,
        actor_id=l5_owner.user_id,
        idempotency_key="original_fixture_setup",
        scope=scope,
        request={"action": "create", "configuration_revision": 1},
        binding_generation=1,
        authorization={"actor_id": l5_owner.user_id},
        intent_hash="",
    )
    intent.intent_hash = _intent_hash(intent)
    binding.creation_receipt = {
        "operation_id": intent.operation_id,
        "intent_hash": intent.intent_hash,
        "native_resource_ref": binding.native_resource_ref,
        "provider_identity": binding.provider_identity,
    }
    run = Run(
        tenant_id=l5_owner.org_id,
        run_id=str(uuid4()),
        product_id=fixture["product_id"],
        project_id=fixture["project_id"],
        service_id="well_architected",
        configuration_id=configuration.configuration_id,
        configuration_revision=1,
        binding_id=binding.binding_id,
        binding_generation=1,
        trigger_id="manual",
        revision=1,
        input={"sources": [], "capability": "architecture_review"},
        admission={"configuration_hash": version.hash, "scope": scope},
        scope=scope,
        state="outcome_unknown",
        provider_state="running",
        deadline_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    now = datetime.now(UTC)
    artifact = {"artifact_id": "fixture/source.zip", "version": "v1", "sha256": "c" * 64, "bytes": 100}
    hold = SourceRetention(
        tenant_id=l5_owner.org_id,
        project_id=fixture["project_id"],
        run_id=run.run_id,
        artifact_id=artifact["artifact_id"],
        artifact_ref=artifact,
        required_until=now + timedelta(minutes=30),
        policy_expires_at=now + timedelta(hours=1),
        reason="remote_generation",
    )
    event = EventReceipt(
        tenant_id=l5_owner.org_id,
        product_id=fixture["product_id"],
        project_id=fixture["project_id"],
        service_id="well_architected",
        trigger_id="selected-tick",
        event_id="schedule:2026-10-31T00:00:00Z",
        idempotency_key="d" * 64,
        event_kind="schedule",
        configuration_id=configuration.configuration_id,
        configuration_revision=1,
        state="rejected",
        reason="trigger_disabled",
        run_id=None,
        received_at=now,
        recovery_attempts=0,
    )
    db_session.add_all([binding, intent, run])
    await db_session.flush()
    db_session.add_all([hold, event])
    await db_session.commit()
    no_provider = Mock(side_effect=AssertionError("observation endpoints must not call a provider"))
    monkeypatch.setattr("boto3.client", no_provider)
    return {
        **fixture,
        "path": fixture["path"] + "/well_architected/qualification-experiments",
        "binding": binding,
        "run": run,
        "hold": hold,
        "event": event,
        "intent": intent,
        "provider": no_provider,
        "selection": {
            "binding_id": binding.binding_id,
            "run_id": run.run_id,
            "events": [{"trigger_id": event.trigger_id, "event_id": event.event_id}],
        },
    }


async def record(client, selected):
    preview = await client.post(selected["path"] + "/preview", json=selected["selection"])
    assert preview.status_code == 200, preview.text
    body = {
        "selection": selected["selection"],
        "selection_digest": preview.json()["selection_digest"],
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    headers = {"If-Match": preview.headers["etag"], "Idempotency-Key": "exact_observation_fixture_01"}
    created = await client.post(selected["path"], json=body, headers=headers)
    assert created.status_code == 200, created.text
    return created, body, headers


async def test_actual_hold_and_disabled_event_readbacks_without_authority_or_side_effects(selected, test_client, db_session):
    created, body, headers = await record(test_client, selected)
    value = created.json()
    path = selected["path"] + "/" + value["experiment_id"]
    observed = await test_client.get(path + "/observations")
    assert observed.status_code == 200, observed.text
    data = observed.json()
    assert data["source"] == "durable-runtime" and data["run"]["state"] == "outcome_unknown"
    assert data["events"][0]["state"] == "rejected" and data["events"][0]["run_id"] is None
    assert data["holds"][0]["artifact"] == selected["hold"].artifact_ref
    assert data["holds"][0]["released_at"] is None
    assert data["execution_authorized"] is value["execution_authorized"] is False
    assert "source_object_presence_not_read" in data["limitations"] and "provider_send_count_not_observed" in data["limitations"]
    replay = await test_client.post(selected["path"], json=body, headers=headers)
    assert replay.json() == value
    assert (await test_client.get(path)).json() == value
    assert await db_session.scalar(select(func.count()).select_from(WellArchitectedQualification)) == 1
    assert await db_session.scalar(select(func.count()).select_from(CredentialServiceDelegation)) == 0
    assert await db_session.scalar(select(func.count()).select_from(SetupWork)) == 0
    assert await db_session.scalar(select(func.count()).select_from(Run)) == 1
    selected["provider"].assert_not_called()


async def test_revocation_is_cas_and_cannot_be_replayed_or_call_source_revocation(selected, test_client, db_session, monkeypatch):
    created, body, headers = await record(test_client, selected)
    path = selected["path"] + "/" + created.json()["experiment_id"]
    assert (await test_client.delete(path)).status_code == 428
    assert (await test_client.delete(path, headers={"If-Match": '"2"'})).status_code == 412
    monkeypatch.delenv("L5_WA_QUALIFICATION_OBSERVATIONS_ENABLED")
    revoked = await test_client.delete(path, headers={"If-Match": created.headers["etag"]})
    assert revoked.status_code == 200 and revoked.json()["active"] is False
    assert (await test_client.get(path + "/observations")).status_code == 410
    monkeypatch.setenv("L5_WA_QUALIFICATION_OBSERVATIONS_ENABLED", "true")
    assert (await test_client.post(selected["path"], json=body, headers=headers)).status_code == 409
    await db_session.refresh(selected["hold"])
    assert selected["hold"].revoked_at is None and selected["hold"].released_at is None


async def test_real_current_source_authorization_accepts_then_denies_revoked_source(selected, test_client, db_session, l5_owner, monkeypatch):
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
    await db_session.commit()
    created, _, _ = await record(test_client, selected)
    path = selected["path"] + "/" + created.json()["experiment_id"] + "/observations"
    assert (await test_client.get(path)).status_code == 200
    source.status = "revoked"
    await db_session.commit()
    denied = await test_client.get(path)
    assert denied.status_code == 404, denied.text
    assert "holds" not in denied.json() and "events" not in denied.json()
    assert (await test_client.post(selected["path"] + "/preview", json=selected["selection"])).status_code == 404
    selected["source_authority"].assert_not_awaited()
    selected["provider"].assert_not_called()


@pytest.mark.parametrize(
    "change",
    ["source", "owner", "membership-admin", "relationship", "config", "binding", "creation", "run", "event", "credential", "expired", "service"],
)
async def test_scope_or_authority_changes_deny_observations(selected, test_client, db_session, l5_owner, app, change):
    created, _, _ = await record(test_client, selected)
    if change == "source":
        selected["source_authority"].side_effect = HTTPException(404)
    elif change == "owner":
        selected["configurations"]["well_architected"][0].owner_id = "foreign"
    elif change == "membership-admin":
        await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == l5_owner.user_id))
        app.dependency_overrides[get_current_user] = lambda: l5_owner.model_copy(update={"is_platform_admin": True})
    elif change == "relationship":
        relationship = await db_session.scalar(select(ProductProjectBinding).where(ProductProjectBinding.product_id == selected["product_id"]))
        relationship.valid_to = datetime.now(UTC)
    elif change == "config":
        selected["configurations"]["well_architected"][1].hash = "f" * 64
    elif change == "binding":
        selected["binding"].generation += 1
    elif change == "creation":
        selected["intent"].intent_hash = "f" * 64
    elif change == "run":
        selected["run"].input = {"sources": [], "capability": "changed"}
    elif change == "event":
        selected["event"].configuration_revision += 1
    elif change == "credential":
        selected["credential"].aws_verified_version_id = "rotated"
    elif change == "service":
        app.dependency_overrides[get_current_user] = lambda: l5_owner.model_copy(update={"account_type": "service", "auth_source": "iam"})
    else:
        row = await db_session.get(WellArchitectedQualification, (l5_owner.org_id, created.json()["experiment_id"]))
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    response = await test_client.get(selected["path"] + "/" + created.json()["experiment_id"] + "/observations")
    assert response.status_code in {404, 409, 410, 412}, response.text
    assert "holds" not in response.json() and "events" not in response.json()


async def test_event_only_and_foreign_event_selection_are_bounded(selected, test_client, db_session):
    selected["selection"]["run_id"] = None
    created, _, _ = await record(test_client, selected)
    observed = await test_client.get(selected["path"] + "/" + created.json()["experiment_id"] + "/observations")
    assert observed.status_code == 200 and observed.json()["holds"] == [] and observed.json()["run"] is None
    selected["selection"]["events"][0]["event_id"] = "unselected-event"
    assert (await test_client.post(selected["path"] + "/preview", json=selected["selection"])).status_code == 404
    selected["selection"]["events"] = []
    assert (await test_client.post(selected["path"] + "/preview", json=selected["selection"])).status_code == 422


async def test_defaults_preconditions_and_injected_fault_fields_fail_closed(selected, test_client, monkeypatch):
    monkeypatch.delenv("L5_WA_QUALIFICATION_OBSERVATIONS_ENABLED")
    denied = await test_client.post(selected["path"] + "/preview", json=selected["selection"])
    assert denied.status_code == 503 and denied.json()["detail"]["code"] == "qualification_observations_disabled"
    monkeypatch.setenv("L5_WA_QUALIFICATION_OBSERVATIONS_ENABLED", "true")
    created, body, headers = await record(test_client, selected)
    assert (await test_client.post(selected["path"], json=body)).status_code == 428
    for change in ({"mode": "restart"}, {"fault": "lost_response"}, {"provider_count": 0}):
        assert (await test_client.post(selected["path"], json={**body, **change}, headers=headers)).status_code == 422
    for expiry in (datetime.now(UTC), datetime.now(UTC) + timedelta(days=2)):
        assert (await test_client.post(selected["path"], json={**body, "expires_at": expiry.isoformat()}, headers=headers)).status_code == 422
    assert (await test_client.post(selected["path"], json={**body, "selection_digest": "f" * 64}, headers=headers)).status_code == 412


@pytest.mark.parametrize("field,value", [("region", "eu-west-1"), ("account_id", "000000000000"), ("role_arn", "another-role")])
async def test_selected_provider_identity_is_not_inferred_from_binding_readiness(selected, test_client, db_session, field, value):
    binding = selected["binding"]
    binding.provider_identity = {**binding.provider_identity, field: value}
    binding.creation_receipt = {**binding.creation_receipt, "provider_identity": binding.provider_identity}
    await db_session.commit()
    response = await test_client.post(selected["path"] + "/preview", json=selected["selection"])
    assert response.status_code == 409 and response.json()["detail"]["code"] == "qualification_binding_unverified"


@pytest.mark.parametrize("change", ["membership", "revoke_selection"])
async def test_authorization_is_rechecked_after_observation_rows_are_read(selected, test_client, db_session, monkeypatch, l5_owner, change):
    from src import l5_wa_qualification as qualification

    created, _, _ = await record(test_client, selected)
    original, reads = qualification.event_rows, 0

    async def racing(backend, scope, targets):
        nonlocal reads
        rows = await original(backend, scope, targets)
        reads += 1
        if reads == 2:
            if change == "membership":
                await backend.db.execute(delete(TeamMembership).where(TeamMembership.user_id == l5_owner.user_id))
            else:
                await backend.db.execute(
                    update(WellArchitectedQualification)
                    .where(
                        WellArchitectedQualification.tenant_id == l5_owner.org_id,
                        WellArchitectedQualification.experiment_id == created.json()["experiment_id"],
                    )
                    .values(revoked_at=datetime.now(UTC), revision=2)
                )
            await backend.db.commit()
        return rows

    monkeypatch.setattr(qualification, "event_rows", racing)
    response = await test_client.get(selected["path"] + "/" + created.json()["experiment_id"] + "/observations")
    assert response.status_code in {404, 409}, response.text
    assert "holds" not in response.json() and "events" not in response.json()


async def test_hold_limit_never_returns_partial_evidence(selected, test_client, db_session, l5_owner):
    created, _, _ = await record(test_client, selected)
    existing = selected["hold"]
    for number in range(200):
        artifact = {**existing.artifact_ref, "artifact_id": f"fixture/artifact-{number}"}
        db_session.add(
            SourceRetention(
                tenant_id=l5_owner.org_id,
                project_id=selected["project_id"],
                run_id=selected["run"].run_id,
                artifact_id=artifact["artifact_id"],
                artifact_ref=artifact,
                required_until=existing.required_until,
                policy_expires_at=existing.policy_expires_at,
                reason=existing.reason,
            )
        )
    await db_session.commit()
    response = await test_client.get(selected["path"] + "/" + created.json()["experiment_id"] + "/observations")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "qualification_observation_incomplete"
    assert "holds" not in response.json()
