"""Authenticated private MCP boundary for schema-pinned Atlassian tools."""

import asyncio
import hashlib
import json
import re
import secrets
import time

import httpx
from adp_tools.adapter_contracts import Admission, AuthorizeRequest, OperationBinding, WireModel
from adp_tools.jira_context import CONTEXT_OPERATION, context_tool
from fastapi import HTTPException, Request
from pydantic import Field
from starlette.responses import JSONResponse, Response

from tool_adapters.atlassian import READ_TOOLS, WRITE_TOOLS, read_tool, write_tool
from tool_adapters.atlassian_observe import ProviderResponseError, canonical
from tool_adapters.caller_identity import matches_role
from tool_adapters.dispatch import _sign_dispatch, consume_dispatch
from tool_adapters.identity_access import retrieve_access_token
from tool_adapters.legacy_identity import legacy_identity
from tool_adapters.observation import decode


def require_gateway(request, configuration, *, require_reads=True):
    settings = configuration.observation
    try:
        proof = request.headers.getlist("x-adp-edge-provenance")
        caller = request.headers.getlist("x-caller-identity")
        if (
            settings is None
            or require_reads
            and (settings.read_schemas is None or settings.read_schemas_sha256 is None)
            or settings.gateway_role_arn is None
            or len(proof) != 1
            or not 0 < len(proof[0]) <= 256
            or not secrets.compare_digest(
                hashlib.sha256(proof[0].encode()).hexdigest(), settings.edge_provenance_sha256
            )
            or len(caller) != 1
            or not matches_role(caller[0], settings.gateway_role_arn)
        ):
            raise ValueError("untrusted gateway")
    except Exception:
        raise HTTPException(403, detail={"error": "adapter_authority_required"}) from None


async def authorize_read(
    assertion,
    binding,
    operation,
    deadline_at_ms,
    configuration,
    *,
    connector="atlassian",
    purpose="tool_call",
    read_arguments=None,
):
    settings = configuration.observation
    body = AuthorizeRequest(
        schema_version="1.0",
        binding=binding,
        purpose=purpose,
        connector=connector,
        operation=operation,
        signed_assertion=assertion,
        deadline_at_ms=deadline_at_ms,
    )
    try:
        payload = canonical(
            {**body.model_dump(mode="json"), **({"read_arguments": read_arguments} if purpose == "reconcile" else {})}
        )
        headers = await asyncio.to_thread(_sign_dispatch, settings.core_authorize_url, payload)
        async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
            response = await client.post(settings.core_authorize_url, content=payload, headers=headers)
        if response.status_code != 200 or len(response.content) > 16384:
            raise ValueError("core denied")
        admission = Admission.model_validate(response.json())
        if (
            admission.purpose != purpose
            or admission.connector != connector
            or admission.operation != operation
            or admission.binding != binding
            or admission.binding.release != configuration.release
            or admission.deadline_at_ms != deadline_at_ms
            or connector == "atlassian"
            and (
                admission.identity.workload != settings.identity_workload
                or admission.identity.provider != settings.identity_provider
            )
            or connector != "atlassian"
            and (
                settings.legacy_identity_workload is None
                or (admission.identity.workload, admission.identity.provider) != legacy_identity(connector, settings)
            )
            or admission.expires_at_ms <= int(time.time() * 1000)
        ):
            raise ValueError("core admission changed")
        return admission
    except Exception:
        raise HTTPException(503, detail={"error": "authority_unavailable"}) from None


class ReadbackRequest(WireModel):
    schema_version: str
    purpose: str
    binding: OperationBinding
    connector: str
    operation: str
    read_arguments: dict
    signed_assertion: str = Field(min_length=1, max_length=16384, repr=False)
    deadline_at_ms: int


