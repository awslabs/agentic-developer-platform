"""Read the READY gateway target schemas used for provisioning and authorization."""

import copy
import os
import re
import time

import boto3
import yaml
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException

from src.agentauth.task_tool_policy import TOOL_PATTERN
from src.internal.tools_dynamic_catalogue import read_catalogue

# These provider operations are reached through the facade's owned recovery routes.
PROVIDER_RECOVERY_TOOLS = frozenset(
    f"{family}.{operation}"
    for family in ("websearch", "code_interpreter", "browser")
    for operation in ("operation_get", "artifact_read", "execution_close")
)


def public_tool_schema(raw: dict | None) -> dict:
    if raw is None:
        return {"type": "object"}
    if not isinstance(raw, dict) or raw.get("type") != "object":
        raise ValueError("invalid gateway tool schema")
    fields = raw.get("properties", [])
    if isinstance(fields, list):
        if any(not isinstance(field, dict) or not isinstance(field.get("name"), str) for field in fields):
            raise ValueError("invalid gateway tool fields")
        properties = {
            field["name"]: {"type": field["type"], "description": field.get("description", "")}
            for field in fields
            if field["name"] not in {"execution_id", "request_id", "adp_assertion"}
        }
        required = [field["name"] for field in fields if field.get("required") is True and field["name"] in properties]
    elif isinstance(fields, dict):
        properties = {name: field for name, field in fields.items() if name not in {"execution_id", "request_id", "adp_assertion"}}
        required = [name for name in raw.get("required", []) if name in properties]
        # Preserve actual dynamic JSON Schema constraints and local definitions.
        return {**raw, "properties": properties, "required": required}
    else:
        raise ValueError("invalid gateway tool fields")
    return {"type": "object", "properties": properties, "required": required}


def openapi_tool_schema(path: dict, action: dict) -> dict | None:
    body = action.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
    parameters = {(parameter["in"], parameter["name"]): parameter for parameter in [*path.get("parameters", []), *action.get("parameters", [])]}
    if not parameters:
        return body
    schema = public_tool_schema(body)
    properties = schema.setdefault("properties", {})
    required = schema.setdefault("required", [])
    for (_, name), parameter in parameters.items():
        properties[name] = dict(parameter["schema"])
        if "description" in parameter:
            properties[name].setdefault("description", parameter["description"])
        if parameter.get("required") and name not in required:
            required.append(name)
    return schema


# The catalogue is read from the AgentCore control plane (list_gateways, get_gateway,
# list_gateway_targets and one get_gateway_target per READY target: ~10 calls, ~1 s on dev).
# The façade asks the gateway to authorize every tool listing and every tool call, and each
# authorization rebuilt the catalogue, so ~1 s of every Dev Box tool step was spent here
# (#8223 Dev Box tool latency). Targets change only on a tools deploy, so one process keeps
# the last good static read for a short while; a failed read is never kept.
CATALOGUE_TTL_SECONDS = 120
_catalogue_cache: dict[str, tuple[float, dict]] = {}


def invalidate_catalogue_cache() -> None:
    _catalogue_cache.clear()


def gateway_catalogue() -> dict:
    """Cache static target schemas, but require current provenance for dynamic tools."""
    role = os.environ.get("ADP_TOOLS_GATEWAY_ROLE_ARN")
    if not role:
        raise HTTPException(503, "tool catalogue unavailable")
    cached = _catalogue_cache.get(role)
    now = time.monotonic()
    if cached is not None and now - cached[0] < CATALOGUE_TTL_SECONDS:
        catalogue = cached[1]
    else:
        catalogue = _read_gateway_catalogue(role)
        _catalogue_cache[role] = (now, copy.deepcopy(catalogue))
    return _current_dynamic_catalogue(catalogue)


def _append_tools(catalogue: list, name: str, credential_kinds: list, operations: list) -> None:
    for operation, description, input_schema in operations:
        tool = f"{name.replace('-', '_')}.{operation}"
        if not re.fullmatch(TOOL_PATTERN, tool):
            raise ValueError("invalid gateway tool")
        if tool in PROVIDER_RECOVERY_TOOLS:
            continue
        catalogue.append(
            {
                "name": tool,
                "description": description or tool,
                "connector": name,
                "credential_kinds": credential_kinds,
                "inputSchema": public_tool_schema(input_schema),
            }
        )


