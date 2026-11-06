"""Human controls for a delivery flow without changing its graph or allowances."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.human_control import authorize_human_session
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

from .environment_leases import LeaseState
from .execution_state import TERMINAL_EXECUTION_STATUSES, UNRESOLVED_ACTION_STATUSES
from .models import (
    ClaimState,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationEnvironmentLease,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from .repository import OrchestrationRepository
from .state import NodeState
from .work_claims import OwnerKind

router = APIRouter()


class FlowExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: StrictBool


class FlowExecutionResponse(BaseModel):
    flow_id: str
    execution_paused: bool


class FlowRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


class FlowRecoveryResponse(BaseModel):
    flow_id: str
    recovery_enabled: bool


class FlowArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=8, max_length=4000)


async def _has_active_flow_work(db: AsyncSession, *, org_id: str, flow_id: str) -> bool:
    executions = select(OrchestrationExecution.id).where(
        OrchestrationExecution.org_id == org_id,
        OrchestrationExecution.flow_id == flow_id,
        OrchestrationExecution.status.not_in(tuple(status.value for status in TERMINAL_EXECUTION_STATUSES)),
    )
    actions = (
        select(OrchestrationAction.id)
        .join(OrchestrationExecution, OrchestrationAction.execution_id == OrchestrationExecution.id)
        .where(
            OrchestrationAction.org_id == org_id,
            OrchestrationExecution.org_id == org_id,
            OrchestrationExecution.flow_id == flow_id,
            OrchestrationAction.status.in_(tuple(status.value for status in UNRESOLVED_ACTION_STATUSES)),
        )
    )
    leases = (
        select(OrchestrationEnvironmentLease.id)
        .join(OrchestrationAction, OrchestrationEnvironmentLease.owner_action_id == OrchestrationAction.id)
        .join(OrchestrationExecution, OrchestrationAction.execution_id == OrchestrationExecution.id)
        .where(
            OrchestrationEnvironmentLease.state == LeaseState.HELD.value,
            OrchestrationEnvironmentLease.owner_org_id == org_id,
            OrchestrationAction.org_id == org_id,
            OrchestrationExecution.org_id == org_id,
            OrchestrationExecution.flow_id == flow_id,
        )
    )
    checks = (
        select(OrchestrationNode.id).where(
            OrchestrationNode.org_id == org_id,
            OrchestrationNode.flow_id == flow_id,
            OrchestrationNode.state.in_((NodeState.RUNNING.value, NodeState.AWAITING_MERGE.value, NodeState.AWAITING_GATE.value)),
        ),
        executions,
        actions,
        select(OrchestrationWorkClaim.id).where(
            OrchestrationWorkClaim.org_id == org_id,
            OrchestrationWorkClaim.owner_kind == OwnerKind.ENGINE_FLOW.value,
            OrchestrationWorkClaim.owner_ref == flow_id,
            OrchestrationWorkClaim.state == ClaimState.HELD.value,
        ),
        leases,
    )
    for query in checks:
        if await db.scalar(query.limit(1)) is not None:
            return True
    return False


async def _archive_summary(db: AsyncSession, *, org_id: str, flow_id: str):
    from .routes import _flow_summaries

    page = await OrchestrationRepository(db).list_flows_page_with_aggregates(org_id=org_id, flow_id=flow_id, limit=1)
    return (await _flow_summaries(db, org_id, page))[0]


@router.post("/flows/{flow_id}/archive")
async def archive_flow(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: FlowArchiveRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    access = AccessControl(db)
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    try:
        human = await authorize_human_session(current_user, db)
    except BootstrapRefusedError:
        raise HTTPException(403, "An authenticated human must archive a flow.") from None
    flow = await db.scalar(
        select(OrchestrationFlow)
        .where(OrchestrationFlow.id == flow_id, OrchestrationFlow.org_id == human.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if flow is None:
        raise HTTPException(404, "Flow not found")
    if flow.archived_at is None:
        if await _has_active_flow_work(db, org_id=human.tenant_id, flow_id=flow.id):
            raise HTTPException(409, {"code": "flow_has_active_work", "message": "Resolve active work before archiving; the slug remains reserved."})

        from .routes import _resolve_actor_role

        flow.archived_at = datetime.now(UTC)
        db.add(
            OrchestrationDecision(
                org_id=human.tenant_id,
                flow_id=flow.id,
                kind="flow_archived",
                actor_id=human.user_id,
                actor_kind="human",
                actor_role=await _resolve_actor_role(access, current_user),
                reason=body.reason,
            )
        )
    await db.commit()
    return await _archive_summary(db, org_id=human.tenant_id, flow_id=flow.id)


@router.post("/flows/{flow_id}/unarchive")
async def unarchive_flow(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: FlowArchiveRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    access = AccessControl(db)
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    try:
        human = await authorize_human_session(current_user, db)
    except BootstrapRefusedError:
        raise HTTPException(403, "An authenticated human must unarchive a flow.") from None
    flow = await db.scalar(
        select(OrchestrationFlow)
        .where(OrchestrationFlow.id == flow_id, OrchestrationFlow.org_id == human.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if flow is None:
        raise HTTPException(404, "Flow not found")
    if flow.archived_at is not None:
        from .routes import _resolve_actor_role

        flow.archived_at = None
        db.add(
            OrchestrationDecision(
                org_id=human.tenant_id,
                flow_id=flow.id,
                kind="flow_unarchived",
                actor_id=human.user_id,
                actor_kind="human",
                actor_role=await _resolve_actor_role(access, current_user),
                reason=body.reason,
            )
        )
    await db.commit()
    return await _archive_summary(db, org_id=human.tenant_id, flow_id=flow.id)


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


@router.post("/flows/{flow_id}/recovery", response_model=FlowRecoveryResponse)
async def set_flow_recovery(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: FlowRecoveryRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from .recovery_control import DISABLED, ENABLED, automatic_recovery_enabled
    from .routes import _resolve_actor_role

    access = AccessControl(db)
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    try:
        human = await authorize_human_session(current_user, db)
    except BootstrapRefusedError:
        raise HTTPException(403, "An authenticated human must change automatic recovery.") from None
    flow = await db.scalar(
        select(OrchestrationFlow)
        .where(OrchestrationFlow.id == flow_id, OrchestrationFlow.org_id == human.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if flow is None:
        raise HTTPException(404, "Flow not found")
    if flow.archived_at is not None:
        raise HTTPException(409, "Archived flows cannot change automatic recovery.")
    if body.enabled != await automatic_recovery_enabled(db, org_id=flow.org_id, flow_id=flow.id):
        flow.updated_at = datetime.now(UTC)
        db.add(
            OrchestrationDecision(
                org_id=human.tenant_id,
                flow_id=flow.id,
                kind=ENABLED if body.enabled else DISABLED,
                actor_id=human.user_id,
                actor_kind="human",
                actor_role=await _resolve_actor_role(access, current_user),
                reason="Automatic retry recovery enabled within existing approved limits"
                if body.enabled
                else "Automatic retry recovery disabled; observation and work already started continue",
            )
        )
    await db.commit()
    return FlowRecoveryResponse(flow_id=flow.id, recovery_enabled=body.enabled)
