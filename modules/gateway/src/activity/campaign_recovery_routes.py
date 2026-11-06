"""Human-owned, exact-version retirement of native campaign authority."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from anyio import from_thread
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from src.activity.exit_recovery_routes import RunID
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.campaign_execution import CampaignExecutionRecovery
from src.agentauth.exit_retention import ExitRetentionError
from src.agentauth.human_control import authorize_human_session
from src.agentauth.routes import get_agent_runtime
from src.agentauth.store import AuthorityStoreError
from src.agentauth.workload import WorkloadRefusedError, WorkloadUnavailableError
from src.auth.dependencies import get_current_user
from src.shared.database import get_db, get_session_factory
from src.shared.identity import resolve_canonical_user_id
from src.shared.schemas.auth import TokenContext

router = APIRouter(tags=["activity"])


class RetirementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: str = Field(pattern=r"^[a-f0-9]{64}$")


def get_campaign_recovery():
    return CampaignExecutionRecovery(get_agent_runtime().store)


async def owned(current_user, db, service, invocation):
    session = await authorize_human_session(current_user, db)
    owner = await run_in_threadpool(service.owner, tenant=session.tenant_id, invocation=invocation)
    canonical = await resolve_canonical_user_id(db, owner, org_id=session.tenant_id)
    if canonical != session.user_id:
        raise BootstrapRefusedError("campaign execution owner changed")
    return session, owner


@router.get("/activity/invocations/{invocation_id}/campaign-binding")
async def campaign_binding(
    invocation_id: RunID,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[CampaignExecutionRecovery, Depends(get_campaign_recovery)],
):
    try:
        session, owner = await owned(current_user, db, service, invocation_id)
        result = await run_in_threadpool(service.capture, tenant=session.tenant_id, invocation=invocation_id, human=owner)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except BootstrapRefusedError:
        raise HTTPException(404, "campaign execution unavailable") from None
    except (AuthorityStoreError, SQLAlchemyError):
        raise HTTPException(503, "campaign authority unavailable") from None


@router.post("/activity/invocations/{invocation_id}/campaign-stop/{operation_id}")
async def stop_campaign_execution(
    invocation_id: RunID,
    operation_id: UUID,
    body: RetirementRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[CampaignExecutionRecovery, Depends(get_campaign_recovery)],
):
    try:
        session, owner = await owned(current_user, db, service, invocation_id)
        arguments = {
            "tenant": session.tenant_id,
            "invocation": invocation_id,
            "human": owner,
            "expected_version": body.expected_version,
            "operation_id": str(operation_id),
        }
        captured = await run_in_threadpool(service.stopping, **arguments)
        execution = captured["binding"]["execution"]
        runtime = get_agent_runtime()
        workload = {"name": execution["pod_name"]["S"], "uid": execution["workload_binding"]["S"]}
        pod_arguments = {**workload, "invocation_id": invocation_id, "tenant_id": session.tenant_id, "operation_id": str(operation_id)}
        recovery = runtime.workloads.campaign_recovery
        exit_arguments = {**arguments, "observation_scope": runtime.workloads.observation_scope}

        async def reauthorize():
            async with get_session_factory()() as fresh_db:
                fresh_session, fresh_owner = await owned(current_user, fresh_db, service, invocation_id)
            if fresh_session != session or fresh_owner != owner or session.expires_at <= datetime.now(UTC):
                raise BootstrapRefusedError("campaign stop owner changed")
            await run_in_threadpool(service.stopping, **arguments)

        receipt = await run_in_threadpool(service.exit_receipt, **exit_arguments)
        if receipt is None and await run_in_threadpool(runtime.workloads.has_exited, **workload):
            await reauthorize()
            receipt = await run_in_threadpool(service.record_exit, **exit_arguments)
        if receipt is not None:
            await run_in_threadpool(service.retire, **arguments, before_commit=lambda: from_thread.run(reauthorize))
            await reauthorize()
            await run_in_threadpool(recovery.release, **pod_arguments)
            return JSONResponse(receipt, headers={"Cache-Control": "no-store"})
        if not await run_in_threadpool(recovery.retained, **pod_arguments):
            await run_in_threadpool(runtime.workloads.verify_bound, **workload)
        await run_in_threadpool(service.retire, **arguments, before_commit=lambda: from_thread.run(reauthorize))
        await reauthorize()
        await run_in_threadpool(recovery.retain, **pod_arguments)
        await reauthorize()
        await run_in_threadpool(recovery.request_stop, **pod_arguments)
        return JSONResponse(
            {"status": "stop_requested", "operation_id": str(operation_id)},
            status_code=202,
            headers={"Cache-Control": "no-store"},
        )
    except (BootstrapRefusedError, WorkloadRefusedError):
        raise HTTPException(409, "campaign stop refused or workload changed") from None
    except (AuthorityStoreError, SQLAlchemyError, WorkloadUnavailableError, ExitRetentionError):
        raise HTTPException(503, "campaign stop outcome unavailable; reconcile the same operation ID") from None


@router.put("/activity/invocations/{invocation_id}/campaign-enrollment/{operation_id}")
async def enroll_campaign_execution(
    invocation_id: RunID,
    operation_id: UUID,
    body: RetirementRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[CampaignExecutionRecovery, Depends(get_campaign_recovery)],
):
    try:
        session, owner = await owned(current_user, db, service, invocation_id)
        arguments = {
            "tenant": session.tenant_id,
            "invocation": invocation_id,
            "human": owner,
            "expected_version": body.expected_version,
            "operation_id": str(operation_id),
        }

        async def reauthorize():
            async with get_session_factory()() as fresh_db:
                fresh_session, fresh_owner = await owned(current_user, fresh_db, service, invocation_id)
            if fresh_session != session or fresh_owner != owner:
                raise BootstrapRefusedError("campaign enrollment owner changed")
            captured = await run_in_threadpool(service.capture, tenant=session.tenant_id, invocation=invocation_id, human=owner)
            if captured["version"] != body.expected_version:
                raise BootstrapRefusedError("campaign enrollment binding changed")

        receipt = await run_in_threadpool(service.enroll, **arguments, before_commit=lambda: from_thread.run(reauthorize))
        runtime = get_agent_runtime()
        execution = receipt["binding"]["execution"]
        workload = {"name": execution["pod_name"]["S"], "uid": execution["workload_binding"]["S"]}
        await run_in_threadpool(runtime.workloads.verify_bound, **workload)
        await reauthorize()
        await run_in_threadpool(
            runtime.workloads.campaign_recovery.retain,
            **workload,
            invocation_id=invocation_id,
            tenant_id=session.tenant_id,
            operation_id=str(operation_id),
        )
        return JSONResponse(receipt, headers={"Cache-Control": "no-store"})
    except (BootstrapRefusedError, WorkloadRefusedError):
        raise HTTPException(409, "campaign enrollment refused or binding changed") from None
    except (AuthorityStoreError, SQLAlchemyError, WorkloadUnavailableError, ExitRetentionError):
        raise HTTPException(503, "campaign enrollment outcome unavailable; reconcile the same operation ID") from None


@router.put("/activity/invocations/{invocation_id}/campaign-retirement/{operation_id}")
async def retire_campaign_execution(
    invocation_id: RunID,
    operation_id: UUID,
    body: RetirementRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[CampaignExecutionRecovery, Depends(get_campaign_recovery)],
):
    try:
        session, owner = await owned(current_user, db, service, invocation_id)

        async def reauthorize():
            async with get_session_factory()() as fresh_db:
                fresh_session, fresh_owner = await owned(current_user, fresh_db, service, invocation_id)
            if fresh_session.user_id != session.user_id or fresh_session.tenant_id != session.tenant_id or fresh_owner != owner:
                raise BootstrapRefusedError("campaign retirement owner changed")

        result = await run_in_threadpool(
            service.retire,
            tenant=session.tenant_id,
            invocation=invocation_id,
            human=owner,
            expected_version=body.expected_version,
            operation_id=str(operation_id),
            before_commit=lambda: from_thread.run(reauthorize),
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except BootstrapRefusedError:
        raise HTTPException(409, "campaign retirement refused or binding changed") from None
    except (AuthorityStoreError, SQLAlchemyError):
        raise HTTPException(503, "campaign retirement outcome unavailable; reconcile the same operation ID") from None
