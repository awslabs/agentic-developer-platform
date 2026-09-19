"""Auth endpoints — API key login, API key creation, and Cognito signup."""

import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.middleware.auth import create_access_token, get_current_org
from app.models.api_key import ApiKey
from app.models.organization import Organization
from app.models.workspace import Workspace
from app.schemas.auth import (
    CreateApiKeyRequest,
    CreateApiKeyResponse,
    LoginRequest,
    LoginResponse,
    SignupRequest,
    SignupResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# API key format: "sp_" + 32 random hex chars (16 bytes)
API_KEY_PREFIX = "sp_"
API_KEY_RANDOM_BYTES = 16  # 32 hex chars → total key length ~35 chars


def _generate_api_key() -> str:
    """Generate a new API key with the sp_ prefix."""
    return API_KEY_PREFIX + secrets.token_hex(API_KEY_RANDOM_BYTES)


def _hash_api_key(raw_key: str) -> str:
    """SHA-256 hash of the API key (constant-time comparison at verify)."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _verify_api_key(raw_key: str, stored_hash: str) -> bool:
    """Constant-time comparison of API key hash."""
    computed = hashlib.sha256(raw_key.encode()).hexdigest()
    return hmac.compare_digest(computed, stored_hash)


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    db: AsyncSession = Depends(get_session),
) -> LoginResponse:
    """Exchange an API key for a JWT access token.

    The API key is verified against the hashed value in the database.
    """
    # Use key_prefix to narrow search, then verify hash (avoids full table scan)
    if not body.api_key.startswith(API_KEY_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    prefix = body.api_key[:12] + "..."
    result = await db.execute(
        select(ApiKey).where(ApiKey.is_active.is_(True), ApiKey.key_prefix == prefix)
    )
    api_keys = result.scalars().all()

    matched_key: ApiKey | None = None
    for ak in api_keys:
        if _verify_api_key(body.api_key, ak.key_hash):
            matched_key = ak
            break

    if matched_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    # Update last_used_at
    matched_key.last_used_at = datetime.now(timezone.utc)
    await db.commit()

    # Create JWT
    token, expires_in = create_access_token(matched_key.org_id)
    return LoginResponse(access_token=token, expires_in=expires_in)


@router.post(
    "/token", response_model=CreateApiKeyResponse, status_code=status.HTTP_201_CREATED
)
async def create_api_key(
    body: CreateApiKeyRequest,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> CreateApiKeyResponse:
    """Create a new API key for the authenticated organization.

    The raw key is returned only once — it cannot be retrieved later.
    """
    raw_key = _generate_api_key()
    key_hash = _hash_api_key(raw_key)
    key_prefix = raw_key[:12] + "..."

    api_key = ApiKey(
        org_id=org_id,
        name=body.name,
        key_hash=key_hash,
        key_prefix=key_prefix,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    return CreateApiKeyResponse(
        id=api_key.id,
        name=api_key.name,
        key=raw_key,
        key_prefix=key_prefix,
    )


def _get_cognito_client():
    """Create a Cognito Identity Provider client."""
    return boto3.client("cognito-idp", region_name=settings.aws_region)


async def _create_cognito_user(email: str, password: str) -> str:
    """Register a user in Cognito and return the user sub (unique ID).

    Raises HTTPException on failure.
    """
    if not settings.cognito_user_pool_id or not settings.cognito_app_client_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cognito is not configured",
        )

    client = _get_cognito_client()
    try:
        response = client.sign_up(
            ClientId=settings.cognito_app_client_id,
            Username=email,
            Password=password,
            UserAttributes=[
                {"Name": "email", "Value": email},
            ],
        )
        cognito_sub = response["UserSub"]
        logger.info("Cognito user created: sub=%s email=%s", cognito_sub, email)

        # Auto-confirm the user for seamless signup (admin action)
        client.admin_confirm_sign_up(
            UserPoolId=settings.cognito_user_pool_id,
            Username=email,
        )
        logger.info("Cognito user auto-confirmed: %s", email)

        return cognito_sub
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        error_msg = exc.response["Error"]["Message"]
        logger.error("Cognito signup failed: %s — %s", error_code, error_msg)

        if error_code == "UsernameExistsException":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A user with this email already exists",
            ) from exc
        if error_code == "InvalidPasswordException":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Password does not meet requirements: {error_msg}",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Cognito signup error: {error_code}",
        ) from exc


@router.post(
    "/signup", response_model=SignupResponse, status_code=status.HTTP_201_CREATED
)
async def signup(
    body: SignupRequest,
    db: AsyncSession = Depends(get_session),
) -> SignupResponse:
    """Self-service signup: create Cognito user + org + default workspace.

    Flow:
    1. Create user in Cognito (email + password)
    2. Create organization row in Aurora
    3. Auto-create "default" workspace (YOLO mode)
    4. Return JWT with org_id claim
    """
    # Step 1: Create Cognito user
    cognito_sub = await _create_cognito_user(body.email, body.password)

    # Step 2: Check if org already exists for this Cognito sub (idempotency)
    existing_org = await db.execute(
        select(Organization).where(Organization.cognito_sub == cognito_sub)
    )
    org = existing_org.scalar_one_or_none()

    if org is not None:
        # Org already exists — find the default workspace and return JWT
        ws_result = await db.execute(
            select(Workspace).where(
                Workspace.org_id == org.id,
                Workspace.name == "default",
            )
        )
        default_ws = ws_result.scalar_one_or_none()
        ws_id = default_ws.id if default_ws else uuid.uuid4()

        token, expires_in = create_access_token(org.id)
        return SignupResponse(
            access_token=token,
            expires_in=expires_in,
            org_id=org.id,
            org_name=org.name,
            default_workspace_id=ws_id,
        )

    # Step 3: Create organization
    org = Organization(
        name=body.org_name,
        cognito_sub=cognito_sub,
        billing_plan="free",
    )
    db.add(org)
    await db.flush()  # Get org.id before creating workspace

    # Step 4: Auto-create "default" workspace (YOLO mode — namespace isolation)
    default_workspace = Workspace(
        org_id=org.id,
        name="default",
        isolation_mode="namespace",
        status="Active",
    )
    db.add(default_workspace)
    await db.commit()
    await db.refresh(org)
    await db.refresh(default_workspace)

    logger.info(
        "Signup complete: org_id=%s org_name=%s workspace_id=%s email=%s",
        org.id,
        org.name,
        default_workspace.id,
        body.email,
    )

    # Step 5: Return JWT
    token, expires_in = create_access_token(org.id)
    return SignupResponse(
        access_token=token,
        expires_in=expires_in,
        org_id=org.id,
        org_name=org.name,
        default_workspace_id=default_workspace.id,
    )
