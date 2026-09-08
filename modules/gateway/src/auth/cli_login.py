"""Web-based CLI login — device-authorization-style flow (no copy-paste).

`bg-cognito-auth.sh login --web` gives the CLI the `aws sso login` experience:

    CLI  ── POST /auth/cli/start ──────────► pending row (user_code + device_code)
    CLI  ── opens browser at /cli-auth?code=<user_code>
    SPA  ── POST /auth/cli/approve ────────► row approved (bound to the signed-in user)
    CLI  ── POST /auth/cli/token (poll) ───► tokens minted on the CLI app client

No credential is ever displayed to a human or passed through a clipboard —
this replaces the "Reveal refresh token and paste it" panel for browser users
(`import` remains the documented fallback for headless machines).

Why a separate Cognito app client: refresh-token validity is per-client. The
SPA client keeps its 30-day sessions; the CLI client is short-lived (default
24 h) with refresh-token rotation, so a stolen on-disk token dies on the next
background refresh.

How tokens are minted: the same mechanism the github-auth-broker uses on every
browser sign-in — AdminSetUserPassword with a fresh random password, then
AdminInitiateAuth (ADMIN_USER_PASSWORD_AUTH) — just aimed at the CLI client.
That reset is only safe for broker-provisioned users (username `GitHub_<id>`),
who never hold a real password; native-password users are refused here and
keep using `bg-cognito-auth.sh login`.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import string
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.cognito_jwt import CognitoTokenClaims, get_cognito_validator
from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.models.cli_auth import CliAuthRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/cli", tags=["authentication"])

# Request lifetime and the poll cadence handed to the CLI.
REQUEST_TTL_SECONDS = 600
POLL_INTERVAL_SECONDS = 3

# user_code alphabet: no 0/O/1/I so the code survives being read aloud or
# retyped. 8 chars over 31 symbols ≈ 8.5e11 — unguessable within the 10-minute
# window, and approval additionally requires an authenticated session.
_USER_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

# Broker-provisioned usernames (github-auth-broker cognito_provisioner.py).
_GITHUB_USERNAME_PREFIX = "GitHub_"

# Best-effort per-pod flood guard for the unauthenticated /start endpoint.
# Not a security boundary (multi-pod, in-memory) — it bounds accidental loops
# and lazy abuse; the DB rows it protects expire in minutes anyway.
_START_RATE_LIMIT = 10  # requests
_START_RATE_WINDOW_SECONDS = 60
_start_times: dict[str, list[float]] = {}
_start_lock = threading.Lock()


class StartResponse(BaseModel):
    user_code: str
    device_code: str
    verification_path: str
    expires_in: int
    interval: int


class ApproveRequest(BaseModel):
    user_code: str = Field(min_length=4, max_length=16)
    action: str = Field(pattern="^(approve|deny)$")


class TokenRequest(BaseModel):
    device_code: str = Field(min_length=16, max_length=256)


def _require_cli_client_configured() -> str:
    cli_client_id = get_settings().cognito_cli_client_id
    if not cli_client_id:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "cli_login_not_configured",
                "message": "Web CLI login is not enabled on this deployment (no CLI app client).",
            },
        )
    return cli_client_id


def _hash_device_code(device_code: str) -> str:
    return hashlib.sha256(device_code.encode("utf-8")).hexdigest()


def _new_user_code() -> str:
    chars = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(8))
    return f"{chars[:4]}-{chars[4:]}"


def _normalize_user_code(raw: str) -> str:
    cleaned = raw.strip().upper().replace("-", "")
    return f"{cleaned[:4]}-{cleaned[4:]}" if len(cleaned) == 8 else raw.strip().upper()


def _check_start_rate(client_ip: str) -> None:
    now = time.monotonic()
    with _start_lock:
        times = [t for t in _start_times.get(client_ip, []) if now - t < _START_RATE_WINDOW_SECONDS]
        if len(times) >= _START_RATE_LIMIT:
            raise HTTPException(
                status_code=429,
                detail={"error": "rate_limited", "message": "Too many CLI login attempts; wait a minute."},
            )
        times.append(now)
        _start_times[client_ip] = times
        # Bound the dict itself so a rotating-IP scan cannot grow it forever.
        if len(_start_times) > 10_000:
            _start_times.clear()


async def _get_cognito_claims(
    authorization: str = Header(None, alias="Authorization"),
) -> CognitoTokenClaims:
    """Validate the browser user's Cognito access token and return raw claims.

    The shared `get_current_user` dependency maps claims to a TokenContext
    that drops `username` — but approval must record the Cognito username
    (`GitHub_<id>`) because that is what token minting authenticates as.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"error": "missing_token", "message": "Authorization header required"})
    try:
        validator = get_cognito_validator()
        return validator.validate_token(authorization.removeprefix("Bearer ").strip())
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail={"error": "invalid_token", "message": "Token validation failed"})


