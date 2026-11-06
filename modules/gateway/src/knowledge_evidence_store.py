"""Versioned private artifacts and append-only knowledge evidence references."""

import json
from datetime import UTC
from uuid import NAMESPACE_URL, uuid5

from starlette.concurrency import run_in_threadpool

from src.orchestration.l5_observation_scope import digest, require
from src.products.knowledge_evidence_model import KnowledgeUseEvidence
from src.products.source_capture import get_snapshot_objects
from src.products.source_snapshots import ArtifactRef

CONTRACT = "devops-knowledge-use/v1"
FLAGS = {"release_check_completed": False, "provider_attested_native_use": False}


def identity(operation_id, actor_id, key):
    return str(uuid5(NAMESPACE_URL, f"{CONTRACT}:{operation_id}:{actor_id}:{key}"))


def row_id(evidence_id, kind):
    return str(uuid5(NAMESPACE_URL, f"{CONTRACT}:{evidence_id}:{kind}"))


def event_body(row):
    return {
        "tenant_id": row.tenant_id,
        "id": row.id,
        "operation_id": row.operation_id,
        "evidence_id": row.evidence_id,
        "kind": row.kind,
        "actor_id": row.actor_id,
        "document": row.document,
        "created_at": row.created_at.replace(tzinfo=row.created_at.tzinfo or UTC).isoformat(),
    }


async def read(db, tenant, operation, evidence, kind, *, required=True):
    row = await db.get(KnowledgeUseEvidence, (tenant, row_id(evidence, kind)), populate_existing=True)
    if row is None:
        require(not required, "knowledge_evidence_missing")
        return None
    require(
        (row.operation_id, row.evidence_id, row.kind) == (operation, evidence, kind)
        and row.document.get("contract") == CONTRACT
        and row.event_hash == digest(event_body(row)),
        "knowledge_evidence_changed",
    )
    return row


async def append(db, tenant, operation, evidence, kind, actor, document, now):
    row = KnowledgeUseEvidence(
        tenant_id=tenant,
        id=row_id(evidence, kind),
        operation_id=operation,
        evidence_id=evidence,
        kind=kind,
        actor_id=actor,
        document={"contract": CONTRACT, **document, **FLAGS},
        created_at=now,
        event_hash="",
    )
    row.event_hash = digest(event_body(row))
    db.add(row)
    await db.flush()
    return row


async def put_artifact(document):
    data = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    require(len(data) <= 3 * 1024 * 1024, "knowledge_evidence_too_large", 422)
    return (await run_in_threadpool(get_snapshot_objects().put, data, "application/json")).model_dump()


async def artifact(reference):
    ref = ArtifactRef.model_validate(reference)
    require(ref.bytes <= 3 * 1024 * 1024 and ref.media_type == "application/json", "knowledge_evidence_changed")
    try:
        return json.loads(await run_in_threadpool(get_snapshot_objects().get, ref))
    except (ValueError, TypeError):
        require(False, "knowledge_evidence_changed")


def view(row):
    # Private bytes are read through the scoped artifact endpoint, never included
    # in task progress, logs or operation readback references.
    return {
        "evidence_id": row.evidence_id,
        "kind": row.kind,
        "actor_id": row.actor_id,
        "event_hash": row.event_hash,
        "created_at": row.created_at.replace(tzinfo=row.created_at.tzinfo or UTC).isoformat(),
        **row.document,
    }
