"""EKS resource names cannot authorize foreign roles, networks or launch templates."""

import copy

import pytest

from test_node_parent_ownership import report
from test_plan_safety import _genuine_plan
from test_relationship_side_ownership import entry, existing_plan

TARGETS = [
    ("aws_eks_cluster.workspace", ("role_arn",)),
    ("aws_eks_cluster.workspace", ("vpc_config", 0, "subnet_ids", 0)),
    ("aws_eks_cluster.workspace", ("vpc_config", 0, "security_group_ids", 0)),
    ("aws_eks_node_group.default", ("node_role_arn",)),
    ("aws_eks_node_group.default", ("subnet_ids", 0)),
    ("aws_eks_node_group.default", ("launch_template", 0, "id")),
    ("aws_eks_addon.vpc_cni", ("service_account_role_arn",)),
]


def set_path(values, path, value):
    for part in path[:-1]:
        values = values[part]
    values[path[-1]] = value


@pytest.mark.parametrize("address,path", TARGETS)
@pytest.mark.parametrize(
    "actions,side",
    [
        (["create"], "after"),
        (["update"], "after"),
        (["update"], "before"),
        (["delete"], "before"),
        (["delete", "create"], "before"),
        (["delete", "create"], "after"),
        (["create", "delete"], "before"),
    ],
)
@pytest.mark.parametrize("mode", ["owned", "supplied"])
def test_foreign_eks_relationship_is_refused(address, path, actions, side, mode):
    plan = existing_plan(mode)
    assert report(plan).ok
    change = entry(plan, address)["change"]
    change["actions"] = actions
    if actions == ["create"]:
        change["before"] = None
    if actions == ["delete"]:
        change["after"] = None
    set_path(change[side], path, "foreign-same-account-target")
    result = report(plan)
    assert any(v.address == address and side in v.reason for v in result.violations)


@pytest.mark.parametrize("address,path", TARGETS)
def test_existing_relationship_cannot_be_unknown(address, path):
    plan = existing_plan()
    set_path(entry(plan, address)["change"]["before"], path, None)
    assert not report(plan).ok


@pytest.mark.parametrize("mode", ["owned", "supplied"])
@pytest.mark.parametrize("creating", [True, False])
def test_complete_eks_network_graph_passes(mode, creating):
    result = report(_genuine_plan(creating=creating, networking=mode))
    assert result.ok, result.violations


@pytest.mark.parametrize(
    "damage", [[], [None], ["sg-0cluster", "sg-foreign"], "sg-0cluster"]
)
def test_every_security_group_list_member_requires_evidence(damage):
    plan = existing_plan()
    entry(plan, "aws_eks_cluster.workspace")["change"]["after"]["vpc_config"][0][
        "security_group_ids"
    ] = damage
    assert not report(plan).ok


def test_before_role_cannot_use_replacement_roles_after_arn():
    plan = existing_plan()
    role = entry(plan, "aws_iam_role.cluster")["change"]
    role["actions"] = ["delete", "create"]
    role["after"]["arn"] += "-replacement"
    entry(plan, "aws_eks_cluster.workspace")["change"]["before"]["role_arn"] = role[
        "after"
    ]["arn"]
    assert not report(plan).ok


def test_unknown_role_requires_arn_reference_not_name():
    plan = _genuine_plan(creating=True)
    cfg = next(
        x
        for x in plan["configuration"]["root_module"]["resources"]
        if x["address"] == "aws_eks_cluster.workspace"
    )
    cfg["expressions"]["role_arn"] = {"references": ["aws_iam_role.cluster.name"]}
    assert not report(plan).ok


def test_destructive_identity_records_nested_targets():
    plan = existing_plan()
    cluster = entry(plan, "aws_eks_cluster.workspace")
    from check_workspace_plan import destructive_identity

    identity = destructive_identity(cluster)
    assert identity["role_arn"] == cluster["change"]["before"]["role_arn"]
    assert identity["vpc_config[].subnet_ids[]"] == [
        "subnet-0private0",
        "subnet-0private1",
    ]
    assert identity["vpc_config[].security_group_ids[]"] == ["sg-0cluster"]
    node = entry(plan, "aws_eks_node_group.default")
    assert destructive_identity(node)["launch_template[].id"] == ["lt-test-root"]


def test_drift_cannot_hide_a_foreign_cluster_role():
    plan = existing_plan()
    drift = copy.deepcopy(entry(plan, "aws_eks_cluster.workspace"))
    drift["change"]["after"]["role_arn"] = "foreign-role"
    plan["resource_drift"] = [drift]
    assert not report(plan).ok


def test_unknown_inline_policy_role_accepts_verified_iam_role_id():
    plan = _genuine_plan(creating=True)
    policy = entry(plan, "aws_iam_role_policy.node_image_pull")["change"]
    policy["after"].pop("role")
    policy["after_unknown"] = {"role": True}
    plan["configuration"]["root_module"]["resources"].append(
        {
            "address": "aws_iam_role_policy.node_image_pull",
            "expressions": {
                "role": {"references": ["aws_iam_role.node.id", "aws_iam_role.node"]}
            },
        }
    )
    assert report(plan).ok
