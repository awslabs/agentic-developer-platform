"""Canonical identity snapshots and durable registration receipts (CLI-11)."""

import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.shared.models.audit import AuditLog
from src.shared.models.persona_models import ServicePrincipal, ServicePrincipalAlias


async def snapshot(db, org_id, canonical_id):
    principal = await db.scalar(
        select(ServicePrincipal)
        .where(
            ServicePrincipal.org_id == org_id,
            ServicePrincipal.canonical_service_principal_id == canonical_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if principal is None:
        raise HTTPException(404, detail={"error": "principal_not_found"})
    aliases = list(
        await db.scalars(
            select(ServicePrincipalAlias)
            .where(
                ServicePrincipalAlias.org_id == org_id,
                ServicePrincipalAlias.canonical_service_principal_id == canonical_id,
            )
            .order_by(ServicePrincipalAlias.id)
            .limit(1001)
            .execution_options(populate_existing=True)
        )
    )
    if len(aliases) > 1000:
        raise HTTPException(409, detail={"error": "identity_snapshot_too_large"})
    value = {
        "tenant_id": org_id,
        "canonical_service_principal_id": canonical_id,
        "display_name": principal.display_name,
        "status": principal.status,
        "aliases": [{"id": row.id, "alias_source": row.alias_source, "alias_id": row.alias_id, "is_active": row.is_active} for row in aliases],
    }
    value["revision"] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    value["effect"] = "Inactive principals and revoked aliases are refused by canonical identity resolution. Existing runs are not terminated."
    return value


async def guard(db, org_id, canonical_id, expected_revision):
    value = await snapshot(db, org_id, canonical_id)
    if expected_revision is not None and value["revision"] != expected_revision:
        raise HTTPException(409, detail={"error": "revision_conflict", "revision": value["revision"]})
    return value


async def registration_receipt(db, org_id, actor_id, operation_id, body):
    """Reserve in the same transaction as principal creation; never store secrets."""
    receipt_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"adp:principal:{org_id}:{actor_id}:{operation_id}"))
    fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    row = await db.get(AuditLog, receipt_id)
    if row is None:
        try:
            async with db.begin_nested():
                row = AuditLog(
                    id=receipt_id,
                    org_id=org_id,
                    actor_id=actor_id,
                    event_type="service_principal_registration_receipt",
                    details={"fingerprint": fingerprint},
                )
                db.add(row)
                await db.flush()
            return row, None
        except IntegrityError:
            row = await db.get(AuditLog, receipt_id)
    if row is None or row.org_id != org_id or row.actor_id != actor_id or row.details.get("fingerprint") != fingerprint:
        raise HTTPException(409, detail={"error": "operation_conflict"})
    result = row.details.get("result")
    if not isinstance(result, dict):
        raise HTTPException(409, detail={"error": "operation_pending"})
    return row, result
