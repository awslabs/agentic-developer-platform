"""Knowledge-assets registry CRUD API router.

Issue #1791 (Story B of E10 #1736): register/list/detail/delete/reindex endpoints.
Scope derived from the authenticated session (TokenContext), soft quota check.

Route-ownership: this router is DEFINED in agent-context, MOUNTED by the gateway
via conditional include_router behind AGENT_CONTEXT_ENABLED. The gateway provides
Cognito JWT auth + get_current_user guard; this module provides the query logic.

Database dependency: the gateway overrides `get_assets_db` at mount time to inject
its own session factory (same Postgres instance, agent_context database).
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agent_context.api.assets_schemas import (
    AssetCreateRequest,
    AssetDetailResponse,
    AssetListResponse,
    AssetResponse,
    QuotaDetail,
    QuotaInfo,
)

logger = logging.getLogger("agent_context.api.assets")

router = APIRouter(prefix="/api/agent-context/assets", tags=["knowledge-assets"])

# ---------------------------------------------------------------------------
# Quota defaults (overridable via env / SSM)
# ---------------------------------------------------------------------------

DEFAULT_QUOTAS: dict[str, dict[str, int]] = {
    "personal": {"repo": 20, "url": 50, "doc": 20},
    "tenant": {"repo": 200, "url": 500, "doc": 200},
}


def _get_quota_limit(scope: str, asset_type: str) -> int:
    """Return the quota limit for a given scope and asset_type."""
    env_key = f"ASSET_QUOTA_{scope.upper()}_{asset_type.upper()}"
    env_val = os.environ.get(env_key)
    if env_val and env_val.isdigit():
        return int(env_val)
    return DEFAULT_QUOTAS.get(scope, {}).get(asset_type, 50)


# ---------------------------------------------------------------------------
# Placeholder DB dependency — overridden by the gateway at mount time
# ---------------------------------------------------------------------------


async def get_assets_db() -> AsyncSession:  # type: ignore[return]
    """Placeholder DB dependency — overridden by the gateway at mount time.

    The gateway calls `app.dependency_overrides[get_assets_db] = get_db`
    so that this router receives the gateway's async DB session.
    """
    raise HTTPException(
        status_code=503,
        detail="Agent-context database dependency not configured",
    )


# ---------------------------------------------------------------------------
# TokenContext placeholder — gateway injects the real user via Depends
# ---------------------------------------------------------------------------
# The gateway mounts this router with:
#   dependencies=[Depends(get_current_user)]
# and each endpoint receives the user via a dependency that reads
# request.state.token_context. We define a lightweight placeholder here
# so the router can import without gateway dependencies.


class _TokenContextPlaceholder:
    """Minimal shape expected from the gateway's TokenContext."""

    user_id: str = ""
    org_id: str = ""
    is_admin: bool = False


async def get_current_user_from_state() -> Any:
    """Placeholder — overridden by the gateway at mount time."""
    raise HTTPException(status_code=503, detail="Auth not configured")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=AssetResponse, status_code=201)
