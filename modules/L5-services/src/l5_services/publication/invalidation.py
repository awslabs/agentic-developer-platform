"""Fence scoped publication in the same transaction as a verified revocation."""

import json

from sqlalchemy import and_, select

from l5_services.models.evidence import AssessmentResult
from l5_services.models.publication import Publication
from l5_services.models.run import Run


async def fence_source(session, *, tenant_id, product_id, project_id, source_ref, watermark, now,
                       reason="source_access_revoked"):
    if (type(watermark) is not int or watermark < 1
            or not isinstance(source_ref, dict)
            or source_ref.get("tenant_id") != tenant_id or source_ref.get("project_id") != project_id
            or any(not source_ref.get(field) for field in (
                "source_connection_id", "record_type", "record_id", "record_revision"))
            or not isinstance(reason, str) or not 1 <= len(reason) <= 1000):
        raise ValueError("revocation requires a scoped source and positive watermark")
    rows = (await session.execute(select(Publication, Run.input).join(AssessmentResult, and_(
        Publication.tenant_id == AssessmentResult.tenant_id,
        Publication.result_id == AssessmentResult.result_id)).join(Run, and_(
        AssessmentResult.tenant_id == Run.tenant_id,
        AssessmentResult.run_id == Run.run_id)).where(
        Publication.tenant_id == tenant_id,
        Run.product_id == product_id, Run.project_id == project_id,
    ).with_for_update())).all()
    identity = tuple(source_ref[field] for field in (
        "tenant_id", "project_id", "source_connection_id", "record_type", "record_id"))
    changed = 0
    for publication, run_input in rows:
        source_refs = ((run_input.get("sources") or [])
                       if publication.payload.get("kind") == "finops_cost_result"
                       else publication.payload.get("source_refs", []))
        matched = [source for source in source_refs if
                   tuple(source.get(field) for field in (
                       "tenant_id", "project_id", "source_connection_id", "record_type", "record_id")) == identity]
        if (watermark <= publication.invalidation_watermark
                or not matched):
            continue
        publication.invalidation_watermark = watermark
        publication.invalidation = {"source_refs": matched, "reason": reason}
        publication.invalidation_receipt = None
        publication.revision += 1
        publication.lease_owner = None
        publication.lease_until = None
        publication.next_due_at = now
        publication.state = "invalidation_pending"
        changed += 1
    return changed


async def receive_verified_revocation(service, event, *, now):
    if service.verify_source_revocation is None:
        raise RuntimeError("source revocation requires current Product verification")
    if not isinstance(event, dict):
        raise ValueError("revocation requires a source event")
    verified = await service.verify_source_revocation(event)
    if (not isinstance(verified, dict) or verified.get("source_ref") != event.get("source_ref")
            or verified.get("invalidation_watermark") != event.get("invalidation_watermark")
            or any(field in event and event[field] != verified.get(field) for field in (
                "tenant_id", "product_id", "project_id"))
            or verified.get("revoked") is not True):
        raise ValueError("source revocation has no verified current authority")
    async with service.sessions() as session:
        changed = await fence_source(
            session, tenant_id=verified["tenant_id"], product_id=verified["product_id"],
            project_id=verified["project_id"], source_ref=verified["source_ref"],
            watermark=verified["invalidation_watermark"],
            reason=verified.get("reason", "source_access_revoked"), now=now)
        await session.commit()
        return changed


def validate_invalidation_receipt(publication, receipt):
    if (not isinstance(receipt, dict)
            or not isinstance(receipt.get("receipt_id"), str) or not receipt["receipt_id"]
            or type(receipt.get("applied_watermark")) is not int
            or receipt["applied_watermark"] < publication.invalidation_watermark
            or not isinstance(receipt.get("record_refs"), list) or len(receipt["record_refs"]) > 100):
        raise ValueError("Context has not acknowledged the revocation watermark")
    scope = publication.payload["scope"]
    for reference in receipt["record_refs"]:
        if (not isinstance(reference, dict)
                or reference.get("tenant_id") != scope["tenant_id"]
                or reference.get("project_id") != scope["project_id"]
                or any(not isinstance(reference.get(field), str) or not reference[field] for field in (
                    "source_connection_id", "record_type", "record_id", "record_revision"))):
            raise ValueError("Context invalidation contains foreign records")
    expected = {json.dumps(reference, sort_keys=True) for reference in
                (publication.receipt or {}).get("record_refs", [])}
    observed = {json.dumps(reference, sort_keys=True) for reference in receipt["record_refs"]}
    if not expected.issubset(observed):
        raise ValueError("Context invalidation does not cover admitted records")
    return receipt
