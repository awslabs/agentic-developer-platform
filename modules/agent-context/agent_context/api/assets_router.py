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

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agent_context.api.assets_schemas import (
    AssetCreateRequest,
    AssetDetailResponse,
    AssetListResponse,
    AssetResponse,
    BulkCommitRequest,
    BulkCommitResponse,
    BulkDuplicateItem,
    BulkPreviewResponse,
    QuotaDetail,
    QuotaInfo,
)
from agent_context.api.bulk_parser import MAX_FILE_SIZE_BYTES, MAX_LINES, parse_bulk_file
from agent_context.ingestion.dispatch import dispatch_ingestion
from agent_context.ingestion.type_registry import is_valid_asset_type, validate_source_ref

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
    """Register one asset. Scope from session, soft quota check, dispatch to SQS."""
    # Validate asset_type against registry (§6.4)
    if not is_valid_asset_type(body.asset_type):
        raise HTTPException(
            status_code=400,
            detail=f"Unknown asset_type: '{body.asset_type}'. Supported types: repo, url, doc",
        )

    # Validate source_ref pattern for the type
    if not validate_source_ref(body.asset_type, body.source_ref):
        raise HTTPException(
            status_code=400,
            detail=f"source_ref does not match expected pattern for type '{body.asset_type}'",
        )

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

    # Phase 1 inline dispatch: publish to SQS, update status to 'queued'.
    # Row is at 'registered' — row-before-publish invariant (§8.9).
    # On failure, row stays at 'registered' (recoverable by sweeper/reindex).
    try:
        await dispatch_ingestion(
            asset_id=asset_id,
            asset_type=body.asset_type,
            source_ref=body.source_ref,
            tenant_id=tenant_id,
            owner_sub=owner_sub,
            project_id=None,
            db=db,
        )
    except Exception:
        logger.warning(
            "Ingestion dispatch failed for asset %s — row stays at 'registered'",
            asset_id,
            exc_info=True,
        )

    # Fetch the created row (may be 'registered' or 'queued' depending on dispatch)
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

    # Phase 1 inline dispatch: re-publish to SQS after resetting to 'registered'.
    try:
        await dispatch_ingestion(
            asset_id=asset_id,
            asset_type=row.asset_type,
            source_ref=row.source_ref,
            tenant_id=row.tenant_id,
            owner_sub=row.owner_sub,
            project_id=str(row.project_id) if row.project_id else None,
            db=db,
        )
    except Exception:
        logger.warning(
            "Ingestion dispatch failed for reindex of asset %s — row stays at 'registered'",
            asset_id,
            exc_info=True,
        )

    updated_row = await _fetch_asset_by_id(db, asset_id)
    if not updated_row:
        raise HTTPException(status_code=500, detail="Failed to read updated asset")
    return _row_to_response(updated_row)


# ---------------------------------------------------------------------------
# Bulk Upload — Two-Step: Preview + Commit (§5, §8.3)
# ---------------------------------------------------------------------------