def _ordered_catalogue(tools: list, connectors: list) -> dict:
    if len(tools) > 256 or len({entry["name"] for entry in tools}) != len(tools):
        raise ValueError("duplicate or oversized gateway catalogue")
    return {"tools": sorted(tools, key=lambda entry: entry["name"]), "connectors": sorted(connectors, key=lambda entry: entry["name"])}


def _current_dynamic_catalogue(snapshot: dict) -> dict:
    """A cached target reference is neither a callable schema nor an authorization receipt."""
    result = copy.deepcopy({"tools": snapshot["tools"], "connectors": snapshot["connectors"]})
    references = snapshot["dynamic_targets"]
    if not references:
        return result
    try:
        control = boto3.client("bedrock-agentcore-control")
    except (BotoCoreError, ClientError):
        return result
    gateways = {}
    connectors = {connector["name"]: connector for connector in result["connectors"]}
    for reference in references:
        try:
            gateway_id = reference["gateway_id"]
            if gateway_id not in gateways:
                gateways[gateway_id] = control.get_gateway(gatewayIdentifier=gateway_id)
            target = control.get_gateway_target(gatewayIdentifier=gateway_id, targetId=reference["target_id"])
            if target.get("name") != reference["name"] or target.get("targetId") != reference["target_id"]:
                continue
            discovered = read_catalogue(gateway_id, target, gateways[gateway_id])
            if not discovered:
                continue
            credentials = sorted({config["credentialProviderType"] for config in target.get("credentialProviderConfigurations", [])})
            tools = []
            _append_tools(tools, reference["name"], credentials, [(tool["name"], tool["description"], tool["inputSchema"]) for tool in discovered])
        except (BotoCoreError, ClientError, KeyError, TypeError, ValueError):
            continue
        result["tools"].extend(tools)
        connector = connectors[reference["name"]]
        connector.update(credential_kinds=credentials, discovery_status="ready")
        connector.pop("discovery_reason", None)
    try:
        return _ordered_catalogue(result["tools"], result["connectors"])
    except ValueError:
        raise HTTPException(503, "tool catalogue unavailable") from None


def _read_gateway_catalogue(role: str) -> dict:
    try:
        control = boto3.client("bedrock-agentcore-control")
        gateways = control.get_paginator("list_gateways").paginate()
        matching = [
            (gateway["gatewayId"], control.get_gateway(gatewayIdentifier=gateway["gatewayId"]))
            for page in gateways
            for gateway in page["items"]
            if gateway.get("name") == "adp-tools"
        ]
        matching = [(gateway_id, gateway) for gateway_id, gateway in matching if gateway.get("roleArn") == role]
        if len(matching) != 1:
            raise ValueError("gateway identity is unavailable or ambiguous")
        gateway_id, gateway = matching[0]
        targets = control.get_paginator("list_gateway_targets").paginate(gatewayIdentifier=gateway_id)
        catalogue = []
        connectors = []
        dynamic_targets = []
        for page in targets:
            for target in page["items"]:
                if target.get("status") != "READY":
                    continue
                detail = control.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target["targetId"])
                name = detail["name"]
                if not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", name):
                    raise ValueError("invalid gateway target")
                mcp = detail["targetConfiguration"]["mcp"]
                if "lambda" in mcp:
                    operations = [
                        (entry["name"], entry.get("description", ""), entry.get("inputSchema"))
                        for entry in mcp["lambda"]["toolSchema"]["inlinePayload"]
                    ]
                elif "openApiSchema" in mcp:
                    spec = yaml.safe_load(mcp["openApiSchema"]["inlinePayload"])
                    operations = [
                        (
                            action["operationId"],
                            action.get("description") or action.get("summary", ""),
                            openapi_tool_schema(path, action),
                        )
                        for path in spec["paths"].values()
                        for method, action in path.items()
                        if method.lower() in {"get", "post", "put", "patch", "delete"}
                    ]
                elif "mcpServer" in mcp:
                    dynamic_targets.append({"gateway_id": gateway_id, "target_id": target["targetId"], "name": name})
                    operations = []
                else:
                    raise ValueError("unsupported gateway target")
                credential_kinds = sorted({config["credentialProviderType"] for config in detail.get("credentialProviderConfigurations", [])})
                connector = {"name": name, "credential_kinds": credential_kinds, "dynamic": "mcpServer" in mcp}
                if "mcpServer" in mcp:
                    connector.update(discovery_status="unavailable", discovery_reason="dynamic_catalogue_provenance_unavailable")
                connectors.append(connector)
                _append_tools(catalogue, name, credential_kinds, operations)
        return {**_ordered_catalogue(catalogue, connectors), "dynamic_targets": dynamic_targets}

    except (BotoCoreError, ClientError, KeyError, TypeError, ValueError, yaml.YAMLError):
        raise HTTPException(503, "tool catalogue unavailable") from None


