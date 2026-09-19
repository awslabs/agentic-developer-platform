"""Platform-admin runtime-posture administration — Issue #5425 (PMM-07).

``/admin/persona-models/posture`` — **platform admin only on every route**.

A separate module from ``routes.py`` on purpose.  That router's gate is
``ORG_UPDATE`` scoped to the caller's own organization, which is a *tenant*
admin; the policy-settings row deliberately carries no ``TenantMixin`` because
the enforcement posture applies across every tenant.  Handing a tenant admin the
ability to change it would let one workspace's administrator flip enforcement for
the whole platform.  It is equally not on the ``/me`` self surface, whose
inability to write platform settings is separately pinned.

Canonical §4.2 requires these writes to be platform-admin-only and fully
audited; §9 requires operational rollback to be this same audited operation.
Every handler calls ``require_platform_admin`` as its first executable statement.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.agentauth.runtime_posture import (
    RUNTIME_POSTURES,
    measured_cache_ttl_seconds,
)
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

from . import posture_service
from .posture_schemas import (
    RuntimePostureResponse,
    SetRuntimePostureRequest,
)

logger = logging.getLogger("bedrockgateway.persona_models.posture")

router = APIRouter(prefix="/admin/persona-models/posture", tags=["persona-models-posture"])


def _rejected(exc: posture_service.PostureMutationError) -> HTTPException:
    return HTTPException(status_code=422, detail={"reason": exc.reason, "message": exc.message})


def _conflict(exc: posture_service.PostureConflictError) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "reason": exc.reason,
            "message": exc.message,
            "current_posture": exc.current_posture,
            "current_posture_revision": exc.current_revision,
        },
    )


@router.get("/{compatibility_class}", response_model=RuntimePostureResponse)
async def get_runtime_posture(
    compatibility_class: str,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RuntimePostureResponse:
    """Read the current stored posture and the revision to pass when changing it.

    Raises:
        HTTPException: ``403`` for any caller who is not a platform admin;
            ``422`` for an unknown or unprovisioned compatibility class.
    """
    AccessControl(db).require_platform_admin(current_user)
    try:
        row = await posture_service.get_posture_setting(db, compatibility_class=compatibility_class)
    except posture_service.PostureMutationError as exc:
        raise _rejected(exc) from exc
    return RuntimePostureResponse(
        compatibility_class=row.compatibility_class,
        posture=row.enforcement_posture,
        posture_revision=row.posture_revision,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
        supported_postures=sorted(RUNTIME_POSTURES),
        propagation_bound_seconds=measured_cache_ttl_seconds(),
    )


@router.put("/{compatibility_class}", response_model=RuntimePostureResponse)
async def set_runtime_posture(
    compatibility_class: str,
    request: SetRuntimePostureRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RuntimePostureResponse:
    """Change the posture via an audited monotonic compare-and-set.

    This is also the §9 operational rollback operation: reverting enforcement is
    the same call with the earlier posture, so a rollback is as audited and as
    version-checked as the change that preceded it.  The response reports the
    measured window after which every gateway instance observes the new value.

    Raises:
        HTTPException:
            ``403`` for any caller who is not a platform admin;
            ``409`` when the expected revision is stale — nothing is written;
            ``422`` for an unsupported posture, an unknown or unprovisioned
            compatibility class, or an acting administrator who does not resolve
            to exactly one registered platform identity;
            ``422`` from schema validation for a non-integer or non-positive
            ``expected_revision``.
    """
    AccessControl(db).require_platform_admin(current_user)
    try:
        actor_id = await posture_service.resolve_posture_actor_id(db, current_user.user_id)
    except posture_service.PostureMutationError as exc:
        # No actor_id is passed on: the point of the refusal is that a raw token
        # subject must not be persisted anywhere, the refusal record included.
        await db.rollback()
        await posture_service.write_posture_refusal_audit(
            db,
            compatibility_class=compatibility_class,
            requested_posture=str(request.posture),
            reason=exc.reason,
            actor_id=None,
        )
        raise _rejected(exc) from exc

    try:
        row = await posture_service.set_runtime_posture(
            db,
            compatibility_class=compatibility_class,
            posture=request.posture,
            expected_revision=request.expected_revision,
            actor_id=actor_id,
            reason=request.reason,
        )
    except posture_service.PostureConflictError as exc:
        await db.rollback()
        await posture_service.write_posture_refusal_audit(
            db,
            compatibility_class=compatibility_class,
            requested_posture=request.posture,
            reason=exc.reason,
            actor_id=actor_id,
        )
        raise _conflict(exc) from exc
    except posture_service.PostureMutationError as exc:
        await db.rollback()
        await posture_service.write_posture_refusal_audit(
            db,
            compatibility_class=compatibility_class,
            requested_posture=str(request.posture),
            reason=exc.reason,
            actor_id=actor_id,
        )
        raise _rejected(exc) from exc

    await db.commit()
    posture_service.finalize_posture_commit(db)
    logger.info(
        "Runtime posture changed",
        extra={
            "compatibility_class": compatibility_class,
            "after_posture": row.enforcement_posture,
            "after_posture_revision": row.posture_revision,
        },
    )
    return RuntimePostureResponse(
        compatibility_class=row.compatibility_class,
        posture=row.enforcement_posture,
        posture_revision=row.posture_revision,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
        supported_postures=sorted(RUNTIME_POSTURES),
        propagation_bound_seconds=measured_cache_ttl_seconds(),
    )
