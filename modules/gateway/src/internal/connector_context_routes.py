"""Resolve cell-bound connector authority through verified service delegation."""

import os
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.connector_work_authority import CollectionWorkRequest, get_connector_work_authorizer
from src.internal.l5_credential_routes import (
    ConnectionRef,
    L5WorkPermit,
    WorkAuthorizer,
    active_grant,
)
from src.internal.l5_product_routes import service_actor
from src.internal.service_authorization import service_tenant
from src.products.connector_policy import PolicyDependency, PolicyScopeError, derive_current_envelope, derive_policy_envelope, descriptor_digest
from src.products.models import (
    ConnectorCell,
    ConnectorCollectionGrant,
    ConnectorEvidenceDependency,
    ConnectorModuleAdmission,
    ConnectorOperationContext,
    ConnectorPolicyRealm,
    ProductOperation,
    ProductSourceBinding,
)
from src.products.routes import scoped_product
from src.products.source_routes import allowed_source_ref, binding_view, service_visible_bindings
from src.shared.database import get_db
from src.shared.identity.product_scope import service_product_scope
from src.shared.models.base import utcnow
from src.shared.models.organization import Organization
from src.shared.models.vault import ChannelTenantMap, UserCredential

router = APIRouter(prefix="/internal/v1/connector-contexts", tags=["internal-products"], dependencies=[Depends(verify_internal_or_irsa)])


class ResolveContextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(min_length=1, max_length=36)
    project_id: str = Field(min_length=1, max_length=255)
    grant_id: str = Field(min_length=1, max_length=36)
    delegation_id: str = Field(min_length=1, max_length=36)
    delegation_revision: int = Field(ge=1)
    operation_id: str = Field(min_length=1, max_length=255)
    attempt_id: str = Field(min_length=1, max_length=255)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _flags_enabled() -> bool:
    return os.getenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "").lower() == "true" and os.getenv("DEVLAKE_COLLECTION_ENABLED", "").lower() == "true"


async def _lock_module(db: AsyncSession, tenant_id: str) -> ConnectorModuleAdmission | None:
    if await db.scalar(select(Organization.id).where(Organization.id == tenant_id).with_for_update()) is None:
        raise HTTPException(404, detail={"code": "not_found"})
    return await db.get(ConnectorModuleAdmission, tenant_id, populate_existing=True)


