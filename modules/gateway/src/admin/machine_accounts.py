"""Guarded SQL IAM service-account adapter; uses the existing auth domain service."""

import hashlib
import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.audit import write_admin_audit
from src.admin.audit_operation import AuditedAdminRoute, mark_admin_effects
from src.admin.config import Permission
from src.admin.persona_models.identity import registration_receipt
from src.auth.dependencies import get_current_user
from src.auth.exceptions import DuplicateServiceAccountError, ServiceAccountNotFoundError, TenantResolutionError
from src.auth.schemas import ServiceAccountCreate, ServiceAccountUpdate
from src.auth.service_account_service import ServiceAccountService
from src.shared.database import get_db
from src.shared.models.organization import ServiceAccount
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/organizations/{org_id}/service-accounts", route_class=AuditedAdminRoute)
Db = Annotated[AsyncSession, Depends(get_db)]
Actor = Annotated[TokenContext, Depends(get_current_user)]


class Register(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: uuid.UUID
    account: ServiceAccountCreate


class Patch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    account: ServiceAccountUpdate


async def permission(db, actor, org_id, *, write=False):
    if actor.account_type != "human" or actor.auth_source != "jwt" or actor.org_id != org_id:
        raise HTTPException(403, detail={"error": "human_tenant_required"})
    await AccessControl(db).check_permission(actor, Permission.ORG_UPDATE if write else Permission.ORG_READ, target_org_id=org_id)


async def domain(operation):
    try:
        return await operation
    except DuplicateServiceAccountError:
        raise HTTPException(409, detail={"error": "service_account_conflict"}) from None
    except ServiceAccountNotFoundError:
        raise HTTPException(404, detail={"error": "service_account_not_found"}) from None
    except TenantResolutionError:
        raise HTTPException(503, detail={"error": "service_account_reconciliation_required"}) from None


async def validate_scope(db, org_id, department_id, team_id):
    try:
        await ServiceAccountService()._validate_department_and_team(department_id, team_id, org_id, db)
    except TenantResolutionError:
        raise HTTPException(422, detail={"error": "service_account_scope_invalid"}) from None


async def snapshot(db, org_id, account_id):
    row = await db.scalar(
        select(ServiceAccount)
        .where(ServiceAccount.org_id == org_id, ServiceAccount.id == account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise HTTPException(404, detail={"error": "service_account_not_found"})
    value = {key: getattr(row, key) for key in ("id", "org_id", "name", "department_id", "team_id", "iam_role_arn", "description")}
    value["revision"] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    value["identity_type"] = "sql-iam"
    value["credential_delivery"] = "none; caller-owned IAM role registration"
    return value


@router.get("/{account_id}/identity")
async def get_machine_account(org_id: str, account_id: str, db: Db, current_user: Actor):
    await permission(db, current_user, org_id)
    return await snapshot(db, org_id, account_id)


@router.post("/register")
async def register_machine_account(org_id: str, request: Register, db: Db, current_user: Actor):
    await permission(db, current_user, org_id, write=True)
    receipt, prior = await registration_receipt(
        db, org_id, current_user.user_id, request.operation_id, {"kind": "sql-iam", "account": request.account.model_dump(mode="json")}
    )
    if prior:
        await write_admin_audit(
            db, actor=current_user, action="reconcile_machine_account", target_type="service_account", target_id=prior["id"], org_id=org_id
        )
        return await snapshot(db, org_id, prior["id"])
    await validate_scope(db, org_id, request.account.department_id, request.account.team_id)
    account_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"adp:sql-iam:{org_id}:{current_user.user_id}:{request.operation_id}"))
    receipt.details = {**receipt.details, "result": {"id": account_id}}
    mark_admin_effects()
    await domain(ServiceAccountService().create_service_account(request.account, org_id, db, service_account_id=account_id))
    await write_admin_audit(
        db, actor=current_user, action="register_machine_account", target_type="service_account", target_id=account_id, org_id=org_id
    )
    return await snapshot(db, org_id, account_id)


@router.patch("/{account_id}/identity")
async def update_machine_account(org_id: str, account_id: str, request: Patch, db: Db, current_user: Actor):
    await permission(db, current_user, org_id, write=True)
    before = await snapshot(db, org_id, account_id)
    if before["revision"] != request.expected_revision:
        raise HTTPException(409, detail={"error": "revision_conflict"})
    await validate_scope(db, org_id, request.account.department_id or before["department_id"], request.account.team_id or before["team_id"])
    mark_admin_effects()
    await domain(ServiceAccountService().update_service_account(account_id, request.account, org_id, db))
    await write_admin_audit(
        db, actor=current_user, action="update_machine_account", target_type="service_account", target_id=account_id, org_id=org_id
    )
    return await snapshot(db, org_id, account_id)


@router.delete("/{account_id}/identity")
async def delete_machine_account(
    org_id: str, account_id: str, db: Db, current_user: Actor, expected_revision: Annotated[str, Query(pattern=r"^[a-f0-9]{64}$")]
):
    await permission(db, current_user, org_id, write=True)
    before = await snapshot(db, org_id, account_id)
    if before["revision"] != expected_revision:
        raise HTTPException(409, detail={"error": "revision_conflict"})
    mark_admin_effects()
    await domain(ServiceAccountService().delete_service_account(account_id, org_id, db))
    await write_admin_audit(
        db, actor=current_user, action="delete_machine_account", target_type="service_account", target_id=account_id, org_id=org_id
    )
    return {
        "id": account_id,
        "org_id": org_id,
        "deleted": True,
        "effect": (
            "Future SQL service-account resolution is refused. Existing tokens/runs are not forcibly terminated; "
            "usage/audit and canonical preferences remain."
        ),
    }
