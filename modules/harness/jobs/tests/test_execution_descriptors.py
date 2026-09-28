"""Lifecycle-sized plans keep request, approval and execution bounds intact."""

import json
from dataclasses import replace

import pytest

from harness_jobs.execution_descriptors import (
    MAX_EXECUTION_STEPS,
    ExecutionStep,
    encode_execution_steps,
    parse_execution_steps,
)
from harness_jobs.identity import (
    MAX_PARAMETER_VALUE_LENGTH,
    MAX_TOTAL_PARAMETER_BYTES,
    ContractViolation,
    OperationRequest,
    decode_payload,
    encode_payload,
    payload_digest,
)


def lifecycle_steps(count=32):
    return tuple(
        ExecutionStep(
            f"step-{i:02}",
            "aws",
            "create",
            f"arn:aws:eks:us-east-1:123456789012:cluster/workspace-bound-target-{i}",
        )
        for i in range(count)
    )


def test_full_lifecycle_round_trips_without_losing_approval_bound_descriptors():
    steps = lifecycle_steps()
    payload = encode_execution_steps(steps)
    assert len(payload) > MAX_PARAMETER_VALUE_LENGTH
    request = OperationRequest(
        action="provision",
        idempotency_key="lifecycle",
        parameters={"execution_steps": payload},
    )
    decoded = decode_payload(encode_payload(request))
    assert parse_execution_steps(decoded.parameters["execution_steps"]) == steps
    assert payload_digest(decoded) == payload_digest(request)
    for changed in (
        (steps[1], steps[0], *steps[2:]),
        (*steps[:-1], replace(steps[-1], target="different-target")),
        steps[:-1],
    ):
        other = replace(
            request, parameters={"execution_steps": encode_execution_steps(changed)}
        )
        assert payload_digest(other) != payload_digest(request)


@pytest.mark.parametrize(
    "key", ["description", "Execution_steps", "execution_steps_extra"]
)
def test_large_scalar_or_similar_parameter_never_gets_plan_exemption(key):
    with pytest.raises(ContractViolation, match="value exceeds"):
        OperationRequest(
            action="provision",
            idempotency_key="large",
            parameters={key: encode_execution_steps(lifecycle_steps())},
        )


def test_lifecycle_plan_shares_the_unchanged_aggregate_byte_budget():
    payload = encode_execution_steps(lifecycle_steps())
    parameters = {"execution_steps": payload, **{f"p{i}": "x" * 1900 for i in range(8)}}
    assert (
        sum(len(k.encode()) + len(v.encode()) for k, v in parameters.items())
        > MAX_TOTAL_PARAMETER_BYTES
    )
    with pytest.raises(ContractViolation, match="in total"):
        OperationRequest(
            action="provision", idempotency_key="large", parameters=parameters
        )


def test_plan_unicode_uses_bytes_not_character_budget():
    payload = encode_execution_steps(lifecycle_steps(1))
    payload = payload.replace("workspace-bound-target-0", "界" * 1900)
    parameters = {"execution_steps": payload, **{f"p{i}": "x" * 1900 for i in range(6)}}
    assert (
        sum(len(k) + len(v) for k, v in parameters.items()) < MAX_TOTAL_PARAMETER_BYTES
    )
    with pytest.raises(ContractViolation, match="in total"):
        OperationRequest(
            action="provision", idempotency_key="unicode", parameters=parameters
        )


@pytest.mark.parametrize(
    "payload", ["x" * 2001, "[]" + " " * 2000, "[" * 3000 + "]" * 3000]
)
def test_long_plan_exception_requires_an_actual_bounded_plan(payload):
    with pytest.raises(ContractViolation, match="execution-step plan"):
        OperationRequest(
            action="provision",
            idempotency_key="malformed",
            parameters={"execution_steps": payload},
        )


def test_plan_count_ceiling_is_enforced_before_admission_and_execution():
    steps = lifecycle_steps(MAX_EXECUTION_STEPS)
    assert parse_execution_steps(encode_execution_steps(steps)) == steps
    extra = (*steps, replace(steps[-1], step_id="excess"))
    with pytest.raises(ValueError, match="step count"):
        encode_execution_steps(extra)
    payload = json.dumps([vars(s) for s in extra])
    with pytest.raises(ContractViolation, match="execution-step plan"):
        OperationRequest(
            action="provision",
            idempotency_key="too-many",
            parameters={"execution_steps": payload},
        )


@pytest.mark.parametrize(
    "variant",
    [
        "duplicate-step",
        "duplicate-field",
        "extra-field",
        "blank",
        "nul",
        "object",
        "long-field",
    ],
)
def test_ambiguous_or_malformed_descriptors_are_not_interpreted(variant):
    items = [vars(step) for step in lifecycle_steps(2)]
    if variant == "duplicate-step":
        items[1]["step_id"] = items[0]["step_id"]
    elif variant == "extra-field":
        items[0]["role"] = "admin"
    elif variant in {"blank", "nul", "object", "long-field"}:
        items[0]["target"] = {
            "blank": " ",
            "nul": "x\x00y",
            "object": {},
            "long-field": "x" * 2049,
        }[variant]
    payload = json.dumps(items)
    if variant == "duplicate-field":
        payload = payload.replace(
            '"provider": "aws"', '"provider": "other", "provider": "aws"', 1
        )
    with pytest.raises(ValueError):
        parse_execution_steps(payload)
