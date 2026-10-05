"""Internal status-callback endpoint for ingestion worker → gateway knowledge_assets.

Issue #2049: Minimal status-callback bridge (C1/Decision 5).

Endpoint (signed asset/attempt grant; internal only):
    POST /internal/v1/knowledge-assets/status-callback
        — ingestion worker writes status back to the gateway knowledge_assets row

The worker calls this endpoint on each state transition (indexing, complete, failed)
so the gateway row reflects live ingestion progress without any cross-DB join.

Authentication:
    The dispatch-minted grant authenticates one asset, tenant and attempt. Its
    digest must match the active persisted attempt. No broad transport key is used.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.internal.credential_binding_metrics import observe_identity_binding
from src.knowledge.ingestion_callback_grant import GrantError, verify_ingestion_grant
from src.shared.database_agent_context import get_agent_context_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/v1", tags=["internal-status-callback"])

# Valid status values the worker can write (subset of the knowledge_assets lifecycle)
_VALID_CALLBACK_STATUSES = frozenset({"indexing", "complete", "failed"})

# Issue #5663 (A09): route label for the identity-binding counters.
_STATUS_CALLBACK_ROUTE = "knowledge-assets-status-callback"


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
    tenant_id: str | None = Field(
        None,
        description=(
            "Owning tenant of the asset, as the caller believes it to be. Issue "
            "#5663 (A09): this is a CHECKED ASSERTION ONLY and never selects which "
            "row is written. The authoritative tenant comes from the signed "
            "callback_grant the gateway minted at dispatch; if this field is present "
            "and disagrees with the grant, the request is refused rather than "
            "resolved in the caller's favour."
        ),
    )
    callback_grant: str | None = Field(
        None,
        description=(
            "Server-minted authority for exactly this asset (issue #5663, A09). "
            "Produced by src/knowledge/dispatch.py, carried through the SQS "
            "envelope, and opaque to the worker. This is what makes the caller's "
            "asset_id/tenant_id checkable instead of authoritative."
        ),
    )


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
    db: AsyncSession = Depends(get_agent_context_db),
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

    # Only a signed, current server-dispatched attempt can update this row.
    tenant_params: dict[str, str] = {}

    grant = None
    if body.callback_grant:
        try:
            grant = verify_ingestion_grant(body.callback_grant)
        except GrantError:
            # An unverifiable grant is worse than none: something presented
            # authority it does not hold. Refused in both modes — there is no
            # compatibility story for a forged or expired token, and "fall back to
            # the caller's claim" would make the grant optional in practice.
            observe_identity_binding(route=_STATUS_CALLBACK_ROUTE, outcome="denied", enforced=True)
            logger.warning(
                "status_callback DENIED — callback grant did not verify asset=%s status=%s",
                body.asset_id,
                body.status,
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "callback_grant_invalid",
                    "message": "The callback grant presented is not valid.",
                },
            ) from None

    if grant is not None:
        if grant.asset_id != body.asset_id:
            # The grant names the row. A caller asking to write a different one is
            # the confused-deputy case this whole change exists to stop.
            observe_identity_binding(route=_STATUS_CALLBACK_ROUTE, outcome="denied", enforced=True)
            logger.warning(
                "status_callback DENIED — grant is for another asset requested=%s status=%s",
                body.asset_id,
                body.status,
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "callback_grant_asset_mismatch",
                    "message": "The callback grant is not for the requested asset.",
                },
            )

        if body.tenant_id is not None and body.tenant_id != (grant.tenant_id or ""):
            # The assertion is checked, not honoured. A caller that names a tenant
            # other than the one the gateway recorded for this asset is refused
            # rather than silently corrected, because the disagreement itself is
            # evidence that one side is wrong about what is being written.
            observe_identity_binding(route=_STATUS_CALLBACK_ROUTE, outcome="denied", enforced=True)
            logger.warning(
                "status_callback DENIED — asserted tenant contradicts the grant asset=%s status=%s",
                body.asset_id,
                body.status,
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "callback_grant_tenant_mismatch",
                    "message": "The asserted tenant does not match this asset's recorded tenant.",
                },
            )

        observe_identity_binding(route=_STATUS_CALLBACK_ROUTE, outcome="allowed", enforced=True)
        if grant.is_shared_scope:
            # Explicit authorization of a shared-scope asset, on the grant's word.
            tenant_clause = "AND tenant_id IS NULL"
        else:
            tenant_clause = "AND tenant_id = :tenant_id"
            tenant_params["tenant_id"] = grant.tenant_id
    else:
        observe_identity_binding(route=_STATUS_CALLBACK_ROUTE, outcome="denied", enforced=True)
        logger.warning(
            "status_callback DENIED — no callback grant presented asset=%s status=%s",
            body.asset_id,
            body.status,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "callback_grant_required",
                "message": "This callback is not bound to an asset.",
            },
        )
    if body.status == "failed":
        # On failure: update status, status_detail, last_error, increment retry_count
        result = await db.execute(
            text(f"""
                UPDATE knowledge_assets
                SET status = :status,
                    status_detail = CAST(:status_detail AS jsonb),
                    last_error = :error,
                    retry_count = retry_count + 1,
                    updated_at = :now
                WHERE id = :asset_id
                  AND status IN ('registered', 'queued', 'indexing')
                  AND ingestion_attempt_id = CAST(:attempt_id AS uuid)
                  AND callback_grant_sha256 = :grant_digest
                  {tenant_clause}
                RETURNING id
            """),
            {
                "status": body.status,
                "status_detail": _json_dumps(body.status_detail),
                "error": body.error[:1000] if body.error else None,
                "asset_id": body.asset_id,
                "now": now,
                "attempt_id": grant.attempt_id,
                "grant_digest": hashlib.sha256(body.callback_grant.encode()).hexdigest(),
                **tenant_params,
            },
        )
    else:
        # indexing or complete: update status + status_detail, clear last_error
        result = await db.execute(
            text(f"""
                UPDATE knowledge_assets
                SET status = :status,
                    status_detail = CAST(:status_detail AS jsonb),
                    last_error = NULL,
                    updated_at = :now
                WHERE id = :asset_id
                  AND status IN ('registered', 'queued', 'indexing')
                  AND ingestion_attempt_id = CAST(:attempt_id AS uuid)
                  AND callback_grant_sha256 = :grant_digest
                  {tenant_clause}
                RETURNING id
            """),
            {
                "status": body.status,
                "status_detail": _json_dumps(body.status_detail),
                "asset_id": body.asset_id,
                "now": now,
                "attempt_id": grant.attempt_id,
                "grant_digest": hashlib.sha256(body.callback_grant.encode()).hexdigest(),
                **tenant_params,
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
