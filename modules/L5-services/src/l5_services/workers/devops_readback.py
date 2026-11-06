"""Scheduled bounded readback for selected DevOps bindings and due native runs."""

import asyncio
from datetime import UTC, timedelta

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import or_, select, update

from l5_services.adapters.devops.observations import readback_native_tasks, readback_prevention
from l5_services.devops.monitoring import reconcile_native_run
from l5_services.devops.results import record_native_result
from l5_services.models.binding import Binding
from l5_services.models.run import Run
from l5_services.workers.execution import load_service

STATUSES = {"IN_PROGRESS": "running", "CREATED": "running", "COMPLETED": "completed",
            "FAILED": "failed", "TIMED_OUT": "failed", "CANCELED": "stopped", "CANCELLED": "stopped"}


async def sweep_native_bindings(sessions, authorize, *, now, limit=10, observe_result=None):
    if not 1 <= limit <= 50:
        raise ValueError("bounded binding limit required")
    async with sessions() as session:
        bindings = (await session.scalars(select(Binding).where(
            Binding.service_id == "devops", Binding.state == "ready",
            or_(Binding.next_check_at.is_(None), Binding.next_check_at <= now),
        ).order_by(Binding.next_check_at, Binding.binding_id).limit(limit))).all()
    checked = 0
    for binding in bindings:
        scope = binding.scope
        async with sessions() as session:
            due = (await session.scalars(select(Run).where(
                Run.tenant_id == binding.tenant_id, Run.binding_id == binding.binding_id,
                Run.service_id == "devops", Run.origin == "native", Run.next_check_at <= now,
            ).order_by(Run.next_check_at, Run.run_id).limit(100))).all()
        interval = 300
        try:
            provider_session = await authorize(binding)
            async with sessions() as session:
                await readback_native_tasks(session, binding, scope, provider_session, now=now)
                await readback_prevention(session, binding, scope, provider_session, now=now)
            client = provider_session.client("devops-agent")
            for run in due:
                task_id = run.input.get("native_task_id")
                if not task_id:
                    await reconcile_native_run(sessions, run.tenant_id, run.run_id, now=now,
                                               error="native_task_id_unknown")
                    continue
                try:
                    if run.state == "cancel_requested":
                        from hashlib import sha256

                        token = sha256(f"{run.tenant_id}:{run.run_id}:cancel".encode()).hexdigest()
                        await asyncio.to_thread(client.update_backlog_task,
                                                agentSpaceId=binding.native_resource_ref["record_id"],
                                                taskId=task_id, taskStatus="CANCELED", clientToken=token)
                    response = await asyncio.to_thread(client.get_backlog_task,
                                                       agentSpaceId=binding.native_resource_ref["record_id"],
                                                       taskId=task_id)
                    task = response.get("task") or {}
                    if (task.get("agentSpaceId") != binding.native_resource_ref["record_id"]
                            or task.get("executionId") != run.native_job_ref["record_id"]
                            or task.get("taskId") != task_id):
                        raise ValueError("provider task identity differs from pinned execution")
                    status = task.get("status")
                    outcome = STATUSES.get(status, "unknown")
                    observation = {"observed_at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                                   "native_job_ref": run.native_job_ref, "native_status": status or "unknown",
                                   "outcome": outcome, "progress_coverage": "unavailable"}
                    evidence = await (observe_result(run, task, now) if observe_result is not None else
                                      record_native_result(sessions, run, task, now=now)) if outcome in {
                                          "completed", "failed", "stopped"} else None
                    await reconcile_native_run(sessions, run.tenant_id, run.run_id, now=now,
                                               observation=observation, provider_readback_ref=evidence)
                except (BotoCoreError, ClientError, ConnectionError, TimeoutError, ValueError):
                    await reconcile_native_run(sessions, run.tenant_id, run.run_id, now=now,
                                               error="provider_readback_unavailable")
        except (BotoCoreError, ClientError, ConnectionError, TimeoutError, ValueError, PermissionError):
            interval = 60
            for run in due:
                await reconcile_native_run(sessions, run.tenant_id, run.run_id, now=now,
                                           error="provider_readback_unavailable")
        async with sessions() as session:
            await session.execute(update(Binding).where(
                Binding.tenant_id == binding.tenant_id, Binding.binding_id == binding.binding_id,
                or_(Binding.next_check_at.is_(None), Binding.next_check_at <= now),
            ).values(next_check_at=now + timedelta(seconds=interval)))
            await session.commit()
        checked += 1
    return checked


def lambda_handler(event, context, *, service=None):
    from l5_services.workers.database_probe import REQUEST_KEY, run_probe

    if isinstance(event, dict) and REQUEST_KEY in event:
        return asyncio.run(run_probe(event))
    if event == {"l5_runtime_probe": 1}:
        return {"service_id": "devops"}
    service = service or load_service()
    return {"bindings_checked": asyncio.run(sweep_native_bindings(
        service.sessions, service.native_authorize, now=service.clock(),
        observe_result=getattr(service, "native_observe_result", None)))}
