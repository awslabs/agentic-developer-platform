"""Tenant tool catalogue and explicit tenant/team entitlements."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.admin.access_control import AccessControl
from src.admin.audit import write_admin_audit
from src.admin.audit_operation import AuditedAdminRoute, mark_admin_effects
from src.admin.config import Permission
from src.agentauth.routes import get_agent_runtime
from src.agentauth.task_tool_policy import TOOL_PATTERN, valid_tools
from src.auth.dependencies import get_current_user
from src.internal.tools_access import tool_access_report
from src.internal.tools_catalogue import tool_catalogue
from src.internal.tools_discovery import refreshed_tool_catalogue
from src.internal.tools_effective_policy import (
    TENANT_CONSENT_DEFAULTS,
    effective_tools_policy,
    platform_consent_default,
    resolve_tenant_consent_default,
    tenant_consent_override,
)
from src.internal.tools_policy import ToolsPolicyStore
from src.shared.database import get_db
from src.shared.identity.workspaces import primary_team_for_workspace
from src.shared.models.organization import Organization, Team, User
from src.shared.models.tool_entitlement import ToolEntitlement
from src.shared.schemas.auth import TokenContext

router = APIRouter(route_class=AuditedAdminRoute, prefix="/admin/tools", tags=["admin-tools"])


class EntitlementWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=255)
    team_id: str | None = Field(default=None, min_length=1, max_length=255)
    tool_name: str = Field(pattern=TOOL_PATTERN)
    enabled: bool
    max_cost_usd: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=6)
    expected_revision: int = Field(ge=0)


async def authorize(db: AsyncSession, user: TokenContext, tenant_id: str) -> None:
    if user.account_type != "human" or user.auth_source != "jwt":
        raise HTTPException(403, "Tool administration requires a human administrator")
    await AccessControl(db).check_permission(user, Permission.TOOLS_MANAGE, target_org_id=tenant_id)


@router.get("/catalog")
async def get_catalog(
    request: Request,
    tenant_id: str = Query(min_length=1, max_length=255),
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await authorize(db, current_user, tenant_id)
    return await refreshed_tool_catalogue(request, tool_catalogue)


@router.get("/entitlements")
async def get_entitlements(
    tenant_id: str = Query(min_length=1, max_length=255),
    team_id: str | None = Query(default=None, min_length=1, max_length=255),
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await authorize(db, current_user, tenant_id)
    if team_id is not None:
        await require_team(db, tenant_id, team_id)
    entries = (
        await db.scalars(
            select(ToolEntitlement).where(
                ToolEntitlement.org_id == tenant_id,
                ToolEntitlement.team_id.in_(["", team_id] if team_id else [""]),
            )
        )
    ).all()
    return {
        "tenant_id": tenant_id,
        "team_id": team_id,
        "entitlements": [
            {
                "team_id": entry.team_id or None,
                "tool_name": entry.tool_name,
                "enabled": entry.enabled,
                "max_cost_usd": str(entry.max_cost_usd) if entry.max_cost_usd is not None else None,
                "revision": entry.revision,
            }
            for entry in entries
        ],
    }


async def require_team(db: AsyncSession, tenant_id: str, team_id: str) -> None:
    team = await db.scalar(select(Team.id).where(Team.org_id == tenant_id, Team.id == team_id))
    if team is None:
        raise HTTPException(404, "Team not found in tenant")


@router.put("/entitlements")
async def put_entitlement(
    body: EntitlementWrite,
    request: Request,
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await authorize(db, current_user, body.tenant_id)
    if body.team_id is not None:
        await require_team(db, body.tenant_id, body.team_id)
    if body.enabled and body.tool_name not in {tool["name"] for tool in (await refreshed_tool_catalogue(request, tool_catalogue))["tools"]}:
        raise HTTPException(422, "Tool not found in deployed gateway catalogue")
    try:
        if body.enabled and body.team_id is not None:
            tenant_grant = await db.get(ToolEntitlement, (body.tenant_id, "", body.tool_name), with_for_update=True)
            if tenant_grant is None or not tenant_grant.enabled:
                raise HTTPException(409, "Enable this tool for the tenant before granting it to a team")
        entry = await db.get(ToolEntitlement, (body.tenant_id, body.team_id or "", body.tool_name), with_for_update=True)
        if (entry.revision if entry else 0) != body.expected_revision:
            raise HTTPException(409, "Tool entitlement changed; reload and retry")
        if entry is None:
            entry = ToolEntitlement(org_id=body.tenant_id, team_id=body.team_id or "", tool_name=body.tool_name, revision=1)
            db.add(entry)
        else:
            entry.revision += 1
        entry.enabled = body.enabled
        entry.max_cost_usd = body.max_cost_usd
        await write_admin_audit(
            db,
            actor=current_user,
            action="update_tool_entitlement",
            target_type="tool_entitlement",
            target_id=body.tool_name,
            org_id=body.tenant_id,
            extra={
                "revision": entry.revision,
                "team_id": body.team_id,
                "enabled": body.enabled,
                "max_cost_usd": str(body.max_cost_usd) if body.max_cost_usd is not None else None,
            },
        )
        mark_admin_effects()
        await db.flush()
        return {
            "tenant_id": body.tenant_id,
            "team_id": body.team_id,
            "tool_name": body.tool_name,
            "enabled": entry.enabled,
            "max_cost_usd": str(entry.max_cost_usd) if entry.max_cost_usd is not None else None,
            "revision": entry.revision,
        }
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Tool entitlement changed; reload and retry") from None
    except SQLAlchemyError:
        await db.rollback()
        raise HTTPException(503, "tool entitlement unavailable") from None


class UserToolPolicyWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=255)
    allowed_tools: list[str] = Field(max_length=64)
    tool_max_cost_usd: dict[str, Decimal]
    expected_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_tools(self):
        if not valid_tools(self.allowed_tools) or set(self.tool_max_cost_usd) != set(self.allowed_tools):
            raise ValueError("each distinct allowed tool needs its own cost ceiling")
        if any(not cost.is_finite() or cost <= 0 or cost.as_tuple().exponent < -6 for cost in self.tool_max_cost_usd.values()):
            raise ValueError("cost ceilings must be positive finite USD amounts with at most six decimal places")
        return self


async def require_policy_owner(db: AsyncSession, tenant_id: str, user_id: str) -> User:
    owner = await db.scalar(select(User).where(User.id == user_id, User.org_id == tenant_id, User.user_kind == "human", User.is_shadow.is_(False)))
    if owner is None:
        raise HTTPException(404, "User not found in tenant")
    return owner


@router.get("/users/{user_id}/policy")
async def get_user_tool_policy(
    user_id: str,
    tenant_id: str = Query(min_length=1, max_length=255),
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    runtime=Depends(get_agent_runtime),
):
    await authorize(db, current_user, tenant_id)
    owner = await require_policy_owner(db, tenant_id, user_id)
    policy = await run_in_threadpool(
        ToolsPolicyStore(table_name=runtime.store.table, client=runtime.store.client).get,
        tenant_id=tenant_id,
        owner_id=user_id,
    )
    if policy is not None and policy.get("status") != "active":
        raise HTTPException(503, "tool policy unavailable")
    effective = await effective_tools_policy(
        db, policy=policy, tenant_id=tenant_id, owner_id=user_id, team_id=await _owner_team(db, owner, tenant_id)
    )
    return {
        "user_id": user_id,
        "tenant_id": tenant_id,
        # The admin-written row, unchanged (#8387 adds the effective view beside it).
        "allowed_tools": policy.get("allowed_tools", []) if policy else [],
        "consented_tools": policy.get("consented_tools", []) if policy else [],
        "tool_max_cost_usd": {name: str(cost) for name, cost in policy.get("tool_max_cost_usd", {}).items()} if policy else {},
        "revision": policy.get("revision", 0) if policy else 0,
        "consent_mode": effective.consent_mode,
        "opted_out_tools": effective.opted_out_tools,
        "effective_allowed_tools": effective.allowed_tools,
        "effective_consented_tools": effective.consented_tools,
        "tenant_consent_default": effective.tenant_consent_default or await resolve_tenant_consent_default(db, tenant_id),
    }


@router.put("/users/{user_id}/policy")
async def put_user_tool_policy(
    user_id: str,
    body: UserToolPolicyWrite,
    request: Request,
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    runtime=Depends(get_agent_runtime),
):
    await authorize(db, current_user, body.tenant_id)
    await require_policy_owner(db, body.tenant_id, user_id)
    catalog = await refreshed_tool_catalogue(request, tool_catalogue)
    if not set(body.allowed_tools).issubset({tool["name"] for tool in catalog["tools"]}):
        raise HTTPException(422, "Tool not found in deployed gateway catalogue")
    selected = [
        {"name": tool["name"].replace(".", "___", 1), "description": tool["description"], "inputSchema": tool["inputSchema"]}
        for tool in catalog["tools"]
        if tool["name"] in body.allowed_tools
    ]
    store = ToolsPolicyStore(table_name=runtime.store.table, client=runtime.store.client)
    mark_admin_effects()
    result = await run_in_threadpool(
        store.put_allowed,
        tenant_id=body.tenant_id,
        owner_id=user_id,
        allowed_tools=body.allowed_tools,
        tool_max_cost_usd=body.tool_max_cost_usd,
        catalog=selected,
        expected_revision=body.expected_revision,
    )
    await write_admin_audit(
        db,
        actor=current_user,
        action="update_user_tool_policy",
        target_type="tools_policy",
        target_id=user_id,
        org_id=body.tenant_id,
        extra={
            "revision": result["revision"],
            "allowed_tools": result["allowed_tools"],
            "consented_tools": result["consented_tools"],
            "tool_max_cost_usd": {name: str(cost) for name, cost in result["tool_max_cost_usd"].items()},
        },
    )
    return {
        "user_id": user_id,
        "tenant_id": body.tenant_id,
        **result,
        "tool_max_cost_usd": {name: str(cost) for name, cost in result["tool_max_cost_usd"].items()},
    }


@router.get("/users/{user_id}/access")
async def get_user_tool_access(
    user_id: str,
    request: Request,
    tenant_id: str = Query(min_length=1, max_length=255),
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    runtime=Depends(get_agent_runtime),
):
    await authorize(db, current_user, tenant_id)
    owner = await require_policy_owner(db, tenant_id, user_id)
    policy = await run_in_threadpool(
        ToolsPolicyStore(table_name=runtime.store.table, client=runtime.store.client).get,
        tenant_id=tenant_id,
        owner_id=user_id,
    )
    catalogue = await refreshed_tool_catalogue(request, tool_catalogue)
    effective = await effective_tools_policy(
        db,
        policy=policy,
        tenant_id=tenant_id,
        owner_id=user_id,
        team_id=await _owner_team(db, owner, tenant_id),
        tenant_default=await resolve_tenant_consent_default(db, tenant_id),
    )
    return await tool_access_report(db, tenant_id=tenant_id, owner=owner, policy=effective.as_policy(), catalogue=catalogue)


async def _owner_team(db: AsyncSession, owner: User, tenant_id: str) -> str | None:
    """The same team the access report and the owner's own consent view resolve against."""
    primary = await primary_team_for_workspace(db, owner, tenant_id)
    return primary.id if primary else owner.team_id


class ConsentDefaultWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=255)
    # None clears the tenant override so the platform posture applies again.
    consent_default: Literal["granted", "explicit"] | None


def _consent_default_view(tenant_id: str, settings) -> dict:
    override = tenant_consent_override(settings)
    platform = platform_consent_default()
    return {
        "tenant_id": tenant_id,
        "consent_default": override if override is not None else platform,
        "tenant_override": override,
        "platform_default": platform,
        "choices": list(TENANT_CONSENT_DEFAULTS),
    }


@router.get("/consent-default")
async def get_tools_consent_default(
    tenant_id: str = Query(min_length=1, max_length=255),
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The tenant's tools consent posture (#8387): its own override or the platform default."""
    await authorize(db, current_user, tenant_id)
    organization = await db.get(Organization, tenant_id)
    if organization is None:
        raise HTTPException(404, "Organization not found")
    return _consent_default_view(tenant_id, organization.settings)


@router.put("/consent-default")
async def put_tools_consent_default(
    body: ConsentDefaultWrite,
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Set or clear the tenant's consent posture; takes effect on the next tool decision."""
    await authorize(db, current_user, body.tenant_id)
    try:
        organization = await db.get(Organization, body.tenant_id, with_for_update=True)
        if organization is None:
            raise HTTPException(404, "Organization not found")
        settings = dict(organization.settings) if isinstance(organization.settings, dict) else {}
        tools = dict(settings.get("tools")) if isinstance(settings.get("tools"), dict) else {}
        previous = tenant_consent_override(settings)
        if body.consent_default is None:
            tools.pop("consent_default", None)
        else:
            tools["consent_default"] = body.consent_default
        if tools:
            settings["tools"] = tools
        else:
            settings.pop("tools", None)
        organization.settings = settings
        await write_admin_audit(
            db,
            actor=current_user,
            action="update_tools_consent_default",
            target_type="organization",
            target_id=body.tenant_id,
            org_id=body.tenant_id,
            extra={"previous": previous, "consent_default": body.consent_default, "platform_default": platform_consent_default()},
        )
        mark_admin_effects()
        await db.flush()
        return _consent_default_view(body.tenant_id, settings)
    except SQLAlchemyError:
        await db.rollback()
        raise HTTPException(503, "tenant settings unavailable") from None
