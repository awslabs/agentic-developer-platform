"""Scoped internal readback of a source event and its durable execution lineage."""

from fastapi import HTTPException
from sqlalchemy import select

from l5_services.models.attempt import WorkerAttempt
from l5_services.models.event import EventReceipt, semantic_key
from l5_services.models.receipt import RunReceipt
from l5_services.models.run import Run
from l5_services.models.work import Work


async def read_event_lineage(session, *, tenant_id, product_id, project_id, trigger_id, event_id):
    receipt = await session.get(EventReceipt, (
        tenant_id, product_id, project_id, "security", trigger_id, event_id,
    ))
    if receipt is None:
        raise HTTPException(404, detail={"code": "not_found"})

    admission = await session.scalar(select(RunReceipt).where(
        RunReceipt.tenant_id == tenant_id, RunReceipt.product_id == product_id,
        RunReceipt.project_id == project_id, RunReceipt.service_id == "security",
        RunReceipt.operation == "submit", RunReceipt.idempotency_key == semantic_key(receipt),
    ))
    run = await session.get(Run, (tenant_id, admission.run_id)) if admission is not None else None
    if (receipt.run_id is not None and (run is None or receipt.run_id != run.run_id)) or (
        admission is not None and (run is None or run.product_id != product_id
                                   or run.project_id != project_id or run.service_id != "security"
                                   or run.trigger_id != trigger_id or run.input != receipt.run_input
                                   or run.admission.get("event_id") != event_id)
    ):
        raise HTTPException(409, detail={"code": "event_lineage_conflict"})

    works = (await session.scalars(select(Work).where(
        Work.tenant_id == tenant_id, Work.run_id == run.run_id,
    ).order_by(Work.due_at, Work.work_id))).all() if run is not None else []
    attempts = (await session.scalars(select(WorkerAttempt).where(
        WorkerAttempt.tenant_id == tenant_id, WorkerAttempt.run_id == run.run_id,
    ).order_by(WorkerAttempt.started_at, WorkerAttempt.attempt_id))).all() if run is not None else []
    return {
        "event_id": receipt.event_id, "trigger_id": receipt.trigger_id,
        "event_kind": receipt.event_kind, "source_ref": receipt.source_ref,
        "source_cursor": receipt.source_cursor,
        "causal_origin": receipt.causal_origin,
        "configuration_id": receipt.configuration_id,
        "configuration_revision": receipt.configuration_revision,
        "target": receipt.target, "run_input": receipt.run_input,
        "receipt_state": receipt.state, "reason": receipt.reason,
        "received_at": receipt.received_at, "recovery_attempts": receipt.recovery_attempts,
        "retry_at": receipt.retry_at,
        "run": ({"run_id": run.run_id, "revision": run.revision, "state": run.state,
                 "input": run.input, "event_id": run.admission["event_id"]} if run is not None else None),
        "work": [{"work_id": work.work_id, "kind": work.kind, "generation": work.generation,
                  "dispatch_attempts": work.attempts, "last_error": work.last_error} for work in works],
        "attempts": [{"attempt_id": attempt.attempt_id, "operation": attempt.operation,
                      "status": attempt.status, "native_job_ref": attempt.native_job_ref}
                     for attempt in attempts],
    }
