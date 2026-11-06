"""Gateway-issued service authority for one persisted handoff observation."""

import hashlib
import hmac
from datetime import UTC, datetime

import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.products.models import ProductOperation
from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.models.base import utcnow
from src.shared.schemas.auth import TokenContext

PRODUCER = "gateway-handoff-reconciliation"
ISSUER = "adp-gateway/product-callback"
AUDIENCE = "product-callback"
TOKEN_LIFETIME_SECONDS = 60


def signing_key() -> bytes:
    secret = get_settings().token_secret_key
    if not secret:
        raise HTTPException(503, detail={"code": "callback_signing_unavailable"})
    return hmac.digest(secret.encode(), b"adp-product-callback-v1", hashlib.sha256)


def operation_binding(operation: ProductOperation) -> dict:
    return {
        "sub": operation.request.get("callback_producer", PRODUCER),
        "scope": operation.request["callback_scope"],
        "operation_id": operation.id,
        "tenant_id": operation.tenant_id,
        "product_id": operation.product_id,
        "project_id": operation.project_id,
        "session_id": operation.session_id,
        "actor_id": operation.actor_id,
        "profile_revision": operation.expected_revision,
        "generation": operation.request["generation"],
    }


def mint_callback_token(operation: ProductOperation) -> str:
    binding = operation_binding(operation)
    if operation.operation != "startHandoff" or binding["sub"] != PRODUCER:
        raise HTTPException(403, detail={"code": "callback_audience_denied"})
    now = int(utcnow().timestamp())
    deadline = operation.expires_at
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    if operation.state in ("cancelled", "expired") or deadline.timestamp() <= now:
        raise HTTPException(410, detail={"code": "operation_expired"})
    return jwt.encode(
        {**binding, "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": min(now + TOKEN_LIFETIME_SECONDS, int(deadline.timestamp()))},
        signing_key(),
        algorithm="HS256",
    )


def verify_callback_token(token: str, operation: ProductOperation) -> TokenContext:
    try:
        claims = jwt.decode(
            token,
            signing_key(),
            algorithms=["HS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"strict_aud": True, "require": ["exp", "iat", "iss", "aud", *operation_binding(operation)]},
        )
        if (
            claims["sub"] != PRODUCER
            or type(claims["iat"]) is not int
            or type(claims["exp"]) is not int
            or not 0 < claims["exp"] - claims["iat"] <= TOKEN_LIFETIME_SECONDS
            or any(claims[key] != value for key, value in operation_binding(operation).items())
        ):
            raise ValueError("callback binding mismatch")
    except (jwt.PyJWTError, ValueError, TypeError):
        raise HTTPException(403, detail={"code": "callback_audience_denied"}) from None
    return TokenContext(
        user_id=claims["sub"],
        org_id=claims["tenant_id"],
        team_id="",
        department_id="",
        account_type="service",
        auth_source="jwt",
        token_use="access",
        token_scopes=claims["scope"],
        expires_at=datetime.fromtimestamp(claims["exp"], UTC),
    )


async def get_callback_user(
    operation_id: str,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> TokenContext:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(403, detail={"code": "callback_audience_denied"})
    token = authorization[7:]
    try:
        claims = jwt.decode(
            token,
            signing_key(),
            algorithms=["HS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"strict_aud": True},
        )
    except jwt.PyJWTError:
        raise HTTPException(403, detail={"code": "callback_audience_denied"}) from None
    operation = await db.scalar(
        select(ProductOperation)
        .where(
            ProductOperation.tenant_id == claims.get("tenant_id"),
            ProductOperation.id == operation_id,
        )
        .with_for_update()
    )
    if operation is None or operation.operation != "startHandoff":
        raise HTTPException(403, detail={"code": "callback_audience_denied"})
    return verify_callback_token(token, operation)
