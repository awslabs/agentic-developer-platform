"""SQL-owned limits and private artifact checkpoints for independent CE reads."""

import json
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import func, select, update

from l5_services.models.collection import CostCollection


class BillingCursorExpired(Exception):
    """The pinned CE cursor can no longer be resumed."""


class BillingUnavailable(Exception):
    """The reserved CE request returned no billing evidence."""


def query_identity(run):
    scope = run.admission["billing_scope"]
    request = {"query": run.input["query"], "billing_scope": scope}
    digest = sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return request, digest


def request_limit(run):
    limit = run.admission.get("limits", {}).get("max_ce_requests")
    if type(limit) is not int or limit < 1:
        return None
    return limit


async def active_collection(session, run, now):
    _, digest = query_identity(run)
    collections = (await session.scalars(select(CostCollection).where(
        CostCollection.tenant_id == run.tenant_id, CostCollection.run_id == run.run_id,
    ).order_by(CostCollection.started_at.desc()))).all()
    current = next((item for item in collections if item.status != "superseded"), None)
    if any(item.query_hash != digest or item.scope_digest != run.admission["billing_scope"]["scope_digest"]
           for item in collections):
        raise ValueError("billing collection differs from pinned request")
    if current is None:
        current = CostCollection(tenant_id=run.tenant_id, run_id=run.run_id,
                                 collection_id=str(uuid4()), query_hash=digest,
                                 scope_digest=run.admission["billing_scope"]["scope_digest"],
                                 started_at=now, status="collecting", request_count=0,
                                 page_count=0, checkpoint_ref={"pages": []})
        session.add(current)
        await session.commit()
    return current


async def reserve_request(session, collection, limit):
    total = await session.scalar(select(func.coalesce(func.sum(CostCollection.request_count), 0)).where(
        CostCollection.tenant_id == collection.tenant_id, CostCollection.run_id == collection.run_id))
    if total >= limit:
        return False
    changed = await session.execute(update(CostCollection).where(
        CostCollection.tenant_id == collection.tenant_id, CostCollection.run_id == collection.run_id,
        CostCollection.collection_id == collection.collection_id,
        CostCollection.status == "collecting", CostCollection.query_hash == collection.query_hash,
        CostCollection.request_count == collection.request_count,
    ).values(request_count=CostCollection.request_count + 1),
        execution_options={"synchronize_session": False})
    if changed.rowcount != 1:
        await session.rollback()
        return False
    await session.commit()
    collection.request_count += 1
    return True


async def checkpoint_page(session, collection, page, now, coverage):
    reference = page["artifact"]
    if (set(reference) != {"artifact_id", "version", "sha256"}
            or not all(isinstance(value, str) and value for value in reference.values())
            or sha256(json.dumps(page["response"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            != reference["sha256"]):
        raise ValueError("billing page lacks verified immutable evidence")
    if page.get("collection_id") != collection.collection_id:
        raise ValueError("billing page belongs to another collection")
    checkpoint = collection.checkpoint_ref or {"pages": []}
    pages = checkpoint["pages"]
    if reference in pages:
        raise ValueError("billing page was already checkpointed")
    if len(pages) != collection.page_count or collection.page_count >= collection.request_count:
        raise ValueError("billing request was not reserved")
    status = ("collecting" if page["response"].get("NextPageToken") is not None else
              "complete" if coverage == "complete" else "partial")
    changed = await session.execute(update(CostCollection).where(
        CostCollection.tenant_id == collection.tenant_id, CostCollection.run_id == collection.run_id,
        CostCollection.collection_id == collection.collection_id, CostCollection.status == "collecting",
        CostCollection.query_hash == collection.query_hash, CostCollection.page_count == collection.page_count,
    ).values(checkpoint_ref={"pages": [*pages, reference]}, page_count=collection.page_count + 1,
             status=status, completed_at=now if status != "collecting" else None,
             coverage=coverage), execution_options={"synchronize_session": False})
    if changed.rowcount != 1:
        await session.rollback()
        raise ValueError("billing collection checkpoint changed")
    await session.commit()
    collection.page_count += 1
    collection.checkpoint_ref = {"pages": [*pages, reference]}
    collection.status = status
    return status


async def supersede(session, collection, now):
    changed = await session.execute(update(CostCollection).where(
        CostCollection.tenant_id == collection.tenant_id, CostCollection.run_id == collection.run_id,
        CostCollection.collection_id == collection.collection_id, CostCollection.status == "collecting",
        CostCollection.query_hash == collection.query_hash,
    ).values(status="superseded", reason="invalid_cursor", completed_at=now, coverage="partial"))
    if changed.rowcount != 1:
        await session.rollback()
        raise ValueError("billing collection cannot be superseded")
    successor = CostCollection(tenant_id=collection.tenant_id, run_id=collection.run_id,
                               collection_id=str(uuid4()), query_hash=collection.query_hash,
                               scope_digest=collection.scope_digest, started_at=now,
                               status="collecting", request_count=0, page_count=0,
                               checkpoint_ref={"pages": []}, supersedes_id=collection.collection_id)
    session.add(successor)
    await session.commit()
    return successor


async def close_collection(session, collection, now, reason):
    changed = await session.execute(update(CostCollection).where(
        CostCollection.tenant_id == collection.tenant_id, CostCollection.run_id == collection.run_id,
        CostCollection.collection_id == collection.collection_id, CostCollection.status == "collecting",
        CostCollection.query_hash == collection.query_hash,
    ).values(status="partial", reason=reason, completed_at=now,
             coverage="partial" if collection.page_count else "unavailable"))
    if changed.rowcount != 1:
        await session.rollback()
        raise ValueError("billing collection cannot be closed")
    await session.commit()
    collection.status = "partial"
