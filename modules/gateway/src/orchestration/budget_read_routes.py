"""Financial visibility uses the same tenant/read permission as the flow graph."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

from .budget_read import FlowBudgetSummary, NodeBudgetSummary, read_flow_budget, read_node_budget
from .models import OrchestrationFlow, OrchestrationNode

router = APIRouter()
ID = Annotated[str, Path(min_length=1, max_length=36)]
User = Annotated[TokenContext, Depends(get_current_user)]
DB = Annotated[AsyncSession, Depends(get_db)]


async def visible_flow(db, user, flow_id):
    flow = await db.scalar(select(OrchestrationFlow).where(OrchestrationFlow.org_id == user.org_id, OrchestrationFlow.id == flow_id))
    if flow is None:
        raise HTTPException(404, "Flow not found")
    return flow


@router.get("/flows/{flow_id}/budget", response_model=FlowBudgetSummary)
async def get_flow_budget(flow_id: ID, current_user: User, db: DB):
    await AccessControl(db).check_permission(current_user, Permission.USAGE_READ, target_org_id=current_user.org_id)
    return await read_flow_budget(db, await visible_flow(db, current_user, flow_id))


@router.get("/flows/{flow_id}/nodes/{node_id}/budget", response_model=NodeBudgetSummary)
async def get_node_budget(flow_id: ID, node_id: ID, current_user: User, db: DB):
    await AccessControl(db).check_permission(current_user, Permission.USAGE_READ, target_org_id=current_user.org_id)
    flow = await visible_flow(db, current_user, flow_id)
    node = await db.scalar(
        select(OrchestrationNode).where(
            OrchestrationNode.org_id == current_user.org_id, OrchestrationNode.flow_id == flow_id, OrchestrationNode.id == node_id
        )
    )
    if node is None:
        raise HTTPException(404, "Story not found")
    return await read_node_budget(db, flow, node)
