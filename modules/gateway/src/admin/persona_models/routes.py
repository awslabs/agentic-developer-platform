"""Delegated-administration persona-model preference endpoints — Issue #5419 (PMM-02).

``/service-principals/{canonical_id}/persona-models`` — human org-admin only.

Every handler checks ``account_type == "human"`` and ``auth_source == "jwt"``
and ``ORG_UPDATE`` as its first executable statement, plus an explicit
same-tenant check on the target canonical service principal.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.agentauth.task_service_policy import TaskServicePolicyError, TaskServicePolicyStore
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext
from src.usage.persona_cost import get_persona_cost_report

from . import catalogue_routes, catalogue_service, service
from .catalogue import persona_compatibility_class
from .catalogue_schemas import ModelCatalogueResponse
from .schemas import (
    AliasResponse,
    ConflictResponse,
    LinkAliasRequest,
    PersonaCostResponse,
    PreferenceDetailResponse,
    PreferenceListResponse,
    RegisterServicePrincipalRequest,
    RegisterServicePrincipalResponse,
    ResetPreferenceRequest,
    ResetPreferenceResponse,
    SetPreferenceRequest,
    StatusTransitionRequest,
    StatusTransitionResponse,
    TaskPolicyPutRequest,
    TaskPolicyResponse,
)

logger = logging.getLogger("bedrockgateway.persona_models.admin")

router = APIRouter(prefix="/service-principals", tags=["persona-models-admin"])


def _rejected(exc: service.PreferenceRejectedError) -> HTTPException:
    return HTTPException(status_code=422, detail={"reason": exc.reason, "message": exc.message})


async def _require_human_org_admin(db: AsyncSession, current_user: TokenContext) -> None:
    """Enforce: human + JWT + ORG_UPDATE.  First statement of every handler."""
    if current_user.account_type != "human":
        raise HTTPException(status_code=403, detail="Administration requires a human caller.")
    if current_user.auth_source != "jwt":
        raise HTTPException(status_code=403, detail="Administration requires JWT authentication.")
    ac = AccessControl(db)
    await ac.check_permission(current_user, Permission.ORG_UPDATE, target_org_id=current_user.org_id)


@router.get("/{canonical_id}/persona-models", response_model=PreferenceListResponse)
async def list_service_principal_preferences(
    canonical_id: str,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PreferenceListResponse:
    """List preferences for an administered service principal."""
    await _require_human_org_admin(db, current_user)

    try:
        await service.validate_target_service_principal(db, canonical_id=canonical_id, org_id=current_user.org_id)
    except service.PreferenceRejectedError as exc:
        raise _rejected(exc) from exc

    entries = await service.build_preference_list(db, org_id=current_user.org_id, principal_kind="service_account", principal_id=canonical_id)
    return PreferenceListResponse(
        tenant_id=current_user.org_id,
        principal_kind="service_account",
        principal_id=canonical_id,
        entries=entries,
    )


@router.get("/{canonical_id}/persona-models/costs", response_model=PersonaCostResponse)
async def get_service_principal_persona_costs(
    canonical_id: str,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    chain_id: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
) -> PersonaCostResponse:
    """Return tenant-scoped usage-ledger costs for an administered principal."""
    await _require_human_org_admin(db, current_user)
    try:
        await service.validate_target_service_principal(
            db,
            canonical_id=canonical_id,
            org_id=current_user.org_id,
        )
    except service.PreferenceRejectedError as exc:
        raise _rejected(exc) from exc
    report = await get_persona_cost_report(
        db,
        org_id=current_user.org_id,
        principal_kind="service_account",
        principal_id=canonical_id,
        chain_id=chain_id,
    )
    return PersonaCostResponse(
        **{
            **report.__dict__,
            "status": report.status.value,
            "entries": [entry.__dict__ for entry in report.entries],
        }
    )


@router.get("/{canonical_id}/persona-models/catalog", response_model=ModelCatalogueResponse)
async def get_service_principal_catalogue(
    canonical_id: str,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    persona_key: Annotated[str, Query(description="Persona whose selectable models are requested.")],
) -> ModelCatalogueResponse:
    """Read the catalogue for an administered service principal.

    The human administrator is only the actor.  Destination, lifecycle and
    principal restrictions are evaluated for the tenant-checked service
    principal named by ``canonical_id``.  This keeps the managed UI/CLI view
    identical to the save path instead of accidentally projecting the human
    administrator's catalogue.
    """
    await _require_human_org_admin(db, current_user)

    try:
        target = await service.validate_target_service_principal(
            db,
            canonical_id=canonical_id,
            org_id=current_user.org_id,
        )
    except service.PreferenceRejectedError as exc:
        raise _rejected(exc) from exc

    compatibility_class = persona_compatibility_class(persona_key)
    if compatibility_class is None:
        raise _rejected(
            service.PreferenceRejectedError(
                "unknown_persona",
                f"Unknown persona key '{persona_key}'.",
            )
        )

    target_context = current_user.model_copy(
        update={
            "user_id": canonical_id,
            "team_id": "",
            "department_id": "",
            "account_type": "service",
            "canonical_service_principal_id": canonical_id,
        }
    )
    account_id, region = await catalogue_routes.resolve_effective_destination(
        db,
        target_context,
        routing_user_id="",
    )
    restriction_pattern_sets, policy_unavailable_reason = await catalogue_routes.resolve_managed_service_restriction_policy(
        db,
        org_id=current_user.org_id,
        canonical_service_principal_id=canonical_id,
    )
    models = await catalogue_service.build_model_catalogue(
        db,
        persona_key=persona_key,
        account_id=account_id,
        region=region,
        principal_kind="service_account",
        canonical_principal_id=canonical_id,
        principal_status=target.status,
        service_restriction_pattern_sets=restriction_pattern_sets,
        policy_unavailable_reason=policy_unavailable_reason,
        tenant_allowed_patterns=None,
    )
    return ModelCatalogueResponse(
        tenant_id=current_user.org_id,
        persona_key=persona_key,
        compatibility_class=compatibility_class,
        models=models,
    )


@router.get("/{canonical_id}/persona-models/explain/{persona_key}", response_model=PreferenceDetailResponse)
async def explain_service_principal_preference(
    canonical_id: str,
    persona_key: str,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PreferenceDetailResponse:
    """Explain a service principal's effective model for one persona."""
    await _require_human_org_admin(db, current_user)

    try:
        await service.validate_target_service_principal(db, canonical_id=canonical_id, org_id=current_user.org_id)
        result = await service.build_explain(
            db,
            org_id=current_user.org_id,
            principal_kind="service_account",
            principal_id=canonical_id,
            persona_key=persona_key,
        )
    except service.PreferenceRejectedError as exc:
        raise _rejected(exc) from exc

    return PreferenceDetailResponse(tenant_id=current_user.org_id, **result)


