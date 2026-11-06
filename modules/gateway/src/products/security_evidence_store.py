"""Exact event identities and immutable local request/consent history."""

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from src.orchestration.l5_observation_scope import digest, require

from .security_evidence_model import SecurityEvidenceEvent

CONTRACT = "security-manual-setup/v1"
FLAGS = {"release_check_completed": False, "native_qualified": False}


def event_id(request_id, kind):
    return str(uuid5(NAMESPACE_URL, CONTRACT + ":" + request_id + ":" + kind))


async def event(db, tenant, request_id, kind, *, required=True):
    row = await db.get(SecurityEvidenceEvent, (tenant, event_id(request_id, kind)), populate_existing=True)
    if required:
        require(row is not None, "security_evidence_pending", 409)
    if row is not None:
        require((row.request_id, row.kind) == (request_id, kind), "security_evidence_invalid")
    return row


async def append(db, tenant, request_id, kind, actor_id, actor_kind, data):
    document = {"contract": CONTRACT, **data, **FLAGS}
    existing = await event(db, tenant, request_id, kind, required=False)
    if existing is not None:
        require(
            existing.document == document and (existing.actor_id, existing.actor_kind) == (actor_id, actor_kind),
            "security_evidence_already_attempted",
        )
        return existing
    row = SecurityEvidenceEvent(
        tenant_id=tenant,
        id=event_id(request_id, kind),
        request_id=request_id,
        kind=kind,
        actor_id=actor_id,
        actor_kind=actor_kind,
        document=document,
        created_at=datetime.now(UTC),
    )
    db.add(row)
    await db.flush()
    return row


def reference(row):
    return {
        "tenant_id": row.tenant_id,
        "id": row.id,
        "kind": row.kind,
        "sha256": digest({"request_id": row.request_id, "actor_id": row.actor_id, "actor_kind": row.actor_kind, "document": row.document}),
    }