class CliTokenMinter:
    """Mints Cognito tokens on the CLI app client for a broker-provisioned user.

    Same recipe as github-auth-broker's cognito_provisioner: set a fresh random
    permanent password, then ADMIN_USER_PASSWORD_AUTH. Deliberately NOT
    reimplemented as a custom-auth Lambda trigger — reusing the broker's
    mechanism keeps one minting story per pool.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._user_pool_id = settings.cognito_user_pool_id
        self._region = settings.aws_region
        self._client: Any = None

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = boto3.client("cognito-idp", region_name=self._region)
        return self._client

    @staticmethod
    def _random_password() -> str:
        # One from each required class + 28 random, shuffled — always satisfies
        # the pool policy regardless of how the random draw lands.
        pools = [string.ascii_uppercase, string.ascii_lowercase, string.digits, "!@#$%^&*"]
        chars = [secrets.choice(pool) for pool in pools]
        alphabet = "".join(pools)
        chars += [secrets.choice(alphabet) for _ in range(28)]
        secrets.SystemRandom().shuffle(chars)
        return "".join(chars)

    def mint(self, username: str, cli_client_id: str) -> dict[str, Any]:
        """Returns AuthenticationResult keys: AccessToken, IdToken, RefreshToken, ExpiresIn."""
        if not username.startswith(_GITHUB_USERNAME_PREFIX):
            # Defense in depth — approve already refused these. A password
            # reset would lock a native-password user out of `login`.
            raise ValueError("CLI web login mints only for broker-provisioned (GitHub_*) users")
        password = self._random_password()
        self.client.admin_set_user_password(
            UserPoolId=self._user_pool_id,
            Username=username,
            Password=password,
            Permanent=True,
        )
        response = self.client.admin_initiate_auth(
            UserPoolId=self._user_pool_id,
            ClientId=cli_client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
        return response["AuthenticationResult"]


def get_token_minter() -> CliTokenMinter:
    """DI seam so tests can stub Cognito."""
    return CliTokenMinter()


@router.post(
    "/start",
    response_model=StartResponse,
    summary="Begin a web CLI login (called by bg-cognito-auth.sh login --web)",
)
async def start_cli_login(request: Request, db: AsyncSession = Depends(get_db)) -> StartResponse:
    _require_cli_client_configured()
    client_ip = request.client.host if request.client else "unknown"
    _check_start_rate(client_ip)

    device_code = secrets.token_urlsafe(48)
    user_code = _new_user_code()
    now = datetime.now(UTC)

    row = CliAuthRequest(
        user_code=user_code,
        device_code_hash=_hash_device_code(device_code),
        status="pending",
        expires_at=now + timedelta(seconds=REQUEST_TTL_SECONDS),
    )
    db.add(row)
    await db.commit()

    logger.info("CLI login started request_id=%s", row.id)
    return StartResponse(
        user_code=user_code,
        device_code=device_code,
        verification_path=f"/cli-auth?code={user_code}",
        expires_in=REQUEST_TTL_SECONDS,
        interval=POLL_INTERVAL_SECONDS,
    )


@router.post(
    "/approve",
    summary="Approve or deny a pending CLI login (called by the dashboard)",
)
async def approve_cli_login(
    body: ApproveRequest,
    claims: CognitoTokenClaims = Depends(_get_cognito_claims),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_cli_client_configured()

    if body.action == "approve" and not claims.username.startswith(_GITHUB_USERNAME_PREFIX):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "password_login_required",
                "message": "Web CLI login is available for GitHub sign-ins. Your account has a "
                "Cognito password — use `bg-cognito-auth.sh login` instead.",
            },
        )

    user_code = _normalize_user_code(body.user_code)
    result = await db.execute(select(CliAuthRequest).where(CliAuthRequest.user_code == user_code))
    row = result.scalars().first()

    now = datetime.now(UTC)
    if row is None or row.expires_at.replace(tzinfo=UTC) < now:
        raise HTTPException(
            status_code=404,
            detail={"error": "unknown_code", "message": "No pending CLI login with that code — it may have expired. Re-run login --web."},
        )
    if row.status != "pending":
        raise HTTPException(
            status_code=409,
            detail={"error": "already_decided", "message": f"This CLI login was already {row.status}."},
        )

    row.status = "approved" if body.action == "approve" else "denied"
    row.approved_username = claims.username
    row.approved_sub = claims.sub
    row.updated_at = now
    await db.commit()

    logger.info("CLI login %s request_id=%s sub=%s", row.status, row.id, claims.sub)
    return {"status": row.status}


@router.post(
    "/token",
    summary="Redeem an approved CLI login for tokens (polled by the CLI)",
)
async def redeem_cli_login(
    body: TokenRequest,
    db: AsyncSession = Depends(get_db),
    minter: CliTokenMinter = Depends(get_token_minter),
) -> dict:
    settings = get_settings()
    cli_client_id = _require_cli_client_configured()

    code_hash = _hash_device_code(body.device_code)
    result = await db.execute(select(CliAuthRequest).where(CliAuthRequest.device_code_hash == code_hash))
    row = result.scalars().first()

    now = datetime.now(UTC)
    if row is None or row.status == "consumed" or row.expires_at.replace(tzinfo=UTC) < now:
        # One terminal answer for gone/expired/replayed — the CLI restarts the flow.
        raise HTTPException(status_code=410, detail={"error": "expired", "message": "Login request expired or already used. Re-run login --web."})
    if row.status == "denied":
        raise HTTPException(status_code=403, detail={"error": "access_denied", "message": "The login was denied in the browser."})
    if row.status == "pending":
        # 202: approval hasn't happened yet — the CLI keeps polling.
        raise HTTPException(status_code=202, detail={"error": "authorization_pending", "message": "Waiting for browser approval."})

    # Atomically claim the approved row so concurrent polls mint at most once.
    claimed = await db.execute(
        update(CliAuthRequest)
        .where(CliAuthRequest.id == row.id, CliAuthRequest.status == "approved")
        .values(status="consumed", updated_at=now)
        .returning(CliAuthRequest.id)
    )
    await db.commit()
    if claimed.scalar_one_or_none() is None:
        raise HTTPException(status_code=410, detail={"error": "expired", "message": "Login request expired or already used. Re-run login --web."})

    try:
        auth_result = minter.mint(row.approved_username or "", cli_client_id)
    except Exception:
        # Give the row back so the CLI's next poll can retry the mint.
        await db.execute(update(CliAuthRequest).where(CliAuthRequest.id == row.id).values(status="approved"))
        await db.commit()
        logger.exception("CLI token mint failed request_id=%s", row.id)
        raise HTTPException(status_code=502, detail={"error": "mint_failed", "message": "Could not mint tokens; the CLI will retry."})

    logger.info("CLI login redeemed request_id=%s sub=%s", row.id, row.approved_sub)
    return {
        "token_type": "Bearer",
        "access_token": auth_result["AccessToken"],
        "id_token": auth_result.get("IdToken", ""),
        "refresh_token": auth_result.get("RefreshToken", ""),
        "expires_in": auth_result.get("ExpiresIn", 3600),
        "client_id": cli_client_id,
        "user_pool_id": settings.cognito_user_pool_id,
        "region": settings.aws_region,
    }
