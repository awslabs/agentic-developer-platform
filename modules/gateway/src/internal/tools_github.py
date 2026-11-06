"""Private GitHub user read; no raw OAuth token crosses the broker boundary."""

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
from src.internal.github_oauth import canonical, observe_github_user, selected_github_connection
from src.internal.mcp_params import operation_params
from src.internal.tools_dynamic_catalogue import publish_catalogue
from src.internal.tools_human_authority import current_tools_human
from src.internal.tools_knowledge import check_fresh, check_policy, current_binding, require_transport, verify_facade
from src.internal.tools_legacy_fence import require_legacy_generation
from src.shared.database import get_session_factory
from src.shared.models.vault import OAuthToolReceipt

PATH = "/internal/v1/tools/github/mcp"
router = APIRouter(tags=["tools-github"])
TOOLS = [
    {
        "name": "get_user",
        "description": "Read the verified connected GitHub user's profile.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    }
]


@asynccontextmanager
async def current_authority(claims, runtime):
    async with get_session_factory()() as db:
        await check_policy(claims, "get_user", db, runtime, namespace="github")
        await require_legacy_generation(db, claims)
        user = await current_tools_human(db, claims["principal"])
        credential, binding, _ = await selected_github_connection(db, user_id=user.id, tenant_id=user.org_id, expected=claims["github_connection"])
        check_fresh(claims)
        yield credential, binding, user


async def claim_receipt(claims):
    digest = hashlib.sha256(
        canonical(
            {
                "principal": claims["principal"],
                "receipt": claims["receipt_id"],
                "tool": "github___get_user",
            }
        )
    ).hexdigest()
    try:
        async with get_session_factory()() as db, db.begin():
            db.add(OAuthToolReceipt(id=digest, expires_at=datetime.fromtimestamp(claims["expires_at"], UTC)))
            await db.flush()
    except IntegrityError:
        raise HTTPException(409, "GitHub receipt already used") from None


async def serve(packet, runtime):
    method, request_id, params = packet["method"], packet.get("id"), operation_params(packet)
    deployment = await asyncio.to_thread(current_binding, metadata_only=method != "tools/call", namespace="github")
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
                    "serverInfo": {"name": "adp-private-github", "version": "1.0"},
                },
            }
        )
    if method == "notifications/initialized" and "id" not in packet and params == {}:
        return Response(status_code=202)
    if method == "ping" and params == {}:
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {}})
    if method == "tools/list" and params == {}:
        if deployment["target_status"] == "READY" and os.environ.get("ADP_TOOLS_GITHUB_TARGET_ID"):
            await asyncio.to_thread(
                publish_catalogue,
                binding=deployment,
                tools=TOOLS,
                client=runtime.store.client,
                table_name=runtime.store.table,
            )
        schema = {**TOOLS[0], "inputSchema": {**TOOLS[0]["inputSchema"], "properties": {"adp_assertion": {"type": "string"}}}}
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [schema]}}, headers={"Cache-Control": "no-store"})
    if (
        method != "tools/call"
        or not isinstance(params, dict)
        or set(params) != {"name", "arguments"}
        or params["name"] != "get_user"
        or not isinstance(params["arguments"], dict)
        or set(params["arguments"]) != {"adp_assertion"}
    ):
        raise HTTPException(400, "unsupported GitHub operation")
    claims = await verify_facade(params["arguments"]["adp_assertion"], "get_user", {}, request_id, namespace="github")
    if not isinstance(claims.get("github_connection"), dict) or claims["github_connection"].get("connection_id") != claims.get("connection_id"):
        raise HTTPException(403, "GitHub connection binding required")
    async with current_authority(claims, runtime) as (credential, _, user):
        await claim_receipt(claims)
        check_fresh(claims)
        token = await _identity_access_token(credential, user, get_identity_client())
    async with current_authority(claims, runtime) as (credential, _, _):
        from src.internal.github_oauth import github_app_client, is_github_app_grant, observe_github_app_user

        if is_github_app_grant(getattr(credential, "scopes", None)):
            client = await github_app_client()
            if client is None:
                raise HTTPException(403, "GitHub connection unqualified")
            observation = await observe_github_app_user(token, **client)
        else:
            observation = await observe_github_user(token)
    async with current_authority(claims, runtime) as (_, connection, _):
        if (
            observation.account_id != connection.provider_account_id
            or type(observation.profile.get("id")) is not int
            or str(observation.profile["id"]) != observation.account_id
            or list(observation.scopes) != connection.observed_scopes
            or (is_github_app_grant(getattr(credential, "scopes", None)) and list(observation.resources) != connection.observed_resources)
        ):
            raise HTTPException(403, "GitHub provider identity changed")
        if await asyncio.to_thread(current_binding, namespace="github") != deployment:
            raise HTTPException(503, "GitHub deployment changed")
        check_fresh(claims)
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(observation.profile)}],
                },
            },
            headers={"Cache-Control": "no-store"},
        )


@router.post(PATH)
async def github_mcp(request: Request):
    require_transport(request, namespace="github")
    if request.url.query or any(
        request.headers.get(name)
        for name in (
            "x-adp-user-authorization",
            "x-adp-run-credential",
            "x-adp-workload-token",
            "x-adp-assertion",
        )
    ):
        raise HTTPException(400, "ambiguous GitHub request")
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
                raise HTTPException(400, "invalid GitHub request")
            return await serve(packet, get_agent_runtime())
    except (ValueError, KeyError, TypeError, UnicodeError, OverflowError, httpx.HTTPError, TimeoutError, SQLAlchemyError, BotoCoreError, ClientError):
        raise HTTPException(503, "GitHub service unavailable") from None
