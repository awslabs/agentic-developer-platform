"""Authenticated operator-only legacy migration; no internal worker route."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

from .compile import ApprovalContext, ProposalRejectedError
from .legacy_binding_apply import LegacyMigrationError, LegacyMigrationRequest, apply_legacy_migration
from .legacy_binding_migration import inventory_legacy_bindings
from .repository_binding import RepositoryBindingError

router = APIRouter()


class MigrationResponse(BaseModel):
    receipt_id: str
    result_plan_version: int
    already_applied: bool


@router.get("/flows/{flow_id}/legacy-migration")
async def migration_preview(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await AccessControl(db).check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    result = await inventory_legacy_bindings(db, org_id=current_user.org_id, flow_id=flow_id)
    if not result:
        raise HTTPException(status_code=404, detail="flow_not_found")
    return result[0]


@router.post("/flows/{flow_id}/legacy-migration", response_model=MigrationResponse)
async def migration_apply(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    request: LegacyMigrationRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MigrationResponse:
    access = AccessControl(db)
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    from .routes import _resolve_actor_role

    actor = ApprovalContext(
        org_id=current_user.org_id,
        actor_id=current_user.user_id,
        actor_role=await _resolve_actor_role(access, current_user),
        username=current_user.cognito_username,
        reason=request.reason,
    )
    try:
        result = await apply_legacy_migration(db, org_id=current_user.org_id, flow_id=flow_id, actor=actor, request=request)
    except LegacyMigrationError as error:
        raise HTTPException(status_code=404 if str(error) == "flow_not_found" else 409, detail=str(error)) from error
    except (RepositoryBindingError, ProposalRejectedError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    await db.commit()
    return MigrationResponse(**vars(result))
