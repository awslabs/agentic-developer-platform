"""Native Cognito CLI login without password resets or local AWS credentials.

Continuation tokens are signed, short lived, pool/client bound and consumed
atomically in the existing CLI auth table. That table also holds expiring rate
records; PostgreSQL advisory locks make the limits shared across gateway pods.
No password or Cognito session is persisted or logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta

import boto3
import jwt
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import require_admin
from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.models.cli_auth import CliAuthRequest
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/auth/cli", tags=["authentication"])
ISSUER = "adp-cli-native-login"
TTL = 300
SUPPORTED = {"NEW_PASSWORD_REQUIRED", "SMS_MFA", "SOFTWARE_TOKEN_MFA"}


def failure(code="authentication_failed", status=401):
    messages = {
        "authentication_failed": "Sign-in could not be completed. Check your credentials or restart adp admin login.",
        "rate_limited": "Too many sign-in attempts. Wait a minute and retry.",
        "cli_login_not_configured": "Native CLI login is unavailable. Contact the platform administrator.",
        "authentication_unavailable": "Sign-in is temporarily unavailable. Retry adp admin login.",
        "invalid_request": "Invalid sign-in request.",
    }
    return HTTPException(status, detail={"error": code, "message": messages[code]}, headers={"Cache-Control": "no-store"})


def settings_for_login():
    settings = get_settings()
    if not settings.cognito_cli_client_id or not settings.cognito_user_pool_id or not settings.token_secret_key:
        raise failure("cli_login_not_configured", 503)
    return settings


def get_native_client():
    return boto3.client(
        "cognito-idp", region_name=get_settings().aws_region, config=Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 0})
    )


async def payload(request: Request) -> dict:
    # Parse explicitly so FastAPI validation errors never echo a password,
    # MFA code or continuation back in their `input` field.
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 32768:
            raise failure("invalid_request", 400)
    try:
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, UnicodeError):
        raise failure("invalid_request", 400) from None


def string_field(body, key, maximum=4096):
    value = body.get(key)
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise failure("invalid_request", 400)
    return value


async def limit(db: AsyncSession, kind: str, value: str, maximum: int):
    now = datetime.now(UTC)
    digest = hashlib.sha256((kind + ":" + value.casefold()).encode()).hexdigest()
    if db.get_bind().dialect.name == "postgresql":
        lock = int.from_bytes(bytes.fromhex(digest[:16]), "big", signed=True)
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
    await db.execute(
        delete(CliAuthRequest).where(CliAuthRequest.status.in_(["native-rate", "native-challenge", "native-used"]), CliAuthRequest.expires_at < now)
    )
    count = await db.scalar(
        select(func.count())
        .select_from(CliAuthRequest)
        .where(CliAuthRequest.status == "native-rate", CliAuthRequest.approved_username == digest, CliAuthRequest.expires_at > now)
    )
    if count >= maximum:
        await db.rollback()
        raise failure("rate_limited", 429)
    db.add(
        CliAuthRequest(
            user_code="native-rate",
            device_code_hash=secrets.token_hex(32),
            status="native-rate",
            approved_username=digest,
            expires_at=now + timedelta(seconds=60),
        )
    )
    await db.commit()


async def call(client, operation, **kwargs):
    try:
        return await asyncio.to_thread(getattr(client, operation), **kwargs)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"TooManyRequestsException", "LimitExceededException"}:
            raise failure("rate_limited", 429) from None
        if code in {"InternalErrorException", "ServiceUnavailableException", "AccessDeniedException"}:
            raise failure("authentication_unavailable", 503) from None
        # UserNotFound, wrong password/MFA and expired challenges share one answer.
        raise failure() from None
    except BotoCoreError:
        raise failure("authentication_unavailable", 503) from None


async def response(result, username, settings, db):
    auth = result.get("AuthenticationResult")
    if auth:
        return JSONResponse(
            {
                "access_token": auth["AccessToken"],
                "id_token": auth.get("IdToken", ""),
                "refresh_token": auth["RefreshToken"],
                "expires_in": auth.get("ExpiresIn", 3600),
                "client_id": settings.cognito_cli_client_id,
                "user_pool_id": settings.cognito_user_pool_id,
                "region": settings.aws_region,
                "token_type": "Bearer",
            },
            headers={"Cache-Control": "no-store"},
        )
    challenge = result.get("ChallengeName", "")
    if challenge not in SUPPORTED:
        return JSONResponse(
            {
                "status": "pending",
                "challenge": "UNSUPPORTED",
                "next_action": "Complete account verification or MFA enrollment in the browser, then rerun adp admin login.",
            },
            headers={"Cache-Control": "no-store"},
        )
    parameters = result.get("ChallengeParameters", {})
    username = parameters.get("USER_ID_FOR_SRP") or parameters.get("USERNAME") or username
    required = json.loads(parameters.get("requiredAttributes", "[]")) if challenge == "NEW_PASSWORD_REQUIRED" else []
    # Cognito may return these names either with or without userAttributes.
    required = ["userAttributes." + name.removeprefix("userAttributes.") for name in required]
    now = datetime.now(UTC)
    nonce = secrets.token_urlsafe(32)
    db.add(
        CliAuthRequest(
            user_code="native-challenge",
            device_code_hash=hashlib.sha256(nonce.encode()).hexdigest(),
            status="native-challenge",
            approved_username=username,
            approved_sub=challenge,
            expires_at=now + timedelta(seconds=TTL),
        )
    )
    await db.commit()
    continuation = jwt.encode(
        {
            "iss": ISSUER,
            "aud": settings.cognito_cli_client_id,
            "pool": settings.cognito_user_pool_id,
            "sub": username,
            "challenge": challenge,
            "session": result["Session"],
            "required": required,
            "jti": nonce,
            "iat": now,
            "exp": now + timedelta(seconds=TTL),
        },
        settings.token_secret_key,
        algorithm="HS256",
    )
    return JSONResponse(
        {"challenge": challenge, "continuation": continuation, "required_attributes": required}, headers={"Cache-Control": "no-store"}
    )


@router.post("/password")
async def password_login(request: Request, db: AsyncSession = Depends(get_db), client=Depends(get_native_client)):
    settings = settings_for_login()
    await limit(db, "ip", request.client.host if request.client else "unknown", 30)
    body = await payload(request)
    username = string_field(body, "username", 128)
    password = string_field(body, "password")
    await limit(db, "user", username, 10)
    result = await call(
        client,
        "admin_initiate_auth",
        UserPoolId=settings.cognito_user_pool_id,
        ClientId=settings.cognito_cli_client_id,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )
    return await response(result, username, settings, db)


@router.post("/challenge")
async def challenge_login(request: Request, db: AsyncSession = Depends(get_db), client=Depends(get_native_client)):
    settings = settings_for_login()
    await limit(db, "ip", request.client.host if request.client else "unknown", 30)
    body = await payload(request)
    token = string_field(body, "continuation", 24000)
    try:
        claims = jwt.decode(
            token,
            settings.token_secret_key,
            algorithms=["HS256"],
            audience=settings.cognito_cli_client_id,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "jti", "sub", "aud", "iss"]},
        )
        if claims["pool"] != settings.cognito_user_pool_id or claims["challenge"] not in SUPPORTED:
            raise ValueError
    except (jwt.PyJWTError, ValueError, KeyError):
        raise failure() from None
    await limit(db, "user", claims["sub"], 10)
    challenge = claims["challenge"]
    key = {"NEW_PASSWORD_REQUIRED": "NEW_PASSWORD", "SMS_MFA": "SMS_MFA_CODE", "SOFTWARE_TOKEN_MFA": "SOFTWARE_TOKEN_MFA_CODE"}[challenge]
    supplied = body.get("responses")
    required = {key, *claims["required"]}
    if not isinstance(supplied, dict) or set(supplied) != required:
        raise failure("invalid_request", 400)
    responses = {name: string_field(supplied, name) for name in required}
    now = datetime.now(UTC)
    consumed = await db.execute(
        update(CliAuthRequest)
        .where(
            CliAuthRequest.device_code_hash == hashlib.sha256(claims["jti"].encode()).hexdigest(),
            CliAuthRequest.status == "native-challenge",
            CliAuthRequest.approved_username == claims["sub"],
            CliAuthRequest.approved_sub == challenge,
            CliAuthRequest.expires_at > now,
        )
        .values(status="native-used")
        .returning(CliAuthRequest.id)
    )
    if consumed.scalar_one_or_none() is None:
        await db.rollback()
        raise failure()
    await db.commit()
    result = await call(
        client,
        "admin_respond_to_auth_challenge",
        UserPoolId=settings.cognito_user_pool_id,
        ClientId=settings.cognito_cli_client_id,
        ChallengeName=challenge,
        Session=claims["session"],
        ChallengeResponses={"USERNAME": claims["sub"], **responses},
    )
    return await response(result, claims["sub"], settings, db)


@router.get("/admin-session")
async def admin_session(user: TokenContext = Depends(require_admin)):
    return {"verified": True, "user_id": user.user_id, "org_id": user.org_id, "role": "platform_admin"}
