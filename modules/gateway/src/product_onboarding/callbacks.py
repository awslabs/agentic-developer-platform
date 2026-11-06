"""Admit operation-scoped service callbacks after independent effect readback."""

from datetime import UTC
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.product_onboarding.callback_auth import PRODUCER, get_callback_user
from src.product_onboarding.handoffs import observed_handoff_effect
from src.products.models import ProductOnboardingSession, ProductOperation
from src.products.routes import require_profile_admission, scoped_product
from src.shared.database import get_db
from src.shared.identity.product_authority import stored_product_actor
from src.shared.models.base import utcnow
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/v1/product-callbacks", tags=["products"])


class PeerReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(min_length=1, max_length=255)
    product_id: str = Field(min_length=1, max_length=36)
    project_id: str = Field(min_length=1, max_length=255)
    profile_revision: int = Field(ge=1)
    provider_operation_id: str = Field(min_length=1, max_length=512)
    status: Literal["accepted", "running", "succeeded", "failed", "denied"]


class Callback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sequence: int = Field(strict=True, ge=1)
    binding_generation: int | None = Field(default=None, strict=True, ge=1)
    receipt: PeerReceipt


@router.post("/{operation_id}", operation_id="applyCallback", dependencies=[Depends(require_profile_admission)])
async def apply_callback(
    operation_id: str,
    request: Callback,
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_callback_user),
):
    if (
        actor.account_type != "service"
        or actor.auth_source != "jwt"
        or actor.token_use != "access"
        or not actor.org_id
        or actor.token_scopes.split() != [f"product-callback/{operation_id}"]
    ):
        raise HTTPException(403, detail={"code": "callback_audience_denied"})
    operation = await db.scalar(
        select(ProductOperation).where(ProductOperation.tenant_id == actor.org_id, ProductOperation.id == operation_id).with_for_update()
    )
    if operation is None or operation.operation != "startHandoff":
        raise HTTPException(404, detail={"code": "not_found"})
    if operation.request.get("callback_scope") != actor.token_scopes or actor.user_id != operation.request.get("callback_producer", PRODUCER):
        raise HTTPException(403, detail={"code": "callback_audience_denied"})
    deadline = operation.expires_at
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    if deadline <= utcnow():
        raise HTTPException(410, detail={"code": "operation_expired"})
    peer = request.receipt
    if (
        peer.tenant_id != operation.tenant_id
        or peer.product_id != operation.product_id
        or peer.project_id != operation.project_id
        or peer.profile_revision != operation.expected_revision
        or request.binding_generation != operation.request["generation"]
    ):
        raise HTTPException(409, detail={"code": "callback_scope_mismatch"})
    member_actor = actor.model_copy(update={"account_type": "human", "user_id": operation.actor_id, "cognito_username": "", "is_admin": False})
    try:
        member, product, _ = await scoped_product(db, member_actor, operation.product_id, operation.project_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        # A stored initiator can be an administrator awaiting team assignment.
        # Revalidate its current global authority; the callback carries no grant.
        member_actor = await stored_product_actor(db, user_id=operation.actor_id, tenant_id=operation.tenant_id)
        member, product, _ = await scoped_product(db, member_actor, operation.product_id, operation.project_id)
    member_actor = member_actor.model_copy(update={"team_id": member.team_id or ""})
    session = await db.get(ProductOnboardingSession, (actor.org_id, operation.session_id))
    if session is None or session.state not in ("open", "waiting_external") or product.archived_at is not None:
        raise HTTPException(409, detail={"code": "operation_cancelled"})
    if operation.state in ("cancelled", "expired"):
        raise HTTPException(409, detail={"code": "operation_cancelled"})
    if request.sequence <= operation.callback_sequence:
        if request.sequence == operation.callback_sequence:
            if operation.receipt != {"peer_receipt": peer.model_dump(), "sequence": request.sequence}:
                raise HTTPException(409, detail={"code": "callback_conflict"})
            return {"operation_id": operation.id, "status": "duplicate"}
        return {"operation_id": operation.id, "status": "ignored_older"}
    if operation.state in ("succeeded", "failed"):
        raise HTTPException(409, detail={"code": "operation_resolved"})
    if operation.provider_operation_id is not None and operation.provider_operation_id != peer.provider_operation_id:
        raise HTTPException(409, detail={"code": "callback_conflict"})
    if peer.status == "succeeded":
        observed_id = await observed_handoff_effect(db, member_actor, operation, provider_operation_id=peer.provider_operation_id)
        if observed_id != peer.provider_operation_id:
            raise HTTPException(409, detail={"code": "effect_unverified"})
    operation.callback_sequence = request.sequence
    operation.provider_operation_id = peer.provider_operation_id
    operation.receipt = {"peer_receipt": peer.model_dump(), "sequence": request.sequence}
    operation.state = "succeeded" if peer.status == "succeeded" else "failed" if peer.status in ("failed", "denied") else "waiting_external"
    operation.updated_at = utcnow()
    if operation.state == "succeeded":
        await db.flush()
        pending = await db.scalar(
            select(ProductOperation.id).where(
                ProductOperation.tenant_id == actor.org_id,
                ProductOperation.session_id == session.id,
                ProductOperation.state.in_(("pending", "running", "waiting_external", "outcome_unknown")),
            )
        )
        if pending is None:
            session.state = "open"
    await db.commit()
    return {"operation_id": operation.id, "status": "applied"}
