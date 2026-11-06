"""Product-owned source pinning and bounded content endpoints."""

import asyncio
import hashlib
import json
import re
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.l5_credential_routes import (
    ConnectionRef,
    CredentialSessionRequest,
    L5WorkPermit,
    WorkAuthorizer,
    get_l5_work_authorizer,
)
from src.internal.l5_product_routes import service_actor
from src.internal.l5_snapshot_routes import authorized_snapshot
from src.products.models import ProductSourceSnapshot
from src.products.routes import scoped_product
from src.products.source_capture import MAX_RANGE_BYTES, SnapshotObjects, capture_source, get_snapshot_objects, refused
from src.products.source_routes import service_visible_bindings, source_ref_authorized
from src.products.source_snapshots import SourceReadRequest, SourceSnapshot, SourceSnapshotRequest, _aware, store_snapshot, stored_receipt
from src.shared.database import get_db
from src.shared.identity.product_scope import service_product_scope
from src.shared.models.base import utcnow
from src.shared.models.vault import ChannelTenantMap

router = APIRouter(
    prefix="/internal/v1/products/{product_id}/projects/{project_id}/source-snapshots",
    tags=["internal-products"],
    dependencies=[Depends(verify_internal_or_irsa)],
)


@router.post("", response_model=SourceSnapshot, operation_id="pinL5SourceSnapshot")
async def pin_source_snapshot(
    product_id: str,
    project_id: str,
    body: SourceSnapshotRequest,
    request: Request,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    work_authorizer: WorkAuthorizer = Depends(get_l5_work_authorizer),
    objects: SnapshotObjects = Depends(get_snapshot_objects),
):
    actor = service_actor(request)
    if not idempotency_key or len(idempotency_key) > 255:
        raise refused("unsupported_source_snapshot")

    async def current_authority():
        grant = await service_product_scope(db, actor, body.delegation_id, product_id, project_id, "source:pin")
        if grant is None or grant.revision != body.delegation_revision:
            raise refused("not_found", 404)
        await scoped_product(db, actor, product_id, project_id, grant.id)
        if body.source_ref.model_dump() not in (grant.sources or []):
            raise refused("not_found", 404)
        bindings = [
            binding for binding in await service_visible_bindings(db, grant) if source_ref_authorized(grant, binding, body.source_ref.model_dump())
        ]
        if len(bindings) != 1:
            raise refused("not_found", 404)
        selection = CredentialSessionRequest(
            delegation_id=grant.id,
            delegation_revision=grant.revision,
            operation_id=grant.operation_id,
            attempt_id=grant.attempt_id,
            connection_ref=ConnectionRef.model_validate(body.source_ref.model_dump()),
        )
        permit = await work_authorizer(grant, selection)
        if not isinstance(permit, L5WorkPermit):
            raise refused("work_authority_unavailable", 503)
        if (
            permit.tenant_id,
            permit.product_id,
            permit.project_id,
            permit.source_connection_id,
            permit.operation_id,
            permit.attempt_id,
            permit.lease_fence,
            permit.binding_generation,
        ) != (
            grant.tenant_id,
            product_id,
            project_id,
            body.source_ref.source_connection_id,
            grant.operation_id,
            grant.attempt_id,
            grant.lease_fence,
            grant.binding_generation,
        ) or _aware(permit.expires_at) <= utcnow():
            raise refused("stale_delegation_or_attempt", 409)
        return grant, bindings[0], permit

    grant, binding, permit = await current_authority()
    binding_state = (binding.revision, binding.generation, binding.observed, binding.desired)
    grant_state = (
        grant.revision,
        grant.operations,
        grant.sources,
        grant.revoked_at,
        grant.operation_id,
        grant.attempt_id,
        grant.lease_fence,
        grant.binding_generation,
        grant.expires_at,
    )
    request_hash = hashlib.sha256(json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    previous = await db.scalar(
        select(ProductSourceSnapshot)
        .where(
            ProductSourceSnapshot.tenant_id == grant.tenant_id,
            ProductSourceSnapshot.delegation_id == grant.id,
            ProductSourceSnapshot.idempotency_key == idempotency_key,
        )
        .execution_options(populate_existing=True)
    )
    if previous is not None:
        if previous.request_hash != request_hash:
            raise refused("idempotency_conflict", 409)
        if _aware(previous.expires_at) <= utcnow():
            raise refused("snapshot_expired", 410)
        receipt = stored_receipt(previous)
        if receipt.source_ref != body.source_ref:
            raise refused("not_found", 404)
        replay_grant, replay_binding, replay_permit = await current_authority()
        if replay_grant.revision != grant_state[0] or replay_binding.revision != binding_state[0] or replay_permit != permit:
            raise refused("stale_delegation_or_attempt", 409)
        response.status_code = 200
        response.headers["Cache-Control"] = "no-store"
        return receipt

    if (
        body.retain_until <= utcnow()
        or body.retain_until > utcnow() + timedelta(days=90)
        or body.max_bytes > 6 * 1024 * 1024
        or body.max_files > 1000
    ):
        raise refused("unsupported_source_snapshot")
    mapping = await db.get(ChannelTenantMap, binding.installation_id) if binding.installation_id else None
    captured = await capture_source(binding, mapping, body, objects)
    checked_grant, checked_binding, checked_permit = await current_authority()
    if (
        (
            checked_grant.revision,
            checked_grant.operations,
            checked_grant.sources,
            checked_grant.revoked_at,
            checked_grant.operation_id,
            checked_grant.attempt_id,
            checked_grant.lease_fence,
            checked_grant.binding_generation,
            checked_grant.expires_at,
        )
        != grant_state
        or checked_binding.revision != binding_state[0]
        or checked_binding.generation != binding_state[1]
        or checked_binding.observed != binding_state[2]
        or checked_binding.desired != binding_state[3]
        or checked_permit != permit
    ):
        raise refused("stale_delegation_or_attempt", 409)
    receipt, created = await store_snapshot(db, checked_grant, body, captured, idempotency_key)
    await db.commit()
    response.status_code = 201 if created else 200
    response.headers["Cache-Control"] = "no-store"
    return receipt


async def snapshot_bytes(product_id, project_id, snapshot_id, body, request, db, work_authorizer, objects, *, manifest):
    receipt = await authorized_snapshot(product_id, project_id, snapshot_id, body, request, db, work_authorizer)
    artifact = receipt.file_manifest if manifest else receipt.content
    range_header = request.headers.get("range")
    headers = {
        "Cache-Control": "no-store",
        "Accept-Ranges": "bytes",
        "X-Artifact-Size": str(artifact.bytes),
        "X-Artifact-Sha256": artifact.sha256,
        "X-Artifact-Version": artifact.version,
    }
    status = 200
    if range_header is not None:
        matched = re.fullmatch(r"bytes=([0-9]{1,12})-([0-9]{1,12})", range_header)
        if not matched or len(request.headers.getlist("range")) != 1:
            raise refused("invalid_source_range", 416)
        start, end = map(int, matched.groups())
        if start > end or end >= artifact.bytes or end - start + 1 > MAX_RANGE_BYTES:
            raise refused("invalid_source_range", 416)
        data = await asyncio.to_thread(objects.read_range, artifact, start, end)
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{artifact.bytes}"
    else:
        data = await asyncio.to_thread(objects.get, artifact)
    if await authorized_snapshot(product_id, project_id, snapshot_id, body, request, db, work_authorizer) != receipt:
        raise refused("stale_delegation_or_attempt", 409)
    return Response(content=data, status_code=status, media_type=artifact.media_type, headers=headers)


@router.post("/{snapshot_id}/content", operation_id="streamL5SourceSnapshot")
async def stream_source_snapshot(
    product_id: str,
    project_id: str,
    snapshot_id: str,
    body: SourceReadRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    work_authorizer: WorkAuthorizer = Depends(get_l5_work_authorizer),
    objects: SnapshotObjects = Depends(get_snapshot_objects),
):
    return await snapshot_bytes(product_id, project_id, snapshot_id, body, request, db, work_authorizer, objects, manifest=False)


@router.post("/{snapshot_id}/manifest", operation_id="manifestL5SourceSnapshot")
async def source_snapshot_manifest(
    product_id: str,
    project_id: str,
    snapshot_id: str,
    body: SourceReadRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    work_authorizer: WorkAuthorizer = Depends(get_l5_work_authorizer),
    objects: SnapshotObjects = Depends(get_snapshot_objects),
):
    return await snapshot_bytes(product_id, project_id, snapshot_id, body, request, db, work_authorizer, objects, manifest=True)
