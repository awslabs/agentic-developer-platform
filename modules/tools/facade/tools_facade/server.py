"""Forward only gateway-authorized calls using the façade's IAM identity."""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import boto3
import httpx
from adp_tools.search_input import SearchInput
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from tools_facade.credential_results import PRIVATE_DELIVERY_REQUIRED, PRIVATE_DELIVERY_TOOLS, require_generic_tool
from tools_facade.encoding import canonical
from tools_facade.knowledge_memory import MEMORY_TOOLS
from tools_facade.native_admission import (
    NativeAdmission,
    NativeCleanupAdmission,
    NativeCleanupHandoff,
    NativeFacadeHandoff,
    NativeInterpreterRecoveryHandoff,
)


def signed_headers(url, body, service, region, extra_headers=None):
    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise RuntimeError("Tools façade IAM credentials unavailable")
    request = AWSRequest(method="POST", url=url, data=body, headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream", **(extra_headers or {}),
    })
    SigV4Auth(credentials.get_frozen_credentials(), service, region).add_auth(request)
    return dict(request.headers)


TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}___[a-z][a-z0-9_]{0,63}$"

INTERPRETER_START = "code_interpreter___start"
INTERPRETER_CLOSE = "code_interpreter___close"
INTERPRETER_FIELDS = {
    "start": set(),
    "execute": {"session_id", "code", "language"},
    "result": {"session_id", "provider_id"},
    "file": {"session_id", "path"},
    "close": {"session_id"},
}


BROWSER_START = "browser___start"
BROWSER_FIELDS = {
    "start": {"url"},
    "step": {"session_id", "view_id", "action"},
    "inspect": {"session_id", "view_id"},
    "close": {"session_id"},
}

CLIENT_SCHEMA_VERSION = "1.0"
# Provider recovery is exposed through owner-bound lifecycle routes and adp___ helpers.
PROVIDER_RECOVERY_TOOLS = frozenset(
    f"{family}___{operation}"
    for family in ("websearch", "code_interpreter", "browser")
    for operation in ("operation_get", "artifact_read", "execution_close")
)
CLIENT_TOOL_FIELDS = {
    "websearch___search": ({"query": {"type": "string", "minLength": 1, "maxLength": 200}}, ["query"]),
    "code_interpreter___start": ({}, []),
    "code_interpreter___execute": ({"session_id": {"type": "string"}, "code": {"type": "string"},
                                    "language": {"type": "string"}}, ["session_id", "code", "language"]),
    "code_interpreter___result": ({"session_id": {"type": "string"}, "provider_id": {"type": "string"}},
                                  ["session_id", "provider_id"]),
    "code_interpreter___file": ({"session_id": {"type": "string"}, "path": {"type": "string"}},
                                ["session_id", "path"]),
    "code_interpreter___close": ({"session_id": {"type": "string"}}, ["session_id"]),
    "browser___start": ({"url": {"type": "string"}}, ["url"]),
    "browser___step": ({"session_id": {"type": "string"}, "view_id": {"type": "string"},
                        "action": {"type": "string"}, "candidate_id": {"type": "string"},
                        "url": {"type": "string"}, "seconds": {"type": "integer"}},
                       ["session_id", "view_id", "action"]),
    "browser___inspect": ({"session_id": {"type": "string"}, "view_id": {"type": "string"}},
                          ["session_id", "view_id"]),
    "browser___close": ({"session_id": {"type": "string"}}, ["session_id"]),
}


def client_tool_description(tool):
    description = tool.model_dump(by_alias=True, exclude_none=True)
    if tool.name in CLIENT_TOOL_FIELDS:
        properties, required = CLIENT_TOOL_FIELDS[tool.name]
        if tool.name.startswith(("code_interpreter___", "browser___")) and not tool.name.endswith("___start"):
            properties = {**properties, "execution_ref": {"type": "string", "format": "uuid"}}
            required = [*required, "execution_ref"]
        description["inputSchema"] = {"type": "object", "properties": properties, "required": required,
                                      "additionalProperties": False, "x-adp-schema-version": CLIENT_SCHEMA_VERSION}
    return description


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str = Field(pattern=TOOL_NAME_PATTERN)
    arguments: dict = Field(default_factory=dict)
    execution_id: UUID | None = None
    request_id: UUID
    ttl_seconds: int | None = Field(default=None, strict=True, ge=1, le=900)


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    team_id: str = Field(min_length=1)
    root_human_id: str = Field(min_length=1)


class NativeReceiptRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str = Field(pattern=r"^atlassian___[a-z_]+$")
    request_id: UUID
    execution_id: UUID
    original_operation_id: UUID


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = Field(pattern=r"^1\.0$")
    allowed: bool
    principal: Principal
    receipt_id: str = Field(min_length=1)
    connection_scopes: list[str] = Field(default_factory=list)
    connection_id: str | None = None
    github_connection: dict | None = None
    slack_connection: dict | None = None
    jira_connection: dict | None = None
    atlassian_connection: dict | None = None
    atlassian_approval: dict | None = None
    run_id: str | None = None
    workload_id: str | None = None
    run_expires_at: int | None = None
    host_generation: int | None = Field(default=None, strict=True, ge=1)
    execution_deadline_at: int | None = None
    native_admission: NativeAdmission | None = Field(default=None, repr=False, exclude=True)
    native_cleanup: NativeCleanupAdmission | None = Field(default=None, repr=False, exclude=True)
    native_receipt_read: NativeReceiptRead | None = Field(default=None, repr=False, exclude=True)
    _native_handoff: object = PrivateAttr(default=None)
    _knowledge_memory: dict | None = PrivateAttr(default=None)
    _atlassian_operation: dict | None = PrivateAttr(default=None)


class ToolDescription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=TOOL_NAME_PATTERN)
    description: str
    input_schema: dict = Field(alias="inputSchema")
    output_schema: dict | None = Field(default=None, alias="outputSchema")
    annotations: dict | None = None


class ToolListing(Decision):
    tools: list[ToolDescription]
    dynamic_catalogue_unavailable: bool = False


class GatewayAuthorizer:
    @staticmethod
    def supports_l5_transport(headers):
        return bool(headers.get("authorization")) and not any(headers.get(name) for name in (
            "x-adp-run-credential", "x-adp-workload-token", "x-adp-chat-capability",
            "x-adp-crew-host-assertion", "x-adp-native-test-capability", "x-adp-native-test-operation",
        ))

    async def list_tools(self, headers):
        return await self._request({"schema_version": "1.0", "action": "list"}, headers)

    async def list_l5(self, headers):
        return await self._request({}, headers, l5="list")

    async def call_l5(self, call, headers):
        return await self._request({"tool": call.tool, "arguments": call.arguments}, headers, l5="call")

    async def authorize(self, tool, arguments, headers):
        return await self._request({"schema_version": "1.0", "tool": tool, "arguments": arguments}, headers)

    async def authorize_call(self, call, headers, *, closing=False):
        # Preserve the original public identity and complete payload for the
        # gateway's quote boundary. An allow receipt is not dispatch authority.
        payload = {"schema_version": "1.0", **call.model_dump(mode="json")}
        if closing:
            payload.update(action="close", arguments={})
            if call.tool == INTERPRETER_CLOSE and headers.get("x-adp-native-test-capability"):
                payload["tool"] = INTERPRETER_START
        return await self._request(payload, headers)

    async def recover(self, tool, headers, *, call=None, execution_id=None, request_id=None):
        payload = {"schema_version": "1.0", "action": "recover", "tool": tool}
        if call is not None and (tool.startswith("atlassian___")
                                 or tool == "code_interpreter___execute" and call.execution_id is not None):
            payload.update(call.model_dump(mode="json"))
        else:
            if call is not None:
                execution_id, request_id = call.execution_id, call.request_id
            payload.update({key: str(value) for key, value in {
                "execution_id": execution_id, "request_id": request_id}.items() if value is not None})
        return await self._request(payload, headers)

    async def close(self, tool, headers, execution_id=None):
        payload = {"schema_version": "1.0", "action": "close", "tool": tool,
                   **({"execution_id": str(execution_id)} if execution_id is not None else {})}
        return await self._request(payload, headers)

    async def dispatch_native(self, payload, proof, headers):
        return await self._request({"call": payload, "proof": proof}, headers, dispatch=True)

    async def record_outcome(self, payload):
        return await self._request(payload, {}, outcome=True)

    async def _request(self, payload, headers, *, dispatch=False, outcome=False, l5=None):
        url = os.environ["ADP_TOOLS_INTERNAL_AUTH_URL"]
        if l5 is not None:
            if not self.supports_l5_transport(headers):
                raise HTTPException(403, "tool authority refused")
            if l5 not in {"list", "call"} or not url.endswith("/tools/authorize"):
                raise ValueError("Invalid L5 tool URL")
            url = url.removesuffix("/tools/authorize") + "/tools/l5-" + l5
        if dispatch:
            if not url.endswith("/tools/authorize"):
                raise ValueError("Invalid native dispatch URL")
            url = url.removesuffix("/tools/authorize") + "/tools/native-dispatch"
        if outcome:
            if not url.endswith("/tools/authorize"):
                raise ValueError("Invalid tool outcome URL")
            url = url.removesuffix("/tools/authorize") + "/tools/outcome"
        body = canonical(payload)
        proofs = {}
        for name in ("x-adp-run-credential", "x-adp-workload-token", "x-adp-chat-capability",
                     "x-adp-chat-run-id", "x-adp-chat-session-id", "x-agent-runid",
                     "x-adp-crew-host-assertion",  # #8380: Dev Box host, minted by the gateway relay
                     "x-adp-native-test-capability", "x-adp-native-test-operation"):
            if hasattr(headers, "getlist") and len(headers.getlist(name)) > 1:
                raise HTTPException(403, "ambiguous tool authority")
            if headers.get(name):
                proofs[name] = headers[name]
        if headers.get("authorization"):
            proofs["x-adp-user-authorization"] = headers["authorization"]
        signed = signed_headers(url, body, "execute-api", os.environ["AWS_REGION"], proofs)
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=10) as client:
            response = await client.post(url, content=body, headers=signed)
        if response.status_code in (401, 403, 404):
            if response.status_code == 403:
                try:
                    reason = response.json().get("detail")
                    if reason in ("scope_not_granted", "tool_undeployed"):
                        raise HTTPException(403, reason)
                except (ValueError, AttributeError):
                    pass
            raise HTTPException(403, "tool authority refused")
        if response.status_code == 429:
            try:
                reason = response.json().get("detail")
            except (ValueError, AttributeError):
                reason = None
            if reason not in ("tool_rate_limit_exceeded", "tool_budget_exceeded"):
                reason = "tool_limit_exceeded"
            retry_after = response.headers.get("retry-after", "")
            valid_retry = retry_after.isascii() and retry_after.isdecimal() and len(retry_after) <= 5
            if not valid_retry or not (0 < int(retry_after) <= 86400):
                retry_after = "1"
            raise HTTPException(429, reason, headers={"Retry-After": retry_after})
        response.raise_for_status()
        return response.json()


