"""Verify request-bound gateway assertions; ignore caller-supplied ACL identity."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import config

log = logging.getLogger(__name__)
IDENTITY_HEADERS = frozenset(
    {
        "x-github-login",
        "x-github-teams",
        "x-tenant-id",
        "x-owner-sub",
        "x-adp-run-service",
        "x-internal-api-key",
        "x-adp-door-identity",
    }
)
MAX_BODY = 1024 * 1024


def _decode(value: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid encoding")
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def verify_identity(
    token: str, keys: dict, *, method: str, path: str, body: bytes, now=None
) -> dict:
    """Verify fixed protocol, pinned key, short expiry and exact request binding."""
    if not token or len(token) > 8192:
        raise ValueError("invalid assertion")
    version, encoded, signature = token.split(".")
    if version != "adpd1":
        raise ValueError("invalid version")
    payload = json.loads(_decode(encoded))
    strings = (
        "iss",
        "aud",
        "kid",
        "sub",
        "tenant_id",
        "github_login",
        "owner_sub",
        "method",
        "path",
        "body_sha256",
    )
    if not isinstance(payload, dict) or any(not isinstance(payload.get(k), str) for k in strings):
        raise ValueError("invalid claims")
    if payload["iss"] != "adp-gateway" or payload["aud"] != "adp-knowledge-door":
        raise ValueError("wrong issuer or audience")
    key = serialization.load_pem_public_key(keys[payload["kid"]].encode())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("invalid key type")
    key.verify(_decode(signature), (version + "." + encoded).encode())
    now = time.time() if now is None else now
    if (
        type(payload.get("iat")) is not int
        or type(payload.get("exp")) is not int
        or not 0 < payload["exp"] - payload["iat"] <= 30
        or payload["iat"] > now + 5
        or payload["exp"] <= now
    ):
        raise ValueError("invalid validity window")
    if (
        not payload["sub"]
        or not payload["tenant_id"]
        or not (payload["github_login"] or payload["owner_sub"])
        or payload["method"] != method
        or payload["path"] != path
        or payload["body_sha256"] != hashlib.sha256(body).hexdigest()
    ):
        raise ValueError("invalid binding")
    if payload["github_login"] and not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,38}", payload["github_login"]
    ):
        raise ValueError("invalid login")
    if payload["owner_sub"] and not re.fullmatch(
        r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", payload["owner_sub"]
    ):
        raise ValueError("invalid owner")
    return payload


def _is_public_path(path: str) -> bool:
    return path.rstrip("/") in {"/health", "/ready"}


async def check_request_auth(request: Request):
    if _is_public_path(request.url.path):
        return None
    try:
        keys = json.loads(config.door_verification_keys)
        if not isinstance(keys, dict) or not keys:
            raise ValueError("keys missing")
    except (ValueError, TypeError):
        return JSONResponse({"error": "not_configured"}, status_code=503)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            return JSONResponse({"error": "request_too_large"}, status_code=413)
        chunks.append(chunk)
    body = b"".join(chunks)
    request._body = body  # Starlette replays the bounded body to the mounted MCP app.
    try:
        if request.url.query:
            raise ValueError("unsigned query")
        claims = verify_identity(
            request.headers.get("x-adp-door-identity", ""),
            keys,
            method=request.method,
            path=request.url.path,
            body=body,
        )
    except (ValueError, TypeError, KeyError, InvalidSignature):
        log.warning("Door rejected unverified request identity")
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    # REST and mounted MCP both read the same sanitized ASGI scope. Never merge
    # arbitrary identity headers with verified claims, even when a signature is valid.
    headers = [
        (k, v) for k, v in request.scope["headers"] if k.decode().lower() not in IDENTITY_HEADERS
    ]
    headers.extend(
        (k.encode(), v.encode())
        for k, v in {
            "x-github-login": claims["github_login"],
            "x-tenant-id": claims["tenant_id"],
            "x-owner-sub": claims["owner_sub"],
            "x-adp-run-service": "true",
        }.items()
    )
    request.scope["headers"] = headers
    if hasattr(request, "_headers"):
        del request._headers
    request.state.door_principal = claims["sub"]
    log.info("Door authenticated principal=%s tenant=%s", claims["sub"], claims["tenant_id"])
    return None
