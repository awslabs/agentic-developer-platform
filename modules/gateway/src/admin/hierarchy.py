"""Conflict-safe hierarchy adapters over the existing administration services."""

import hashlib
import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import inspect, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.audit import write_admin_audit
from src.admin.audit_operation import AuditedAdminRoute, mark_admin_effects
from src.admin.config import PLATFORM_LEVEL_ROLES, AdminRole, Permission
from src.admin.exceptions import AccessDeniedError, ResourceNotFoundError
from src.admin.service import AdminService
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, TeamMembership, User
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/organizations/{org_id}/hierarchy", route_class=AuditedAdminRoute)
Kind = Literal["org", "department", "team", "member"]
MODELS = {"org": Organization, "department": Department, "team": Team, "member": User}
PUBLIC = ("id", "org_id", "name", "description", "department_id", "team_id", "email", "role", "created_at", "updated_at")


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    patch: dict[str, Any] = Field(default_factory=dict)


async def load(db, org, kind, key):
    model = MODELS[kind]
    if kind == "org" and key != org:
        raise ResourceNotFoundError("Organization", key)
    terms = [model.id == key] + ([] if kind == "org" else [model.org_id == org])
    value = await db.scalar(select(model).where(*terms).with_for_update().execution_options(populate_existing=True))
    if value is None:
        raise ResourceNotFoundError(kind, key)
    return value


async def dependencies(db, org, kind, key):
    """Report tables containing scoped dependencies without disclosing their rows.

    Include both declared foreign keys and tenant/entity-scoped configuration
    because some legacy pointers do not have database foreign keys. No cascade
    is exposed. Unknown/new dependency tables automatically participate.
    """
    if kind == "member":
        return []
    tables = sorted(Base.metadata.tables.values(), key=lambda table: table.name)
    if len(tables) > 512:
        raise HTTPException(409, {"error": "dependency_inventory_too_large"})
    connection = await db.connection()
    existing_tables = set(await connection.run_sync(lambda conn: inspect(conn).get_table_names()))
    found = []
    target = MODELS[kind].__tablename__
    for table in tables:
        # Audit history has no ownership cascade and intentionally survives the
        # resource. Counting it would make every audited create undeletable.
        if table.name not in existing_tables or table.name == "security_audit_logs":
            continue
        if table.name == target:
            if kind != "org" or "parent_tenant_id" not in table.c:
                continue
            terms = [table.c.parent_tenant_id == key]
        else:
            terms = []
            for column in table.c:
                if any(fk.target_fullname == target + ".id" for fk in column.foreign_keys):
                    terms.append(column == key)
            if kind == "org" and "org_id" in table.c:
                terms.append(table.c.org_id == org)
            if kind in {"department", "team"} and kind + "_id" in table.c:
                terms.append(table.c[kind + "_id"] == key)
            if "entity_type" in table.c and "entity_id" in table.c:
                terms.append((table.c.entity_type == kind) & (table.c.entity_id == key))
        if not terms:
            continue
        query = select(1).select_from(table).where(or_(*terms)).limit(1)
        if kind != "org" and "org_id" in table.c:
            query = query.where(table.c.org_id == org)
        if await db.scalar(query):
            found.append(table.name)
    return found


async def snapshot(db, org, kind, key):
    row = await load(db, org, kind, key)
    raw = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    public = {field: getattr(row, field) for field in PUBLIC if hasattr(row, field)}
    if kind == "member":
        membership = await db.scalar(
            select(TenantMembership).where(TenantMembership.user_id == key, TenantMembership.tenant_id == org).with_for_update()
        )
        raw["membership"] = {c.name: getattr(membership, c.name) for c in membership.__table__.columns} if membership else None
        teams = (
            await db.scalars(select(TeamMembership).where(TeamMembership.user_id == key, TeamMembership.org_id == org).order_by(TeamMembership.id))
        ).all()
        raw["teams"] = [{c.name: getattr(team, c.name) for c in team.__table__.columns} for team in teams]
        public["teams"] = [{"team_id": team.team_id, "role": team.role, "is_primary": team.is_primary} for team in teams]
        public["membership_status"] = "revoked" if membership and membership.revoked_at else "active"
    dependent = await dependencies(db, org, kind, key)
    raw["dependencies"] = dependent
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()
    return {
        "org_id": org,
        "kind": kind,
        "id": key,
        "revision": digest,
        "resource": public,
        "dependent_tables": dependent,
        "delete_permitted": not dependent,
        "cascade_supported": False,
    }


