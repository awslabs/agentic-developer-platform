"""Private STS is priced and may reference only this workspace's owned resources."""

import copy

import pytest

from check_workspace_plan import (
    _estimate,
    HOURS_PER_MONTH,
    INTERFACE_ENDPOINT_AZ_HOURLY_USD,
)
from workspace_ownership import WorkspaceOwnershipError
from test_node_parent_ownership import report
from test_plan_safety import _genuine_plan
from test_relationship_side_ownership import existing_plan, entry


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize(
    "address,field",
    [
        ("aws_vpc_endpoint.private_sts[0]", "vpc_id"),
        ("aws_vpc_endpoint.private_sts[0]", "subnet_ids"),
        ("aws_vpc_endpoint.private_sts[0]", "security_group_ids"),
        (
            "aws_vpc_security_group_ingress_rule.private_sts_nodes[0]",
            "security_group_id",
        ),
        (
            "aws_vpc_security_group_ingress_rule.private_sts_nodes[0]",
            "referenced_security_group_id",
        ),
    ],
)
def test_private_sts_cannot_modify_foreign_relationship(side, address, field):
    plan = existing_plan()
    assert report(plan).ok
    change = entry(plan, address)["change"]
    change["actions"] = ["delete", "create"]
    change[side][field] = ["foreign"] if field.endswith("ids") else "foreign"
    result = report(plan)
    assert not result.ok
    assert any(
        problem.address == address and side + "." + field in problem.reason
        for problem in result.violations
    )


def test_computed_node_group_reference_requires_exact_owned_cluster_attribute():
    plan = _genuine_plan(creating=True)
    assert report(plan).ok
    expression = next(
        item
        for item in plan["configuration"]["root_module"]["resources"]
        if item["address"] == "aws_vpc_security_group_ingress_rule.private_sts_nodes"
    )
    expression["expressions"]["referenced_security_group_id"]["references"] = [
        "aws_eks_cluster.workspace.id",
        "aws_eks_cluster.workspace",
    ]
    assert not report(plan).ok


def test_endpoint_fixed_cost_uses_conservative_owned_subnet_ceiling():
    plan = _genuine_plan(creating=True)
    plan["resource_changes"] = [
        item
        for item in plan["resource_changes"]
        if item["address"].startswith(("aws_vpc_endpoint.", "aws_subnet."))
    ]
    baseline = copy.deepcopy(plan)
    baseline["resource_changes"] = [
        item
        for item in baseline["resource_changes"]
        if not item["address"].startswith("aws_vpc_endpoint.")
    ]
    estimate = _estimate(plan, aws_region="us-east-1")
    previous = _estimate(baseline, aws_region="us-east-1")
    assert estimate["bounded_monthly_usd"] - previous[
        "bounded_monthly_usd"
    ] == pytest.approx(4 * HOURS_PER_MONTH * INTERFACE_ENDPOINT_AZ_HOURLY_USD)
    assert any(
        "endpoint data processing" in line
        for line in estimate["not_bounded_by_this_estimate"]
    )


def test_unbounded_endpoint_az_count_is_not_priced_at_zero():
    plan = {
        "resource_changes": [
            {
                "address": "aws_vpc_endpoint.private_sts[0]",
                "change": {
                    "actions": ["create"],
                    "after": {"vpc_endpoint_type": "Interface"},
                },
            }
        ]
    }
    with pytest.raises(WorkspaceOwnershipError, match="subnet ceiling is unknown"):
        _estimate(plan, aws_region="us-east-1")
