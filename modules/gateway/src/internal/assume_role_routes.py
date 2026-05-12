"""Internal endpoint: POST /internal/v1/credential-assume-role

Issue #481: aws_role credential type + STS assume_role as a vault delivery path.

Server-side STS AssumeRole with session tagging. Returns short-lived temporary
credentials so the agent can write them to an AWS profile.

Authentication: X-Internal-Api-Key shared secret (same as other /internal/v1/* endpoints).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.sts_assume_service import STSAssumeError, assume_role
from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.organization import User
from src.shared.models.vault import UserCredential
from src.shared.services.credential_resolver import CredentialNotFoundError, CredentialResolver
from src.shared.services.secrets_manager import SecretsManagerHelper

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/v1", tags=["internal-credentials"])

_sm_helper = SecretsManagerHelper()


def get_secrets_manager() -> SecretsManagerHelper:
    """FastAPI dependency — override in tests."""
    return _sm_helper


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AssumeRoleRequestBody(BaseModel):
    """Body for POST /internal/v1/credential-assume-role."""

    user_id: str
    agent_id: str
    task_id: str
    service: str = "aws"
    label: str | None = None
    purpose: str | None = None


class AssumeRoleResponse(BaseModel):
    """Response for POST /internal/v1/credential-assume-role."""

    profile_name: str
    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: str
    region: str
    provenance_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_user(user_id: str, db: AsyncSession) -> User:
    """Fetch User row or raise 404."""
    from sqlalchemy import select

    stmt = select(User).where(User.id == user_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "user_not_found", "message": f"No user with id={user_id!r}"},
        )
    return user


async def _resolve_credential(
    *,
    db: AsyncSession,
    org_id: str,
    service: str,
    label: str | None,
    user_id: str,
    team_id: str | None,
) -> UserCredential:
    """Resolve credential via scope chain; raise 404 on miss."""
    resolver = CredentialResolver(db)
    try:
        return await resolver.resolve(
            org_id=org_id,
            service=service,
            label=label,
            user_id=user_id,
            team_id=team_id or None,
        )
    except CredentialNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "credential_not_found", "message": str(exc)},
        ) from exc


async def _write_audit(
    db: AsyncSession,
    *,
    event_type: str,
    org_id: str,
    actor_id: str | None,
    details: dict | None,
) -> None:
    log = AuditLog(
        org_id=org_id,
        event_type=event_type,
        actor_id=actor_id,
        details=details,
    )
    db.add(log)
    await db.flush()


# ---------------------------------------------------------------------------
# Endpoint: POST /internal/v1/credential-assume-role
# ---------------------------------------------------------------------------


@router.post(
    "/credential-assume-role",
    response_model=AssumeRoleResponse,
    summary="Assume an AWS role stored in the user's vault (internal)",
    description=(
        "Resolves the aws_role credential for (user_id, service, label?), performs "
        "STS AssumeRole with session tagging, and returns short-lived temporary "
        "credentials. Updates last_used_at and writes an audit log on every call."
    ),
)
async def credential_assume_role(
    body: AssumeRoleRequestBody,
    db: AsyncSession = Depends(get_db),
    sm: SecretsManagerHelper = Depends(get_secrets_manager),
    _: None = Depends(verify_internal_or_irsa),
) -> AssumeRoleResponse:
    provenance_id = str(uuid.uuid4())
    settings = get_settings()

    user = await _get_user(body.user_id, db)
    cred = await _resolve_credential(
        db=db,
        org_id=user.org_id,
        service=body.service,
        label=body.label,
        user_id=body.user_id,
        team_id=user.team_id,
    )

    # Validate credential type.
    if cred.credential_type != "aws_role":
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_credential_type",
                "message": (f"credential_type={cred.credential_type!r} is not an aws_role. This endpoint only works with aws_role credentials."),
            },
        )

    # Fetch and parse the aws_role JSON from Secrets Manager.
    secret_value = await asyncio.to_thread(sm.get_secret, cred.secret_arn)
    try:
        role_config = json.loads(secret_value)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.error("Failed to parse aws_role secret for credential %s", cred.id)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "invalid_credential_value",
                "message": "aws_role credential value is not valid JSON.",
            },
        ) from exc

    role_arn = role_config.get("role_arn")
    if not role_arn:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "invalid_credential_value",
                "message": "aws_role credential is missing 'role_arn'.",
            },
        )

    external_id = role_config.get("external_id")
    session_duration = role_config.get("session_duration_seconds", 3600)
    default_region = role_config.get("default_region", settings.aws_region)
    label_for_profile = body.label or cred.label or "default"

    # Perform the STS AssumeRole call (blocking — run in thread).
    try:
        result = await asyncio.to_thread(
            assume_role,
            role_arn=role_arn,
            external_id=external_id,
            session_duration_seconds=session_duration,
            default_region=default_region,
            user_id=body.user_id,
            agent_id=body.agent_id,
            task_id=body.task_id,
            label=label_for_profile,
            aws_region=settings.aws_region,
        )
    except STSAssumeError as exc:
        # Write audit for failed attempt — do NOT include role_arn in user-facing error.
        await _write_audit(
            db,
            event_type="vault_aws_role_assumed",
            org_id=user.org_id,
            actor_id=body.agent_id,
            details={
                "provenance_id": provenance_id,
                "user_id": body.user_id,
                "agent_id": body.agent_id,
                "task_id": body.task_id,
                "service": body.service,
                "label": body.label,
                "credential_id": cred.id,
                "purpose": body.purpose,
                "success": False,
                "error_code": exc.code,
            },
        )
        await db.commit()
        raise HTTPException(
            status_code=502,
            detail={
                "error": "sts_assume_failed",
                "message": f"Failed to assume AWS role: {exc}",
                "provenance_id": provenance_id,
            },
        ) from exc

    # Update last_used_at.
    stmt = update(UserCredential).where(UserCredential.id == cred.id).values(last_used_at=datetime.now(UTC))
    await db.execute(stmt)

    # Write success audit.
    await _write_audit(
        db,
        event_type="vault_aws_role_assumed",
        org_id=user.org_id,
        actor_id=body.agent_id,
        details={
            "provenance_id": provenance_id,
            "user_id": body.user_id,
            "agent_id": body.agent_id,
            "task_id": body.task_id,
            "service": body.service,
            "label": body.label,
            "credential_id": cred.id,
            "purpose": body.purpose,
            "success": True,
            # NOTE: role_arn intentionally logged in audit (server-side only, not returned to agent).
            "role_arn": role_arn,
        },
    )
    await db.commit()

    logger.info(
        "AWS role assumed provenance_id=%s service=%s label=%s",
        provenance_id,
        body.service,
        body.label,
    )

    return AssumeRoleResponse(
        profile_name=result.profile_name,
        access_key_id=result.access_key_id,
        secret_access_key=result.secret_access_key,
        session_token=result.session_token,
        expiration=result.expiration,
        region=result.region,
        provenance_id=provenance_id,
    )
