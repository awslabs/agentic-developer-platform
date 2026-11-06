"""Private Slack user read using the current owned user-token binding."""

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
from src.internal.mcp_params import operation_params
from src.internal.slack_oauth import selected_slack_connection
from src.internal.slack_poster import approved_post_fixture, configured_post_fixture, fixture_text, post_slack_fixture
from src.internal.slack_reader import SlackIdentityObservation, read_slack_user
from src.internal.tools_dynamic_catalogue import canonical, publish_catalogue
from src.internal.tools_human_authority import current_tools_human
from src.internal.tools_knowledge import check_fresh, check_policy, current_binding, require_transport, verify_facade
from src.internal.tools_legacy_fence import require_legacy_generation
from src.shared.database import get_session_factory
from src.shared.models.vault import OAuthToolReceipt

PATH = "/internal/v1/tools/slack/mcp"
router = APIRouter(tags=["tools-slack"])
TOOLS = [
    {
        "name": "get_user",
        "description": "Read one Slack user visible to this verified user connection.",
        "inputSchema": {"type": "object", "properties": {"user": {"type": "string"}}, "required": ["user"], "additionalProperties": False},
    },
    {
        "name": "post_message",
        "description": "Post exactly one approved, run-marked Slack test-channel fixture message.",
        "inputSchema": {
            "type": "object",
            "properties": {"channel": {"type": "string"}, "text": {"type": "string"}, "run_marker": {"type": "string"}},
            "required": ["channel", "text", "run_marker"],
            "additionalProperties": False,
        },
    },
]


@asynccontextmanager
async def current_authority(claims, runtime, operation="get_user"):
    async with get_session_factory()() as db:
        await check_policy(claims, operation, db, runtime, namespace="slack")
        await require_legacy_generation(db, claims)
        user = await current_tools_human(db, claims["principal"])
        credential, binding, _ = await selected_slack_connection(db, user_id=user.id, tenant_id=user.org_id, expected=claims["slack_connection"])
        check_fresh(claims)
        yield credential, binding, user


async def claim_receipt(claims, *, operation="get_user", run_marker=None):
    payload = {"principal": claims["principal"], "receipt": claims["receipt_id"], "tool": "slack___" + operation}
    digest = hashlib.sha256(canonical(payload)).hexdigest()
    try:
        async with get_session_factory()() as db, db.begin():
            expiry = datetime.fromtimestamp(claims["expires_at"], UTC)
            db.add(OAuthToolReceipt(id=digest, expires_at=expiry))
            if operation == "post_message":
                marker_digest = hashlib.sha256(
                    canonical(
                        {
                            "tool": "slack___post_message",
                            "owner_id": claims["principal"]["user_id"],
                            "run_marker": run_marker,
                        }
                    )
                ).hexdigest()
                db.add(OAuthToolReceipt(id=marker_digest, expires_at=datetime.max.replace(tzinfo=UTC)))
            await db.flush()
    except IntegrityError:
        raise HTTPException(409, "Slack receipt already used") from None


