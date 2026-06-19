"""Activity REST API routes — agent invocation read endpoints.

Endpoints:
- GET /me/agent-invocations — caller's own invocations (user-index GSI)
- GET /admin/agent-invocations — org/tenant-scoped (tenant-index GSI, admin only)
- GET /me/agent-invocations/chain/{correlation_id} — chain view for a correlation
- GET /admin/agent-invocations/chain/{correlation_id} — admin chain view
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.activity.cost_service import get_cost_by_run_ids
from src.activity.schemas import InvocationChainResponse, InvocationListResponse
from src.activity.service import ActivityService
from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.models.organization import User
from src.shared.schemas.auth import TokenContext

logger = logging.getLogger("bedrockgateway.activity")

router = APIRouter(tags=["activity"])


def get_activity_service() -> ActivityService:
    """Get activity service instance (singleton-ish; boto3 handles connection pooling)."""
    return ActivityService()


async def get_access_control(db: Annotated[AsyncSession, Depends(get_db)]) -> AccessControl:
    """Get access control instance."""
    return AccessControl(db)


async def _resolve_canonical_user_id(db: AsyncSession, token: TokenContext) -> str:
    """Resolve the caller's Cognito sub (TokenContext.user_id) to the canonical
    ADP user_id (`users.id`).

    Invocation rows in the webhook-events table are stored under the canonical
    `users.id` (the UUID that the webhook-ingress identity resolver maps every
    provider identity — GitHub sender, Cognito sub — to). But the gateway's
    JWT middleware sets `TokenContext.user_id = claims.sub` (the raw Cognito
    sub), which is a *provider* identity, NOT the canonical id. Querying the
    `user-index` GSI with the raw sub therefore matches nothing and the screen
    shows an empty "mine" view even when the user has invocations (issue: a
    logged-in user sees no agent activity).

    This mirrors the established `select(User).where(User.cognito_sub == sub)`
    pattern used across the gateway (e.g. auth/org_id_resolver.py). If no
    matching user row exists, fall back to the raw token value so behavior is
    unchanged for identities that aren't (yet) provisioned in Postgres.
    """
    canonical = await db.scalar(select(User.id).where(User.cognito_sub == token.user_id))
    if canonical:
        return canonical
    logger.warning(
        "No users row for cognito_sub=%s; falling back to raw token user_id for activity query",
        token.user_id,
    )
    return token.user_id


async def _enrich_with_cost(db: AsyncSession, response: InvocationListResponse) -> InvocationListResponse:
    """Enrich invocation items with per-run cost data from Postgres.

    Issue #1616: Batched Postgres query to avoid N+1. Uses invocation_id as the
    agent_run_id join key. Graceful degradation: if Postgres query fails, items
    are returned with null cost fields (no 500).
    """
    if not response.items:
        return response

    # Collect invocation IDs for batch query
    run_ids = [item.invocation_id for item in response.items]

    try:
        cost_map = await get_cost_by_run_ids(db, run_ids)
    except Exception as exc:
        logger.warning(
            "Failed to enrich activity with cost data — returning items without cost",
            extra={"error": str(exc)},
        )
        return response

    # Merge cost data into items
    for item in response.items:
        cost_data = cost_map.get(item.invocation_id)
        if cost_data:
            item.total_cost_usd = cost_data["total_cost_usd"]
            item.total_tokens = cost_data["total_tokens"]
            item.call_count = cost_data["call_count"]

    return response


# ---------------------------------------------------------------------------
# GET /me/agent-invocations — user's own invocations
# ---------------------------------------------------------------------------


@router.get("/me/agent-invocations", response_model=InvocationListResponse)
async def get_my_invocations(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    service: Annotated[ActivityService, Depends(get_activity_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    last_key: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    channel: Annotated[str | None, Query()] = None,
    persona: Annotated[str | None, Query()] = None,
    since: Annotated[str | None, Query()] = None,
    until: Annotated[str | None, Query()] = None,
) -> InvocationListResponse:
    """Get the authenticated user's own agent invocations.

    Scoping: derives the canonical user_id from the JWT token ONLY (the Cognito
    sub is resolved to `users.id`) — ignores any user_id param. Queries the
    `user-index` GSI (PK=user_id, SK=arrived_at desc).

    Pagination note: filtered pages may be short/empty with a non-null
    `last_key`. Keep following until `last_key` is null.
    """
    canonical_user_id = await _resolve_canonical_user_id(db, current_user)
    try:
        result = service.query_by_user(
            user_id=canonical_user_id,
            page_size=page_size,
            last_key=last_key,
            status=status,
            channel=channel,
            persona=persona,
            since=since,
            until=until,
        )
        # Issue #1616: Enrich with per-run cost from Postgres
        return await _enrich_with_cost(db, result)
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
    db: Annotated[AsyncSession, Depends(get_db)],
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
        result = service.query_by_tenant(
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
        # Issue #1616: Enrich with per-run cost from Postgres
        return await _enrich_with_cost(db, result)
    except ValueError as exc:
        # Bad cursor
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# GET /me/agent-invocations/chain/{correlation_id} — user chain view
# ---------------------------------------------------------------------------


@router.get("/me/agent-invocations/chain/{correlation_id}", response_model=InvocationChainResponse)
async def get_my_invocation_chain(
    correlation_id: Annotated[str, Path(description="Correlation ID of the chain to view")],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    service: Annotated[ActivityService, Depends(get_activity_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InvocationChainResponse:
    """Get the chain view for a specific correlation_id.

    Scoping: only returns invocations the caller owns (canonical user_id derived
    from the token's Cognito sub). Shows the entire chain the caller roots or
    participates in.
    """
    canonical_user_id = await _resolve_canonical_user_id(db, current_user)
    return service.get_chain(
        correlation_id=correlation_id,
        user_id=canonical_user_id,
    )


# ---------------------------------------------------------------------------
# GET /admin/agent-invocations/chain/{correlation_id} — admin chain view
# ---------------------------------------------------------------------------


@router.get("/admin/agent-invocations/chain/{correlation_id}", response_model=InvocationChainResponse)
async def get_admin_invocation_chain(
    correlation_id: Annotated[str, Path(description="Correlation ID of the chain to view")],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    service: Annotated[ActivityService, Depends(get_activity_service)],
    tenant_id: Annotated[str | None, Query()] = None,
) -> InvocationChainResponse:
    """Get the chain view for a specific correlation_id (admin).

    Scoping: org admins see chains within their tenant; platform admins
    can specify any tenant_id.
    """
    await access.check_permission(current_user, Permission.USAGE_READ, target_org_id=tenant_id)

    if current_user.is_admin and tenant_id:
        effective_tenant_id = tenant_id
    else:
        effective_tenant_id = current_user.org_id

    return service.get_chain(
        correlation_id=correlation_id,
        tenant_id=effective_tenant_id,
    )
