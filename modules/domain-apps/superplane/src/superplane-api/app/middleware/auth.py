"""JWT authentication middleware — extracts and validates JWT from Authorization header."""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.config import settings
from app.schemas.auth import TokenPayload

logger = logging.getLogger(__name__)

security = HTTPBearer()

# auto_error=False so a missing credential reaches the dependency body, which
# decides between the verified-caller path and the legacy decoder before
# answering. With auto_error=True, HTTPBearer would 403 on the way in and a
# request already admitted by the domain guard could never reach its handler.
optional_security = HTTPBearer(auto_error=False)


class JWTSecretKeyMissing(RuntimeError):
    """No signing key was supplied for the legacy org-scoped token path.

    Issue #5683 (A04). Raised rather than defaulted: `app/config.py` used to ship a
    hardcoded placeholder key, so a deployment that never set JWT_SECRET_KEY signed
    and accepted tokens under a value committed to this repository. Both halves
    matter — an attacker who knows the key can forge a token, and the server
    verifying with it cannot tell a forged token from a real one.

    The removed value is deliberately not quoted here. Any environment still running
    on it remains forgeable until it is rotated, so restating it in a shipping file
    would re-publish a live credential to make a historical point.
    """


def require_jwt_secret_key() -> str:
    """The configured signing key, or refuse.

    Every sign and verify call routes through here, which is what makes the check
    unavoidable: a future code path that reads `settings.jwt_secret_key` directly
    would reintroduce the fallback, so there is exactly one reader.

    An empty key is treated as "unset" rather than as a key, because
    `jose.jwt.encode` will sign with an empty string — see the note in
    `app/config.py` for why that makes an empty default no safer than the
    placeholder it replaced.

    The message names the variable to set and never includes the value, so a
    startup failure in a shared log does not become the credential disclosure the
    check exists to prevent.
    """
    key = settings.jwt_secret_key
    if not key or not key.strip():
        raise JWTSecretKeyMissing(
            "JWT_SECRET_KEY is not set. The org-scoped token path cannot sign or "
            "verify without it, and this deployment must supply it by reference "
            "from its secret store. Refusing rather than using a built-in default: "
            "a committed key is forgeable by anyone who can read the source."
        )
    return key


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
        payload, require_jwt_secret_key(), algorithm=settings.jwt_algorithm
    )
    return token, expires_in


def decode_token(token: str) -> TokenPayload:
    """Decode and validate a JWT token.

    Raises:
        HTTPException 401 if the token is invalid or expired.
        JWTSecretKeyMissing if no signing key is configured.
    """
    # Resolved BEFORE the try, deliberately. Inside it, the missing-key refusal
    # would be indistinguishable from a bad token and answered with 401 — telling
    # an operator "invalid or expired token" when the real fault is that the
    # deployment has no signing key, which is the hardest possible way to diagnose
    # a total login outage. A configuration fault is not an authentication result.
    key = require_jwt_secret_key()
    try:
        payload = jwt.decode(token, key, algorithms=[settings.jwt_algorithm])
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


def _verified_org_id(request: Request) -> uuid.UUID | None:
    """The org of the caller the domain guard already admitted, if any.

    Issue #5055 (U14). ``app/domain_guard.py`` runs before any handler
    dependency and, when enforcement is on, has already verified the token's
    signature and admitted it under the strict policy. This reads that decision
    instead of re-deriving identity.

    Two paths deciding the same question is the bypass this story exists to
    close, so the order matters: when a verified caller exists, the legacy
    decoder is not consulted at all. It is not a fallback — a valid domain token
    is not HS256-signed and would fail the legacy decoder, and "try the other
    validator on failure" is exactly how a permissive path re-admits what the
    strict one refused.
    """
    caller = getattr(request.state, "caller", None)
    if caller is None:
        return None
    try:
        return uuid.UUID(caller.principal.org_id)
    except (ValueError, AttributeError, TypeError) as exc:
        # Verified, but not a usable identifier. A denial rather than a 500 from
        # the query layer.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="token organization claim is not a valid identifier",
        ) from exc


async def get_current_org(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(optional_security),
) -> uuid.UUID:
    """FastAPI dependency — the caller's organization.

    Usage in routers:
        @router.get("/workspaces")
        async def list_workspaces(org_id: uuid.UUID = Depends(get_current_org)):
            ...
    """
    verified = _verified_org_id(request)
    if verified is not None:
        return verified
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return decode_token(credentials.credentials).org_id


async def get_current_user_context(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(optional_security),
) -> dict:
    """FastAPI dependency — extracts full user context.

    Returns dict with: org_id, user_id, role.
    Used by RBAC middleware and user-aware endpoints.
    """
    caller = getattr(request.state, "caller", None)
    verified = _verified_org_id(request)
    if verified is not None:
        # The role is deliberately NOT taken from the token. Under enforcement,
        # authority is the server-held grant the guard already checked for this
        # operation; a role claim here would be a second, weaker source of
        # authority that no grant backs.
        return {
            "org_id": verified,
            "user_id": None,
            "role": caller.principal.account_type,
        }
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token_data = decode_token(credentials.credentials)
    return {
        "org_id": token_data.org_id,
        "user_id": token_data.user_id,
        "role": token_data.role or "developer",
    }
