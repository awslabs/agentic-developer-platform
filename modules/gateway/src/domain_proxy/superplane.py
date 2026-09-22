"""Route inventoried user APIs to Superplane; authorization stays in its API.

The domain installer owns one conditional S3 registration. A short cache permits activation
and removal without a Gateway rollout. Missing/malformed registration is off.
Only namespace selection is configurable: no arbitrary upstream URL or headers.
"""

import asyncio
import json
import os
import re
import time
from pathlib import Path

import boto3
import httpx
from botocore.config import Config
from fastapi import APIRouter, HTTPException, Request, Response

router = APIRouter(prefix="/superplane", tags=["superplane"])
MAX_BODY = 2 * 1024 * 1024
_cache: tuple[float, dict] = (0, {})
ROUTES = tuple(
    (method, re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", path) + "$"))
    for method, path in json.loads(Path(__file__).with_name("superplane_routes.json").read_text())
)


def registration() -> dict:
    global _cache
    now = time.monotonic()
    if now < _cache[0]:
        return _cache[1]
    environment = os.environ.get("BG_ENVIRONMENT", "")
    result = {}
    bucket = os.environ.get("BG_SUPERPLANE_ROUTE_BUCKET", "")
    if re.fullmatch(r"[a-z][a-z0-9-]{0,39}", environment) and re.fullmatch(r"adp-terraform-state-[0-9]{12}", bucket):
        try:
            client = boto3.client("s3", config=Config(connect_timeout=1, read_timeout=1, retries={"max_attempts": 0}))
            response = client.get_object(Bucket=bucket, Key=f"domain-routes/{environment}/superplane/public-route.json")
            body = response["Body"]
            try:
                raw = body.read(4097)
            finally:
                body.close()
            value = json.loads(raw) if len(raw) <= 4096 else {}
            if (
                isinstance(value, dict)
                and value.get("version") == 2
                and value.get("enabled") is True
                and re.fullmatch(r"[0-9a-f]{24}", str(value.get("installation_id", "")))
                and re.fullmatch(r"[0-9a-f]{32}", str(value.get("revision", "")))
                and re.fullmatch(r"[a-z][a-z0-9-]{0,39}", str(value.get("namespace", "")))
                and value["namespace"] not in {"adp", "default", "kube-system", "kube-public"}
                and re.fullmatch(r"[0-9a-f]{64}", str(value.get("release_id", "")))
            ):
                result = value
        except Exception:
            # No remote exception text: it may carry credentials or a URL.
            result = {}
    _cache = (now + 5, result)
    return result


def enabled() -> bool:
    override = os.environ.get("FEATURE_SUPERPLANE_ENABLED")
    if override is not None and override.lower() != "true":
        return False
    return bool(registration())


@router.get("/installation-support")
async def installation_support():
    # Capability discovery does not expose a domain route or any configuration.
    return {
        "version": 2,
        "transport": "s3-conditional-domain-registration",
        "cache_seconds": 5,
        "configured": bool(
            re.fullmatch(r"adp-terraform-state-[0-9]{12}", os.environ.get("BG_SUPERPLANE_ROUTE_BUCKET", ""))
            and re.fullmatch(r"[a-z][a-z0-9-]{0,39}", os.environ.get("BG_ENVIRONMENT", ""))
        ),
    }


@router.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def forward(path: str, request: Request):
    raw_path = request.scope.get("raw_path", b"")
    native_path = "/" + path
    if (
        b"%" in raw_path
        or "\\" in path
        or any(part in {".", "..", ""} for part in path.split("/"))
        or not any(method == request.method and pattern.fullmatch(native_path) for method, pattern in ROUTES)
    ):
        raise HTTPException(404, "Not found")
    active = await asyncio.to_thread(enabled)
    config = await asyncio.to_thread(registration)
    if not active or not config:
        raise HTTPException(404, "Not found")
    authorization = request.headers.get("authorization", "")
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "ADP bearer token required")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY:
            raise HTTPException(413, "Request too large")
    target = f"http://superplane-api.{config['namespace']}.svc.cluster.local:8000{native_path}"
    headers = {"Authorization": authorization, "Content-Type": request.headers.get("content-type", "application/json")}
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            async with client.stream(
                request.method, target, params=request.query_params.multi_items(), headers=headers, content=bytes(body)
            ) as upstream:
                payload = bytearray()
                async for chunk in upstream.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > MAX_BODY:
                        raise HTTPException(502, "Domain response too large")
                if upstream.status_code in {301, 302, 303, 307, 308}:
                    raise HTTPException(502, "Unexpected domain redirect")
                # No upstream cookies, internal Location, identity or hop headers.
                return Response(
                    bytes(payload),
                    status_code=upstream.status_code,
                    headers={"Content-Type": upstream.headers.get("content-type", "application/json"), "X-Superplane-Release": config["release_id"]},
                )
    except httpx.HTTPError:
        raise HTTPException(503, "Superplane is unavailable") from None