def _native_credential_catalogue() -> dict:
    """Permission metadata for the configured issuer, never a generic execution target."""
    if os.environ.get("ADP_NATIVE_CREDENTIALS_ENABLED") != "true":
        return {"tools": [], "connectors": []}
    from src.auth.native_credential_authority import NativeConfig
    from src.auth.native_credential_wire import CreateRequest

    # Use the issuer's own validation. An enabled but incomplete configuration is
    # unavailable, not a reason to advertise a permission that cannot be used.
    NativeConfig.configured()
    fields = CreateRequest.model_json_schema()["properties"]
    tools = []
    for operation, description in (
        ("assume_role", "Use a 900-second AWS role session in an approved private local command through adp credential use."),
        ("materialize", "Use a credential file in an approved private local command through adp credential use."),
    ):
        selected = ("service", "label", "purpose") if operation == "assume_role" else ("service", "label")
        tools.append(
            {
                "name": f"credentials.{operation}",
                "description": description,
                "connector": "credentials",
                "credential_kinds": [],
                "native_private_delivery": True,
                "inputSchema": {
                    "type": "object",
                    "properties": {name: fields[name] for name in selected},
                    "required": ["service", "label"],
                    "additionalProperties": False,
                },
            }
        )
    return {
        "tools": tools,
        "connectors": [{"name": "credentials", "credential_kinds": [], "dynamic": False, "native_private_delivery": True}],
    }


def tool_catalogue() -> dict:
    """The tenant catalogue: deployed targets, platform reads and configured native permissions.

    ``gateway_catalogue()`` keeps its fail-closed contract for the façade authorization
    path, where every tool must be a READY gateway target. Admin entitlement, per-user
    policy, consent and access reporting use this composed view so platform reads
    and native private delivery are governed by the same layers. Native permissions
    remain explicit grants; they do not inherit platform read tools' implicit allow.
    "Not configured" is distinguished from
    "unavailable": a deployment without the tools platform (no
    ``ADP_TOOLS_GATEWAY_ROLE_ARN``) still has its first-party tools, while a configured
    gateway that cannot be read stays a 503 rather than a silently shorter catalogue.
    """
    from src.internal.tools_platform import PLATFORM_CONNECTOR, platform_catalogue

    first_party = platform_catalogue()
    native = _native_credential_catalogue()
    deployed = gateway_catalogue() if os.environ.get("ADP_TOOLS_GATEWAY_ROLE_ARN") else {"tools": [], "connectors": []}
    if any(entry["connector"] == PLATFORM_CONNECTOR for entry in deployed["tools"]) or any(
        connector["name"] == PLATFORM_CONNECTOR for connector in deployed["connectors"]
    ):
        # A gateway target that borrows the first-party namespace would make one name mean two tools.
        raise HTTPException(503, "tool catalogue unavailable")
    # The optional broker target can already describe these exact permissions.
    # Preserve its schemas and connector instead of duplicating either name.
    deployed_tools = {entry["name"] for entry in deployed["tools"]}
    deployed_connectors = {entry["name"] for entry in deployed["connectors"]}
    return {
        "tools": sorted(
            [*deployed["tools"], *first_party["tools"], *(entry for entry in native["tools"] if entry["name"] not in deployed_tools)],
            key=lambda entry: entry["name"],
        ),
        "connectors": sorted(
            [
                *deployed["connectors"],
                *first_party["connectors"],
                *(entry for entry in native["connectors"] if entry["name"] not in deployed_connectors),
            ],
            key=lambda entry: entry["name"],
        ),
    }
