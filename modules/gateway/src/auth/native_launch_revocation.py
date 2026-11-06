"""Expire native-browser launch access when a human logs out of ADP."""

import hashlib
import math
from datetime import UTC, datetime
from functools import lru_cache

import jwt
from fastapi import HTTPException

from src.auth.tenant_context import split_token
from src.ratelimit.config import get_ratelimit_config
from src.shared.redis_client import create_redis_client


@lru_cache(maxsize=1)
def _redis(url):
    return create_redis_client(url, socket_timeout=2, socket_connect_timeout=2)


def _key(authorization: str) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, detail={"code": "session_required"})
    base_token, _ = split_token(authorization[7:])
    if not base_token:
        raise HTTPException(401, detail={"code": "session_required"})
    return "l5:devops:logged_out:" + hashlib.sha256(base_token.encode()).hexdigest()


def _client():
    config = get_ratelimit_config()
    if config.backend_type != "redis" or not config.redis_url:
        raise HTTPException(503, detail={"code": "session_check_unavailable"})
    return _redis(config.redis_url)


async def revoke_native_launch(authorization: str, expires_at: datetime):
    base_token, _ = split_token(authorization.removeprefix("Bearer "))
    if expires_at.astimezone(UTC) <= datetime.now(UTC):
        raise HTTPException(401, detail={"code": "session_required"})
    try:
        token_expiry = jwt.decode(base_token, options={"verify_signature": False, "verify_exp": False})["exp"]
        if not isinstance(token_expiry, int | float) or token_expiry < expires_at.timestamp():
            raise ValueError("invalid token expiry")
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        raise HTTPException(401, detail={"code": "session_required"}) from None
    remaining = math.ceil(token_expiry - datetime.now(UTC).timestamp())
    if remaining <= 0:
        raise HTTPException(401, detail={"code": "session_required"})
    try:
        saved = await _client().set(_key(authorization), "1", ex=remaining)
        if not saved:
            raise ValueError("revocation not persisted")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, detail={"code": "session_check_unavailable"}) from None


async def require_native_launch_session(authorization: str):
    await require_native_launch_session_digest(_key(authorization).removeprefix("l5:devops:logged_out:"))


async def require_native_launch_session_digest(fingerprint: str):
    """Recheck an already authenticated, server-retained session fingerprint."""
    if len(fingerprint) != 64 or any(value not in "0123456789abcdef" for value in fingerprint):
        raise HTTPException(401, detail={"code": "session_required"})
    try:
        revoked = await _client().get("l5:devops:logged_out:" + fingerprint)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, detail={"code": "session_check_unavailable"}) from None
    if revoked:
        raise HTTPException(401, detail={"code": "session_required"})
