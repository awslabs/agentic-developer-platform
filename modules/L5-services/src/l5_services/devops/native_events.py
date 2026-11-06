"""Persist documented EventBridge lifecycle hints without admitting work."""

import hashlib
import json
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from l5_services.adapters.devops.observations import import_native_task
from l5_services.devops.monitoring import reconcile_native_run
from l5_services.models.binding import Binding
from l5_services.models.devops_event import NativeEventReceipt
from l5_services.models.run import Run

INVESTIGATION = {"Investigation Created", "Investigation Priority Updated", "Investigation In Progress",
                 "Investigation Completed", "Investigation Failed", "Investigation Timed Out", "Investigation Cancelled"}
MITIGATION = {"Mitigation In Progress", "Mitigation Completed", "Mitigation Failed",
              "Mitigation Timed Out", "Mitigation Cancelled"}


async def ingest_native_event(sessions, event: dict, *, now: datetime):
    detail = event.get("detail") or {}
    metadata = detail.get("metadata") or {}
    data = detail.get("data") or {}
    kind = event.get("detail-type")
    space_id = metadata.get("agent_space_id")
    execution_id = metadata.get("execution_id")
    event_id = event.get("id")
    if (event.get("source") != "aws.aidevops" or detail.get("version") != "1.0.0"
            or kind not in INVESTIGATION | MITIGATION
            or not all(isinstance(value, str) and 0 < len(value) <= 255
                       for value in (space_id, execution_id, event_id, metadata.get("task_id")))
            or data.get("task_type") != "INVESTIGATION"
            or not isinstance(event.get("account"), str) or not isinstance(event.get("region"), str)
            or not isinstance(data.get("status"), str)):
        raise HTTPException(422, detail={"code": "validation_error"})
    try:
        updated_at = datetime.fromisoformat(event["time"].replace("Z", "+00:00")).astimezone(UTC)
        native_updated = datetime.fromisoformat(data["updated_at"].replace("Z", "+00:00")).astimezone(UTC)
    except (ValueError, KeyError, AttributeError) as exc:
        raise HTTPException(422, detail={"code": "validation_error"}) from exc
    if updated_at != native_updated or updated_at > now:
        raise HTTPException(422, detail={"code": "validation_error"})
    expected_status = {"Completed": "COMPLETED", "Failed": "FAILED", "Timed Out": "TIMED_OUT",
                       "Cancelled": "CANCELLED", "In Progress": "IN_PROGRESS"}
    if any(kind.endswith(suffix) and data["status"] != status for suffix, status in expected_status.items()):
        raise HTTPException(422, detail={"code": "validation_error"})
    expected_resource = (f"arn:aws:aidevops:{event['region']}:{event['account']}:agentspace/{space_id}")
    if event.get("resources") != [expected_resource]:
        raise HTTPException(404, detail={"code": "not_found"})
    async with sessions() as session:
        bindings = (await session.scalars(select(Binding).where(
            Binding.service_id == "devops", Binding.state == "ready",
            Binding.native_resource_ref["record_id"].as_string() == space_id,
            Binding.provider_identity["account_id"].as_string() == event["account"],
            Binding.provider_identity["region"].as_string() == event["region"],
        ).limit(2))).all()
        if len(bindings) != 1:
            raise HTTPException(404, detail={"code": "not_found"})
        binding = bindings[0]
        connection_id = binding.connection_ref["source_connection_id"]
        key = (binding.tenant_id, connection_id, space_id, event_id)
        digest = hashlib.sha256(json.dumps(event, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        existing = await session.get(NativeEventReceipt, key)
        if existing is not None:
            if existing.event_hash != digest:
                raise HTTPException(409, detail={"code": "idempotency_conflict"})
            return await session.get(Run, (binding.tenant_id, existing.run_id))
        scope = binding.scope
        task = {"agentSpaceId": space_id, "taskType": "MITIGATION" if kind in MITIGATION else data["task_type"],
                "executionId": execution_id,
                "taskId": metadata["task_id"], "status": data["status"]}
        run = await import_native_task(session, binding, scope, task, now=now, source="native_event")
        session.add(NativeEventReceipt(tenant_id=binding.tenant_id, connection_id=connection_id,
                                       space_id=space_id, event_id=event_id, event_hash=digest, run_id=run.run_id))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await session.get(NativeEventReceipt, key)
            if existing is None or existing.event_hash != digest:
                raise
            return await session.get(Run, (binding.tenant_id, existing.run_id))
    outcome = ("completed" if kind.endswith("Completed") else "stopped" if kind.endswith("Cancelled")
               else "failed" if kind.endswith(("Failed", "Timed Out")) else "running" if kind.endswith("In Progress")
               else "unknown")
    observation = {"observed_at": event["time"], "provider_updated_at": event["time"],
                   "native_job_ref": run.native_job_ref, "outcome": outcome,
                   "native_status": data["status"], "progress_coverage": "unavailable"}
    if run.origin != "native":
        return run
    return await reconcile_native_run(sessions, run.tenant_id, run.run_id, now=now,
                                      observation=observation, source="event")
