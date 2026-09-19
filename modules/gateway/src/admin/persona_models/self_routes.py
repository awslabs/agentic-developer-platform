"""Self-service persona-model preference endpoints — Issue #5419 (PMM-02).

``/me/persona-models`` — one router serving both human JWT and SigV4 service
callers.  No target parameter names a principal at any position: the caller's
identity is derived server-side from the authenticated context.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.models.persona_models import ServicePrincipal
from src.shared.schemas.auth import TokenContext

from . import service
from .schemas import (
    ConflictResponse,
    ManageableServicePrincipalsResponse,
    PreferenceDetailResponse,
    PreferenceListResponse,
    SetPreferenceRequest,
)

logger = logging.getLogger("bedrockgateway.persona_models.self")

router = APIRouter(prefix="/me/persona-models", tags=["persona-models"])


def _rejected(exc: service.PreferenceRejectedError) -> HTTPException:
    return HTTPException(status_code=422, detail={"reason": exc.reason, "message": exc.message})


async def get_persona_model_current_user(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenContext:
    """Request-scoped dependency that resolves the canonical service principal.

    Runs after ``get_current_user`` and uses the request-scoped DB session
    (no side-channel global-factory session).

    For service callers, resolves ``canonical_service_principal_id`` using the
    exact trusted ``canonical_alias_source`` stamped during authentication.
    DB failures propagate as 500 (not silent degradation) because a preference
    route that cannot verify identity must not proceed.

    For human callers, this is a pass-through — the canonical user ID is resolved
    later in ``_resolve_caller`` via ``validate_human_principal``.
    """
    if current_user.account_type != "service":
        return current_user

    trusted_source = current_user.canonical_alias_source
    if not trusted_source:
        # No auth adapter stamps this source — the caller has no self-auth path.
        return current_user

    canonical_id, matched_source = await service.resolve_by_exact_source(
        db,
        alias_source=trusted_source,
        alias_id=current_user.user_id,
        org_id=current_user.org_id,
    )
    if canonical_id:
        return current_user.model_copy(
            update={
                "canonical_service_principal_id": canonical_id,
                "canonical_alias_source": matched_source,
            }
        )

    return current_user


async def _resolve_caller(db: AsyncSession, current_user: TokenContext) -> tuple[str, str, str]:
    """Resolve the caller to (principal_kind, principal_source, principal_id).

    When ``canonical_service_principal_id`` is populated by the enrichment
    dependency, uses it directly rather than re-resolving.

    Human callers always resolve via ``validate_human_principal`` (the
    ``canonical_service_principal_id`` field is intentionally empty for humans).
    """
    principal_kind = service.derive_principal_kind(current_user.account_type)
    principal_source = service.derive_principal_source(
        current_user.account_type,
        current_user.auth_source,
        current_user.canonical_alias_source,
    )

    if principal_kind == "human":
        # Human callers resolve through resolve_canonical_user_id → users.id
        canonical_id = await service.validate_human_principal(db, user_id=current_user.user_id, org_id=current_user.org_id)
        return principal_kind, principal_source, canonical_id

    # Service caller — require canonical identity from the enrichment dependency.
    # A missing canonical field means the caller has no registered alias (either
    # no alias_source was stamped or no alias row matched).
    if not current_user.canonical_service_principal_id:
        raise service.PreferenceRejectedError(
            "unregistered_service_principal",
            "This service account has no registered canonical identity. A human administrator must register it before it can manage preferences.",
        )

    # Confirm the principal is active in-tenant
    principal = await db.scalar(
        sa_select(ServicePrincipal).where(
            ServicePrincipal.canonical_service_principal_id == current_user.canonical_service_principal_id,
            ServicePrincipal.org_id == current_user.org_id,
            ServicePrincipal.status == "active",
        )
    )
    if principal is None:
        raise service.PreferenceRejectedError(
            "principal_not_active",
            "The service principal associated with this caller is not active in this tenant.",
        )
    return principal_kind, principal_source, current_user.canonical_service_principal_id


@router.get("", response_model=PreferenceListResponse)
async def list_my_preferences(
    current_user: Annotated[TokenContext, Depends(get_persona_model_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PreferenceListResponse:
    """List all persona preferences for the authenticated caller."""
    try:
        kind, _source, pid = await _resolve_caller(db, current_user)
    except service.PreferenceRejectedError as exc:
        raise _rejected(exc) from exc

    entries = await service.build_preference_list(db, org_id=current_user.org_id, principal_kind=kind, principal_id=pid)
    return PreferenceListResponse(principal_kind=kind, principal_id=pid, entries=entries)


@router.get("/explain/{persona_key}", response_model=PreferenceDetailResponse)
async def explain_my_preference(
    persona_key: str,
    current_user: Annotated[TokenContext, Depends(get_persona_model_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PreferenceDetailResponse:
    """Explain why a specific model is effective for a persona."""
    try:
        kind, _source, pid = await _resolve_caller(db, current_user)
        result = await service.build_explain(db, org_id=current_user.org_id, principal_kind=kind, principal_id=pid, persona_key=persona_key)
    except service.PreferenceRejectedError as exc:
        raise _rejected(exc) from exc
    return PreferenceDetailResponse(**result)


@router.put("/{persona_key}")
async def set_my_preference(
    persona_key: str,
    request: SetPreferenceRequest,
    current_user: Annotated[TokenContext, Depends(get_persona_model_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PreferenceDetailResponse:
    """Set a model preference for a persona.

    While the PMM-03 validation service is not yet integrated, all writes are
    refused with ``probing_disabled`` and an actionable message.
    """
    try:
        kind, source, pid = await _resolve_caller(db, current_user)
    except service.PreferenceRejectedError as exc:
        # actor_id stays NULL here on purpose. This refusal means the caller was
        # never resolved to a canonical ID, so the only identifier available is
        # the raw subject — and §5.5 reserves `actor_id` for canonical IDs
        # exclusively, because an actor column mixing canonical IDs with raw
        # per-auth-path subjects cannot answer "everything this principal did".
        # The subject is still recorded, under a key that says what it is.
        await service.write_refusal_audit(
            db,
            event_type="persona_model_self_set_rejected",
            org_id=current_user.org_id,
            actor_id=None,
            details={
                "persona_key": persona_key,
                "model": request.model,
                "reason": exc.reason,
                "actor_kind": service.derive_principal_kind(current_user.account_type),
                "unresolved_subject": current_user.user_id,
                "auth_source": current_user.auth_source,
            },
        )
        raise _rejected(exc) from exc

    # Before-state for the audit record (§5.5 requires before and after, so a
    # reader can tell a first-time save from a change and see what was replaced).
    before = await service.get_preference(db, org_id=current_user.org_id, principal_kind=kind, principal_id=pid, persona_key=persona_key)
    before_model = before.canonical_model_id if before else None

    try:
        row = await service.set_preference(
            db,
            org_id=current_user.org_id,
            principal_kind=kind,
            principal_source=source,
            principal_id=pid,
            persona_key=persona_key,
            model=request.model,
            expected_revision=request.expected_revision,
            actor_id=pid,
            actor_source=source,
        )
    except service.PreferenceConflictError as exc:
        platform_default = await service.get_platform_default(db)
        default_model_id = platform_default.active_default_model_id if platform_default else None
        return JSONResponse(
            status_code=409,
            content=ConflictResponse(
                persona_key=persona_key,
                principal_kind=kind,
                principal_id=pid,
                effective_model_id=exc.row.canonical_model_id,
                current_model_id=exc.row.canonical_model_id,
                current_revision=exc.row.revision,
                updated_at=exc.row.updated_at,
                updated_by=exc.row.updated_by,
                default_model_id=default_model_id,
            ).model_dump(mode="json"),
        )
    except service.PreferenceRejectedError as exc:
        await service.write_refusal_audit(
            db,
            event_type="persona_model_self_set_rejected",
            org_id=current_user.org_id,
            actor_id=pid,
            details={
                "persona_key": persona_key,
                "model": request.model,
                "reason": exc.reason,
                "actor_kind": kind,
                "subject_key": pid,
                "principal_source": source,
            },
        )
        raise _rejected(exc) from exc

    await service.write_audit(
        db,
        event_type="persona_model_self_set",
        org_id=current_user.org_id,
        actor_id=pid,
        details={
            "principal_kind": kind,
            "principal_id": pid,
            "persona_key": persona_key,
            "before_model": before_model,
            "after_model": row.canonical_model_id,
            "requested_alias": request.model,
            "revision": row.revision,
            "actor_kind": kind,
            "subject_key": pid,
            "principal_source": source,
            "updated_by_source": source,
        },
    )
    await db.commit()

    result = await service.build_explain(db, org_id=current_user.org_id, principal_kind=kind, principal_id=pid, persona_key=persona_key)
    return PreferenceDetailResponse(**result)


@router.delete("/{persona_key}")
async def reset_my_preference(
    persona_key: str,
    current_user: Annotated[TokenContext, Depends(get_persona_model_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PreferenceDetailResponse:
    """Reset a persona preference so the platform default becomes effective."""
    try:
        kind, source, pid = await _resolve_caller(db, current_user)
    except service.PreferenceRejectedError as exc:
        # A refused reset is an attempted mutation by an unresolved caller and is
        # audited like the refused set; see that handler for why actor_id is NULL.
        await service.write_refusal_audit(
            db,
            event_type="persona_model_self_reset_rejected",
            org_id=current_user.org_id,
            actor_id=None,
            details={
                "persona_key": persona_key,
                "reason": exc.reason,
                "actor_kind": service.derive_principal_kind(current_user.account_type),
                "unresolved_subject": current_user.user_id,
                "auth_source": current_user.auth_source,
            },
        )
        raise _rejected(exc) from exc

    # Validate the persona key BEFORE mutating. Unlike the set path, which reaches
    # `validate_model_for_persona` and refuses there, reset had no such gate: an
    # unknown key ran the delete, then raised out of `build_explain` below with no
    # handler — a 500 on a caller error. It also meant the only rejection of a bad
    # key happened AFTER the write attempt.
    if persona_key not in service.PERSONA_KEYS:
        raise _rejected(service.PreferenceRejectedError("unknown_persona", f"Unknown persona key '{persona_key}'."))

    existing = await service.get_preference(db, org_id=current_user.org_id, principal_kind=kind, principal_id=pid, persona_key=persona_key)

    removed = await service.reset_preference(db, org_id=current_user.org_id, principal_kind=kind, principal_id=pid, persona_key=persona_key)

    if removed and existing:
        await service.write_audit(
            db,
            event_type="persona_model_self_reset",
            org_id=current_user.org_id,
            actor_id=pid,
            details={
                "principal_kind": kind,
                "principal_id": pid,
                "persona_key": persona_key,
                "before_model": existing.canonical_model_id,
                # The reset's whole effect is that no preference remains and the
                # platform default becomes effective; an absent after_model would
                # read as "not recorded" rather than "deliberately none".
                "after_model": None,
                "revision": existing.revision,
                "actor_kind": kind,
                "subject_key": pid,
                "principal_source": source,
                "updated_by_source": source,
            },
        )
    await db.commit()

    result = await service.build_explain(db, org_id=current_user.org_id, principal_kind=kind, principal_id=pid, persona_key=persona_key)
    return PreferenceDetailResponse(**result)


@router.get("/manageable-service-principals", response_model=ManageableServicePrincipalsResponse)
async def list_manageable_service_principals(
    current_user: Annotated[TokenContext, Depends(get_persona_model_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ManageableServicePrincipalsResponse:
    """Discover service principals the caller may administer.

    Restricted to human org-admins — service callers get 403.
    """
    if current_user.account_type != "human":
        raise HTTPException(status_code=403, detail="Only human callers may enumerate manageable service principals.")
    if current_user.auth_source != "jwt":
        raise HTTPException(status_code=403, detail="Administration requires JWT authentication.")

    from src.admin.access_control import AccessControl
    from src.admin.config import Permission

    ac = AccessControl(db)
    await ac.check_permission(current_user, Permission.ORG_UPDATE, target_org_id=current_user.org_id)

    principals = await service.list_manageable_service_principals(db, org_id=current_user.org_id)
    return ManageableServicePrincipalsResponse(principals=principals)
