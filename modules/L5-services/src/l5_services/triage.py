"""Atomically append a human decision against the visible finding head."""

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select, update

from l5_services.models.finding import Finding
from l5_services.models.triage import Triage


async def finding_head(store, result_id, finding_id):
    result = await store.get(result_id)
    finding = await store.session.get(Finding, (result["scope"]["tenant_id"], finding_id))
    if finding is None or finding.result_id != result_id:
        raise HTTPException(404, detail={"code": "not_found"})
    return finding


async def triage_history(store, result_id, finding_id):
    finding = await finding_head(store, result_id, finding_id)
    rows = (await store.session.scalars(select(Triage).where(
        Triage.tenant_id == finding.tenant_id, Triage.finding_id == finding_id,
    ).order_by(Triage.revision))).all()
    return [{"finding_id": row.finding_id, "revision": row.revision, "actor_id": row.actor_id,
             "observed_at": row.observed_at.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z"),
             "decision": row.decision} for row in rows], finding.triage_revision


async def append_triage(store, result_id, finding_id, decision, if_match):
    if store.actor_id is None:
        raise HTTPException(404, detail={"code": "not_found"})
    if (set(decision) != {"decision", "reason"} or decision["decision"] not in
            {"open", "accepted_risk", "false_positive", "resolved"} or
            not isinstance(decision["reason"], str) or not decision["reason"].strip() or
            len(decision["reason"]) > 2000):
        raise HTTPException(422, detail={"code": "validation_error"})
    finding = await finding_head(store, result_id, finding_id)
    if if_match != f'"{finding.triage_revision}"':
        raise HTTPException(412, detail={"code": "stale_revision"})
    updated = await store.session.execute(update(Finding).where(
        Finding.tenant_id == finding.tenant_id, Finding.finding_id == finding_id,
        Finding.result_id == result_id, Finding.triage_revision == finding.triage_revision,
    ).values(triage_revision=Finding.triage_revision + 1), execution_options={"synchronize_session": False})
    if updated.rowcount != 1:
        await store.session.rollback()
        raise HTTPException(412, detail={"code": "stale_revision"})
    row = Triage(tenant_id=finding.tenant_id, finding_id=finding_id, revision=finding.triage_revision + 1,
                 actor_id=store.actor_id, observed_at=store.clock(), decision=decision)
    store.session.add(row)
    await store.session.commit()
    return {"finding_id": row.finding_id, "revision": row.revision, "actor_id": row.actor_id,
            "observed_at": row.observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "decision": row.decision}