async def register_asset(
    body: AssetCreateRequest,
    db: Annotated[AsyncSession, Depends(get_assets_db)],
    current_user: Annotated[Any, Depends(get_current_user_from_state)],
) -> AssetResponse:
    """Register one asset. Scope from session, soft quota check."""
    # Derive scope from session
    tenant_id = current_user.org_id or None
    owner_sub: str | None = None
    scope_key = body.scope

    if body.scope == "personal":
        owner_sub = current_user.user_id
    elif body.scope == "tenant":
        if not current_user.is_admin:
            raise HTTPException(
                status_code=403,
                detail="Tenant-scope registration requires admin privileges",
            )

    # Soft quota check
    quota_limit = _get_quota_limit(scope_key, body.asset_type)
    count_result = await db.execute(
        text("""
            SELECT COUNT(*) FROM knowledge_assets
            WHERE tenant_id = :tid
              AND COALESCE(owner_sub, '') = COALESCE(:sub, '')
              AND asset_type = :atype
              AND status != 'removed'
        """),
        {"tid": tenant_id or "", "sub": owner_sub or "", "atype": body.asset_type},
    )
    current_count = count_result.scalar() or 0

    if current_count >= quota_limit:
        raise HTTPException(
            status_code=429,
            detail={
                "message": "Quota exceeded",
                "quota": {
                    body.asset_type: {"used": current_count, "limit": quota_limit},
                },
            },
        )

    # Check for duplicate (same source_ref in same scope)
    dup_result = await db.execute(
        text("""
            SELECT id FROM knowledge_assets
            WHERE source_ref = :sref
              AND COALESCE(tenant_id, '') = COALESCE(:tid, '')
              AND COALESCE(owner_sub, '') = COALESCE(:sub, '')
              AND status != 'removed'
            LIMIT 1
        """),
        {"sref": body.source_ref, "tid": tenant_id or "", "sub": owner_sub or ""},
    )
    existing = dup_result.fetchone()
    if existing:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Asset already registered under this scope",
                "existing_id": str(existing.id),
            },
        )

    # Insert
    asset_id = str(uuid.uuid4())
    await db.execute(
        text("""
            INSERT INTO knowledge_assets
                (id, asset_type, source_ref, tenant_id, owner_sub, project_id,
                 status, registered_by, metadata, display_name, tags)
            VALUES
                (:id, :asset_type, :source_ref, :tenant_id, :owner_sub, NULL,
                 'registered', :registered_by, :metadata::jsonb,
                 :display_name, :tags::jsonb)
        """),
        {
            "id": asset_id,
            "asset_type": body.asset_type,
            "source_ref": body.source_ref,
            "tenant_id": tenant_id,
            "owner_sub": owner_sub,
            "registered_by": current_user.user_id,
            "metadata": _json_dumps(body.metadata),
            "display_name": body.display_name,
            "tags": _json_dumps(body.tags),
        },
    )
    await db.commit()

    # Fetch the created row
    row = await _fetch_asset_by_id(db, asset_id)
    if not row:
        raise HTTPException(status_code=500, detail="Failed to read created asset")
    return _row_to_response(row)


