"""Private legacy MCP calls using only current core admission and one-use dispatch."""

import asyncio
import hashlib
import json
import time

import httpx
from adp_tools.adapter_contracts import OperationBinding
from fastapi import HTTPException, Request
from starlette.responses import JSONResponse, Response

from tool_adapters.atlassian_observe import canonical
from tool_adapters.atlassian_route import _sign_dispatch, authorize_read, require_gateway
from tool_adapters.dispatch import consume_dispatch
from tool_adapters.identity_access import retrieve_access_token
from tool_adapters.legacy_catalogue import LEGACY_SCOPES, legacy_tools
from tool_adapters.legacy_execution import execute_legacy
from tool_adapters.legacy_jira import JiraSiteObservation
from tool_adapters.legacy_slack_post import configured_post_fixture
from tool_adapters.observation import decode


def _deny():
    raise HTTPException(403, detail={"error": "connection_changed"})


async def publish_legacy_catalogue(configuration, connector, tools):
    url = configuration.observation.core_authorize_url.removesuffix("/authorize") + "/catalogue/legacy"
    payload = canonical(
        {
            "schema_version": "1.0",
            "release": configuration.release.model_dump(),
            "connector": connector,
            "tools": tools,
        }
    )
    try:
        headers = await asyncio.to_thread(_sign_dispatch, url, payload)
        async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
            response = await client.post(url, content=payload, headers=headers)
        if response.status_code != 200 or len(response.content) > 4096:
            raise ValueError("catalogue rejected")
    except Exception:
        raise HTTPException(503, detail={"error": "catalogue_incomplete"}) from None


