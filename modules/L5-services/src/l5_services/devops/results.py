"""Immutable native result readback, without an invented ADP admission or cost."""

import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from l5_services.models.binding import Binding
from l5_services.models.evidence import AssessmentResult
from l5_services.models.result import ResultDetail
from l5_services.models.run import Run

TERMINAL_STATUS = {"COMPLETED": "completed", "FAILED": "failed", "TIMED_OUT": "failed",
                   "CANCELED": "stopped", "CANCELLED": "stopped"}


def _normalize_evidence(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {key: _normalize_evidence(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_evidence(item) for item in value]
    return value


def evidence_bytes(task):
    return json.dumps(_normalize_evidence(task), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _costs(task, reference):
    reported = task.get("costs")
    if reported is None:
        return [{"kind": "unknown", "currency": "USD"}]
    if not isinstance(reported, list) or len(reported) > 3:
        raise ValueError("unsupported native cost observation")
    if not reported:
        return [{"kind": "unknown", "currency": "USD"}]
    costs = []
    for cost in reported:
        if not isinstance(cost, dict) or cost.get("currency") != "USD" or cost.get("kind") not in {
                "unknown", "estimated", "observed"}:
            raise ValueError("unsupported native cost observation")
        if cost["kind"] == "unknown":
            if "amount" in cost:
                raise ValueError("unobserved native cost cannot be zero")
            costs.append({"kind": "unknown", "currency": "USD"})
        else:
            amount = cost.get("amount")
            try:
                parsed = Decimal(amount) if isinstance(amount, str) else None
            except InvalidOperation:
                parsed = None
            if (parsed is None or not parsed.is_finite() or parsed < 0
                    or re.fullmatch(r"[0-9]+(?:\.[0-9]{1,6})?", amount) is None):
                raise ValueError("unsupported native cost amount")
            costs.append({"kind": cost["kind"], "currency": "USD", "amount": amount,
                          "evidence_ref": reference})
    return costs


def normalize_investigation_result(run, result_id, version, collected, now):
    native = run.native_job_ref
    task = collected.get("task") or {}
    if (collected.get("kind") != "devops_investigation" or native is None
            or collected.get("native_job_ref") != native or task.get("executionId") != native["record_id"]
            or task.get("taskId") != native["record_revision"] or task.get("status") != "COMPLETED"
            or task.get("agentSpaceId") != run.admission["native_space_ref"]["record_id"]):
        raise ValueError("investigation result differs from accepted native task")
    encoded = evidence_bytes(collected)
    reference = {"artifact_id": f"readback-{result_id}", "version": str(version),
                 "sha256": sha256(encoded).hexdigest(), "media_type": "application/json", "bytes": len(encoded)}
    projection = {"result_id": result_id, "run_id": run.run_id, "scope": run.admission["scope"],
                  "revision": version, "service_id": "devops", "origin": "adp_submitted",
                  "admission": run.admission,
                  "native_job_ref": native, "native_task_id": task["taskId"],
                  "observed_at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                  "coverage": "unknown", "coverage_mode": "native_observation",
                  "provider_outcome": "completed", "finding_count": 0,
                  "evidence_ref": reference, "costs": [{"kind": "unknown", "currency": "USD"}]}
    return ResultDetail(tenant_id=run.tenant_id, result_id=result_id, payload=projection), []


async def record_native_result(sessions, run: Run, task: dict, *, now):
    if run.origin != "native" or run.admission is not None or run.native_job_ref is None:
        raise ValueError("native readback requires a native-origin run")
    if task.get("executionId") != run.native_job_ref["record_id"] or task.get("taskId") != run.input.get("native_task_id"):
        raise ValueError("native result belongs to a different execution")
    outcome = TERMINAL_STATUS.get(task.get("status"))
    if outcome is None:
        raise ValueError("terminal provider readback required")
    task = _normalize_evidence(task)
    async with sessions() as session:
        persisted = await session.get(Run, (run.tenant_id, run.run_id))
        binding = await session.get(Binding, (run.tenant_id, run.binding_id))
        if (persisted is None or binding is None or binding.state != "ready" or binding.scope != run.scope
                or persisted.scope != run.scope or persisted.native_job_ref != run.native_job_ref
                or persisted.origin != "native"
                or binding.connection_ref.get("source_connection_id") != run.native_job_ref.get("source_connection_id")
                or task.get("agentSpaceId") != (binding.native_resource_ref or {}).get("record_id")):
            raise HTTPException(404, detail={"code": "not_found"})
        previous = await session.scalar(select(AssessmentResult).where(
            AssessmentResult.tenant_id == run.tenant_id, AssessmentResult.run_id == run.run_id,
            AssessmentResult.version == 1))
        if previous is not None:
            if previous.payload != task:
                raise ValueError("provider terminal outcome changed after an immutable result")
            detail = await session.get(ResultDetail, (run.tenant_id, previous.result_id))
            return detail.payload["evidence_ref"]
        result_id = str(uuid4())
        encoded = evidence_bytes(task)
        reference = {"artifact_id": f"readback-{result_id}", "version": "1",
                     "sha256": sha256(encoded).hexdigest(), "media_type": "application/json", "bytes": len(encoded)}
        projection = {"result_id": result_id, "run_id": run.run_id, "scope": run.scope, "revision": 1,
                      "service_id": "devops", "origin": "native", "native_job_ref": run.native_job_ref,
                      "observed_at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                      "coverage": "unknown", "coverage_mode": "native_observation",
                      "provider_outcome": outcome, "evidence_ref": reference, "costs": _costs(task, reference)}
        if run.input.get("native_event_ref"):
            projection["native_event_ref"] = run.input["native_event_ref"]
        session.add(AssessmentResult(tenant_id=run.tenant_id, result_id=result_id,
                                     run_id=run.run_id, version=1, payload=task))
        session.add(ResultDetail(tenant_id=run.tenant_id, result_id=result_id, payload=projection))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            previous = await session.scalar(select(AssessmentResult).where(
                AssessmentResult.tenant_id == run.tenant_id, AssessmentResult.run_id == run.run_id,
                AssessmentResult.version == 1))
            if previous is None or previous.payload != task:
                raise
            detail = await session.get(ResultDetail, (run.tenant_id, previous.result_id))
            return detail.payload["evidence_ref"]
        return reference