async def reconcile_readback(request: Request, body: ReadbackRequest, configuration):
    settings = configuration.observation
    try:
        proof = request.headers.getlist("x-adp-edge-provenance")
        caller = request.headers.getlist("x-caller-identity")
        if (
            settings is None
            or not settings.write_enabled
            or settings.read_schemas is None
            or settings.reconciler_role_arn is None
            or len(caller) != 1
            or not matches_role(caller[0], settings.reconciler_role_arn)
            or len(proof) != 1
            or not secrets.compare_digest(
                hashlib.sha256(proof[0].encode()).hexdigest(),
                settings.edge_provenance_sha256,
            )
            or body.purpose != "reconcile"
            or body.connector != "atlassian"
            or body.operation != "get_jira_issue"
            or body.binding.release != configuration.release
            or not int(time.time() * 1000) < body.deadline_at_ms <= int(time.time() * 1000) + 20_000
            or not isinstance(body.read_arguments.get("cloudId"), str)
        ):
            raise ValueError("reconciliation refused")
        claims = json.loads(decode(body.signed_assertion))["claims"]
        if (
            claims.get("read_arguments_sha256") != hashlib.sha256(canonical(body.read_arguments)).hexdigest()
            or claims.get("atlassian_operation", {}).get("operation_id") != body.binding.operation_id
            or claims.get("atlassian_operation", {}).get("receipt_id") != body.binding.receipt_id
            or claims.get("atlassian_operation", {}).get("payload_sha256") != body.binding.payload_sha256
            or claims.get("connection", {}).get("site_id") != body.read_arguments["cloudId"]
        ):
            raise ValueError("unbound readback")
    except Exception:
        raise HTTPException(403, detail={"error": "adapter_authority_required"}) from None
    admission = await authorize_read(
        body.signed_assertion,
        body.binding,
        body.operation,
        body.deadline_at_ms,
        configuration,
        purpose="reconcile",
        read_arguments=body.read_arguments,
    )
    if (
        admission.identity.canonical_user_id != claims["principal"]["user_id"]
        or admission.context.site_id != body.read_arguments["cloudId"]
        or "read:jira:agent-interface" not in admission.context.scopes
    ):
        raise HTTPException(403, detail={"error": "site_denied"})
    token = await retrieve_access_token(admission, configuration)
    remaining = (min(admission.expires_at_ms, admission.deadline_at_ms) - int(time.time() * 1000)) / 1000
    if remaining <= 0:
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    try:
        await asyncio.wait_for(
            asyncio.to_thread(
                read_tool,
                token,
                "get_jira_issue",
                body.read_arguments,
                settings.read_schemas_sha256,
                deadline=time.monotonic() + remaining,
                reviewed_schemas=settings.read_schemas,
            ),
            timeout=remaining,
        )
    except Exception:
        raise HTTPException(503, detail={"error": "provider_readback_unavailable"}) from None
    return {
        "status": "unknown",
        "operation_id": body.binding.operation_id,
        "backend_generation": body.binding.release.backend_generation,
        "readback_performed": True,
    }


async def publish_reads(configuration):
    settings = configuration.observation
    schemas = dict(settings.read_schemas)
    if settings.write_enabled:
        schemas.update(settings.write_schemas)
    tools = [{"name": alias, "description": alias, "inputSchema": schemas[alias]} for alias in sorted(schemas)]
    if settings.system_context_enabled:
        tools = sorted([*tools, context_tool()], key=lambda item: item["name"])
    url = settings.core_authorize_url.removesuffix("/authorize") + "/catalogue"
    payload = canonical({"schema_version": "1.0", "release": configuration.release.model_dump(), "tools": tools})
    try:
        headers = await asyncio.to_thread(_sign_dispatch, url, payload)
        async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
            response = await client.post(url, content=payload, headers=headers)
        if response.status_code != 200 or len(response.content) > 4096:
            raise ValueError("catalogue rejected")
    except Exception:
        raise HTTPException(503, detail={"error": "catalogue_incomplete"}) from None
    return tools


