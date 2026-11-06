import copy
import uuid

import pytest

from workspace_provisioning.artifacts import digest
from workspace_provisioning.partial_apply_plan import (
    CLUSTER,
    EIP,
    repair_changes,
    state_inventory,
)
from workspace_provisioning.runtime_config import LifecycleRefused


@pytest.fixture
def evidence():
    target = dict(
        account_id="123456789012",
        aws_region="us-west-2",
        environment="test",
        org_id=str(uuid.uuid4()),
        workspace_id=str(uuid.uuid4()),
    )
    name = "adp-test-spw-" + digest([target["org_id"], target["workspace_id"]])[:32]
    raw = {
        "version": 4,
        "terraform_version": "1.9.8",
        "serial": 2,
        "lineage": str(uuid.uuid4()),
        "resources": [
            {
                "mode": "managed",
                "type": "aws_eks_cluster",
                "name": "workspace",
                "instances": [
                    {
                        "attributes": {
                            "id": name,
                            "arn": f"arn:aws:eks:us-west-2:123456789012:cluster/{name}",
                        }
                    }
                ],
            },
            {
                "mode": "managed",
                "type": "aws_eip",
                "name": "nat",
                "instances": [
                    {
                        "index_key": 0,
                        "status": "tainted",
                        "attributes": {"id": "eipalloc-0123456789abcdef0"},
                    }
                ],
            },
        ],
    }
    inventory = state_inventory(raw, target)

    def change(address, kind, actions, before, reason=None):
        return {
            "mode": "managed",
            "address": address,
            "type": kind,
            "provider_name": "registry.terraform.io/hashicorp/aws",
            "change": {"actions": actions, "before": before},
            "action_reason": reason,
        }

    plan = {
        "format_version": "1.2",
        "errored": False,
        "complete": True,
        "applyable": True,
        "terraform_version": "1.9.8",
        "resource_changes": [
            change(CLUSTER, "aws_eks_cluster", ["no-op"], {"id": name}),
            change(
                EIP,
                "aws_eip",
                ["delete", "create"],
                {"id": "eipalloc-0123456789abcdef0"},
                "replace_because_tainted",
            ),
            change("aws_nat_gateway.workspace[0]", "aws_nat_gateway", ["create"], None),
        ],
    }
    return raw, target, inventory, plan


def test_retains_cluster_and_reviews_exact_tainted_eip_without_editing_state(evidence):
    raw, target, inventory, plan = evidence
    before = copy.deepcopy(raw)
    review = repair_changes(plan, inventory)
    assert review == {
        "created": ["aws_nat_gateway.workspace[0]"],
        "preserved": [CLUSTER],
        "replaced": [
            {
                "address": EIP,
                "id": "eipalloc-0123456789abcdef0",
                "actions": ["delete", "create"],
            }
        ],
    }
    assert raw == before and inventory["serial"] == 2


@pytest.mark.parametrize(
    "bad",
    [
        "foreign-cluster",
        "nodegroup",
        "other-taint",
        "deposed",
        "unversioned",
        "zero-serial",
        "duplicate",
        "module",
        "untainted",
    ],
)
def test_partial_state_rejects_ambiguous_or_unsupported_ownership(evidence, bad):
    raw, target, _, _ = evidence
    cluster, eip = raw["resources"]
    if bad == "foreign-cluster":
        cluster["instances"][0]["attributes"]["arn"] += "-foreign"
    elif bad == "nodegroup":
        raw["resources"].append(
            {
                "mode": "managed",
                "type": "aws_eks_node_group",
                "name": "default",
                "instances": [{"attributes": {"id": "nodegroup"}}],
            }
        )
    elif bad == "other-taint":
        cluster["instances"][0]["status"] = "tainted"
    elif bad == "deposed":
        eip["instances"][0]["deposed"] = "deadbeef"
    elif bad == "unversioned":
        del raw["lineage"]
    elif bad == "zero-serial":
        raw["serial"] = 0
    elif bad == "duplicate":
        raw["resources"].append(copy.deepcopy(eip))
    elif bad == "module":
        cluster["module"] = "module.foreign"
    elif bad == "untainted":
        eip["instances"][0].pop("status")
    with pytest.raises(LifecycleRefused):
        state_inventory(raw, target)


@pytest.mark.parametrize(
    "bad",
    [
        "replace-cluster",
        "update-cluster",
        "missing-cluster",
        "foreign-eip",
        "eip-create-first",
        "other-reason",
        "import",
        "move",
        "provider",
        "duplicate",
        "missing-new",
        "unrecorded-update",
        "unknown-type",
    ],
)
def test_repair_plan_cannot_replace_existing_cluster_or_expand_scope(evidence, bad):
    _, _, inventory, plan = evidence
    cluster, eip, new = plan["resource_changes"]
    if bad == "replace-cluster":
        cluster["change"]["actions"] = ["delete", "create"]
    elif bad == "update-cluster":
        cluster["change"]["actions"] = ["update"]
    elif bad == "missing-cluster":
        plan["resource_changes"].remove(cluster)
    elif bad == "foreign-eip":
        eip["change"]["before"]["id"] = "eipalloc-00000000000000000"
    elif bad == "eip-create-first":
        eip["change"]["actions"] = ["create", "delete"]
    elif bad == "other-reason":
        eip["action_reason"] = "replace_because_cannot_update"
    elif bad == "import":
        new["change"]["importing"] = {"id": "foreign"}
    elif bad == "move":
        cluster["previous_address"] = "aws_eks_cluster.foreign"
    elif bad == "provider":
        new["provider_name"] = "custom/aws"
    elif bad == "duplicate":
        plan["resource_changes"].append(copy.deepcopy(eip))
    elif bad == "missing-new":
        plan["resource_changes"].remove(new)
    elif bad == "unrecorded-update":
        new["change"] = {"actions": ["update"], "before": {"id": "nat-foreign"}}
    elif bad == "unknown-type":
        new["type"] = "aws_iam_user"
    with pytest.raises(LifecycleRefused):
        repair_changes(plan, inventory)


@pytest.mark.parametrize(
    "field,value",
    [("errored", True), ("complete", False), ("applyable", False), ("complete", None)],
)
def test_incomplete_or_unapplyable_repair_is_refused(evidence, field, value):
    _, _, inventory, plan = evidence
    plan[field] = value
    with pytest.raises(LifecycleRefused, match="incomplete"):
        repair_changes(plan, inventory)