async def serve(packet, runtime):
    method, request_id, params = packet["method"], packet.get("id"), operation_params(packet)
    deployment = await asyncio.to_thread(current_binding, metadata_only=method != "tools/call", namespace="slack")
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
                    "serverInfo": {"name": "adp-private-slack", "version": "1.0"},
                },
            }
        )
    if method == "notifications/initialized" and "id" not in packet and params == {}:
        return Response(status_code=202)
    if method == "ping" and params == {}:
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {}})
    if method == "tools/list" and params == {}:
        enabled = False
        if deployment["target_status"] == "READY" and os.environ.get("ADP_TOOLS_SLACK_TARGET_ID"):
            try:
                configured_post_fixture()
                enabled = True
            except HTTPException as exc:
                if exc.status_code != 403:
                    raise
            await asyncio.to_thread(
                publish_catalogue,
                binding=deployment,
                tools=TOOLS if enabled else TOOLS[:1],
                client=runtime.store.client,
                table_name=runtime.store.table,
            )
        schemas = [
            {**tool, "inputSchema": {**tool["inputSchema"], "properties": {**tool["inputSchema"]["properties"], "adp_assertion": {"type": "string"}}}}
            for tool in (TOOLS if enabled else TOOLS[:1])
        ]
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {"tools": schemas}}, headers={"Cache-Control": "no-store"})
    if (
        method != "tools/call"
        or not isinstance(params, dict)
        or set(params) != {"name", "arguments"}
        or params["name"] not in {"get_user", "post_message"}
        or not isinstance(params["arguments"], dict)
        or set(params["arguments"])
        != ({"user", "adp_assertion"} if params["name"] == "get_user" else {"channel", "text", "run_marker", "adp_assertion"})
    ):
        raise HTTPException(400, "unsupported Slack operation")
    operation = params["name"]
    arguments = {key: value for key, value in params["arguments"].items() if key != "adp_assertion"}
    claims = await verify_facade(params["arguments"]["adp_assertion"], operation, arguments, request_id, namespace="slack")
    if not isinstance(claims.get("slack_connection"), dict) or claims["slack_connection"].get("connection_id") != claims.get("connection_id"):
        raise HTTPException(403, "Slack connection binding required")

    def authority():
        if operation == "post_message":
            return current_authority(claims, runtime, operation=operation)
        return current_authority(claims, runtime)

    async with authority() as (credential, binding, owner):
        if operation == "post_message":
            fixture = approved_post_fixture(owner_id=owner.id, team_id=binding.provider_account_id.split(":", 1)[0])
            if (
                arguments["channel"] != fixture.channel_id
                or arguments["run_marker"] != fixture.run_marker
                or arguments["text"] != fixture_text(fixture, arguments["run_marker"])
            ):
                raise HTTPException(403, "Slack fixture denied")
            if "chat:write" not in credential.scopes["oauth_scopes"] or "chat:write" not in binding.observed_scopes:
                raise HTTPException(403, "Slack write scope missing")
            await claim_receipt(claims, operation=operation, run_marker=arguments["run_marker"])
        else:
            await claim_receipt(claims)
        check_fresh(claims)
        token = await _identity_access_token(credential, owner, get_identity_client())
        identity = SlackIdentityObservation(*binding.provider_account_id.split(":", 1)[::-1], tuple(binding.observed_scopes))
    async with authority():
        if operation == "post_message":
            profile = await post_slack_fixture(
                token,
                protected_identity=identity,
                fixture=fixture,
                owner_id=owner.id,
                channel_id=arguments["channel"],
                run_marker=arguments["run_marker"],
                text=arguments["text"],
            )
        else:
            profile = await read_slack_user(token, protected_identity=identity, user_id=arguments["user"])
    async with authority():
        if await asyncio.to_thread(current_binding, namespace="slack") != deployment:
            raise HTTPException(503, "Slack deployment changed")
        check_fresh(claims)
        return JSONResponse(
            {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": json.dumps(profile)}]}},
            headers={"Cache-Control": "no-store"},
        )


@router.post(PATH)
async def slack_mcp(request: Request):
    require_transport(request, namespace="slack")
    if request.url.query or any(
        request.headers.get(name)
        for name in (
            "x-adp-user-authorization",
            "x-adp-run-credential",
            "x-adp-workload-token",
            "x-adp-assertion",
        )
    ):
        raise HTTPException(400, "ambiguous Slack request")
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
                raise HTTPException(400, "invalid Slack request")
            return await serve(packet, get_agent_runtime())
    except (ValueError, KeyError, TypeError, UnicodeError, OverflowError, httpx.HTTPError, TimeoutError, SQLAlchemyError, BotoCoreError, ClientError):
        raise HTTPException(503, "Slack service unavailable") from None
