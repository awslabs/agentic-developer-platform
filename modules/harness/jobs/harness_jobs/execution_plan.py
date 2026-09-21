"""The immutable admitted plan shared by execution, lease claims and recovery."""

import hashlib
import json
from dataclasses import dataclass
from enum import Enum

from .identity import ContractViolation
from .store import _record


@dataclass(frozen=True)
class ExecutionStep:
    """A descriptor in the immutable, approval-bound admitted request."""

    step_id: str
    provider: str
    operation_kind: str
    target: str


def admitted_steps(record):
    """Read only the stored plan, never worker arguments or mutable service config.

    The admission approval binds the digest of the entire request, including this
    ordered JSON parameter. Production composition must submit these descriptors
    before approval/admission; absence cannot default to worker-selected calls.
    """
    try:
        data = json.loads(record.admitted_request().parameters["execution_steps"])
        if not isinstance(data, list) or not 1 <= len(data) <= 16:
            raise ValueError("Invalid step count")
        steps = []
        for item in data:
            if not isinstance(item, dict) or set(item) != {
                "step_id",
                "provider",
                "operation_kind",
                "target",
            }:
                raise ValueError("Invalid descriptor")
            if any(
                not isinstance(v, str) or not v.strip() or len(v) > 2048 or "\x00" in v
                for v in item.values()
            ):
                raise ValueError("Invalid descriptor value")
            steps.append(ExecutionStep(**item))
        if len({s.step_id for s in steps}) != len(steps):
            raise ValueError("Duplicate step")
        return tuple(steps)
    except (KeyError, ValueError, TypeError) as exc:
        raise ContractViolation("An approved execution-step plan is required") from exc


def step_key(record, step):
    # Stable across attempt/holder changes: an uncertain transport must never acquire
    # a fresh provider idempotency key merely by restarting the executor.
    material = json.dumps(
        [
            record.org_id,
            record.workspace_id,
            record.operation_id,
            record.plan_digest,
            step.step_id,
        ],
        separators=(",", ":"),
    )
    return "operation-step:" + hashlib.sha256(material.encode()).hexdigest()


class PlanProgress(Enum):
    COMPLETE = "complete"
    PREFIX = "prefix"
    UNKNOWN = "unknown"


async def confirmed_plan_progress(connection, operation_id):
    """Check digest, complete descriptors and a contiguous confirmed prefix.

    Callers making a write must hold the operation row and lease locks throughout
    this check and the write. Provider uncertainty never authorizes another call.
    """
    row = await connection.fetchrow(
        "SELECT * FROM harness_operations WHERE operation_id=$1", operation_id
    )
    if row is None:
        return PlanProgress.UNKNOWN
    try:
        record = _record(row)
        steps = admitted_steps(record)
    except (ContractViolation, ValueError, TypeError, KeyError):
        return PlanProgress.UNKNOWN
    calls = await connection.fetch(
        "SELECT * FROM harness_provider_call_intent WHERE operation_id=$1", operation_id
    )
    if not calls or len(calls) > len(steps):
        return PlanProgress.UNKNOWN
    expected = {step_key(record, step): step for step in steps[: len(calls)]}
    for call in calls:
        step = expected.get(call["idempotency_key"])
        if (
            step is None
            or call["stage"] not in ("observed", "reconciled")
            or call["outcome"] != "succeeded"
            or (call["org_id"], call["workspace_id"])
            != (record.org_id, record.workspace_id)
            or (call["provider"], call["operation_kind"], call["target"])
            != (step.provider, step.operation_kind, step.target)
        ):
            return PlanProgress.UNKNOWN
    return PlanProgress.COMPLETE if len(calls) == len(steps) else PlanProgress.PREFIX
