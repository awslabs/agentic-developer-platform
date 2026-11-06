"""Guarded campaign membership leases; never mutate the Cognito identity."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import String, cast, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.audit import write_admin_audit
from src.admin.audit_operation import AuditedAdminRoute, mark_admin_effects
from src.admin.config import AdminRole, Permission
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Team, User
from src.shared.schemas.auth import TokenContext

router = APIRouter(route_class=AuditedAdminRoute)
RESOURCE = "/organizations/{org_id}/campaign-identities/{subject}"


class IdentityEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: str = Field(pattern="^[a-f0-9]{64}$")
    enabled: bool = Field(strict=True)
    restore_operation_id: UUID | None = None


def fields(row):
    result = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        if isinstance(value, datetime):
            value = value.replace(tzinfo=UTC).isoformat() if value.tzinfo is None else value.isoformat()
        result[column.name] = value
    return result


async def snapshot(db, org_id, subject, actor):
    access = AccessControl(db)
    await access.check_permission(actor, Permission.USER_MANAGE, target_org_id=org_id)
    user = await db.scalar(
        select(User).where(User.cognito_sub == subject, User.org_id == org_id).with_for_update().execution_options(populate_existing=True)
    )
    if user is None:
        raise HTTPException(404, "Campaign identity not in selected organization")
    member = await db.scalar(
        select(TenantMembership)
        .where(TenantMembership.user_id == user.id, TenantMembership.tenant_id == org_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        member is None
        or member.role not in {"member", "user"}
        or user.role not in {None, "member", "user"}
        or user.is_shadow
        or user.user_kind != "human"
    ):
        raise HTTPException(409, "Campaign identity must be a dedicated nonprivileged human membership")
    team = await db.scalar(select(Team).where(Team.id == user.team_id, Team.org_id == org_id))
    role, _, department = await access.get_user_role(actor)
    if role == AdminRole.DEPT_ADMIN and (team is None or team.department_id != department):
        raise HTTPException(403, "Campaign identity outside your department")
    await access.check_permission(actor, Permission.USER_MANAGE, target_org_id=org_id, target_dept_id=team.department_id if team else None)
    value = {"user": fields(user), "member": fields(member)}
    if db.get_bind().dialect.name == "postgresql":
        for model in (User, TenantMembership):
            row = user if model is User else member
            value[model.__tablename__ + "_version"] = await db.scalar(
                select(cast(literal_column(model.__tablename__ + ".xmin"), String)).select_from(model).where(model.id == row.id)
            )
    version = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return member, {"subject": subject, "tenant": org_id, "version": version, "value": {"enabled": member.revoked_at is None}, "state": value}


async def operation(db, operation_id, org_id, subject):
    record = await db.get(AuditLog, str(operation_id))
    if record is None:
        return None
    if record.event_type != "campaign_identity_receipt" or record.org_id != org_id or record.details.get("subject") != subject:
        raise HTTPException(409, "Campaign operation belongs to another resource")
    return record.details


@router.get("/campaign-identity")
async def credential_probe(context: Annotated[TokenContext, Depends(get_current_user)]):
    if context.account_type != "human" or context.is_admin or context.auth_source != "jwt":
        raise HTTPException(403, {"error": "campaign_credential_unsupported"})
    return {"subject": context.user_id, "tenant": context.org_id, "expires_at": context.expires_at.isoformat()}


@router.get(RESOURCE)
async def read_identity(
    org_id: str, subject: str, db: Annotated[AsyncSession, Depends(get_db)], actor: Annotated[TokenContext, Depends(get_current_user)]
):
    _, result = await snapshot(db, org_id, subject, actor)
    return result


@router.get(RESOURCE + "/operations/{operation_id}")
async def read_operation(
    org_id: str,
    subject: str,
    operation_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[TokenContext, Depends(get_current_user)],
):
    await snapshot(db, org_id, subject, actor)
    result = await operation(db, operation_id, org_id, subject)
    if result is None:
        raise HTTPException(404, "Campaign operation not committed")
    return result


@router.put(RESOURCE + "/operations/{operation_id}")
async def edit_identity(
    org_id: str,
    subject: str,
    operation_id: UUID,
    body: IdentityEdit,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[TokenContext, Depends(get_current_user)],
):
    member, before = await snapshot(db, org_id, subject, actor)
    request = body.model_dump(mode="json")
    result = await operation(db, operation_id, org_id, subject)
    if result is not None:
        if result["request"] != request:
            raise HTTPException(409, "Campaign operation reused with different input")
    else:
        if before["version"] != body.expected_version:
            raise HTTPException(409, "Campaign membership changed")
        if body.enabled:
            if before["value"]["enabled"] or body.restore_operation_id is not None:
                raise HTTPException(409, "Campaign setup requires a pre-provisioned revoked membership")
            revoked_at = None
        else:
            prior = await operation(db, body.restore_operation_id, org_id, subject) if body.restore_operation_id else None
            if (
                prior is None
                or prior["after"]["version"] != before["version"]
                or prior["request"]["enabled"] is not True
                or prior["before"]["value"]["enabled"] is not False
            ):
                raise HTTPException(409, "Campaign restoration requires the exact committed activation")
            revoked_at = datetime.fromisoformat(prior["before"]["state"]["member"]["revoked_at"])
        mark_admin_effects()
        member.revoked_at = revoked_at
        member.updated_at = datetime.now(UTC)
        await db.flush()
        _, after = await snapshot(db, org_id, subject, actor)
        result = {"subject": subject, "operation_id": str(operation_id), "request": request, "before": before, "after": after}
        db.add(AuditLog(id=str(operation_id), org_id=org_id, actor_id=actor.user_id, event_type="campaign_identity_receipt", details=result))
    await write_admin_audit(db, actor=actor, action="campaign_identity", target_type="user", target_id=subject, org_id=org_id)
    await db.commit()
    return result
