"""A signed incident uses existing owner consent through real admission/broker."""

import hmac
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.event import EventReceipt
from l5_services.models.run import Run
from l5_services.models.work import Work
from sqlalchemy import select

from src.internal.l5_credential_routes import get_l5_work_authorizer
from src.l5_integration import GatewayConfigurationBackend, incident_work_backend, load_incident_secret
from src.shared.models.budget import BudgetConfig
from src.shared.models.vault import CredentialServiceDelegation
from tests.products import test_l5_owner_delegation_work as cases
from tests.products.l5_devops_resource_fixtures import seed_verified_lambda

consent_fixture = cases.consent_fixture
l5_owner = cases.l5_owner
work_fixture = cases.work_fixture


@pytest.fixture
async def incident_fixture(test_client, db_session, work_fixture, l5_owner, app):
    fixture = work_fixture
    selected = await cases._derived_run(test_client, db_session, fixture, l5_owner, "devops")
    # Reuse the valid binding fixture, then remove its synthetic work so this
    # test exercises only admission created by the real signed endpoint.
    await db_session.delete(selected.attempt)
    await db_session.delete(selected.work)
    await db_session.delete(selected.run)
    await db_session.commit()
    assert (await test_client.delete(selected.path + "/delegations/" + selected.parent.id, headers={"If-Match": '"1"'})).status_code == 200
    config, version = fixture["configurations"]["devops"]
    resources = await seed_verified_lambda(db_session, selected.binding, version)
    environment = version.settings["source_refs"][0]
    incident_source = {**fixture["reference"], "record_type": "incident", "record_id": "selected-event-source"}
    policy_ref = {**fixture["reference"], "record_type": "policy", "record_id": str(uuid4())}
    trigger = {
        "trigger_id": "selected-native-trigger",
        "enabled": True,
        "kind": "native_event",
        "capability": "investigation",
        "binding_id": selected.binding.binding_id,
        "resource_refs": resources["resource_refs"],
        "telemetry_refs": resources["telemetry_refs"],
        "severities": ["high"],
        "environment_refs": [environment],
        "maintenance_windows": [],
    }
    version.settings = {
        **version.settings,
        "classification": "internal",
        "profile_ref": {**fixture["reference"], "record_type": "product"},
        "limits": {
            "deadline_seconds": 600,
            "max_provider_attempts": 1,
            "max_polls": 10,
            "concurrency": 2,
            "max_cost": "1.00",
            "currency": "USD",
            "policy_ref": policy_ref,
        },
        "devops": {
            **version.settings["devops"],
            "trigger_policies": [trigger],
            "incident_sources": [
                {
                    "source_id": "signed-source",
                    "secret_ref": "test-secret",
                    "source_ref": incident_source,
                    "environment_ref": environment,
                    "trigger_id": trigger["trigger_id"],
                }
            ],
        },
    }
    db_session.add(
        BudgetConfig(
            id=policy_ref["record_id"],
            org_id=l5_owner.org_id,
            entity_type="org",
            entity_id=l5_owner.org_id,
            period_type="daily",
            budget_amount_usd=Decimal("5.00"),
            enforcement_mode="hard",
        )
    )
    await db_session.commit()
    granted, _, _, _ = await cases.consent_fixtures.request_grant(test_client, selected.path, key="explicit_incident_consent_01")
    assert granted.status_code == 200, granted.text
    parent = await db_session.get(CredentialServiceDelegation, granted.json()["delegation_id"])
    publish = AsyncMock()

    def backend(db, receipt):
        return GatewayConfigurationBackend(db, SimpleNamespace(org_id=receipt.tenant_id, user_id=None), publish=publish)

    app.dependency_overrides[load_incident_secret] = lambda: AsyncMock(return_value="test-signing-secret")
    app.dependency_overrides[incident_work_backend] = lambda: backend
    return SimpleNamespace(
        path=selected.path,
        parent=parent,
        binding=selected.binding,
        publish=publish,
        source=incident_source,
        event={"event_id": "event-001", "question": "Why is the selected service unavailable?", "severity": "high", "environment_ref": environment},
    )


async def _send(client, event, *, signed=True):
    body = json.dumps(event, sort_keys=True).encode()
    signature = "sha256=" + hmac.new(b"test-signing-secret", body, sha256).hexdigest() if signed else "sha256=" + "0" * 64
    return await client.post("/v1/devops/incident-sources/signed-source/events", content=body, headers={"X-DevOps-Signature": signature})


