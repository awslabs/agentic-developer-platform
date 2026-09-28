"""Platform persona defaults reuse the existing promotion proof and audit gates."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.base import utcnow
from src.shared.models.persona_models import PersonaModelPolicySetting, PersonaPlatformDefault
from src.shared.schemas.auth import TokenContext

from .catalogue import PLATFORM_MODEL_CATALOGUE, compatibility_class_harness_contract_revision
from .default_routes import _promotion_evidence, refused
from .operation_receipts import begin_operation, finish_operation
from .posture_service import PLATFORM_AUDIT_ORG, resolve_posture_actor_id
from .service import CONFIGURABLE_PERSONAS, INTERIM_PERSONA_CATALOGUE

router = APIRouter(prefix="/admin/persona-defaults", tags=["persona-model-defaults"])


class SetPersonaDefaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    canonical_model_id: Annotated[str, Field(min_length=1, max_length=255)] | None
    expected_revision: Annotated[StrictInt, Field(ge=0)]
    reason: str = Field(min_length=1, max_length=512)
    operation_id: UUID | None = None


def _persona(key):
    persona = next((p for p in INTERIM_PERSONA_CATALOGUE if p["key"] == key), None)
    if persona is None:
        raise refused("unknown_persona")
    if key not in CONFIGURABLE_PERSONAS:
        raise refused("persona_not_configurable")
    return persona


def _entry(persona, row, class_default):
    base = persona["key"].removeprefix("agent-task-").removeprefix("gpt-").removeprefix("agent-codex-")
    suggested = {"architect": "astra", "developer": "sol", "reviewer": "astra", "operations": "sol", "aidlc": "astra"}.get(base)
    return {
        "persona_key": persona["key"],
        "display_name": persona["display_name"],
        "compatibility_class": persona["compatibility_class"],
        "canonical_model_id": row.canonical_model_id if row else None,
        "revision": row.revision if row else 0,
        "inherited_model_id": class_default.active_default_model_id if class_default else None,
        "recommended_model_id": f"openai.gpt-6-{suggested}" if suggested and persona["compatibility_class"] == "codex-sdk" else None,
    }


@router.get("")
async def list_persona_defaults(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    AccessControl(db).require_platform_admin(current_user)
    rows = {row.persona_key: row for row in await db.scalars(select(PersonaPlatformDefault))}
    classes = {row.compatibility_class: row for row in await db.scalars(select(PersonaModelPolicySetting))}
    return {
        "entries": [
            _entry(p, rows.get(p["key"]), classes.get(p["compatibility_class"]))
            for p in INTERIM_PERSONA_CATALOGUE
            if p["key"] in CONFIGURABLE_PERSONAS
        ],
        "models": {
            compatibility: [
                {"id": m.canonical_model_id, "label": f"{m.model_family} {m.canonical_version}"}
                for m in PLATFORM_MODEL_CATALOGUE
                if m.compatibility_class == compatibility
                and m.lifecycle == "active"
                and m.harness_contract_revision == compatibility_class_harness_contract_revision(compatibility)
            ]
            for compatibility in {p["compatibility_class"] for p in INTERIM_PERSONA_CATALOGUE}
        },
    }


@router.put("/{persona_key}")
async def set_persona_default(
    persona_key: str,
    request: SetPersonaDefaultRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    AccessControl(db).require_platform_admin(current_user)
    persona = _persona(persona_key)
    actor = await resolve_posture_actor_id(db, current_user.user_id)
    try:
        receipt, replay = await begin_operation(
            db, actor_id=actor, operation_id=request.operation_id, resource="persona-default:" + persona_key, request=request.model_dump(mode="json")
        )
        if replay is not None:
            return replay
        row = await db.get(PersonaPlatformDefault, persona_key)
        if (row.revision if row else 0) != request.expected_revision:
            raise refused("default_revision_conflict", 409)
        evidence_details = {}
        compatibility = persona["compatibility_class"]
        revision = compatibility_class_harness_contract_revision(compatibility)
        if request.canonical_model_id is not None:
            model, revision, account, region, shape, evidence = await _promotion_evidence(db, compatibility, request.canonical_model_id)
            evidence_details = {
                "account_id": account,
                "region": region,
                "request_shape_sha256": shape,
                "provider_request_id": evidence.provider_request_id,
                "evidence_verified_at": evidence.verified_at_utc.isoformat(),
            }
        before = row.canonical_model_id if row else None
        values = dict(
            canonical_model_id=request.canonical_model_id,
            compatibility_class=compatibility,
            harness_contract_revision=revision,
            revision=request.expected_revision + 1,
            updated_by=actor,
            updated_at=utcnow(),
        )
        if row is None:
            try:
                async with db.begin_nested():
                    row = PersonaPlatformDefault(persona_key=persona_key, **values)
                    db.add(row)
                    await db.flush()
            except IntegrityError as exc:
                raise refused("default_revision_conflict", 409) from exc
        else:
            result = await db.execute(
                update(PersonaPlatformDefault)
                .where(PersonaPlatformDefault.persona_key == persona_key, PersonaPlatformDefault.revision == request.expected_revision)
                .values(**values)
            )
            if result.rowcount != 1:
                raise refused("default_revision_conflict", 409)
            await db.refresh(row)
        db.add(
            AuditLog(
                org_id=PLATFORM_AUDIT_ORG,
                event_type="persona_platform_default_changed",
                actor_id=actor,
                details={
                    "persona_key": persona_key,
                    "compatibility_class": compatibility,
                    "before_model": before,
                    "after_model": request.canonical_model_id,
                    "revision": row.revision,
                    "change_reason": request.reason,
                    **evidence_details,
                },
            )
        )
        response = _entry(persona, row, await db.get(PersonaModelPolicySetting, compatibility))
        finish_operation(receipt, response)
        await db.commit()
        return response
    except HTTPException as exc:
        await db.rollback()
        db.add(
            AuditLog(
                org_id=PLATFORM_AUDIT_ORG,
                event_type="persona_platform_default_rejected",
                actor_id=actor,
                details={"persona_key": persona_key, "reason": exc.detail["reason"]},
            )
        )
        await db.commit()
        raise
