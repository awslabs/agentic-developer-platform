"""Fixed-path Knowledge Door bridge; shared keys stay in the gateway (#5195)."""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import Response

from src.agentauth.grants import AUTHORITY_SERVICE_POLICY
from src.agentauth.routes import AgentRuntime, get_agent_runtime, require_agent_transport
from src.agentauth.run_services import live_context
from src.shared.database import get_session_factory
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from src.shared.models.vault import UserIdentity

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
SERVICE_TIMEOUT_SECONDS = 30
_PATHS = {("POST", "call"): "/call", ("POST", "mcp/"): "/mcp/", ("GET", "tools"): "/tools"}
router = APIRouter(prefix="/internal/v1/agent/self", tags=["agent-authority"], dependencies=[Depends(require_agent_transport)])


@asynccontextmanager
async def locked_door_identity(record, grant):
    """Read current tenant membership and linked identity, locking through use.

    These shared locks prevent membership removal or identity reassignment while
    the authenticated request is forwarded. Worker headers/env are never inputs.
    A service root has no human identity and cannot impersonate one at the Door.
    """
    if (
        grant.authority.kind == AUTHORITY_SERVICE_POLICY
        or grant.authority.org_id != record.tenant_id
        or grant.tenant_id != record.tenant_id
        or grant.principal != record.principal
    ):
        raise HTTPException(404, "not found")
    async with get_session_factory()() as db, db.begin():
        user = (
            await db.execute(
                select(User.cognito_sub)
                .join(TenantMembership, TenantMembership.user_id == User.id)
                .where(
                    User.id == grant.authority.human_id,
                    User.org_id == record.tenant_id,
                    User.user_kind == "human",
                    User.is_shadow.is_(False),
                    TenantMembership.tenant_id == record.tenant_id,
                    TenantMembership.is_active.is_(True),
                )
                .with_for_update(read=True, of=(User, TenantMembership))
            )
        ).one_or_none()
        if user is None:
            raise HTTPException(404, "not found")
        linked = (
            await db.execute(
                select(UserIdentity.provider_username)
                .where(UserIdentity.user_id == grant.authority.human_id, UserIdentity.org_id == record.tenant_id, UserIdentity.provider == "github")
                .order_by(UserIdentity.is_primary.desc(), UserIdentity.provider_user_id.asc())
                .limit(1)
                .with_for_update(read=True)
            )
        ).one_or_none()
        headers = {
            "x-tenant-id": record.tenant_id,
            # Forces tenant scoping even when the legacy Door flag is disabled.
            "x-adp-run-service": "true",
        }
        if linked is not None:
            if not isinstance(linked.provider_username, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", linked.provider_username):
                raise HTTPException(404, "not found")
            headers["x-github-login"] = linked.provider_username.lower()
        if user.cognito_sub:
            if not re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", user.cognito_sub):
                raise HTTPException(404, "not found")
            headers["x-owner-sub"] = user.cognito_sub.lower()
        # Platform teams are not GitHub teams. Do not fabricate GitHub ACL grants.
        yield headers


async def bounded_body(request: Request) -> bytes:
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_REQUEST_BYTES:
            raise HTTPException(413, "knowledge request too large")
        chunks.append(chunk)
    return b"".join(chunks)


def door_config(runtime: AgentRuntime) -> tuple[str, str]:
    import os

    env = os.environ if runtime.env is None else runtime.env
    base = env.get("ADP_DOOR_SERVICE_URL", "").rstrip("/")
    key = env.get("ADP_DOOR_SERVICE_KEY", "")
    parsed = urlsplit(base)
    if (
        not key
        or not base
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path
    ):
        raise HTTPException(503, "knowledge service unavailable")
    return base, key


async def forward_door(method: str, url: str, headers: dict[str, str], body: bytes) -> Response:
    # No worker-selected host, path, auth, proxy, cookie, session id or redirects.
    async with httpx.AsyncClient(timeout=SERVICE_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False) as client:
        async with client.stream(method, url, headers=headers, content=body) as upstream:
            if not 200 <= upstream.status_code < 300:
                raise HTTPException(502, "knowledge service request failed")
            chunks = []
            size = 0
            async for chunk in upstream.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise HTTPException(502, "knowledge response too large")
                chunks.append(chunk)
            content_type = upstream.headers.get("content-type", "").split(";", 1)[0]
            if content_type not in {"application/json", "text/event-stream"} and upstream.status_code != 202:
                raise HTTPException(502, "knowledge response invalid")
            return Response(
                b"".join(chunks),
                status_code=upstream.status_code,
                media_type=content_type or None,
                headers={"Cache-Control": "no-store"},
            )


@router.api_route("/knowledge/{path:path}", methods=["GET", "POST"])
async def own_knowledge(path: str, request: Request, runtime: AgentRuntime = Depends(get_agent_runtime)) -> Response:
    target_path = _PATHS.get((request.method, path))
    if target_path is None or request.url.query:
        raise HTTPException(404, "not found")
    initial = await live_context(request, runtime)
    base, key = door_config(runtime)
    try:
        async with asyncio.timeout(SERVICE_TIMEOUT_SECONDS):
            body = await bounded_body(request)
            async with locked_door_identity(initial[2], initial[3]) as identity:
                current = await live_context(request, runtime)
                if initial[1:] != current[1:]:
                    raise HTTPException(404, "not found")
                headers = {
                    **identity,
                    "x-internal-api-key": key,
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                }
                response = await forward_door(request.method, base + target_path, headers, body)
                # Do not release private data to a revoked/expired run after a slow query.
                final = await live_context(request, runtime)
                if current[1:] != final[1:]:
                    raise HTTPException(404, "not found")
                return response
    except (httpx.HTTPError, TimeoutError, SQLAlchemyError):
        raise HTTPException(503, "knowledge service unavailable") from None