@pytest.mark.asyncio
async def test_signed_incident_creates_owner_child_then_passes_real_broker(
    test_client, db_session, work_fixture, l5_owner, incident_fixture, app, monkeypatch
):
    selected = incident_fixture
    invalid = await _send(test_client, selected.event, signed=False)
    assert invalid.status_code == 401 and not selected.publish.called
    accepted = await _send(test_client, selected.event)
    assert accepted.status_code == 202, accepted.text
    selected.run = await db_session.get(Run, (l5_owner.org_id, accepted.json()["run_id"]), populate_existing=True)
    selected.child = await db_session.get(CredentialServiceDelegation, selected.run.admission["delegation_id"], populate_existing=True)
    assert selected.child.parent_delegation_id == selected.parent.id
    assert selected.child.execution_scope == selected.parent.execution_scope
    assert selected.run.admission["actor_id"] == l5_owner.user_id
    assert selected.run.admission["event_source_ref"] == selected.source
    assert selected.run.admission["event_kind"] == "native_event"
    assert selected.publish.call_count == 1
    repeated = await _send(test_client, selected.event)
    assert repeated.status_code == 202 and repeated.json() == accepted.json()
    assert selected.publish.call_count == 1
    children = (
        await db_session.scalars(select(CredentialServiceDelegation).where(CredentialServiceDelegation.parent_delegation_id == selected.parent.id))
    ).all()
    assert len(children) == 1
    selected.work = await db_session.scalar(select(Work).where(Work.run_id == selected.run.run_id))
    selected.work.lease_owner, selected.work.fence = "fixture-worker", 1
    selected.work.lease_until = datetime.now(UTC) + timedelta(minutes=5)
    db_session.add(
        WorkerAttempt(
            tenant_id=l5_owner.org_id,
            attempt_id=str(uuid4()),
            run_id=selected.run.run_id,
            operation="submit",
            first_work_id=selected.work.work_id,
            first_fence=1,
            idempotency_token="a" * 64,
            input_hash="b" * 64,
            started_at=datetime.now(UTC),
            status="prepared",
        )
    )
    await db_session.commit()
    selected.body = cases._body(selected.child, selected.run.run_id, selected.work.work_id, work_fixture["reference"])
    admission = selected.run.admission
    selected.run.admission = {**admission, "event_source_ref": {**selected.source, "record_id": "different-event-source"}}
    await db_session.commit()
    with pytest.raises(HTTPException):
        await get_l5_work_authorizer(db_session)(selected.child, selected.body)
    selected.run.admission = admission
    await db_session.commit()
    permit = await get_l5_work_authorizer(db_session)(selected.child, selected.body)
    assert json.loads(permit.session_policy)["Statement"]
    # Preserve real signed ingress/admission and reuse only the fake provider
    # boundary of the existing mounted broker integration harness.
    monkeypatch.setattr(cases, "_derived_run", AsyncMock(return_value=selected))
    await cases.test_owner_grant_crosses_mounted_broker_with_real_work_verifier(
        test_client, db_session, work_fixture, l5_owner, app, monkeypatch, "devops"
    )
    denied = await _send(test_client, {**selected.event, "event_id": "event-after-revocation"})
    assert denied.status_code == 403 and selected.publish.call_count == 1


@pytest.mark.asyncio
async def test_incident_scope_change_blocks_before_dispatch(test_client, db_session, work_fixture, incident_fixture):
    _, version = work_fixture["configurations"]["devops"]
    version.hash = "c" * 64
    await db_session.commit()
    response = await _send(test_client, incident_fixture.event)
    assert response.status_code == 403 and response.json()["detail"]["code"] == "delegation_scope_changed"
    assert not incident_fixture.publish.called
    event = await db_session.scalar(select(EventReceipt))
    assert event.state == "pending" and event.run_id is None


@pytest.mark.asyncio
async def test_incident_broker_rejects_missing_durable_event(test_client, db_session, work_fixture, l5_owner):
    selected = await cases._derived_run(test_client, db_session, work_fixture, l5_owner, "devops")
    selected.run.admission = {**selected.run.admission, "event_id": "invented-event", "event_kind": "native_event"}
    await db_session.commit()
    with pytest.raises(HTTPException) as denied:
        await get_l5_work_authorizer(db_session)(selected.child, selected.body)
    assert denied.value.status_code == 409
