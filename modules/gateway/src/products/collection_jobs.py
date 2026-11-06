"""Admit and lease bounded collection work from explicit Product source grants."""

from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.connector_context_routes import ResolveContextInput, _authorize, _aware, _flags_enabled, _lock_module
from src.internal.connector_work_authority import get_connector_work_authorizer
from src.internal.l5_product_routes import service_actor
from src.product_onboarding.routes import fingerprint, preconditions
from src.product_onboarding.source_discovery import connector_credential
from src.products.models import ConnectorCell, ConnectorCollectionGrant, ConnectorOperationContext, ProductOperation, ProductSourceBinding
from src.products.routes import scoped_product
from src.products.source_routes import binding_view, connection_access
from src.shared.database import get_db
from src.shared.models.base import new_uuid, utcnow
from src.shared.models.vault import CredentialServiceDelegation
from src.shared.schemas.auth import TokenContext

router = APIRouter(tags=["collection-jobs"])
LEASE = timedelta(seconds=120)
COLLECTION_KINDS = {
    "github": "repository",
    "jira": "tracker",
    "gitlab": "repository",
    "sonarqube": "quality_project",
    "jenkins": "ci_job",
    "argocd": "deployment_application",
    "pagerduty": "incident_service",
    "linear": "tracker_team",
    "slack": "communication_channel",
}


class CollectionJobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grant_ids: list[str] = Field(min_length=2, max_length=2)
    cutoff: datetime
    page_size: int = Field(default=100, ge=1, le=100)
    page_limit: int = Field(default=5, ge=1, le=5)


class ClaimInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class LeaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: str = Field(min_length=1, max_length=64)
    lease_fence: int = Field(ge=1, le=10)


class FinishInput(LeaseInput):
    state: Literal["succeeded", "failed", "cancelled"]
    content_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    batch_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    items: int | None = Field(default=None, ge=0, le=100000)


def source_request(job, child, delegation):
    return ResolveContextInput(
        product_id=job.product_id,
        project_id=job.project_id,
        grant_id=child.request["grant_id"],
        delegation_id=delegation.id,
        delegation_revision=delegation.revision,
        operation_id=child.id,
        attempt_id=child.lease_owner,
    )


async def enabled(db, tenant):
    admission = await _lock_module(db, tenant)
    if not _flags_enabled() or admission is not None and not admission.enabled:
        raise HTTPException(503, detail={"code": "feature_disabled"})


