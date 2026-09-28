"""Replacement targets and VPC scope must be attributed independently per side."""

import copy

import pytest

from test_node_parent_ownership import report
from test_plan_safety import PREFIX, _genuine_plan, _owned_tags

TARGETS = [
    ("aws_iam_role_policy_attachment.node_worker", "role"),
    ("aws_iam_role_policy.workspace_admin[0]", "role"),
    ("aws_route_table_association.private[0]", "subnet_id"),
    ("aws_route_table_association.private[0]", "route_table_id"),
    ("aws_vpc_security_group_egress_rule.cluster_all", "security_group_id"),
]


def existing_plan(mode="owned"):
    plan = _genuine_plan(creating=False, networking=mode)
    for entry in plan["resource_changes"]:
        change = entry["change"]
        if change["actions"] == ["delete"]:
            change["actions"] = ["no-op"]
            change["after"] = copy.deepcopy(change["before"])
    for address, values in [
        (
            "aws_iam_role.workspace_admin[0]",
            {"name": f"{PREFIX}-admin", "tags_all": _owned_tags()},
        ),
        (
            "aws_iam_role_policy.workspace_admin[0]",
            {"name": f"{PREFIX}-admin-eks-access", "role": f"{PREFIX}-admin"},
        ),
    ]:
        plan["resource_changes"].append(
            {
                "address": address,
                "change": {
                    "actions": ["no-op"],
                    "before": copy.deepcopy(values),
                    "after": copy.deepcopy(values),
                },
            }
        )
    return plan


def entry(plan, address):
    return next(e for e in plan["resource_changes"] if e["address"] == address)


@pytest.mark.parametrize("address,target", TARGETS)
@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize("bad", [None, "foreign-parent"])
@pytest.mark.parametrize("actions", [["delete", "create"], ["create", "delete"]])
def test_replacement_cannot_borrow_other_sides_relationship(
    address, target, side, bad, actions
):
    plan = existing_plan()
    assert report(plan).ok
    item = entry(plan, address)
    item["change"]["actions"] = actions
    item["change"][side][target] = bad
    result = report(plan)
    assert not result.ok
    assert any(
        v.address == address and f"{side}.{target}" in v.reason
        for v in result.violations
    )


@pytest.mark.parametrize("mode", ["owned", "supplied"])
@pytest.mark.parametrize("creating", [False, True])
def test_complete_native_shaped_networking_plans_still_pass(mode, creating):
    assert report(_genuine_plan(creating=creating, networking=mode)).ok


@pytest.mark.parametrize("mode", ["owned", "supplied"])
@pytest.mark.parametrize("side", ["before", "after"])
def test_same_named_security_group_in_foreign_vpc_is_refused(mode, side):
    plan = existing_plan(mode)
    sg = entry(plan, "aws_security_group.cluster")
    sg["change"][side]["vpc_id"] = "vpc-foreign"
    assert not report(plan).ok


@pytest.mark.parametrize("tag", ["OrgId", "WorkspaceId", "Environment"])
@pytest.mark.parametrize("tag_map", ["tags", "tags_all"])
@pytest.mark.parametrize("side", ["before", "after"])
def test_security_group_name_cannot_override_conflicting_ownership_tags(
    tag, tag_map, side
):
    plan = existing_plan()
    values = entry(plan, "aws_security_group.cluster")["change"][side]
    values["tags_all"] = _owned_tags()
    values.setdefault(tag_map, {})[tag] = "foreign"
    result = report(plan)
    assert not result.ok
    assert any("contradicts immutable ownership" in v.reason for v in result.violations)


@pytest.mark.parametrize("damage", ["missing", "foreign", "wrong-type", "unmarked"])
def test_unknown_create_target_requires_authenticated_typed_reference(damage):
    plan = _genuine_plan(creating=True)
    assert report(plan).ok
    address = "aws_vpc_security_group_egress_rule.cluster_all"
    cfg = next(
        r
        for r in plan["configuration"]["root_module"]["resources"]
        if r["address"] == address
    )
    expr = cfg["expressions"]["security_group_id"]
    if damage == "missing":
        expr.clear()
    elif damage == "foreign":
        expr["references"] = ["aws_security_group.foreign.id"]
    elif damage == "wrong-type":
        expr["references"] = ["aws_iam_role.node.id"]
    else:
        entry(plan, address)["change"]["after_unknown"].clear()
    assert not report(plan).ok


def test_destroyed_target_cannot_use_replacement_parents_new_id():
    plan = existing_plan()
    subnet = entry(plan, "aws_subnet.private[0]")["change"]
    subnet["actions"] = ["delete", "create"]
    subnet["after"]["id"] = "subnet-new-owned"
    association = entry(plan, "aws_route_table_association.private[0]")["change"]
    association["before"]["subnet_id"] = "subnet-new-owned"
    assert not report(plan).ok


def test_supplied_vpc_requires_its_authenticated_configuration_binding():
    plan = _genuine_plan(creating=True, networking="supplied")
    plan["configuration"]["root_module"]["resources"] = []
    assert not report(plan).ok


def test_destructive_identity_includes_security_group_vpc():
    from check_workspace_plan import destructive_identity

    plan = _genuine_plan(creating=False, networking="supplied")
    assert (
        destructive_identity(entry(plan, "aws_security_group.cluster"))["vpc_id"]
        == "vpc-0suppliedbyowner"
    )