async def legacy_mcp(request: Request, configuration, connector: str):
    require_gateway(request, configuration, require_reads=False)
    if connector not in LEGACY_SCOPES:
        _deny()
    try:
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 65536:
                raise ValueError("oversized packet")
            raw.extend(chunk)
        packet = json.loads(raw)
        if type(packet) is not dict or packet.get("jsonrpc") != "2.0":
            raise ValueError("invalid packet")
        method = packet.get("method")
        params = packet.get("params", {})
        if method == "notifications/initialized" and "id" not in packet and params == {}:
            return Response(status_code=202)
        if type(packet.get("id")) not in (str, int) or not 0 < len(str(packet["id"])) <= 256:
            raise ValueError("invalid packet")
        if (
            method == "initialize"
            and type(params) is dict
            and params.get("protocolVersion") in {"2024-11-05", "2025-03-26", "2025-06-18"}
        ):
            result = {
                "protocolVersion": params["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "adp-private-" + connector, "version": "1.0"},
            }
        elif method == "ping" and params == {}:
            result = {}
        elif method == "tools/list" and params == {}:
            published = legacy_tools(connector)
            if connector == "slack":
                try:
                    configured_post_fixture()
                except HTTPException as error:
                    if error.status_code != 403:
                        raise
                    published = published[:1]
            await publish_legacy_catalogue(configuration, connector, published)
            result = {
                "tools": [
                    {
                        **tool,
                        "inputSchema": {
                            **tool["inputSchema"],
                            "properties": {**tool["inputSchema"]["properties"], "adp_assertion": {"type": "string"}},
                        },
                    }
                    for tool in published
                ]
            }
        elif method == "tools/call" and type(params) is dict and set(params) == {"name", "arguments"}:
            operation, supplied = params["name"], params["arguments"]
            if (
                type(operation) is not str
                or operation not in LEGACY_SCOPES[connector]
                or type(supplied) is not dict
                or type(supplied.get("adp_assertion")) is not str
            ):
                raise ValueError("unsupported operation")
            arguments = {key: value for key, value in supplied.items() if key != "adp_assertion"}
            tools = {tool["name"]: tool for tool in legacy_tools(connector)}
            schema = tools[operation]["inputSchema"]
            if set(arguments) != set(schema.get("required", ())):
                raise ValueError("unsupported arguments")
            assertion = supplied["adp_assertion"]
            claims = json.loads(decode(assertion))["claims"]
            binding = OperationBinding.model_validate(claims["binding"])
            connection = claims.get("connection")
            # AgentCore can replace the transport ID after the interceptor
            # verifies ingress. Preserve the signed operation for admission
            # and one-use dispatch, independently of the response transport ID.
            if (
                type(claims.get("rpc_id")) not in (str, int)
                or not 0 < len(str(claims["rpc_id"])) <= 256
                or binding.operation_id != str(claims["rpc_id"])
                or claims.get("tool") != connector + "___" + operation
                or claims.get("arguments_sha256") != hashlib.sha256(canonical(arguments)).hexdigest()
                or binding.payload_sha256 != claims["arguments_sha256"]
                or type(connection) is not dict
                or connection != claims.get(connector + "_connection")
                or connection.get("connection_id") != claims.get("connection_id")
            ):
                _deny()
            deadline = min(int(time.time() * 1000) + 20_000, claims["expires_at"] * 1000)
            admission = await authorize_read(
                assertion, binding, operation, deadline, configuration, connector=connector
            )
            legacy = admission.context.legacy
            principal = claims.get("principal")
            if (
                type(principal) is not dict
                or legacy is None
                or admission.context.tenant_id != principal.get("tenant_id")
                or admission.context.canonical_user_id != principal.get("user_id")
                or admission.identity.canonical_user_id != principal.get("user_id")
                or admission.context.connection_id != connection["connection_id"]
                or admission.context.binding_revision != connection.get("revision")
                or LEGACY_SCOPES[connector][operation] not in claims.get("connection_scopes", ())
                or (
                    connector == "jira"
                    and (
                        admission.context.site_id != claims.get("site_id")
                        or arguments.get("cloudId") != admission.context.site_id
                        or "read:jira-work" not in legacy.site_scopes
                    )
                )
                or (
                    connector == "slack"
                    and operation == "post_message"
                    and arguments.get("run_marker") != claims.get("run_marker")
                )
            ):
                _deny()
            if connector == "github" and legacy.app_id is not None:
                raise HTTPException(403, detail={"error": "github_app_grant_unverified"})
            protected = {
                "account_id": legacy.provider_account_id,
                "scopes": list(admission.context.scopes),
                "app_id": legacy.app_id,
                "resources_sha256": legacy.resources_sha256,
            }
            if connector == "jira":
                protected["sites"] = (JiraSiteObservation(admission.context.site_id, legacy.site_scopes),)
            if connector == "slack":
                protected.update(
                    {
                        "owner_id": principal["user_id"],
                        "team_id": legacy.provider_account_id.split(":", 1)[0],
                        "user_id": legacy.provider_account_id.split(":", 1)[-1],
                    }
                )
            await consume_dispatch(admission, configuration)
            token = await retrieve_access_token(admission, configuration)
            remaining = (min(admission.deadline_at_ms, admission.expires_at_ms) - int(time.time() * 1000)) / 1000
            if remaining <= 0:
                raise TimeoutError
            try:
                value = await asyncio.wait_for(
                    execute_legacy(token, connector, operation, arguments, protected), timeout=remaining
                )
                result = {"content": [{"type": "text", "text": json.dumps(value)}]}
            except ValueError:
                raise HTTPException(502, detail={"error": "provider_unavailable"}) from None
        else:
            raise ValueError("unsupported method")
        return JSONResponse(
            {"jsonrpc": "2.0", "id": packet["id"], "result": result}, headers={"Cache-Control": "no-store"}
        )
    except HTTPException:
        raise
    except TimeoutError:
        raise HTTPException(503, detail={"error": "provider_unavailable"}) from None
    except (ValueError, TypeError, KeyError, OverflowError):
        raise HTTPException(400, detail={"error": "unsupported_operation"}) from None
