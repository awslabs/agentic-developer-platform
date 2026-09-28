"""Exact access review and durable decisions over canonical onboarding services."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.audit_operation import AuditedAdminRoute
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.models.onboarding import TenantAccessRequest
from src.shared.schemas.auth import TokenContext

router = APIRouter(route_class=AuditedAdminRoute)


async def request_lock(request_id: str, db: AsyncSession = Depends(get_db)):
    # The canonical approval service commits internally. A separate transaction
    # keeps all UI and CLI decisions serialized across those commits.
    if db.get_bind().dialect.name == "postgresql":
        async with db.bind.begin() as connection:
            await connection.execute(text("SELECT pg_advisory_xact_lock(5625, hashtext(:key))"), {"key": request_id})
            yield
    else:
        yield


async def submit_lock(actor: TokenContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    async for _ in request_lock("submit:" + actor.user_id, db):
        yield


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str = Field(min_length=64, max_length=64)
    expected_role: str = Field(min_length=1, max_length=32)
    expected_scope: str = Field(pattern="^(join_existing|create_new)$")
    operation_id: UUID
    decision_note: str = Field(min_length=1, max_length=2000)


def revision(row):
    values = [
        row.id,
        row.cognito_sub,
        row.provider,
        row.provider_user_id,
        row.proposed_tenant_id,
        row.target_login,
        row.motivation,
        row.status,
        str(row.created_at),
        str(row.updated_at),
    ]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


async def reviewed(row, db, actor, access):
    from . import handler
    from .approval_decision import derive_approval_decision

    await handler._authorize_decision(access, db, actor, row)
    receipt = row.decision_receipt or {}
    proposed_role, requested_scope = receipt.get("reviewed_role", "unknown"), receipt.get("reviewed_scope", "unknown")
    if row.status == "pending":
        decision = await derive_approval_decision(db, access, actor, row, role_for_existing_org=handler._determine_role_for_matched_user)
        proposed_role, requested_scope = decision.granted_role, decision.request_class.value
    return {
        "id": row.id,
        "requester": row.cognito_sub,
        "provider": row.provider,
        "target_tenant": row.proposed_tenant_id,
        "target_login": row.target_login,
        "motivation": row.motivation,
        "status": row.status,
        "revision": revision(row),
        "requested_scope": requested_scope,
        "proposed_role": proposed_role,
        "decision_receipt": row.decision_receipt,
    }


@router.get("/admin/access-requests/review")
async def review_list(
    limit: int = Query(50, ge=1, le=200), after: str = "", actor: TokenContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    from src.admin.config import AdminRole, Permission

    from . import handler

    access = AccessControl(db)
    await access.check_permission(actor, Permission.USER_MANAGE)
    role, org = await handler._resolve_decider_scope(access, actor)
    query = select(TenantAccessRequest).where(TenantAccessRequest.id > after).order_by(TenantAccessRequest.id).limit(limit + 1)
    if role != AdminRole.PLATFORM_ADMIN:
        if not org or not await handler._is_existing_org(db, org):
            return {"items": [], "next_cursor": None}
        query = query.where(TenantAccessRequest.proposed_tenant_id == org)
    rows = list((await db.execute(query)).scalars().all())
    return {
        "items": [await reviewed(row, db, actor, access) for row in rows[:limit]],
        "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
    }


@router.get("/admin/access-requests/{request_id}/review")
async def review_one(request_id: str, actor: TokenContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    row = await db.get(TenantAccessRequest, request_id)
    if row is None:
        raise HTTPException(404, "Request not found")
    return await reviewed(row, db, actor, AccessControl(db))


async def decide(request_id, body, actor, db, action):
    from . import handler
    from .schemas import AdminDecisionPayload

    row = await db.get(TenantAccessRequest, request_id, populate_existing=True)
    if row is None:
        raise HTTPException(404, "Request not found")
    access = AccessControl(db)
    await handler._authorize_decision(access, db, actor, row)
    fingerprint = hashlib.sha256(
        json.dumps([action, body.expected_revision, body.expected_role, body.expected_scope, body.decision_note], separators=(",", ":")).encode()
    ).hexdigest()
    previous = row.decision_receipt
    if previous:
        if previous.get("operation_id") != str(body.operation_id) or previous.get("fingerprint") != fingerprint:
            raise HTTPException(409, "A different decision already owns this request")
        if not previous.get("result"):
            raise HTTPException(409, "Decision delivery needs reconciliation; do not create another operation")
        return handler._onboarding_result(previous["result"], request_id=request_id)
    if row.status != "pending" or revision(row) != body.expected_revision:
        raise HTTPException(409, "Request changed; review it again")
    row.decision_receipt = {
        "operation_id": str(body.operation_id),
        "fingerprint": fingerprint,
        "result": None,
        "reviewed_role": body.expected_role,
        "reviewed_scope": body.expected_scope,
    }
    function = handler.approve_access_request if action == "approve" else handler.deny_access_request
    result = await function(
        request_id,
        AdminDecisionPayload(decision_note=body.decision_note, expected_role=body.expected_role, expected_scope=body.expected_scope),
        actor,
        access,
        db,
    )
    data = result.model_dump() if hasattr(result, "model_dump") else dict(result)
    data.update({"request_id": request_id, "operation_id": str(body.operation_id), "tenant_id": row.proposed_tenant_id})
    row.decision_receipt = {**row.decision_receipt, "result": data}
    await db.commit()
    return data


@router.post("/admin/access-requests/{request_id}/approve/revision", dependencies=[Depends(request_lock)])
async def approve_reviewed(request_id: str, body: Decision, actor: TokenContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await decide(request_id, body, actor, db, "approve")


@router.post("/admin/access-requests/{request_id}/deny/revision", dependencies=[Depends(request_lock)])
async def deny_reviewed(request_id: str, body: Decision, actor: TokenContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await decide(request_id, body, actor, db, "deny")
