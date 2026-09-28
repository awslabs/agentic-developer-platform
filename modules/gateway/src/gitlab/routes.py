"""Cognito-human GitLab adapters; no workload credentials or arbitrary host input."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.audit import write_admin_audit
from src.admin.audit_operation import AuditedAdminRoute, mark_admin_effects
from src.admin.exceptions import AccessDeniedError
from src.auth.dependencies import get_current_user
from src.auth.vault_routes import _resolve_user_id_in_context
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

from . import service

router = APIRouter(prefix="/gitlab", tags=["gitlab"], route_class=AuditedAdminRoute)


class Reviewed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    expected_provider_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_revision: str | None = None


class Configure(Reviewed):
    provider_id: str = Field(pattern=r"^[a-f0-9]{24}$")


class Project(Reviewed):
    project_id: int = Field(gt=0)
    repo: str = Field(min_length=3, max_length=255, pattern=r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+$")


class Connect(Project):
    credential_id: UUID


async def human(context: TokenContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if context.account_type != "human" or context.auth_source != "jwt":
        raise HTTPException(403, "Human ADP login required")
    caller = context.model_copy()
    await _resolve_user_id_in_context(caller, db)
    if not caller.user_id or not caller.org_id:
        raise HTTPException(403, "Tenant identity required")
    return caller


async def admin(context: TokenContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    try:
        AccessControl(db).require_platform_admin(context)
    except AccessDeniedError:
        raise HTTPException(403, "Platform administrator privileges required") from None
    return await human(context, db)


@router.get("/status")
async def status(repo: str | None = None, credential_id: UUID | None = None, caller=Depends(human), db: AsyncSession = Depends(get_db)):
    return await service.describe(db, caller, repo=repo, credential_id=str(credential_id) if credential_id else None)


@router.get("/admin/status")
async def admin_status(caller=Depends(admin), db: AsyncSession = Depends(get_db)):
    return await service.describe(db, caller)


@router.post("/admin/configure")
async def configure(request: Configure, caller=Depends(admin), actor: TokenContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    mark_admin_effects()
    result = await service.mutate(db, caller, "configure", request)
    await write_admin_audit(
        db,
        actor=actor,
        action="gitlab_configure",
        target_type="gitlab_project",
        target_id=str(getattr(request, "project_id", request.operation_id)),
        org_id=caller.org_id,
        best_effort=True,
    )
    return result


@router.get("/admin/revalidate")
async def revalidate(repo: str, credential_id: UUID, caller=Depends(admin), db: AsyncSession = Depends(get_db)):
    # Provider reads only; never provisions a webhook or asserts delivery.
    return await service.describe(db, caller, repo=repo, credential_id=str(credential_id))


@router.post("/connect")
async def connect(request: Connect, caller=Depends(human), actor: TokenContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    mark_admin_effects()
    result = await service.mutate(db, caller, "connect", request)
    await write_admin_audit(
        db,
        actor=actor,
        action="gitlab_connect",
        target_type="gitlab_project",
        target_id=str(getattr(request, "project_id", request.operation_id)),
        org_id=caller.org_id,
        best_effort=True,
    )
    return result


@router.post("/disconnect")
async def disconnect(request: Project, caller=Depends(human), actor: TokenContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    mark_admin_effects()
    result = await service.mutate(db, caller, "disconnect", request)
    await write_admin_audit(
        db,
        actor=actor,
        action="gitlab_disconnect",
        target_type="gitlab_project",
        target_id=str(getattr(request, "project_id", request.operation_id)),
        org_id=caller.org_id,
        best_effort=True,
    )
    return result
