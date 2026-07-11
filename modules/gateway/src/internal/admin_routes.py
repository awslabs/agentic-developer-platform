"""Admin read-only endpoints for adversarial E2E and operational inspection.

Issue #3462: The credential-binding adversarial E2E script needs to:
  1. Verify sandbox tenant config (enable_user_credentials + enforce_credential_binding)
  2. Query audit entries by provenance_id to assert the boundary held

Both are read-only, internal-only (X-Internal-Api-Key / IRSA dual-auth).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.internal.auth_deps import verify_internal_or_irsa
from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.models.audit import AuditLog

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/v1", tags=["internal-admin"])


@router.get("/admin/tenant-config/{tenant}")
async def get_tenant_config(
    tenant: str,
    _: None = Depends(verify_internal_or_irsa),
) -> dict:
    """Return credential-binding feature flags for a tenant.

    The adversarial E2E script uses this to verify the sandbox is correctly
    configured before running injection tests (anti-gaming clause).

    Note: In the current single-tenant deployment, the tenant path param is
    accepted but the response always reflects the gateway's own settings.
    Multi-tenant per-org overrides are a future concern.
    """
    settings = get_settings()
    return {
        "tenant": tenant,
        "enable_user_credentials": settings.enable_user_credentials,
        "enforce_credential_binding": settings.enforce_credential_binding,
    }


@router.get("/admin/audit-entries")
async def get_audit_entries(
    provenance_id: str = Query(..., description="Filter audit entries by provenance_id (stored in details JSON)"),
    org_id: str | None = Query(default=None, description="Optional tenant/org_id filter — when provided, only returns entries for this org"),
    limit: int = Query(default=100, ge=1, le=500, description="Maximum entries to return"),
    _: None = Depends(verify_internal_or_irsa),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Query security_audit_logs filtered by provenance_id.

    The adversarial E2E script collects audit entries for a specific agent run
    (identified by provenance_id) to assert the credential boundary held.

    provenance_id is stored in the details JSON column by credential endpoints.
    org_id is optional — when provided, scopes results to a single tenant to
    prevent cross-tenant evidence leaking into harness artifacts.
    """
    # Query audit logs where details->provenance_id matches.
    # SQLAlchemy JSON column access varies by dialect; use the generic JSON
    # path accessor which works on PostgreSQL (->>) and SQLite (json_extract).
    stmt = select(AuditLog).where(AuditLog.details["provenance_id"].as_string() == provenance_id)

    # Optional tenant scoping — prevents cross-tenant audit evidence ingestion.
    if org_id is not None:
        stmt = stmt.where(AuditLog.org_id == org_id)

    stmt = stmt.order_by(AuditLog.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    rows = result.scalars().all()

    entries = [
        {
            "id": row.id,
            "event_type": row.event_type,
            "actor_id": row.actor_id,
            "org_id": row.org_id,
            "details": row.details,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]

    logger.info(
        "Returned %d audit entries for provenance_id=%s",
        len(entries),
        provenance_id,
    )
    return {"entries": entries}
