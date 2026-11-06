"""Bound a fresh repair plan to retained owned state without editing that state.

This verifier grants no execution authority. The protected owner, fresh planning
operation and exact repair approval must independently bind its inputs. Existing
cloud resources are preserved, except one explicitly reviewed unused tainted NAT
EIP, whose delete/create replacement remains in the saved-plan authorization.
"""

import json
import re
import uuid

from .artifacts import digest
from .runtime_config import LifecycleRefused

EIP = "aws_eip.nat[0]"
CLUSTER = "aws_eks_cluster.workspace"
# First supported recovery boundary: control plane created, managed nodes/NAT
# not yet created. Later partial phases need their own observed recovery recipe.
RETAINED_TYPES = frozenset(
    {
        "aws_cloudwatch_log_group",
        "aws_eip",
        "aws_eks_addon",
        "aws_eks_cluster",
        "aws_iam_openid_connect_provider",
        "aws_iam_role",
        "aws_iam_role_policy",
        "aws_iam_role_policy_attachment",
        "aws_internet_gateway",
        "aws_launch_template",
        "aws_route_table",
        "aws_route_table_association",
        "aws_security_group",
        "aws_subnet",
        "aws_vpc",
        "aws_vpc_security_group_egress_rule",
        "aws_vpc_security_group_ingress_rule",
        "aws_vpc_endpoint",
    }
)


def require(value, reason):
    if not value:
        raise LifecycleRefused("partial apply repair refused: " + reason)


def state_inventory(state, target):
    require(isinstance(state, dict), "state is not an object")
    require(
        state.get("version") == 4 and state.get("terraform_version") == "1.9.8",
        "state format changed",
    )
    try:
        uuid.UUID(state["lineage"])
    except (ValueError, KeyError, TypeError, AttributeError):
        raise LifecycleRefused(
            "partial apply repair refused: state lineage unavailable"
        ) from None
    require(
        type(state.get("serial")) is int and state["serial"] > 0,
        "state serial unavailable",
    )
    resources = state.get("resources")
    require(
        isinstance(resources, list) and 0 < len(resources) <= 500,
        "state resource bound",
    )
    name = (
        "adp-"
        + target["environment"]
        + "-spw-"
        + digest([target["org_id"], target["workspace_id"]])[:32]
    )
    expected_cluster = (
        f"arn:aws:eks:{target['aws_region']}:{target['account_id']}:cluster/{name}"
    )
    result = {}
    for resource in resources:
        require(
            isinstance(resource, dict) and resource.get("mode") in {"managed", "data"},
            "resource shape",
        )
        if resource["mode"] == "data" or resource.get("type") == "terraform_data":
            continue
        kind = resource.get("type")
        instances = resource.get("instances")
        require(isinstance(instances, list) and len(instances) <= 100, "instance bound")
        if not instances:
            continue
        require(
            kind in RETAINED_TYPES and not resource.get("module"),
            "unsupported partial phase or child module",
        )
        for instance in instances:
            require(
                isinstance(instance, dict) and not instance.get("deposed"),
                "deposed resource",
            )
            address = kind + "." + resource["name"]
            if "index_key" in instance:
                index = instance["index_key"]
                require(type(index) in {int, str}, "index type")
                address += "[" + json.dumps(index, separators=(",", ":")) + "]"
            attributes = instance.get("attributes")
            require(
                isinstance(attributes, dict)
                and isinstance(attributes.get("id"), str)
                and attributes["id"],
                "provider identity",
            )
            require(address not in result, "duplicate state address")
            status = instance.get("status")
            require(status in {None, "tainted"}, "unknown state status")
            require(status != "tainted" or address == EIP, "another tainted resource")
            result[address] = {
                "type": kind,
                "id": attributes["id"],
                "tainted": status == "tainted",
            }
            if address == CLUSTER:
                require(
                    attributes.get("arn") == expected_cluster
                    and attributes["id"] == name,
                    "cluster ownership",
                )
    require(
        CLUSTER in result and EIP in result and result[EIP]["tainted"],
        "unsupported partial state boundary",
    )
    require(re.fullmatch(r"eipalloc-[a-f0-9]+", result[EIP]["id"]), "EIP identity")
    return {
        "lineage": state["lineage"],
        "serial": state["serial"],
        "resources": result,
        "cluster_arn": expected_cluster,
    }


def repair_changes(plan, inventory):
    """Preserve every recorded AWS identity and allow only the bound EIP replacement."""
    require(
        isinstance(plan, dict) and plan.get("format_version") == "1.2", "plan format"
    )
    require(
        plan.get("terraform_version") == "1.9.8"
        and plan.get("errored") is False
        and plan.get("complete") is True
        and plan.get("applyable") is True,
        "plan incomplete",
    )
    retained = inventory["resources"]
    seen, created, replaced = set(), [], []
    changes = plan.get("resource_changes")
    require(isinstance(changes, list) and 0 < len(changes) <= 500, "plan change bound")
    for item in changes:
        require(isinstance(item, dict), "plan resource shape")
        if item.get("mode") == "data" or item.get("type") == "terraform_data":
            continue
        address, kind = item.get("address"), item.get("type")
        require(
            item.get("mode") == "managed"
            and isinstance(address, str)
            and address not in seen,
            "ambiguous planned resource",
        )
        require(
            item.get("provider_name") == "registry.terraform.io/hashicorp/aws",
            "provider changed",
        )
        require(
            not item.get("previous_address") and not item.get("module_address"),
            "resource ownership moved",
        )
        change = item.get("change")
        require(
            isinstance(change, dict) and not change.get("importing"), "resource import"
        )
        actions, before = change.get("actions"), change.get("before")
        if address in retained:
            old = retained[address]
            require(
                kind == old["type"]
                and isinstance(before, dict)
                and before.get("id") == old["id"],
                "retained identity changed",
            )
            if address == EIP:
                require(
                    old["tainted"]
                    and actions == ["delete", "create"]
                    and item.get("action_reason") == "replace_because_tainted",
                    "EIP replacement reason changed",
                )
                replaced.append(
                    {"address": address, "id": old["id"], "actions": actions}
                )
            else:
                require(actions == ["no-op"], "existing infrastructure would change")
        else:
            require(
                actions == ["create"] and before is None, "unrecorded existing resource"
            )
            require(
                kind in RETAINED_TYPES | {"aws_nat_gateway", "aws_eks_node_group"},
                "unsupported new resource",
            )
            created.append(address)
        seen.add(address)
    require(set(retained) <= seen, "retained resource missing from plan")
    require(len(replaced) == 1 and created, "repair must finish missing infrastructure")
    return {
        "created": sorted(created),
        "replaced": replaced,
        "preserved": sorted(set(retained) - {EIP}),
    }
