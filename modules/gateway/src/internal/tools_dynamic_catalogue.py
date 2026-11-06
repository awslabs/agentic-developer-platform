"""Fresh, protected metadata emitted by the private knowledge target itself.

Only the IAM-authenticated mediator publishes these records. Readers rebind them
against current control-plane state and protected source configuration on every
use; no user policy, consent, entitlement or cost ceiling is written here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from urllib.parse import urlsplit

import boto3
from botocore.exceptions import BotoCoreError, ClientError

TTL_SECONDS = 60
KNOWLEDGE_TTL_SECONDS = 10 * 60
MAX_BYTES = 262144
MAX_TOOLS = 64
RESERVED = frozenset(
    {
        "adp_assertion",
        "principal",
        "tenant_id",
        "owner_sub",
        "github_login",
        "github_teams",
        "root_human_id",
        "run_id",
        "workload_id",
        "receipt_id",
        "caller_role_arn",
    }
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def target_binding(gateway_id: str, target: dict, gateway: dict, *, metadata_only=False) -> dict:
    """Require the reviewed private mediator, not an arbitrary dynamic URL."""
    namespace = target.get("name")
    if namespace not in {"knowledge", "github", "jira", "slack", "atlassian"}:
        raise ValueError("unsupported private target")
    prefix = f"ADP_TOOLS_{namespace.upper()}"
    if os.environ.get(prefix + "_MEDIATOR_ENABLED") != "true":
        raise ValueError("mediator disabled")
    source = os.environ.get(prefix + "_SOURCE_REVISION", "")
    endpoint = os.environ.get(prefix + "_MEDIATOR_URL", "")
    parsed = urlsplit(endpoint)
    region = os.environ.get("AWS_REGION", "")
    expected_id = os.environ.get(prefix + "_TARGET_ID", "")
    allowed_states = {"READY", "CREATING", "UPDATING", "SYNCHRONIZING"} if metadata_only else {"READY"}
    mcp = target.get("targetConfiguration", {}).get("mcp", {}).get("mcpServer", {})
    if (
        not re.fullmatch(r"[a-f0-9]{40}", source)
        or parsed.scheme != "https"
        or not region
        or not parsed.hostname
        or not parsed.hostname.endswith(f".execute-api.{region}.amazonaws.com")
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.port not in (None, 443)
        or not re.fullmatch(rf"/[A-Za-z0-9_-]+/internal/v1/tools/{namespace}/mcp", parsed.path)
        or gateway_id != os.environ.get(prefix + "_GATEWAY_ID")
        or not target.get("targetId")
        or (expected_id and target["targetId"] != expected_id)
        or (not metadata_only and not expected_id)
        or target.get("name") != namespace
        or target.get("status") not in allowed_states
        or mcp.get("endpoint") != endpoint
        or mcp.get("listingMode") != "DYNAMIC"
        or "mcpToolSchema" in mcp
        or not target.get("privateEndpoint", {}).get("managedVpcResource")
        or not target.get("updatedAt")
    ):
        raise ValueError("dynamic target binding unavailable")
    credentials = target.get("credentialProviderConfigurations")
    if (
        not isinstance(credentials, list)
        or len(credentials) != 1
        or credentials[0].get("credentialProviderType") != "GATEWAY_IAM_ROLE"
        or credentials[0].get("credentialProvider", {}).get("iamCredentialProvider", {}).get("service") != "execute-api"
        or credentials[0].get("credentialProvider", {}).get("iamCredentialProvider", {}).get("region", region) != region
    ):
        raise ValueError("dynamic target IAM binding unavailable")
    role = os.environ.get("ADP_TOOLS_GATEWAY_ROLE_ARN", "")
    if not re.fullmatch(r"arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+", role):
        raise ValueError("dynamic issuer unavailable")
    if (
        gateway.get("gatewayId") != gateway_id
        or gateway.get("roleArn") != role
        or gateway.get("status") != "READY"
        or gateway.get("authorizerType") != "AWS_IAM"
        or not gateway.get("interceptorConfigurations")
        or not gateway.get("gatewayArn")
        or target.get("gatewayArn") != gateway["gatewayArn"]
        or not gateway.get("updatedAt")
    ):
        raise ValueError("dynamic gateway binding unavailable")
    configuration = {
        key: target.get(key) for key in ("targetConfiguration", "credentialProviderConfigurations", "privateEndpoint", "metadataConfiguration")
    }
    return {
        "gateway_id": gateway_id,
        "target_id": target["targetId"],
        "target_name": target["name"],
        "gateway_role_arn": role,
        "source_revision": source,
        "endpoint_sha256": hashlib.sha256(endpoint.encode()).hexdigest(),
        "configuration_sha256": hashlib.sha256(canonical(configuration)).hexdigest(),
        "target_updated_at": str(target["updatedAt"]),
        "target_status": target["status"],
        "gateway_updated_at": str(gateway["updatedAt"]),
        "gateway_configuration_sha256": hashlib.sha256(
            canonical(
                {
                    key: gateway.get(key)
                    for key in (
                        "roleArn",
                        "kmsKeyArn",
                        "authorizerType",
                        "authorizerConfiguration",
                        "interceptorConfigurations",
                        "protocolConfiguration",
                        "policyEngineConfiguration",
                    )
                }
            )
        ).hexdigest(),
    }


def atlassian_tool_names():
    names = {"search_jira", "get_jira_issue", "search_confluence", "get_confluence_page"}
    if os.environ.get("ADP_TOOLS_ATLASSIAN_WRITES_ENABLED") == "true":
        names.update({"create_jira_issue", "update_jira_issue", "comment_jira_issue", "transition_jira_issue"})
    if os.environ.get("ADP_TOOLS_ATLASSIAN_SYSTEM_ENABLED") == "true":
        names.add("get_project_context")
    return names


def validate_tools(tools: list, *, namespace=None) -> list:
    max_bytes = 8 * MAX_BYTES + 8192 if namespace == "atlassian" else MAX_BYTES
    if not isinstance(tools, list) or not tools or len(tools) > MAX_TOOLS or len(canonical(tools)) > max_bytes:
        raise ValueError("dynamic schema list unavailable")
    names = set()
    result = []
    for tool in tools:
        if not isinstance(tool, dict):
            raise ValueError("invalid dynamic tool")
        name, schema = tool.get("name"), tool.get("inputSchema")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", name)
            or "___" in name
            or name in names
            or not isinstance(tool.get("description"), str)
            or not isinstance(schema, dict)
            or schema.get("type") != "object"
            or not isinstance(schema.get("properties", {}), dict)
            or RESERVED.intersection(schema.get("properties", {}))
            or not isinstance(schema.get("required", []), list)
            or any(not isinstance(field, str) or field not in schema.get("properties", {}) for field in schema.get("required", []))
        ):
            raise ValueError("invalid dynamic schema")
        if namespace == "atlassian" and (len(canonical(schema)) > MAX_BYTES or len(tool["description"].encode()) > 4096):
            raise ValueError("dynamic schema list unavailable")
        names.add(name)
        result.append({"name": name, "description": tool["description"], "inputSchema": schema})
    return result


def _key(binding):
    return {"pk": {"S": "TOOLS_DYNAMIC_CATALOGUE#" + binding["gateway_id"]}, "sk": {"S": "TARGET#" + binding["target_id"]}}


def catalogue_ttl(binding):
    return KNOWLEDGE_TTL_SECONDS if binding["target_name"] == "knowledge" else TTL_SECONDS


def _schema_key(binding, name):
    return {"pk": {"S": "TOOLS_DYNAMIC_CATALOGUE#" + binding["gateway_id"]}, "sk": {"S": "TARGET#" + binding["target_id"] + "#SCHEMA#" + name}}


def publish_catalogue(*, binding: dict, tools: list, client, table_name: str, now=None) -> None:
    """Called only after verified target transport and successful native listing."""
    now = int(time.time()) if now is None else now
    tools = validate_tools(tools, namespace=binding.get("target_name"))
    record = {
        "version": "1",
        "issuer": "adp-private-" + binding["target_name"] + "-mediator",
        "binding": binding,
        "observed_at": now,
        "expires_at": now + catalogue_ttl(binding),
        "tools_sha256": hashlib.sha256(canonical(tools)).hexdigest(),
    }
    if binding["target_name"] == "atlassian" and len(canonical(tools)) > MAX_BYTES:
        record["version"] = "2"
        record["tool_digests"] = {tool["name"]: hashlib.sha256(canonical(tool)).hexdigest() for tool in tools}
        record["tool_order"] = [tool["name"] for tool in tools]
        children = [
            {
                "Put": {
                    "TableName": table_name,
                    "Item": {
                        **_schema_key(binding, tool["name"]),
                        "document": {"S": canonical(tool).decode()},
                    },
                }
            }
            for tool in tools
        ]
        root = {
            "Put": {
                "TableName": table_name,
                "Item": {
                    **_key(binding),
                    "document": {"S": canonical(record).decode()},
                    "observed_at": {"N": str(now)},
                },
                "ConditionExpression": "attribute_not_exists(pk) OR observed_at <= :now",
                "ExpressionAttributeValues": {":now": {"N": str(now)}},
            }
        }
        client.transact_write_items(TransactItems=[*children, root])
        return
    record["tools"] = tools
    client.put_item(
        TableName=table_name,
        Item={**_key(binding), "document": {"S": canonical(record).decode()}, "observed_at": {"N": str(now)}},
        ConditionExpression="attribute_not_exists(pk) OR observed_at <= :now",
        ExpressionAttributeValues={":now": {"N": str(now)}},
    )


def read_catalogue(gateway_id: str, target: dict, gateway: dict) -> list | None:
    """Failure removes only this dynamic connector; static tools remain usable."""
    try:
        binding = target_binding(gateway_id, target, gateway)
        table = os.environ.get("AGENT_AUTHORITY_TABLE", "")
        if not table:
            return None
        client = boto3.client("dynamodb")
        item = client.get_item(TableName=table, Key=_key(binding), ConsistentRead=True).get("Item")
        if not item or len(item["document"]["S"].encode()) > MAX_BYTES + 8192:
            return None
        record = json.loads(item["document"]["S"])
        now = time.time()
        if (
            record.get("version") not in {"1", "2"}
            or record.get("issuer") != "adp-private-" + binding["target_name"] + "-mediator"
            or record.get("binding") != binding
            or type(record.get("observed_at")) is not int
            or type(record.get("expires_at")) is not int
            or not record["observed_at"] <= now < record["expires_at"]
            or not 0 < record["expires_at"] - record["observed_at"] <= catalogue_ttl(binding)
        ):
            return None
        if record["version"] == "2":
            if (
                binding["target_name"] != "atlassian"
                or type(record.get("tool_digests")) is not dict
                or type(record.get("tool_order")) is not list
                or len(record["tool_order"]) != len(atlassian_tool_names())
                or set(record["tool_order"]) != set(record["tool_digests"])
                or set(record["tool_digests"]) != atlassian_tool_names()
            ):
                return None
            tools = []
            for name in record["tool_order"]:
                expected_digest = record["tool_digests"][name]
                if not isinstance(expected_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_digest):
                    return None
                child = client.get_item(TableName=table, Key=_schema_key(binding, name), ConsistentRead=True).get("Item")
                if not child or len(child["document"]["S"].encode()) > MAX_BYTES + 8192:
                    return None
                tool = json.loads(child["document"]["S"])
                if type(tool) is not dict or tool.get("name") != name or hashlib.sha256(canonical(tool)).hexdigest() != expected_digest:
                    return None
                tools.append(tool)
        else:
            tools = record["tools"]
        tools = validate_tools(tools, namespace=binding["target_name"])
        digest = hashlib.sha256(canonical(tools)).hexdigest()
        if record.get("tools_sha256") != digest:
            return None
        if binding["target_name"] == "atlassian":
            writes = os.environ.get("ADP_TOOLS_ATLASSIAN_WRITES_ENABLED") == "true"
            expected = os.environ.get("ADP_TOOLS_ATLASSIAN_WRITES_SHA256" if writes else "ADP_TOOLS_ATLASSIAN_READS_SHA256", "")
            names = atlassian_tool_names()
            if not re.fullmatch(r"[a-f0-9]{64}", expected) or digest != expected or {tool["name"] for tool in tools} != names:
                return None
        return tools
    except (BotoCoreError, ClientError, ValueError, KeyError, TypeError, OverflowError, AttributeError):
        return None
