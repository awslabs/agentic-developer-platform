"""Private Jira user read; no raw OAuth token crosses the broker boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.responses import JSONResponse, Response

from src.agentauth.knowledge_service import bounded_body
from src.auth.connections_routes import get_identity_client
from src.internal.credential_routes import _identity_access_token
from src.internal.github_oauth import canonical
from src.internal.jira_oauth import protected_sites, selected_jira_connection
from src.internal.jira_provider import CLOUD_ID, ISSUE_ID, ISSUE_KEY, observe_jira_sites, observe_jira_user, read_jira_issue, select_jira_site
from src.internal.mcp_params import operation_params
from src.internal.tools_dynamic_catalogue import publish_catalogue
from src.internal.tools_human_authority import current_tools_human
from src.internal.tools_knowledge import check_fresh, check_policy, current_binding, require_transport, verify_facade
from src.internal.tools_legacy_fence import require_legacy_generation
from src.shared.database import get_session_factory
from src.shared.models.vault import OAuthToolReceipt

PATH = "/internal/v1/tools/jira/mcp"
router = APIRouter(tags=["tools-jira"])
TOOLS = [
    {
        "name": "get_issue",
        "description": "Read an issue from a Jira Cloud site verified for your connected account.",
        "inputSchema": {
            "type": "object",
            "properties": {"cloudId": {"type": "string"}, "issueIdOrKey": {"type": "string"}},
            "required": ["cloudId", "issueIdOrKey"],
            "additionalProperties": False,
        },
    }
]


@asynccontextmanager
async def current_authority(claims, runtime):
    async with get_session_factory()() as db:
        await check_policy(claims, "get_issue", db, runtime, namespace="jira")
        await require_legacy_generation(db, claims)
        user = await current_tools_human(db, claims["principal"])
        credential, binding, _ = await selected_jira_connection(db, user_id=user.id, tenant_id=user.org_id, expected=claims["jira_connection"])
        check_fresh(claims)
        yield credential, binding, user


async def claim_receipt(claims):
    digest = hashlib.sha256(
        canonical(
            {
                "principal": claims["principal"],
                "receipt": claims["receipt_id"],
                "tool": "jira___get_issue",
            }
        )
    ).hexdigest()
    try:
        async with get_session_factory()() as db, db.begin():
            db.add(OAuthToolReceipt(id=digest, expires_at=datetime.fromtimestamp(claims["expires_at"], UTC)))
            await db.flush()
    except IntegrityError:
        raise HTTPException(409, "Jira receipt already used") from None


async def serve(packet, runtime):
    method, request_id, params = packet["method"], packet.get("id"), operation_params(packet)
    deployment = await asyncio.to_thread(current_binding, metadata_only=method != "tools/call", namespace="jira")
    if method == "initialize":
        if not isinstance(params, dict) or params.get("protocolVersion") not in {"2024-11-05", "2025-03-26", "2025-06-18"}:
            raise HTTPException(400, "unsupported MCP initialization")
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": params["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "adp-private-jira", "version": "1.0"},
                },
            }
        )
    if method == "notifications/initialized" and "id" not in packet and params == {}:
        return Response(status_code=202)
    if method == "ping" and params == {}:
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {}})
    if method == "tools/list" and params == {}:
        if deployment["target_status"] == "READY" and os.environ.get("ADP_TOOLS_JIRA_TARGET_ID"):
            await asyncio.to_thread(
                publish_catalogue,
                binding=deployment,
                tools=TOOLS,
                client=runtime.store.client,
                table_name=runtime.store.table,
            )
        schema = {
            **TOOLS[0],
            "inputSchema": {**TOOLS[0]["inputSchema"], "properties": {**TOOLS[0]["inputSchema"]["properties"], "adp_assertion": {"type": "string"}}},
        }
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [schema]}}, headers={"Cache-Control": "no-store"})
    if (
        method != "tools/call"
        or not isinstance(params, dict)
        or set(params) != {"name", "arguments"}
        or params["name"] != "get_issue"
        or not isinstance(params["arguments"], dict)
        or set(params["arguments"]) != {"cloudId", "issueIdOrKey", "adp_assertion"}
    ):
        raise HTTPException(400, "unsupported Jira operation")
    arguments = {key: value for key, value in params["arguments"].items() if key != "adp_assertion"}
    cloud_id, issue = arguments["cloudId"], arguments["issueIdOrKey"]
    if not isinstance(cloud_id, str) or not CLOUD_ID.fullmatch(cloud_id):
        raise HTTPException(400, detail={"error": "jira_invalid_cloud_id"})
    if not isinstance(issue, str) or not (ISSUE_ID.fullmatch(issue) or ISSUE_KEY.fullmatch(issue)):
        raise HTTPException(400, detail={"error": "jira_invalid_issue_id"})
    claims = await verify_facade(params["arguments"]["adp_assertion"], "get_issue", arguments, request_id, namespace="jira")
    if not isinstance(claims.get("jira_connection"), dict) or claims["jira_connection"].get("connection_id") != claims.get("connection_id"):
        raise HTTPException(403, "Jira connection binding required")
    async with current_authority(claims, runtime) as (credential, connection, user):
        expected_site = select_jira_site(protected_sites(connection), cloud_id)
        await claim_receipt(claims)
        check_fresh(claims)
        token = await _identity_access_token(credential, user, get_identity_client())
    async with current_authority(claims, runtime) as (_, connection, _):
        observation = await observe_jira_user(token)
        if observation.account_id != connection.provider_account_id:
            raise HTTPException(403, detail={"error": "jira_identity_changed"})
    async with current_authority(claims, runtime):
        current_site = select_jira_site(await observe_jira_sites(token), cloud_id)
        if current_site != expected_site:
            raise HTTPException(403, detail={"error": "jira_site_changed"})
    async with current_authority(claims, runtime):
        result = await read_jira_issue(token, protected_site=current_site, cloud_id=cloud_id, issue_id_or_key=issue)
    async with current_authority(claims, runtime):
        if await asyncio.to_thread(current_binding, namespace="jira") != deployment:
            raise HTTPException(503, "Jira deployment changed")
        check_fresh(claims)
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result)}],
                },
            },
            headers={"Cache-Control": "no-store"},
        )


@router.post(PATH)
async def jira_mcp(request: Request):
    require_transport(request, namespace="jira")
    if request.url.query or any(
        request.headers.get(name)
        for name in (
            "x-adp-user-authorization",
            "x-adp-run-credential",
            "x-adp-workload-token",
            "x-adp-assertion",
        )
    ):
        raise HTTPException(400, "ambiguous Jira request")
    from src.agentauth.routes import get_agent_runtime

    try:
        async with asyncio.timeout(25):
            packet = json.loads(await bounded_body(request))
            if (
                not isinstance(packet, dict)
                or packet.get("jsonrpc") != "2.0"
                or not isinstance(packet.get("method"), str)
                or not set(packet) <= {"jsonrpc", "id", "method", "params"}
                or packet["method"] != "notifications/initialized"
                and (type(packet.get("id")) not in {str, int} or len(str(packet["id"])) > 256)
            ):
                raise HTTPException(400, "invalid Jira request")
            return await serve(packet, get_agent_runtime())
    except (ValueError, KeyError, TypeError, UnicodeError, OverflowError, httpx.HTTPError, TimeoutError, SQLAlchemyError, BotoCoreError, ClientError):
        raise HTTPException(503, "Jira service unavailable") from None
