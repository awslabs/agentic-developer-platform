"""An EKS node-group name is unique only within its parent cluster."""

import copy
import pytest

from test_plan_safety import (
    ACCOUNT,
    ENVIRONMENT,
    ORG_ID,
    PREFIX,
    WORKSPACE,
    _cluster,
    _node_group,
    _plan,
)
from workspace_ownership import validate_plan
from check_workspace_plan import destructive_identity


def report(plan):
    return validate_plan(
        plan,
        environment=ENVIRONMENT,
        workspace_name=WORKSPACE,
        org_id=ORG_ID,
        workspace_id=WORKSPACE,
        account_id=ACCOUNT,
    )


@pytest.mark.parametrize(
    "actions", [("create",), ("no-op",), ("delete",), ("delete", "create")]
)
def test_owned_parent_relationship_is_valid_on_each_action(actions):
    assert report(_plan(_node_group(actions), _cluster(list(actions)))).ok


@pytest.mark.parametrize(
    "side,actions",
    [
        ("before", ("delete",)),
        ("before", ("delete", "create")),
        ("after", ("delete", "create")),
        ("after", ("create",)),
        ("before", ("no-op",)),
        ("after", ("no-op",)),
    ],
)
@pytest.mark.parametrize("parent", ["foreign-production-cluster", None])
def test_foreign_or_absent_parent_is_refused_on_every_present_side(
    side, actions, parent
):
    node = copy.deepcopy(_node_group(actions))
    node["change"][side] = {**node["change"][side], "cluster_name": parent}
    result = report(_plan(node, _cluster(list(actions))))
    assert not result.ok
    assert any("cluster_name" in v.reason for v in result.violations)


def test_correct_parent_spelling_without_verified_cluster_cannot_vouch_for_ownership():
    plan = _plan(_node_group(("delete",)))
    plan["resource_changes"] = [
        x
        for x in plan["resource_changes"]
        if x["address"] != "aws_eks_cluster.workspace"
    ]
    assert not report(plan).ok


def test_destructive_evidence_records_parent_cluster():
    node = _node_group(("delete",))
    assert destructive_identity(node)["cluster_name"] == PREFIX
