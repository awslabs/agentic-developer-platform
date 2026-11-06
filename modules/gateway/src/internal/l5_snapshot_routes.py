"""Read durable Product source receipts with current IAM delegation and work proof."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
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
from src.products.models import ProductSourceSnapshot
from src.products.routes import scoped_product
from src.products.source_routes import service_visible_bindings
from src.products.source_snapshots import SourceReadRequest, SourceSnapshot, _aware, stored_receipt
from src.shared.database import get_db
from src.shared.identity.product_scope import service_product_scope
from src.shared.models.base import utcnow

router = APIRouter(
    prefix="/internal/v1/products/{product_id}/projects/{project_id}/source-snapshots",
    tags=["internal-products"],
    dependencies=[Depends(verify_internal_or_irsa)],
)


async def authorized_snapshot(
    product_id: str,
    project_id: str,
    snapshot_id: str,
    body: SourceReadRequest,
    request: Request,
    db: AsyncSession,
    work_authorizer: WorkAuthorizer,
) -> SourceSnapshot:
    actor = service_actor(request)

    async def current_grant():
        grant = await service_product_scope(db, actor, body.delegation_id, product_id, project_id, "source:read")
        if grant is None:
            raise HTTPException(404, detail={"code": "not_found"})
        from src.auth.l5_devops_knowledge_scope import is_knowledge_scope

        # Knowledge uses a retained parent source grant and a separately fenced
        # SetupWork claim. The work verifier below must prove exact admission;
        # ordinary Run grants retain their existing operation/attempt checks.
        knowledge = is_knowledge_scope(grant)
        # Admission captures sources before the worker claims its first work.
        # Let the work authorizer atomically bind that child grant, after proving
        # its exact run, live lease, configuration, parent and source membership.
        unbound = (
            grant.operation_id is None
            and grant.parent_delegation_id is not None
            and grant.attempt_id is None
            and grant.lease_fence is None
            and bool(body.lease_owner)
            and body.lease_fence >= 1
        )
        if knowledge and (grant.revision != body.delegation_revision or not body.lease_owner or body.lease_fence < 1):
            raise HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
        if not knowledge and (
            grant.revision != body.delegation_revision
            or not unbound
            and (
                grant.operation_id != body.operation_id
                or grant.attempt_id is None
                or grant.lease_fence is None
                or (grant.attempt_id == body.attempt_id and grant.lease_fence > body.lease_fence)
                or (grant.attempt_id != body.attempt_id and not body.lease_owner)
            )
        ):
            raise HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
        await scoped_product(db, actor, product_id, project_id, body.delegation_id)
        return grant

    grant = await current_grant()

    async def current_row():
        row = await db.scalar(
            select(ProductSourceSnapshot)
            .where(
                ProductSourceSnapshot.tenant_id == grant.tenant_id,
                ProductSourceSnapshot.product_id == product_id,
                ProductSourceSnapshot.project_id == project_id,
                ProductSourceSnapshot.delegation_id == grant.id,
                ProductSourceSnapshot.delegation_revision == body.delegation_revision,
                ProductSourceSnapshot.id == snapshot_id,
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise HTTPException(404, detail={"code": "not_found"})
        if row.source_ref not in (grant.sources or []):
            raise HTTPException(404, detail={"code": "not_found"})
        return row

    async def current_content(row):
        if _aware(row.expires_at) <= utcnow():
            raise HTTPException(410, detail={"code": "snapshot_expired"})
        from src.products.source_routes import source_ref_authorized

        if not any(source_ref_authorized(grant, binding, row.source_ref) for binding in await service_visible_bindings(db, grant)):
            raise HTTPException(404, detail={"code": "not_found"})

    row = await current_row()
    selection = CredentialSessionRequest(
        delegation_id=body.delegation_id,
        delegation_revision=body.delegation_revision,
        operation_id=body.operation_id,
        attempt_id=body.attempt_id,
        connection_ref=ConnectionRef.model_validate(row.source_ref),
        lease_owner=body.lease_owner,
        lease_fence=body.lease_fence,
    )

    async def current_work():
        permit = await work_authorizer(grant, selection)
        if not isinstance(permit, L5WorkPermit):
            raise HTTPException(503, detail={"code": "work_authority_unavailable"})
        if snapshot_id not in permit.admitted_source_snapshot_ids:
            raise HTTPException(404, detail={"code": "not_found"})
        if (
            permit.tenant_id != grant.tenant_id
            or permit.product_id != product_id
            or permit.project_id != project_id
            or permit.source_connection_id != row.source_ref["source_connection_id"]
            or permit.operation_id != body.operation_id
            or permit.attempt_id != body.attempt_id
            or permit.lease_fence != body.lease_fence
            or (body.lease_owner is not None and permit.lease_owner != body.lease_owner)
            or permit.binding_generation != grant.binding_generation
            or _aware(permit.expires_at) <= utcnow()
        ):
            raise HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
        return permit

    permit = await current_work()
    await current_content(row)
    receipt = stored_receipt(row)
    await current_grant()
    row = await current_row()
    if await current_work() != permit:
        raise HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
    await current_grant()
    await current_content(row)
    if stored_receipt(row) != receipt:
        raise HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
    return receipt


@router.post("/{snapshot_id}/read", response_model=SourceSnapshot, operation_id="readL5SourceSnapshot")
async def read_source_snapshot(
    product_id: str,
    project_id: str,
    snapshot_id: str,
    body: SourceReadRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    work_authorizer: WorkAuthorizer = Depends(get_l5_work_authorizer),
):
    receipt = await authorized_snapshot(product_id, project_id, snapshot_id, body, request, db, work_authorizer)
    response.headers["Cache-Control"] = "no-store"
    return receipt
