"""Private TLS for recovery calls and operation-scoped bootstrap observations.

The API retains credential, scope, admission, and claim checks. This process has
no AWS or database client and does not start another application reconciler.
"""

import httpx
from uuid import UUID
from fastapi import FastAPI, HTTPException, Request, Response

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
ACTIONS = frozenset(
    {"inventory", "observe", "lifecycle", "account-creation", "bootstrap", "settlement"}
)
LIMIT = 262144


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/internal/controller/recovery/{action}")
async def forward(action: str, request: Request):
    if action not in ACTIONS or request.url.query:
        raise HTTPException(404)
    return await relay(request, "POST", "/internal/controller/recovery/" + action)


@app.get("/api/v1/workspaces/{workspace_id}/bootstrap-observation")
async def bootstrap_observation(workspace_id: UUID, request: Request):
    if request.url.query:
        raise HTTPException(404)
    return await relay(
        request, "GET", f"/api/v1/workspaces/{workspace_id}/bootstrap-observation"
    )


async def relay(request: Request, method: str, path: str):
    authorization = request.headers.get("authorization", "")
    if not authorization or len(authorization) > 4096:
        raise HTTPException(401)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > LIMIT:
            raise HTTPException(413)
    try:
        async with httpx.AsyncClient(
            timeout=25, follow_redirects=False, trust_env=False
        ) as client:
            async with client.stream(
                method,
                "http://127.0.0.1:8000" + path,
                headers={
                    "Authorization": authorization,
                    "Content-Type": "application/json",
                },
                content=bytes(body),
            ) as upstream:
                if upstream.status_code not in {
                    200,
                    400,
                    401,
                    403,
                    404,
                    409,
                    422,
                    429,
                    503,
                }:
                    raise HTTPException(503, "recovery observation unavailable")
                result = bytearray()
                async for chunk in upstream.aiter_bytes():
                    result.extend(chunk)
                    if len(result) > LIMIT:
                        raise HTTPException(503, "recovery observation unavailable")
                return Response(
                    bytes(result),
                    status_code=upstream.status_code,
                    media_type="application/json",
                )
    except httpx.HTTPError:
        raise HTTPException(503, "recovery observation unavailable") from None
