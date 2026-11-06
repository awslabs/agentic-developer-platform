"""The named Kubernetes scheduler may requeue registered assets, never invent scope."""

import asyncio
import os
from functools import lru_cache
from uuid import UUID

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.knowledge.dispatch import dispatch_ingestion
from src.shared.database import get_db
from src.shared.database_agent_context import get_agent_context_db

router = APIRouter(prefix="/internal/v1/knowledge-assets", tags=["ingestion-refresh"])


@lru_cache(maxsize=2)
def _keys(issuer):
    return jwt.PyJWKClient(issuer + "/keys", timeout=10)


async def verify_refresh_identity(authorization: str | None = Header(default=None)):
    issuer = os.environ.get("INGESTION_REFRESH_ISSUER", "").rstrip("/")
    subject = os.environ.get("INGESTION_REFRESH_SUBJECT", "")
    if not issuer.startswith("https://") or not subject:
        raise HTTPException(503, "Ingestion scheduler identity is not configured")
    if not authorization or not authorization.startswith("Bearer ") or len(authorization) > 16384:
        raise HTTPException(403, "Ingestion scheduler identity required")
    token = authorization[7:]
    try:
        key = await asyncio.to_thread(_keys(issuer).get_signing_key_from_jwt, token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience="adp-ingestion-refresh",
            issuer=issuer,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
        if claims["sub"] != subject:
            raise ValueError()
    except (jwt.PyJWTError, ValueError):
        raise HTTPException(403, "Ingestion scheduler identity required") from None


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    after: UUID | None = None


@router.post("/refresh", dependencies=[Depends(verify_refresh_identity)])
async def refresh_registered(body: RefreshRequest, db: AsyncSession = Depends(get_agent_context_db), gateway_db: AsyncSession = Depends(get_db)):
    # No body-controlled source, identity, installation, role or output prefix.
    rows = (
        (
            await db.execute(
                text("""SELECT id,asset_type,source_ref,tenant_id,owner_sub,
        project_id,product_id,installation_id,to_jsonb(knowledge_assets)->'product_source' AS product_source FROM knowledge_assets
        WHERE status IN ('complete','partial') AND asset_type IN ('repo','url','doc')
          AND (CAST(:after AS text) IS NULL OR id::text > CAST(:after AS text))
          AND updated_at < NOW() - INTERVAL '20 hours'
          AND (to_jsonb(knowledge_assets)->>'refresh_enabled') IS DISTINCT FROM 'false'
        ORDER BY id::text LIMIT 100 FOR UPDATE SKIP LOCKED"""),
                {"after": str(body.after) if body.after else None},
            )
        )
        .mappings()
        .all()
    )
    queued = 0
    blocked = 0
    for row in rows:
        from src.knowledge import product_storage

        if row.get("product_id") and product_storage.configured():
            try:
                await product_storage.require_service_product(gateway_db, row["tenant_id"], row["product_id"])
            except HTTPException:
                await db.execute(
                    text("UPDATE knowledge_assets SET refresh_error=:error WHERE id=:id"),
                    {"id": row["id"], "error": "Product Context is no longer available for ingestion"},
                )
                blocked += 1
                continue
        if row.get("product_source"):
            from src.knowledge.product_sources import current_grant
            from src.knowledge.routes import _require_scoped_imports

            try:
                _require_scoped_imports()
                await current_grant(gateway_db, row["tenant_id"], row["product_id"], row["product_source"])
            except HTTPException as exc:
                message = exc.detail.get("message", "Product source refresh unavailable") if isinstance(exc.detail, dict) else str(exc.detail)
                await db.execute(text("UPDATE knowledge_assets SET refresh_error=:error WHERE id=:id"), {"id": row["id"], "error": message})
                blocked += 1
                continue
        reserved = await db.execute(
            text("""UPDATE knowledge_assets SET status='registered',
            ingestion_attempt_id=NULL,callback_grant_sha256=NULL,updated_at=NOW() WHERE id=:id
            AND status IN ('complete','partial') AND updated_at < NOW() - INTERVAL '20 hours'
            AND (to_jsonb(knowledge_assets)->>'refresh_enabled') IS DISTINCT FROM 'false'
            RETURNING id"""),
            {"id": row["id"]},
        )
        if reserved.first() is None:
            continue
        ok = await dispatch_ingestion(
            str(row["id"]),
            row["asset_type"],
            row["source_ref"],
            row["tenant_id"],
            row["owner_sub"],
            str(row["project_id"]) if row["project_id"] else None,
            db,
            product_id=row["product_id"],
            installation_id=row["installation_id"],
        )
        if not ok:
            # dispatch retains an uncertain attempt rather than replaying it.
            await db.commit()
            raise HTTPException(503, "Registered ingestion dispatch failed")
        queued += 1
    await db.commit()
    return {"queued": queued, "blocked": blocked, "after": str(rows[-1]["id"]) if len(rows) == 100 else None}
