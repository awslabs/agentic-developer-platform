"""Reconcile persisted handoffs by observation, never by repeating effects."""

from datetime import UTC

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.connections.github_client import github_account_id
from src.admin.installations.resolver import InstallationOwnershipError, assert_installation_owned_by
from src.auth.dependencies import get_current_user
from src.product_onboarding.callback_auth import mint_callback_token, verify_callback_token
from src.product_onboarding.callbacks import Callback, PeerReceipt, apply_callback
from src.product_onboarding.handoffs import handoff_view, observed_handoff_effect
from src.product_onboarding.routes import headers, preconditions
from src.products.models import ProductOnboardingSession, ProductOperation
from src.products.routes import require_profile_admission, scoped_product
from src.shared.database import get_db
from src.shared.identity.product_authority import stored_product_actor
from src.shared.models.base import utcnow
from src.shared.models.vault import ChannelTenantMap
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/v1/products/{product_id}/projects/{project_id}/operations", tags=["products"])


async def scoped_handoff(db: AsyncSession, actor: TokenContext, product_id: str, project_id: str, operation_id: str):
    member, product, _ = await scoped_product(db, actor, product_id, project_id)
    operation = await db.scalar(
        select(ProductOperation).where(ProductOperation.tenant_id == actor.org_id, ProductOperation.id == operation_id).with_for_update()
    )
    if (
        operation is None
        or operation.product_id != product_id
        or operation.project_id != project_id
        or operation.operation != "startHandoff"
        or member.id not in (operation.actor_id, product.owner_user_id)
    ):
        raise HTTPException(404, detail={"code": "not_found"})
    session = await db.get(ProductOnboardingSession, (actor.org_id, operation.session_id))
    if session is None or session.product_id != product_id or session.project_id != project_id:
        raise HTTPException(404, detail={"code": "not_found"})
    requester = actor.model_copy(update={"user_id": operation.actor_id, "cognito_username": "", "team_id": "", "is_admin": False})
    try:
        initiator, _, _ = await scoped_product(db, requester, product_id, project_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        requester = await stored_product_actor(db, user_id=operation.actor_id, tenant_id=operation.tenant_id)
        initiator, _, _ = await scoped_product(db, requester, product_id, project_id)
    requester = requester.model_copy(update={"team_id": initiator.team_id or ""})
    return product, operation, session, requester


async def release_session(db: AsyncSession, session: ProductOnboardingSession):
    if session.state != "waiting_external":
        return
    await db.flush()
    remaining = await db.scalar(
        select(ProductOperation.id).where(
            ProductOperation.tenant_id == session.tenant_id,
            ProductOperation.session_id == session.id,
            ProductOperation.state.in_(("pending", "running", "waiting_external", "outcome_unknown")),
        )
    )
    if remaining is None:
        session.state = "open"


@router.post("/{operation_id}/reconcile", status_code=202, operation_id="reconcileOperation", dependencies=[Depends(require_profile_admission)])
async def reconcile_operation(
    product_id: str,
    project_id: str,
    operation_id: str,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_revision: int | None = Header(default=None, alias="If-Profile-Revision"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    product, operation, session, requester = await scoped_handoff(db, actor, product_id, project_id, operation_id)
    preconditions(idempotency_key, expected_revision)
    if operation.state == "succeeded":
        response.status_code = 200
    elif product.revision != expected_revision:
        raise HTTPException(412, detail={"code": "stale_revision"})
    elif operation.request.get("request", {}).get("target") == "repository_create":
        from src.product_onboarding.repository_creation import finish_repository, reconcile_repository

        plan = operation.request["request"]["repository_plan"]
        mapping = await db.get(ChannelTenantMap, plan["connection_ref"]["id"])
        if (
            mapping is None
            or mapping.org_id != actor.org_id
            or mapping.provider != "github"
            or mapping.ownership_disputed
            or mapping.provider_scope_id != plan["owner_native_id"]
            or not github_account_id(mapping.installation_id)
        ):
            raise HTTPException(403, detail={"code": "permission_denied"})
        try:
            await assert_installation_owned_by(actor.org_id, int(mapping.installation_id), db=db)
        except InstallationOwnershipError:
            raise HTTPException(403, detail={"code": "permission_denied"}) from None
        try:
            origin_id = operation.request.get("resume_operation_id") or operation.id
            observed = await reconcile_repository(int(mapping.installation_id), plan, origin_id)
        except (httpx.HTTPError, OSError, ValueError):
            raise HTTPException(503, detail={"code": "source_unavailable"}) from None
        if observed is not None:
            if operation.provider_operation_id not in (None, observed["native_id"]):
                raise HTTPException(409, detail={"code": "effect_identity_conflict"})
            operation.provider_operation_id = observed["native_id"]
            operation.receipt = observed
            active = product.archived_at is None and operation.state not in ("cancelled", "expired", "failed")
            if active and observed["coverage"] != "complete":
                operation.state = "outcome_unknown"
                try:
                    observed = await finish_repository(int(mapping.installation_id), plan, origin_id, observed["native_id"])
                except HTTPException:
                    operation.updated_at = utcnow()
                    await db.commit()
                    raise
                except (httpx.HTTPError, OSError, ValueError):
                    operation.updated_at = utcnow()
                    await db.commit()
                    raise HTTPException(503, detail={"code": "source_unavailable"}) from None
                operation.receipt = observed
            if active:
                operation.state = "succeeded" if observed["coverage"] == "complete" else "outcome_unknown"
            operation.updated_at = utcnow()
            await release_session(db, session)
            await db.commit()
        elif operation.state == "cancelled":
            response.status_code = 200
    else:
        observed_id = await observed_handoff_effect(db, requester, operation)
        if observed_id is not None and (operation.state != "cancelled" or operation.provider_operation_id == observed_id):
            if operation.provider_operation_id is not None and operation.provider_operation_id != observed_id:
                raise HTTPException(409, detail={"code": "effect_identity_conflict"})
            deadline = operation.expires_at
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            if operation.state not in ("cancelled", "expired", "failed") and deadline > utcnow():
                token = mint_callback_token(operation)
                producer = verify_callback_token(token, operation)
                await apply_callback(
                    operation.id,
                    Callback(
                        sequence=operation.callback_sequence + 1,
                        binding_generation=operation.request["generation"],
                        receipt=PeerReceipt(
                            tenant_id=operation.tenant_id,
                            product_id=operation.product_id,
                            project_id=operation.project_id,
                            profile_revision=operation.expected_revision,
                            provider_operation_id=observed_id,
                            status="succeeded",
                        ),
                    ),
                    db=db,
                    actor=producer,
                )
            else:
                operation.provider_operation_id = observed_id
                operation.state = "succeeded"
                if operation.receipt is None:
                    operation.receipt = {"coverage": "complete", "provider_operation_id": observed_id}
                operation.updated_at = utcnow()
                await release_session(db, session)
                await db.commit()
        elif operation.state == "cancelled":
            response.status_code = 200
    headers(response, product.revision)
    if response.status_code != 200:
        response.headers["Location"] = f"/products/{product_id}/projects/{project_id}/operations/{operation_id}"
        response.headers["Retry-After"] = "2"
    return handoff_view(operation, product.revision)


@router.post("/{operation_id}/cancel", operation_id="cancelOperation", dependencies=[Depends(require_profile_admission)])
async def cancel_operation(
    product_id: str,
    project_id: str,
    operation_id: str,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_revision: int | None = Header(default=None, alias="If-Profile-Revision"),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    product, operation, session, requester = await scoped_handoff(db, actor, product_id, project_id, operation_id)
    preconditions(idempotency_key, expected_revision)
    if operation.state not in ("succeeded", "cancelled"):
        if product.revision != expected_revision:
            raise HTTPException(412, detail={"code": "stale_revision"})
        observed_id = await observed_handoff_effect(db, requester, operation)
        if observed_id is not None and operation.provider_operation_id in (None, observed_id):
            operation.provider_operation_id = observed_id
        operation.request = {**operation.request, "generation": operation.request["generation"] + 1}
        operation.state = "cancelled"
        operation.updated_at = utcnow()
        await release_session(db, session)
        await db.commit()
    headers(response, product.revision)
    return handoff_view(operation, product.revision)
