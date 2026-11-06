"""Validate the saved target release plan, before applying that exact artifact.

IAM scopes target operations to the parent gateway. Only the protected workflow
binding may authorize first creation of exact private mediator targets. This file
and deploy.sh are compared with the running workflow revision before assuming
the release role, including when an older main source is selected.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import sys


GATEWAY = "aws_bedrockagentcore_gateway.tools"
KNOWLEDGE = "aws_bedrockagentcore_gateway_target.knowledge[0]"
POLICY = "aws_bedrockagentcore_resource_policy.gateway"
TARGET_ADDRESS = re.compile(r'^aws_bedrockagentcore_gateway_target\.(tools|credentials|knowledge)(\[.*\])?$')
CONNECTION_TARGETS = {name: f"aws_bedrockagentcore_gateway_target.{name}_user[0]" for name in ("github", "jira", "slack")}
AUTHORITY_FIELDS = (
    "name", "gateway_identifier", "credential_provider_configuration",
    "target_configuration", "private_endpoint", "metadata_configuration",
    "certificate_configuration", "region",
)


class Refused(ValueError):
    """Messages describe the invariant, never private plan/input content."""


def require(condition, message):
    if not condition:
        raise Refused(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON field")
        result[key] = value
    return result


def unknown(value):
    if isinstance(value, dict):
        return any(unknown(item) for item in value.values())
    if isinstance(value, list):
        return any(unknown(item) for item in value)
    return value is True


def compact(value):
    """Terraform encodes absent optional provider blocks as null or empty lists."""
    if isinstance(value, dict):
        return {key: compact(item) for key, item in value.items() if item not in (None, [], {})}
    if isinstance(value, list):
        return [compact(item) for item in value]
    return value


def identifiers(value, prefix, minimum, maximum):
    return (
        isinstance(value, list) and minimum <= len(value) <= maximum
        and all(isinstance(item, str) and re.fullmatch(rf"{prefix}-[a-f0-9]{{8,17}}", item) for item in value)
        and len(set(value)) == len(value)
    )


def existing_binding(raw, *, account, region):
    require(bool(raw), "protected existing gateway and target selectors are required")
    try:
        selectors = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, TypeError):
        raise Refused("invalid protected existing release selectors") from None
    require(isinstance(selectors, dict) and set(selectors) == {"version", "account", "region", "cluster", "components"}
            and type(selectors["version"]) is int and selectors["version"] == 1
            and selectors["account"] == account and selectors["region"] == region,
            "release selectors must bind the protected account and region")
    require(isinstance(selectors["components"], dict), "release selector components are missing")
    targets = selectors["components"].get("platform targets")
    require(isinstance(targets, dict) and set(targets) == {"gateway_id", "target_ids"}, "complete existing gateway/target selectors required")
    identifier = targets["gateway_id"]
    require(isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9_-]+", identifier), "invalid selected gateway ID")
    ids = targets["target_ids"]
    require(isinstance(ids, list) and bool(ids)
            and all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in ids)
            and len(ids) == len(set(ids)), "invalid or duplicate selected existing target IDs")
    return identifier, set(ids)


def creation_binding(raw, *, gateway_arn, region, environment, target="knowledge"):
    require(target in {"knowledge", *CONNECTION_TARGETS}, "unsupported private target")
    require(bool(raw), "new targets are disabled without a protected Knowledge creation binding")
    try:
        binding = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, TypeError):
        raise Refused("invalid protected Knowledge creation binding") from None
    require(isinstance(binding, dict) and set(binding) == {
        "version", "gateway_arn", "endpoint", "vpc_id", "subnet_ids", "security_group_ids", "routing_domain",
    }, "unexpected protected Knowledge binding fields")
    require(type(binding["version"]) is int and binding["version"] == 1, "unsupported Knowledge binding version")
    require(binding["gateway_arn"] == gateway_arn, "Knowledge binding selects another gateway")
    require(isinstance(binding["endpoint"], str) and re.fullmatch(
        rf"https://[a-z0-9-]+\.execute-api\.{re.escape(region)}\.amazonaws\.com/{environment}/internal/v1/tools/{target}/mcp",
        binding["endpoint"],
    ), "Knowledge binding must select the private mediator route")
    require(isinstance(binding["vpc_id"], str) and re.fullmatch(r"vpc-[a-f0-9]{8,17}", binding["vpc_id"]), "Knowledge VPC required")
    require(identifiers(binding["subnet_ids"], "subnet", 2, 16), "explicit Knowledge subnets required")
    require(identifiers(binding["security_group_ids"], "sg", 1, 5), "explicit Knowledge security groups required")
    require(isinstance(binding["routing_domain"], str) and re.fullmatch(
        rf"vpce-[a-f0-9]{{8,17}}-[a-z0-9]+\.execute-api\.{re.escape(region)}\.vpce\.amazonaws\.com",
        binding["routing_domain"],
    ), "Knowledge routing must use the regional endpoint DNS name")
    return binding


def connection_bindings(raw, *, gateway_arn, region, environment):
    """Explicit per-target bindings, with exact Connections-owned provider references."""
    if not raw:
        return {}
    try:
        document = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, TypeError):
        raise Refused("invalid protected connection target bindings") from None
    require(isinstance(document, dict) and set(document) == {"version", "targets"}
            and type(document["version"]) is int and document["version"] == 1,
            "unexpected connection target binding fields or version")
    targets = document["targets"]
    require(isinstance(targets, dict) and 1 <= len(targets) <= 3 and set(targets) <= set(CONNECTION_TARGETS),
            "explicit supported connection targets required")
    result = {}
    for target, value in targets.items():
        require(isinstance(value, dict) and "provider_arn" in value, "connection provider reference required")
        vendor = "atlassian" if target == "jira" else target
        account = gateway_arn.split(":")[4]
        expected = (f"arn:aws:bedrock-agentcore:{region}:{account}:token-vault/default/"
                    f"oauth2credentialprovider/adp-tools-{environment}-{vendor}-user")
        require(value["provider_arn"] == expected, "connection provider differs from selected account, region or vendor")
        network = {key: item for key, item in value.items() if key != "provider_arn"}
        binding = creation_binding(json.dumps(network), gateway_arn=gateway_arn, region=region,
                                   environment=environment, target=target)
        result[target] = {**binding, "provider_arn": expected}
    return result


def gateway_identity(resources, account, region, environment):
    matches = [resource for resource in resources if resource.get("address") == GATEWAY]
    require(len(matches) == 1, "target changes require the existing gateway in the saved plan")
    resource = matches[0]
    change = resource.get("change", {})
    require(resource.get("type") == "aws_bedrockagentcore_gateway" and change.get("actions") in (["no-op"], ["update"]),
            "gateway creation or replacement is forbidden")
    before, after = change.get("before"), change.get("after")
    require(isinstance(before, dict) and isinstance(after, dict), "existing gateway identity is missing")
    gateway_id = before.get("gateway_id")
    require(isinstance(gateway_id, str) and re.fullmatch(r"[A-Za-z0-9_-]+", gateway_id), "existing gateway ID is missing")
    arn = f"arn:aws:bedrock-agentcore:{region}:{account}:gateway/{gateway_id}"
    role = f"arn:aws:iam::{account}:role/adp-tools-{environment}-gateway"
    for field, expected in {"gateway_id": gateway_id, "gateway_arn": arn, "role_arn": role, "authorizer_type": "AWS_IAM"}.items():
        require(before.get(field) == expected and after.get(field) == expected,
                "the existing gateway account, identity, role and authorizer must be preserved")
        require(not unknown(change.get("after_unknown", {}).get(field)), "gateway authority must be known before apply")
    dynamic_targets = any(
        server.get("listing_mode") == "DYNAMIC"
        for target in resources if target.get("type") == "aws_bedrockagentcore_gateway_target"
        for configuration in (target["change"].get("after") or {}).get("target_configuration", []) or []
        for mcp in configuration.get("mcp", []) or []
        for server in mcp.get("mcp_server", []) or []
    )
    if dynamic_targets:
        protocol = after.get("protocol_configuration")
        require(not unknown(change.get("after_unknown", {}).get("protocol_configuration"))
                and isinstance(protocol, list) and len(protocol) == 1
                and isinstance(protocol[0].get("mcp"), list) and len(protocol[0]["mcp"]) == 1
                and protocol[0]["mcp"][0].get("search_type") in (None, ""),
                "dynamic MCP targets require known gateway configuration without semantic tool search")
    return gateway_id, arn


def private_target(resource, binding, gateway_id, *, target="knowledge", creating=True):
    change = resource["change"]
    require(resource["address"] == (KNOWLEDGE if target == "knowledge" else CONNECTION_TARGETS[target]),
            "private target address differs from its protected binding")
    if creating:
        require("before" in change and change["before"] is None, "only first creation of a private target is permitted")
    after = change.get("after")
    require(isinstance(after, dict), "Private target target values are missing")
    unresolved = deepcopy(change.get("after_unknown", {}))
    # AWS rejects resourcePriority for DYNAMIC targets, while the provider marks
    # this optional+computed field unknown on creation. Only that exact leaf may
    # be unresolved; the known projection below must still omit any priority.
    try:
        server = unresolved["target_configuration"][0]["mcp"][0]["mcp_server"][0]
        if server.get("resource_priority") is True:
            del server["resource_priority"]
    except (KeyError, IndexError, TypeError, AttributeError):
        pass
    require(not any(unknown(unresolved.get(field)) for field in AUTHORITY_FIELDS),
            "Private target authority fields must be known before apply")
    require(after.get("name") == target and after.get("gateway_identifier") == gateway_id,
            "Private target target name or gateway does not match")
    require(compact(after.get("credential_provider_configuration")) == [{"gateway_iam_role": [{"service": "execute-api"}]}],
            "Private target requires only execute-api gateway IAM credentials")
    require(compact(after.get("target_configuration")) == [{"mcp": [{"mcp_server": [{
        "endpoint": binding["endpoint"], "listing_mode": "DYNAMIC",
    }]}]}], "Private target requires the exact DYNAMIC mediator endpoint")
    network = compact(after.get("private_endpoint"))
    require(isinstance(network, list) and len(network) == 1 and set(network[0]) == {"managed_vpc_resource"},
            "Private target requires only the selected managed private endpoint")
    managed = network[0]["managed_vpc_resource"]
    require(isinstance(managed, list) and len(managed) == 1 and isinstance(managed[0], dict), "Private target network is missing")
    selected = dict(managed[0])
    for field in ("subnet_ids", "security_group_ids"):
        require(identifiers(selected.get(field), "subnet" if field == "subnet_ids" else "sg", 2 if field == "subnet_ids" else 1, 16 if field == "subnet_ids" else 5),
                "Private target network identifiers are invalid")
        selected[field] = sorted(selected[field])
    require(selected == {
        "vpc_identifier": binding["vpc_id"], "subnet_ids": sorted(binding["subnet_ids"]),
        "security_group_ids": sorted(binding["security_group_ids"]), "endpoint_ip_address_type": "IPV4",
        "routing_domain": binding["routing_domain"],
    }, "Private target network differs from the protected binding")
    require(not compact(after.get("metadata_configuration")) and not compact(after.get("certificate_configuration")),
            "unexpected Private target metadata or certificate forwarding")


def validate(plan, *, account, region, environment, release_selectors="", knowledge_binding="", connections_binding="", connections_config=""):
    require(re.fullmatch(r"[0-9]{12}", account) and re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", region)
            and environment in {"dev", "prod"}, "invalid release account, region or environment")
    require(isinstance(plan, dict) and isinstance(plan.get("resource_changes"), list), "saved plan resource changes are missing")
    resources = plan["resource_changes"]
    expected_gateway, expected_targets = existing_binding(release_selectors, account=account, region=region)
    require(all(isinstance(resource, dict) and isinstance(resource.get("change"), dict) for resource in resources), "invalid saved plan resource")
    addresses = [resource.get("address") for resource in resources]
    require(all(isinstance(address, str) for address in addresses) and len(set(addresses)) == len(addresses), "duplicate or missing resource address")
    for resource in resources:
        require(not resource.get("previous_address") and not resource["change"].get("importing"), "release cannot move or import state ownership")
        if resource.get("mode") == "data":
            require(resource.get("type") == "aws_lambda_function" and resource["address"] == "data.aws_lambda_function.shared_tools[0]"
                    and resource["change"].get("actions") in (["no-op"], ["read"]), "unapproved data source in release plan")
            after = resource["change"].get("after")
            require(isinstance(after, dict) and after.get("function_name") == f"adp-{environment}-shared-tools-shared"
                    and after.get("region", region) == region
                    and not any(unknown(resource["change"].get("after_unknown", {}).get(field)) for field in ("function_name", "region")),
                    "shared tools data read must bind the existing regional function")
            continue
        require(resource["change"].get("actions") in (["no-op"], ["update"], ["create"]), "release cannot delete, replace or change bootstrap-owned resources")
        require(isinstance(resource["change"].get("after_unknown", {}), dict), "invalid unknown-field projection")
    gateway_id, gateway_arn = gateway_identity(resources, account, region, environment)
    require(gateway_id == expected_gateway, "saved plan gateway differs from protected selectors")
    connection_create = connection_bindings(connections_binding, gateway_arn=gateway_arn, region=region, environment=environment)
    connection_config = connection_bindings(connections_config, gateway_arn=gateway_arn, region=region, environment=environment)
    before_ids = []
    for resource in resources:
        if resource.get("mode", "managed") == "managed" and resource.get("type") == "aws_bedrockagentcore_gateway_target":
            before = resource["change"].get("before")
            if before is not None:
                require(isinstance(before, dict) and before.get("gateway_identifier") == expected_gateway
                        and isinstance(before.get("target_id"), str), "existing target is outside the protected gateway")
                before_ids.append(before["target_id"])
    require(len(before_ids) == len(set(before_ids)) and set(before_ids) == expected_targets,
            "saved plan existing targets differ from protected selectors")
    changes = [resource for resource in resources if resource.get("mode", "managed") == "managed" and resource["change"]["actions"] != ["no-op"]]
    creations = 0
    for resource in changes:
        address, kind, change = resource["address"], resource.get("type"), resource["change"]
        require(resource.get("mode", "managed") == "managed", "release cannot change bootstrap-owned resources")
        require(isinstance(change.get("after"), dict) and change["after"].get("region", region) == region
                and not unknown(change.get("after_unknown", {}).get("region")), "resource must remain in the selected region")
        if address == GATEWAY and kind == "aws_bedrockagentcore_gateway":
            require(change["actions"] == ["update"], "release cannot create a gateway")
        elif address == POLICY and kind == "aws_bedrockagentcore_resource_policy":
            for state in (change.get("before"), change.get("after")):
                require(state is None or state.get("resource_arn") == gateway_arn, "resource policy selects another gateway")
            require(isinstance(change.get("after"), dict) and not unknown(change.get("after_unknown", {}).get("resource_arn")),
                    "resource policy gateway must be known")
        elif kind == "aws_bedrockagentcore_gateway_target" and (TARGET_ADDRESS.fullmatch(address) or address in CONNECTION_TARGETS.values()):
            target = next((name for name, selected in CONNECTION_TARGETS.items() if selected == address), None)
            if change["actions"] == ["create"]:
                if target is None:
                    creations += 1
                    require(creations == 1, "only one new Knowledge target may be released")
                    binding = creation_binding(knowledge_binding, gateway_arn=gateway_arn, region=region, environment=environment)
                    private_target(resource, binding, gateway_id)
                else:
                    require(target in connection_create and target in connection_config,
                            "new connection targets are disabled without protected creation and durable bindings")
                    private_target(resource, connection_create[target], gateway_id, target=target)
                    private_target(resource, connection_config[target], gateway_id, target=target)
            else:
                if target is not None:
                    require(target in connection_config, "connection target updates require their durable protected binding")
                    private_target(resource, connection_config[target], gateway_id, target=target, creating=False)
                before, after = change.get("before"), change.get("after")
                require(isinstance(before, dict) and isinstance(after, dict), "existing target identity is missing")
                require(before.get("gateway_identifier") == gateway_id and after.get("gateway_identifier") == gateway_id,
                        "existing targets must remain under their selected gateway")
                target_id = before.get("target_id")
                require(isinstance(target_id, str) and bool(target_id) and after.get("target_id") == target_id,
                        "existing target identity must be preserved")
                require(not any(unknown(change.get("after_unknown", {}).get(field)) for field in ("gateway_identifier", "target_id")),
                        "existing target identity must be known")
        else:
            raise Refused("release cannot change bootstrap-owned or unapproved resources")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("plan", type=Path)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--environment", required=True)
    args = parser.parse_args()
    try:
        plan = json.loads(args.plan.read_text(), object_pairs_hook=unique_object)
        validate(plan, account=args.account, region=args.region, environment=args.environment,
                 release_selectors=os.environ.get("ADP_TOOLS_RELEASE_SELECTORS_JSON", ""),
                 knowledge_binding=os.environ.get("ADP_TOOLS_KNOWLEDGE_CREATE_JSON", ""),
                 connections_binding=os.environ.get("ADP_TOOLS_CONNECTIONS_CREATE_JSON", ""),
                 connections_config=os.environ.get("ADP_TOOLS_CONNECTIONS_CONFIG_JSON", ""))
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as error:
        detail = str(error) if isinstance(error, Refused) else "invalid saved plan"
        print(f"Tools release plan refused: {detail}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
