"""Service-account Jira reads through Atlassian MCP, on the existing adapter."""

import asyncio
import hashlib
import json
import secrets
import time

import boto3
from adp_tools.jira_system_contract import JiraSystemRequest
from botocore.config import Config
from fastapi import HTTPException

from tool_adapters.atlassian import ReadMCPTransport
from tool_adapters.atlassian_observe import (
    MAX_CATALOGUE_BYTES,
    MAX_CATALOGUE_PAGES,
    MAX_CATALOGUE_TOOLS,
    MAX_DEFINITION_BYTES,
    PROTOCOL_VERSION,
    ProviderResponseError,
    canonical,
)
from tool_adapters.caller_identity import matches_role
from tool_adapters.observation import decode
from tool_adapters.schema_contract import same_input_contract

TOOLS = {
    "verify": "getJiraCurrentUser",
    "projects": "listJiraProjects",
    "boards": "listJiraBoards",
    "issues": "searchJiraIssuesUsingJql",
    "backlog": "getJiraBoardIssueData",
    "board": "getJiraBoardConfig",
    "sprints": "listJiraBoardSprints",
}
REQUIRED_TOOLS = {TOOLS[name] for name in ("verify", "projects", "issues")}


def request_digest(body):
    return hashlib.sha256(canonical(body.model_dump(mode="json", exclude={"signed_assertion"}))).hexdigest()


async def verify_request(body, request, configuration):
    settings = configuration.observation
    now = int(time.time() * 1000)
    try:
        if settings is None or body.release != configuration.release or not now < body.deadline_at_ms <= now + 20_000:
            raise ValueError("unavailable")
        proof = request.headers.getlist("x-adp-edge-provenance")
        caller = request.headers.getlist("x-caller-identity")
        if (
            len(proof) != 1
            or not 0 < len(proof[0]) <= 256
            or len(caller) != 1
            or not secrets.compare_digest(
                hashlib.sha256(proof[0].encode()).hexdigest(), settings.edge_provenance_sha256
            )
            or not matches_role(caller[0], settings.core_role_arn)
            or body.workload != settings.identity_workload.removesuffix("-oauth-user") + "-system"
            or not body.provider.startswith(
                body.workload + "-" + hashlib.sha256(body.tenant_id.encode()).hexdigest()[:12] + "-"
            )
        ):
            raise ValueError("unavailable")
        envelope = json.loads(decode(body.signed_assertion))
        expected = {
            "audience": "tool-adapters:jira-system",
            "caller_role_arn": settings.core_role_arn,
            "request_sha256": request_digest(body),
        }
        if set(envelope) != {"claims", "signature"} or envelope["claims"] != expected:
            raise ValueError("unavailable")
        verified = await asyncio.to_thread(
            boto3.client("kms").verify,
            KeyId=settings.assertion_key_arn,
            Message=canonical(expected),
            MessageType="RAW",
            Signature=decode(envelope["signature"]),
            SigningAlgorithm="RSASSA_PSS_SHA_256",
        )
        if verified.get("SignatureValid") is not True:
            raise ValueError("unavailable")
    except Exception:
        raise HTTPException(403, detail={"error": "adapter_authority_required"}) from None


def resource_key(body):
    client = boto3.client(
        "bedrock-agentcore", config=Config(connect_timeout=2, read_timeout=5, retries={"total_max_attempts": 1})
    )
    workload = client.get_workload_access_token(workloadName=body.workload)
    result = client.get_resource_api_key(
        workloadIdentityToken=workload["workloadAccessToken"],
        resourceCredentialProviderName=body.provider,
    )
    key = result.get("apiKey")
    if not isinstance(key, str) or not key or len(key) > 65536 or any(c.isspace() for c in key):
        raise ValueError("identity_unavailable")
    return key


def discover(transport, key):
    result = transport.send(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "adp-jira-context", "version": "1"},
        },
        key,
    )
    if result.get("result", {}).get("protocolVersion") != PROTOCOL_VERSION:
        raise ValueError("provider protocol mismatch")
    transport.send("notifications/initialized", {}, key)
    schemas, seen, cursors = {}, set(), set()
    cursor, size = None, 0
    for _ in range(MAX_CATALOGUE_PAGES):
        reply = transport.send("tools/list", {"cursor": cursor} if cursor else {}, key)
        page = reply.get("result")
        if "error" in reply or not isinstance(page, dict) or not isinstance(page.get("tools"), list):
            raise ValueError("catalogue_incomplete")
        for tool in page["tools"]:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or tool["name"] in seen:
                raise ValueError("catalogue_incomplete")
            length = len(canonical(tool))
            size += length
            seen.add(tool["name"])
            if size > MAX_CATALOGUE_BYTES or length > MAX_DEFINITION_BYTES or len(seen) > MAX_CATALOGUE_TOOLS:
                raise ValueError("catalogue_incomplete")
            if tool["name"] in set(TOOLS.values()):
                schema = tool.get("inputSchema")
                if (
                    not isinstance(schema, dict)
                    or schema.get("type") != "object"
                    or not isinstance(schema.get("properties"), dict)
                ):
                    raise ValueError("catalogue_incomplete")
                schemas[tool["name"]] = schema
        cursor = page.get("nextCursor")
        if cursor is None:
            return schemas
        if not isinstance(cursor, str) or not cursor or len(cursor) > 512 or cursor in cursors:
            raise ValueError("catalogue_incomplete")
        cursors.add(cursor)
    raise ValueError("catalogue_incomplete")


def unwrap(value):
    return value["data"] if isinstance(value, dict) and set(value) == {"data"} else value