async def _set_module_enabled(db: AsyncSession, tenant_id: str, admission: ConnectorModuleAdmission | None, enabled: bool) -> int:
    if admission is not None and admission.enabled == enabled:
        return admission.generation
    now = utcnow()
    if admission is None:
        admission = ConnectorModuleAdmission(tenant_id=tenant_id, generation=2, enabled=enabled, changed_at=now)
        db.add(admission)
    else:
        admission.generation += 1
        admission.enabled = enabled
        admission.changed_at = now
    if not enabled:
        await db.execute(
            update(ProductOperation)
            .where(
                ProductOperation.tenant_id == tenant_id,
                ProductOperation.operation.in_(("collectConnectorSources", "collectConnectorSource")),
                ProductOperation.state.in_(("pending", "running")),
            )
            .values(state="cancelled")
        )
        await db.execute(
            update(ConnectorOperationContext)
            .where(ConnectorOperationContext.tenant_id == tenant_id, ConnectorOperationContext.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await db.execute(
            update(ConnectorCollectionGrant)
            .where(ConnectorCollectionGrant.tenant_id == tenant_id, ConnectorCollectionGrant.state == "active")
            .values(state="revoked", revoked_at=now)
        )
        await db.execute(
            update(ConnectorCell)
            .where(ConnectorCell.tenant_id == tenant_id, ConnectorCell.state == "active")
            .values(routing_revision=ConnectorCell.routing_revision + 1)
        )
    await db.commit()
    return admission.generation


class ModuleTransitionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


@router.put("/module/{tenant_id}", operation_id="transitionConnectorModule")
async def transition_module(
    tenant_id: str,
    body: ModuleTransitionInput,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    if service_tenant(request, tenant_id, "connector:module:transition") != tenant_id:
        raise HTTPException(403, detail={"code": "tenant_not_entitled"})
    admission = await _lock_module(db, tenant_id)
    if body.enabled and not _flags_enabled():
        raise HTTPException(503, detail={"code": "feature_disabled"})
    generation = await _set_module_enabled(db, tenant_id, admission, body.enabled)
    return {"enabled": body.enabled, "generation": generation}


async def _latest_dependencies(db: AsyncSession, tenant_id: str, project_id: str, cell_id: str):
    rows = (
        await db.scalars(
            select(ConnectorEvidenceDependency)
            .where(
                ConnectorEvidenceDependency.tenant_id == tenant_id,
                ConnectorEvidenceDependency.project_id == project_id,
                ConnectorEvidenceDependency.cell_id == cell_id,
            )
            .order_by(ConnectorEvidenceDependency.identity_hash, ConnectorEvidenceDependency.revision.desc())
            .limit(101)
        )
    ).all()
    if len(rows) > 100:
        raise HTTPException(409, detail={"code": "policy_denied"})
    latest = {}
    for row in rows:
        if row.identity_hash not in latest:
            latest[row.identity_hash] = row
    return latest


def _dependency_envelope(realm: ConnectorPolicyRealm, root: dict, latest: dict, asserted_audience: str | None = None) -> dict:
    try:
        return derive_current_envelope(realm, root, latest, asserted_audience)
    except PolicyScopeError:
        raise HTTPException(409, detail={"code": "policy_denied"}) from None


async def _authorize(db: AsyncSession, actor, body: ResolveContextInput, work_authorizer: WorkAuthorizer):
    delegation = await service_product_scope(db, actor, body.delegation_id, body.product_id, body.project_id, "source:read")
    if delegation is None:
        raise HTTPException(404, detail={"code": "not_found"})
    await scoped_product(db, actor, body.product_id, body.project_id, body.delegation_id)
    admission = await _lock_module(db, delegation.tenant_id)
    if not _flags_enabled():
        await _set_module_enabled(db, delegation.tenant_id, admission, False)
        raise HTTPException(503, detail={"code": "feature_disabled"})
    if admission is not None and not admission.enabled:
        raise HTTPException(503, detail={"code": "feature_disabled"})
    module_generation = admission.generation if admission is not None else 1
    grant = await db.scalar(
        select(ConnectorCollectionGrant)
        .where(
            ConnectorCollectionGrant.tenant_id == delegation.tenant_id,
            ConnectorCollectionGrant.project_id == body.project_id,
            ConnectorCollectionGrant.id == body.grant_id,
        )
        .execution_options(populate_existing=True)
    )
    if grant is None or grant.consumer != actor.agent_registry_id:
        raise HTTPException(404, detail={"code": "not_found"})
    cell = await db.get(ConnectorCell, (delegation.tenant_id, grant.cell_id))
    realm = (
        await db.get(ConnectorPolicyRealm, (delegation.tenant_id, body.project_id, cell.realm_id, cell.realm_revision)) if cell is not None else None
    )
    if (
        grant.state != "active"
        or _aware(grant.expires_at) <= utcnow()
        or cell is None
        or cell.project_id != body.project_id
        or cell.state != "active"
        or cell.workload_id != actor.agent_registry_id
        or realm is None
        or realm.state != "active"
        or grant.policy_revision != realm.revision
    ):
        raise HTTPException(409, detail={"code": "stale_connector_grant"})
    binding = await db.scalar(
        select(ProductSourceBinding)
        .where(
            ProductSourceBinding.tenant_id == delegation.tenant_id,
            ProductSourceBinding.project_id == body.project_id,
            ProductSourceBinding.id == grant.binding_id,
        )
        .execution_options(populate_existing=True)
    )
    if (
        binding is None
        or binding.generation != grant.binding_generation
        or binding_view(binding, True)["status"] != "healthy"
        or binding.id not in {item.id for item in await service_visible_bindings(db, delegation)}
        or len(grant.source_selections) != 1
        or grant.source_selections[0] != allowed_source_ref(delegation, binding)
    ):
        raise HTTPException(404, detail={"code": "not_found"})
    source_ref = grant.source_selections[0]
    if source_ref["record_revision"] != (binding.observed or {}).get("source_revision"):
        raise HTTPException(409, detail={"code": "stale_source"})
    latest = await _latest_dependencies(db, delegation.tenant_id, body.project_id, cell.id)
    if descriptor_digest(source_ref) in latest:
        envelope = _dependency_envelope(realm, source_ref, latest)
    else:
        try:
            envelope = derive_policy_envelope(realm, source_ref, [PolicyDependency(source_ref, ())])
        except PolicyScopeError:
            raise HTTPException(409, detail={"code": "policy_denied"}) from None
    if envelope["audience"] != cell.audience:
        raise HTTPException(409, detail={"code": "policy_denied"})
    source_policy = next(policy for policy in envelope["required_policies"] if policy["ref"] == realm.source_policy_ref)
    if source_policy["visibility"] == "shared":
        if binding.credential_id:
            credential = await db.get(UserCredential, binding.credential_id)
            if credential is None or credential.user_id is not None:
                raise HTTPException(409, detail={"code": "policy_denied"})
        else:
            installation = await db.get(ChannelTenantMap, binding.installation_id)
            if installation is None or installation.provider_scope_id.startswith("personal:"):
                raise HTTPException(409, detail={"code": "policy_denied"})
    if delegation.revision != body.delegation_revision:
        raise HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
    selection = CollectionWorkRequest(
        delegation_id=body.delegation_id,
        delegation_revision=body.delegation_revision,
        operation_id=body.operation_id,
        attempt_id=body.attempt_id,
        connection_ref=ConnectionRef.model_validate(source_ref),
        cell_id=cell.id,
        grant_id=grant.id,
        binding_id=binding.id,
    )
    permit = await work_authorizer(delegation, selection)
    if not isinstance(permit, L5WorkPermit):
        raise HTTPException(503, detail={"code": "work_authority_unavailable"})
    # Context reads do not issue STS credentials and accept short work leases.
    # Credential-session callers retain the existing 15-minute STS minimum.
    deadline = active_grant(delegation, selection, permit, minimum_remaining=timedelta(0))
    if permit.binding_generation != binding.generation:
        raise HTTPException(409, detail={"code": "stale_source"})
    return delegation, grant, cell, realm, binding, permit, min(deadline, _aware(grant.expires_at)), envelope, module_generation


@router.post("/resolve", operation_id="resolveConnectorContext", status_code=201)
async def resolve_context(
    body: ResolveContextInput,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    work_authorizer: WorkAuthorizer = Depends(get_connector_work_authorizer),
):
    actor = service_actor(request)
    delegation, grant, cell, realm, binding, permit, deadline, _, module_generation = await _authorize(db, actor, body, work_authorizer)
    current = await db.scalar(
        select(ConnectorOperationContext)
        .where(
            ConnectorOperationContext.tenant_id == delegation.tenant_id,
            ConnectorOperationContext.consumer == actor.agent_registry_id,
            ConnectorOperationContext.cell_id == cell.id,
            ConnectorOperationContext.operation_id == body.operation_id,
            ConnectorOperationContext.attempt_id == body.attempt_id,
        )
        .execution_options(populate_existing=True)
    )
    if current is not None:
        if (
            current.revoked_at is not None
            or _aware(current.expires_at) <= utcnow()
            or (
                current.product_id,
                current.project_id,
                current.grant_id,
                current.cell_id,
                current.delegation_id,
                current.delegation_revision,
                current.lease_fence,
                current.source_generation,
                current.routing_revision,
                current.policy_revision,
                current.policy_digest,
                current.module_generation,
            )
            != (
                body.product_id,
                body.project_id,
                grant.id,
                cell.id,
                delegation.id,
                delegation.revision,
                permit.lease_fence,
                binding.generation,
                cell.routing_revision,
                realm.revision,
                realm.descriptor_digest,
                module_generation,
            )
        ):
            raise HTTPException(409, detail={"code": "context_conflict"})
        response.status_code = 200
        response.headers["Cache-Control"] = "no-store"
        return {"context_version": 1, "handle": current.id, "expires_at": _aware(current.expires_at).isoformat()}
    context = ConnectorOperationContext(
        tenant_id=delegation.tenant_id,
        project_id=body.project_id,
        product_id=body.product_id,
        grant_id=grant.id,
        cell_id=cell.id,
        delegation_id=delegation.id,
        delegation_revision=delegation.revision,
        consumer=actor.agent_registry_id,
        operation_id=body.operation_id,
        attempt_id=body.attempt_id,
        lease_fence=permit.lease_fence,
        source_generation=binding.generation,
        routing_revision=cell.routing_revision,
        policy_revision=realm.revision,
        policy_digest=realm.descriptor_digest,
        module_generation=module_generation,
        expires_at=deadline,
    )
    db.add(context)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code": "context_conflict"}) from None
    response.headers["Cache-Control"] = "no-store"
    return {"context_version": 1, "handle": context.id, "expires_at": _aware(context.expires_at).isoformat()}


@router.get("/{handle}", operation_id="readConnectorContext")
async def read_context(
    handle: str,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    work_authorizer: WorkAuthorizer = Depends(get_connector_work_authorizer),
):
    actor = service_actor(request)
    context = await db.scalar(select(ConnectorOperationContext).where(ConnectorOperationContext.id == handle))
    if context is None or context.consumer != actor.agent_registry_id or context.revoked_at is not None or _aware(context.expires_at) <= utcnow():
        raise HTTPException(404, detail={"code": "not_found"})
    body = ResolveContextInput(
        product_id=context.product_id,
        project_id=context.project_id,
        grant_id=context.grant_id,
        delegation_id=context.delegation_id,
        delegation_revision=context.delegation_revision,
        operation_id=context.operation_id,
        attempt_id=context.attempt_id,
    )
    delegation, grant, cell, realm, binding, permit, deadline, envelope, module_generation = await _authorize(db, actor, body, work_authorizer)
    await db.refresh(context)
    if (
        context.revoked_at is not None
        or _aware(context.expires_at) <= utcnow()
        or context.tenant_id != delegation.tenant_id
        or context.cell_id != cell.id
        or context.lease_fence != permit.lease_fence
        or context.source_generation != binding.generation
        or context.routing_revision != cell.routing_revision
        or context.policy_revision != realm.revision
        or context.policy_digest != realm.descriptor_digest
        or context.module_generation != module_generation
    ):
        raise HTTPException(409, detail={"code": "stale_connector_context"})
    response.headers["Cache-Control"] = "no-store"
    return {
        "context_version": context.context_version,
        "tenant_id": context.tenant_id,
        "project_id": context.project_id,
        "cell_id": context.cell_id,
        "grant_id": grant.id,
        "operation_id": context.operation_id,
        "attempt_id": context.attempt_id,
        "purpose": grant.purpose,
        "audience": cell.audience,
        "source_selections": grant.source_selections,
        "policy_revision": realm.revision,
        "policy_envelope": envelope,
        "source_generation": binding.generation,
        "binding_id": binding.id,
        "binding_revision": binding.revision,
        "provider": binding.provider,
        "consumer": context.consumer,
        "lease_fence": permit.lease_fence,
        "module_generation": module_generation,
        "routing_revision": cell.routing_revision,
        "expires_at": min(_aware(context.expires_at), deadline).isoformat(),
        "destination": {"database_ref": cell.database_ref, "role_ref": cell.role_ref, "object_prefix": cell.object_prefix},
        "destinations": _destinations(cell.object_prefix, cell.audience),
    }


def _destinations(prefix: str, audience: str) -> dict:
    base = prefix.rstrip("/") + "/"
    locations = {"raw": base + "raw/", "results": base + "results/"}
    if audience == "private":
        locations["quarantine"] = base + "quarantine/"
    return locations


class DeriveEvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: dict[str, str]
    asserted_audience: str | None = None


@router.post("/{handle}/derive", operation_id="deriveConnectorEvidence")
async def derive_evidence(
    handle: str,
    body: DeriveEvidenceInput,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    work_authorizer: WorkAuthorizer = Depends(get_connector_work_authorizer),
):
    current = await read_context(handle, request, response, db, work_authorizer)
    source = current["source_selections"][0]
    root = body.root
    if (
        root.get("tenant_id") != current["tenant_id"]
        or root.get("project_id") != current["project_id"]
        or root.get("source_connection_id") != source["source_connection_id"]
    ):
        raise HTTPException(404, detail={"code": "not_found"})
    cell = await db.get(ConnectorCell, (current["tenant_id"], current["cell_id"]))
    realm = await db.get(ConnectorPolicyRealm, (current["tenant_id"], current["project_id"], cell.realm_id, cell.realm_revision))
    latest = await _latest_dependencies(db, current["tenant_id"], current["project_id"], current["cell_id"])
    envelope = _dependency_envelope(realm, root, latest, body.asserted_audience)
    if envelope["audience"] != cell.audience:
        raise HTTPException(409, detail={"code": "policy_denied"})
    response.headers["Cache-Control"] = "no-store"
    return {"policy_envelope": envelope, "destinations": _destinations(cell.object_prefix, cell.audience)}
