"""Audited platform-default promotion backed by an existing real SDK receipt."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.auth.dependencies import get_current_user
from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.base import utcnow
from src.shared.models.bedrock_routing import BedrockDestinationRegistry
from src.shared.models.persona_model_catalogue import ModelInvocabilityEvidence
from src.shared.models.persona_models import PersonaModelPolicySetting
from src.shared.schemas.auth import TokenContext

from .catalogue import catalogue_lookup, compatibility_class_harness_contract_revision
from .catalogue_service import compute_request_shape_sha256
from .posture_service import PLATFORM_AUDIT_ORG, PostureMutationError, get_posture_setting, resolve_posture_actor_id

router = APIRouter(prefix="/admin/persona-models/default", tags=["persona-model-defaults"])


class SetClassDefaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_model_id: str = Field(min_length=1, max_length=255)
    expected_revision: Annotated[StrictInt, Field(ge=1)]
    reason: str = Field(min_length=1, max_length=512)


class ClassDefaultResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    compatibility_class: str
    candidate_default_model_id: str | None
    active_default_model_id: str | None
    harness_contract_revision: str | None
    revision: int


def refused(reason: str, status: int = 422) -> HTTPException:
    return HTTPException(status, detail={"reason": reason})


@router.get("/{compatibility_class}", response_model=ClassDefaultResponse)
async def get_class_default(
    compatibility_class: str,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ClassDefaultResponse:
    AccessControl(db).require_platform_admin(current_user)
    try:
        row = await get_posture_setting(db, compatibility_class=compatibility_class)
    except PostureMutationError as exc:
        raise refused(exc.reason) from exc
    return ClassDefaultResponse.model_validate(row)


@router.put("/{compatibility_class}", response_model=ClassDefaultResponse)
async def set_class_default(
    compatibility_class: str,
    request: SetClassDefaultRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ClassDefaultResponse:
    AccessControl(db).require_platform_admin(current_user)
    actor_id = None
    try:
        actor_id = await resolve_posture_actor_id(db, current_user.user_id)
        row = await get_posture_setting(db, compatibility_class=compatibility_class)
        if row.revision != request.expected_revision:
            raise refused("default_revision_conflict", 409)
        model = catalogue_lookup(request.canonical_model_id)
        revision = compatibility_class_harness_contract_revision(compatibility_class)
        if model is None or model.canonical_model_id != request.canonical_model_id:
            raise refused("unknown_model")
        if model.compatibility_class != compatibility_class or model.harness_contract_revision != revision:
            raise refused("harness_incompatible")
        if model.lifecycle == "retired":
            raise refused("retired")

        # The administrator selects a model, never another tenant's destination.
        settings = get_settings()
        account, region = settings.platform_bedrock_account_id, settings.aws_region
        if not re.fullmatch(r"[0-9]{12}", account):
            raise refused("platform_account_unconfigured")
        destination = await db.scalar(
            select(BedrockDestinationRegistry)
            .where(
                BedrockDestinationRegistry.account_id == account,
                BedrockDestinationRegistry.region == region,
                BedrockDestinationRegistry.is_platform_registered.is_(True),
                BedrockDestinationRegistry.owner_org_id.is_(None),
                BedrockDestinationRegistry.credential_id.is_(None),
                BedrockDestinationRegistry.routing_capable.is_(True),
                BedrockDestinationRegistry.verified_at.is_not(None),
            )
            .with_for_update(read=True)
        )
        if destination is None:
            raise refused("platform_destination_unavailable")
        shape = compute_request_shape_sha256(model.canonical_model_id)
        evidence = await db.scalar(
            select(ModelInvocabilityEvidence)
            .where(
                ModelInvocabilityEvidence.account_id == account,
                ModelInvocabilityEvidence.region == region,
                ModelInvocabilityEvidence.canonical_model_id == model.canonical_model_id,
                ModelInvocabilityEvidence.compatibility_class == compatibility_class,
                ModelInvocabilityEvidence.harness_contract_revision == revision,
                ModelInvocabilityEvidence.request_shape_sha256 == shape,
            )
            .with_for_update(read=True)
        )
        if evidence is None or not evidence.is_proven or not evidence.provider_request_id:
            raise refused("model_unproven")
        if evidence.is_stale:
            raise refused("evidence_stale")
        before = row.active_default_model_id
        result = await db.execute(
            update(PersonaModelPolicySetting)
            .where(
                PersonaModelPolicySetting.compatibility_class == compatibility_class,
                PersonaModelPolicySetting.revision == request.expected_revision,
            )
            .values(
                active_default_model_id=model.canonical_model_id,
                harness_contract_revision=revision,
                revision=request.expected_revision + 1,
                updated_by=actor_id,
                updated_at=utcnow(),
            )
        )
        if result.rowcount != 1:
            raise refused("default_revision_conflict", 409)
        db.add(
            AuditLog(
                org_id=PLATFORM_AUDIT_ORG,
                event_type="persona_model_default_changed",
                actor_id=actor_id,
                details={
                    "compatibility_class": compatibility_class,
                    "before_model": before,
                    "after_model": model.canonical_model_id,
                    "before_revision": request.expected_revision,
                    "after_revision": request.expected_revision + 1,
                    "account_id": account,
                    "region": region,
                    "harness_contract_revision": revision,
                    "request_shape_sha256": shape,
                    "provider_request_id": evidence.provider_request_id,
                    "evidence_verified_at": evidence.verified_at_utc.isoformat(),
                    "change_reason": request.reason,
                },
            )
        )
        await db.flush()
        await db.refresh(row)
        response = ClassDefaultResponse.model_validate(row)
        await db.commit()
        return response
    except (PostureMutationError, HTTPException) as exc:
        await db.rollback()
        reason = exc.reason if isinstance(exc, PostureMutationError) else exc.detail["reason"]
        db.add(
            AuditLog(
                org_id=PLATFORM_AUDIT_ORG,
                event_type="persona_model_default_rejected",
                actor_id=actor_id,
                details={"compatibility_class": compatibility_class, "requested_model": request.canonical_model_id, "reason": reason},
            )
        )
        await db.commit()
        if isinstance(exc, PostureMutationError):
            raise refused(reason) from exc
        raise
