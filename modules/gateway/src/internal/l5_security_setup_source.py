"""Capture the configured architecture document for a fenced Security setup."""

import asyncio
import base64
from datetime import timedelta

from fastapi import HTTPException
from l5_services.models.configuration import ConfigurationVersion
from l5_services.models.setup import SetupIntent
from sqlalchemy import select

from src.internal.l5_credential_routes import L5WorkPermit, active_grant
from src.internal.l5_product_routes import service_actor
from src.internal.service_authorization import require_service_operation
from src.l5_service_identity import service_for_worker
from src.products.models import ProductSourceSnapshot
from src.products.routes import scoped_product
from src.products.source_capture import capture_source, get_snapshot_objects
from src.products.source_routes import service_visible_bindings, source_ref_authorized
from src.products.source_snapshots import SourceSnapshotRequest, _aware, store_snapshot, stored_receipt
from src.shared.identity.product_scope import service_product_scope
from src.shared.models.base import utcnow
from src.shared.models.vault import ChannelTenantMap, CredentialServiceDelegation


async def setup_document(body, request, response, db, work_authorizer):
    actor = service_actor(request)
    require_service_operation(request, "l5:credential-session")
    denied = HTTPException(409, detail={"code": "security_setup_source_unavailable"})

    async def current():
        grant = await db.get(CredentialServiceDelegation, body.delegation_id, populate_existing=True)
        if grant is None or service_for_worker(grant.service_id) != "security":
            raise denied
        grant = await service_product_scope(db, actor, grant.id, grant.product_id, grant.project_id, "source:read")
        if grant is None or "source:pin" not in grant.operations:
            raise denied
        await scoped_product(db, actor, grant.product_id, grant.project_id, grant.id)
        permit = await work_authorizer(grant, body)
        if not isinstance(permit, L5WorkPermit) or not permit.setup_verified:
            raise denied
        active_grant(grant, body, permit, minimum_remaining=timedelta(0))
        intent = await db.get(SetupIntent, (grant.tenant_id, body.operation_id), populate_existing=True)
        if intent is None or intent.service_id != "security" or intent.request.get("action") not in {"check", "update", "resume"}:
            raise denied
        version = await db.get(
            ConfigurationVersion, (grant.tenant_id, intent.configuration_id, intent.request["configuration_revision"]), populate_existing=True
        )
        if version is None or "threat_model" not in version.settings.get("capabilities", []):
            raise denied
        documents = [source for source in version.settings.get("source_refs", []) if source.get("record_type") == "document"]
        if len(documents) != 1 or documents[0] not in grant.sources:
            raise HTTPException(409, detail={"code": "security_architecture_document_required"})
        source = documents[0]
        matches = [binding for binding in await service_visible_bindings(db, grant) if source_ref_authorized(grant, binding, source)]
        if len(matches) != 1:
            raise denied
        binding = matches[0]
        state = (grant.revision, permit, version.hash, source, binding.id, binding.revision, binding.generation, binding.desired, binding.observed)
        return grant, binding, source, state

    grant, binding, source, before = await current()
    objects = get_snapshot_objects()
    key = f"security-model-setup:{body.operation_id}"
    capture_request = SourceSnapshotRequest(
        delegation_id=grant.id,
        delegation_revision=grant.revision,
        source_ref=source,
        exclusions=[],
        max_bytes=6 * 1024 * 1024,
        max_files=1,
        retain_until=_aware(grant.expires_at),
    )
    previous = await db.scalar(
        select(ProductSourceSnapshot).where(
            ProductSourceSnapshot.tenant_id == grant.tenant_id,
            ProductSourceSnapshot.delegation_id == grant.id,
            ProductSourceSnapshot.idempotency_key == key,
        )
    )
    if previous is None:
        mapping = await db.get(ChannelTenantMap, binding.installation_id) if binding.installation_id else None
        captured = await capture_source(binding, mapping, capture_request, objects)
        if (await current())[3] != before:
            raise denied
        receipt, _ = await store_snapshot(db, grant, capture_request, captured, key)
        await db.commit()
    else:
        receipt = stored_receipt(previous)
    if receipt.source_ref.model_dump() != source or _aware(receipt.expires_at) <= utcnow():
        raise denied
    content = await asyncio.to_thread(objects.get, receipt.content)
    if (await current())[3] != before:
        raise denied
    response.headers["Cache-Control"] = "no-store"
    return {"snapshot": receipt.model_dump(mode="json"), "content_base64": base64.b64encode(content).decode("ascii")}