def gateway_tool_name(tool):
    if tool.startswith("code_interpreter___"):
        return "code-interpreter___" + tool.split("___", 1)[1]
    return tool


def normalize_lambda_result(response, tool):
    if "error" in response or not tool.startswith(("websearch___", "code_interpreter___", "browser___")):
        return response
    result = response.get("result")
    if not isinstance(result, dict):
        raise ValueError("Invalid AgentCore tool result")
    if "isError" in result and not isinstance(result["isError"], bool):
        raise ValueError("Invalid AgentCore error flag")
    if result.get("isError") is True:
        return response
    if "structuredContent" in result:
        payload = result["structuredContent"]
    else:
        content = result.get("content")
        if (not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict)
                or content[0].get("type") != "text" or not isinstance(content[0].get("text"), str)):
            raise ValueError("Invalid AgentCore Lambda content")
        payload = json.loads(content[0]["text"])
    if not isinstance(payload, dict):
        raise ValueError("Invalid AgentCore Lambda payload")
    return {**response, "result": {**payload,
        **({"_meta": result["_meta"]} if "_meta" in result else {}),
    }}


class AgentCoreClient:
    async def invoke(self, tool, arguments, decision):
        gateway_tool = gateway_tool_name(tool)
        result = await self._request("tools/call", gateway_tool, arguments, decision)
        return normalize_lambda_result(result, tool)

    async def discover(self, decision, *, require_search=True):
        tools, seen, names, cursor = [], set(), set(), None
        for _ in range(1 if decision.native_admission is not None else 5):
            response = await self._request("tools/list", "tools/list", {"cursor": cursor} if cursor else {}, decision)
            result = response.get("result")
            if ("error" in response or not isinstance(result, dict) or not isinstance(result.get("tools"), list)
                    or result.get("_meta", {}).get("adp_receipt_id") != decision.receipt_id):
                raise ValueError("Invalid AgentCore discovery")
            for tool in result["tools"]:
                if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"]
                        or tool["name"] in names or not isinstance(tool.get("inputSchema"), dict)
                        or tool["inputSchema"].get("type") != "object"):
                    raise ValueError("Invalid AgentCore tool schema")
                names.add(tool["name"])
                tools.append(tool)
            cursor = result.get("nextCursor")
            if not cursor:
                if require_search and "websearch___search" not in names:
                    raise ValueError("AgentCore search tool missing")
                return {
                    "gateway_url_sha256": hashlib.sha256(os.environ["ADP_TOOLS_AGENTCORE_URL"].encode()).hexdigest(),
                    "tools_list_sha256": hashlib.sha256(canonical(tools)).hexdigest(),
                }
            if not isinstance(cursor, str) or len(cursor) > 4096 or cursor in seen:
                raise ValueError("Invalid AgentCore discovery cursor")
            seen.add(cursor)
        raise ValueError("AgentCore discovery page limit exceeded")

    async def _request(self, method, gateway_tool, arguments, decision):
        endpoint = os.environ["ADP_TOOLS_AGENTCORE_URL"]
        region = os.environ["AWS_REGION"]
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "https" or not parsed.hostname or
                not parsed.hostname.endswith(f".gateway.bedrock-agentcore.{region}.amazonaws.com") or
                parsed.path != "/mcp" or parsed.username or parsed.password or parsed.query or parsed.fragment or
                parsed.port not in (None, 443)):
            raise RuntimeError("Invalid AgentCore gateway URL")
        packet = {"jsonrpc": "2.0", "id": str(uuid4()), "method": method,
                  "params": arguments if method == "tools/list" else {"name": gateway_tool, "arguments": arguments}}
        body = canonical(packet)
        now = int(time.time())
        claims = {
            "caller_role_arn": os.environ["ADP_TOOLS_FACADE_ROLE_ARN"],
            "principal": decision.principal.model_dump(exclude={"root_human_id"}),
            "root_human_id": decision.principal.root_human_id,
            "run_id": decision.run_id, "workload_id": decision.workload_id, "run_expires_at": decision.run_expires_at,
            "host_generation": decision.host_generation,
            "receipt_id": decision.receipt_id,
            "connection_scopes": decision.connection_scopes,
            "connection_id": decision.connection_id,
            "github_connection": decision.github_connection,
            "slack_connection": decision.slack_connection,
            "jira_connection": decision.jira_connection,
            "method": method, "tool": gateway_tool,
            "arguments_sha256": hashlib.sha256(canonical(arguments)).hexdigest(),
            "issued_at": now, "expires_at": now + 30,
        }
        if method == "tools/call" and gateway_tool.startswith(
            ("knowledge___", "github___", "jira___", "slack___", "atlassian___")
        ):
            claims["rpc_id"] = packet["id"]
        connector = gateway_tool.split("___", 1)[0] if method == "tools/call" else None
        legacy = (
            connector in {"github", "jira", "slack"}
            and os.environ.get("ADP_TOOL_ADAPTER_LEGACY_ENABLED") == "true"
        )
        if connector == "atlassian" or legacy:
            if connector == "atlassian":
                connection = decision.atlassian_connection
                if (
                    not isinstance(connection, dict)
                    or set(connection) != {"id", "revision", "site_id"}
                    or connection.get("id") != decision.connection_id
                    or any(not isinstance(value, str) or not value for value in connection.values())
                ):
                    raise ValueError("Atlassian connection required")
            else:
                connection = getattr(decision, connector + "_connection")
                if (
                    not isinstance(connection, dict)
                    or connection.get("connection_id") != decision.connection_id
                    or not isinstance(connection.get("revision"), str)
                    or not connection["revision"]
                    or not isinstance(connection.get("account_id"), str)
                    or not connection["account_id"]
                ):
                    raise ValueError("Legacy connection required")
                if connector == "jira":
                    claims["site_id"] = arguments.get("cloudId")
                if gateway_tool == "slack___post_message":
                    claims["run_marker"] = arguments.get("run_marker")
            release = json.loads(os.environ["ADP_TOOL_ADAPTER_RELEASE"])
            claims["audience"] = "tool-adapters:authorize"
            claims["purpose"] = "tool_call"
            claims["connector"] = connector
            claims["adapter_release"] = release
            claims["connection"] = connection
            claims["binding"] = {
                "operation_id": str(packet["id"]), "receipt_id": decision.receipt_id,
                "payload_sha256": claims["arguments_sha256"], "release": release,
            }
            if connector == "atlassian" and gateway_tool.split("___", 1)[1] in {
                "create_jira_issue", "update_jira_issue", "comment_jira_issue", "transition_jira_issue"
            }:
                operation = decision._atlassian_operation
                if (not isinstance(operation, dict)
                        or operation.get("tool") != gateway_tool
                        or operation.get("payload_sha256") != claims["arguments_sha256"]
                        or operation.get("connection") != connection
                        or operation.get("release") != release
                        or operation.get("receipt_id") != decision.receipt_id
                        or operation.get("approval") != decision.atlassian_approval
                        or operation.get("dispatch_intent") != "dispatched"):
                    raise ValueError("original Jira write dispatch required")
                claims["atlassian_operation"] = operation
                claims["atlassian_approval"] = decision.atlassian_approval
        if method == "tools/call" and gateway_tool in MEMORY_TOOLS:
            if (decision.run_id and decision.host_generation is None or decision.native_admission is not None
                    or decision._knowledge_memory is None):
                raise ValueError("Original human memory operation required")
            claims["knowledge_memory"] = decision._knowledge_memory
        if method == "tools/call" and gateway_tool in {"browser___start", "code-interpreter___start"}:
            claims["execution_deadline_at"] = getattr(decision, "execution_deadline_at", None)
        native = decision._native_handoff
        if decision.native_admission is not None and type(native) not in {
            NativeFacadeHandoff, NativeInterpreterRecoveryHandoff,
        }:
            raise ValueError("Native tool handoff required")
        if decision.native_cleanup is not None and type(native) is not NativeCleanupHandoff:
            raise ValueError("Native cleanup handoff required")
        if native is not None:
            if len(body) > 65536:
                raise ValueError("Oversized native AgentCore request")
            native.before_sign(method, gateway_tool, arguments, decision)
            if method == "tools/call" and gateway_tool.startswith("knowledge___"):
                claims["native_test"] = native.knowledge_binding()
            if method == "tools/call" and gateway_tool.startswith("atlassian___"):
                claims["native_atlassian"] = native.atlassian_binding()
            if method == "tools/call" and gateway_tool.startswith("code-interpreter___"):
                claims["native_interpreter"] = native.interpreter_binding()
        kms = boto3.client("kms", region_name=region, config=Config(
            connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1}))
        signature = await asyncio.to_thread(
            kms.sign,
            KeyId=os.environ["ADP_TOOLS_ASSERTION_KEY_ARN"], Message=canonical(claims),
            MessageType="RAW", SigningAlgorithm="RSASSA_PSS_SHA_256",
        )
        envelope = {
            "claims": claims,
            "signature": base64.urlsafe_b64encode(signature["Signature"]).decode().rstrip("="),
        }
        headers = signed_headers(endpoint, body, "bedrock-agentcore", region)
        headers["x-adp-assertion"] = base64.urlsafe_b64encode(canonical(envelope)).decode().rstrip("=")
        headers["x-adp-tenant"] = decision.principal.tenant_id
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=30) as client:
            if native is None:
                response = await client.post(endpoint, content=body, headers=headers)
            else:
                native.before_send()
                # Bound the streamed application body. This is not evidence of
                # a total-wire transfer bound; the protected tariff must supply it.
                try:
                    # HTTPX's read timeout resets between chunks. Keep one total
                    # local wait bound even when the provider keeps trickling.
                    async with asyncio.timeout(30):
                        async with client.stream("POST", endpoint, content=body, headers=headers) as stream:
                            stream.raise_for_status()
                            raw = bytearray()
                            async for chunk in stream.aiter_bytes():
                                if len(raw) + len(chunk) > 262144:
                                    raise ValueError("Oversized AgentCore response")
                                raw.extend(chunk)
                            response = httpx.Response(stream.status_code, content=bytes(raw),
                                                      headers={"Content-Type": stream.headers.get("content-type", "")},
                                                      request=stream.request)
                except TimeoutError:
                    # Preserve existing unknown/no-retry handling. Cancellation
                    # of our wait does not prove cancellation of provider work.
                    raise httpx.ReadTimeout("Native AgentCore response deadline exceeded") from None
        response.raise_for_status()
        if len(response.content) > 262144:
            raise ValueError("Oversized AgentCore response")
        if response.headers.get("content-type", "").split(";", 1)[0] == "text/event-stream":
            events = [
                json.loads("\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")))
                for event in response.text.replace("\r\n", "\n").split("\n\n")
                if any(line.startswith("data:") for line in event.splitlines())
            ]
            if len(events) != 1:
                raise ValueError("Unexpected AgentCore event stream")
            result = events[0]
        else:
            result = response.json()
        if not isinstance(result, dict) or result.get("jsonrpc") != "2.0" or result.get("id") != packet["id"]:
            raise ValueError("Invalid AgentCore response")
        return result


