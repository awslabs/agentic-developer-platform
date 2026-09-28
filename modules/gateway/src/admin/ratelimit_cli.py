"""Scoped, revision-guarded rate-limit configuration using the shared table."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import or_, select
from sqlalchemy.exc import MultipleResultsFound
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.audit import write_admin_audit
from src.admin.audit_operation import AuditedAdminRoute, mark_admin_effects
from src.admin.config import AdminRole, Permission
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.identity.workspaces import login_subject_for_user, primary_team_for_workspace, workspace_user
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.usage import RateLimitConfig
from src.shared.schemas.auth import TokenContext

Scope = Literal["org", "department", "team", "user"]
Dimension = Annotated[int, Field(strict=True, ge=1, le=2147483647)]
DIMENSIONS = ("rpm", "tpm", "concurrent_requests")
router = APIRouter(prefix="/organizations/{org_id}/ratelimit-cli", route_class=AuditedAdminRoute)


class RateLimitPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rpm: Dimension | None = None
    tpm: Dimension | None = None
    concurrent_requests: Dimension | None = None
    expected_revision: datetime | None = None
    expect_absent: bool = False

    @model_validator(mode="after")
    def validate_patch(self):
        if not self.model_fields_set.intersection(DIMENSIONS):
            raise ValueError("Specify a dimension; null removes its override")
        if self.expect_absent == (self.expected_revision is not None):
            raise ValueError("Supply exactly one of expect_absent and expected_revision")
        if self.expected_revision is not None and self.expected_revision.tzinfo is None:
            raise ValueError("Revision must include timezone")
        return self


def serialize(row):
    if row is None:
        return None
    revision = row.updated_at
    if revision.tzinfo is None:
        revision = revision.replace(tzinfo=UTC)
    return {
        "org_id": row.org_id,
        "entity_type": "org" if row.entity_type == "organization" else row.entity_type,
        "entity_id": row.entity_id,
        **{key: getattr(row, key) for key in DIMENSIONS},
        "updated_at": revision.isoformat(),
    }


async def target(db, context, org_id, scope, supplied, permission):
    access = AccessControl(db)
    await access.check_permission(context, permission, target_org_id=org_id)
    canonical_user_id = None
    if scope == "user":
        candidates = (await db.execute(select(User).where(or_(User.id == supplied, User.cognito_sub == supplied)))).scalars().all()
        if len(candidates) > 1:
            raise HTTPException(409, "Ambiguous user ID or login subject")
        if not candidates:
            raise HTTPException(404, "Target not in selected organization")
        try:
            subject = await login_subject_for_user(db, candidates[0])
            if not subject:
                raise HTTPException(409, "User has no supported token identity for rate-limit enforcement")
            member = await workspace_user(db, subject, org_id)
            if member is None:
                raise HTTPException(404, "Target has no active membership in selected organization")
            team = await primary_team_for_workspace(db, member, org_id)
        except (ValueError, MultipleResultsFound):
            raise HTTPException(409, "Ambiguous login, membership or primary team") from None
        canonical_user_id = member.id
        supplied = subject
        department = team.department_id if team else None
    else:
        model = {"org": Organization, "department": Department, "team": Team}[scope]
        query = select(model).where(model.id == supplied)
        if scope != "org":
            query = query.where(model.org_id == org_id)
        elif supplied != org_id:
            raise HTTPException(404, "Target not in selected organization")
        row = (await db.execute(query)).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "Target not in selected organization")
        department = supplied if scope == "department" else getattr(row, "department_id", None)
    role, _, allowed_department = await access.get_user_role(context)
    if role == AdminRole.DEPT_ADMIN and (not allowed_department or department != allowed_department):
        raise HTTPException(403, "Target outside your department")
    await access.check_permission(context, permission, target_org_id=org_id, target_dept_id=department)
    return supplied, canonical_user_id


async def saved(db, org_id, scope, key, *, lock=False):
    query = select(RateLimitConfig).where(
        RateLimitConfig.org_id == org_id,
        RateLimitConfig.entity_type.in_(["org", "organization"] if scope == "org" else [scope]),
        RateLimitConfig.entity_id == key,
    )
    if lock:
        query = query.with_for_update()
    rows = (await db.execute(query)).scalars().all()
    if len(rows) > 1:
        raise HTTPException(409, "Ambiguous legacy rate-limit rows; reconcile duplicate scope overrides first")
    return rows[0] if rows else None


def revision_matches(row, revision):
    return row is not None and datetime.fromisoformat(serialize(row)["updated_at"]) == revision


def runtime_metadata(request):
    service = getattr(request.app.state, "ratelimit_service", None)
    if service is None:
        return {"state": "unavailable", "worker_convergence": "unknown", "tpm": "unavailable_actual_usage_not_reconciled"}
    config = service._config
    return {
        "state": "configured_not_probed",
        "backend": type(service._backend).__name__,
        "quota_storage": "shared_redis" if type(service._backend).__name__ == "RedisBackend" else "process_local",
        "worker_convergence": "unknown",
        "configuration_refresh": "forced_on_each_admission_timeout_3s",
        "rpm": "available_unqualified",
        "concurrent_requests": "available_unqualified",
        "tpm": "unavailable_actual_usage_not_reconciled",
        "enforce_hierarchy": config.enforce_hierarchy,
        "burst_multiplier": config.burst_multiplier,
        "refill_buffer_seconds": config.refill_buffer_seconds,
        "defaults": {
            "human": {"rpm": config.default_rpm, "tpm": config.default_tpm, "concurrent_requests": config.default_concurrent},
            "service": {
                "rpm": config.service_account_default_rpm,
                "tpm": config.service_account_default_tpm,
                "concurrent_requests": config.service_account_default_concurrent,
            },
        },
        "inheritance": "Each applicable hierarchy rung enforces its configured dimension or account-type default; strictest rung applies.",
        "legacy_zero": "A saved zero bypasses that dimension at that rung; new setters refuse zero. Other rungs still apply.",
    }


@router.get("")
async def list_configs(
    org_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TokenContext, Depends(get_current_user)],
    scope: Scope | None = None,
    page: int = Query(1, ge=1, le=10000),
    limit: int = Query(20, ge=1, le=100),
):
    access = AccessControl(db)
    await access.check_permission(context, Permission.RATELIMIT_READ, target_org_id=org_id)
    role, _, _ = await access.get_user_role(context)
    if role == AdminRole.DEPT_ADMIN:
        raise HTTPException(403, "Use exact show for department-scoped reads")
    query = select(RateLimitConfig).where(RateLimitConfig.org_id == org_id)
    if scope:
        query = query.where(RateLimitConfig.entity_type.in_(["org", "organization"] if scope == "org" else [scope]))
    rows = (
        (await db.execute(query.order_by(RateLimitConfig.entity_type, RateLimitConfig.entity_id).offset((page - 1) * limit).limit(limit + 1)))
        .scalars()
        .all()
    )
    return {"org_id": org_id, "items": [serialize(row) for row in rows[:limit]], "page": page, "page_size": limit, "has_more": len(rows) > limit}


@router.get("/{scope}/{key}")
async def show_config(
    org_id: str,
    scope: Scope,
    key: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TokenContext, Depends(get_current_user)],
):
    requested_target = key
    key, canonical_user_id = await target(db, context, org_id, scope, key, Permission.RATELIMIT_READ)
    config = serialize(await saved(db, org_id, scope, key))
    runtime = runtime_metadata(request)
    effective = {
        account: {name: config[name] if config and config[name] is not None else defaults[name] for name in DIMENSIONS}
        for account, defaults in runtime.get("defaults", {}).items()
    }
    return {
        "org_id": org_id,
        "entity_type": scope,
        "entity_id": key,
        "saved": config,
        "requested_target": requested_target,
        "canonical_user_id": canonical_user_id,
        "scope_effective_by_account_type": effective,
        "sources": {name: scope if config and config[name] is not None else "account_type_default" for name in DIMENSIONS},
        "runtime": runtime,
    }


@router.put("/{scope}/{key}")
async def set_config(
    org_id: str,
    scope: Scope,
    key: str,
    body: RateLimitPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TokenContext, Depends(get_current_user)],
):
    key, _ = await target(db, context, org_id, scope, key, Permission.RATELIMIT_UPDATE)
    # Organization lock also serializes absent-row creation without adding a parallel store.
    await db.execute(select(Organization.id).where(Organization.id == org_id).with_for_update())
    row = await saved(db, org_id, scope, key, lock=True)
    changes = body.model_dump(include=body.model_fields_set.intersection(DIMENSIONS))
    if row is not None and all(getattr(row, name) == value for name, value in changes.items()):
        result = serialize(row)
    else:
        if (body.expect_absent and row is not None) or (not body.expect_absent and not revision_matches(row, body.expected_revision)):
            raise HTTPException(409, "Rate-limit revision changed; inspect again")
        mark_admin_effects()
        if row is None:
            row = RateLimitConfig(org_id=org_id, entity_type=scope, entity_id=key)
            db.add(row)
        for name, value in changes.items():
            setattr(row, name, value)
        row.updated_at = datetime.now(UTC)
        await db.flush()
        result = serialize(row)
    await write_admin_audit(db, actor=context, action="update_ratelimit", target_type="ratelimit", target_id=f"{scope}/{key}", org_id=org_id)
    await db.commit()
    return result


@router.delete("/{scope}/{key}")
async def delete_config(
    org_id: str,
    scope: Scope,
    key: str,
    expected_revision: datetime,
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TokenContext, Depends(get_current_user)],
):
    key, _ = await target(db, context, org_id, scope, key, Permission.RATELIMIT_UPDATE)
    await db.execute(select(Organization.id).where(Organization.id == org_id).with_for_update())
    row = await saved(db, org_id, scope, key, lock=True)
    if row is not None:
        if not revision_matches(row, expected_revision):
            raise HTTPException(409, "Rate-limit revision changed; inspect again")
        mark_admin_effects()
        await db.delete(row)
    await write_admin_audit(db, actor=context, action="delete_ratelimit", target_type="ratelimit", target_id=f"{scope}/{key}", org_id=org_id)
    await db.commit()
    return {"deleted": True, "org_id": org_id, "entity_type": scope, "entity_id": key}