async def project_context(assertion, binding, arguments, deadline, configuration):
    """The core verifies the signed call and resolves Product scope before reading."""
    url = configuration.observation.core_authorize_url
    payload = canonical(
        {
            "schema_version": "1.0",
            "purpose": "tool_call",
            "connector": "atlassian",
            "operation": CONTEXT_OPERATION,
            "binding": binding.model_dump(mode="json"),
            "signed_assertion": assertion,
            "deadline_at_ms": deadline,
            "context_arguments": arguments,
        }
    )
    headers = await asyncio.to_thread(_sign_dispatch, url, payload)
    remaining = (deadline - int(time.time() * 1000)) / 1000
    if remaining <= 0:
        raise HTTPException(504, detail={"error": "jira_context_deadline_exceeded"})
    async with httpx.AsyncClient(timeout=remaining, follow_redirects=False, trust_env=False) as client:
        response = await client.post(url, content=payload, headers=headers)
    if response.status_code != 200:
        raise HTTPException(
            response.status_code if response.status_code in {403, 404, 409, 422, 429} else 503,
            detail={"error": "jira_context_unavailable"},
        )
    if len(response.content) > 4 * 1024 * 1024:
        raise ValueError("oversized context")
    result = response.json().get("jira_context_result")
    if not isinstance(result, dict):
        raise ValueError("invalid context result")
    return {"structuredContent": result, "content": [{"type": "text", "text": canonical(result).decode()}]}