async def access_to(db, context, org, kind, key, permission):
    access = AccessControl(db)
    await access.check_permission(context, permission, target_org_id=org)
    row = await load(db, org, kind, key)
    role, _, allowed = await access.get_user_role(context)
    if role == AdminRole.DEPT_ADMIN:
        department = row.id if kind == "department" else getattr(row, "department_id", None)
        if kind == "member":
            department = await db.scalar(select(Team.department_id).where(Team.id == row.team_id, Team.org_id == org))
        if not allowed or department != allowed:
            raise AccessDeniedError("Hierarchy target is outside your department")
    return access


@router.get("/{kind}/{key}")
async def read(
    org_id: str, kind: Kind, key: str, db: Annotated[AsyncSession, Depends(get_db)], context: Annotated[TokenContext, Depends(get_current_user)]
):
    await access_to(db, context, org_id, kind, key, Permission.ORG_READ)
    return await snapshot(db, org_id, kind, key)


@router.patch("/{kind}/{key}")
async def patch(
    org_id: str,
    kind: Kind,
    key: str,
    change: Change,
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TokenContext, Depends(get_current_user)],
):
    from src.admin import routes
    from src.admin.schemas import OrganizationUpdateRequest
    from src.shared.schemas.admin import DepartmentUpdateRequest, TeamUpdateRequest, UserUpdateRequest

    permission = Permission.USER_MANAGE if kind == "member" else Permission.ORG_UPDATE
    access = await access_to(db, context, org_id, kind, key, permission)
    before = await snapshot(db, org_id, kind, key)
    if before["revision"] != change.expected_revision:
        raise HTTPException(409, {"error": "stale_revision"})
    if kind == "member" and set(change.patch) in ({"team_add"}, {"team_remove"}):
        from src.shared.schemas.admin import TeamMemberAddRequest

        if before["resource"]["membership_status"] == "revoked":
            raise HTTPException(409, {"error": "membership_revoked"})
        details = change.patch.get("team_add") or change.patch.get("team_remove")
        if not isinstance(details, dict) or not isinstance(details.get("team_id"), str):
            raise HTTPException(422, {"error": "invalid_team_change"})
        team_id = details["team_id"]
        await access_to(db, context, org_id, "team", team_id, Permission.ORG_UPDATE)
        if "team_add" in change.patch:
            if set(details) - {"team_id", "role", "is_primary"}:
                raise HTTPException(422, {"error": "invalid_team_change"})
            request = TeamMemberAddRequest(user_id=key, **{k: v for k, v in details.items() if k != "team_id"})
            await routes.add_team_member(org_id, team_id, request, db, access, context)
        else:
            if set(details) != {"team_id"}:
                raise HTTPException(422, {"error": "invalid_team_change"})
            await routes.remove_team_member(org_id, team_id, key, db, access, context)
        return await snapshot(db, org_id, kind, key)
    allowed = {"name", "role"} if kind == "member" else {"name"} if kind == "org" else {"name", "description"}
    if not change.patch or set(change.patch) - allowed or any(v is None for v in change.patch.values()):
        raise HTTPException(422, {"error": "unsupported_patch_fields"})
    if kind == "member" and before["resource"]["membership_status"] == "revoked":
        raise HTTPException(409, {"error": "membership_revoked", "message": "Use explicit member add first"})
    service = AdminService(db)
    schemas = {"org": OrganizationUpdateRequest, "department": DepartmentUpdateRequest, "team": TeamUpdateRequest, "member": UserUpdateRequest}
    try:
        request = schemas[kind].model_validate(change.patch)
    except ValidationError:
        raise HTTPException(422, {"error": "invalid_hierarchy_patch"}) from None
    if kind == "org":
        await routes.update_organization(org_id, request, service, access, context)
    elif kind == "department":
        await routes.update_department(org_id, key, request, service, access, context)
    elif kind == "team":
        await routes.update_team(org_id, key, request, service, access, context)
    else:
        await routes.update_user(org_id, key, request, service, access, context)
    return await snapshot(db, org_id, kind, key)


