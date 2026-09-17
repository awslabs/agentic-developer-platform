"""JWT authentication middleware — extracts and validates JWT from Authorization header."""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.config import settings
from app.schemas.auth import TokenPayload

logger = logging.getLogger(__name__)

security = HTTPBearer()


def create_access_token(
    org_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    role: str | None = None,
) -> tuple[str, int]:
    """Create a JWT access token for the given org_id.

    Args:
        org_id: Organization UUID
        user_id: Optional user UUID (included when user context is available)
        role: Optional RBAC role (developer, workspace-admin, org-admin)

    Returns:
        Tuple of (token_string, expires_in_seconds).
    """
    expires_in = settings.jwt_expire_minutes * 60
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {
        "sub": str(org_id),
        "org_id": str(org_id),
        "exp": expire,
    }
    if user_id is not None:
        payload["user_id"] = str(user_id)
    if role is not None:
        payload["role"] = role
    token = jwt.encode(
        payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm
    )
    return token, expires_in


def decode_token(token: str) -> TokenPayload:
    """Decode and validate a JWT token.

    Raises:
        HTTPException 401 if the token is invalid or expired.
    """
    try:
        payload = jwt.decode(
            token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )
        return TokenPayload(
            sub=payload["sub"],
            org_id=uuid.UUID(payload["org_id"]),
            exp=payload["exp"],
            user_id=uuid.UUID(payload["user_id"]) if payload.get("user_id") else None,
            role=payload.get("role"),
        )
    except (JWTError, KeyError, ValueError) as exc:
        logger.warning("JWT decode failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def get_current_org(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> uuid.UUID:
    """FastAPI dependency — extracts org_id from a valid JWT Bearer token.

    Usage in routers:
        @router.get("/workspaces")
        async def list_workspaces(org_id: uuid.UUID = Depends(get_current_org)):
            ...
    """
    token_data = decode_token(credentials.credentials)
    return token_data.org_id


async def get_current_user_context(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """FastAPI dependency — extracts full user context from JWT.

    Returns dict with: org_id, user_id, role.
    Used by RBAC middleware and user-aware endpoints.
    """
    token_data = decode_token(credentials.credentials)
    return {
        "org_id": token_data.org_id,
        "user_id": token_data.user_id,
        "role": token_data.role or "developer",
    }
