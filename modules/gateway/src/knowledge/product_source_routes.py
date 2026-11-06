"""Expose existing Product repositories and their Knowledge refresh settings."""

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user
from src.knowledge.product_access import require_product_access
from src.knowledge.product_sources import current_grant, describe_source, resolve_source
from src.products.models import ProductProjectBinding, ProductSourceBinding
from src.shared.database import get_db
from src.shared.database_agent_context import get_agent_context_db
from src.shared.models.base import utcnow
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/agent-context", tags=["knowledge-assets"])


async def require_source_storage(db):
    installed = await db.scalar(
        text("""SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name='knowledge_assets' AND column_name='product_source'
    )""")
    )
    if not installed:
        raise HTTPException(
            503,
            detail={
                "code": "knowledge_source_setup_required",
                "message": "Product source refresh requires the Context database update.",
            },
        )


@router.get("/products/{product_id}/sources")
async def list_product_sources(
    product_id: str,
    response: Response,
    after: str | None = Query(None, max_length=36),
    db: AsyncSession = Depends(get_agent_context_db),
    gateway_db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    from src.knowledge.routes import _can_manage

    access = await require_product_access(gateway_db, actor, product_id)
    await require_source_storage(db)
    response.headers["Cache-Control"] = "no-store"
    candidates = set(
        (
            await gateway_db.scalars(
                select(ProductSourceBinding.id)
                .join(
                    ProductProjectBinding,
                    (ProductProjectBinding.tenant_id == ProductSourceBinding.tenant_id)
                    & (ProductProjectBinding.project_id == ProductSourceBinding.project_id),
                )
                .where(
                    ProductSourceBinding.tenant_id == actor.org_id,
                    ProductProjectBinding.product_id == product_id,
                    ProductProjectBinding.valid_to.is_(None),
                    ProductSourceBinding.unlinked_at.is_(None),
                    ProductSourceBinding.provider == "github",
                    ProductSourceBinding.kind == "repository",
                )
            )
        ).all()
    )
    assets = (
        await db.execute(
            text("""SELECT id, product_id, product_source, source_ref, status, registered_by,
        refresh_enabled, refresh_error, last_refreshed_at, last_error
        FROM knowledge_assets WHERE tenant_id=:tenant AND product_id=:product AND status!='removed'
        AND product_source IS NOT NULL"""),
            {"tenant": actor.org_id, "product": product_id},
        )
    ).all()
    included = {row.product_source["binding_id"]: row for row in assets}
    candidates.update(included)
    ids = sorted(key for key in candidates if after is None or key > after)[:51]
    semaphore = asyncio.Semaphore(5)
    results = []
    pending = []

    async def describe(source, item, reconnect=False):
        async with semaphore:
            try:
                details = await describe_source(source)
                if item["included"] and details["source_ref"] != item.get("source_ref"):
                    reconnect = True
                    item["error"] = "The repository name changed. Reconnect to keep this Context entry updated."
                item.update(details)
                item["eligible"] = not reconnect
                item["needs_reconnect"] = reconnect
            except HTTPException as exc:
                item["error"] = exc.detail.get("message", "Source unavailable") if isinstance(exc.detail, dict) else str(exc.detail)

    # Session operations are sequential; only independent GitHub reads overlap.
    for binding_id in ids[:50]:
        asset = included.get(binding_id)
        item = {"binding_id": binding_id, "eligible": False, "included": bool(asset), "can_manage": False}
        if asset:
            grant = asset.product_source
            item.update(
                name=grant.get("name"),
                source_ref=asset.source_ref,
                connection_name=grant.get("connection_name"),
                connection_id=grant.get("connection_id"),
                project_id=grant.get("project_id"),
                asset_id=str(asset.id),
                status=asset.status,
                keep_updated=asset.refresh_enabled,
                revision=grant["configuration_revision"],
                last_refreshed_at=asset.last_refreshed_at,
                error=asset.refresh_error or asset.last_error,
                can_manage=_can_manage(asset, actor, access),
            )
        try:
            source = await (
                current_grant(gateway_db, actor.org_id, product_id, asset.product_source, lock=False)
                if asset
                else resolve_source(gateway_db, actor.org_id, product_id, binding_id)
            )
            item.update(project_id=source.project.id, project_name=source.project.name, connection_id=source.connection.id)
            pending.append(describe(source, item))
        except HTTPException as exc:
            item["error"] = exc.detail.get("message", "Source unavailable") if isinstance(exc.detail, dict) else str(exc.detail)
            if asset:
                try:
                    replacement = await resolve_source(gateway_db, actor.org_id, product_id, binding_id)
                    pending.append(describe(replacement, item, reconnect=True))
                except HTTPException:
                    pass  # Revoked/deleted connections cannot be reauthorized.
        results.append(item)
    await asyncio.gather(*pending)
    return {"product_id": product_id, "items": results, "after": ids[49] if len(ids) > 50 else None}


class RefreshSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    revision: int = Field(ge=1)


class IncludeSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_ref: str = Field(min_length=1, max_length=2048)
    keep_updated: bool = False


@router.post("/products/{product_id}/sources/{binding_id}", status_code=201)
async def include_source(
    product_id: str,
    binding_id: str,
    body: IncludeSource,
    db: AsyncSession = Depends(get_agent_context_db),
    gateway_db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    from src.knowledge.routes import register_asset
    from src.knowledge.schemas import AssetCreateRequest

    # A distinct route prevents an older Gateway from ignoring the new fields
    # and silently creating a legacy import during a rolling deployment.
    return await register_asset(
        AssetCreateRequest(
            asset_type="repo",
            source_ref=body.source_ref,
            scope="product",
            product_id=product_id,
            source_binding_id=binding_id,
            keep_updated=body.keep_updated,
        ),
        db,
        gateway_db,
        actor,
    )


@router.patch("/assets/{asset_id}/refresh")
async def update_refresh(
    asset_id: str,
    body: RefreshSettings,
    db: AsyncSession = Depends(get_agent_context_db),
    gateway_db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    from src.knowledge.routes import _authorize_read, _can_manage, _fetch_asset_by_id, _require_scoped_imports

    await require_source_storage(db)
    row = await _fetch_asset_by_id(db, asset_id)
    if row is None or not row.product_source:
        raise HTTPException(404, "Product source not found")
    access = await _authorize_read(row, actor, gateway_db)
    if not _can_manage(row, actor, access):
        raise HTTPException(403, "Only the contributor or Product owner can configure refresh")
    if body.enabled:
        _require_scoped_imports()
        await current_grant(gateway_db, row.tenant_id, row.product_id, row.product_source)
    grant = {
        **row.product_source,
        "configuration_revision": body.revision + 1,
        "refresh_updated_by": access.member_id,
        "refresh_updated_at": utcnow().isoformat(),
    }
    applied = await db.execute(
        text("""UPDATE knowledge_assets SET refresh_enabled=:enabled,
        product_source=CAST(:grant AS jsonb), refresh_error=NULL
        WHERE id=:id AND status!='removed'
        AND (product_source->>'configuration_revision')::bigint=:revision RETURNING id"""),
        {"id": asset_id, "enabled": body.enabled, "grant": json.dumps(grant), "revision": body.revision},
    )
    if applied.first() is None:
        await db.rollback()
        raise HTTPException(409, "Refresh settings changed. Reload before saving.")
    await db.commit()
    return {"enabled": body.enabled, "revision": body.revision + 1}
