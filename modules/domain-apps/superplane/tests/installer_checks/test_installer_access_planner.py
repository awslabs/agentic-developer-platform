"""Ownership and update boundaries for the maintained access planner."""

import copy
import importlib.util
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "installer_access_planner", MODULE / "scripts/plan-installer-access.py"
)
planner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)


def plan(actions):
    return {
        "resource_changes": [
            {"address": address, "mode": "managed", "change": {"actions": actions}}
            for address in sorted(planner.OWNED_RESOURCES)
        ]
    }


def test_initial_and_repeat_update():
    assert len(planner.inspect_changes(plan(["create"]), update=False)) == 5
    assert len(planner.inspect_changes(plan(["no-op"]), update=True)) == 5
    current = plan(["no-op"])
    for resource in current["resource_changes"]:
        if resource["address"] == "aws_iam_role_policy.installer":
            resource["change"]["actions"] = ["update"]
    assert len(planner.inspect_changes(current, update=True)) == 5


@pytest.mark.parametrize("actions", [["create"], ["delete"], ["delete", "create"]])
def test_update_refuses_resource_lifecycle_changes(actions):
    with pytest.raises(SystemExit):
        planner.inspect_changes(plan(actions), update=True)


@pytest.mark.parametrize(
    "address", sorted(planner.OWNED_RESOURCES - {"aws_iam_role_policy.installer"})
)
def test_policy_update_cannot_change_trust_or_kubernetes_access(address):
    current = plan(["no-op"])
    for resource in current["resource_changes"]:
        if resource["address"] == address:
            resource["change"]["actions"] = ["update"]
    with pytest.raises(SystemExit):
        planner.inspect_changes(current, update=True)


def test_missing_or_extra_plan_ownership_refused():
    missing = plan(["no-op"])
    missing["resource_changes"].pop()
    extra = plan(["no-op"])
    extra["resource_changes"].append(
        {
            "address": "aws_iam_role.unrelated",
            "mode": "managed",
            "change": {"actions": ["no-op"]},
        }
    )
    for current in [missing, extra]:
        with pytest.raises(SystemExit):
            planner.inspect_changes(current, update=True)


@pytest.fixture
def retained():
    role = {
        "RoleName": "installer",
        "Arn": "arn:aws:iam::123456789012:role/installer",
        "RoleId": "AROAIMMUTABLE",
    }
    resources = []
    for address in sorted(planner.OWNED_RESOURCES):
        kind, name = address.split(".")
        resources.append(
            {
                "type": kind,
                "name": name,
                "mode": "managed",
                "instances": [{"attributes": {}}],
            }
        )
    next(r for r in resources if r["type"] == "aws_iam_role")["instances"][0][
        "attributes"
    ] = {
        "name": role["RoleName"],
        "arn": role["Arn"],
        "unique_id": role["RoleId"],
    }
    return {
        "lineage": "retained-state-lineage",
        "serial": 3,
        "resources": resources,
    }, role


def test_update_requires_retained_immutable_role(retained):
    state, role = retained
    planner.retained_owner(state, role)
    with pytest.raises(SystemExit):
        planner.retained_owner(state, {**role, "RoleId": "AROAREPLACED"})


def test_refresh_cannot_accept_a_recreated_role(retained):
    _, role = retained
    current = plan(["no-op"])
    change = next(
        r
        for r in current["resource_changes"]
        if r["address"] == "aws_iam_role.installer"
    )["change"]
    attributes = {"arn": role["Arn"], "unique_id": role["RoleId"]}
    change.update(before=dict(attributes), after=dict(attributes))
    planner.planned_owner(current, role)
    change["after"]["unique_id"] = "AROARECREATED"
    with pytest.raises(SystemExit):
        planner.planned_owner(current, role)


@pytest.mark.parametrize(
    "fault", ["empty", "missing", "module", "deposed", "duplicate", "no-lineage"]
)
def test_update_refuses_ambiguous_or_absent_state(retained, fault):
    state, role = copy.deepcopy(retained)
    if fault == "empty":
        state["resources"] = []
    elif fault == "missing":
        state["resources"].pop()
    elif fault == "module":
        state["resources"][0]["module"] = "module.foreign"
    elif fault == "deposed":
        next(r for r in state["resources"] if r["type"] == "aws_iam_role")["instances"][
            0
        ]["deposed"] = "old"
    elif fault == "duplicate":
        state["resources"].append(state["resources"][0])
    else:
        state.pop("lineage")
    with pytest.raises(SystemExit):
        planner.retained_owner(state, role)
