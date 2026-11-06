"""Add exact protected Knowledge settings without reading or replacing a GitHub secret."""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re

from .release_plan import Refused, connection_bindings, creation_binding, existing_binding, require, unique_object


# These are the inactive defaults in platform/infra/variables.tf. Only these
# five fields may be filled; other private release configuration stays intact.
FIELDS = {
    "knowledge_door_endpoint": ("endpoint", ""),
    "knowledge_door_vpc_id": ("vpc_id", ""),
    "knowledge_door_subnet_ids": ("subnet_ids", []),
    "knowledge_door_security_group_ids": ("security_group_ids", []),
    "knowledge_door_routing_domain": ("routing_domain", ""),
}


def same_binding(left, right):
    """Subnet and security-group order does not change the selected network."""
    return all(
        sorted(left[key]) == sorted(right[key])
        if key in ("subnet_ids", "security_group_ids")
        else left[key] == right[key]
        for key in left
    )


def merge(inputs, config, *, selectors, account, region, environment, first_create=""):
    require(isinstance(inputs, dict), "private tools inputs must be an object")
    result = copy.deepcopy(inputs)
    if not config:
        return result
    require(
        isinstance(account, str) and re.fullmatch(r"[0-9]{12}", account)
        and isinstance(region, str) and re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", region)
        and environment in {"dev", "prod"},
        "invalid protected Knowledge account, region or environment",
    )
    require(
        inputs.get("aws_account_id") == account
        and inputs.get("aws_region", "us-east-1") == region
        and inputs.get("environment", "dev") == environment,
        "private tools inputs differ from the protected Knowledge environment",
    )
    gateway_id, _ = existing_binding(selectors, account=account, region=region)
    gateway_arn = f"arn:aws:bedrock-agentcore:{region}:{account}:gateway/{gateway_id}"
    binding = creation_binding(config, gateway_arn=gateway_arn, region=region, environment=environment)
    if first_create:
        creation = creation_binding(first_create, gateway_arn=gateway_arn, region=region, environment=environment)
        require(same_binding(binding, creation), "Knowledge configuration and first-create binding differ")
    for field, (source, inactive) in FIELDS.items():
        current = inputs.get(field, inactive)
        selected = binding[source]
        if type(current) is type(inactive) and current == inactive:
            result[field] = copy.deepcopy(selected)
            continue
        matches = current == selected
        if isinstance(inactive, list):
            matches = (
                isinstance(current, list) and all(isinstance(item, str) for item in current)
                and len(set(current)) == len(current) and sorted(current) == sorted(selected)
            )
        require(matches, f"existing {field} conflicts with protected Knowledge configuration")
        # Preserve an already matching original value, including list order.
    return result


def merge_connections(inputs, config, *, selectors, account, region, environment, first_create=""):
    """Fill only reviewed Connections targets, retaining the existing private edge."""
    result = copy.deepcopy(inputs)
    if not config:
        require(not first_create, "connection creation requires durable configuration")
        return result
    require(isinstance(account, str) and re.fullmatch(r"[0-9]{12}", account)
            and isinstance(region, str) and re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", region)
            and environment in {"dev", "prod"}, "invalid protected connection environment")
    require(isinstance(inputs, dict) and inputs.get("aws_account_id") == account
            and inputs.get("aws_region", "us-east-1") == region
            and inputs.get("environment", "dev") == environment,
            "private tools inputs differ from the protected connection environment")
    gateway_id, _ = existing_binding(selectors, account=account, region=region)
    gateway_arn = f"arn:aws:bedrock-agentcore:{region}:{account}:gateway/{gateway_id}"
    bindings = connection_bindings(config, gateway_arn=gateway_arn, region=region, environment=environment)
    creation = connection_bindings(first_create, gateway_arn=gateway_arn, region=region, environment=environment)
    require(set(creation) <= set(bindings), "connection creation selects an unconfigured target")
    providers = result.setdefault("external_oauth_provider_arns", {})
    owned = result.get("oauth_provider_secret_ids", {})
    require(isinstance(providers, dict) and isinstance(owned, dict), "invalid existing provider ownership")
    for target, binding in bindings.items():
        if target in creation:
            require(same_binding(binding, creation[target]), "connection configuration and first-create binding differ")
        # All mediator targets reuse the already configured Knowledge edge.
        # Do not provision or redirect that edge as a side effect of app setup.
        for field, (source, inactive) in FIELDS.items():
            current = result.get(field)
            selected = binding[source]
            if source == "endpoint":
                selected = selected.removesuffix(f"/{target}/mcp") + "/knowledge/mcp"
            matches = current == selected
            if isinstance(inactive, list):
                matches = (isinstance(current, list) and all(isinstance(item, str) for item in current)
                           and len(set(current)) == len(current) and sorted(current) == sorted(selected))
            require(matches, "connection targets must retain the configured private edge")
        field = f"{target}_mediator_endpoint"
        require(result.get(field, "") in ("", binding["endpoint"]), "existing connection endpoint conflicts with protected configuration")
        result[field] = binding["endpoint"]
        vendor = "atlassian" if target == "jira" else target
        require(vendor not in owned, "Connections cannot adopt a Terraform-owned provider")
        require(vendor not in providers or providers[vendor] == binding["provider_arn"],
                "existing connection provider conflicts with protected configuration")
        providers[vendor] = binding["provider_arn"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        inputs = json.loads(args.inputs.read_text(), object_pairs_hook=unique_object)
        output = merge(
            inputs, os.environ.get("ADP_TOOLS_KNOWLEDGE_CONFIG_JSON", ""),
            selectors=os.environ.get("ADP_TOOLS_RELEASE_SELECTORS_JSON", ""),
            account=os.environ.get("ADP_TOOLS_ACCOUNT_ID", ""),
            region=os.environ.get("ADP_TOOLS_AWS_REGION", ""),
            environment=os.environ.get("ADP_TOOLS_ENVIRONMENT", ""),
            first_create=os.environ.get("ADP_TOOLS_KNOWLEDGE_CREATE_JSON", ""),
        )
        output = merge_connections(
            output, os.environ.get("ADP_TOOLS_CONNECTIONS_CONFIG_JSON", ""),
            selectors=os.environ.get("ADP_TOOLS_RELEASE_SELECTORS_JSON", ""),
            account=os.environ.get("ADP_TOOLS_ACCOUNT_ID", ""),
            region=os.environ.get("ADP_TOOLS_AWS_REGION", ""),
            environment=os.environ.get("ADP_TOOLS_ENVIRONMENT", ""),
            first_create=os.environ.get("ADP_TOOLS_CONNECTIONS_CREATE_JSON", ""),
        )
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(output, stream, separators=(",", ":"))
            stream.write("\n")
    except Refused as error:
        parser.exit(2, f"Knowledge configuration refused: {error}\n")
    except (OSError, ValueError, TypeError):
        parser.exit(2, "Knowledge configuration refused: invalid private input or output file\n")


if __name__ == "__main__":
    main()