@router.post("/v1/products/{product_id}/projects/{project_id}/collection-jobs", status_code=201)
async def create_job(
    product_id: str,
    project_id: str,
    body: CollectionJobInput,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_revision: int | None = Header(default=None, alias="If-Profile-Revision"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    if actor.account_type != "human":
        raise HTTPException(404, detail={"code": "not_found"})
    await enabled(db, actor.org_id)
    preconditions(idempotency_key, expected_revision)
    member, product, _ = await scoped_product(db, actor, product_id, project_id)
    now = utcnow()
    if (
        body.cutoff.tzinfo is None
        or not now - timedelta(days=30) <= body.cutoff <= now
        or len(set(body.grant_ids)) != 2
        or product.archived_at is not None
        or product.revision != expected_revision
    ):
        raise HTTPException(409, detail={"code": "invalid_collection_selection"})
    selected = []
    for grant_id in sorted(body.grant_ids):
        grant = await db.get(ConnectorCollectionGrant, (actor.org_id, grant_id), populate_existing=True)
        if (
            grant is None
            or grant.project_id != project_id
            or grant.state != "active"
            or grant.purpose != "collection"
            or _aware(grant.expires_at) <= now
            or len(grant.source_selections) != 1
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        binding = await db.get(ProductSourceBinding, (actor.org_id, grant.binding_id), populate_existing=True)
        if (
            binding is None
            or binding.project_id != project_id
            or binding.generation != grant.binding_generation
            or binding.unlinked_at is not None
            or binding_view(binding, True)["status"] != "healthy"
        ):
            raise HTTPException(409, detail={"code": "stale_source"})
        connection = await connection_access(db, actor, binding, lock=True)
        if connection is None:
            raise HTTPException(409, detail={"code": "stale_source"})
        if binding.kind != COLLECTION_KINDS.get(binding.provider) or not (
            (binding.provider == "github" and binding.installation_id and not binding.credential_id)
            or (binding.provider == "jira" and binding.credential_id and not binding.installation_id)
            or (binding.provider not in ("github", "jira") and binding.credential_id and not binding.installation_id)
        ):
            raise HTTPException(422, detail={"code": "unsupported_collection_source"})
        if binding.provider not in ("github", "jira"):
            if not connector_credential(binding.provider, connection) or (
                binding.provider == "gitlab"
                and (not binding.native_id.isdecimal() or int(binding.native_id) < 1 or str(int(binding.native_id)) != binding.native_id)
            ):
                raise HTTPException(422, detail={"code": "unsupported_collection_source"})
        reference = {
            "tenant_id": actor.org_id,
            "project_id": project_id,
            "source_connection_id": binding.installation_id or binding.credential_id,
            "record_type": binding.kind,
            "record_id": binding.native_id,
            "record_revision": (binding.observed or {}).get("source_revision"),
        }
        if grant.source_selections != [reference]:
            raise HTTPException(409, detail={"code": "stale_source"})
        selected.append((grant, binding, reference))
    first = selected[0][0]
    if selected[1][0].cell_id != first.cell_id or selected[1][0].consumer != first.consumer or len({row[1].provider for row in selected}) != 2:
        raise HTTPException(422, detail={"code": "invalid_collection_selection"})
    cell = await db.get(ConnectorCell, (actor.org_id, first.cell_id), with_for_update=True, populate_existing=True)
    if cell is None or cell.state != "active" or cell.project_id != project_id or cell.workload_id != first.consumer:
        raise HTTPException(409, detail={"code": "stale_connector_grant"})
    key_hash = fingerprint({"key": idempotency_key})
    request_hash = fingerprint({"body": body.model_dump(mode="json"), "product": product_id, "project": project_id})
    prior = await db.scalar(
        select(ProductOperation).where(
            ProductOperation.tenant_id == actor.org_id,
            ProductOperation.actor_id == member.id,
            ProductOperation.operation == "collectConnectorSources",
            ProductOperation.key_hash == key_hash,
        )
    )
    if prior is not None:
        if prior.request_hash != request_hash:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        response.status_code = 200
        return {"job_id": prior.id, "tenant_id": actor.org_id, "state": prior.state}
    active = (
        await db.scalars(
            select(ProductOperation).where(
                ProductOperation.tenant_id == actor.org_id,
                ProductOperation.operation == "collectConnectorSources",
                ProductOperation.state.in_(("pending", "running")),
                ProductOperation.expires_at > now,
            )
        )
    ).all()
    if any(operation.request.get("cell_id") == cell.id for operation in active):
        raise HTTPException(409, detail={"code": "collection_already_active"})
    deadline = min(now + timedelta(minutes=30), *(_aware(row[0].expires_at) for row in selected))
    job = ProductOperation(
        id=new_uuid(),
        tenant_id=actor.org_id,
        product_id=product_id,
        project_id=project_id,
        actor_id=member.id,
        operation="collectConnectorSources",
        key_hash=key_hash,
        request_hash=request_hash,
        state="pending",
        expires_at=deadline,
        request={},
    )
    children = []
    for grant, binding, reference in selected:
        child_id = new_uuid()
        delegation = CredentialServiceDelegation(
            id=new_uuid(),
            tenant_id=actor.org_id,
            credential_id=binding.credential_id,
            installation_id=binding.installation_id,
            service_id=first.consumer,
            product_id=product_id,
            project_id=project_id,
            created_by=member.id,
            operations=["product:read", "source:read"],
            sources=[reference],
            capabilities=[],
            visibility="restricted",
            operation_id=child_id,
            binding_generation=binding.generation,
            expires_at=deadline,
        )
        child = ProductOperation(
            id=child_id,
            tenant_id=actor.org_id,
            product_id=product_id,
            project_id=project_id,
            actor_id=member.id,
            operation="collectConnectorSource",
            key_hash=fingerprint({"job": job.id, "grant": grant.id}),
            request_hash=request_hash,
            state="pending",
            binding_id=binding.id,
            binding_generation=binding.generation,
            expires_at=deadline,
            request={
                "job_id": job.id,
                "consumer": first.consumer,
                "cell_id": cell.id,
                "grant_id": grant.id,
                "delegation_id": delegation.id,
                "delegation_revision": 1,
                "source_ref": reference,
            },
        )
        db.add_all([delegation, child])
        children.append(child_id)
    job.request = {**body.model_dump(mode="json"), "cell_id": cell.id, "consumer": first.consumer, "children": children}
    db.add(job)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code": "collection_conflict"}) from None
    response.headers["Cache-Control"] = "no-store"
    return {"job_id": job.id, "tenant_id": actor.org_id, "state": job.state}


async def owned_operation(db, request, tenant, job_id):
    actor = service_actor(request)
    await _lock_module(db, tenant)
    job = await db.scalar(
        select(ProductOperation)
        .where(ProductOperation.tenant_id == tenant, ProductOperation.id == job_id, ProductOperation.operation == "collectConnectorSources")
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if job is None or job.request.get("consumer") != actor.agent_registry_id:
        raise HTTPException(404, detail={"code": "not_found"})
    return actor, job


async def owned_job(db, request, tenant, job_id):
    actor, job = await owned_operation(db, request, tenant, job_id)
    children = []
    for child_id in job.request["children"]:
        child = await db.get(ProductOperation, (tenant, child_id), populate_existing=True)
        if child is None or child.request.get("job_id") != job.id:
            raise HTTPException(409, detail={"code": "stale_collection_work"})
        delegation = await db.get(CredentialServiceDelegation, child.request["delegation_id"], populate_existing=True)
        children.append((child, delegation))
    if job.state in ("pending", "running") and _aware(job.expires_at) <= utcnow():
        await retire_job(db, job, children, "expired", None)
        await db.commit()
        raise HTTPException(409, detail={"code": "collection_retired"})
    return actor, job, children


@router.post("/internal/v1/collection-jobs/{tenant}/{job_id}/receipt", dependencies=[Depends(verify_internal_or_irsa)])
async def read_receipt(tenant: str, job_id: str, body: ClaimInput, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    _, job = await owned_operation(db, request, tenant, job_id)
    response.headers["Cache-Control"] = "no-store"
    if job.state in ("pending", "running"):
        return {"job_id": job.id, "tenant_id": tenant, "state": job.state, "receipt": None}
    if job.request.get("claim_key") != fingerprint({"claim": body.claim_key}):
        raise HTTPException(404, detail={"code": "not_found"})
    return {
        "job_id": job.id,
        "tenant_id": tenant,
        "state": job.state,
        "attempt_id": job.lease_owner,
        "lease_fence": job.attempt_count,
        "receipt": job.receipt if job.state == "succeeded" else None,
    }


async def retire_job(db, job, children, state, digest):
    now = utcnow()
    job.state, job.receipt = state, {"content_digest": digest}
    for child, delegation in children:
        child.state, child.receipt = state, job.receipt
        if delegation is not None:
            delegation.revoked_at = now
        await db.execute(
            update(ConnectorOperationContext)
            .where(ConnectorOperationContext.tenant_id == job.tenant_id, ConnectorOperationContext.operation_id == child.id)
            .values(revoked_at=now)
        )


async def authorize_children(db, actor, job, children):
    for child, delegation in children:
        if delegation is None:
            raise HTTPException(409, detail={"code": "stale_collection_work"})
        await _authorize(db, actor, source_request(job, child, delegation), get_connector_work_authorizer(db))


def lease_current(job, body):
    if (
        job.state != "running"
        or job.lease_owner != body.attempt_id
        or job.attempt_count != body.lease_fence
        or job.lease_until is None
        or min(_aware(job.expires_at), _aware(job.lease_until)) <= utcnow()
    ):
        raise HTTPException(409, detail={"code": "stale_collection_work"})


@router.post("/internal/v1/collection-jobs/{tenant}/{job_id}/claim", dependencies=[Depends(verify_internal_or_irsa)])
async def claim_job(tenant: str, job_id: str, body: ClaimInput, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    actor, job, children = await owned_job(db, request, tenant, job_id)
    await enabled(db, tenant)
    now = utcnow()
    key = fingerprint({"claim": body.claim_key})
    if _aware(job.expires_at) <= now or job.state not in ("pending", "running"):
        raise HTTPException(409, detail={"code": "collection_retired"})
    if job.state == "running" and _aware(job.lease_until) > now:
        if job.request.get("claim_key") != key:
            raise HTTPException(409, detail={"code": "collection_leased"})
    else:
        if job.attempt_count >= 10:
            await retire_job(db, job, children, "failed", None)
            await db.commit()
            raise HTTPException(409, detail={"code": "collection_attempts_exhausted"})
        job.lease_owner, job.lease_until = new_uuid(), min(now + LEASE, _aware(job.expires_at))
        job.attempt_count += 1
        job.state, job.request = "running", {**job.request, "claim_key": key}
        for child, delegation in children:
            if delegation is None:
                raise HTTPException(409, detail={"code": "stale_collection_work"})
            child.state, child.lease_owner, child.lease_until, child.attempt_count = "running", job.lease_owner, job.lease_until, job.attempt_count
            delegation.attempt_id, delegation.lease_fence = job.lease_owner, job.attempt_count
            delegation.revision = job.attempt_count
            child.request = {**child.request, "delegation_revision": delegation.revision, "read_count": 0}
            await db.execute(
                update(ConnectorOperationContext)
                .where(ConnectorOperationContext.tenant_id == tenant, ConnectorOperationContext.operation_id == child.id)
                .values(revoked_at=now)
            )
        await db.flush()
    await authorize_children(db, actor, job, children)
    result = {
        "job_id": job.id,
        "tenant_id": tenant,
        "attempt_id": job.lease_owner,
        "lease_fence": job.attempt_count,
        "expires_at": _aware(job.lease_until).isoformat(),
        "cutoff": job.request["cutoff"],
        "window_end": _aware(job.created_at).isoformat(),
        "page_size": job.request["page_size"],
        "page_limit": job.request["page_limit"],
        "contexts": [source_request(job, child, delegation).model_dump() for child, delegation in children],
    }
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/internal/v1/collection-jobs/{tenant}/{job_id}/renew", dependencies=[Depends(verify_internal_or_irsa)])
async def renew_job(tenant: str, job_id: str, body: LeaseInput, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    actor, job, children = await owned_job(db, request, tenant, job_id)
    await enabled(db, tenant)
    lease_current(job, body)
    await authorize_children(db, actor, job, children)
    job.lease_until = min(utcnow() + LEASE, _aware(job.expires_at))
    for child, _ in children:
        child.lease_until = job.lease_until
        await db.execute(
            update(ConnectorOperationContext)
            .where(
                ConnectorOperationContext.tenant_id == tenant,
                ConnectorOperationContext.operation_id == child.id,
                ConnectorOperationContext.attempt_id == job.lease_owner,
                ConnectorOperationContext.revoked_at.is_(None),
            )
            .values(expires_at=job.lease_until)
        )
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"expires_at": _aware(job.lease_until).isoformat()}


@router.post("/internal/v1/collection-jobs/{tenant}/{job_id}/finish", dependencies=[Depends(verify_internal_or_irsa)])
async def finish_job(tenant: str, job_id: str, body: FinishInput, request: Request, db: AsyncSession = Depends(get_db)):
    actor, job, children = await owned_job(db, request, tenant, job_id)
    if (body.batch_id is None) != (body.items is None) or body.batch_id is not None and body.state != "succeeded":
        raise HTTPException(422, detail={"code": "invalid_collection_receipt"})
    receipt = {"content_digest": body.content_digest}
    if body.batch_id is not None:
        receipt.update(batch_id=body.batch_id, items=body.items)
    if job.lease_owner != body.attempt_id or job.attempt_count != body.lease_fence:
        raise HTTPException(409, detail={"code": "stale_collection_work"})
    if job.state == body.state and job.receipt == receipt:
        return {"state": job.state}
    if job.state != "running":
        raise HTTPException(409, detail={"code": "collection_retired"})
    if body.state == "succeeded":
        await enabled(db, tenant)
        lease_current(job, body)
        if body.content_digest is None:
            raise HTTPException(422, detail={"code": "sealed_digest_required"})
        await authorize_children(db, actor, job, children)
    await retire_job(db, job, children, body.state, body.content_digest)
    job.receipt = receipt
    for child, _ in children:
        child.receipt = receipt
    await db.commit()
    return {"state": job.state}
