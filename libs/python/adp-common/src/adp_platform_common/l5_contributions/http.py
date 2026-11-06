"""Private receiver routes; a fresh gateway decision fences every durable operation."""

import json
import os
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse

from .assertions import HEADER, verify
from .contract import DESTINATIONS, MAX_BYTES, SUFFIXES
from .engine import Conflict, Consumer, Contended, Unavailable


def paths(consumer):
    return {"/internal/v1/" + consumer + "/" + suffix for kind, suffix in SUFFIXES.items()
            if consumer == "context" or kind != "security_result"}


def is_receiver_path(path, consumer):
    return any(path == base or path.startswith(base + "/") for base in paths(consumer))


async def authenticate(request, *, destination, keys):
    if request.url.query or len(request.headers.getlist(HEADER)) != 1:
        raise HTTPException(401, detail={"code": "unauthorized"})
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BYTES:
            raise HTTPException(413, detail={"code": "request_too_large"})
    request._body = bytes(body)
    try:
        return verify(request.headers[HEADER], keys, destination=destination,
                      method=request.method, path=request.url.path, body=bytes(body))
    except (ValueError, KeyError, TypeError, InvalidSignature):
        raise HTTPException(401, detail={"code": "unauthorized"}) from None


class GatewayAuthority:
    def __init__(self, token, gateway_url=None):
        self.token = token
        self.gateway_url = gateway_url or os.environ.get("L5_AUTHORITY_GATEWAY_URL") or os.environ.get("GATEWAY_CALLBACK_URL", "")
        self.gateway_url = self.gateway_url.removesuffix("/internal/v1/knowledge-assets/status-callback").rstrip("/")
        parsed = urlsplit(self.gateway_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path):
            raise ValueError("L5 current authority endpoint is not installed")

    async def __call__(self, claims):
        # No positive authorization cache. Callback returns no source or result data.
        async with httpx.AsyncClient(base_url=self.gateway_url, trust_env=False,
                                     follow_redirects=False, timeout=10) as client:
            response = await client.post("/internal/v1/l5/contribution-authority",
                                         json={"assertion": self.token})
        return response.status_code == 200 and response.json() == {"authorized": True}


def receiver_router(*, consumer, store_factory, keys_factory, authority_factory=GatewayAuthority):
    router = APIRouter()
    destination = DESTINATIONS[consumer]

    async def handle(request: Request):
        claims = await authenticate(request, destination=destination, keys=keys_factory())
        if not is_receiver_path(request.url.path, consumer):
            raise HTTPException(404, detail={"code": "not_found"})
        base = "/internal/v1/" + consumer + "/" + SUFFIXES[claims["kind"]]
        path = request.url.path
        own = base + "/" + claims["contribution_id"]
        engine = Consumer(store_factory(), authority_factory(request.headers[HEADER]))
        payload = json.loads(await request.body()) if request.method == "POST" else None
        try:
            if request.method == "POST" and path == base and claims.get("action") == "admit":
                result = await engine.admit(payload, claims)
                if result is None:
                    return JSONResponse({"status": "admission_pending"}, status_code=202,
                                        headers={"Cache-Control": "no-store"})
            elif request.method == "GET" and path in {own, own + "/projection"} and claims.get("action") == "read":
                result = await engine.read(claims, projection=path.endswith("/projection"))
            elif request.method == "POST" and path == own + "/invalidate" and claims.get("action") == "invalidate":
                result = await engine.invalidate(payload, claims)
            elif path == own + "/tasks/" + claims.get("task_id", "") and claims.get("action") == "use":
                result = await engine.use(payload if payload is not None else claims["usage"], claims,
                                          read_only=request.method == "GET")
            else:
                raise Unavailable()
        except Unavailable:
            raise HTTPException(404, detail={"code": "not_found"}) from None
        except Conflict:
            raise HTTPException(409, detail={"code": "contribution_conflict"}) from None
        except Contended:
            raise HTTPException(503, detail={"code": "contribution_busy", "retryable": True}) from None
        except (ValueError, TypeError, KeyError):
            raise HTTPException(422, detail={"code": "invalid_contribution"}) from None
        return JSONResponse(result, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    for base in sorted(paths(consumer)):
        router.add_api_route(base, handle, methods=["POST"], include_in_schema=False)
        router.add_api_route(base + "/{contribution_id}", handle, methods=["GET"], include_in_schema=False)
        router.add_api_route(base + "/{contribution_id}/projection", handle, methods=["GET"], include_in_schema=False)
        router.add_api_route(base + "/{contribution_id}/invalidate", handle, methods=["POST"], include_in_schema=False)
        router.add_api_route(base + "/{contribution_id}/tasks/{task_id}", handle, methods=["GET", "POST"], include_in_schema=False)
    return router
