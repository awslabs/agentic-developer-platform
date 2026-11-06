"""Resume the original admitted knowledge work after separate native consent."""

from datetime import UTC, datetime

from fastapi import HTTPException
from l5_services.devops.knowledge_admission import digest
from l5_services.dispatcher import MAX_DISPATCH_ATTEMPTS
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration
from l5_services.models.setup import BindingOperation, SetupWork
from sqlalchemy import select

from src.auth.l5_knowledge_native_consent import NATIVE_PURPOSE, current_native_grant, native_scope, preview
from src.products.models import ProductSourceSnapshot
from src.shared.models.audit import AuditLog
from src.shared.models.vault import CredentialServiceDelegation


async def resume(backend, product_id, project_id, service_id, operation_id, reason, key):
    intent, source, _, _ = await preview(backend, product_id, project_id, service_id, operation_id)
    db = backend.db
    await db.scalar(
        select(Configuration)
        .where(Configuration.tenant_id == intent.tenant_id, Configuration.configuration_id == intent.configuration_id)
        .with_for_update()
    )
    await db.scalar(
        select(Binding).where(Binding.tenant_id == intent.tenant_id, Binding.binding_id == intent.request["binding_id"]).with_for_update()
    )
    work = await db.scalar(
        select(SetupWork)
        .where(SetupWork.tenant_id == intent.tenant_id, SetupWork.operation_id == operation_id, SetupWork.generation == intent.binding_generation)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    operation = await db.scalar(
        select(BindingOperation)
        .where(BindingOperation.tenant_id == intent.tenant_id, BindingOperation.operation_id == operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    intent, source, _, _ = await preview(backend, product_id, project_id, service_id, operation_id)
    expected = native_scope(intent, source)
    now = datetime.now(UTC)
    candidates = (
        await db.scalars(
            select(CredentialServiceDelegation).where(
                CredentialServiceDelegation.parent_delegation_id == source.id,
                CredentialServiceDelegation.operation_id == operation_id,
                CredentialServiceDelegation.execution_scope["purpose"].as_string() == NATIVE_PURPOSE,
                CredentialServiceDelegation.revoked_at.is_(None),
                CredentialServiceDelegation.expires_at > now,
            )
        )
    ).all()
    grants = [candidate for candidate in candidates if candidate.execution_scope == expected]
    if len(grants) != 1:
        raise HTTPException(
            403 if not grants else 409,
            detail={
                "code": "knowledge_native_consent_required",
                "message": "Review native asset consent for this original operation before resuming it.",
            },
        )
    native = grants[0]
    await current_native_grant(db, native, intent)
    request_hash = digest({"operation_id": operation_id, "reason": reason})
    audit = await db.scalar(
        select(AuditLog).where(
            AuditLog.org_id == intent.tenant_id,
            AuditLog.event_type == "l5_knowledge_resumed",
            AuditLog.details["operation_id"].as_string() == operation_id,
            AuditLog.details["idempotency_key"].as_string() == key,
        )
    )
    if audit is not None:
        if audit.details.get("request_hash") != request_hash:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        return operation
    if work is None or work.completed_at is not None:
        raise HTTPException(
            409,
            detail={
                "code": "knowledge_native_use_unqualified",
                "message": "Publication work has completed. Inspect its retained native readback and qualify later native use.",
            },
        )
    if any(deadline is not None and deadline.replace(tzinfo=deadline.tzinfo or UTC) > now for deadline in (work.lease_until, work.dispatch_until)):
        raise HTTPException(
            409,
            detail={
                "code": "knowledge_work_already_running",
                "message": "The original work still has a current lease. Read its progress; do not start duplicate work.",
            },
        )
    for snapshot in intent.authorization["knowledge_admission"]["source_snapshots"]:
        row = await db.get(ProductSourceSnapshot, (intent.tenant_id, snapshot["snapshot_id"]), populate_existing=True)
        if (
            row is None
            or row.receipt != snapshot
            or row.delegation_id != source.id
            or row.delegation_revision != source.revision
            or row.expires_at.replace(tzinfo=row.expires_at.tzinfo or UTC) <= now
        ):
            raise HTTPException(
                409,
                detail={
                    "code": "knowledge_source_receipt_unavailable",
                    "message": "The original captured source receipt is unavailable or expired. "
                    "Cancel this operation if no native effect has started. After an effect starts, retain its history for recovery "
                    "or explicitly acknowledge abandonment without cleanup; "
                    "this operation cannot select fresh sources or extend retention.",
                },
            )
    prior_limit = work.dispatch_attempt_limit or MAX_DISPATCH_ATTEMPTS
    supplement = MAX_DISPATCH_ATTEMPTS if work.attempts >= prior_limit else 0
    if supplement:
        work.dispatch_attempt_limit = work.attempts + supplement
    db.add(
        AuditLog(
            org_id=intent.tenant_id,
            actor_id=intent.actor_id,
            event_type="l5_knowledge_resumed",
            details={
                "operation_id": operation_id,
                "work_id": work.work_id,
                "work_generation": work.generation,
                "previous_fence": work.fence,
                "idempotency_key": key,
                "request_hash": request_hash,
                "source_delegation_id": source.id,
                "source_delegation_revision": source.revision,
                "native_delegation_id": native.id,
                "native_delegation_revision": native.revision,
                "admission_hash": expected["admission_hash"],
                "prior_state": operation.state,
                "lifetime_dispatch_attempts": work.attempts,
                "prior_dispatch_limit": prior_limit,
                "dispatch_supplement": supplement,
                "dispatch_attempt_limit": work.dispatch_attempt_limit or MAX_DISPATCH_ATTEMPTS,
            },
        )
    )
    work.blocked_at, work.last_error, work.due_at = None, None, now
    work.redeliver_after = now
    work.lease_owner, work.lease_until = None, None
    operation.state, operation.gaps = "accepted", []
    operation.revision += 1
    operation.reconcile_key, operation.reconcile_reason = key, reason
    await db.commit()
    await backend.dispatch_binding(operation)
    return operation
