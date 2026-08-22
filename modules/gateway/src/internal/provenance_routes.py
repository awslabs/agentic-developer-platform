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
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from src.shared.models.provenance import ActionProvenance

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/v1", tags=["internal-provenance"])


async def _actor_is_member_of(db: AsyncSession, actor_user_id: str, org_id: str) -> bool:
    """Whether ``actor_user_id`` may have provenance attributed to ``org_id``.

    Issue #3985 (A2): the caller asserts ``body.org_id``, so FK-existence of the
    actor is not enough — without this compare any internal-plane caller can
    attribute an action to an org the actor has nothing to do with, poisoning
    another tenant's audit trail.

    ``tenant_memberships`` (migration 021) is the authority for org membership.
    Note that ``TenantMembership.user_id`` FKs ``users.id``, which is exactly what
    ``actor_user_id`` is, so this needs no ``cognito_sub`` indirection (unlike
    ``admin/access_control.py``, which starts from a token's Cognito sub).

    Fallback: ``POST /resolve-user`` auto-provisions shadow users with
    ``users.org_id`` set but no membership row (only three code paths create
    memberships, and migration 021 backfilled pre-existing users once). A
    strict membership-only check would therefore 403 legitimate provenance for
    shadow-user actors, so when an actor has no membership rows at all we fall
    back to their ``users.org_id``. The compare is still enforced either way —
    the fallback narrows the authority source, it does not skip the check.
    """
    rows = (await db.execute(select(TenantMembership.tenant_id).where(TenantMembership.user_id == actor_user_id))).scalars().all()

    if rows:
        return org_id in set(rows)

    actor_org_id = (await db.execute(select(User.org_id).where(User.id == actor_user_id))).scalar_one_or_none()
    if actor_org_id is None:
        return False

    logger.info(
        "provenance_membership_fallback actor=%s org=%s reason=no_membership_rows",
        actor_user_id,
        org_id,
    )
    return actor_org_id == org_id


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
    parent_invocation_id: str | None = None


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

    # Issue #3985 (A2): the actor must actually belong to the org the caller is
    # attributing this action to. Checked before any further validation so a
    # cross-tenant attempt cannot probe which user IDs exist in other orgs.
    if not await _actor_is_member_of(db, body.actor_user_id, body.org_id):
        logger.warning(
            "Rejecting provenance write: actor=%s is not a member of org=%s",
            body.actor_user_id,
            body.org_id,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "actor_org_mismatch",
                "message": "actor_user_id is not a member of the specified org_id",
            },
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
    row_id = new_uuid()

    # Guard against self-referential parent (prevents cycles in lineage tree)
    parent_inv_id = body.parent_invocation_id
    if parent_inv_id and parent_inv_id == row_id:
        raise HTTPException(
            status_code=400,
            detail={"error": "self_referential_parent", "message": "parent_invocation_id must not equal the row's own id"},
        )

    row = ActionProvenance(
        id=row_id,
        org_id=body.org_id,
        actor_user_id=body.actor_user_id,
        triggered_by=body.triggered_by,
        root_human_id=body.root_human_id,
        is_human_rooted=body.is_human_rooted,
        action_kind=body.action_kind,
        source_event=body.source_event,
        correlation_id=body.correlation_id,
        parent_invocation_id=parent_inv_id,
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
