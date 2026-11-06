"""Fresh consent is required for a changed configuration or selected binding."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from l5_services.models.binding import Binding
from l5_services.models.configuration import ConfigurationVersion
from l5_services.models.setup import SetupWork
from sqlalchemy import delete, select

from src.auth.l5_delegation import issue_l5_work_delegation
from src.internal.l5_credential_routes import get_l5_work_authorizer
from src.l5_service_identity import WORKER_IDS
from src.shared.identity.product_scope import current_l5_parent
from src.shared.models.audit import AuditLog
from src.shared.models.vault import CredentialServiceDelegation
from tests.products import test_l5_owner_delegation_work as cases

consent_fixture = cases.consent_fixture
l5_owner = cases.l5_owner
work_fixture = cases.work_fixture


async def _changed_version(db, config, old, settings):
    new = ConfigurationVersion(
        tenant_id=config.tenant_id,
        configuration_id=config.configuration_id,
        revision=config.current_revision + 1,
        settings=settings,
        inherited_fields=[],
        gaps=[],
        state="ready",
        hash="b" * 64,
    )
    db.add(new)
    config.current_revision = new.revision
    await db.commit()
    return new


@pytest.mark.asyncio
@pytest.mark.parametrize("service", list(WORKER_IDS))
async def test_configuration_change_denies_setup_and_child_admission(test_client, db_session, work_fixture, l5_owner, service):
    selected = await cases._derived_run(test_client, db_session, work_fixture, l5_owner, service)
    config, old = work_fixture["configurations"][service]
    settings = dict(old.settings)
    if service in {"devops", "transform"}:
        settings[service] = {**settings[service], "region": "us-west-2"}
    else:
        settings["region"] = "us-west-2"
    await _changed_version(db_session, config, old, settings)
    with pytest.raises(HTTPException) as denied:
        await cases._backend(db_session, l5_owner).binding_authority(work_fixture["product_id"], work_fixture["project_id"], service)
    assert denied.value.detail["code"] == "delegation_scope_changed"
    with pytest.raises(HTTPException) as denied:
        await issue_l5_work_delegation(
            db_session,
            l5_owner,
            selected.parent,
            work_fixture["credential"],
            selected.binding,
            work_fixture["reference"],
            old.settings["source_refs"],
            old.settings["capabilities"][0],
            600,
            "submit",
            "changed_scope_child_01",
        )
    assert denied.value.detail["code"] == "delegation_scope_changed"
    assert not await current_l5_parent(db_session, selected.child)
    # Align otherwise-valid durable run rows with revision 2; the retained
    # revision-1 consent must independently reject the credential request.
    selected.run.configuration_revision = 2
    await db_session.commit()
    with pytest.raises(HTTPException):
        await get_l5_work_authorizer(db_session)(selected.child, selected.body)


@pytest.mark.asyncio
async def test_enabling_configuration_requires_new_consent(test_client, db_session, work_fixture, l5_owner):
    config, old = work_fixture["configurations"]["devops"]
    _, parent = await cases._grant(test_client, db_session, work_fixture, "devops")
    disabled = await _changed_version(db_session, config, old, {**old.settings, "enabled": False})
    assert not await current_l5_parent(db_session, parent)
    await _changed_version(db_session, config, disabled, {**old.settings, "enabled": True})
    backend = cases._backend(db_session, l5_owner)
    with pytest.raises(HTTPException) as denied:
        await backend.start_binding(
            work_fixture["product_id"], work_fixture["project_id"], "devops", {"action": "create", "configuration_revision": 3}, "enabled_setup_01"
        )
    assert denied.value.detail["code"] == "delegation_scope_changed"
    assert await db_session.scalar(select(SetupWork)) is None


async def _binding_grant(client, db, fixture, selected, *, for_control=False):
    assert (await client.delete(selected.path + "/delegations/" + selected.parent.id, headers={"If-Match": '"1"'})).status_code == 200
    params = {"binding_id": selected.binding.binding_id, "for_control": for_control}
    preview = await client.get(selected.path + "/delegation-scope", params=params)
    assert preview.status_code == 200, preview.text
    response = await client.post(
        selected.path + "/delegations",
        json={
            **params,
            "scope_digest": preview.json()["scope_digest"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        },
        headers={"If-Match": preview.headers["etag"], "Idempotency-Key": "binding_consent_01"},
    )
    assert response.status_code == 200, response.text
    return await db.get(CredentialServiceDelegation, response.json()["delegation_id"], populate_existing=True)


@pytest.mark.asyncio
async def test_exact_binding_consent_rejects_sibling_at_derivation_and_broker(test_client, db_session, work_fixture, l5_owner):
    selected = await cases._derived_run(test_client, db_session, work_fixture, l5_owner, "devops")
    parent = await _binding_grant(test_client, db_session, work_fixture, selected)
    binding = selected.binding
    sibling = Binding(
        **{column.name: getattr(binding, column.name) for column in Binding.__table__.columns if column.name not in {"binding_id", "connection_key"}}
    )
    sibling.binding_id, sibling.connection_key = str(uuid4()), "sibling"
    db_session.add(sibling)
    await db_session.commit()
    with pytest.raises(HTTPException) as denied:
        await issue_l5_work_delegation(
            db_session,
            l5_owner,
            parent,
            work_fixture["credential"],
            sibling,
            work_fixture["reference"],
            [],
            "investigation",
            600,
            "submit",
            "sibling_01",
        )
    assert denied.value.detail["code"] == "delegation_scope_changed"
    child = await issue_l5_work_delegation(
        db_session,
        l5_owner,
        parent,
        work_fixture["credential"],
        binding,
        work_fixture["reference"],
        [],
        "investigation",
        600,
        "submit",
        "selected_01",
    )
    selected.run.binding_id = sibling.binding_id
    selected.run.admission = {**selected.run.admission, "delegation_id": child.id}
    await db_session.commit()
    with pytest.raises(HTTPException):
        await get_l5_work_authorizer(db_session)(child, cases._body(child, selected.run.run_id, selected.work.work_id, work_fixture["reference"]))


@pytest.mark.asyncio
async def test_control_consent_authorizes_only_next_generation_and_reuses_receipt(test_client, db_session, work_fixture, l5_owner):
    selected = await cases._derived_run(test_client, db_session, work_fixture, l5_owner, "devops")
    parent = await _binding_grant(test_client, db_session, work_fixture, selected, for_control=True)
    assert parent.binding_generation == 2 and selected.binding.generation == 1
    backend = cases._backend(db_session, l5_owner)
    request = {"action": "check", "configuration_revision": 1, "binding_id": selected.binding.binding_id}
    operation = await backend.start_binding(work_fixture["product_id"], work_fixture["project_id"], "devops", request, "control_01", '"1"')
    work = await db_session.scalar(select(SetupWork).where(SetupWork.operation_id == operation.operation_id))
    work.lease_owner, work.fence, work.lease_until = "fixture-worker", 1, datetime.now(UTC) + timedelta(minutes=5)
    await db_session.commit()
    permit = await get_l5_work_authorizer(db_session)(parent, cases._body(parent, operation.operation_id, work.work_id, work_fixture["reference"]))
    assert permit.binding_generation == 2
    retry = await backend.start_binding(work_fixture["product_id"], work_fixture["project_id"], "devops", request, "control_01", '"1"')
    assert retry.operation_id == operation.operation_id


@pytest.mark.asyncio
async def test_audit_deletion_and_scope_stripping_cannot_expand_consent(test_client, db_session, work_fixture, l5_owner):
    selected = await cases._derived_run(test_client, db_session, work_fixture, l5_owner, "devops")
    await db_session.execute(delete(AuditLog))
    await db_session.commit()
    assert await current_l5_parent(db_session, selected.child)
    selected.child.execution_scope = None
    await db_session.commit()
    assert not await current_l5_parent(db_session, selected.child)
