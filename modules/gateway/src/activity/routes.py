"""Activity REST API routes — agent invocation read endpoints.

Endpoints:
- GET /me/agent-invocations — caller's own invocations (user-index GSI)
- GET /admin/agent-invocations — org/tenant-scoped (tenant-index GSI, admin only)
- GET /me/agent-invocations/chain/{correlation_id} — chain view for a correlation
- GET /admin/agent-invocations/chain/{correlation_id} — admin chain view
- GET /me/agent-invocations/{invocation_id}/transcript — user transcript (Issue #3069)
- GET /admin/agent-invocations/{invocation_id}/transcript — admin transcript (Issue #3069)
"""

import logging
import os
import re
from typing import Annotated, Literal

import boto3
import httpx
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.activity.control_schemas import (
    ControlCommandResponse,
    ControlPingResponse,
    ControlStateResponse,
)
from src.activity.control_service import ControlError, ControlService, validate_command_body
from src.activity.cost_service import get_cost_by_date_range, get_cost_by_run_ids
from src.activity.schemas import (
    ChainListResponse,
    InvocationChainItem,
    InvocationChainResponse,
    InvocationItem,
    InvocationListResponse,
)
from src.activity.service import ActivityService
from src.activity.stats_schemas import Spend, StatsResponse
from src.activity.stats_service import StatsService
from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.identity import resolve_canonical_user_id
from src.shared.schemas.auth import TokenContext

logger = logging.getLogger("bedrockgateway.activity")

router = APIRouter(tags=["activity"])


def get_activity_service() -> ActivityService:
    """Get activity service instance (singleton-ish; boto3 handles connection pooling)."""
    return ActivityService()


async def get_access_control(db: Annotated[AsyncSession, Depends(get_db)]) -> AccessControl:
    """Get access control instance."""
    return AccessControl(db)


def get_stats_service() -> StatsService:
    """Get stats service instance (singleton-ish; boto3 handles connection pooling)."""
    return StatsService()


_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _expand_date_bound(value: str | None, *, end: bool) -> str | None:
    """Widen a bare YYYY-MM-DD to a full-day ISO-8601 instant.

    Issue #4390: `since`/`until` are compared *lexicographically* against the
    `arrived_at` DynamoDB sort key, which stores a full ISO-8601 instant
    (e.g. "2026-06-13T22:00:00Z"). A bare date is therefore a broken bound:
    "2026-06-13" < "2026-06-13T22:00:00Z", so as an upper bound it silently
    excludes the whole end day, and since == until returns zero rows.

    Callers that already send a timed value (and malformed values) pass through
    untouched — malformed input stays rejected downstream exactly as before.
    """
    if value and _DATE_ONLY.match(value):
        return f"{value}T23:59:59.999Z" if end else f"{value}T00:00:00Z"
    return value


# ---------------------------------------------------------------------------
# GET /me/agent-run-stats — user's aggregate dashboard stats (Issue #3630)
# ---------------------------------------------------------------------------


