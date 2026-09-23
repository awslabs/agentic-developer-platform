"""Human pause/resume of a flow without changing its stories or allowances."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.human_control import authorize_human_session
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

from .models import OrchestrationDecision, OrchestrationFlow

router = APIRouter()


class FlowExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: StrictBool


class FlowExecutionResponse(BaseModel):
    flow_id: str
    execution_paused: bool


@router.post("/flows/{flow_id}/execution", response_model=FlowExecutionResponse)
async def set_flow_execution(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: FlowExecutionRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    access = AccessControl(db)
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    try:
        human = await authorize_human_session(current_user, db)
    except BootstrapRefusedError:
        raise HTTPException(403, "An authenticated human must pause or resume a flow.") from None
    flow = await db.scalar(
        select(OrchestrationFlow)
        .where(OrchestrationFlow.id == flow_id, OrchestrationFlow.org_id == human.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if flow is None:
        raise HTTPException(404, "Flow not found")
    if flow.execution_paused != body.paused:
        from .routes import _resolve_actor_role

        flow.execution_paused = body.paused
        db.add(
            OrchestrationDecision(
                org_id=human.tenant_id,
                flow_id=flow.id,
                kind="flow_paused" if body.paused else "flow_resumed",
                actor_id=human.user_id,
                actor_kind="human",
                actor_role=await _resolve_actor_role(access, current_user),
                reason="Flow execution paused" if body.paused else "Flow execution resumed; existing attempts and progress retained",
            )
        )
    await db.commit()
    return FlowExecutionResponse(flow_id=flow.id, execution_paused=body.paused)
