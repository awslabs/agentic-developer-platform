"""Relay Crew tools with the original, live pod and session proofs."""

import os
import re
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from src.agentauth.chat_capability import ChatAuthorizationRefusedError, Identifier
from src.agentauth.chat_data_routes import bearer, contract_errors, runtime
from src.agentauth.chat_model import _sandbox_launch
from src.agentauth.workload import WORKLOAD_HEADER

router = APIRouter()
FACADE_URL = "http://tools-facade.adp-gateway.svc/agent/tools"
UUID_PATH = r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}"
READ_PATH = re.compile(rf"/tools(?:/executions/{UUID_PATH}/operations/{UUID_PATH}(?:/artifacts/result)?)?\Z")
WRITE_PATH = re.compile(rf"/tools/(?:call|operations/recover|executions/{UUID_PATH}/close)\Z")
CREW_HEADERS = ("x-adp-chat-capability", "x-adp-chat-run-id", "x-adp-chat-session-id", "x-agent-runid")


class ToolsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: Identifier
    session_id: Identifier
    method: Literal["GET", "POST"]
    path: str = Field(min_length=1, max_length=2048)
    body: dict | None = None


async def verify_crew(request, token, run_id, session_id, services):
    _, launch, _, crew = await _sandbox_launch(
        request,
        token,
        services,
        run_id,
        session_id,
        operation="tools.invoke",
        crew_run=True,
    )
    if crew is None:
        raise ChatAuthorizationRefusedError("persistent Crew binding required")
    return launch, crew


async def authenticate_crew(request):
    if request.headers.get("x-adp-run-credential") or request.headers.get("x-adp-user-authorization"):
        raise ChatAuthorizationRefusedError("ambiguous tools identity")
    if any(len(request.headers.getlist(name)) != 1 for name in (*CREW_HEADERS, WORKLOAD_HEADER)):
        raise ChatAuthorizationRefusedError("Crew proofs required")
    return await verify_crew(
        request,
        request.headers[CREW_HEADERS[0]],
        request.headers[CREW_HEADERS[1]],
        request.headers[CREW_HEADERS[2]],
        runtime(),
    )


def facade_endpoint() -> str:
    """The tools façade base URL: the in-cluster service, or a validated HTTPS override."""
    endpoint = os.environ.get("ADP_TOOLS_GATEWAY_URL", FACADE_URL).rstrip("/")
    target = urlsplit(endpoint)
    if endpoint != FACADE_URL and (
        target.scheme != "https"
        or not target.hostname
        or target.username
        or target.password
        or target.query
        or target.fragment
        or target.path not in ("/agent/tools", "/api/agent/tools")
    ):
        raise HTTPException(503, "tools endpoint unavailable")
    return endpoint


@router.post("/v1/chat/data/tools")
@contract_errors
async def tools_request(body: ToolsRequest, request: Request, services=Depends(runtime)):
    token = bearer(request)
    await verify_crew(request, token, body.run_id, body.session_id, services)
    parsed = urlsplit(body.path)
    pattern = READ_PATH if body.method == "GET" else WRITE_PATH
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or not pattern.fullmatch(parsed.path)
        or "%" in parsed.path
        or (body.method == "GET" and body.body is not None)
    ):
        raise HTTPException(400, "invalid tools route")
    endpoint = facade_endpoint()
    headers = {
        CREW_HEADERS[0]: token,
        CREW_HEADERS[1]: body.run_id,
        CREW_HEADERS[2]: body.session_id,
        "x-agent-runid": request.headers["x-agent-runid"],
        WORKLOAD_HEADER: request.headers[WORKLOAD_HEADER],
    }
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=25) as client:
            response = await client.request(
                body.method, endpoint + body.path.removeprefix("/tools"), headers=headers, **({"json": body.body} if body.method == "POST" else {})
            )
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return JSONResponse({"status": 502, "body": {"error": "tools response unavailable; recover before retrying"}})
    return JSONResponse({"status": response.status_code, "body": payload}, headers={"Cache-Control": "no-store"})