@router.get("", response_model=AssetListResponse)
async def list_assets(
    db: Annotated[AsyncSession, Depends(get_assets_db)],
    current_user: Annotated[Any, Depends(get_current_user_from_state)],
    scope: Annotated[str | None, Query()] = None,
    asset_type: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> AssetListResponse:
    """List/filter assets for caller's scope."""
    conditions: list[str] = ["status != 'removed'"]
    params: dict[str, Any] = {}

    # Scope filtering
    if scope == "personal":
        conditions.append("owner_sub = :sub")
        params["sub"] = current_user.user_id
        conditions.append("tenant_id = :tid")
        params["tid"] = current_user.org_id
    elif scope == "tenant":
        conditions.append("tenant_id = :tid")
        params["tid"] = current_user.org_id
        conditions.append("owner_sub IS NULL")
    else:
        # Default: show all assets the user can see (personal + tenant)
        conditions.append("tenant_id = :tid")
        params["tid"] = current_user.org_id

    if asset_type:
        conditions.append("asset_type = :atype")
        params["atype"] = asset_type
    if status:
        conditions.append("status = :status")
        params["status"] = status

    where_clause = " AND ".join(conditions)

    # Count
    count_result = await db.execute(
        text(f"SELECT COUNT(*) FROM knowledge_assets WHERE {where_clause}"),
        params,
    )
    total = count_result.scalar() or 0

    # Fetch page
    offset = (page - 1) * page_size
    params["limit"] = page_size
    params["offset"] = offset
    rows_result = await db.execute(
        text(f"""
            SELECT id, asset_type, source_ref, display_name, tags, metadata,
                   tenant_id, owner_sub, project_id, status, last_error,
                   retry_count, registered_by, created_at, updated_at
            FROM knowledge_assets
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    )
    rows = rows_result.fetchall()

    items = [_row_to_response(r) for r in rows]

    # Quota info
    quota = await _get_quota_info(db, current_user)

    return AssetListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_more=(offset + page_size) < total,
        quota=quota,
    )


@router.get("/{asset_id}", response_model=AssetDetailResponse)
async def get_asset_detail(
    asset_id: str,
    db: Annotated[AsyncSession, Depends(get_assets_db)],
    current_user: Annotated[Any, Depends(get_current_user_from_state)],
) -> AssetDetailResponse:
    """Get asset detail. Only visible if caller has scope access."""
    row = await _fetch_asset_by_id(db, asset_id)
    if not row:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Scope check: user must be in the same tenant
    if row.tenant_id and row.tenant_id != current_user.org_id:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Personal assets: only visible to owner or admin
    if row.owner_sub and row.owner_sub != current_user.user_id and not current_user.is_admin:
        raise HTTPException(status_code=404, detail="Asset not found")

    return AssetDetailResponse(**_row_to_response(row).model_dump())


@router.delete("/{asset_id}", status_code=204)
async def delete_asset(
    asset_id: str,
    db: Annotated[AsyncSession, Depends(get_assets_db)],
    current_user: Annotated[Any, Depends(get_current_user_from_state)],
) -> None:
    """Soft-delete asset (status = 'removed'). Owner or tenant admin."""
    row = await _fetch_asset_by_id(db, asset_id)
    if not row:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Authorization: owner or tenant admin
    _authorize_modify(row, current_user)

    await db.execute(
        text("""
            UPDATE knowledge_assets
            SET status = 'removed', updated_at = NOW()
            WHERE id = :id
        """),
        {"id": asset_id},
    )
    await db.commit()


@router.post("/{asset_id}/reindex", response_model=AssetResponse)
async def reindex_asset(
    asset_id: str,
    db: Annotated[AsyncSession, Depends(get_assets_db)],
    current_user: Annotated[Any, Depends(get_current_user_from_state)],
) -> AssetResponse:
    """Re-queue asset for indexing: set status = 'registered'. Owner or admin."""
    row = await _fetch_asset_by_id(db, asset_id)
    if not row:
        raise HTTPException(status_code=404, detail="Asset not found")

    _authorize_modify(row, current_user)

    await db.execute(
        text("""
            UPDATE knowledge_assets
            SET status = 'registered', last_error = NULL, updated_at = NOW()
            WHERE id = :id
        """),
        {"id": asset_id},
    )
    await db.commit()

    updated_row = await _fetch_asset_by_id(db, asset_id)
    if not updated_row:
        raise HTTPException(status_code=500, detail="Failed to read updated asset")
    return _row_to_response(updated_row)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _authorize_modify(row: Any, current_user: Any) -> None:
    """Check that the current user can modify (delete/reindex) the asset."""
    # Tenant check
    if row.tenant_id and row.tenant_id != current_user.org_id:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Owner or admin can modify
    is_owner = row.registered_by == current_user.user_id
    is_tenant_admin = current_user.is_admin and (row.tenant_id == current_user.org_id)
    if not is_owner and not is_tenant_admin:
        raise HTTPException(
            status_code=403,
            detail="Only the asset owner or a tenant admin can perform this action",
        )


async def _fetch_asset_by_id(db: AsyncSession, asset_id: str) -> Any | None:
    """Fetch a single asset row by ID."""
    result = await db.execute(
        text("""
            SELECT id, asset_type, source_ref, display_name, tags, metadata,
                   tenant_id, owner_sub, project_id, status, last_error,
                   retry_count, registered_by, created_at, updated_at
            FROM knowledge_assets
            WHERE id = :id
        """),
        {"id": asset_id},
    )
    return result.fetchone()


def _row_to_response(row: Any) -> AssetResponse:
    """Convert a DB row to an AssetResponse."""
    return AssetResponse(
        id=str(row.id),
        asset_type=row.asset_type,
        source_ref=row.source_ref,
        display_name=row.display_name,
        tags=row.tags if row.tags else {},
        metadata=row.metadata if row.metadata else {},
        tenant_id=row.tenant_id,
        owner_sub=row.owner_sub,
        project_id=str(row.project_id) if row.project_id else None,
        status=row.status,
        last_error=row.last_error,
        retry_count=row.retry_count,
        registered_by=row.registered_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _get_quota_info(db: AsyncSession, current_user: Any) -> QuotaInfo:
    """Compute quota usage for the current user's scope."""
    # Count assets per type for personal scope
    result = await db.execute(
        text("""
            SELECT asset_type, COUNT(*) as cnt
            FROM knowledge_assets
            WHERE tenant_id = :tid
              AND (owner_sub = :sub OR owner_sub IS NULL)
              AND status != 'removed'
            GROUP BY asset_type
        """),
        {"tid": current_user.org_id, "sub": current_user.user_id},
    )
    counts: dict[str, int] = {}
    for r in result.fetchall():
        counts[r.asset_type] = r.cnt

    scope_key = "personal"
    return QuotaInfo(
        repos=QuotaDetail(
            used=counts.get("repo", 0),
            limit=_get_quota_limit(scope_key, "repo"),
        ),
        urls=QuotaDetail(
            used=counts.get("url", 0),
            limit=_get_quota_limit(scope_key, "url"),
        ),
        docs=QuotaDetail(
            used=counts.get("doc", 0),
            limit=_get_quota_limit(scope_key, "doc"),
        ),
    )


def _json_dumps(obj: dict[str, Any]) -> str:
    """Serialize dict to JSON string for Postgres JSONB cast."""
    import json

    return json.dumps(obj)
