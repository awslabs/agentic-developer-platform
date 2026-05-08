"""Onboarding API handler — access status, request, admin approve/deny.

Issue #538: Self-serve onboarding flow routes.
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user, require_admin
from src.shared.database import get_db
from src.shared.models.onboarding import TenantAccessRequest
from src.shared.models.organization import Organization, User
from src.shared.schemas.auth import TokenContext

from .approval import approve_request, deny_request
from .schemas import (
    AccessRequestPayload,
    AccessRequestResponse,
    AccessStatusResponse,
    AdminAccessRequestItem,
    AdminAccessRequestList,
    AdminDecisionPayload,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_auto_approve_orgs() -> list[dict]:
    """Parse ONBOARDING_AUTO_APPROVE_ORGS from environment (JSON list)."""
    raw = os.environ.get("ONBOARDING_AUTO_APPROVE_ORGS", "[]")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


def _is_auto_approve(target_login: str) -> bool:
    """Check if the target_login is in the auto-approve list."""
    orgs = _get_auto_approve_orgs()
    for entry in orgs:
        if entry.get("login") == target_login:
            return True
    return False


def _v2_write_enabled() -> bool:
    return os.environ.get("USER_IDENTITY_INDEX_V2_WRITE", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Public routes (authenticated but no tenant required)
# ---------------------------------------------------------------------------


@router.get("/api/access/status", response_model=AccessStatusResponse)
async def get_access_status(
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AccessStatusResponse:
    """Check if the caller has a user row (registered) or needs to onboard."""
    cognito_sub = current_user.user_id

    # Check if user already exists
    stmt = select(User).where(User.cognito_sub == cognito_sub)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if user is not None:
        return AccessStatusResponse(status="registered")

    # Check if there's a pending request
    stmt = select(TenantAccessRequest).where(
        TenantAccessRequest.cognito_sub == cognito_sub,
        TenantAccessRequest.status == "pending",
    )
    result = await db.execute(stmt)
    pending = result.scalar_one_or_none()
    if pending is not None:
        return AccessStatusResponse(status="pending", request_id=pending.id)

    return AccessStatusResponse(status="new")


@router.post("/api/access/request", response_model=AccessRequestResponse)
async def submit_access_request(
    payload: AccessRequestPayload,
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AccessRequestResponse:
    """Submit an onboarding access request."""
    # Preflight: check feature flag
    if not _v2_write_enabled():
        return AccessRequestResponse(
            status="unavailable",
            reason="USER_IDENTITY_INDEX_V2_WRITE=false, run #537 migration steps first",
        )

    cognito_sub = current_user.user_id
    proposed_id = payload.proposed_tenant_id

    # Collision check 1: organizations table
    existing_org = await db.get(Organization, proposed_id)
    if existing_org is not None:
        return AccessRequestResponse(
            status="collision",
            reason="proposed_tenant_id already exists",
        )

    # Collision check 2: pending requests for same tenant_id by someone else
    stmt = select(TenantAccessRequest).where(
        TenantAccessRequest.proposed_tenant_id == proposed_id,
        TenantAccessRequest.status == "pending",
    )
    result = await db.execute(stmt)
    existing_request = result.scalar_one_or_none()
    if existing_request is not None and existing_request.cognito_sub != cognito_sub:
        return AccessRequestResponse(
            status="collision",
            reason="proposed_tenant_id already requested by another user",
        )

    # Idempotency: check if this user already has a pending request
    stmt = select(TenantAccessRequest).where(
        TenantAccessRequest.cognito_sub == cognito_sub,
        TenantAccessRequest.status == "pending",
    )
    result = await db.execute(stmt)
    dup_request = result.scalar_one_or_none()
    if dup_request is not None:
        return AccessRequestResponse(
            status="pending",
            request_id=dup_request.id,
            eta_hours=24,
        )

    # Create the request
    request = TenantAccessRequest(
        cognito_sub=cognito_sub,
        provider=payload.provider,
        provider_user_id=payload.provider_user_id,
        proposed_tenant_id=proposed_id,
        target_login=payload.target_login,
        motivation=payload.motivation,
    )
    db.add(request)
    await db.commit()
    await db.refresh(request)

    # Auto-approve check
    if _is_auto_approve(payload.target_login):
        from src.admin.identity.identity_index_writer import IdentityIndexWriter

        writer = IdentityIndexWriter()
        tenant_id = await approve_request(
            db=db,
            request=request,
            admin_sub="system:auto-approve",
            identity_writer=writer,
        )
        return AccessRequestResponse(
            status="approved",
            tenant_id=tenant_id,
            redirect="/dashboard",
        )

    return AccessRequestResponse(
        status="pending",
        request_id=request.id,
        eta_hours=24,
    )


# ---------------------------------------------------------------------------
# Admin routes (platform_admin role required)
# ---------------------------------------------------------------------------


@router.get("/admin/access-requests", response_model=AdminAccessRequestList)
async def list_access_requests(
    admin: TokenContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminAccessRequestList:
    """List pending access requests for admin review."""
    stmt = select(TenantAccessRequest).where(TenantAccessRequest.status == "pending")
    result = await db.execute(stmt)
    requests = result.scalars().all()

    items = [
        AdminAccessRequestItem(
            id=r.id,
            cognito_sub=r.cognito_sub,
            provider=r.provider,
            provider_user_id=r.provider_user_id,
            proposed_tenant_id=r.proposed_tenant_id,
            target_login=r.target_login,
            motivation=r.motivation,
            status=r.status,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in requests
    ]
    return AdminAccessRequestList(requests=items)


@router.post("/admin/access-requests/{request_id}/approve")
async def approve_access_request(
    request_id: str,
    body: AdminDecisionPayload | None = None,
    admin: TokenContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Approve a pending access request (admin only)."""
    request = await db.get(TenantAccessRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Request not found")

    # Idempotent: already approved
    if request.status == "approved":
        return {"status": "approved", "tenant_id": request.proposed_tenant_id}

    from src.admin.identity.identity_index_writer import IdentityIndexWriter

    writer = IdentityIndexWriter()
    try:
        tenant_id = await approve_request(
            db=db,
            request=request,
            admin_sub=admin.user_id,
            identity_writer=writer,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {"status": "approved", "tenant_id": tenant_id}


@router.post("/admin/access-requests/{request_id}/deny")
async def deny_access_request(
    request_id: str,
    body: AdminDecisionPayload | None = None,
    admin: TokenContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Deny a pending access request and delete the user's Cognito account."""
    request = await db.get(TenantAccessRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Request not found")

    # Get Cognito client for AdminDeleteUser
    cognito_client = None
    user_pool_id = os.environ.get("COGNITO_USER_POOL_ID")
    if user_pool_id:
        try:
            import boto3

            cognito_client = boto3.client(
                "cognito-idp",
                region_name=os.environ.get("AWS_REGION", "us-east-1"),
            )
        except Exception:
            logger.warning("Could not create Cognito client for deny action")

    try:
        await deny_request(
            db=db,
            request=request,
            admin_sub=admin.user_id,
            decision_note=body.decision_note if body else None,
            cognito_client=cognito_client,
            user_pool_id=user_pool_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {"status": "denied", "request_id": request_id}