@router.post("/bulk", response_model=BulkPreviewResponse)
async def bulk_preview(
    db: Annotated[AsyncSession, Depends(get_assets_db)],
    current_user: Annotated[Any, Depends(get_current_user_from_state)],
    file: UploadFile = File(...),
    scope: str = Form("tenant"),
) -> BulkPreviewResponse:
    """Parse + validate a bulk upload file. Returns preview — NO DB writes.

    Tenant admin can upload at tenant scope; any user can upload at personal scope.
    """
    # Validate scope value
    if scope not in ("personal", "tenant"):
        raise HTTPException(status_code=400, detail="scope must be 'personal' or 'tenant'")

    # Admin gate for tenant scope
    if scope == "tenant" and not current_user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Tenant-scope bulk upload requires admin privileges",
        )

    # Read and validate file size
    content_bytes = await file.read()
    if len(content_bytes) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum size is {MAX_FILE_SIZE_BYTES // 1024} KB",
        )

    content = content_bytes.decode("utf-8", errors="replace")

    # Check line count
    line_count = len(content.splitlines())
    if line_count > MAX_LINES:
        raise HTTPException(
            status_code=413,
            detail=f"File has {line_count} lines. Maximum is {MAX_LINES}",
        )

    # Parse the file
    valid_items, rejected_items, total_lines, skipped_comments = parse_bulk_file(content)

    # Derive scope for dedup + quota check
    tenant_id = current_user.org_id or None
    owner_sub: str | None = None
    if scope == "personal":
        owner_sub = current_user.user_id

    # Check for duplicates against existing DB rows
    duplicates: list[BulkDuplicateItem] = []
    non_duplicate_valid = []

    for item in valid_items:
        dup_result = await db.execute(
            text("""
                SELECT id FROM knowledge_assets
                WHERE source_ref = :sref
                  AND COALESCE(tenant_id, '') = COALESCE(:tid, '')
                  AND COALESCE(owner_sub, '') = COALESCE(:sub, '')
                  AND status != 'removed'
                LIMIT 1
            """),
            {"sref": item.source_ref, "tid": tenant_id or "", "sub": owner_sub or ""},
        )
        existing = dup_result.fetchone()
        if existing:
            duplicates.append(
                BulkDuplicateItem(
                    line=item.line,
                    source_ref=item.source_ref,
                    existing_id=str(existing.id),
                )
            )
        else:
            non_duplicate_valid.append(item)

    # Quota check: count existing + new per asset_type
    scope_key = scope
    quota_after: dict[str, QuotaDetail] = {}
    quota_ok = True

    # Get existing counts by type
    count_result = await db.execute(
        text("""
            SELECT asset_type, COUNT(*) as cnt
            FROM knowledge_assets
            WHERE COALESCE(tenant_id, '') = COALESCE(:tid, '')
              AND COALESCE(owner_sub, '') = COALESCE(:sub, '')
              AND status != 'removed'
            GROUP BY asset_type
        """),
        {"tid": tenant_id or "", "sub": owner_sub or ""},
    )
    existing_counts: dict[str, int] = {}
    for r in count_result.fetchall():
        existing_counts[r.asset_type] = r.cnt

    # Count new items by type
    new_counts: dict[str, int] = {}
    for item in non_duplicate_valid:
        new_counts[item.asset_type] = new_counts.get(item.asset_type, 0) + 1

    # Check each type's quota
    all_types = set(list(existing_counts.keys()) + list(new_counts.keys()))
    for atype in all_types:
        existing = existing_counts.get(atype, 0)
        new = new_counts.get(atype, 0)
        limit = _get_quota_limit(scope_key, atype)
        after = existing + new
        quota_after[f"{atype}s"] = QuotaDetail(used=after, limit=limit)
        if after > limit:
            quota_ok = False

    return BulkPreviewResponse(
        total_lines=total_lines,
        parsed=len(valid_items) + len(rejected_items),
        skipped_comments=skipped_comments,
        valid=non_duplicate_valid,
        rejected=rejected_items,
        duplicates=duplicates,
        quota_ok=quota_ok,
        quota_after=quota_after,
    )