@router.get("/me/agent-run-stats", response_model=StatsResponse)
async def get_my_stats(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    stats_service: Annotated[StatsService, Depends(get_stats_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: Annotated[int, Query(ge=1, le=30)] = 7,
) -> StatsResponse:
    """Get aggregated agent run stats for the authenticated user.

    Issue #3630: Single-call dashboard payload combining DynamoDB invocation
    aggregates with Postgres cost data. Time-bounded by `days` param (1-30):
    days=N means the N calendar days ending today (UTC). days=1 returns
    today only; days=7 returns the last 7 calendar days including today.
    Results are cached in-process for 60s per (user, days) key.

    Status filter: excludes no_op and webhook_received (same as Issue #1658).
    Cost enrichment: graceful degradation — if Postgres fails, spend is null.
    """
    canonical_user_id = await resolve_canonical_user_id(db, current_user.user_id, org_id=current_user.org_id)
    result = stats_service.get_stats_by_user(user_id=canonical_user_id, tenant_id=current_user.org_id, days=days)

    # Enrich with cost data from Postgres (cross-store pattern)
    result = await _enrich_stats_with_cost(db, result, stats_service, canonical_user_id, days, tenant_id=current_user.org_id)
    return result


# ---------------------------------------------------------------------------
# GET /admin/agent-run-stats — tenant-scoped aggregate stats (Issue #3630)
# ---------------------------------------------------------------------------


@router.get("/admin/agent-run-stats", response_model=StatsResponse)
async def get_admin_stats(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    stats_service: Annotated[StatsService, Depends(get_stats_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: Annotated[int, Query(ge=1, le=30)] = 7,
    tenant_id: Annotated[str | None, Query()] = None,
) -> StatsResponse:
    """Get aggregated agent run stats for a tenant (admin only).

    Issue #3630: Org admins see their own tenant; platform admins can specify
    any tenant_id. days=N means the N calendar days ending today (UTC).
    Same aggregation + cost enrichment as the user endpoint.
    """
    effective_tenant_id = tenant_id or current_user.org_id
    if not effective_tenant_id or not effective_tenant_id.strip():
        raise HTTPException(status_code=403, detail="An authorized tenant scope is required")
    await access.check_permission(current_user, Permission.ACTIVITY_READ_ALL, target_org_id=effective_tenant_id)

    result = stats_service.get_stats_by_tenant(tenant_id=effective_tenant_id, days=days)

    # Enrich with cost data from Postgres
    result = await _enrich_stats_with_cost(db, result, stats_service, effective_tenant_id, days, is_tenant=True)
    return result


async def _enrich_stats_with_cost(
    db: AsyncSession,
    result: StatsResponse,
    stats_service: StatsService,
    scope_id: str,
    days: int,
    *,
    is_tenant: bool = False,
    tenant_id: str | None = None,
) -> StatsResponse:
    """Enrich stats response with cost data from Postgres.

    Issue #3630: Fetches all run_ids from the stats service's last fetch and
    queries Postgres for aggregate spend. Graceful degradation: if Postgres
    fails, spend remains null (no 500).
    """
    # Re-fetch items to get run_ids for cost query (uses cache so no extra DDB call)
    if is_tenant:
        items = stats_service._fetch_items(
            index_name="tenant-index",
            partition_key_name="tenant_id",
            partition_key_value=scope_id,
            days=days,
        )
    else:
        # Issue #3705: Use merged fetch (user-index + root-human-index) so cost
        # enrichment includes chain-attributed runs.
        items = stats_service._fetch_items_merged(
            user_id=scope_id,
            days=days,
            tenant_id=tenant_id,
        )

    run_ids = [item.get("event_id", "") for item in items if item.get("event_id")]
    if not run_ids:
        return result

    try:
        cost_data = await get_cost_by_date_range(db, run_ids)
        result.spend = Spend(
            total_cost_usd=cost_data["total_cost_usd"],
            total_tokens=cost_data["total_tokens"],
            total_calls=cost_data["total_calls"],
        )
    except Exception as exc:
        logger.warning(
            "Failed to enrich stats with cost data — returning stats without spend",
            extra={"scope_id": scope_id, "error": str(exc)},
        )

    return result


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


async def _enrich_chains_with_cost(db: AsyncSession, response: ChainListResponse) -> ChainListResponse:
    """Enrich all chains in a ChainListResponse with per-run cost data.

    Issue #1662: Single batched Postgres query across ALL run_ids in ALL chains
    on the page. Bounded by page_size(20) × chain_depth_cap(50) = max 1000 IDs.
    Computes per-run cost and chain totals. Graceful degradation on failure.
    """
    if not response.chains:
        return response

    # Collect ALL run_ids across all chains (root + descendants)
    all_run_ids: list[str] = []
    for chain in response.chains:
        all_run_ids.append(chain.root.invocation_id)
        for desc in chain.descendants:
            all_run_ids.append(desc.invocation_id)

    if not all_run_ids:
        return response

    try:
        cost_map = await get_cost_by_run_ids(db, all_run_ids)
    except Exception as exc:
        logger.warning(
            "Failed to enrich chains with cost data — returning chains without cost",
            extra={"error": str(exc)},
        )
        return response

    # Apply cost to each chain
    for chain in response.chains:
        # Enrich root
        root_cost = cost_map.get(chain.root.invocation_id)
        if root_cost:
            chain.root.total_cost_usd = root_cost["total_cost_usd"]
            chain.root.total_tokens = root_cost["total_tokens"]
            chain.root.call_count = root_cost["call_count"]

        # Enrich descendants
        for desc in chain.descendants:
            desc_cost = cost_map.get(desc.invocation_id)
            if desc_cost:
                desc.total_cost_usd = desc_cost["total_cost_usd"]
                desc.total_tokens = desc_cost["total_tokens"]
                desc.call_count = desc_cost["call_count"]

        # Compute chain totals (root + descendants)
        chain_run_ids = [chain.root.invocation_id] + [d.invocation_id for d in chain.descendants]
        chain_costs = [cost_map[rid] for rid in chain_run_ids if rid in cost_map]
        if chain_costs:
            chain.chain_total_cost_usd = sum(c["total_cost_usd"] for c in chain_costs)
            chain.chain_total_tokens = sum(c["total_tokens"] for c in chain_costs)
            chain.chain_total_call_count = sum(c["call_count"] for c in chain_costs)

    return response


def _collect_chain_ids(nodes: list[InvocationChainItem]) -> list[str]:
    """Flatten a chain tree into a list of invocation_ids."""
    ids: list[str] = []
    for node in nodes:
        ids.append(node.invocation_id)
        if node.children:
            ids.extend(_collect_chain_ids(node.children))
    return ids


def _apply_cost_to_tree(nodes: list[InvocationChainItem], cost_map: dict) -> None:
    """Walk the chain tree and apply per-node cost from cost_map."""
    for node in nodes:
        cost_data = cost_map.get(node.invocation_id)
        if cost_data:
            node.total_cost_usd = cost_data["total_cost_usd"]
            node.total_tokens = cost_data["total_tokens"]
            node.call_count = cost_data["call_count"]
        if node.children:
            _apply_cost_to_tree(node.children, cost_map)


async def _enrich_chain_with_cost(db: AsyncSession, chain: InvocationChainResponse) -> InvocationChainResponse:
    """Enrich chain nodes with per-node cost and compute chain totals.

    Issue #1653: Single batched get_cost_by_run_ids over all chain invocation_ids
    (no N+1). Chain is capped at 50 items, so the IN clause is bounded.
    Graceful degradation: if Postgres fails, chain returns with null cost fields.
    """
    all_ids = _collect_chain_ids(chain.items)
    if not all_ids:
        return chain

    try:
        cost_map = await get_cost_by_run_ids(db, all_ids)
    except Exception as exc:
        logger.warning(
            "Failed to enrich chain with cost data — returning chain without cost",
            extra={"correlation_id": chain.correlation_id, "error": str(exc)},
        )
        return chain

    _apply_cost_to_tree(chain.items, cost_map)

    # Compute chain totals
    if cost_map:
        chain.chain_total_cost_usd = sum(v["total_cost_usd"] for v in cost_map.values())
        chain.chain_total_tokens = sum(v["total_tokens"] for v in cost_map.values())
        chain.chain_total_call_count = sum(v["call_count"] for v in cost_map.values())

    return chain


# ---------------------------------------------------------------------------
# GET /me/agent-invocations — user's own invocations
# ---------------------------------------------------------------------------


@router.get("/me/agent-invocations", response_model=InvocationListResponse | ChainListResponse)
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
    include_non_triggering: Annotated[bool, Query()] = False,
    view: Annotated[Literal["runs", "chains"], Query()] = "runs",
) -> InvocationListResponse | ChainListResponse:
    """Get the authenticated user's own agent invocations.

    Scoping: derives the canonical user_id from the JWT token ONLY (the Cognito
    sub is resolved to `users.id`) — ignores any user_id param. Queries the
    `user-index` GSI (PK=user_id, SK=arrived_at desc).

    Pagination note: filtered pages may be short/empty with a non-null
    `last_key`. Keep following until `last_key` is null.

    Issue #1658: When include_non_triggering is False (default), rows with
    status no_op or webhook_received are excluded from results. An explicit
    status filter takes precedence (selecting status=no_op will still return
    those rows regardless of this flag).

    Issue #1662: When view=chains, returns a ChainListResponse — one row per
    chain (root + descendants inline), paginated over chains by root arrived_at.
    Default view=runs preserves the flat list behavior.
    """
    canonical_user_id = await resolve_canonical_user_id(db, current_user.user_id, org_id=current_user.org_id)
    # Issue #4390: widen bare YYYY-MM-DD bounds to full-day instants
    since = _expand_date_bound(since, end=False)
    until = _expand_date_bound(until, end=True)
    try:
        if view == "chains":
            chain_result = service.query_chains_by_user(
                user_id=canonical_user_id,
                tenant_id=current_user.org_id,
                page_size=page_size,
                last_key=last_key,
                status=status,
                channel=channel,
                persona=persona,
                since=since,
                until=until,
                include_non_triggering=include_non_triggering,
            )
            # Issue #1662: Batch cost enrichment across all chains on the page
            return await _enrich_chains_with_cost(db, chain_result)
        else:
            result = service.query_by_user(
                user_id=canonical_user_id,
                tenant_id=current_user.org_id,
                page_size=page_size,
                last_key=last_key,
                status=status,
                channel=channel,
                persona=persona,
                since=since,
                until=until,
                include_non_triggering=include_non_triggering,
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
    include_non_triggering: Annotated[bool, Query()] = False,
) -> InvocationListResponse:
    """Get agent invocations for the caller's tenant (admin only).

    Scoping:
    - Org admins: pinned to their own org_id (from token). Cannot pass tenant_id.
    - Platform admins: may pass an explicit `tenant_id` to view any tenant.

    The `user_id` param filters to a specific user within the tenant (admin use).

    Issue #1658: When include_non_triggering is False (default), rows with
    status no_op or webhook_received are excluded. An explicit status filter
    takes precedence.
    """
    effective_tenant_id = tenant_id or current_user.org_id
    if not effective_tenant_id or not effective_tenant_id.strip():
        raise HTTPException(status_code=403, detail="An authorized tenant scope is required")
    await access.check_permission(current_user, Permission.ACTIVITY_READ_ALL, target_org_id=effective_tenant_id)

    # Issue #4390: widen bare YYYY-MM-DD bounds to full-day instants
    since = _expand_date_bound(since, end=False)
    until = _expand_date_bound(until, end=True)

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
            include_non_triggering=include_non_triggering,
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
    include_non_triggering: Annotated[bool, Query()] = False,
) -> InvocationChainResponse:
    """Get the chain view for a specific correlation_id.

    Scoping: only returns invocations the caller owns (canonical user_id derived
    from the token's Cognito sub). Shows the entire chain the caller roots or
    participates in.

    Issue #3708: When include_non_triggering is False (default), no_op and
    webhook_received items are excluded from the chain — same convention as
    the flat list endpoints (Issue #1658).
    """
    canonical_user_id = await resolve_canonical_user_id(db, current_user.user_id, org_id=current_user.org_id)
    chain = service.get_chain(
        correlation_id=correlation_id,
        user_id=canonical_user_id,
        tenant_id=current_user.org_id,
        include_non_triggering=include_non_triggering,
    )
    return await _enrich_chain_with_cost(db, chain)


# ---------------------------------------------------------------------------
# GET /me/agent-invocations/{invocation_id} — single-run detail (user)
# NOTE: Must be defined AFTER /chain/{correlation_id} so FastAPI matches
# the literal "chain" path segment before the catch-all {invocation_id}.
# ---------------------------------------------------------------------------


@router.get("/me/agent-invocations/{invocation_id}", response_model=InvocationItem)
async def get_my_invocation_detail(
    invocation_id: Annotated[str, Path(description="The invocation ID to fetch detail for")],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    service: Annotated[ActivityService, Depends(get_activity_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InvocationItem:
    """Get a single invocation's full detail.

    Issue #1653: Dedicated detail endpoint. Uses query-based lookup on
    user-index GSI with FilterExpression on event_id (cannot use GetItem
    because it requires both PK and SK, and the endpoint only has event_id).

    Returns 404 (not 403) if the run doesn't belong to the caller (existence-hiding).
    """
    canonical_user_id = await resolve_canonical_user_id(db, current_user.user_id, org_id=current_user.org_id)
    item = service.get_invocation(invocation_id, user_id=canonical_user_id, tenant_id=current_user.org_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Invocation not found")

    # Enrich with cost data
    try:
        cost_map = await get_cost_by_run_ids(db, [item.invocation_id])
        cost_data = cost_map.get(item.invocation_id)
        if cost_data:
            item.total_cost_usd = cost_data["total_cost_usd"]
            item.total_tokens = cost_data["total_tokens"]
            item.call_count = cost_data["call_count"]
    except Exception as exc:
        logger.warning(
            "Failed to enrich detail with cost data",
            extra={"invocation_id": invocation_id, "error": str(exc)},
        )

    return item


# ---------------------------------------------------------------------------
# GET /admin/agent-invocations/chain/{correlation_id} — admin chain view
# NOTE: Must be defined BEFORE /admin/agent-invocations/{invocation_id} so
# FastAPI matches the literal "chain" segment before the catch-all.
# ---------------------------------------------------------------------------


@router.get("/admin/agent-invocations/chain/{correlation_id}", response_model=InvocationChainResponse)
async def get_admin_invocation_chain(
    correlation_id: Annotated[str, Path(description="Correlation ID of the chain to view")],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    service: Annotated[ActivityService, Depends(get_activity_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    tenant_id: Annotated[str | None, Query()] = None,
    include_non_triggering: Annotated[bool, Query()] = False,
) -> InvocationChainResponse:
    """Get the chain view for a specific correlation_id (admin).

    Scoping: org admins see chains within their tenant; platform admins
    can specify any tenant_id.

    Issue #3708: When include_non_triggering is False (default), no_op and
    webhook_received items are excluded — same convention as flat list.
    """
    effective_tenant_id = tenant_id or current_user.org_id
    if not effective_tenant_id or not effective_tenant_id.strip():
        raise HTTPException(status_code=403, detail="An authorized tenant scope is required")
    await access.check_permission(current_user, Permission.ACTIVITY_READ_ALL, target_org_id=effective_tenant_id)

    chain = service.get_chain(
        correlation_id=correlation_id,
        tenant_id=effective_tenant_id,
        include_non_triggering=include_non_triggering,
    )
    return await _enrich_chain_with_cost(db, chain)


# ---------------------------------------------------------------------------
# GET /admin/agent-invocations/{invocation_id} — single-run detail (admin)
# ---------------------------------------------------------------------------


@router.get("/admin/agent-invocations/{invocation_id}", response_model=InvocationItem)
async def get_admin_invocation_detail(
    invocation_id: Annotated[str, Path(description="The invocation ID to fetch detail for")],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    service: Annotated[ActivityService, Depends(get_activity_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    tenant_id: Annotated[str | None, Query()] = None,
) -> InvocationItem:
    """Get a single invocation's full detail (admin).

    Issue #1653: Admin variant scoped by tenant_id.
    """
    effective_tenant_id = tenant_id or current_user.org_id
    if not effective_tenant_id or not effective_tenant_id.strip():
        raise HTTPException(status_code=403, detail="An authorized tenant scope is required")
    await access.check_permission(current_user, Permission.ACTIVITY_READ_ALL, target_org_id=effective_tenant_id)

    item = service.get_invocation(invocation_id, tenant_id=effective_tenant_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Invocation not found")

    # Enrich with cost data
    try:
        cost_map = await get_cost_by_run_ids(db, [item.invocation_id])
        cost_data = cost_map.get(item.invocation_id)
        if cost_data:
            item.total_cost_usd = cost_data["total_cost_usd"]
            item.total_tokens = cost_data["total_tokens"]
            item.call_count = cost_data["call_count"]
    except Exception as exc:
        logger.warning(
            "Failed to enrich admin detail with cost data",
            extra={"invocation_id": invocation_id, "error": str(exc)},
        )

    return item


# ---------------------------------------------------------------------------
# GET /me/agent-invocations/{invocation_id}/transcript — user transcript
# Issue #3069: Serve the S3-stored run transcript, scoped by ownership.
# ---------------------------------------------------------------------------

# Cap transcript response at 5 MB (transcripts are 10–500 KB; cap is a guard).
_TRANSCRIPT_MAX_BYTES = 5 * 1024 * 1024


def _get_s3_client():
    """Get a boto3 S3 client (lazily cached by boto3 internally)."""
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _get_run_logs_bucket() -> str:
    """Resolve the agent run-logs bucket name from env."""
    return os.environ.get("AGENT_RUN_LOGS_BUCKET", "")


async def _fetch_transcript(transcript_key: str) -> str:
    """Fetch transcript markdown from S3.

    Raises HTTPException(404) if the object is missing or bucket is unconfigured.
    Raises HTTPException(502) on unexpected S3 errors.
    """
    bucket = _get_run_logs_bucket()
    if not bucket:
        raise HTTPException(status_code=404, detail="Transcript storage not configured")

    try:
        response = _get_s3_client().get_object(
            Bucket=bucket,
            Key=transcript_key,
        )
        body = response["Body"].read(_TRANSCRIPT_MAX_BYTES)
        return body.decode("utf-8", errors="replace")
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("NoSuchKey", "NoSuchBucket", "AccessDenied"):
            raise HTTPException(status_code=404, detail="Transcript not found") from exc
        logger.warning(
            "S3 error fetching transcript",
            extra={"key": transcript_key, "error": str(exc)},
        )
        raise HTTPException(status_code=502, detail="Failed to retrieve transcript") from exc


@router.get("/me/agent-invocations/{invocation_id}/transcript")
async def get_my_invocation_transcript(
    invocation_id: Annotated[str, Path(description="The invocation ID to fetch transcript for")],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    service: Annotated[ActivityService, Depends(get_activity_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PlainTextResponse:
    """Get the full run transcript for a user's own invocation.

    Issue #3069: Resolves the transcript_key from the invocation row (never from
    the client), then proxies the S3 object. Returns text/markdown.
    """
    canonical_user_id = await resolve_canonical_user_id(db, current_user.user_id, org_id=current_user.org_id)
    item = service.get_invocation(invocation_id, user_id=canonical_user_id, tenant_id=current_user.org_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Invocation not found")

    if not item.transcript_key:
        raise HTTPException(status_code=404, detail="Transcript not available for this invocation")

    content = await _fetch_transcript(item.transcript_key)
    return PlainTextResponse(content=content, media_type="text/markdown")


# ---------------------------------------------------------------------------
# GET /admin/agent-invocations/{invocation_id}/transcript — admin transcript
# Issue #3069: Admin variant, same AuthZ as admin detail.
# ---------------------------------------------------------------------------


@router.get("/admin/agent-invocations/{invocation_id}/transcript")
async def get_admin_invocation_transcript(
    invocation_id: Annotated[str, Path(description="The invocation ID to fetch transcript for")],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    service: Annotated[ActivityService, Depends(get_activity_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    tenant_id: Annotated[str | None, Query()] = None,
) -> PlainTextResponse:
    """Get the full run transcript for an invocation (admin).

    Issue #3069: Admin variant scoped by tenant_id. Same AuthZ as admin detail.
    """
    effective_tenant_id = tenant_id or current_user.org_id
    if not effective_tenant_id or not effective_tenant_id.strip():
        raise HTTPException(status_code=403, detail="An authorized tenant scope is required")
    await access.check_permission(current_user, Permission.ACTIVITY_READ_ALL, target_org_id=effective_tenant_id)

    item = service.get_invocation(invocation_id, tenant_id=effective_tenant_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Invocation not found")

    if not item.transcript_key:
        raise HTTPException(status_code=404, detail="Transcript not available for this invocation")

    content = await _fetch_transcript(item.transcript_key)
    return PlainTextResponse(content=content, media_type="text/markdown")


# ---------------------------------------------------------------------------
# Live run controls — Issue #3960 (S1 foundations)
#
# Browsers reach these under `/api/activity/invocations/{id}/agent/...`; the
# `/api` prefix is stripped by CloudFront before the origin, which is why the
# router is mounted without it (the convention asserted app-wide by
# tests/test_route_prefix_convention.py).
#
# Every handler is a thin adapter over `ControlService`. Nothing here decides who
# may control a run, which status an outcome maps to, or where a request is
# forwarded — those live in the service so this module and
# `orchestration/controls.py` cannot drift into two different answers. The only
# work done here is what genuinely belongs at the HTTP edge: reading the body
# size before parsing, translating the service's typed error into an
# `HTTPException`, and resolving the caller's canonical identity.
# ---------------------------------------------------------------------------


def get_control_service() -> ControlService:
    """Provide the control service; overridden via dependency_overrides in tests."""
    return ControlService()


async def _control_identity(current_user: TokenContext, db: AsyncSession) -> tuple[str, str]:
    """Resolve the (canonical user id, tenant id) the control gate authorizes on.

    The canonical id is required rather than the raw token subject because
    invocation rows are keyed by the canonical `users.id`, not by the Cognito
    sub — the same resolution the detail endpoint performs. `org_id` is used for
    the tenant, never `attributed_org_id`: that field is caller-influenced for
    billing attribution, so authorizing on it would let a caller nominate the
    tenant whose runs they may control.
    """
    canonical_user_id = await resolve_canonical_user_id(db, current_user.user_id, org_id=current_user.org_id)
    return canonical_user_id, current_user.org_id


def _raise_control_error(exc: ControlError) -> None:
    """Translate a service-layer control error into its HTTP response."""
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


async def _validated_command_body(action: str, request: Request) -> str:
    """Read the raw body and validate it through the shared control validator.

    The validation itself — the 413 byte cap applied before parsing, the 400 for
    malformed or non-object bodies, the `extra="forbid"` schema check and the UUID
    requirement — lives in `control_service.validate_command_body`, not here.
    That move is #3960 review finding F1: this module had the only copy, and
    `orchestration/controls.py`'s verb routes take no body parameter, so none of
    it ran on that adapter. Reading the raw bytes is the one part that genuinely
    belongs at the HTTP edge; deciding what a valid command is does not.
    """
    return validate_command_body(action, await request.body())


@router.get("/activity/invocations/{invocation_id}/agent/ping", response_model=ControlPingResponse)
async def ping_invocation_agent(
    invocation_id: Annotated[str, Path(min_length=1, max_length=128)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    control: Annotated[ControlService, Depends(get_control_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ControlPingResponse:
    """Check whether this run's control channel is reachable.

    Side-effect free, which is what makes it safe to ship before any verb works:
    it exercises browser auth, tenant and owner authorization, target validation,
    the NetworkPolicy and the pod's token check without changing run state.
    """
    user_id, tenant_id = await _control_identity(current_user, db)
    try:
        return await control.ping(invocation_id, user_id=user_id, tenant_id=tenant_id)
    except ControlError as exc:
        _raise_control_error(exc)


@router.get("/activity/invocations/{invocation_id}/agent/state", response_model=ControlStateResponse)
async def get_invocation_agent_state(
    invocation_id: Annotated[str, Path(min_length=1, max_length=128)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    control: Annotated[ControlService, Depends(get_control_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ControlStateResponse:
    """Read current control capabilities, phase and bounded command history.

    This is the read contract the dashboard polls. A GET never starts an
    assistant turn and never spends model tokens — it is served from state the
    worker already recorded.
    """
    user_id, tenant_id = await _control_identity(current_user, db)
    try:
        return await control.get_state(invocation_id, user_id=user_id, tenant_id=tenant_id)
    except ControlError as exc:
        _raise_control_error(exc)


@router.get("/activity/invocations/{invocation_id}/agent/events")
async def stream_invocation_explanations(
    invocation_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    control: Annotated[ControlService, Depends(get_control_service)],
):
    from src.activity.explanation_stream import open_explanation_stream
    from src.agentauth.bootstrap import BootstrapRefusedError
    from src.agentauth.human_control import authorize_human_session
    from src.shared.database import get_session_factory

    if request.query_params:
        raise HTTPException(400, "event stream accepts no query parameters")

    async def reauthorize():
        async with get_session_factory()() as db:
            return await authorize_human_session(current_user, db)

    try:
        session = await reauthorize()
        return await open_explanation_stream(
            control, invocation_id, session=session, reauthorize=reauthorize, cursor=request.headers.get("last-event-id")
        )
    except BootstrapRefusedError:
        raise HTTPException(404, "run not found") from None
    except ControlError as exc:
        _raise_control_error(exc)
    except httpx.HTTPError:
        raise HTTPException(503, "live explanations unavailable") from None


@router.post("/activity/invocations/{invocation_id}/agent/{action}")
async def command_invocation_agent(
    invocation_id: Annotated[str, Path(min_length=1, max_length=128)],
    action: Annotated[Literal["pause", "resume", "steer", "abort"], Path()],
    request: Request,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    control: Annotated[ControlService, Depends(get_control_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ControlCommandResponse:
    """Submit a signed human command; acceptance is distinct from application."""
    try:
        await _validated_command_body(action, request)
    except ControlError as exc:
        _raise_control_error(exc)

    from src.agentauth.bootstrap import BootstrapRefusedError
    from src.agentauth.human_control import authorize_human_session

    user_id, tenant_id = await _control_identity(current_user, db)
    try:
        # Preserve the authorization/flag/terminal/verb status ordering before
        # doing the additional signing and live-session checks.
        control.authorize_command(invocation_id, action, user_id=user_id, tenant_id=tenant_id)
        session = await authorize_human_session(current_user, db)
        result, status = await control.command(invocation_id, action, request_body=await request.body(), session=session)
        return JSONResponse(result.model_dump(), status_code=status, headers={"Cache-Control": "no-store"})
    except BootstrapRefusedError:
        raise HTTPException(status_code=404, detail="run not found") from None
    except ControlError as exc:
        _raise_control_error(exc)
