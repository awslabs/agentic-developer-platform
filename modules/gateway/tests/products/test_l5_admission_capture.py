"""Gateway capture uses stored preparation and Product authority, not request claims."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from l5_services.models.base import Base as L5Base
from l5_services.models.preparation import AdmissionPreparation
from sqlalchemy import select

from src.auth.l5_delegation import issue_l5_work_delegation
from src.l5_service_identity import worker_id
from src.products.l5_admission_capture import GatewayAdmissionSourceClient
from src.products.models import ProductSourceBinding, ProductSourceSnapshot
from src.shared.models.vault import UserCredential
from tests.products.test_l5_snapshots import snapshot_fixture


@pytest.mark.asyncio
async def test_owner_grant_issues_one_revocable_work_grant(test_client, db_session, test_engine, app, source_member, monkeypatch):
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    template, request, _, _, _, _ = await snapshot_fixture(test_client, db_session, app, source_member, monkeypatch)
    credential = await db_session.get(UserCredential, template.credential_id)
    connection = {**request.source_ref.model_dump(), "record_type": "connection"}
    template.service_id = "l5-worker"
    template.operations = ["product:read", "source:pin", "source:read", "credential:session"]
    template.capabilities = ["threat_model"]
    template.sources = [{**connection, "record_type": "credential"}, request.source_ref.model_dump()]
    await db_session.commit()
    binding = SimpleNamespace(project_id=template.project_id, service_id="security", generation=1)

    async def issue():
        return await issue_l5_work_delegation(
            db_session,
            source_member,
            template,
            credential,
            binding,
            connection,
            [request.source_ref.model_dump()],
            "threat_model",
            3600,
            "submit",
            "admission-one",
        )

    grant = await issue()
    await db_session.commit()
    replay = await issue()
    assert replay.id == grant.id and replay.parent_delegation_id == template.id
    assert (grant.operation_id, grant.attempt_id, grant.lease_fence) == (None, None, None)
    selected_request = request.model_copy(update={"delegation_id": grant.id, "delegation_revision": grant.revision})
    verified = {
        "scope": {"tenant_id": grant.tenant_id, "product_id": grant.product_id, "project_id": grant.project_id},
        "trigger": {"source_refs": [request.source_ref.model_dump()]},
        "exclusions": request.exclusions,
        "max_bytes": request.max_bytes,
        "max_files": request.max_files,
    }
    db_session.add(
        AdmissionPreparation(
            tenant_id=grant.tenant_id,
            product_id=grant.product_id,
            project_id=grant.project_id,
            service_id="security",
            actor_id=source_member.user_id,
            operation="submit",
            idempotency_key="admission-one",
            request_hash="a" * 64,
            verified=verified,
            deadline_at=request.retain_until,
        )
    )
    await db_session.commit()
    capture = GatewayAdmissionSourceClient(db_session, source_member, grant.id, verified, "submit", "admission-one", service_id="security")
    template.revoked_at = request.retain_until
    await db_session.commit()
    with pytest.raises(HTTPException) as revoked:
        await issue()
    assert revoked.value.status_code == 403
    with pytest.raises(HTTPException) as denied_capture:
        await capture.pin_source(grant.product_id, grant.project_id, selected_request.model_dump(mode="json"), idempotency_key="admission-pin")
    assert denied_capture.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("service_id", ("security", "transform", "well_architected"))
async def test_mounted_capture_requires_preparation_and_current_product_rights(
    test_client,
    db_session,
    test_engine,
    app,
    source_member,
    monkeypatch,
    service_id,
):
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    grant, request, captured, _, _, _ = await snapshot_fixture(test_client, db_session, app, source_member, monkeypatch)
    grant.service_id = worker_id(service_id)
    await db_session.commit()
    verified = {
        "scope": {"tenant_id": grant.tenant_id, "product_id": grant.product_id, "project_id": grant.project_id},
        "delegation_id": grant.id,
        "trigger": {"source_refs": [request.source_ref.model_dump()]},
        "exclusions": request.exclusions,
        "max_bytes": request.max_bytes,
        "max_files": request.max_files,
    }
    selected = GatewayAdmissionSourceClient(db_session, source_member, grant.id, verified, "submit", "capture-request-001", service_id=service_id)
    capture = AsyncMock(return_value=captured)
    monkeypatch.setattr("src.products.l5_admission_capture.get_snapshot_objects", lambda: object())
    monkeypatch.setattr("src.products.l5_admission_capture.capture_source", capture)
    with pytest.raises(HTTPException) as unprepared:
        await selected.pin_source(grant.product_id, grant.project_id, request.model_dump(mode="json"), idempotency_key="capture-once")
    assert unprepared.value.status_code == 404
    capture.assert_not_awaited()
    db_session.add(
        AdmissionPreparation(
            tenant_id=grant.tenant_id,
            product_id=grant.product_id,
            project_id=grant.project_id,
            service_id=service_id,
            actor_id=source_member.user_id,
            operation="submit",
            idempotency_key="capture-request-001",
            request_hash="a" * 64,
            verified=verified,
            deadline_at=request.retain_until,
        )
    )
    await db_session.commit()
    foreign_service = "transform" if service_id == "security" else "security"
    foreign = GatewayAdmissionSourceClient(db_session, source_member, grant.id, verified, "submit", "capture-request-001", service_id=foreign_service)
    with pytest.raises(HTTPException) as foreign_scope:
        await foreign.pin_source(grant.product_id, grant.project_id, request.model_dump(mode="json"), idempotency_key="foreign-capture")
    assert foreign_scope.value.status_code == 404 and capture.await_count == 0
    first = await selected.pin_source(grant.product_id, grant.project_id, request.model_dump(mode="json"), idempotency_key="capture-once")
    await db_session.commit()
    assert first["source_ref"] == request.source_ref.model_dump()
    assert (await db_session.scalar(select(ProductSourceSnapshot).where(ProductSourceSnapshot.id == first["snapshot_id"]))) is not None
    replay = await selected.pin_source(grant.product_id, grant.project_id, request.model_dump(mode="json"), idempotency_key="capture-once")
    assert replay == first and capture.await_count == 1
    binding = await db_session.scalar(select(ProductSourceBinding).where(ProductSourceBinding.native_id == "document-a"))
    binding.status = "revoked"
    await db_session.commit()
    with pytest.raises(HTTPException) as revoked:
        await selected.pin_source(grant.product_id, grant.project_id, request.model_dump(mode="json"), idempotency_key="capture-two")
    assert revoked.value.status_code == 404 and capture.await_count == 1
