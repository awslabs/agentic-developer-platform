"""Internal provenance write endpoint.

Issue #785: Phase 2-b — gateway /internal/v1/provenance endpoint.

Endpoint (IAM-signed / shared-secret; internal only):
    POST /internal/v1/provenance — insert an action_provenance row

Called by the webhook-ingress Lambda (not in VPC) to record provenance
via the gateway's internal-only path.

Authentication:
    Reuses verify_internal_or_irsa from auth_deps.py (dual-auth: IRSA +
    shared-secret). Returns 403 on auth failure (not 401) to avoid
    endpoint discovery.

# TODO: Per-endpoint auth scoping (vs. the current single shared-secret)
# is a Phase 3 hardening item. The shared-secret grants access to ALL
# /internal/v1/* endpoints — if it leaks, an attacker can insert fake
# provenance rows. Tracked as inherited risk from the existing pattern.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.internal.auth_deps import verify_internal_or_irsa
from src.shared.database import get_db
from src.shared.models.base import new_uuid, utcnow
from src.shared.models.organization import User
from src.shared.models.provenance import ActionProvenance

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/v1", tags=["internal-provenance"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CreateProvenanceRequest(BaseModel):
    """Body for POST /internal/v1/provenance."""

    actor_user_id: str
    triggered_by: str | None = None
    root_human_id: str
    is_human_rooted: bool
    action_kind: str
    source_event: dict
    correlation_id: str
    org_id: str


class CreateProvenanceResponse(BaseModel):
    id: str
    created_at: str


# ---------------------------------------------------------------------------
# Endpoint: POST /internal/v1/provenance
# ---------------------------------------------------------------------------


@router.post(
    "/provenance",
    response_model=CreateProvenanceResponse,
    status_code=201,
    summary="Record an action provenance row (Lambda/internal call)",
    description=("Called by the webhook-ingress Lambda to record provenance for agent/human actions. Validates FK references to users table."),
)
async def create_provenance(
    body: CreateProvenanceRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_internal_or_irsa),
) -> CreateProvenanceResponse:
    # Validate FK: actor_user_id must exist
    actor = await db.execute(select(User.id).where(User.id == body.actor_user_id))
    if actor.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_actor", "message": f"actor_user_id '{body.actor_user_id}' not found"},
        )

    # Validate FK: triggered_by must exist if provided
    if body.triggered_by is not None:
        triggered = await db.execute(select(User.id).where(User.id == body.triggered_by))
        if triggered.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_triggered_by", "message": f"triggered_by '{body.triggered_by}' not found"},
            )

    # Validate FK: root_human_id must exist
    root = await db.execute(select(User.id).where(User.id == body.root_human_id))
    if root.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_root_human", "message": f"root_human_id '{body.root_human_id}' not found"},
        )

    # Insert provenance row
    now = utcnow()
    row = ActionProvenance(
        id=new_uuid(),
        org_id=body.org_id,
        actor_user_id=body.actor_user_id,
        triggered_by=body.triggered_by,
        root_human_id=body.root_human_id,
        is_human_rooted=body.is_human_rooted,
        action_kind=body.action_kind,
        source_event=body.source_event,
        correlation_id=body.correlation_id,
        created_at=now,
    )
    db.add(row)
    await db.commit()

    logger.info(
        "Provenance recorded id=%s actor=%s correlation=%s kind=%s",
        row.id,
        body.actor_user_id,
        body.correlation_id,
        body.action_kind,
    )

    return CreateProvenanceResponse(
        id=row.id,
        created_at=now.isoformat(),
    )