@router.delete("/{kind}/{key}")
async def remove(
    org_id: str,
    kind: Kind,
    key: str,
    expected_revision: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TokenContext, Depends(get_current_user)],
):
    from src.admin import routes
    from src.admin.membership_revocation import revoke_membership

    permission = Permission.ORG_DELETE if kind == "org" else Permission.ORG_UPDATE
    access = await access_to(db, context, org_id, kind, key, permission)
    before = await snapshot(db, org_id, kind, key)
    if before["revision"] != expected_revision:
        raise HTTPException(409, {"error": "stale_revision"})
    if before["dependent_tables"]:
        raise HTTPException(409, {"error": "hierarchy_has_dependencies", "dependent_tables": before["dependent_tables"], "cascade_supported": False})
    service = AdminService(db)
    if kind == "member":
        target = await service.get_user_authz_state(org_id, key)
        await access.require_modifiable_target(
            context, target.membership_role, target_is_platform_admin=(target.users_role or "").strip().lower() in PLATFORM_LEVEL_ROLES
        )
        if context.user_id in {target.cognito_sub, target.user_id}:
            raise AccessDeniedError("Cannot remove your own membership")
        mark_admin_effects()
        await revoke_membership(db, org_id=org_id, user_id=key)
        await write_admin_audit(db, actor=context, action="remove_org_membership", target_type="tenant_membership", target_id=key, org_id=org_id)
        return await snapshot(db, org_id, kind, key)
    if kind == "org":
        await routes.delete_organization(org_id, service, access, context)
    elif kind == "department":
        await routes.delete_department(org_id, key, service, access, context, None)
    else:
        await routes.delete_team(org_id, key, service, access, context)
    return {"org_id": org_id, "kind": kind, "id": key, "deleted": True}


@router.get("/{kind}")
async def listing(
    org_id: str,
    kind: Kind,
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TokenContext, Depends(get_current_user)],
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    department_id: str | None = None,
    team_id: str | None = None,
):
    from sqlalchemy import func

    access = AccessControl(db)
    await access.check_permission(context, Permission.ORG_READ, target_org_id=org_id)
    model = MODELS[kind]
    terms = [model.id == org_id] if kind == "org" else [model.org_id == org_id]
    role, _, allowed_department = await access.get_user_role(context)
    if role == AdminRole.DEPT_ADMIN:
        if not allowed_department or kind == "org" or (department_id and department_id != allowed_department):
            raise AccessDeniedError("Hierarchy list is outside your department")
        department_id = allowed_department
    if department_id:
        if kind == "department":
            terms.append(Department.id == department_id)
        elif kind == "team":
            terms.append(Team.department_id == department_id)
        elif kind == "member":
            terms.append(User.team_id.in_(select(Team.id).where(Team.org_id == org_id, Team.department_id == department_id)))
    if team_id:
        if kind != "member":
            raise HTTPException(422, "Team filter applies to members only")
        await access_to(db, context, org_id, "team", team_id, Permission.ORG_READ)
        # Authoritative many-team memberships, not the legacy primary pointer.
        terms.append(User.id.in_(select(TeamMembership.user_id).where(TeamMembership.team_id == team_id, TeamMembership.org_id == org_id)))
    total = await db.scalar(select(func.count()).select_from(model).where(*terms))
    rows = (await db.scalars(select(model).where(*terms).order_by(model.id).offset((page - 1) * page_size).limit(page_size))).all()
    items = [{field: getattr(row, field) for field in PUBLIC if hasattr(row, field)} for row in rows]
    if kind == "member":
        memberships = (
            await db.scalars(
                select(TenantMembership).where(TenantMembership.user_id.in_([row.id for row in rows]), TenantMembership.tenant_id == org_id)
            )
        ).all()
        states = {row.user_id: "revoked" if row.revoked_at else "active" for row in memberships}
        for item in items:
            item["membership_status"] = states.get(item["id"], "active")
    return {
        "org_id": org_id,
        "kind": kind,
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": page * page_size < total,
    }