def value_from_result(result):
    if not isinstance(result, dict) or result.get("isError"):
        raise ProviderResponseError("provider_unavailable")
    texts = []
    for content in result.get("content", []):
        if not isinstance(content, dict) or content.get("type") != "text":
            raise ValueError("unsupported provider content")
        texts.append(unwrap(json.loads(content["text"])))
    if texts and any(canonical(item) != canonical(texts[0]) for item in texts[1:]):
        raise ValueError("ambiguous provider content")
    structured = unwrap(result.get("structuredContent"))
    # Atlassian's MCP app supplies a second UI representation: issues.nodes,
    # issues.pageInfo and runtime context. The text is the documented tool data.
    if isinstance(structured, dict) and isinstance(structured.get("issues"), dict):
        issues = structured["issues"]
        page = issues.get("pageInfo", {})
        normalized = {"issues": issues.get("nodes"), "isLast": not page.get("hasNextPage", False)}
        if page.get("hasNextPage"):
            normalized["nextPageToken"] = page.get("endCursor")
        if texts:
            text = texts[0]
            if (
                not isinstance(text, dict)
                or not isinstance(text.get("issues"), list)
                or not isinstance(normalized["issues"], list)
            ):
                raise ValueError("ambiguous provider content")

            def identities(rows):
                return [(row.get("id"), row.get("key")) for row in rows]

            if identities(text["issues"]) != identities(normalized["issues"]) or text.get(
                "nextPageToken"
            ) != normalized.get("nextPageToken"):
                raise ValueError("ambiguous provider content")
        else:
            return normalized
    elif structured is not None and texts and canonical(structured) != canonical(texts[0]):
        raise ValueError("ambiguous provider content")
    value = texts[0] if texts else structured
    if value is None or isinstance(value, dict) and "error" in value:
        raise ValueError("provider content unavailable")
    return value


def execute(body, *, key_loader=resource_key, transport_factory=ReadMCPTransport):
    if getattr(body, "connection_method", "service_account") == "forge_app":
        import jwt

        from tool_adapters.forge_jira import read, verified_delivery

        if body.forge_action is not None:
            if body.operation != "verify":
                raise ValueError("invalid_forge_delivery")
            try:
                delivery = verified_delivery(body)
            except (ValueError, KeyError, TypeError, jwt.InvalidTokenError):
                raise HTTPException(403, detail={"error": "forge_delivery_refused"}) from None
            return {**read(body, body.forge_token), **delivery}
        if body.forge_invocation is not None or body.forge_token is not None:
            raise ValueError("invalid_forge_delivery")
        return read(body, key_loader(body))
    if any(getattr(body, name, None) is not None for name in ("forge_action", "forge_invocation", "forge_token")):
        raise ValueError("invalid_forge_delivery")
    key = key_loader(body)
    remaining = (body.deadline_at_ms - int(time.time() * 1000)) / 1000
    if remaining <= 0:
        raise ValueError("provider deadline exceeded")
    transport = transport_factory(deadline=time.monotonic() + remaining)
    schemas = discover(transport, key)
    name = TOOLS[body.operation]
    if body.operation == "issues" and "boardId" in body.arguments:
        name = TOOLS["backlog"]
    schema = schemas.get(name)
    if schema is None or (body.operation != "verify" and not same_input_contract(schema, body.schemas.get(name))):
        raise ValueError("provider_schema_changed")
    if body.operation == "verify" and not REQUIRED_TOOLS <= schemas.keys():
        raise ValueError("required_tools_unavailable")
    args = {**body.arguments, "cloudId": body.site_id}
    if set(args) - schema["properties"].keys() or set(schema.get("required", [])) - args.keys():
        raise ValueError("provider_schema_changed")
    reply = transport.send("tools/call", {"name": name, "arguments": args}, key)
    if "error" in reply:
        raise ProviderResponseError("provider_unavailable")
    value = value_from_result(reply.get("result"))
    if body.operation == "verify":
        if not isinstance(value, dict) or not isinstance(value.get("accountId"), str) or not value["accountId"]:
            raise ValueError("provider_identity_unavailable")
        # Prove both read and search permissions before offering this key to
        # Products; catalogue visibility alone does not establish access.
        for tool, args in (
            (TOOLS["projects"], {"cloudId": body.site_id, "maxResults": 1}),
            (
                TOOLS["issues"],
                {
                    "cloudId": body.site_id,
                    "jql": "project IS NOT EMPTY",
                    "maxResults": 1,
                    "searchResultMode": "issues",
                    "view": "compact",
                },
            ),
        ):
            checked = transport.send("tools/call", {"name": tool, "arguments": args}, key)
            if "error" in checked:
                raise ProviderResponseError("provider_unavailable")
            value_from_result(checked.get("result"))
        return {"account_id": value["accountId"], "schemas": schemas}
    return {"data": value, "upstream_tool": name, "retrieved_at_ms": int(time.time() * 1000)}


async def system_read(body: JiraSystemRequest, request, configuration):
    await verify_request(body, request, configuration)
    remaining = (body.deadline_at_ms - int(time.time() * 1000)) / 1000
    try:
        if remaining <= 0:
            raise TimeoutError()
        return await asyncio.wait_for(asyncio.to_thread(execute, body), timeout=remaining)
    except ProviderResponseError as error:
        status = {
            "provider_unauthorized": 401,
            "provider_forbidden": 403,
            "not_found": 404,
            "rate_limited": 429,
            "unsupported_view": 422,
        }.get(error.code, 503)
        raise HTTPException(status, detail={"error": error.code}) from None
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, detail={"error": "jira_context_unavailable"}) from None