@router.put("/{canonical_id}/persona-models/{persona_key}")
async def set_service_principal_preference(
    canonical_id: str,
    persona_key: str,
    request: SetPreferenceRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PreferenceDetailResponse:
    """Set a model preference for a service principal."""
    await _require_human_org_admin(db, current_user)

    # Resolve the ACTOR before the target, so the refusal below has a canonical
    # actor_id to record. This raises PreferenceRejectedError when the caller has
    # no users row in this tenant, which must surface as the same 422 as every
    # other refusal on this surface — unwrapped it escaped as a 500.
    try:
        admin_id = await service.validate_human_principal(db, user_id=current_user.user_id, org_id=current_user.org_id)
    except service.PreferenceRejectedError as exc:
        # No audit: the actor could not be resolved to a canonical ID, and §5.5
        # forbids writing a raw subject into actor_id.
        raise _rejected(exc) from exc

    try:
        target = await service.validate_target_service_principal(db, canonical_id=canonical_id, org_id=current_user.org_id)
    except service.PreferenceRejectedError as exc:
        # An administrator naming a principal that is absent from their tenant is
        # the most suspicious act this surface allows — it is how a cross-tenant
        # write would be attempted. §5.6 requires it be audited in the CALLER's
        # tenant (never the target's, which would put a row in the victim's trail
        # while still refusing). Previously this refusal returned 422 and recorded
        # nothing anywhere, so the attempt left no trace at all.
        await service.write_refusal_audit(
            db,
            event_type="persona_model_admin_set_rejected",
            org_id=current_user.org_id,
            actor_id=admin_id,
            details={
                "target_principal_id": canonical_id,
                "persona_key": persona_key,
                "model": request.model,
                "reason": exc.reason,
                "actor_kind": "human_admin",
                "subject_key": canonical_id,
            },
        )
        raise _rejected(exc) from exc

    if target.status == "retired":
        raise _rejected(
            service.PreferenceRejectedError(
                "principal_retired",
                f"Service principal '{canonical_id}' is retired and cannot accept preference changes.",
            )
        )

    # Capture before-state for audit
    before = await service.get_preference(
        db,
        org_id=current_user.org_id,
        principal_kind="service_account",
        principal_id=canonical_id,
        persona_key=persona_key,
    )
    before_model = before.canonical_model_id if before else None

    # Determine the principal_source from the first active alias.
    # The org_id predicate is required: without it a cross-tenant alias could
    # leak into the admin path when two tenants share a canonical_id string.
    alias = await db.scalar(
        select(service.ServicePrincipalAlias).where(
            service.ServicePrincipalAlias.canonical_service_principal_id == canonical_id,
            service.ServicePrincipalAlias.org_id == current_user.org_id,
            service.ServicePrincipalAlias.is_active == True,  # noqa: E712
        )
    )
    principal_source = alias.alias_source if alias else "sa_registration"

    # Resolve the administered service principal's destination, never the
    # human administrator's user/team destination. There is no service-specific
    # mapping rung today, so the target is eligible only for its org/platform
    # rungs and cannot accidentally collide with a users.id value.
    target_context = current_user.model_copy(
        update={
            "user_id": canonical_id,
            "team_id": "",
            "department_id": "",
            "account_type": "service",
            "canonical_service_principal_id": canonical_id,
        }
    )
    account_id, region = await catalogue_routes.resolve_effective_destination(
        db,
        target_context,
        routing_user_id="",
    )
    restriction_pattern_sets, policy_unavailable_reason = await catalogue_routes.resolve_managed_service_restriction_policy(
        db,
        org_id=current_user.org_id,
        canonical_service_principal_id=canonical_id,
    )

    try:
        row = await service.set_preference(
            db,
            org_id=current_user.org_id,
            principal_kind="service_account",
            principal_source=principal_source,
            principal_id=canonical_id,
            persona_key=persona_key,
            model=request.model,
            expected_revision=request.expected_revision,
            actor_id=admin_id,
            actor_source="self",
            validation_account_id=account_id,
            validation_region=region,
            validation_principal_status=target.status,
            validation_service_restriction_pattern_sets=restriction_pattern_sets,
            validation_policy_unavailable_reason=policy_unavailable_reason,
        )
    except service.PreferenceConflictError as exc:
        compatibility_class, default_model_id, class_default_status = await service.get_persona_class_default(db, persona_key)
        return JSONResponse(
            status_code=409,
            content=ConflictResponse(
                tenant_id=current_user.org_id,
                persona_key=persona_key,
                compatibility_class=compatibility_class,
                harness_contract_revision=service.persona_harness_contract_revision(persona_key),
                principal_kind="service_account",
                principal_id=canonical_id,
                effective_model_id=exc.row.canonical_model_id,
                current_model_id=exc.row.canonical_model_id,
                current_revision=exc.row.revision,
                updated_at=exc.row.updated_at,
                updated_by=exc.row.updated_by,
                default_model_id=default_model_id,
                default_source=compatibility_class,
                class_default_status=class_default_status,
            ).model_dump(mode="json"),
        )
    except service.PreferenceRejectedError as exc:
        await service.write_refusal_audit(
            db,
            event_type="persona_model_admin_set_rejected",
            org_id=current_user.org_id,
            actor_id=admin_id,
            details={
                "target_principal_id": canonical_id,
                "persona_key": persona_key,
                "model": request.model,
                "reason": exc.reason,
                "actor_kind": "human_admin",
                "subject_key": canonical_id,
            },
        )
        raise _rejected(exc) from exc

    await service.write_audit(
        db,
        event_type="persona_model_admin_set",
        org_id=current_user.org_id,
        actor_id=admin_id,
        details={
            "principal_kind": "service_account",
            "principal_id": canonical_id,
            "persona_key": persona_key,
            "before_model": before_model,
            "after_model": row.canonical_model_id,
            "requested_alias": request.model,
            "revision": row.revision,
            "actor_kind": "human_admin",
            "subject_key": canonical_id,
            # The subject's own provenance, beside the actor's — §5.5 keeps both
            # as evidence and neither as part of what identifies the row.
            "principal_source": principal_source,
            "updated_by_source": "self",
        },
    )
    await db.commit()

    result = await service.build_explain(
        db,
        org_id=current_user.org_id,
        principal_kind="service_account",
        principal_id=canonical_id,
        persona_key=persona_key,
    )
    return PreferenceDetailResponse(tenant_id=current_user.org_id, **result)


@router.delete("/{canonical_id}/persona-models/{persona_key}")
async def reset_service_principal_preference(
    canonical_id: str,
    persona_key: str,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Annotated[ResetPreferenceRequest | None, Body()] = None,
) -> ResetPreferenceResponse:
    """Reset a service principal's preference so the default becomes effective."""
    await _require_human_org_admin(db, current_user)

    # Resolve the ACTOR before the target, so the refusal below has a canonical
    # actor_id to record. This raises PreferenceRejectedError when the caller has
    # no users row in this tenant, which must surface as the same 422 as every
    # other refusal on this surface — unwrapped it escaped as a 500.
    try:
        admin_id = await service.validate_human_principal(db, user_id=current_user.user_id, org_id=current_user.org_id)
    except service.PreferenceRejectedError as exc:
        # No audit: the actor could not be resolved to a canonical ID, and §5.5
        # forbids writing a raw subject into actor_id.
        raise _rejected(exc) from exc

    try:
        await service.validate_target_service_principal(db, canonical_id=canonical_id, org_id=current_user.org_id)
    except service.PreferenceRejectedError as exc:
        # Audited in the caller's tenant for the same reason as the set path.
        await service.write_refusal_audit(
            db,
            event_type="persona_model_admin_reset_rejected",
            org_id=current_user.org_id,
            actor_id=admin_id,
            details={
                "target_principal_id": canonical_id,
                "persona_key": persona_key,
                "reason": exc.reason,
                "actor_kind": "human_admin",
                "subject_key": canonical_id,
            },
        )
        raise _rejected(exc) from exc

    # Same gate as the self reset path: without it an unknown key ran the delete
    # and then raised out of build_explain with no handler, returning 500 for a
    # caller error.
    if persona_key not in service.PERSONA_KEYS:
        raise _rejected(service.PreferenceRejectedError("unknown_persona", f"Unknown persona key '{persona_key}'."))

    existing = await service.get_preference(
        db,
        org_id=current_user.org_id,
        principal_kind="service_account",
        principal_id=canonical_id,
        persona_key=persona_key,
    )

    try:
        removed = await service.reset_preference(
            db,
            org_id=current_user.org_id,
            principal_kind="service_account",
            principal_id=canonical_id,
            persona_key=persona_key,
            expected_revision=request.expected_revision if request else None,
        )
    except service.PreferenceConflictError as exc:
        compatibility_class, default_model_id, class_default_status = await service.get_persona_class_default(db, persona_key)
        return JSONResponse(
            status_code=409,
            content=ConflictResponse(
                tenant_id=current_user.org_id,
                persona_key=persona_key,
                compatibility_class=compatibility_class,
                harness_contract_revision=service.persona_harness_contract_revision(persona_key),
                principal_kind="service_account",
                principal_id=canonical_id,
                effective_model_id=exc.row.canonical_model_id,
                current_model_id=exc.row.canonical_model_id,
                current_revision=exc.row.revision,
                updated_at=exc.row.updated_at,
                updated_by=exc.row.updated_by,
                default_model_id=default_model_id,
                default_source=compatibility_class,
                class_default_status=class_default_status,
            ).model_dump(mode="json"),
        )

    if removed and existing:
        await service.write_audit(
            db,
            event_type="persona_model_admin_reset",
            org_id=current_user.org_id,
            actor_id=admin_id,
            details={
                "principal_kind": "service_account",
                "principal_id": canonical_id,
                "persona_key": persona_key,
                "before_model": existing.canonical_model_id,
                "after_model": None,
                "revision": existing.revision,
                "actor_kind": "human_admin",
                "subject_key": canonical_id,
                "principal_source": existing.principal_source,
                "updated_by_source": "self",
            },
        )
    await db.commit()

    result = await service.build_explain(
        db,
        org_id=current_user.org_id,
        principal_kind="service_account",
        principal_id=canonical_id,
        persona_key=persona_key,
    )
    return ResetPreferenceResponse(tenant_id=current_user.org_id, removed=removed, **result)


# ── Service-principal lifecycle ─────────────────────────────────────────────


@router.post("/register", response_model=RegisterServicePrincipalResponse)
async def register_service_principal(
    request: RegisterServicePrincipalRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RegisterServicePrincipalResponse:
    """Register a new canonical service principal and its first alias.

    Human org-admin only. Re-registering a revoked alias creates a new
    canonical principal — revoked aliases cannot be reactivated.
    """
    await _require_human_org_admin(db, current_user)

    admin_id = await service.validate_human_principal(db, user_id=current_user.user_id, org_id=current_user.org_id)

    try:
        principal, alias = await service.register_service_principal(
            db,
            org_id=current_user.org_id,
            display_name=request.display_name,
            alias_source=request.alias_source,
            alias_id=request.alias_id,
            approved_by=admin_id,
        )
    except service.PreferenceRejectedError as exc:
        raise _rejected(exc) from exc

    await service.write_audit(
        db,
        event_type="service_principal_registered",
        org_id=current_user.org_id,
        actor_id=admin_id,
        details={
            "canonical_principal_id": principal.canonical_service_principal_id,
            "display_name": principal.display_name,
            "alias_source": alias.alias_source,
            "alias_id": alias.alias_id,
            "actor_kind": "human_admin",
        },
    )
    await db.commit()

    return RegisterServicePrincipalResponse(
        canonical_service_principal_id=principal.canonical_service_principal_id,
        display_name=principal.display_name,
        alias_source=alias.alias_source,
        alias_id=alias.alias_id,
        status=principal.status,
    )


@router.post("/{canonical_id}/aliases", response_model=AliasResponse)
async def link_alias(
    canonical_id: str,
    request: LinkAliasRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AliasResponse:
    """Link an additional alias to an existing service principal."""
    await _require_human_org_admin(db, current_user)

    admin_id = await service.validate_human_principal(db, user_id=current_user.user_id, org_id=current_user.org_id)

    try:
        alias = await service.link_alias(
            db,
            canonical_id=canonical_id,
            org_id=current_user.org_id,
            alias_source=request.alias_source,
            alias_id=request.alias_id,
            registered_by=admin_id,
        )
    except service.PreferenceRejectedError as exc:
        raise _rejected(exc) from exc

    await service.write_audit(
        db,
        event_type="service_principal_alias_linked",
        org_id=current_user.org_id,
        actor_id=admin_id,
        details={
            "canonical_principal_id": canonical_id,
            "alias_source": alias.alias_source,
            "alias_id": alias.alias_id,
            "actor_kind": "human_admin",
        },
    )
    await db.commit()

    return AliasResponse(
        alias_id=alias.alias_id,
        alias_source=alias.alias_source,
        canonical_service_principal_id=canonical_id,
        is_active=alias.is_active,
        registered_by=alias.registered_by,
    )


@router.patch("/{canonical_id}/status", response_model=StatusTransitionResponse)
async def transition_service_principal_status(
    canonical_id: str,
    request: StatusTransitionRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StatusTransitionResponse:
    """Transition a service principal's lifecycle status.

    Human org-admin only. Valid transitions:
    - active → suspended, active → retired
    - suspended → active, suspended → retired
    - retired is terminal (no transitions out)
    """
    await _require_human_org_admin(db, current_user)

    admin_id = await service.validate_human_principal(db, user_id=current_user.user_id, org_id=current_user.org_id)

    try:
        target = await service.validate_target_service_principal(db, canonical_id=canonical_id, org_id=current_user.org_id)
        previous_status = target.status
        principal = await service.transition_service_principal_status(
            db,
            canonical_id=canonical_id,
            org_id=current_user.org_id,
            new_status=request.status,
        )
    except service.PreferenceRejectedError as exc:
        # A refused transition is audited for the same reason the refused set and
        # reset on this surface are (§5.5): the two refusals reachable here are an
        # attempt to reinstate a retired principal and an attempt to transition one
        # in another tenant, and both are exactly what an audit trail exists to
        # show. Raising bare left no trace of either.
        await service.write_refusal_audit(
            db,
            event_type="service_principal_status_transition_rejected",
            org_id=current_user.org_id,
            actor_id=admin_id,
            details={
                "target_principal_id": canonical_id,
                "requested_status": request.status,
                "reason": exc.reason,
                "actor_kind": "human_admin",
                "subject_key": canonical_id,
            },
        )
        raise _rejected(exc) from exc

    await service.write_audit(
        db,
        event_type="service_principal_status_changed",
        org_id=current_user.org_id,
        actor_id=admin_id,
        details={
            "canonical_principal_id": canonical_id,
            "previous_status": previous_status,
            "new_status": principal.status,
            "actor_kind": "human_admin",
        },
    )
    await db.commit()

    return StatusTransitionResponse(
        canonical_service_principal_id=principal.canonical_service_principal_id,
        display_name=principal.display_name,
        previous_status=previous_status,
        status=principal.status,
    )


@router.delete("/{canonical_id}/aliases/{alias_row_id}", response_model=AliasResponse)
async def revoke_alias(
    canonical_id: str,
    alias_row_id: str,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AliasResponse:
    """Revoke an alias. Revoked aliases cannot be reactivated."""
    await _require_human_org_admin(db, current_user)

    admin_id = await service.validate_human_principal(db, user_id=current_user.user_id, org_id=current_user.org_id)

    try:
        alias = await service.revoke_alias(
            db,
            alias_row_id=alias_row_id,
            canonical_id=canonical_id,
            org_id=current_user.org_id,
            revoked_by=admin_id,
        )
    except service.PreferenceRejectedError as exc:
        raise _rejected(exc) from exc

    await service.write_audit(
        db,
        event_type="service_principal_alias_revoked",
        org_id=current_user.org_id,
        actor_id=admin_id,
        details={
            "canonical_principal_id": canonical_id,
            "alias_row_id": alias_row_id,
            "alias_source": alias.alias_source,
            "alias_id": alias.alias_id,
            "actor_kind": "human_admin",
        },
    )
    await db.commit()

    return AliasResponse(
        alias_id=alias.alias_id,
        alias_source=alias.alias_source,
        canonical_service_principal_id=canonical_id,
        is_active=alias.is_active,
        registered_by=alias.registered_by,
    )


def task_policy_store() -> TaskServicePolicyStore:
    return TaskServicePolicyStore()


@router.get("/{canonical_id}/task-policy", response_model=TaskPolicyResponse)
async def get_task_policy(
    canonical_id: str,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    policy_store: Annotated[TaskServicePolicyStore, Depends(task_policy_store)],
) -> TaskPolicyResponse:
    await _require_human_org_admin(db, current_user)
    try:
        await service.validate_target_service_principal(db, canonical_id=canonical_id, org_id=current_user.org_id)
        policy = policy_store.get(tenant_id=current_user.org_id, canonical_principal_id=canonical_id)
    except service.PreferenceRejectedError as exc:
        raise _rejected(exc) from exc
    except TaskServicePolicyError:
        raise HTTPException(503, "task policy unavailable") from None
    if policy is None:
        raise HTTPException(404, "task policy not found")
    return TaskPolicyResponse.model_validate(policy)


@router.put("/{canonical_id}/task-policy", response_model=TaskPolicyResponse)
async def put_task_policy(
    canonical_id: str,
    body: TaskPolicyPutRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    policy_store: Annotated[TaskServicePolicyStore, Depends(task_policy_store)],
) -> TaskPolicyResponse:
    await _require_human_org_admin(db, current_user)
    try:
        await service.validate_target_service_principal(db, canonical_id=canonical_id, org_id=current_user.org_id)
        admin_id = await service.validate_human_principal(
            db, user_id=current_user.user_id, org_id=current_user.org_id
        )
        values = body.model_dump(exclude={"expected_version"}, mode="python")
        policy = policy_store.put(
            tenant_id=current_user.org_id, canonical_principal_id=canonical_id,
            expected_version=body.expected_version, policy=values, updated_by=admin_id,
        )
    except service.PreferenceRejectedError as exc:
        raise _rejected(exc) from exc
    except TaskServicePolicyError as exc:
        if exc.code == "version_conflict":
            raise HTTPException(409, "task policy version conflict") from None
        if exc.code == "invalid_policy":
            raise HTTPException(422, "invalid task policy") from None
        raise HTTPException(503, "task policy unavailable") from None
    return TaskPolicyResponse.model_validate(policy)
