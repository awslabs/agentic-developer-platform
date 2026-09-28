"""The immutable admitted plan shared by execution, lease claims and recovery."""

import hashlib
import json
from enum import Enum

from .effects import CallEffect, call_effect
from .execution_descriptors import (
    ExecutionStep as ExecutionStep,
)
from .execution_descriptors import (
    encode_execution_steps as encode_execution_steps,
)
from .execution_descriptors import parse_execution_steps
from .identity import ContractViolation
from .store import _record, stored_outcome


def admitted_steps(record):
    """Read only the stored plan, never worker arguments or mutable service config.

    The admission approval binds the digest of the entire request, including this
    ordered JSON parameter. Production composition must submit these descriptors
    before approval/admission; absence cannot default to worker-selected calls.
    """
    try:
        return parse_execution_steps(
            record.admitted_request().parameters["execution_steps"]
        )
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
    planned_keys = {step_key(record, step) for step in steps}
    # Trusted post-plan provider observations are diagnostic reads, not extra
    # mutating plan steps. Planned reads still need their exact descriptor and
    # successful observation. Unknown/mutating extra calls remain a refusal.
    calls = [
        call
        for call in calls
        if call["idempotency_key"] in planned_keys
        or call_effect(call["operation_kind"], provider=call["provider"])
        is not CallEffect.OBSERVES
    ]
    if not calls or len(calls) > len(steps):
        return PlanProgress.UNKNOWN
    expected = {step_key(record, step): step for step in steps[: len(calls)]}
    for call in calls:
        step = expected.get(call["idempotency_key"])
        if (
            step is None
            or call["stage"] not in ("observed", "reconciled")
            # The stored column carries the provider's free text after the enum, so
            # this must compare the decoded enum. A raw equality test here read every
            # call that returned any detail as unconfirmed, which is fail-closed for
            # this function but makes a finished plan permanently unfinishable.
            or stored_outcome(call["outcome"]) != "succeeded"
            or (call["org_id"], call["workspace_id"])
            != (record.org_id, record.workspace_id)
            or (call["provider"], call["operation_kind"], call["target"])
            != (step.provider, step.operation_kind, step.target)
        ):
            return PlanProgress.UNKNOWN
    return PlanProgress.COMPLETE if len(calls) == len(steps) else PlanProgress.PREFIX