@router.post("/bulk/commit", response_model=BulkCommitResponse, status_code=201)
async def bulk_commit(
    body: BulkCommitRequest,
    db: Annotated[AsyncSession, Depends(get_assets_db)],
    current_user: Annotated[Any, Depends(get_current_user_from_state)],
) -> BulkCommitResponse:
    """Commit a previewed bulk upload batch. Writes rows + dispatches to SQS.

    Tenant admin can commit at tenant scope; any user can commit at personal scope.
    """
    # Admin gate for tenant scope
    if body.scope == "tenant" and not current_user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Tenant-scope bulk commit requires admin privileges",
        )

    if not body.items:
        raise HTTPException(status_code=400, detail="No items to commit")

    # Enforce file limit on commit as well
    if len(body.items) > MAX_LINES:
        raise HTTPException(
            status_code=413,
            detail=f"Too many items. Maximum is {MAX_LINES}",
        )

    # Derive scope
    tenant_id = current_user.org_id or None
    owner_sub: str | None = None
    scope_key = body.scope
    if body.scope == "personal":
        owner_sub = current_user.user_id

    # Quota check before committing
    count_result = await db.execute(
        text("""
            SELECT asset_type, COUNT(*) as cnt
            FROM knowledge_assets
            WHERE COALESCE(tenant_id, '') = COALESCE(:tid, '')
              AND COALESCE(owner_sub, '') = COALESCE(:sub, '')
              AND status != 'removed'
            GROUP BY asset_type
        """),
        {"tid": tenant_id or "", "sub": owner_sub or ""},
    )
    existing_counts: dict[str, int] = {}
    for r in count_result.fetchall():
        existing_counts[r.asset_type] = r.cnt

    new_counts: dict[str, int] = {}
    for item in body.items:
        new_counts[item.asset_type] = new_counts.get(item.asset_type, 0) + 1

    for atype, new_count in new_counts.items():
        existing = existing_counts.get(atype, 0)
        limit = _get_quota_limit(scope_key, atype)
        if existing + new_count > limit:
            raise HTTPException(
                status_code=429,
                detail={
                    "message": "Quota exceeded",
                    "quota": {
                        atype: {"used": existing, "limit": limit, "requested": new_count},
                    },
                },
            )

    # Insert rows, skipping duplicates
    created_assets: list[AssetResponse] = []
    skipped_duplicates = 0

    for item in body.items:
        # Validate asset_type
        if not is_valid_asset_type(item.asset_type):
            continue  # Skip invalid types silently (should have been caught in preview)

        # Validate source_ref
        if not validate_source_ref(item.asset_type, item.source_ref):
            continue

        # Check duplicate
        dup_result = await db.execute(
            text("""
                SELECT id FROM knowledge_assets
                WHERE source_ref = :sref
                  AND COALESCE(tenant_id, '') = COALESCE(:tid, '')
                  AND COALESCE(owner_sub, '') = COALESCE(:sub, '')
                  AND status != 'removed'
                LIMIT 1
            """),
            {"sref": item.source_ref, "tid": tenant_id or "", "sub": owner_sub or ""},
        )
        if dup_result.fetchone():
            skipped_duplicates += 1
            continue

        # Insert
        asset_id = str(uuid.uuid4())
        await db.execute(
            text("""
                INSERT INTO knowledge_assets
                    (id, asset_type, source_ref, tenant_id, owner_sub, project_id,
                     status, registered_by, metadata, display_name, tags)
                VALUES
                    (:id, :asset_type, :source_ref, :tenant_id, :owner_sub, NULL,
                     'registered', :registered_by, '{}'::jsonb,
                     :display_name, :tags::jsonb)
            """),
            {
                "id": asset_id,
                "asset_type": item.asset_type,
                "source_ref": item.source_ref,
                "tenant_id": tenant_id,
                "owner_sub": owner_sub,
                "registered_by": current_user.user_id,
                "display_name": item.display_name,
                "tags": _json_dumps(item.tags),
            },
        )

        # Dispatch to SQS (Phase 1 inline dispatch)
        try:
            await dispatch_ingestion(
                asset_id=asset_id,
                asset_type=item.asset_type,
                source_ref=item.source_ref,
                tenant_id=tenant_id,
                owner_sub=owner_sub,
                project_id=None,
                db=db,
            )
        except Exception:
            logger.warning(
                "Ingestion dispatch failed for bulk asset %s — row stays at 'registered'",
                asset_id,
                exc_info=True,
            )

        # Fetch the created row
        row = await _fetch_asset_by_id(db, asset_id)
        if row:
            created_assets.append(_row_to_response(row))

    await db.commit()

    return BulkCommitResponse(
        created=len(created_assets),
        skipped_duplicates=skipped_duplicates,
        assets=created_assets,
    )


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
