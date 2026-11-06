"""Replay real provider unknown-field shapes against synthetic authority."""

import copy
import json
from pathlib import Path

import pytest

from installation.config import Refusal
from installation.runtime_preparation import inspect_plan
from .test_runtime_preparation_execution import execution
from .test_runtime_preparation import contract_input
from .test_paid_worker import native

__all__ = ["execution", "contract_input", "native"]


@pytest.fixture
def provider_plan(execution):
    proposal, plan = execution[4:6]
    plan = copy.deepcopy(plan)
    shapes = json.loads(
        (
            Path(__file__).parent / "fixtures/runtime-create-aws-6.65-shapes.json"
        ).read_text()
    )["after_unknown"]
    for resource in plan["resource_changes"]:
        resource["change"]["after_unknown"] = shapes[resource["address"]]
    configs = plan["configuration"]["root_module"]["resources"]
    for role in ("worker", "observer"):
        configs.append(
            {
                "address": "aws_iam_role." + role,
                "expressions": {"managed_policy_arns": {"constant_value": []}},
            }
        )
    configs.append(
        {
            "address": "aws_sqs_queue.operations",
            "expressions": {
                key: {"constant_value": ""}
                for key in ("policy", "redrive_policy", "redrive_allow_policy")
            },
        }
    )
    return plan, proposal


def test_real_provider_create_shape_with_exact_empty_intent(provider_plan):
    plan, proposal = provider_plan
    assert inspect_plan(plan, proposal)


@pytest.mark.parametrize(
    "key",
    [
        "managed_policy_arns",
        "inline_policy",
        "policy",
        "redrive_policy",
        "redrive_allow_policy",
    ],
)
@pytest.mark.parametrize("attack", ["reference", "nonempty", "unknown-on-existing"])
def test_unresolved_or_added_authority_still_refused(provider_plan, key, attack):
    plan, proposal = provider_plan
    address = (
        "aws_iam_role.worker"
        if key in {"managed_policy_arns", "inline_policy"}
        else "aws_sqs_queue.operations"
    )
    resource = next(r for r in plan["resource_changes"] if r["address"] == address)
    config = next(
        r
        for r in plan["configuration"]["root_module"]["resources"]
        if r["address"] == address
    )
    if attack == "reference":
        config["expressions"][key] = {"references": ["var.unreviewed_authority"]}
    elif attack == "nonempty":
        resource["change"]["after"][key] = (
            [{"name": "foreign", "policy": "{}"}]
            if key == "inline_policy"
            else ["arn:aws:iam::aws:policy/AdministratorAccess"]
            if key == "managed_policy_arns"
            else "{}"
        )
    else:
        for r in plan["resource_changes"]:
            r["change"]["actions"] = ["no-op"]
    with pytest.raises(Refusal):
        inspect_plan(plan, proposal, existing=attack == "unknown-on-existing")


@pytest.mark.parametrize(
    "address,key",
    [
        ("aws_iam_role.worker", "managed_policy_arns"),
        ("aws_sqs_queue.operations", "policy"),
        ("aws_sqs_queue.operations", "redrive_policy"),
        ("aws_sqs_queue.operations", "redrive_allow_policy"),
    ],
)
def test_omitted_empty_intent_does_not_qualify(provider_plan, address, key):
    plan, proposal = provider_plan
    config = next(
        r
        for r in plan["configuration"]["root_module"]["resources"]
        if r["address"] == address
    )
    config["expressions"].pop(key)
    with pytest.raises(Refusal):
        inspect_plan(plan, proposal)


def test_inline_policy_unknown_needs_saved_role_configuration(provider_plan):
    plan, proposal = provider_plan
    configs = plan["configuration"]["root_module"]["resources"]
    configs[:] = [r for r in configs if r["address"] != "aws_iam_role.worker"]
    with pytest.raises(Refusal):
        inspect_plan(plan, proposal)
