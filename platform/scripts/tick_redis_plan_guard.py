"""Permit only the exact additions needed to share the gateway Redis budget."""
from __future__ import annotations

import json
import re
from shared_runtime_plan_guard import known, normalized, require, unchanged
from tick_redis import KEYS, TICK

PREFIX = "module.orchestration_tick[0]."
LAMBDA = PREFIX + "aws_lambda_function.tick"
SG = PREFIX + "aws_security_group.tick"
INGRESS = PREFIX + "aws_security_group_rule.redis_from_tick[0]"
IAM = PREFIX + "aws_iam_role_policy.tick_redis[0]"
TARGETS = {LAMBDA, SG, INGRESS, IAM}


def redis_policy(context):
    return {"Version": "2012-10-17", "Statement": [{"Sid": "ConnectSharedBudgetRedis", "Effect": "Allow", "Action": ["elasticache:Connect"], "Resource": context["resource_arns"]}]}


def egress_rule(context):
    return {"description": "Redis budget access to the existing gateway store", "from_port": context["port"], "to_port": context["port"], "protocol": "tcp", "security_groups": [context["redis_sg"]], "cidr_blocks": [], "ipv6_cidr_blocks": [], "prefix_list_ids": [], "self": False}


def network_rules(rules):
    result = []
    for rule in rules:
        item = dict(rule)
        # AWS provider plans may represent omitted collection attributes as null;
        # refresh represents the same empty collections as [].
        for key in ("cidr_blocks", "ipv6_cidr_blocks", "prefix_list_ids", "security_groups"):
            if item.get(key) is None:
                item[key] = []
        result.append(item)
    return normalized(result)


def plan_shape(plan):
    """Resource identities/actions only; no keyed addresses or provider values."""
    result = []
    for resource in plan.get("resource_changes", []):
        actions = resource.get("change", {}).get("actions")
        address = resource.get("address")
        require(isinstance(address, str) and bool(re.fullmatch(r"[A-Za-z0-9_.\[\]-]{1,256}", address)), "unprojectable Terraform resource address")
        require(isinstance(actions, list) and all(value in {"no-op", "read", "create", "update", "delete", "forget"} for value in actions), "unknown Terraform action")
        if actions != ["no-op"]:
            result.append({"address": address, "actions": actions})
    return result


def check_plan(plan, context):
    require(not plan.get("errored") and not plan.get("deferred_changes"), "incomplete Redis plan")
    changes = []
    seen = set()
    for resource in plan.get("resource_changes", []):
        change = resource["change"]
        actions = change["actions"]
        address = resource["address"]
        require(address not in seen, "duplicate planned resource")
        seen.add(address)
        require(not change.get("importing") and not resource.get("previous_address"), "Redis plan cannot import or move resources")
        require("delete" not in actions and "forget" not in actions, "Redis plan cannot destroy resources")
        if actions == ["no-op"] or resource.get("mode") == "data" and actions == ["read"]:
            continue
        require(resource.get("mode", "managed") == "managed" and address in TARGETS, "Redis plan changes an unrelated resource")
        before, after, unknown = change.get("before"), change.get("after") or {}, change.get("after_unknown") or {}
        if address == LAMBDA:
            require(actions == ["update"] and isinstance(before, dict), "Redis maintenance cannot create Lambda")
            require(known({k: v for k, v in unknown.items() if k != "last_modified"}), "Lambda change contains unresolved fields")
            unchanged(before, after, {"environment", "last_modified"}, "Redis plan changes unrelated Lambda settings or image")
            require(before.get("image_uri") == after.get("image_uri") == context["image_uri"], "Lambda image changed since diagnosis")
            require(before.get("function_name") == TICK and before.get("role") == context["configuration"]["Role"], "Lambda identity changed")
            require(before.get("environment") == [{"variables": context["configuration"]["Environment"]["Variables"]}], "Lambda environment changed since diagnosis")
            expected = context["configuration"]["Environment"]["Variables"] | context["expected_env"]
            require(after.get("environment") == [{"variables": expected}], "Redis plan does not preserve the entire existing environment")
            require(set(context["expected_env"]) == set(KEYS), "unapproved environment additions")
        elif address == SG:
            require(actions == ["update"] and isinstance(before, dict) and known(unknown), "Redis security group change is unresolved")
            require(before.get("id") == context["configuration"]["VpcConfig"]["SecurityGroupIds"][0], "tick security group changed")
            unchanged(before, after, {"egress"}, "Redis plan changes unrelated security group settings")
            expected = before["egress"] + [egress_rule(context)]
            require(network_rules(after["egress"]) == network_rules(expected), "Redis plan must add only the exact peer and port egress rule")
        elif address == INGRESS:
            require(actions == ["create"] and before is None, "Redis ingress must be a new scoped rule")
            require(known({k: v for k, v in unknown.items() if k not in {"id", "security_group_rule_id"}}), "Redis ingress is unresolved")
            expected = {"description": "Redis budget access from the orchestration tick", "type": "ingress", "protocol": "tcp", "from_port": context["port"], "to_port": context["port"], "security_group_id": context["redis_sg"], "source_security_group_id": context["configuration"]["VpcConfig"]["SecurityGroupIds"][0], "self": False}
            require(all(after.get(k) == v for k, v in expected.items()), "Redis ingress scope changed")
            require(set(after) <= set(expected) | {"id", "security_group_rule_id", "cidr_blocks", "ipv6_cidr_blocks", "prefix_list_ids"}, "unexpected Redis ingress fields")
            require(not any(after.get(k) for k in ("cidr_blocks", "ipv6_cidr_blocks", "prefix_list_ids")), "Redis ingress broadens network access")
        elif address == IAM:
            require(actions == ["create"] and before is None, "Redis policy must not replace existing permissions")
            require(known({k: v for k, v in unknown.items() if k not in {"id", "name_prefix"}}), "Redis policy is unresolved")
            require(after.get("name") == TICK + "-redis" and after.get("role") == context["configuration"]["Role"].rsplit("/", 1)[1], "Redis IAM role or policy identity changed")
            require(normalized(json.loads(after["policy"])) == normalized(redis_policy(context)), "Redis permission exceeds the existing gateway store")
        changes.append({"address": address, "actions": actions})
    require(context.get("required_changes", set()) <= {row["address"] for row in changes}, "Redis plan omits a diagnosed missing prerequisite")
    return changes
