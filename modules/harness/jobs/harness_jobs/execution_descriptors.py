"""Bounded immutable descriptors shared by admission, execution and recovery."""

import json
from dataclasses import asdict, dataclass

MAX_EXECUTION_STEPS = 64
MAX_EXECUTION_PLAN_BYTES = 16_384
MAX_DESCRIPTOR_VALUE_LENGTH = 2048


@dataclass(frozen=True)
class ExecutionStep:
    """One provider call in the exact request covered by plan approval."""

    step_id: str
    provider: str
    operation_kind: str
    target: str


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate descriptor field")
        result[key] = value
    return result


def parse_execution_steps(payload):
    """Validate before interpreting a plan; never truncate or guess a descriptor."""
    if not isinstance(payload, str) or len(payload.encode()) > MAX_EXECUTION_PLAN_BYTES:
        raise ValueError("Execution plan exceeds its byte limit")
    try:
        data = json.loads(payload, object_pairs_hook=_unique_fields)
    except RecursionError as exc:
        raise ValueError("Execution plan nesting is invalid") from exc
    if not isinstance(data, list) or not 1 <= len(data) <= MAX_EXECUTION_STEPS:
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
            not isinstance(v, str)
            or not v.strip()
            or len(v) > MAX_DESCRIPTOR_VALUE_LENGTH
            or "\x00" in v
            for v in item.values()
        ):
            raise ValueError("Invalid descriptor value")
        steps.append(ExecutionStep(**item))
    if len({s.step_id for s in steps}) != len(steps):
        raise ValueError("Duplicate step")
    return tuple(steps)


def encode_execution_steps(steps):
    """Serialize the approved descriptor list without changing its order."""
    if not isinstance(steps, tuple | list) or not all(
        isinstance(step, ExecutionStep) for step in steps
    ):
        raise ValueError("ExecutionStep sequence required")
    if not 1 <= len(steps) <= MAX_EXECUTION_STEPS:
        raise ValueError("Invalid step count")
    payload = json.dumps(
        [asdict(step) for step in steps], separators=(",", ":"), ensure_ascii=False
    )
    parse_execution_steps(payload)
    return payload