def create_app(*, gateway=None, agentcore=None):
    from tools_facade.executions import ATLASSIAN_WRITES, SYNCHRONOUS_TOOLS, ExecutionStore

    app = FastAPI()
    authorizer = gateway or GatewayAuthorizer()
    provider = agentcore or AgentCoreClient()
    executions = ExecutionStore()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    def require_identity(request):
        bearer = request.headers.getlist("authorization")
        crew = request.headers.getlist("x-adp-chat-capability")
        host = request.headers.getlist("x-adp-crew-host-assertion")
        if host:
            # #8380: a Dev Box host assertion is the whole identity; it never combines with a
            # bearer, crew or run proof, and names the host run it was minted for.
            if (bearer or crew or request.headers.get("x-adp-run-credential")
                    or request.headers.get("x-adp-workload-token") or len(host) != 1 or not host[0]
                    or len(request.headers.getlist("x-agent-runid")) != 1 or not request.headers["x-agent-runid"]):
                raise HTTPException(403, "ambiguous tool identity")
            return
        if crew:
            if bearer or request.headers.get("x-adp-run-credential") or any(
                len(request.headers.getlist(name)) != 1 or not request.headers[name]
                for name in ("x-adp-chat-capability", "x-adp-chat-run-id", "x-adp-chat-session-id",
                             "x-adp-workload-token", "x-agent-runid")
            ):
                raise HTTPException(403, "ambiguous tool identity")
            return
        bound = bool(request.headers.get("x-adp-run-credential") and request.headers.get("x-adp-workload-token"))
        if bearer:
            if (len(bearer) != 1 or request.headers.get("x-adp-run-credential")
                    or request.headers.get("x-adp-workload-token")):
                raise HTTPException(403, "ambiguous tool identity")
            scheme, separator, token = bearer[0].partition(" ")
            if scheme.lower() != "bearer" or not separator or not token or len(token) > 8192 or any(
                character.isspace() for character in token
            ):
                raise HTTPException(403, "bearer required")
        elif not bound:
            raise HTTPException(403, "verified run binding required")

    @app.get("/api/agent/tools")
    @app.get("/agent/tools")
    async def list_tools(request: Request, live: bool = False):
        require_identity(request)
        try:
            authorized = await authorizer.list_tools(request.headers)
            if isinstance(authorizer, GatewayAuthorizer) and "native_status" in authorized:
                return JSONResponse(status_code=202, content=authorized, headers={"Cache-Control": "no-store"})
            listing = ToolListing.model_validate(authorized)
            if listing.native_admission is not None:
                if not isinstance(authorizer, GatewayAuthorizer) or not isinstance(provider, AgentCoreClient):
                    raise ValueError("Native discovery runtime required")
                listing._native_handoff = NativeFacadeHandoff(gateway=authorizer, admission=listing.native_admission,
                    call=None, headers=request.headers, decision=listing)
                await listing._native_handoff.begin()
        except HTTPException:
            raise
        except (httpx.HTTPError, ValidationError, ValueError, KeyError) as exc:
            raise HTTPException(503, "tool authority unavailable") from exc
        if not listing.allowed or listing.principal.user_id != listing.principal.root_human_id:
            raise HTTPException(403, "tool authority refused")
        dynamic_status = {}
        if listing.dynamic_catalogue_unavailable:
            # A DYNAMIC target receives this authenticated metadata request and
            # publishes its actual listing. Retry authority once to consume that
            # fresh provenance; a failed connector must not remove static core.
            try:
                try:
                    await provider.discover(listing, require_search=False)
                except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, RuntimeError,
                        BotoCoreError, ClientError):
                    # One target may publish valid metadata before another target
                    # fails discovery. Only fresh core authority can accept it.
                    pass
                refreshed = ToolListing.model_validate(await authorizer.list_tools(request.headers))
                if not refreshed.allowed or refreshed.principal != listing.principal:
                    raise HTTPException(403, "tool authority refused")
                listing = refreshed
            except HTTPException:
                raise
            except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, RuntimeError,
                    BotoCoreError, ClientError):
                pass
            dynamic_status = {
                "dynamic_catalogue_status": "unavailable" if listing.dynamic_catalogue_unavailable else "ready"
            }
        discovery = {}
        if live:
            if not any(tool.name == "websearch___search" for tool in listing.tools):
                raise HTTPException(403, "search authority required for discovery smoke")
            try:
                discovery = {"discovery": await provider.discover(listing)}
            except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, RuntimeError,
                    BotoCoreError, ClientError) as exc:
                raise HTTPException(503, "live tool discovery unavailable") from exc
        unavailable = [{"name": tool.name, "reason": PRIVATE_DELIVERY_REQUIRED}
                       for tool in listing.tools if tool.name in PRIVATE_DELIVERY_TOOLS]
        l5_tools = []
        if isinstance(authorizer, GatewayAuthorizer) and authorizer.supports_l5_transport(request.headers):
            try:
                l5_listing = await authorizer.list_l5(request.headers)
                if l5_listing.get("root_human_id") != listing.principal.root_human_id:
                    raise HTTPException(403, "tool authority refused")
                l5_tools = [ToolDescription.model_validate(tool) for tool in l5_listing["tools"]]
            except HTTPException:
                raise
            except (httpx.HTTPError, ValidationError, ValueError, KeyError) as exc:
                raise HTTPException(503, "tool authority unavailable") from exc
        return {"tools": [client_tool_description(tool) for tool in listing.tools
                          if tool.name not in PROVIDER_RECOVERY_TOOLS | PRIVATE_DELIVERY_TOOLS] +
                         [client_tool_description(tool) for tool in l5_tools],
                "receipt_id": listing.receipt_id, "root_human_id": listing.principal.root_human_id,
                **({"unavailable_tools": unavailable} if unavailable else {}), **discovery, **dynamic_status}

    def interpreter_receipt_matches(execution_id, request_id, tool, decision, receipt, current):
        if tool in {"start", "execute", "file"}:
            return (request_id == current["request_id"]["S"] and
                    receipt == current["receipt_id"]["S"])
        try:
            original_tool = INTERPRETER_START if tool == "close" else "code_interpreter___execute"
            if tool == "result" and request_id != current["request_id"]["S"]:
                return False
            original = executions.lookup(UUID(execution_id), UUID(request_id), original_tool, decision)
            return original["receipt_id"]["S"] == receipt
        except (HTTPException, ValueError, KeyError):
            return False

    @app.post("/api/agent/tools/call")
    @app.post("/api/tools/{tool}")
    @app.post("/agent/tools/call")
    @app.post("/tools/{tool}")
    async def call(body: ToolCall, request: Request, tool: str | None = None):
        if tool is not None and tool != body.tool:
            raise HTTPException(400, "tool mismatch")
        require_identity(request)
        if body.tool.startswith("l5___"):
            if not isinstance(authorizer, GatewayAuthorizer) or not request.headers.get("authorization"):
                raise HTTPException(403, "tool authority refused")
            try:
                return await authorizer.call_l5(body, request.headers)
            except httpx.HTTPError as exc:
                raise HTTPException(503, "tool authority unavailable") from exc
        native_requested = bool(request.headers.get("x-adp-native-test-capability"))
        if (native_requested != bool(request.headers.get("x-adp-native-test-operation")) or
                native_requested and (not isinstance(authorizer, GatewayAuthorizer) or
                                      not isinstance(provider, AgentCoreClient))):
            raise HTTPException(403, "native tool authority refused")
        if body.tool.startswith("knowledge___") and not native_requested:
            # Native discovery is an explicit, separately admitted request. Its
            # call must not spend the same operation on an implicit tools/list.
            # The native authorizer still rechecks the exact fresh catalogue.
            await list_tools(request)
        expected_gateway = request.headers.get("x-adp-expected-gateway-sha256")
        if expected_gateway and expected_gateway != hashlib.sha256(
            os.environ.get("ADP_TOOLS_AGENTCORE_URL", "").encode()
        ).hexdigest():
            raise HTTPException(409, "deployed gateway mismatch")
        try:
            closing = (body.tool in {"code_interpreter___close", "browser___close"}
                       and isinstance(provider, AgentCoreClient))
            async def authorize_current_call():
                if isinstance(authorizer, GatewayAuthorizer):
                    return await authorizer.authorize_call(body, request.headers, closing=closing)
                return await (authorizer.close(body.tool, request.headers) if closing else
                              authorizer.authorize(body.tool, body.arguments, request.headers))

            try:
                authorized = await authorize_current_call()
            except HTTPException as error:
                if (native_requested or closing or error.status_code != 403 or error.detail != "tool_undeployed"
                        or body.tool.split("___", 1)[0] not in {"atlassian", "github", "jira", "slack"}):
                    raise
                # Expired schema metadata is refused before admission or provider
                # dispatch. Refresh once, then authorize the exact original call.
                # User grants, site scopes and cost limits are still checked by core.
                await list_tools(request)
                authorized = await authorize_current_call()
            if isinstance(authorizer, GatewayAuthorizer) and "native_status" in authorized:
                return JSONResponse(status_code=202, content=authorized,
                                    headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
            if isinstance(authorizer, GatewayAuthorizer) and "tool_status" in authorized:
                status = authorized["tool_status"]
                if (native_requested or authorized.get("schema_version") != "1.0"
                        or set(authorized) != {"schema_version", "tool_status"}
                        or not isinstance(status, dict)
                        or set(status) != {"status", "replay", "receipt_id", "reservation_status", "recovery_path"}
                        or status["status"] != "pending" or status["replay"] is not True
                        or status["reservation_status"] not in {"unknown", "estimated_hold"}
                        or status["recovery_path"] != "/agent/tools/operations/recover"
                        or type(status["receipt_id"]) is not str
                        or str(UUID(status["receipt_id"])) != status["receipt_id"]):
                    raise HTTPException(503, "tool authority unavailable")
                if status["reservation_status"] == "estimated_hold":
                    ownership = await recover(request, body.tool, body)
                    try:
                        original = await asyncio.to_thread(
                            executions.recover_request, body.request_id, body.tool, body.arguments,
                            ownership, body.ttl_seconds, body.execution_id,
                        )
                    except HTTPException as exc:
                        if exc.status_code != 404:
                            raise
                    else:
                        if original["receipt_id"]["S"] != status["receipt_id"]:
                            raise HTTPException(503, "tool admission receipt mismatch")
                        original_receipt = await operation_receipt(original, body.tool, ownership)
                        return JSONResponse(
                            status_code=200 if original_receipt["status"] == "confirmed" else 202,
                            content=original_receipt, headers={"Cache-Control": "no-store"},
                        )
                return JSONResponse(status_code=202, content=authorized,
                                    headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
            decision = Decision.model_validate(authorized)
            native_interpreter_close = (
                native_requested and closing and body.tool == INTERPRETER_CLOSE
                and decision.native_cleanup is not None and decision.native_admission is None
            )
            if native_requested and decision.native_admission is None and not native_interpreter_close:
                raise HTTPException(403, "native tool authority refused")
            if decision.native_cleanup is not None:
                if not native_interpreter_close or body.execution_id is None:
                    raise HTTPException(403, "native tool authority refused")
                decision._native_handoff = NativeCleanupHandoff(
                    admission=decision.native_cleanup, decision=decision, execution_id=body.execution_id,
                    tool=INTERPRETER_START, headers=request.headers)
            if decision.native_admission is not None:
                if not isinstance(authorizer, GatewayAuthorizer) or not isinstance(provider, AgentCoreClient):
                    raise ValueError("Native tool runtime required")
                decision._native_handoff = NativeFacadeHandoff(
                    gateway=authorizer, admission=decision.native_admission, call=body,
                    headers=request.headers, decision=decision)
                await decision._native_handoff.begin()
        except HTTPException:
            raise
        except (httpx.HTTPError, ValidationError, ValueError, KeyError) as exc:
            raise HTTPException(503, "tool authority unavailable") from exc
        if not decision.allowed or decision.principal.user_id != decision.principal.root_human_id:
            raise HTTPException(403, "tool authority refused")
        require_generic_tool(body.tool)
        if (request.headers.get("x-adp-run-credential") or request.headers.get("x-adp-chat-capability")
                or request.headers.get("x-adp-crew-host-assertion")) and not (
            decision.run_id and decision.workload_id and decision.run_expires_at
        ):
            raise HTTPException(503, "tool authority unavailable")
        if body.ttl_seconds is not None and body.tool not in {INTERPRETER_START, BROWSER_START}:
            raise HTTPException(422, "session lifetime is available only on initial start")
        if body.tool == "websearch___search" and isinstance(provider, AgentCoreClient):
            if set(body.arguments) != {"query"}:
                raise HTTPException(422, "invalid search arguments")
            try:
                SearchInput.model_validate(body.arguments)
            except ValidationError as exc:
                raise HTTPException(422, "invalid search arguments") from exc
        interpreter_operation = None
        frozen_grants = None
        if isinstance(provider, AgentCoreClient) and body.tool.startswith("code_interpreter___"):
            interpreter_operation = body.tool.split("___", 1)[1]
            if (interpreter_operation not in INTERPRETER_FIELDS or
                    set(body.arguments) != INTERPRETER_FIELDS[interpreter_operation] or
                    (interpreter_operation != "start" and body.execution_id is None)):
                raise HTTPException(422, "invalid interpreter arguments")
            if interpreter_operation == "start" and body.execution_id is None:
                if decision.native_admission is not None:
                    frozen_grants = [name for name in decision.native_admission.frozen_grants
                                     if name.startswith("code_interpreter___")]
                else:
                    try:
                        listing = ToolListing.model_validate(await authorizer.list_tools(request.headers))
                    except (httpx.HTTPError, ValidationError, ValueError, KeyError) as exc:
                        raise HTTPException(503, "tool authority unavailable") from exc
                    if (not listing.allowed or listing.principal != decision.principal or
                            listing.run_id != decision.run_id or listing.workload_id != decision.workload_id or
                            listing.run_expires_at != decision.run_expires_at):
                        raise HTTPException(403, "tool authority refused")
                    frozen_grants = sorted({entry.name for entry in listing.tools
                                            if entry.name.startswith("code_interpreter___") and
                                            entry.name.split("___", 1)[1] in INTERPRETER_FIELDS})
                if INTERPRETER_START not in frozen_grants:
                    raise HTTPException(403, "tool authority refused")
            if interpreter_operation == "result":
                await asyncio.to_thread(executions.lookup, body.execution_id, body.request_id,
                                        "code_interpreter___execute", decision)
            if interpreter_operation == "close":
                if native_interpreter_close:
                    operation_key, operation, created = await asyncio.to_thread(
                        executions.claim_native_cleanup, body.execution_id, body.request_id,
                        body.arguments, decision)
                    if created:
                        current = await close_owned_execution(body.execution_id, INTERPRETER_START, decision)
                        operation = await confirm_native_close(operation_key, operation, current, decision)
                    receipt = await operation_receipt(operation, body.tool, decision)
                    return JSONResponse(status_code=200 if receipt["status"] == "confirmed" else 202,
                                        content=receipt, headers={"Cache-Control": "no-store"})
                try:
                    prior = await asyncio.to_thread(executions.lookup, body.execution_id, body.request_id,
                                                    body.tool, decision)
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
                else:
                    if prior["request_digest"]["S"] != hashlib.sha256(canonical(body.arguments)).hexdigest():
                        raise HTTPException(409, "request id payload conflict")
                    prior = await recover_interpreter_result(body.execution_id, body.request_id,
                                                             body.tool, decision, prior)
                    receipt = await operation_receipt(prior, body.tool, decision)
                    return JSONResponse(status_code=200 if receipt["status"] == "confirmed" else 202, content=receipt)
        browser_operation = None
        if isinstance(provider, AgentCoreClient) and body.tool.startswith("browser___"):
            browser_operation = body.tool.split("___", 1)[1]
            fields = set(body.arguments)
            if (browser_operation not in BROWSER_FIELDS or
                    not BROWSER_FIELDS[browser_operation] <= fields or
                    fields - BROWSER_FIELDS[browser_operation] -
                    ({"url", "candidate_id", "seconds"} if browser_operation == "step" else set()) or
                    (browser_operation != "start" and body.execution_id is None)):
                raise HTTPException(422, "invalid browser arguments")
            if browser_operation == "start" and body.execution_id is None:
                if decision.native_admission is not None:
                    frozen_grants = [name for name in decision.native_admission.frozen_grants
                                     if name.startswith("browser___")]
                else:
                    try:
                        listing = ToolListing.model_validate(await authorizer.list_tools(request.headers))
                    except (httpx.HTTPError, ValidationError, ValueError, KeyError) as exc:
                        raise HTTPException(503, "tool authority unavailable") from exc
                    if (not listing.allowed or listing.principal != decision.principal or
                            listing.run_id != decision.run_id or listing.workload_id != decision.workload_id or
                            listing.run_expires_at != decision.run_expires_at):
                        raise HTTPException(403, "tool authority refused")
                    frozen_grants = sorted({entry.name for entry in listing.tools
                                            if entry.name.startswith("browser___") and
                                            entry.name.split("___", 1)[1] in BROWSER_FIELDS})
                if BROWSER_START not in frozen_grants:
                    raise HTTPException(403, "tool authority refused")
            if browser_operation == "close":
                try:
                    prior = await asyncio.to_thread(executions.lookup, body.execution_id, body.request_id,
                                                    body.tool, decision)
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
                else:
                    if prior["request_digest"]["S"] != hashlib.sha256(canonical(body.arguments)).hexdigest():
                        raise HTTPException(409, "request id payload conflict")
                    prior = await recover_browser_result(body.execution_id, body.request_id,
                                                         body.tool, decision, prior)
                    receipt = await operation_receipt(prior, body.tool, decision)
                    return JSONResponse(status_code=200 if receipt["status"] == "confirmed" else 202, content=receipt)
        operation_key, operation, created = await asyncio.to_thread(
            executions.claim, body.execution_id, body.request_id, body.tool, body.arguments, decision,
            frozen_grants, body.ttl_seconds,
        )
        if not created:
            if decision.native_admission is not None:
                # A repeated public operation cannot trigger paid recovery with
                # the call's already-consumed single-send allowance.
                return JSONResponse(status_code=202, content=executions.receipt(operation),
                                    headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
            operation = await recover_search_result(UUID(operation["operation_execution_id"]["S"]), body.request_id,
                                                    body.tool, decision, operation)
            operation = await recover_interpreter_result(UUID(operation["operation_execution_id"]["S"]),
                                                        body.request_id, body.tool, decision, operation)
            operation = await recover_browser_result(UUID(operation["operation_execution_id"]["S"]),
                                                    body.request_id, body.tool, decision, operation)
            receipt = await operation_receipt(operation, body.tool, decision)
            if body.tool == "websearch___search" and receipt["status"] != "confirmed":
                return await search_unknown_response(operation, decision)
            return JSONResponse(status_code=200 if receipt["status"] == "confirmed" else 202, content=receipt)
        if body.tool in {INTERPRETER_START, BROWSER_START}:
            decision = decision.model_copy(update={"execution_deadline_at": int(
                operation["execution_deadline_at"]["N"])})
        await asyncio.to_thread(executions.dispatched, operation_key)
        operation["dispatch_intent"] = {"S": "dispatched"}
        operation["status"] = {"S": "unknown"}
        if body.tool in ATLASSIAN_WRITES:
            operation["write_report_status"] = {"S": "pending"}
        if body.tool in {
            "atlassian___create_jira_issue", "atlassian___update_jira_issue",
            "atlassian___comment_jira_issue", "atlassian___transition_jira_issue",
        }:
            try:
                recorded = json.loads(operation["atlassian_binding"]["S"])
                decision._atlassian_operation = {
                    **recorded,
                    "tool": body.tool,
                    "operation_id": operation_key,
                    "execution_id": operation["operation_execution_id"]["S"],
                    "request_id": operation["request_id"]["S"],
                    "receipt_id": operation["receipt_id"]["S"],
                    "payload_sha256": operation["request_digest"]["S"],
                    "dispatch_intent": "dispatched",
                }
            except (KeyError, TypeError, ValueError):
                await record_operation(operation, executions.receipt(operation), decision)
                return JSONResponse(status_code=202, content=executions.receipt(operation))
        if body.tool in MEMORY_TOOLS:
            decision._knowledge_memory = {
                "schema_version": "1.0", "request_id": str(body.request_id),
                "execution_id": operation["operation_execution_id"]["S"],
                "requested_execution_id": str(body.execution_id) if body.execution_id else None,
            }
        try:
            arguments = body.arguments
            if body.tool == "websearch___search" and isinstance(provider, AgentCoreClient):
                arguments = {"query": body.arguments["query"], "execution_id": operation["operation_execution_id"]["S"],
                             "request_id": str(body.request_id)}
            if interpreter_operation is not None:
                arguments = {**body.arguments, "execution_id": operation["operation_execution_id"]["S"]}
                if interpreter_operation not in {"close"}:
                    arguments["request_id"] = str(body.request_id)
                else:
                    await asyncio.to_thread(executions.close, body.execution_id, INTERPRETER_START, decision)
            if browser_operation is not None:
                arguments = {**body.arguments, "execution_id": operation["operation_execution_id"]["S"],
                             "request_id": str(body.request_id)}
                if browser_operation == "close":
                    await asyncio.to_thread(executions.close, body.execution_id, BROWSER_START, decision)
            result = await provider.invoke(body.tool, arguments, decision)
        except (httpx.HTTPError, BotoCoreError, ClientError, ValueError, KeyError, RuntimeError):
            await record_operation(operation, executions.receipt(operation), decision)
            return JSONResponse(status_code=202, content=executions.receipt(operation))
        tool_result = result.get("result") if isinstance(result, dict) else None
        search_result = tool_result.get("result") if isinstance(tool_result, dict) else None
        if body.tool == "websearch___search" and isinstance(provider, AgentCoreClient):
            if (not isinstance(search_result, dict) or search_result.get("status") != "confirmed"
                    or search_result.get("execution_id") != operation["operation_execution_id"]["S"]
                    or search_result.get("request_id") != str(body.request_id)
                    or search_result.get("receipt_id") != operation["receipt_id"]["S"]
                    or not search_artifact_valid(search_result)):
                return await search_unknown_response(operation, decision, search_result)
        if interpreter_operation is not None:
            if (not isinstance(search_result, dict) or not isinstance(tool_result, dict) or
                    not interpreter_receipt_matches(operation["operation_execution_id"]["S"],
                        search_result.get("request_id") if isinstance(search_result, dict) else None,
                        interpreter_operation, decision,
                        search_result.get("receipt_id") if isinstance(search_result, dict) else None, operation) or
                    tool_result.get("user_id") != decision.principal.user_id or
                    tool_result.get("tenant_id") != decision.principal.tenant_id or
                    not isinstance(search_result.get("receipt_id"), str) or
                    search_result.get("execution_id") != operation["operation_execution_id"]["S"] or
                    search_result.get("status") not in {"confirmed", "failed"} or
                    (interpreter_operation != "close" and search_result.get("request_id") != str(body.request_id)) or
                    (interpreter_operation == "close" and
                     (search_result.get("termination_confirmed") is not True or
                      search_result.get("status") != "confirmed"))):
                await record_operation(operation, executions.receipt(operation), decision)
                return JSONResponse(status_code=202, content=executions.receipt(operation))
            if search_result["status"] == "failed":
                result = {**result, "result": {**tool_result, "isError": True}}
        if browser_operation is not None:
            if (not isinstance(tool_result, dict) or not isinstance(search_result, dict) or
                    tool_result.get("user_id") != decision.principal.user_id or
                    tool_result.get("tenant_id") != decision.principal.tenant_id or
                    search_result.get("execution_id") != operation["operation_execution_id"]["S"] or
                    search_result.get("request_id") != str(body.request_id) or
                    search_result.get("receipt_id") != operation["receipt_id"]["S"] or
                    search_result.get("status") not in {"confirmed", "failed"} or
                    (browser_operation == "close" and
                     (search_result["status"] != "confirmed" or
                      not isinstance(search_result.get("result"), dict) or
                      search_result["result"].get("termination_confirmed") is not True))):
                await record_operation(operation, executions.receipt(operation), decision)
                return JSONResponse(status_code=202, content=executions.receipt(operation))
            if search_result["status"] == "failed":
                result = {**result, "result": {**tool_result, "isError": True}}
        try:
            confirmed = await asyncio.to_thread(executions.confirmed, operation_key, result)
        except HTTPException:
            await record_operation(operation, executions.receipt(operation), decision)
            return JSONResponse(status_code=202, content=executions.receipt(operation))
        if interpreter_operation == "close":
            await asyncio.to_thread(executions.confirm_closed, body.execution_id, INTERPRETER_START, decision,
                                    search_result)
        if browser_operation == "close":
            await recover_browser_result(body.execution_id, body.request_id, body.tool, decision, confirmed)
        receipt = {**await operation_receipt(confirmed, body.tool, decision),
                   **({"gateway_url_sha256": expected_gateway} if expected_gateway else {})}
        return JSONResponse(status_code=200 if receipt["status"] == "confirmed" else 202, content=receipt)

    def browser_artifact(receipt):
        result = receipt.get("result")
        for _ in range(5):
            if not isinstance(result, dict):
                break
            artifact = result.get("artifact")
            if isinstance(artifact, dict):
                return artifact
            result = result.get("result")
        return None

    async def record_operation(operation, receipt, decision):
        if (not isinstance(authorizer, GatewayAuthorizer) or decision.native_admission is not None
                or decision.native_cleanup is not None
                or operation.get("dispatch_intent", {}).get("S") != "dispatched"):
            return
        payload = {
            "schema_version": "1.0", "receipt_id": operation["receipt_id"]["S"],
            "tenant_id": decision.principal.tenant_id,
            "user_id": decision.principal.user_id,
            "team_id": decision.principal.team_id,
            "run_id": decision.run_id,
            "tool": operation["tool"]["S"],
            "request_id": operation["request_id"]["S"],
            "execution_id": operation["operation_execution_id"]["S"],
            "result_status": ("failed" if receipt.get("result_status") == "error" else
                              "confirmed" if receipt["status"] == "confirmed" else "unknown"),
            **({"cleanup_status": receipt["cleanup_status"]} if "cleanup_status" in receipt else {}),
        }
        write = operation["tool"]["S"] in ATLASSIAN_WRITES
        if write and operation.get("write_report_status", {}).get("S") != "pending":
            return
        try:
            acknowledgement = await authorizer.record_outcome(payload)
            if write and (not isinstance(acknowledgement, dict) or acknowledgement.get("status") != "recorded"):
                raise ValueError("Jira write report acknowledgement unavailable")
        except (HTTPException, httpx.HTTPError, BotoCoreError, ValueError, KeyError):
            logging.getLogger(__name__).warning("tool outcome not recorded; owner recovery can retry")
            if write:
                try:
                    await asyncio.to_thread(executions.finish_write_report, operation, recorded=False)
                except (HTTPException, ValueError):
                    logging.getLogger(__name__).warning("Jira write report retry remains due")
        else:
            if write:
                try:
                    recorded = await asyncio.to_thread(executions.finish_write_report, operation, recorded=True)
                    if recorded is not None:
                        receipt["report_status"] = "recorded"
                except (HTTPException, ValueError):
                    logging.getLogger(__name__).warning("Jira write report acknowledgement remains due")

    async def record_original_cleanup(execution_id, tool, decision, cleanup_status):
        if (not isinstance(authorizer, GatewayAuthorizer) or decision.native_admission is not None
                or decision.native_cleanup is not None):
            return
        try:
            original = await asyncio.to_thread(executions.original_operation, execution_id, tool, decision)
        except HTTPException:
            logging.getLogger(__name__).warning("tool cleanup outcome not recorded; owner recovery can retry")
            return
        if original is not None:
            await record_operation(
                original, {**executions.receipt(original), "cleanup_status": cleanup_status}, decision,
            )

    async def operation_receipt(operation, tool, decision):
        receipt = executions.receipt(operation)
        if tool in {"browser___start", "browser___step", "browser___inspect"} and receipt["status"] == "confirmed":
            artifact = browser_artifact(receipt)
            if artifact is not None:
                receipt["artifact"] = artifact
        if tool in {"code_interpreter___close", "browser___close"} and receipt["status"] == "confirmed":
            current = await asyncio.to_thread(executions.close, UUID(receipt["execution_id"]),
                                             tool.replace("___close", "___start"), decision)
            await record_original_cleanup(UUID(receipt["execution_id"]),
                                          tool.replace("___close", "___start"), decision,
                                          current["cleanup_status"])
            if current["cleanup_status"] != "confirmed":
                # Keep the original operation evidence, but never report a
                # successful close while the execution lacks terminal proof.
                receipt = {**receipt, "status": "unknown", "cleanup_status": "unknown"}
        await record_operation(operation, receipt, decision)
        return receipt

    def search_artifact_valid(receipt):
        if not isinstance(receipt, dict) or not isinstance(receipt.get("result"), dict):
            return False
        artifact = receipt.get("artifact")
        if not isinstance(artifact, dict):
            return False
        content = canonical(receipt["result"])
        return (artifact.get("artifact_id") == receipt.get("request_id") and
                artifact.get("content_type") == "application/json" and
                type(artifact.get("byte_length")) is int and artifact["byte_length"] == len(content) and
                artifact.get("content_sha256") == hashlib.sha256(content).hexdigest())

    async def search_unknown_response(operation, decision, provider_receipt=None):
        receipt = executions.receipt(operation)
        await record_operation(operation, receipt, decision)
        seconds = operation.get("retry_after_seconds", {}).get("N")
        if isinstance(provider_receipt, dict) and all(provider_receipt.get(key) == value for key, value in (
                ("status", "unknown"), ("execution_id", operation["operation_execution_id"]["S"]),
                ("request_id", operation["request_id"]["S"]),
                ("receipt_id", operation["receipt_id"]["S"]))):
            seconds = provider_receipt.get("retry_after_seconds", seconds)
        if isinstance(seconds, int | str) and str(seconds).isascii() and str(seconds).isdecimal() and (
                0 < int(seconds) <= 86400):
            receipt["retry_after_seconds"] = int(seconds)
            return JSONResponse(status_code=202, content=receipt, headers={"Retry-After": str(seconds)})
        return JSONResponse(status_code=202, content=receipt)

    async def recover(request, tool, body=None, *, execution_id=None, request_id=None):
        require_identity(request)
        native_requested = bool(request.headers.get("x-adp-native-test-capability"))
        atlassian_receipt = body is not None and body.tool.startswith("atlassian___")
        if (native_requested != bool(request.headers.get("x-adp-native-test-operation"))
                or native_requested and (body is None
                    or not (body.tool == "code_interpreter___execute" or atlassian_receipt)
                    or not isinstance(authorizer, GatewayAuthorizer) or not isinstance(provider, AgentCoreClient))):
            raise HTTPException(403, "native recovery authority refused")
        try:
            reply = (await authorizer.recover(tool, request.headers, call=body,
                         execution_id=execution_id, request_id=request_id)
                     if isinstance(authorizer, GatewayAuthorizer) else await authorizer.recover(tool, request.headers))
            if native_requested and "native_status" in reply:
                raise HTTPException(409, "original native recovery already attempted")
            decision = Decision.model_validate(reply)
            if native_requested:
                if atlassian_receipt:
                    binding = decision.native_receipt_read
                    if (binding is None or decision.native_admission is not None or decision.native_cleanup is not None
                            or binding.tool != body.tool or binding.request_id != body.request_id
                            or str(binding.original_operation_id) != request.headers.get("x-adp-native-test-operation")
                            or body.execution_id is not None and binding.execution_id != body.execution_id):
                        raise HTTPException(403, "native recovery authority refused")
                elif (decision.native_admission is None or decision.native_cleanup is not None
                      or decision.native_receipt_read is not None):
                    raise HTTPException(403, "native recovery authority refused")
        except HTTPException:
            raise
        except (httpx.HTTPError, ValidationError, ValueError, KeyError) as exc:
            raise HTTPException(503, "tool authority unavailable") from exc
        if not decision.allowed or decision.principal.user_id != decision.principal.root_human_id:
            raise HTTPException(403, "tool authority refused")
        if (request.headers.get("x-adp-run-credential") or request.headers.get("x-adp-chat-capability")
                or request.headers.get("x-adp-crew-host-assertion")) and not (
            decision.run_id and decision.workload_id and decision.run_expires_at
        ):
            raise HTTPException(503, "tool authority unavailable")
        return decision

    @app.post("/api/agent/tools/executions/{execution_id}/close")
    @app.post("/agent/tools/executions/{execution_id}/close")
    async def execution_close(execution_id: UUID, request: Request,
                              tool: str = Query(pattern=TOOL_NAME_PATTERN)):
        require_identity(request)
        try:
            authorized = await (
                authorizer.close(tool, request.headers, execution_id) if isinstance(authorizer, GatewayAuthorizer)
                else authorizer.close(tool, request.headers))
            if isinstance(authorizer, GatewayAuthorizer) and "native_status" in authorized:
                return JSONResponse(status_code=202, content=authorized, headers={"Cache-Control": "no-store"})
            decision = Decision.model_validate(authorized)
            if decision.native_cleanup is not None:
                if not isinstance(authorizer, GatewayAuthorizer) or not isinstance(provider, AgentCoreClient):
                    raise ValueError("Native cleanup runtime required")
                decision._native_handoff = NativeCleanupHandoff(
                    admission=decision.native_cleanup, decision=decision, execution_id=execution_id,
                    tool=tool, headers=request.headers)
        except HTTPException:
            raise
        except (httpx.HTTPError, ValidationError, ValueError, KeyError) as exc:
            raise HTTPException(503, "tool authority unavailable") from exc
        if not decision.allowed or decision.principal.user_id != decision.principal.root_human_id:
            raise HTTPException(403, "tool authority refused")
        if (request.headers.get("x-adp-run-credential") or request.headers.get("x-adp-chat-capability")
                or request.headers.get("x-adp-crew-host-assertion")) and not (
            decision.run_id and decision.workload_id and decision.run_expires_at
        ):
            raise HTTPException(503, "tool authority unavailable")
        receipt = await close_owned_execution(execution_id, tool, decision)
        return JSONResponse(status_code=200 if receipt["cleanup_status"] == "confirmed" else 202, content=receipt)

    async def close_owned_execution(execution_id, tool, decision):
        """Direct and lifecycle close share the original execution handoff."""
        receipt = await asyncio.to_thread(executions.close, execution_id, tool, decision)
        root_tool = await asyncio.to_thread(executions.root_tool, execution_id, tool, decision)
        if receipt["cleanup_status"] != "confirmed" and root_tool in SYNCHRONOUS_TOOLS:
            if await asyncio.to_thread(executions.stateless_ready, execution_id):
                receipt = await asyncio.to_thread(executions.confirm_closed, execution_id, root_tool, decision)
        if (root_tool in {INTERPRETER_START, BROWSER_START} and isinstance(provider, AgentCoreClient)
                and receipt["cleanup_status"] != "confirmed"):
            try:
                found = await provider.invoke(root_tool.replace("___start", "___execution_close"),
                    {"execution_id": str(execution_id)}, decision)
                closed = found.get("result", {}).get("result", {})
                if executions.termination_proved(
                        {"execution_id": {"S": str(execution_id)}, "tool": {"S": root_tool}}, closed):
                    receipt = await asyncio.to_thread(executions.confirm_closed, execution_id, root_tool, decision,
                                                      closed)
            except (httpx.HTTPError, BotoCoreError, ClientError, HTTPException,
                    ValueError, KeyError, RuntimeError, AttributeError):
                pass
        await record_original_cleanup(execution_id, root_tool, decision, receipt["cleanup_status"])
        return receipt

    async def confirm_native_close(operation_key, operation, current, decision):
        if current["cleanup_status"] != "confirmed" or operation["status"]["S"] == "confirmed":
            return operation
        result = {"jsonrpc": "2.0", "result": {
            "result": {**current, "status": "confirmed", "termination_confirmed": True,
                       "session_id": operation["cleanup_session_id"]["S"],
                       "request_id": operation["request_id"]["S"]},
            "receipt_id": operation["receipt_id"]["S"], "user_id": decision.principal.user_id,
            "tenant_id": decision.principal.tenant_id}}
        try:
            return await asyncio.to_thread(executions.confirmed, operation_key, result)
        except HTTPException:
            return operation

    async def recover_search_result(execution_id, request_id, tool, decision, operation):
        if (tool != "websearch___search" or not isinstance(provider, AgentCoreClient)
                or operation["status"]["S"] != "unknown"):
            return operation
        try:
            found = await provider.invoke("websearch___operation_get",
                {"execution_id": str(execution_id), "request_id": str(request_id)}, decision)
            envelope = found.get("result", {})
            receipt = envelope.get("result", {})
            if (isinstance(receipt, dict) and receipt.get("status") == "unknown" and
                    all(receipt.get(field) == value for field, value in (
                        ("execution_id", str(execution_id)), ("request_id", str(request_id)),
                        ("receipt_id", operation["receipt_id"]["S"])))):
                seconds = receipt.get("retry_after_seconds")
                if type(seconds) is int and 0 < seconds <= 86400:
                    return {**operation, "retry_after_seconds": {"N": str(seconds)}}
            if not isinstance(receipt, dict) or any(receipt.get(field) != value for field, value in (
                ("status", "confirmed"), ("execution_id", str(execution_id)),
                ("request_id", str(request_id)), ("receipt_id", operation["receipt_id"]["S"]),
            )) or not search_artifact_valid(receipt):
                return operation
            original = {"jsonrpc": "2.0", "id": "recovered", "result": {
                "result": receipt, "receipt_id": receipt["receipt_id"],
                "user_id": decision.principal.user_id, "tenant_id": decision.principal.tenant_id,
            }}
            operation_key = f"OP#{decision.principal.tenant_id}#{decision.principal.user_id}#{tool}#{request_id}"
            return await asyncio.to_thread(executions.confirmed, operation_key, original)
        except (httpx.HTTPError, BotoCoreError, ClientError, HTTPException,
                ValueError, KeyError, RuntimeError, AttributeError):
            return await asyncio.to_thread(executions.lookup, execution_id, request_id, tool, decision)

    async def recover_interpreter_result(execution_id, request_id, tool, decision, operation):
        closing = tool == "code_interpreter___close"
        if (not tool.startswith("code_interpreter___") or not isinstance(provider, AgentCoreClient) or
                operation["status"]["S"] != "unknown" and not
                (closing and operation["status"]["S"] == "confirmed")):
            return operation
        try:
            if closing:
                current = await asyncio.to_thread(executions.close, execution_id, INTERPRETER_START, decision)
                if "native_cleanup_operation_id" in operation:
                    # Status never consumes a new cleanup send. A separate
                    # original cleanup attempt/reconciler may have proved it.
                    return await confirm_native_close(operation["execution_id"]["S"], operation, current, decision)
                if current["cleanup_status"] == "confirmed" and operation["status"]["S"] == "confirmed":
                    return operation
            found = await provider.invoke("code_interpreter___execution_close" if closing else
                "code_interpreter___operation_get", {"execution_id": str(execution_id)} if closing else
                {"execution_id": str(execution_id), "request_id": str(request_id)}, decision)
            if type(decision._native_handoff) is NativeInterpreterRecoveryHandoff:
                decision._native_handoff.observe_original(found)
            receipt = found.get("result", {}).get("result", {})
            if tool in {"code_interpreter___execute", "code_interpreter___result"} and (
                    receipt.get("status") == "pending" and receipt.get("provider_id") and receipt.get("session_id")):
                found = await provider.invoke("code_interpreter___result", {
                    "execution_id": str(execution_id), "request_id": str(request_id),
                    "session_id": receipt["session_id"], "provider_id": receipt["provider_id"],
                }, decision)
                receipt = found.get("result", {}).get("result", {})
            if (not isinstance(receipt, dict) or
                    not interpreter_receipt_matches(str(execution_id), receipt.get("request_id"),
                        tool.split("___", 1)[1], decision, receipt.get("receipt_id"), operation) or
                    (tool in {"code_interpreter___execute", "code_interpreter___result"} and
                     not receipt.get("provider_id") and
                     not (tool == "code_interpreter___execute" and receipt.get("status") == "failed")) or
                    receipt.get("execution_id") != str(execution_id) or
                    (not closing and receipt.get("request_id") != str(request_id)) or
                    (closing and (receipt.get("termination_confirmed") is not True or
                                  receipt.get("status") != "confirmed")) or
                    receipt.get("status") not in {"confirmed", "failed"}):
                return operation
            if closing:
                closed = await asyncio.to_thread(executions.confirm_closed, execution_id, INTERPRETER_START, decision,
                                                 receipt)
                if closed["cleanup_status"] != "confirmed":
                    return operation
            if operation["status"]["S"] == "confirmed":
                return operation
            original = {"jsonrpc": "2.0", "id": "recovered", "result": {
                "result": receipt, "receipt_id": receipt.get("receipt_id"),
                "user_id": decision.principal.user_id, "tenant_id": decision.principal.tenant_id,
                **({"isError": True} if receipt["status"] == "failed" else {}),
            }}
            operation_key = f"OP#{decision.principal.tenant_id}#{decision.principal.user_id}#{tool}#{request_id}"
            return await asyncio.to_thread(executions.confirmed, operation_key, original)
        except (httpx.HTTPError, BotoCoreError, ClientError, HTTPException,
                ValueError, KeyError, RuntimeError, AttributeError):
            return await asyncio.to_thread(executions.lookup, execution_id, request_id, tool, decision)

    async def recover_browser_result(execution_id, request_id, tool, decision, operation):
        closing = tool == "browser___close"
        if (not tool.startswith("browser___") or not isinstance(provider, AgentCoreClient) or
                operation["status"]["S"] != "unknown" and not
                (closing and operation["status"]["S"] == "confirmed")):
            return operation
        try:
            if closing:
                current = await asyncio.to_thread(executions.close, execution_id, BROWSER_START, decision)
                if current["cleanup_status"] == "confirmed" and operation["status"]["S"] == "confirmed":
                    return operation
            found = await provider.invoke("browser___operation_get",
                {"execution_id": str(execution_id), "request_id": str(request_id)}, decision)
            receipt = found.get("result", {}).get("result", {})
            if (not isinstance(receipt, dict) or
                    receipt.get("execution_id") != str(execution_id) or
                    receipt.get("request_id") != str(request_id) or
                    receipt.get("receipt_id") != operation["receipt_id"]["S"] or
                    receipt.get("status") not in {"confirmed", "failed"} or
                    (tool == "browser___close" and
                     (receipt["status"] != "confirmed" or
                      not isinstance(receipt.get("result"), dict) or
                      receipt["result"].get("termination_confirmed") is not True))):
                return operation
            if tool == "browser___close":
                await asyncio.to_thread(executions.close, execution_id, BROWSER_START, decision)
                found_close = await provider.invoke("browser___execution_close", {"execution_id": str(execution_id)},
                                                    decision)
                verified_close = found_close.get("result", {}).get("result", {})
                closed = await asyncio.to_thread(executions.confirm_closed, execution_id, BROWSER_START, decision,
                                                 verified_close)
                if closed["cleanup_status"] != "confirmed":
                    return operation
            if operation["status"]["S"] == "confirmed":
                return operation
            original = {"jsonrpc": "2.0", "id": "recovered", "result": {
                "result": receipt, "receipt_id": receipt["receipt_id"],
                "user_id": decision.principal.user_id, "tenant_id": decision.principal.tenant_id,
                **({"isError": True} if receipt["status"] == "failed" else {}),
            }}
            operation_key = f"OP#{decision.principal.tenant_id}#{decision.principal.user_id}#{tool}#{request_id}"
            return await asyncio.to_thread(executions.confirmed, operation_key, original)
        except (httpx.HTTPError, BotoCoreError, ClientError, HTTPException,
                ValueError, KeyError, RuntimeError, AttributeError):
            return await asyncio.to_thread(executions.lookup, execution_id, request_id, tool, decision)

    @app.post("/api/agent/tools/operations/recover")
    @app.post("/agent/tools/operations/recover")
    async def operation_recover(body: ToolCall, request: Request):
        decision = await recover(request, body.tool, body)
        if decision.native_receipt_read is not None:
            operation = await asyncio.to_thread(executions.lookup, decision.native_receipt_read.execution_id,
                                               body.request_id, body.tool, decision)
            return executions.receipt(operation)
        operation = await asyncio.to_thread(executions.recover_request, body.request_id, body.tool,
            body.arguments, decision, body.ttl_seconds, body.execution_id)
        execution_id = UUID(operation["operation_execution_id"]["S"])
        if decision.native_admission is not None:
            try:
                decision._native_handoff = NativeInterpreterRecoveryHandoff(
                    operation=operation, gateway=authorizer, admission=decision.native_admission,
                    call=body, headers=request.headers, decision=decision)
                await decision._native_handoff.begin()
            except (ValueError, KeyError, httpx.HTTPError):
                raise HTTPException(503, "native recovery dispatch unavailable") from None
        operation = await recover_search_result(execution_id, body.request_id, body.tool, decision, operation)
        operation = await recover_interpreter_result(execution_id, body.request_id, body.tool, decision, operation)
        operation = await recover_browser_result(execution_id, body.request_id, body.tool, decision, operation)
        return await operation_receipt(operation, body.tool, decision)

    @app.get("/api/agent/tools/executions/{execution_id}/operations/{request_id}")
    @app.get("/agent/tools/executions/{execution_id}/operations/{request_id}")
    async def operation_get(execution_id: UUID, request_id: UUID, request: Request,
                            tool: str = Query(pattern=TOOL_NAME_PATTERN)):
        decision = await recover(request, tool, execution_id=execution_id, request_id=request_id)
        operation = await asyncio.to_thread(executions.lookup, execution_id, request_id, tool, decision)
        operation = await recover_search_result(execution_id, request_id, tool, decision, operation)
        operation = await recover_interpreter_result(execution_id, request_id, tool, decision, operation)
        operation = await recover_browser_result(execution_id, request_id, tool, decision, operation)
        return await operation_receipt(operation, tool, decision)

    @app.get("/api/agent/tools/executions/{execution_id}/operations/{request_id}/artifacts/result")
    @app.get("/agent/tools/executions/{execution_id}/operations/{request_id}/artifacts/result")
    async def artifact_read(execution_id: UUID, request_id: UUID, request: Request,
                            tool: str = Query(pattern=TOOL_NAME_PATTERN), offset: int = Query(default=0, ge=0),
                            length: int = Query(default=8192, ge=1, le=8192)):
        decision = await recover(request, tool, execution_id=execution_id, request_id=request_id)
        operation = await asyncio.to_thread(executions.lookup, execution_id, request_id, tool, decision)
        operation = await recover_search_result(execution_id, request_id, tool, decision, operation)
        operation = await recover_interpreter_result(execution_id, request_id, tool, decision, operation)
        operation = await recover_browser_result(execution_id, request_id, tool, decision, operation)
        if tool in {"browser___start", "browser___step", "browser___inspect"} and isinstance(provider, AgentCoreClient):
            receipt = await operation_receipt(operation, tool, decision)
            artifact = receipt.get("artifact")
            if receipt["status"] != "confirmed" or not isinstance(artifact, dict):
                raise HTTPException(404, "Browser artifact unavailable")
            digest, total = artifact.get("content_sha256"), artifact.get("byte_length")
            if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or
                    artifact.get("artifact_id") != str(request_id) or
                    type(total) is not int or total < 1):
                raise HTTPException(503, "Browser artifact metadata invalid")
            if offset >= total:
                raise HTTPException(416, "invalid artifact range")
            try:
                response = await provider.invoke("browser___artifact_read",
                    {"execution_id": str(execution_id), "artifact_id": str(request_id), "offset": offset}, decision)
                chunk = response["result"]["result"]
                data = base64.b64decode(chunk["data_base64"], validate=True)
            except (httpx.HTTPError, BotoCoreError, ClientError, HTTPException,
                    KeyError, ValueError, TypeError, RuntimeError, AttributeError) as exc:
                raise HTTPException(503, "Browser artifact read unavailable") from exc
            if (chunk.get("artifact_id") != str(request_id) or chunk.get("sha256") != digest or
                    chunk.get("total_length") != total or chunk.get("offset") != offset or
                    chunk.get("content_type") != artifact.get("content_type") or
                    chunk.get("length") != len(data) or len(data) != min(8192, total - offset)):
                raise HTTPException(503, "Browser artifact integrity mismatch")
            data = data[:length]
            return {"artifact_id": "result", "content_type": chunk["content_type"],
                    "offset": offset, "length": len(data), "total_length": total,
                    "sha256": digest, "data_base64": base64.b64encode(data).decode("ascii")}
        return executions.result_chunk(operation, offset, length)

    return app


app = create_app()
