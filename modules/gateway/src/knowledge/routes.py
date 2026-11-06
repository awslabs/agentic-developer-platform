"""Knowledge-assets registry CRUD API router.

Issue #2045: Relocated from agent_context.api.assets_router into the gateway.
Original: Issue #1791 (Story B of E10 #1736).

Register/list/detail/delete/reindex endpoints + bulk upload (preview + commit).
Scope derived from the authenticated session (TokenContext), soft quota check.

Database dependency: uses get_agent_context_db (targets agent_context DB, Issue #2182).
Auth: uses the gateway's get_current_user dependency directly.

Gating: this module's router is only exposed when AGENT_CONTEXT_ENABLED=true.
The UNIT_MODULES auto-discovery loop checks `hasattr(module, "router")` — when
the gate is off, `router` is not defined so the routes are silently skipped.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user
from src.knowledge.accessibility import (
    AccessibilityResult,
    validate_repo_accessibility,
)
from src.knowledge.bulk_parser import MAX_FILE_SIZE_BYTES, MAX_LINES, parse_bulk_file
from src.knowledge.dispatch import (
    IngestionQueueUnavailableError,
    dispatch_ingestion,
    extract_source_identifier,
)
from src.knowledge.product_access import ProductAccess, require_product_access
from src.knowledge.product_source_routes import router as product_source_router
from src.knowledge.product_storage_routes import router as product_storage_router
from src.knowledge.schemas import (
    AssetCreateRequest,
    AssetDetailResponse,
    AssetIndexStage,
    AssetListResponse,
    AssetResponse,
    AssetStatusResponse,
    BulkCommitRequest,
    BulkCommitResponse,
    BulkDuplicateItem,
    BulkPreviewResponse,
    QuotaDetail,
    QuotaInfo,
)
from src.knowledge.search import KnowledgeSearchRequest, KnowledgeSearchResponse, search_knowledge
from src.knowledge.source_admission import SourceAdmissionError, admit_source
from src.knowledge.type_registry import is_valid_asset_type, validate_source_ref
from src.shared.database import get_db
from src.shared.database_agent_context import get_agent_context_db
from src.shared.identity import resolve_canonical_user_id
from src.shared.models.base import utcnow

logger = logging.getLogger("bedrockgateway.knowledge.routes")

_router = APIRouter(prefix="/agent-context/assets", tags=["knowledge-assets"])
_cli_router = APIRouter(prefix="/agent-context/assets", tags=["knowledge-assets"])
_search_router = APIRouter(prefix="/agent-context/assets", tags=["knowledge-assets"])

# ---------------------------------------------------------------------------
# Quota defaults (overridable via env / SSM)
# ---------------------------------------------------------------------------

DEFAULT_QUOTAS: dict[str, dict[str, int]] = {
    "personal": {"repo": 20, "url": 50, "doc": 20},
    "tenant": {"repo": 200, "url": 500, "doc": 200},
    "product": {"repo": 200, "url": 500, "doc": 200},
}


def _get_quota_limit(scope: str, asset_type: str) -> int:
    """Return the quota limit for a given scope and asset_type."""
    env_key = f"ASSET_QUOTA_{scope.upper()}_{asset_type.upper()}"
    env_val = os.environ.get(env_key)
    if env_val and env_val.isdigit():
        return int(env_val)
    return DEFAULT_QUOTAS.get(scope, {}).get(asset_type, 50)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@_router.post("", response_model=AssetResponse, status_code=201)
async def register_asset(
    body: AssetCreateRequest,
    db: Annotated[AsyncSession, Depends(get_agent_context_db)],
    gateway_db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
) -> AssetResponse:
    """Register one asset. Scope from session, soft quota check, dispatch to SQS."""
    # Validate asset_type against registry
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

    # --- Register-time accessibility validation (Issue #2087) ---
    # For repos: determine public/private, resolve installation, verify ownership.
    accessibility: AccessibilityResult | None = None
    product_source = None
    upload = None
    if body.source_binding_id:
        from src.knowledge.product_source_routes import require_source_storage
        from src.knowledge.product_sources import grant_source

        access = await require_product_access(gateway_db, current_user, body.product_id, write=True, lock=True)
        await _require_product_storage(db)
        await require_source_storage(db)
        product_source = await grant_source(gateway_db, current_user.org_id, body.product_id, body.source_binding_id, access.member_id)
        if body.source_ref.casefold() != product_source["source_ref"].casefold():
            raise HTTPException(409, detail="The repository changed. Refresh Product sources before adding it.")
        product_source["configuration_revision"] = 1
        product_source["refresh_updated_by"] = access.member_id
        body.source_ref = product_source["source_ref"]
    elif body.asset_type == "repo":
        accessibility = await validate_repo_accessibility(
            body.source_ref,
            current_user.org_id or "",
            db=db,
            gateway_db=gateway_db,
            caller_user_id=current_user.user_id,
        )
        if not accessibility.allowed:
            raise HTTPException(
                status_code=accessibility.error_code,
                detail=accessibility.error_message,
            )

    # Source access authorizes import, never silently widens the requested scope.
    tenant_id = current_user.org_id or None
    owner_sub: str | None = None
    scope_key = body.scope
    installation_id = product_source["installation_id"] if product_source else accessibility.installation_id if accessibility else None
    if body.scope == "product":
        access = await require_product_access(gateway_db, current_user, body.product_id, write=True, lock=True)
        await _require_product_storage(db)
    elif body.scope == "personal":
        _require_scoped_imports()
        owner_sub = await resolve_canonical_user_id(gateway_db, current_user.user_id)
    elif body.scope == "tenant":
        # Preserve the explicit legacy tenant import policy: verified members
        # may choose the org owning the installation; personal imports never do.
        if accessibility and accessibility.tenant_id:
            tenant_id = accessibility.tenant_id
        elif not current_user.is_admin:
            raise HTTPException(403, detail="Tenant-scope registration requires admin privileges")

    # Both single and bulk registration share the verified-upload boundary.
    from src.knowledge.product_storage import require_verified_import

    require_verified_import(body.source_ref, tenant_id, body.product_id, upload_id=body.source_upload_id)

    if body.source_upload_id:
        from src.knowledge.product_storage import load_storage
        from src.knowledge.product_storage_routes import upload_record

        configured_storage = await load_storage(db, tenant_id, body.product_id)
        upload = await upload_record(db, tenant_id, body.product_id, body.source_upload_id, access)
        if (
            upload["asset_id"]
            or not upload["source_version_id"]
            or upload["expires_at"] <= utcnow()
            or upload["bucket"] != configured_storage.bucket
            or body.source_ref != f"s3://{upload['bucket']}/{upload['object_key']}"
        ):
            raise HTTPException(409, "Complete and verify the original upload before registering it")

    try:
        await admit_source(body.asset_type, body.source_ref, tenant_id, owner_sub, product_id=body.product_id)
    except SourceAdmissionError as exc:
        raise HTTPException(status_code=400, detail=f"Source admission refused: {exc}") from exc

    # Soft quota check
    quota_limit = _get_quota_limit(scope_key, body.asset_type)
    count_result = await db.execute(
        text("""
            SELECT COUNT(*) FROM knowledge_assets
            WHERE tenant_id = :tid
              AND COALESCE(owner_sub, '') = COALESCE(:sub, '')
              AND (to_jsonb(knowledge_assets)->>'product_id') IS NOT DISTINCT FROM CAST(:pid AS text)
              AND asset_type = :atype
              AND status != 'removed'
        """),
        {"tid": tenant_id or "", "sub": owner_sub or "", "pid": body.product_id, "atype": body.asset_type},
    )
    current_count = count_result.scalar() or 0

    quota_detail = {"message": "Quota exceeded", "quota": {body.asset_type: {"used": current_count, "limit": quota_limit}}}
    if current_count >= quota_limit and not product_source:
        raise HTTPException(
            status_code=429,
            detail=quota_detail,
        )

    # Check for duplicate (same source_ref in same scope)
    dup_result = await db.execute(
        text("""
            SELECT id FROM knowledge_assets
            WHERE (source_ref = :sref OR (
                CAST(:binding_id AS text) IS NOT NULL
                AND to_jsonb(knowledge_assets)#>>'{product_source,binding_id}' = CAST(:binding_id AS text)
              ))
              AND COALESCE(tenant_id, '') = COALESCE(:tid, '')
              AND COALESCE(owner_sub, '') = COALESCE(:sub, '')
              AND (to_jsonb(knowledge_assets)->>'product_id') IS NOT DISTINCT FROM CAST(:pid AS text)
              AND status != 'removed'
            LIMIT 1
        """),
        {"sref": body.source_ref, "tid": tenant_id or "", "sub": owner_sub or "", "pid": body.product_id, "binding_id": body.source_binding_id},
    )
    existing = dup_result.fetchone()
    if existing:
        if product_source:
            # Attach an explicit connection to an existing Product import only
            # through its current management permission. Reuse the corpus.
            row = await _fetch_asset_by_id(db, str(existing.id))
            if not _can_manage(row, current_user, access):
                raise HTTPException(403, detail="Only the contributor or Product owner can connect this existing information")
            previous = getattr(row, "product_source", None)
            if previous:
                identity_fields = (
                    "binding_id",
                    "binding_generation",
                    "connection_id",
                    "installation_id",
                    "repository_id",
                    "namespace",
                    "relationship_id",
                    "app_id",
                    "source_ref",
                )
                if all(previous.get(key) == product_source.get(key) for key in identity_fields):
                    return _row_to_response(row, can_manage=True)
                product_source["configuration_revision"] = int(previous["configuration_revision"]) + 1
            if row.status not in {"indexed", "complete", "partial", "failed"}:
                raise HTTPException(409, detail="Wait for the current ingestion to finish before connecting this source.")
            attached = await db.execute(
                text("""UPDATE knowledge_assets
                SET product_source=CAST(:grant AS jsonb), refresh_enabled=:enabled, installation_id=:installation, source_ref=:source_ref,
                    refresh_error=NULL,
                    last_refreshed_at=COALESCE(last_refreshed_at, CASE WHEN status IN ('indexed','complete') THEN updated_at ELSE NULL END)
                WHERE id=:id AND status=:status AND updated_at=:updated
                  AND product_source IS NOT DISTINCT FROM CAST(:previous AS jsonb) RETURNING id"""),
                {
                    "id": str(row.id),
                    "grant": json.dumps(product_source),
                    "enabled": body.keep_updated,
                    "installation": installation_id,
                    "source_ref": body.source_ref,
                    "status": row.status,
                    "updated": row.updated_at,
                    "previous": json.dumps(previous) if previous is not None else None,
                },
            )
            if attached.first() is None:
                await db.rollback()
                raise HTTPException(409, detail="This information changed. Reload before connecting it.")
            await db.commit()
            return _row_to_response(await _fetch_asset_by_id(db, str(row.id)), can_manage=True)
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Asset already registered under this scope",
                "existing_id": str(existing.id),
            },
        )

    # Insert (includes installation_id from accessibility validation — Issue #2087)
    if current_count >= quota_limit:
        raise HTTPException(status_code=429, detail=quota_detail)
    asset_id = str(uuid.uuid4())
    try:
        await db.execute(
            text(f"""
                INSERT INTO knowledge_assets
                    (id, asset_type, source_ref, tenant_id, owner_sub, project_id,
                     status, registered_by, metadata, display_name, tags,
                     installation_id{", product_id" if body.product_id else ""}{", product_source, refresh_enabled" if product_source else ""}
                     {", source_upload_id, source_object_version, refresh_enabled" if upload else ""})
                VALUES
                    (:id, :asset_type, :source_ref, :tenant_id, :owner_sub, NULL,
                     'registered', :registered_by, CAST(:metadata AS jsonb),
                     :display_name, CAST(:tags AS jsonb), :installation_id{", :product_id" if body.product_id else ""}
                     {", CAST(:product_source AS jsonb), :refresh_enabled" if product_source else ""}
                     {", CAST(:source_upload_id AS uuid), :source_version, false" if upload else ""})
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
                "installation_id": installation_id,
                "product_id": body.product_id,
                "product_source": json.dumps(product_source),
                "refresh_enabled": body.keep_updated,
                "source_upload_id": body.source_upload_id,
                "source_version": upload["source_version_id"] if upload else None,
            },
        )
        if upload:
            await db.execute(
                text("UPDATE product_context_uploads SET asset_id=CAST(:asset AS uuid) WHERE id=CAST(:upload AS uuid)"),
                {"asset": asset_id, "upload": body.source_upload_id},
            )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if any(name in (str(exc.orig) or "") for name in ("uq_knowledge_assets_source_scope", "uq_knowledge_product_source")):
            # Race condition: another request inserted the same source/scope
            # between our dup-check and INSERT. Return the same 409 as above.
            logger.warning(
                "IntegrityError race on register_asset source_ref=%s — returning 409",
                body.source_ref,
            )
            dup_row = await db.execute(
                text("""
                    SELECT id FROM knowledge_assets
                    WHERE source_ref = :sref
                      AND COALESCE(tenant_id, '') = COALESCE(:tid, '')
                      AND COALESCE(owner_sub, '') = COALESCE(:sub, '')
                      AND (to_jsonb(knowledge_assets)->>'product_id') IS NOT DISTINCT FROM CAST(:pid AS text)
                      AND status != 'removed'
                    LIMIT 1
                """),
                {"sref": body.source_ref, "tid": tenant_id or "", "sub": owner_sub or "", "pid": body.product_id},
            )
            dup = dup_row.fetchone()
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Asset already registered under this scope",
                    "existing_id": str(dup.id) if dup else None,
                },
            ) from exc
        raise

    # Phase 1 inline dispatch: publish to SQS, update status to 'queued'.
    # Row is at 'registered' — row-before-publish invariant.
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
            installation_id=installation_id,
            product_id=body.product_id,
        )
    except IngestionQueueUnavailableError:
        raise HTTPException(
            status_code=503,
            detail="Context ingestion queue is not configured. Set INGESTION_QUEUE_URL to enable asset dispatch.",
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
    return _row_to_response(row, can_manage=True)


@_router.get("", response_model=AssetListResponse)
async def list_assets(
    db: Annotated[AsyncSession, Depends(get_agent_context_db)],
    gateway_db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
    scope: Annotated[str | None, Query(pattern=r"^(personal|tenant|product|all)$")] = None,
    product_id: Annotated[str | None, Query(max_length=36)] = None,
    search: Annotated[str | None, Query(max_length=512)] = None,
    asset_type: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> AssetListResponse:
    """List/filter assets for caller's scope."""
    conditions: list[str] = ["status != 'removed'"]
    params: dict[str, Any] = {}

    if (scope == "product") != bool(product_id):
        raise HTTPException(422, detail="product_id is required only for Product scope")
    access = None
    if product_id:
        access = await require_product_access(gateway_db, current_user, product_id)
        conditions.extend(["tenant_id = :tid", "(to_jsonb(knowledge_assets)->>'product_id') = :pid"])
        params.update(tid=current_user.org_id, pid=product_id)
    else:
        conditions.append("(to_jsonb(knowledge_assets)->>'product_id') IS NULL")

    # Scope filtering — resolve canonical id for personal scope (#2047/#1319)
    if scope == "product":
        pass
    elif scope == "personal":
        canonical_sub = await resolve_canonical_user_id(gateway_db, current_user.user_id)
        conditions.append("owner_sub = :sub")
        params["sub"] = canonical_sub
        conditions.append("tenant_id = :tid")
        params["tid"] = current_user.org_id
    elif scope == "tenant":
        conditions.append("tenant_id = :tid")
        params["tid"] = current_user.org_id
        conditions.append("owner_sub IS NULL")
    else:
        # Default ("All"): everything the user can see — their tenant's assets
        # AND legacy shared assets (tenant_id IS NULL). Their existing
        # ownership remains unchanged when new imports become explicitly scoped.
        conditions.append("(tenant_id = :tid OR tenant_id IS NULL)")
        params["tid"] = current_user.org_id
        # Apply the same personal-owner check as the detail route before
        # counting or paginating; a tenant match does not grant personal access.
        canonical_sub = await resolve_canonical_user_id(gateway_db, current_user.user_id)
        conditions.append("(owner_sub = :sub OR owner_sub IS NULL)")
        params["sub"] = canonical_sub

    if search and search.strip():
        conditions.append("(display_name ILIKE :search OR source_ref ILIKE :search)")
        params["search"] = "%" + search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    if asset_type:
        conditions.append("asset_type = :atype")
        params["atype"] = asset_type
    if status:
        conditions.append("status = :status")
        params["status"] = status

    where_clause = " AND ".join(conditions)

    # Count
    count_result = await db.execute(
        text(f"SELECT COUNT(*) FROM knowledge_assets WHERE {where_clause}"),  # nosemgrep: avoid-sqlalchemy-text
        params,
    )
    total = count_result.scalar() or 0

    # Fetch page
    offset = (page - 1) * page_size
    params["limit"] = page_size
    params["offset"] = offset
    rows_result = await db.execute(
        # nosemgrep: avoid-sqlalchemy-text
        text(f"""
            SELECT id, asset_type, source_ref, display_name, tags, metadata,
                   tenant_id, owner_sub, project_id, to_jsonb(knowledge_assets)->>'product_id' AS product_id, status, last_error,
                   retry_count, registered_by, created_at, updated_at
            FROM knowledge_assets
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    )
    rows = rows_result.fetchall()

    items = [_row_to_response(r, can_manage=_can_manage(r, current_user, access)) for r in rows]

    # Quota info
    quota = await _get_quota_info(db, gateway_db, current_user, scope=scope or "personal", product_id=product_id)

    return AssetListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_more=(offset + page_size) < total,
        quota=quota,
    )


@_search_router.post("/search", response_model=KnowledgeSearchResponse)
async def search_assets(
    body: KnowledgeSearchRequest,
    db: Annotated[AsyncSession, Depends(get_agent_context_db)],
    gateway_db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
    response: Response,
    request: Request,
) -> KnowledgeSearchResponse:
    """Search indexed content in personal or current Product knowledge."""
    response.headers["Cache-Control"] = "no-store"
    return await search_knowledge(body, db, gateway_db, current_user, request)


@_router.get("/{asset_id}", response_model=AssetDetailResponse)
async def get_asset_detail(
    asset_id: str,
    db: Annotated[AsyncSession, Depends(get_agent_context_db)],
    gateway_db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
) -> AssetDetailResponse:
    """Get asset detail. Only visible if caller has scope access."""
    row = await _fetch_asset_by_id(db, asset_id)
    if not row:
        raise HTTPException(status_code=404, detail="Asset not found")

    access = await _authorize_read(row, current_user, gateway_db)

    return AssetDetailResponse(**_row_to_response(row, can_manage=_can_manage(row, current_user, access)).model_dump())


@_router.delete("/{asset_id}", status_code=204)
async def delete_asset(
    asset_id: str,
    db: Annotated[AsyncSession, Depends(get_agent_context_db)],
    gateway_db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
) -> None:
    """Soft-delete asset (status = 'removed'). Owner or tenant admin."""
    row = await _fetch_asset_by_id(db, asset_id)
    if not row:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Authorization: owner or tenant admin
    access = await _authorize_read(row, current_user, gateway_db, write=True)
    if not _can_manage(row, current_user, access):
        raise HTTPException(403, detail="Only the contributor or Product owner can manage this information")

    await db.execute(
        text("""
            UPDATE knowledge_assets
            SET status = 'removed', updated_at = NOW()
            WHERE id = :id
        """),
        {"id": asset_id},
    )
    await db.commit()


@_router.post("/{asset_id}/reindex", response_model=AssetResponse)
async def reindex_asset(
    asset_id: str,
    db: Annotated[AsyncSession, Depends(get_agent_context_db)],
    gateway_db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
    request_id: Annotated[uuid.UUID | None, Query()] = None,
) -> AssetResponse:
    """Re-queue asset for indexing: set status = 'registered'. Owner or admin."""
    row = await _fetch_asset_by_id(db, asset_id)
    if not row:
        raise HTTPException(status_code=404, detail="Asset not found")

    access = await _authorize_read(row, current_user, gateway_db, write=True)
    if not _can_manage(row, current_user, access):
        raise HTTPException(403, detail="Only the contributor or Product owner can manage this information")

    if getattr(row, "product_id", None):
        await _require_product_storage(db)
    elif row.owner_sub:
        _require_scoped_imports()

    if getattr(row, "product_source", None):
        from src.knowledge.product_sources import current_grant

        await current_grant(gateway_db, row.tenant_id, row.product_id, row.product_source)

    # Every caller reserves the same asset generation with compare-and-swap.
    # Keyed receipts survive lost responses and subsequent generations. Legacy
    # browser calls share the in-flight guard even without a request ID.
    metadata = dict(row.metadata or {})
    if request_id is not None:
        receipt = hashlib.sha256(f"{current_user.user_id}:{request_id}".encode()).hexdigest()
        receipts = metadata.get("_cli_reindex_requests", [])
        if not isinstance(receipts, list):
            raise HTTPException(409, detail={"reason": "reindex_receipts_invalid"})
        if receipt in receipts:
            return _row_to_response(row, can_manage=True)
        if len(receipts) >= 256:
            raise HTTPException(409, detail={"reason": "reindex_receipts_full"})
        metadata["_cli_reindex_requests"] = [*receipts, receipt]
    # The signed ingestion callback persists its successful terminal state as
    # "complete"; legacy indexed rows remain eligible too.
    if row.status not in {"indexed", "complete", "partial", "failed"}:
        raise HTTPException(409, detail={"reason": "indexing_not_terminal"})
    reserved = await db.execute(
        text("""
            UPDATE knowledge_assets
            SET status = 'registered', last_error = NULL, updated_at = NOW(),
                ingestion_attempt_id = NULL, callback_grant_sha256 = NULL,
                metadata = CAST(:metadata AS jsonb)
            WHERE id = :id AND updated_at = :updated_at AND status = :status
            RETURNING id
        """),
        {"id": asset_id, "updated_at": row.updated_at, "status": row.status, "metadata": json.dumps(metadata)},
    )
    if reserved.fetchone() is None:
        await db.rollback()
        raise HTTPException(409, detail={"reason": "reindex_conflict"})
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
            installation_id=row.installation_id,
            product_id=getattr(row, "product_id", None),
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
    return _row_to_response(updated_row, can_manage=True)


@_router.get("/{asset_id}/status", response_model=AssetStatusResponse)
async def get_asset_status(
    asset_id: str,
    db: Annotated[AsyncSession, Depends(get_agent_context_db)],
    gateway_db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
) -> AssetStatusResponse:
    """Asset status: registry row + per-stage index detail.

    Post-#2188 the knowledge_assets registry and index_run_stages both live in
    the agent_context DB, so this joins them (the earlier #2048 "cross-DB join
    forbidden" caveat no longer applies). Returns the registry status plus the
    stages of the latest index run (clone, cgc_structural, deepwiki, zoekt, ...)
    so the UI can render per-tool progress instead of "Not yet indexed".
    """
    row = await _fetch_asset_by_id(db, asset_id)
    if not row:
        raise HTTPException(status_code=404, detail="Asset not found")

    await _authorize_read(row, current_user, gateway_db)

    # Resolve per-stage detail from the latest index run for this asset.
    # For repos: index_run_stages.repo holds the canonical "owner/repo" slug.
    # For non-repo assets (url/doc): index_run_stages.repo holds the
    # registry_asset_id (UUID) as the lookup key (issue #2308).
    repo_slug = extract_source_identifier(row.source_ref, row.asset_type)

    # For non-repo assets, use the asset_id (registry_asset_id) as the lookup key
    # since that's what the ingestion workers write to index_run_stages.repo.
    stage_lookup_key = asset_id if row.asset_type in ("url", "doc") or row.owner_sub or getattr(row, "product_id", None) else repo_slug

    stages: list[AssetIndexStage] = []
    run_id: str | None = None
    run_status: str | None = None
    run_started_at: str | None = None

    stages_result = await db.execute(
        text("""
            SELECT s.run_id, s.stage, s.status, s.artifact_ref, s.error,
                   s.started_at, s.completed_at, s.metrics, s.worker_pod,
                   r.status AS run_status, r.started_at AS run_started_at
            FROM index_run_stages s
            LEFT JOIN index_runs r ON r.id = s.run_id
            WHERE s.repo = :repo
              AND s.run_id = (
                  SELECT run_id FROM index_run_stages
                  WHERE repo = :repo
                  ORDER BY started_at DESC NULLS LAST
                  LIMIT 1
              )
            ORDER BY s.started_at ASC NULLS LAST
        """),
        {"repo": stage_lookup_key},
    )
    for sr in stages_result.fetchall():
        if run_id is None:
            run_id = str(sr.run_id) if sr.run_id else None
            run_status = sr.run_status
            run_started_at = sr.run_started_at.isoformat() if sr.run_started_at else None
        stages.append(
            AssetIndexStage(
                stage=sr.stage,
                status=sr.status,
                artifact_ref=sr.artifact_ref,
                error=sr.error,
                started_at=sr.started_at.isoformat() if sr.started_at else None,
                completed_at=sr.completed_at.isoformat() if sr.completed_at else None,
                metrics=sr.metrics,
                worker_pod=sr.worker_pod,
            )
        )

    return AssetStatusResponse(
        asset_id=asset_id,
        source_ref=row.source_ref,
        status=row.status,
        status_detail=row.status_detail if hasattr(row, "status_detail") else None,
        repo_found=bool(stages),
        run_id=run_id,
        run_status=run_status,
        run_started_at=run_started_at,
        stages=stages,
    )


# ---------------------------------------------------------------------------
# Bulk Upload — Two-Step: Preview + Commit
# ---------------------------------------------------------------------------


@_router.post("/bulk", response_model=BulkPreviewResponse)
async def bulk_preview(
    db: Annotated[AsyncSession, Depends(get_agent_context_db)],
    gateway_db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
    file: UploadFile = File(...),
    scope: str = Form("tenant"),
    product_id: str | None = Form(None),
) -> BulkPreviewResponse:
    """Parse + validate a bulk upload file. Returns preview — NO DB writes.

    Tenant admin can upload at tenant scope; any user can upload at personal scope.
    """
    # Validate scope value
    if scope not in ("personal", "tenant", "product") or (scope == "product") != bool(product_id):
        raise HTTPException(422, detail="Product scope requires product_id")
    if product_id:
        await require_product_access(gateway_db, current_user, product_id, write=True)
        await _require_product_storage(db)
    elif scope == "personal":
        _require_scoped_imports()

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

    # Derive scope for dedup + quota check — resolve canonical id (#2047/#1319)
    tenant_id = current_user.org_id or None
    owner_sub: str | None = None
    if scope == "personal":
        owner_sub = await resolve_canonical_user_id(gateway_db, current_user.user_id)

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
                  AND (to_jsonb(knowledge_assets)->>'product_id') IS NOT DISTINCT FROM CAST(:pid AS text)
                  AND status != 'removed'
                LIMIT 1
            """),
            {"sref": item.source_ref, "tid": tenant_id or "", "sub": owner_sub or "", "pid": product_id},
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
              AND (to_jsonb(knowledge_assets)->>'product_id') IS NOT DISTINCT FROM CAST(:pid AS text)
              AND status != 'removed'
            GROUP BY asset_type
        """),
        {"tid": tenant_id or "", "sub": owner_sub or "", "pid": product_id},
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


@_cli_router.post("/bulk/preview-json", response_model=BulkPreviewResponse)
async def bulk_preview_json(
    body: BulkCommitRequest,
    db: Annotated[AsyncSession, Depends(get_agent_context_db)],
    gateway_db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
) -> BulkPreviewResponse:
    """JSON adapter over canonical read-only preview, with lossless conversion."""
    if not 1 <= len(body.items) <= MAX_LINES:
        raise HTTPException(413, detail="Preview requires 1 to 500 items")
    lines = []
    for item in body.items:
        tags = ",".join(f"{key}:{value}" for key, value in item.tags.items())
        lines.append(f"{item.source_ref} | {item.display_name or ''} | {tags}")
    content = "\n".join(lines)
    parsed, rejected, _, _ = parse_bulk_file(content)
    expected = [item.model_dump() for item in body.items]
    actual = [item.model_dump(exclude={"line"}) for item in parsed]
    if rejected or actual != expected:
        raise HTTPException(422, detail="Items cannot be represented losslessly by the canonical bulk parser")
    return await bulk_preview(
        db=db,
        gateway_db=gateway_db,
        current_user=current_user,
        file=UploadFile(file=io.BytesIO(content.encode())),
        scope=body.scope,
        product_id=body.product_id,
    )


@_router.post("/bulk/commit", response_model=BulkCommitResponse, status_code=201)
async def bulk_commit(
    body: BulkCommitRequest,
    db: Annotated[AsyncSession, Depends(get_agent_context_db)],
    gateway_db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
) -> BulkCommitResponse:
    """Validate a batch and import into the explicitly requested ownership scope.

    Provider authorization permits reading the source; it never promotes a
    personal import to a Product, tenant, or shared collection.
    """
    if body.scope == "tenant" and not current_user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Tenant-scope bulk commit requires admin privileges",
        )

    if body.product_id:
        await require_product_access(gateway_db, current_user, body.product_id, write=True, lock=True)
        await _require_product_storage(db)
    elif body.scope == "personal":
        _require_scoped_imports()

    if not body.items:
        raise HTTPException(status_code=400, detail="No items to commit")

    # Enforce file limit on commit as well
    if len(body.items) > MAX_LINES:
        raise HTTPException(
            status_code=413,
            detail=f"Too many items. Maximum is {MAX_LINES}",
        )

    # Derive scope — resolve canonical id (#2047/#1319)
    tenant_id = current_user.org_id or None
    owner_sub: str | None = None
    if body.scope == "personal":
        owner_sub = await resolve_canonical_user_id(gateway_db, current_user.user_id)

    # --- Accessibility validation: validate ALL repo items before inserting any ---
    # (Issue #2087: validate-all-before-insert; no partial commit)
    repo_items = [item for item in body.items if item.asset_type == "repo"]
    # Map from source_ref → AccessibilityResult for repo items
    accessibility_map: dict[str, AccessibilityResult] = {}

    if repo_items:
        # Validate all repo items in parallel using asyncio.gather
        validation_coros = [
            validate_repo_accessibility(
                item.source_ref,
                current_user.org_id or "",
                db=db,
                gateway_db=gateway_db,
                caller_user_id=current_user.user_id,
            )
            for item in repo_items
        ]
        results = await asyncio.gather(*validation_coros)

        for item, result in zip(repo_items, results):
            if not result.allowed:
                # One bad item → reject entire batch
                raise HTTPException(
                    status_code=result.error_code,
                    detail={
                        "message": f"Accessibility check failed for {item.source_ref}",
                        "source_ref": item.source_ref,
                        "reason": result.error_message,
                    },
                )
            accessibility_map[item.source_ref] = result

    # Preserve requested scope for quota, deduplication, and each import.
    # Build resolved scope for each item
    resolved_items: list[tuple[Any, str | None, str | None, int | None]] = []  # (item, item_tenant_id, item_owner_sub, item_installation_id)

    for item in body.items:
        # Validate asset_type
        if not is_valid_asset_type(item.asset_type):
            raise HTTPException(status_code=400, detail="Asset type has no source validator")

        # Validate source_ref
        if not validate_source_ref(item.asset_type, item.source_ref):
            raise HTTPException(status_code=400, detail="Source does not match its registered validator")

        # Determine scope overrides (Issue #2087 + #3266)
        item_tenant_id = tenant_id
        item_owner_sub = owner_sub
        item_installation_id: int | None = None

        acc = accessibility_map.get(item.source_ref)
        if acc:
            item_installation_id = acc.installation_id
        from src.knowledge.product_storage import require_verified_import

        require_verified_import(item.source_ref, item_tenant_id, body.product_id)
        try:
            await admit_source(item.asset_type, item.source_ref, item_tenant_id, item_owner_sub, product_id=body.product_id)
        except SourceAdmissionError as exc:
            raise HTTPException(status_code=400, detail=f"Source admission refused: {exc}") from exc
        resolved_items.append((item, item_tenant_id, item_owner_sub, item_installation_id))

    # Count the batch against the requested ownership scope.
    scope_groups: dict[tuple[str, str], dict[str, int]] = {}
    for item, item_tenant_id, item_owner_sub, _ in resolved_items:
        key = (item_tenant_id or "", item_owner_sub or "")
        if key not in scope_groups:
            scope_groups[key] = {}
        scope_groups[key][item.asset_type] = scope_groups[key].get(item.asset_type, 0) + 1

    for (tid, sub), new_counts in scope_groups.items():
        # Determine scope_key for quota limits
        group_scope_key = "product" if body.product_id else "personal" if sub else "tenant"

        count_result = await db.execute(
            text("""
                SELECT asset_type, COUNT(*) as cnt
                FROM knowledge_assets
                WHERE COALESCE(tenant_id, '') = COALESCE(:tid, '')
                  AND COALESCE(owner_sub, '') = COALESCE(:sub, '')
                  AND (to_jsonb(knowledge_assets)->>'product_id') IS NOT DISTINCT FROM CAST(:pid AS text)
                  AND status != 'removed'
                GROUP BY asset_type
            """),
            {"tid": tid, "sub": sub, "pid": body.product_id},
        )
        existing_counts: dict[str, int] = {}
        for r in count_result.fetchall():
            existing_counts[r.asset_type] = r.cnt

        for atype, new_count in new_counts.items():
            existing = existing_counts.get(atype, 0)
            limit = _get_quota_limit(group_scope_key, atype)
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

    for item, item_tenant_id, item_owner_sub, item_installation_id in resolved_items:
        # Same source may be independently imported into different Products.
        dup_result = await db.execute(
            text("""
                SELECT id FROM knowledge_assets
                WHERE source_ref = :sref
                  AND COALESCE(tenant_id, '') = COALESCE(:tid, '')
                  AND COALESCE(owner_sub, '') = COALESCE(:sub, '')
                  AND (to_jsonb(knowledge_assets)->>'product_id') IS NOT DISTINCT FROM CAST(:pid AS text)
                  AND status != 'removed'
                LIMIT 1
            """),
            {"sref": item.source_ref, "tid": item_tenant_id or "", "sub": item_owner_sub or "", "pid": body.product_id},
        )
        if dup_result.fetchone():
            skipped_duplicates += 1
            continue

        # Insert (includes installation_id — Issue #2087)
        asset_id = str(uuid.uuid4())
        try:
            await db.execute(
                text(f"""
                    INSERT INTO knowledge_assets
                        (id, asset_type, source_ref, tenant_id, owner_sub, project_id,
                         status, registered_by, metadata, display_name, tags,
                         installation_id{", product_id" if body.product_id else ""})
                    VALUES
                        (:id, :asset_type, :source_ref, :tenant_id, :owner_sub, NULL,
                         'registered', :registered_by, '{{}}'::jsonb,
                         :display_name, CAST(:tags AS jsonb), :installation_id{", :product_id" if body.product_id else ""})
                """),
                {
                    "id": asset_id,
                    "asset_type": item.asset_type,
                    "source_ref": item.source_ref,
                    "tenant_id": item_tenant_id,
                    "owner_sub": item_owner_sub,
                    "registered_by": current_user.user_id,
                    "display_name": item.display_name,
                    "tags": _json_dumps(item.tags),
                    "installation_id": item_installation_id,
                    "product_id": body.product_id,
                },
            )
        except IntegrityError as exc:
            await db.rollback()
            if "uq_knowledge_assets_source_scope" in (str(exc.orig) or ""):
                # Race: another request inserted the same source/scope
                logger.warning(
                    "IntegrityError race on bulk_commit source_ref=%s — skipping",
                    item.source_ref,
                )
                skipped_duplicates += 1
                continue
            raise

        # Dispatch to SQS (Phase 1 inline dispatch)
        try:
            await dispatch_ingestion(
                asset_id=asset_id,
                asset_type=item.asset_type,
                source_ref=item.source_ref,
                tenant_id=item_tenant_id,
                owner_sub=item_owner_sub,
                project_id=None,
                db=db,
                installation_id=item_installation_id,
                product_id=body.product_id,
            )
        except IngestionQueueUnavailableError:
            raise HTTPException(
                status_code=503,
                detail="Context ingestion queue is not configured. Set INGESTION_QUEUE_URL to enable asset dispatch.",
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
            created_assets.append(_row_to_response(row, can_manage=True))

    await db.commit()

    return BulkCommitResponse(
        created=len(created_assets),
        skipped_duplicates=skipped_duplicates,
        assets=created_assets,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_scoped_imports() -> None:
    # New owned imports need updated workers AND all vector readers. Existing
    # registrations remain readable while the independently deployed code rolls.
    if os.environ.get("SCOPED_KNOWLEDGE_IMPORTS_ENABLED", "").lower() != "true":
        raise HTTPException(503, detail="Personal and Product knowledge imports are being upgraded. Please try again shortly.")


async def _require_product_storage(db: AsyncSession) -> None:
    _require_scoped_imports()
    result = await db.execute(
        text("""SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'knowledge_assets' AND column_name = 'product_id'
    )""")
    )
    if not result.scalar():
        raise HTTPException(503, detail="Product knowledge storage is being upgraded")


async def _authorize_read(row: Any, current_user: Any, gateway_db: AsyncSession, *, write: bool = False) -> ProductAccess | None:
    if row.tenant_id and row.tenant_id != current_user.org_id:
        raise HTTPException(404, detail="Asset not found")
    product_id = getattr(row, "product_id", None)
    if product_id:
        return await require_product_access(gateway_db, current_user, product_id, write=write, lock=write)
    if row.owner_sub:
        canonical_sub = await resolve_canonical_user_id(gateway_db, current_user.user_id)
        if row.owner_sub != canonical_sub:
            raise HTTPException(404, detail="Asset not found")
    return None


def _can_manage(row: Any, current_user: Any, access: ProductAccess | None = None) -> bool:
    if getattr(row, "product_id", None):
        return bool(
            access
            and access.product.archived_at is None
            and (row.registered_by in {current_user.user_id, access.member_id} or access.product.owner_user_id == access.member_id)
        )
    return bool(row.registered_by == current_user.user_id or (current_user.is_admin and row.tenant_id == current_user.org_id))


async def _fetch_asset_by_id(db: AsyncSession, asset_id: str) -> Any | None:
    """Fetch a single asset row by ID."""
    result = await db.execute(
        text("""
            SELECT id, asset_type, source_ref, display_name, tags, metadata,
                   tenant_id, owner_sub, project_id, to_jsonb(knowledge_assets)->>'product_id' AS product_id, status, status_detail,
                   last_error, retry_count, registered_by, installation_id,
                   to_jsonb(knowledge_assets)->'product_source' AS product_source,
                   to_jsonb(knowledge_assets)->'refresh_enabled' AS refresh_enabled,
                   to_jsonb(knowledge_assets)->>'source_upload_id' AS source_upload_id,
                   created_at, updated_at
            FROM knowledge_assets
            WHERE id = :id AND status != 'removed'
        """),
        {"id": asset_id},
    )
    return result.fetchone()


def _row_to_response(row: Any, *, can_manage: bool = False) -> AssetResponse:
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
        product_id=getattr(row, "product_id", None),
        can_manage=can_manage,
        can_download=bool(getattr(row, "source_upload_id", None)),
        status=row.status,
        last_error=row.last_error,
        retry_count=row.retry_count,
        registered_by=row.registered_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _get_quota_info(
    db: AsyncSession, gateway_db: AsyncSession, current_user: Any, *, scope: str = "personal", product_id: str | None = None
) -> QuotaInfo:
    """Compute quota usage for the current user's scope.

    ``db`` is the agent_context session (knowledge_assets); ``gateway_db`` is the
    gateway session used for the ``users`` identity lookup (the ``users`` table
    lives in the gateway DB, not agent_context — see #2213 follow-up).
    """
    # Resolve canonical id for personal-scope quota count (#2047/#1319)
    canonical_sub = await resolve_canonical_user_id(gateway_db, current_user.user_id)

    # Count assets per type for personal scope
    result = await db.execute(
        text("""
            SELECT asset_type, COUNT(*) as cnt
            FROM knowledge_assets
            WHERE tenant_id = :tid
              AND owner_sub IS NOT DISTINCT FROM CAST(:sub AS text)
              AND (to_jsonb(knowledge_assets)->>'product_id') IS NOT DISTINCT FROM CAST(:pid AS text)
              AND status != 'removed'
            GROUP BY asset_type
        """),
        {"tid": current_user.org_id, "sub": canonical_sub if scope not in {"tenant", "product"} else None, "pid": product_id},
    )
    counts: dict[str, int] = {}
    for r in result.fetchall():
        counts[r.asset_type] = r.cnt

    scope_key = scope if scope in {"personal", "tenant", "product"} else "personal"
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


# ---------------------------------------------------------------------------
# Conditional export — gated behind AGENT_CONTEXT_ENABLED
# ---------------------------------------------------------------------------

_legacy_router = _router
_router = APIRouter()
# The gateway ingress strips its outer /api prefix. Native clients send
# /api/agent-context/assets; older UI clients send /api/api/agent-context/assets.
# Mount the same handlers at both paths so authorization and behavior stay equal.
_router.include_router(_legacy_router)
_router.include_router(_legacy_router, prefix="/api")
_router.include_router(_cli_router)
_router.include_router(_search_router)
_router.include_router(product_source_router)
_router.include_router(product_storage_router)

if os.environ.get("AGENT_CONTEXT_ENABLED", "").lower() == "true":
    router = _router
