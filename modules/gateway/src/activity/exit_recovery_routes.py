"""Explicit human recovery of an archived, terminated bootstrap workload."""

from __future__ import annotations

import os
from typing import Annotated

import boto3
from anyio import from_thread
from botocore.config import Config
from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from src.agentauth.archived_worker_exit import ArchivedWorkerExitReader, ExitRecoveryRefusedError, ExitRecoveryUnavailableError
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.bootstrap_exit_recovery import BootstrapExitRecovery, ExitRecoveryRequest
from src.agentauth.human_control import authorize_human_session
from src.agentauth.routes import get_agent_runtime
from src.agentauth.store import AuthorityStoreError
from src.auth.dependencies import get_current_user
from src.shared.database import get_db, get_session_factory
from src.shared.identity import resolve_canonical_user_id
from src.shared.schemas.auth import TokenContext

router = APIRouter(tags=["activity"])
RunID = Annotated[str, Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:_-]+$")]


def get_exit_recovery() -> BootstrapExitRecovery:
    runtime = get_agent_runtime()
    try:
        scope = runtime.workloads.observation_scope
        reader = ArchivedWorkerExitReader(
            client=boto3.client(
                "logs",
                region_name=os.environ.get("AWS_REGION", "us-east-1"),
                config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}),
            ),
            log_group=os.environ.get("AGENT_WORKER_AUDIT_LOG_GROUP", ""),
            namespace=scope["namespace"],
            service_account=scope["service_account"],
            image_digests=frozenset(filter(None, os.environ.get("AGENT_WORKER_IMAGE_DIGESTS", "").split(","))),
        )
        return BootstrapExitRecovery(store=runtime.store, events_table=os.environ.get("WEBHOOK_EVENTS_TABLE", ""), reader=reader)
    except ExitRecoveryUnavailableError:
        raise HTTPException(503, "worker exit recovery unavailable") from None


async def _owned_context(current_user, db, service, invocation):
    session = await authorize_human_session(current_user, db)
    context = await run_in_threadpool(service.context, tenant=session.tenant_id, invocation=invocation)
    owner = await resolve_canonical_user_id(db, context.owner, org_id=session.tenant_id)
    if owner != session.user_id:
        raise ExitRecoveryRefusedError("run not found")
    return session, context


async def _recover(invocation, body, current_user, db, service, *, preview):
    try:
        session, context = await _owned_context(current_user, db, service, invocation)

        async def reauthorize():
            # A query in the request session can reuse retained ORM identities
            # after another transaction revokes membership during provider I/O.
            # A separate session also starts a new database transaction.
            async with get_session_factory()() as fresh_db:
                fresh_session, fresh = await _owned_context(current_user, fresh_db, service, invocation)
            if fresh_session.user_id != session.user_id or fresh.owner != context.owner or fresh.raw != context.raw:
                raise ExitRecoveryRefusedError("protected recovery identity changed")

        result = await run_in_threadpool(
            service.recover,
            tenant=session.tenant_id,
            invocation=invocation,
            actor=session.user_id,
            context=context,
            request=body,
            preview=preview,
            before_commit=lambda: from_thread.run(reauthorize),
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except (BootstrapRefusedError, ExitRecoveryRefusedError):
        raise HTTPException(409, "worker exit recovery refused or identity changed") from None
    except (ExitRecoveryUnavailableError, AuthorityStoreError, SQLAlchemyError):
        raise HTTPException(503, "worker exit recovery unavailable; reconcile the same operation ID") from None


@router.post("/activity/invocations/{invocation_id}/reconcile-exit/preview")
async def preview_exit_recovery(
    invocation_id: RunID,
    body: ExitRecoveryRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[BootstrapExitRecovery, Depends(get_exit_recovery)],
):
    return await _recover(invocation_id, body, current_user, db, service, preview=True)


@router.post("/activity/invocations/{invocation_id}/reconcile-exit")
async def recover_bootstrap_exit(
    invocation_id: RunID,
    body: ExitRecoveryRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[BootstrapExitRecovery, Depends(get_exit_recovery)],
):
    return await _recover(invocation_id, body, current_user, db, service, preview=False)


@router.get("/activity/invocations/{invocation_id}/reconcile-exit/{operation_id}")
async def read_exit_recovery(
    invocation_id: RunID,
    operation_id: Annotated[str, Path(pattern=r"^[a-f0-9-]{36}$")],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[BootstrapExitRecovery, Depends(get_exit_recovery)],
):
    try:
        session, context = await _owned_context(current_user, db, service, invocation_id)
        receipt = await run_in_threadpool(
            service.receipt, tenant=session.tenant_id, invocation=invocation_id, attempt=int(context.raw["current_attempt"]["N"])
        )
        if (
            receipt is None
            or receipt.get("operation_id") != operation_id
            or receipt.get("actor") != session.user_id
            or context.raw.get("status") != {"S": "completed"}
            or context.raw.get("terminal_outcome") != {"S": "failed"}
            or receipt.get("workload_uid") != context.raw["workload_binding"]["S"]
            or receipt.get("credential_epoch") != int(context.raw["current_credential_epoch"]["N"])
        ):
            raise ExitRecoveryRefusedError("receipt unavailable")
        return JSONResponse(receipt, headers={"Cache-Control": "no-store"})
    except (BootstrapRefusedError, ExitRecoveryRefusedError):
        raise HTTPException(404, "recovery receipt unavailable") from None
    except (ExitRecoveryUnavailableError, AuthorityStoreError):
        raise HTTPException(503, "recovery receipt unavailable") from None
