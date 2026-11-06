"""Conservatively retain pinned native Profile scope while a run may still use it."""

from sqlalchemy import select

from l5_services.models.attempt import WorkerAttempt
from l5_services.models.run import Run


async def uncertain_start(session, run):
    return await session.scalar(select(WorkerAttempt.attempt_id).where(
        WorkerAttempt.tenant_id == run.tenant_id,
        WorkerAttempt.run_id == run.run_id,
        WorkerAttempt.operation == "submit",
        WorkerAttempt.status != "definitive_failure").limit(1)) is not None


async def active_generation(session, tenant_id, *, configuration_id=None, binding_id=None):
    query = select(Run).where(Run.tenant_id == tenant_id, Run.service_id == "well_architected")
    if configuration_id is not None:
        query = query.where(Run.configuration_id == configuration_id)
    if binding_id is not None:
        query = query.where(Run.binding_id == binding_id)
    runs = (await session.scalars(query)).all()
    for run in runs:
        if run.state not in {"succeeded", "failed", "cancelled", "partial", "blocked"}:
            return True
        if run.provider_state == "not_started" and run.native_job_ref is None:
            if await uncertain_start(session, run):
                return True
            continue
        if run.provider_state not in {"completed", "stopped"} or not run.provider_readback_ref:
            return True
    return False
