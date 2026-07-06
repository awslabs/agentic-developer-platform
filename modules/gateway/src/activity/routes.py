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
from typing import Annotated, Literal

import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.activity.cost_service import get_cost_by_run_ids
from src.activity.schemas import (
    ChainListResponse,
    InvocationChainItem,
    InvocationChainResponse,
    InvocationItem,
    InvocationListResponse,
)
from src.activity.service import ActivityService
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
    canonical_user_id = await resolve_canonical_user_id(db, current_user.user_id)
    try:
        if view == "chains":
            chain_result = service.query_chains_by_user(
                user_id=canonical_user_id,
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
) -> InvocationChainResponse:
    """Get the chain view for a specific correlation_id.

    Scoping: only returns invocations the caller owns (canonical user_id derived
    from the token's Cognito sub). Shows the entire chain the caller roots or
    participates in.
    """
    canonical_user_id = await resolve_canonical_user_id(db, current_user.user_id)
    chain = service.get_chain(
        correlation_id=correlation_id,
        user_id=canonical_user_id,
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
    canonical_user_id = await resolve_canonical_user_id(db, current_user.user_id)
    item = service.get_invocation(invocation_id, user_id=canonical_user_id)
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

    chain = service.get_chain(
        correlation_id=correlation_id,
        tenant_id=effective_tenant_id,
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
    await access.check_permission(current_user, Permission.USAGE_READ, target_org_id=tenant_id)

    if current_user.is_admin and tenant_id:
        effective_tenant_id = tenant_id
    else:
        effective_tenant_id = current_user.org_id

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
    canonical_user_id = await resolve_canonical_user_id(db, current_user.user_id)
    item = service.get_invocation(invocation_id, user_id=canonical_user_id)
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
    await access.check_permission(current_user, Permission.USAGE_READ, target_org_id=tenant_id)

    if current_user.is_admin and tenant_id:
        effective_tenant_id = tenant_id
    else:
        effective_tenant_id = current_user.org_id

    item = service.get_invocation(invocation_id, tenant_id=effective_tenant_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Invocation not found")

    if not item.transcript_key:
        raise HTTPException(status_code=404, detail="Transcript not available for this invocation")

    content = await _fetch_transcript(item.transcript_key)
    return PlainTextResponse(content=content, media_type="text/markdown")
