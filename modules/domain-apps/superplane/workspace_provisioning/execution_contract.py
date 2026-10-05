"""The approved-plan descriptor surface this package composes against.

`retirement_plan.py` must emit its deletion plan in the exact form the shared
execution layer admits and enforces: an ordered list of immutable descriptors carried
as the `execution_steps` request parameter, bound into the approval digest, and
checked at dispatch by `harness_jobs.execution_rpc.execute_step`.

## Why this is a structural copy and not an import

`src/superplane-api/tests/test_workspaces.py:908` asserts that `harness_jobs` is **not
importable** from this module's interpreter, deliberately: composing the real
operation facade is #5535's (w6-12) decision, and importability alone is not evidence
that it was made. The domain CI lane therefore installs no `harness-jobs`
distribution, so a production import here has two failure modes and no upside — it
breaks that tripwire if the package is installed, and it breaks collection of this
package's suite if it is not.

So the dependency is structural, exactly as `infra/account-provisioning/
account_provisioning/execution.py` does for `CallOutcome` and the executor: the names,
field order and bounds below are **copied from `harness_jobs.execution_descriptors`,
not invented**, and the composer passes the encoded result to the real facade.

## What keeps the copy honest

A copied vocabulary needs a drift test, and a copy that only checked itself would be
worthless. `tests/test_execution_contract_agreement.py` imports the authoritative
module (tests only, via `tests/__init__.py`) and asserts three things:

* the bounds are equal, so a plan this module accepts is not one the real admission
  layer would reject for size;
* the field names and their **order** are identical, so `encode` is byte-identical to
  the authoritative encoder — the encoding is hashed into the approval's payload
  digest, so a reordered field is a different approved plan;
* valid encodings round-trip through the authoritative `parse_execution_steps`,
  and invalid descriptors are rejected by both encoders. The runtime must reject
  them here too: tests cannot substitute for validation of the actual request.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

# Copied from `harness_jobs.execution_descriptors`. The drift test asserts equality
# against those names, so a change there fails this package's suite rather than
# silently letting it compose a plan admission would refuse.
MAX_EXECUTION_STEPS = 64
MAX_EXECUTION_PLAN_BYTES = 16_384
MAX_DESCRIPTOR_VALUE_LENGTH = 2048


@dataclass(frozen=True)
class ExecutionStep:
    """One provider call in the exact request covered by plan approval.

    Field order is load-bearing, not cosmetic: `encode_execution_steps` serializes
    `asdict(step)`, and the resulting string is hashed into the approval's payload
    digest. A reordered field would produce a different digest for the same logical
    plan, which admission reads as a changed request under a used idempotency key.
    """

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
    """Apply the authoritative descriptor rules before returning an encoded plan."""
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


def encode_execution_steps(
    steps: tuple[ExecutionStep, ...] | list[ExecutionStep],
) -> str:
    """Serialize the approved descriptor list without changing its order.

    Byte-identical to the authoritative encoder: same separators, same
    `ensure_ascii=False`, same field order. Asserted by the drift test rather than
    assumed.
    """
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
