"""AWS account connect flow — CloudFormation Quick-Create pattern.

Issue #562: Self-serve AWS account connect UI.

Endpoints:
  POST /auth/credentials/aws/connect — start a connect flow (pending credential)
  POST /auth/credentials/aws/verify  — verify the stack was created (STS AssumeRole)

Reuses existing endpoints for list (GET /auth/credentials) and delete
(DELETE /auth/credentials/{id}) — no reimplementation needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.internal.sts_assume_service import STSAssumeError, assume_role
from src.shared.database import get_db
from src.shared.models.vault import UserCredential
from src.shared.schemas.auth import TokenContext
from src.shared.services.secrets_manager import SecretsManagerHelper

from .cfn_template import build_launch_url, compute_role_arn
from .middleware import get_current_user_context
from .vault_routes import get_secrets_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/credentials/aws", tags=["aws-connect"])


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class ConnectStartRequest(BaseModel):
    """Body for POST /auth/credentials/aws/connect."""

    nickname: str
    account_id: str
    role_name: str = "ADP-Agent-Role"

    @field_validator("account_id")
    @classmethod
    def validate_account_id(cls, v: str) -> str:
        """AWS account IDs are 12 digits."""
        if not v.isdigit() or len(v) != 12:
            raise ValueError("AWS account IDs must be exactly 12 digits")
        return v

    @field_validator("nickname")
    @classmethod
    def validate_nickname(cls, v: str) -> str:
        """Nickname must be non-empty and reasonable length."""
        v = v.strip()
        if not v:
            raise ValueError("Nickname cannot be empty")
        if len(v) > 64:
            raise ValueError("Nickname must be 64 characters or fewer")
        return v


class ConnectStartResponse(BaseModel):
    credential_id: str
    launch_url: str


class ConnectVerifyRequest(BaseModel):
    """Body for POST /auth/credentials/aws/verify."""

    credential_id: str


class ConnectVerifyResponse(BaseModel):
    status: str  # "verified" | "failed"
    reason: str | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/connect",
    response_model=ConnectStartResponse,
    status_code=201,
    summary="Start an AWS account connect flow",
    description=(
        "Creates a pending credential row and returns a CloudFormation Quick-Create "
        "URL. The user opens the URL in their AWS Console to create the IAM role."
    ),
)
async def connect_start(
    data: ConnectStartRequest,
    token_context: TokenContext = Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
    sm: SecretsManagerHelper = Depends(get_secrets_manager),
) -> ConnectStartResponse:
    # Generate a unique external ID for confused-deputy protection
    external_id = str(uuid.uuid4())

    # Compute the expected role ARN
    role_arn = compute_role_arn(data.account_id, data.nickname)

    # Build the SM secret payload (same shape as #481 consumer expects)
    secret_payload = json.dumps(
        {
            "role_arn": role_arn,
            "external_id": external_id,
            "account_id": data.account_id,
            "default_region": "us-east-1",
        }
    )

    # Store in Secrets Manager
    secret_arn: str = await asyncio.to_thread(
        sm.create_secret,
        "aws",
        data.nickname,
        secret_payload,
        user_sub=token_context.user_id,
    )

    # Create the DB row with status=pending in scopes JSON
    cred = UserCredential(
        org_id=token_context.org_id,
        user_id=token_context.user_id,
        service="aws",
        credential_type="aws_role",
        label=data.nickname,
        secret_arn=secret_arn,
        scopes={
            "account_id": data.account_id,
            "role_arn": role_arn,
            "status": "pending",
        },
    )
    db.add(cred)
    await db.commit()
    await db.refresh(cred)

    # Build the launch URL
    launch_url = build_launch_url(
        nickname=data.nickname,
        external_id=external_id,
        account_id=data.account_id,
        user_id=token_context.user_id,
        role_name=data.role_name,
    )

    logger.info(
        "AWS connect flow started credential_id=%s user=%s account=%s",
        cred.id,
        token_context.user_id,
        data.account_id,
    )

    return ConnectStartResponse(credential_id=cred.id, launch_url=launch_url)


@router.post(
    "/verify",
    response_model=ConnectVerifyResponse,
    summary="Verify the AWS role was created",
    description=("Attempts STS AssumeRole with the pending credential's external ID and session tags. On success, marks the credential as verified."),
)
async def connect_verify(
    data: ConnectVerifyRequest,
    token_context: TokenContext = Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
    sm: SecretsManagerHelper = Depends(get_secrets_manager),
) -> ConnectVerifyResponse:
    # Fetch the credential row (scoped to caller)
    stmt = select(UserCredential).where(
        UserCredential.id == data.credential_id,
        UserCredential.user_id == token_context.user_id,
        UserCredential.org_id == token_context.org_id,
        UserCredential.credential_type == "aws_role",
    )
    result = await db.execute(stmt)
    cred = result.scalar_one_or_none()

    if cred is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Credential not found"},
        )

    # Idempotency: already verified → no-op
    if cred.scopes and cred.scopes.get("status") == "verified":
        return ConnectVerifyResponse(status="verified")

    # Read secret payload to get role_arn and external_id
    secret_value: str = await asyncio.to_thread(sm.get_secret, cred.secret_arn)
    secret_data = json.loads(secret_value)

    role_arn = secret_data["role_arn"]
    external_id = secret_data.get("external_id")

    # Attempt STS AssumeRole using the existing service
    try:
        await asyncio.to_thread(
            assume_role,
            role_arn=role_arn,
            external_id=external_id,
            session_duration_seconds=900,  # minimum for verify
            default_region=secret_data.get("default_region", "us-east-1"),
            user_id=token_context.user_id,
            agent_id="connect-verify",
            task_id="verify",
            label=cred.label,
        )
    except STSAssumeError as exc:
        # Map STS error codes to user-friendly reasons
        reason = _sts_error_to_reason(exc.code)
        logger.warning(
            "AWS connect verify failed credential_id=%s code=%s",
            cred.id,
            exc.code,
        )
        return ConnectVerifyResponse(status="failed", reason=reason)

    # Success — update the scopes JSON to verified
    updated_scopes = dict(cred.scopes) if cred.scopes else {}
    updated_scopes["status"] = "verified"
    updated_scopes["verified_at"] = datetime.now(UTC).isoformat()
    cred.scopes = updated_scopes
    await db.commit()

    logger.info(
        "AWS connect verified credential_id=%s user=%s role_arn=%s",
        cred.id,
        token_context.user_id,
        role_arn,
    )

    return ConnectVerifyResponse(status="verified")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sts_error_to_reason(code: str) -> str:
    """Map STS error codes to user-friendly messages."""
    mapping = {
        "NoSuchEntity": "The IAM role has not been created yet. Please ensure the CloudFormation stack completed successfully.",
        "AccessDenied": "The role's trust policy rejected the assume request. Please verify the stack finished creating.",
        "AccessDeniedException": "The role's trust policy rejected the assume request. Please verify the stack finished creating.",
        "MalformedPolicyDocument": "The role's trust policy is malformed. Please delete and recreate the stack.",
        "RegionDisabledException": "The target region is disabled. Please check your AWS account settings.",
    }
    return mapping.get(code, f"Verification failed: {code}. Please check the CloudFormation stack status in your AWS Console.")
