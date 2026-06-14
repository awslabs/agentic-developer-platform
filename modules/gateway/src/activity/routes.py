"""Activity REST API routes — agent invocation read endpoints.

Two endpoints:
- GET /me/agent-invocations — caller's own invocations (user-index GSI)
- GET /admin/agent-invocations — org/tenant-scoped (tenant-index GSI, admin only)
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.activity.schemas import InvocationListResponse
from src.activity.service import ActivityService
from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

logger = logging.getLogger("bedrockgateway.activity")

router = APIRouter(tags=["activity"])


def get_activity_service() -> ActivityService:
    """Get activity service instance (singleton-ish; boto3 handles connection pooling)."""
    return ActivityService()


async def get_access_control(db: Annotated[AsyncSession, Depends(get_db)]) -> AccessControl:
    """Get access control instance."""
    return AccessControl(db)


# ---------------------------------------------------------------------------
# GET /me/agent-invocations — user's own invocations
# ---------------------------------------------------------------------------


@router.get("/me/agent-invocations", response_model=InvocationListResponse)
async def get_my_invocations(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    service: Annotated[ActivityService, Depends(get_activity_service)],
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    last_key: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    channel: Annotated[str | None, Query()] = None,
    persona: Annotated[str | None, Query()] = None,
    since: Annotated[str | None, Query()] = None,
    until: Annotated[str | None, Query()] = None,
) -> InvocationListResponse:
    """Get the authenticated user's own agent invocations.

    Scoping: uses `user_id` from the JWT token ONLY — ignores any user_id param.
    Queries the `user-index` GSI (PK=user_id, SK=arrived_at desc).

    Pagination note: filtered pages may be short/empty with a non-null
    `last_key`. Keep following until `last_key` is null.
    """
    try:
        return service.query_by_user(
            user_id=current_user.user_id,
            page_size=page_size,
            last_key=last_key,
            status=status,
            channel=channel,
            persona=persona,
            since=since,
            until=until,
        )
    except ValueError as exc:
        # Bad cursor
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# GET /admin/agent-invocations — tenant-scoped, admin only
# ---------------------------------------------------------------------------


@router.get("/admin/agent-invocations", response_model=InvocationListResponse)
async def get_admin_invocations(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    service: Annotated[ActivityService, Depends(get_activity_service)],
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    last_key: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    channel: Annotated[str | None, Query()] = None,
    persona: Annotated[str | None, Query()] = None,
    since: Annotated[str | None, Query()] = None,
    until: Annotated[str | None, Query()] = None,
    user_id: Annotated[str | None, Query()] = None,
    tenant_id: Annotated[str | None, Query()] = None,
) -> InvocationListResponse:
    """Get agent invocations for the caller's tenant (admin only).

    Scoping:
    - Org admins: pinned to their own org_id (from token). Cannot pass tenant_id.
    - Platform admins: may pass an explicit `tenant_id` to view any tenant.

    The `user_id` param filters to a specific user within the tenant (admin use).
    """
    # Permission check — reuses USAGE_READ which all admin roles have
    await access.check_permission(current_user, Permission.USAGE_READ, target_org_id=tenant_id)

    # Determine which tenant to query
    if current_user.is_admin and tenant_id:
        # Platform admin may specify any tenant
        effective_tenant_id = tenant_id
    else:
        # Org admins are pinned to their own org (org_id == tenant_id in this product)
        effective_tenant_id = current_user.org_id

    try:
        return service.query_by_tenant(
            tenant_id=effective_tenant_id,
            page_size=page_size,
            last_key=last_key,
            status=status,
            channel=channel,
            persona=persona,
            since=since,
            until=until,
            user_id=user_id,
        )
    except ValueError as exc:
        # Bad cursor
        raise HTTPException(status_code=400, detail=str(exc)) from exc
