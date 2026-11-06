"""Owner cancellation before native effect start; no source renewal or cleanup."""

import re
from datetime import UTC, datetime

from fastapi import Depends, Header, HTTPException, Response
from l5_services.api.bindings import _intent_hash, view_operation
from l5_services.devops.knowledge_admission import digest
from l5_services.devops.knowledge_effects import KnowledgeEffects
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration
from l5_services.models.knowledge_effect import KnowledgeEffect, KnowledgeOwnership
from l5_services.models.setup import BindingOperation, SetupIntent, SetupWork
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from src.auth.l5_owner_delegation import _owner
from src.shared.models.audit import AuditLog


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1000)


async def cancel(backend, product_id, project_id, service_id, operation_id, reason, key, etag):
    """Lock configuration -> binding -> work before competing effect start."""
    config, _ = await _owner(backend, product_id, project_id, service_id, active=False)
    db, actor = backend.db, backend.actor
    intent = await db.get(SetupIntent, (actor.org_id, operation_id), populate_existing=True)
    if (
        service_id != "devops"
        or intent is None
        or (intent.product_id, intent.project_id, intent.service_id, intent.configuration_id)
        != (product_id, project_id, service_id, config.configuration_id)
        or intent.request.get("action") != "sync_knowledge"
        or intent.intent_hash != _intent_hash(intent)
    ):
        raise HTTPException(404, detail={"code": "not_found"})
    await db.scalar(
        select(Configuration)
        .where(Configuration.tenant_id == intent.tenant_id, Configuration.configuration_id == intent.configuration_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    # Cancellation uses current ownership, including after ownership transfer;
    # source/native consent expiry cannot deny this local-only action.
    await _owner(backend, product_id, project_id, service_id, active=False)
    binding = await db.scalar(
        select(Binding)
        .where(Binding.tenant_id == intent.tenant_id, Binding.binding_id == intent.request["binding_id"])
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    works = (
        await db.scalars(
            select(SetupWork)
            .where(SetupWork.tenant_id == intent.tenant_id, SetupWork.operation_id == operation_id)
            .order_by(SetupWork.generation)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    operation = await db.scalar(
        select(BindingOperation)
        .where(BindingOperation.tenant_id == intent.tenant_id, BindingOperation.operation_id == operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        binding is None
        or binding.configuration_id != intent.configuration_id
        or operation is None
        or operation.action != "sync_knowledge"
        or operation.binding_id != binding.binding_id
        or len(works) != 1
        or works[0].generation != intent.binding_generation
    ):
        raise HTTPException(409, detail={"code": "knowledge_cancel_state_changed"})
    work = works[0]
    request_hash = digest({"operation_id": operation_id, "reason": reason, "if_match": etag})
    audit = await db.scalar(
        select(AuditLog).where(
            AuditLog.org_id == intent.tenant_id,
            AuditLog.event_type == "l5_knowledge_cancelled",
            AuditLog.details["operation_id"].as_string() == operation_id,
            AuditLog.details["idempotency_key"].as_string() == key,
        )
    )
    if audit is not None:
        if audit.details.get("request_hash") != request_hash or audit.actor_id != actor.user_id:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        return audit.details["response"]
    if etag != f'"{operation.revision}"':
        raise HTTPException(412, detail={"code": "stale_revision"})
    now = datetime.now(UTC)
    if work.lease_until is not None and work.lease_until.replace(tzinfo=work.lease_until.tzinfo or UTC) > now:
        raise HTTPException(
            409,
            detail={
                "code": "knowledge_work_already_running",
                "message": "This work has a current lease. Wait for the current worker to stop before cancelling; no duplicate work is needed.",
            },
        )
    if work.completed_at is not None or operation.state not in {"blocked", "accepted", "running", "waiting"}:
        raise HTTPException(409, detail={"code": "knowledge_cancel_not_available"})
    effects, ownership = await unstarted_effects(db, intent, binding)
    cancellation = {
        "cancelled_at": now.isoformat(),
        "native_write_started": False,
        "native_cleanup_performed": False,
        "intent_hash": intent.intent_hash,
        "work_id": work.work_id,
        "work_generation": work.generation,
        "fence": work.fence,
        "effect_hashes": sorted(effect.event_hash for effect in effects),
    }
    prior_state, prior_error = operation.state, work.last_error
    work.completed_at, work.last_error = now, "knowledge_publication_cancelled"
    operation.state, operation.gaps = "cancelled", ["knowledge_publication_cancelled"]
    operation.revision += 1
    operation.readback_ref = {**(operation.readback_ref or {}), "knowledge_cancellation": cancellation}
    for owned in ownership:
        # Retain its row and any prior published receipt. A cancelled plan made
        # no native change, so later explicit admission may use the prior receipt.
        owned.state = "cancelled"
    response = view_operation(operation)
    db.add(
        AuditLog(
            org_id=intent.tenant_id,
            actor_id=actor.user_id,
            event_type="l5_knowledge_cancelled",
            details={
                "operation_id": operation_id,
                "idempotency_key": key,
                "request_hash": request_hash,
                "prior_state": prior_state,
                "prior_error": prior_error,
                "reason": reason,
                "response": response,
                "original_actor_id": intent.actor_id,
                **cancellation,
            },
        )
    )
    await db.commit()
    return response


async def unstarted_effects(db, intent, binding):
    effects = (
        await db.scalars(
            select(KnowledgeEffect).where(KnowledgeEffect.tenant_id == intent.tenant_id, KnowledgeEffect.operation_id == intent.operation_id)
        )
    ).all()
    ownership = (
        await db.scalars(
            select(KnowledgeOwnership)
            .where(KnowledgeOwnership.tenant_id == intent.tenant_id, KnowledgeOwnership.operation_id == intent.operation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    if (
        any(
            effect.phase != "planned"
            or effect.sequence != 1
            or effect.previous_hash is not None
            or effect.event_hash != digest(KnowledgeEffects._body(effect))
            for effect in effects
        )
        or any(owned.state != "planned" or owned.binding_id != binding.binding_id for owned in ownership)
        or {effect.source_key for effect in effects} != {owned.source_key for owned in ownership}
    ):
        raise HTTPException(
            409,
            detail={
                "code": "knowledge_cancel_not_available",
                "message": "Cancellation is available only before any native effect starts. "
                "Reconcile this operation or explicitly acknowledge abandonment after its worker stops; "
                "cancellation cannot assert cleanup.",
            },
        )
    return effects, ownership


def add_routes(router, backend_dependency):
    @router.post("/binding-operations/{operation_id}/cancel", operation_id="cancelKnowledgeBindingOperation")
    async def cancel_operation(
        product_id: str,
        project_id: str,
        service_id: str,
        operation_id: str,
        body: CancelRequest,
        response: Response,
        idempotency_key: str | None = Header(None),
        if_match: str | None = Header(None),
        backend=Depends(backend_dependency),
    ):
        if idempotency_key is None or if_match is None:
            raise HTTPException(428, detail={"code": "precondition_required"})
        if (
            re.fullmatch(r"[A-Za-z0-9_-]{16,128}", idempotency_key) is None
            or re.fullmatch(r'"[1-9][0-9]*"', if_match) is None
            or not body.reason.strip()
        ):
            raise HTTPException(422, detail={"code": "validation_error"})
        result = await cancel(backend, product_id, project_id, service_id, operation_id, body.reason, idempotency_key, if_match)
        response.headers.update({"Cache-Control": "no-store", "ETag": f'"{result["revision"]}"'})
        return result
