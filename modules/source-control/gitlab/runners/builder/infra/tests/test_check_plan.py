"""Synthetic creation-only builder plan checks."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "check_plan.py"
spec = importlib.util.spec_from_file_location("check_builder_plan", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def plan():
    return {
        "complete": True, "applyable": True, "errored": False,
        "variables": {"account_id": {"value": "123456789012"}, "aws_region": {"value": "us-east-1"}},
        "resource_changes": [{"address": address, "mode": "managed", "change": {"actions": ["create"]}}
                             for address in sorted(module.OWNED)],
        "configuration": {"root_module": {"resources": [
            {"address": address, "mode": "managed"} for address in (
                "aws_iam_role.builder", "aws_iam_role_policy.builder", "aws_codebuild_project.builder",
                "aws_ecr_repository.runtime")
        ]}},
    }


def test_exact_creation_only_builder_plan(plan):
    module.check(plan, "123456789012", "us-east-1")

def test_existing_boundary_read_is_permitted(plan):
    plan["resource_changes"].append({"address": "data.aws_iam_policy.existing_boundary", "mode": "data",
                                     "change": {"actions": ["read"]}})
    plan["prior_state"] = {"values": {"root_module": {"resources": [
        {"address": "data.aws_iam_policy.existing_boundary", "mode": "data"}]}}}
    module.check(plan, "123456789012", "us-east-1")



@pytest.mark.parametrize("change", [
    lambda plan: plan.update(complete=False),
    lambda plan: plan.update(applyable=False),
    lambda plan: plan.update(errored=True),
    lambda plan: plan.update(deferred_changes=[{"reason": "unresolved"}]),
    lambda plan: plan["variables"]["account_id"].update(value="999999999999"),
    lambda plan: plan["variables"]["aws_region"].update(value="eu-west-1"),
    lambda plan: plan["resource_changes"][0]["change"].update(actions=["delete", "create"]),
    lambda plan: plan["resource_changes"].append({"address": "aws_iam_role.shared", "mode": "managed", "change": {"actions": ["delete"]}}),
    lambda plan: plan["configuration"]["root_module"]["resources"].append({"address": "aws_iam_policy.shared", "mode": "managed"}),
    lambda plan: plan["resource_changes"].append({"address": "data.aws_iam_policy.shared", "mode": "data", "change": {"actions": ["read"]}}),
    lambda plan: plan.update(prior_state={"values": {"root_module": {"resources": [{"address": "aws_iam_role.shared"}]}}}),
    lambda plan: plan.update(prior_state={"values": {"root_module": {"resources": [
        {"address": "data.aws_iam_policy.other", "mode": "data"}]}}}),
    lambda plan: plan.update(prior_state={"values": {"root_module": {"child_modules": [
        {"address": "module.foreign", "resources": [{"address": "module.foreign.aws_iam_role.other", "mode": "managed"}]}]}}}),
    lambda plan: plan.update(prior_state={"values": {"root_module": {"resources": [
        {"address": "data.aws_iam_policy.existing_boundary", "mode": "data"},
        {"address": "data.aws_iam_policy.existing_boundary", "mode": "data"}]}}}),
])
def test_partial_foreign_or_mutating_plans_refused(plan, change):
    change(plan)
    with pytest.raises(ValueError):
        module.check(plan, "123456789012", "us-east-1")