async def atlassian_mcp(request: Request, configuration):
    require_gateway(request, configuration)
    try:
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 65536:
                raise ValueError("oversized packet")
            raw.extend(chunk)
        packet = json.loads(raw)
        if type(packet) is not dict or packet.get("jsonrpc") != "2.0":
            raise ValueError("invalid packet")
        if (
            packet.get("method") == "notifications/initialized"
            and "id" not in packet
            and packet.get("params", {}) == {}
        ):
            return Response(status_code=202)
        if type(packet.get("id")) not in (str, int) or not 0 < len(str(packet["id"])) <= 256:
            raise ValueError("invalid packet")
        method = packet.get("method")
        params = packet.get("params", {})
        if (
            method == "initialize"
            and type(params) is dict
            and type(params.get("protocolVersion")) is str
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", params["protocolVersion"])
        ):
            # MCP clients propose a version; servers select a supported version.
            # AgentCore's target handshake can differ from the user CLI's version.
            proposed = params["protocolVersion"]
            result = {
                "protocolVersion": proposed if proposed in {"2024-11-05", "2025-03-26", "2025-06-18"} else "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "adp-private-atlassian", "version": "1.0"},
            }
        elif method == "ping" and params == {}:
            result = {}
        elif method == "tools/list" and params == {}:
            await publish_reads(configuration)
            schemas = dict(configuration.observation.read_schemas)
            if configuration.observation.write_enabled:
                schemas.update(configuration.observation.write_schemas)
            if configuration.observation.system_context_enabled:
                schemas[CONTEXT_OPERATION] = context_tool()["inputSchema"]
            result = {
                "tools": [
                    {
                        "name": alias,
                        "description": context_tool()["description"] if alias == CONTEXT_OPERATION else alias,
                        "inputSchema": {
                            **schema,
                            "properties": {**schema["properties"], "adp_assertion": {"type": "string"}},
                        },
                    }
                    for alias, schema in schemas.items()
                ]
            }
        elif method == "tools/call" and type(params) is dict:
            alias, supplied = params.get("name"), params.get("arguments")
            context = alias == CONTEXT_OPERATION and configuration.observation.system_context_enabled
            write = alias in WRITE_TOOLS and configuration.observation.write_enabled
            if (
                alias not in READ_TOOLS
                and not write
                and not context
                or type(supplied) is not dict
                or type(supplied.get("adp_assertion")) is not str
            ):
                raise ValueError("unsupported operation")
            assertion = supplied["adp_assertion"]
            arguments = {name: value for name, value in supplied.items() if name != "adp_assertion"}
            claims = json.loads(decode(assertion))["claims"]
            binding = OperationBinding.model_validate(claims["binding"])
            # The interceptor verifies the signed ingress ID before AgentCore
            # creates a separate target request. Keep authority and dispatch
            # bound to that signed ID; the target's transport ID is only echoed.
            if (
                type(claims.get("rpc_id")) not in (str, int)
                or not 0 < len(str(claims["rpc_id"])) <= 256
                or binding.operation_id != str(claims["rpc_id"])
                or claims.get("tool") != "atlassian___" + alias
                or claims.get("arguments_sha256") != hashlib.sha256(canonical(arguments)).hexdigest()
                or binding.payload_sha256 != claims["arguments_sha256"]
            ):
                raise ValueError("unbound arguments")
            if write:
                recorded = claims.get("atlassian_operation")
                principal = claims.get("principal")
                if (
                    type(recorded) is not dict
                    or type(principal) is not dict
                    or recorded.get("tool") != claims["tool"]
                    or recorded.get("operation_id")
                    != (
                        f"OP#{principal.get('tenant_id')}#{principal.get('user_id')}#"
                        f"{claims['tool']}#{recorded.get('request_id')}"
                    )
                    or recorded.get("receipt_id") != binding.receipt_id
                    or recorded.get("payload_sha256") != binding.payload_sha256
                    or recorded.get("release") != binding.release.model_dump()
                    or recorded.get("connection") != claims.get("connection")
                    or recorded.get("approval") != claims.get("atlassian_approval")
                    or recorded.get("dispatch_intent") != "dispatched"
                ):
                    raise ValueError("unbound write dispatch")
            deadline = min(int(time.time() * 1000) + 20_000, claims["expires_at"] * 1000)
            if context:
                result = await project_context(assertion, binding, arguments, deadline, configuration)
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": packet["id"], "result": result}, headers={"Cache-Control": "no-store"}
                )
            admission = await authorize_read(assertion, binding, alias, deadline, configuration)
            principal = claims.get("principal")
            connection = claims.get("connection")
            if (
                not isinstance(principal, dict)
                or not isinstance(connection, dict)
                or admission.operation != alias
                or admission.context.tenant_id != principal.get("tenant_id")
                or admission.context.canonical_user_id != principal.get("user_id")
                or admission.identity.canonical_user_id != principal.get("user_id")
                or admission.context.connection_id != connection.get("id")
                or admission.context.binding_revision != connection.get("revision")
                or admission.context.site_id != connection.get("site_id")
            ):
                raise HTTPException(403, detail={"error": "connection_changed"})
            if (
                admission.context.site_id is None
                or arguments.get("cloudId") != admission.context.site_id
                or (WRITE_TOOLS if write else READ_TOOLS)[alias][1] not in admission.context.scopes
            ):
                raise HTTPException(403, detail={"error": "site_denied"})
            if write:
                await publish_reads(configuration)
            await consume_dispatch(admission, configuration)
            token = await retrieve_access_token(admission, configuration)
            remaining = (min(admission.deadline_at_ms, admission.expires_at_ms) - int(time.time() * 1000)) / 1000
            if remaining <= 0:
                raise ValueError("deadline exceeded")
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    write_tool if write else read_tool,
                    token,
                    alias,
                    arguments,
                    (
                        configuration.observation.write_schemas_sha256
                        if write
                        else configuration.observation.read_schemas_sha256
                    ),
                    deadline=time.monotonic() + remaining,
                    **({"reviewed_schemas": configuration.observation.read_schemas} if not write else {}),
                ),
                timeout=remaining,
            )
        else:
            raise ValueError("unsupported method")
        return JSONResponse(
            {"jsonrpc": "2.0", "id": packet["id"], "result": result}, headers={"Cache-Control": "no-store"}
        )
    except HTTPException:
        raise
    except TimeoutError:
        raise HTTPException(503, detail={"error": "provider_unavailable"}) from None
    except ProviderResponseError as error:
        status = {
            "provider_unauthorized": 401,
            "provider_forbidden": 403,
            "not_found": 404,
            "rate_limited": 429,
        }.get(error.code, 503)
        raise HTTPException(status, detail={"error": error.code}) from None
    except ValueError as error:
        if str(error) in {"provider deadline exceeded", "catalogue_incomplete"}:
            raise HTTPException(503, detail={"error": str(error)}) from None
        if str(error) == "unverified provider schema":
            raise HTTPException(503, detail={"error": "provider_contract_unverified"}) from None
        raise HTTPException(400, detail={"error": "adapter_request_refused"}) from None
    except Exception:
        raise HTTPException(503, detail={"error": "adapter_unavailable"}) from None
