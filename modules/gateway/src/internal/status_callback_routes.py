"""Internal status-callback endpoint for ingestion worker → gateway knowledge_assets.

Issue #2049: Minimal status-callback bridge (C1/Decision 5).

Endpoint (IAM-signed / shared-secret; internal only):
    POST /internal/v1/knowledge-assets/status-callback
        — ingestion worker writes status back to the gateway knowledge_assets row

The worker calls this endpoint on each state transition (indexing, complete, failed)
so the gateway row reflects live ingestion progress without any cross-DB join.

Authentication:
    Reuses verify_internal_or_irsa from auth_deps.py (dual-auth: IRSA +
    shared-secret). Returns 403 on auth failure.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.internal.auth_deps import verify_internal_or_irsa
from src.shared.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/v1", tags=["internal-status-callback"])

# Valid status values the worker can write (subset of the knowledge_assets lifecycle)
_VALID_CALLBACK_STATUSES = frozenset({"indexing", "complete", "failed"})


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class StatusCallbackRequest(BaseModel):
    """Body for POST /internal/v1/knowledge-assets/status-callback."""

    asset_id: str = Field(..., description="UUID of the knowledge_assets row")
    status: str = Field(..., description="New status: indexing, complete, or failed")
    status_detail: dict | None = Field(
        None,
        description="Compact projection of run-state (stage, summary counts, etc.)",
    )
    error: str | None = Field(None, description="Error message on failure")


class StatusCallbackResponse(BaseModel):
    """Response for status callback."""

    asset_id: str
    status: str
    updated_at: str


# ---------------------------------------------------------------------------
# Endpoint: POST /internal/v1/knowledge-assets/status-callback
# ---------------------------------------------------------------------------


@router.post(
    "/knowledge-assets/status-callback",
    response_model=StatusCallbackResponse,
    status_code=200,
    summary="Update knowledge_assets status from ingestion worker",
    description=(
        "Called by the ingestion worker on state transitions to update the "
        "gateway knowledge_assets row. Updates status, status_detail, and "
        "optionally last_error/retry_count. No cross-DB joins."
    ),
)
async def status_callback(
    body: StatusCallbackRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_internal_or_irsa),
) -> StatusCallbackResponse:
    # Validate status value
    if body.status not in _VALID_CALLBACK_STATUSES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_status",
                "message": f"Status must be one of: {sorted(_VALID_CALLBACK_STATUSES)}",
            },
        )

    # Validate asset_id format (basic UUID check)
    if not body.asset_id or len(body.asset_id) < 32:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_asset_id", "message": "asset_id must be a valid UUID"},
        )

    # Build the UPDATE statement — only touches knowledge_assets columns
    # No read of repositories/index_runs/index_run_stages (cross-DB join forbidden)
    now = datetime.now(UTC)

    if body.status == "failed":
        # On failure: update status, status_detail, last_error, increment retry_count
        result = await db.execute(
            text("""
                UPDATE knowledge_assets
                SET status = :status,
                    status_detail = :status_detail::jsonb,
                    last_error = :error,
                    retry_count = retry_count + 1,
                    updated_at = :now
                WHERE id = :asset_id
                  AND status != 'removed'
                RETURNING id
            """),
            {
                "status": body.status,
                "status_detail": _json_dumps(body.status_detail),
                "error": body.error[:1000] if body.error else None,
                "asset_id": body.asset_id,
                "now": now,
            },
        )
    else:
        # indexing or complete: update status + status_detail, clear last_error
        result = await db.execute(
            text("""
                UPDATE knowledge_assets
                SET status = :status,
                    status_detail = :status_detail::jsonb,
                    last_error = NULL,
                    updated_at = :now
                WHERE id = :asset_id
                  AND status != 'removed'
                RETURNING id
            """),
            {
                "status": body.status,
                "status_detail": _json_dumps(body.status_detail),
                "asset_id": body.asset_id,
                "now": now,
            },
        )

    row = result.fetchone()
    if not row:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "asset_not_found",
                "message": f"No active knowledge_assets row with id '{body.asset_id}'",
            },
        )

    await db.commit()

    logger.info(
        "Status callback: asset=%s status=%s",
        body.asset_id,
        body.status,
    )

    return StatusCallbackResponse(
        asset_id=body.asset_id,
        status=body.status,
        updated_at=now.isoformat(),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_dumps(obj: dict | None) -> str | None:
    """Serialize dict to JSON string for Postgres JSONB cast, or None."""
    if obj is None:
        return None
    import json

    return json.dumps(obj)
