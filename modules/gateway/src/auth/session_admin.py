"""Scoped token-family inspection and guarded legacy gateway-token revocation."""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.audit import write_admin_audit
from src.admin.audit_operation import AuditedAdminRoute, mark_admin_effects
from src.admin.config import Permission
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from src.shared.models.token import Token
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/auth", route_class=AuditedAdminRoute)


class Revoke(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=1, max_length=2000)
    expected_revision: str = Field(min_length=64, max_length=64)


async def snapshot(user_id, org, actor, db):
    access = AccessControl(db)
    await access.check_permission(actor, Permission.USER_MANAGE, target_org_id=org)
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "Target user not found in this organization")
    membership = await db.scalar(select(TenantMembership).where(TenantMembership.user_id == user.id, TenantMembership.tenant_id == org))
    if membership is None or getattr(membership, "revoked_at", None) is not None:
        raise HTTPException(404, "Target user not found in this organization")
    from src.admin.config import PLATFORM_LEVEL_ROLES

    await access.require_modifiable_target(actor, target_current_role=membership.role, target_is_platform_admin=user.role in PLATFORM_LEVEL_ROLES)
    rows = list((await db.execute(select(Token).where(Token.entity_id == user_id, Token.org_id == org).order_by(Token.id))).scalars().all())
    revision = hashlib.sha256(json.dumps([[row.id, str(row.revoked_at)] for row in rows]).encode()).hexdigest()
    return {
        "user_id": user_id,
        "org": org,
        "revision": revision,
        "active_gateway_tokens": sum(row.revoked_at is None for row in rows),
        "revoked_gateway_tokens": sum(row.revoked_at is not None for row in rows),
        "credential_families": ["gateway_jwt"],
        "cognito_sessions_revoked": False,
        "effect": (
            "Gateway-issued tokens are refused on their next database validation. "
            "Cognito login/refresh and established connections are unchanged. Hosted runs are not stopped."
        ),
    }


@router.get("/admin/revoke-user-tokens/{user_id}/review")
async def review_user_sessions(user_id: str, org: str, actor: TokenContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await snapshot(user_id, org, actor, db)


@router.post("/admin/revoke-user-tokens/{user_id}/revision")
async def revoke_reviewed_sessions(user_id: str, body: Revoke, actor: TokenContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    before = await snapshot(user_id, body.org, actor, db)
    if before["revision"] != body.expected_revision:
        raise HTTPException(409, "Token family changed; review it again")
    from src.auth.routes import auth_service

    mark_admin_effects()
    count = await auth_service.revoke_all_user_tokens(user_id, body.org, db)
    result = await snapshot(user_id, body.org, actor, db)
    result["tokens_revoked"] = count
    await write_admin_audit(
        db,
        actor=actor,
        action="revoke_user_tokens",
        target_type="user",
        target_id=user_id,
        org_id=body.org,
        extra={"reason_sha256": hashlib.sha256(body.reason.encode()).hexdigest()},
    )
    return result
