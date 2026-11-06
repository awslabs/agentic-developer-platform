"""Read-only, token-scoped Crew executor inventory."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.audit_operation import AuditedAdminRoute
from src.admin.config import Permission
from src.auth.dependencies import get_current_user
from src.crew.inventory import list_crew_executors
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

router = APIRouter(tags=["admin"], route_class=AuditedAdminRoute)


class CrewExecutorView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    user_id: str
    team_id: str | None
    target_kind: str
    state: str
    target_ref: str | None
    dev_box_id: str | None
    generation: int
    created_at: datetime
    last_started_at: datetime | None
    last_stopped_at: datetime | None
    idle_policy: str | None
    approved_by: str | None


@router.get("/crew-executors", response_model=list[CrewExecutorView])
async def get_crew_executors(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> list[CrewExecutorView]:
    await AccessControl(db).check_permission(current_user, Permission.ORG_READ, target_org_id=current_user.org_id)
    rows = await list_crew_executors(db, tenant_id=current_user.org_id, limit=limit, offset=offset)
    return [CrewExecutorView.model_validate(row) for row in rows]
