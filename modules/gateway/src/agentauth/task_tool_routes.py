"""Run-bound authorization for separately deployed generic tool services.

The caller's registered IAM transport is independent of the forwarded worker
identity. Authorization never turns a tool service into the Task's owner.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.agentauth.routes import require_agent_transport
from src.agentauth.task_agent_runtime import get_task_agent_runtime
from src.agentauth.task_runtime_routes import (
    TaskAttemptBody,
    authenticate_task_attempt,
    authenticate_task_settlement,
    require_body_attempt,
    task_runtime,
)
from src.agentauth.task_service_policy import TaskServicePolicyError, TaskServicePolicyStore
from src.agentauth.task_tool_policy import TOOL_PATTERN, TaskToolPolicyError, persona_tools, valid_tools
from src.shared.database import get_db
from src.tasks.human_authority import require_current_owner
from src.tasks.store import TaskStoreError, WorkBindingError, _protected_grant_digest

router = APIRouter(prefix="/internal/v1/agent/task", tags=["task-api"], dependencies=[Depends(require_agent_transport)])


class ToolAuthorizationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = Field(pattern=r"^1\.0$")
    attempt: TaskAttemptBody
    tool: str = Field(pattern=TOOL_PATTERN)
    cleanup: bool = Field(default=False, strict=True)


def authorize_tool(repo, policies, identity, tool, *, cleanup=False, env=None):
    task = repo.read_task(identity.task_id)
    if (
        not task
        or task.get("scope") != {"tenant": identity.tenant, "canonical_principal": identity.canonical_principal}
        or (task.get("invocation_id"), int(task.get("generation", 0)), task.get("runtime_attempt_id"))
        != (identity.invocation_id, identity.generation, identity.runtime_attempt_id)
    ):
        raise HTTPException(403, "Task tool ownership refused")
    grant = repo._get_authority("TENANT#" + identity.tenant, f"TASK_RUN#{identity.invocation_id}#GEN#{identity.generation:010d}")
    frozen = task.get("tool_grants", [])
    if not grant or not valid_tools(frozen) or frozen != grant.get("tool_grants", []) or _protected_grant_digest(grant) != task.get("grant_digest"):
        raise HTTPException(403, "Task tool grant refused")
    if cleanup:
        # Stop-only exception: even a model-only Task must be able to prove
        # there is no outstanding domain work before terminal settlement.
        # The service must bind every cancelled resource to this verified Task.
        _, operation = tool.split(".")
        if operation != "cancel_jobs":
            raise HTTPException(403, "Only Task cleanup is permitted")
    else:
        if (
            task["state"] not in {"accepted", "queued", "running", "waiting_for_input"}
            or time.time() >= datetime.fromisoformat(task["deadline_at"].replace("Z", "+00:00")).timestamp()
        ):
            raise HTTPException(403, "Task no longer authorizes tools")
        policy = policies.get(tenant_id=identity.tenant, canonical_principal_id=identity.canonical_principal)
        if (
            not policy
            or policy.get("status") != "active"
            or not valid_tools(policy.get("allowed_tools", []))
            or task["persona"] not in policy.get("allowed_personas", [])
        ):
            raise HTTPException(403, "Task tool policy refused")
        if tool not in frozen or tool not in policy.get("allowed_tools", []) or tool not in persona_tools(task["persona"], env):
            raise HTTPException(403, "Task tool permission refused")
        if "repository_binding" in grant:
            from src.agentauth.task_repository_policy import require_current_repository

            try:
                require_current_repository(grant["repository_binding"], policy)
            except (ValueError, TypeError):
                raise HTTPException(403, "Task repository authority unavailable") from None
    # Return only execution input/identity, never protected credentials/budget grants.
    snapshot = {
        key: task[key]
        for key in (
            "task_id",
            "invocation_id",
            "generation",
            "version",
            "runtime_attempt_id",
            "scope",
            "persona",
            "state",
            "input_payload",
            "deadline_at",
            "tool_grants",
        )
        if key in task
    }
    snapshot["version"] = int(task["version"])
    if not cleanup and "repository_binding" in grant:
        snapshot["repository_binding"] = grant["repository_binding"]
    return {
        "schema_version": "1.0",
        "identity": {
            key: getattr(identity, key) for key in ("task_id", "invocation_id", "generation", "runtime_attempt_id", "tenant", "canonical_principal")
        },
        "task": snapshot,
    }


@router.post("/tool-authorize")
async def tool_authorize(body: ToolAuthorizationBody, request: Request, db: AsyncSession = Depends(get_db)):
    authenticate = authenticate_task_settlement if body.cleanup else authenticate_task_attempt
    identity = await authenticate(request)
    require_body_attempt(identity, body.attempt)
    if not body.cleanup:
        await require_current_owner(db, tenant=identity.tenant, principal=identity.canonical_principal)
    runtime = get_task_agent_runtime()
    repo = task_runtime(runtime, stop_only=body.cleanup).repository
    policies = TaskServicePolicyStore(table_name=repo.authority_table_name, client=repo._client)
    try:
        result = await run_in_threadpool(authorize_tool, repo, policies, identity, body.tool, cleanup=body.cleanup)
        if await authenticate(request) != identity:
            raise HTTPException(403, "Task tool identity changed")
        return result
    except (TaskToolPolicyError, TaskServicePolicyError, WorkBindingError):
        raise HTTPException(403, "Task tool authority unavailable") from None


# These operations are trusted-host calls, never model-visible tools. The host
# retains owner_token; only the public receipt is sent to the SDK child.
class ToolClaimBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"]
    action: Literal["claim"]
    attempt: TaskAttemptBody
    turn_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    call_id: str = Field(min_length=1, max_length=200)
    tool: str = Field(pattern=TOOL_PATTERN)
    arguments: dict = Field(max_length=128)


class ToolSettleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"]
    action: Literal["settle"]
    attempt: TaskAttemptBody
    call_id: str = Field(min_length=1, max_length=200)
    owner_token: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    status: Literal["confirmed", "unknown", "rejected"]
    content: str | None = Field(default=None, max_length=32768)
    is_error: bool = Field(default=False, strict=True)


def tool_journal(identity):
    from src.agentauth.task_tool_policy import codex_tool_name
    from src.agentauth.task_tool_receipts import TaskToolReceipts

    repo = task_runtime(get_task_agent_runtime()).repository
    task = repo.read_task(identity.task_id)
    if not task or not valid_tools(task.get("tool_grants", [])):
        raise HTTPException(403, "Task tool grants unavailable")
    policies = TaskServicePolicyStore(table_name=repo.authority_table_name, client=repo._client)
    return TaskToolReceipts(
        repo,
        authorize=lambda current, tool: authorize_tool(repo, policies, current, tool),
        catalogue={tool: codex_tool_name(tool) for tool in task.get("tool_grants", [])},
        clock=repo._clock,
    )


def public_tool_receipt(row):
    return {
        key: row[key]
        for key in (
            "schema_version",
            "task_id",
            "turn_id",
            "call_id",
            "tool",
            "request_digest",
            "operation_status",
            "automatic_replay_permitted",
            "content",
            "is_error",
        )
        if key in row
    }


@router.post("/tool-operation")
async def tool_operation(body: Annotated[ToolClaimBody | ToolSettleBody, Field(discriminator="action")], request: Request):
    identity = await authenticate_task_attempt(request)
    require_body_attempt(identity, body.attempt)
    try:
        journal = await run_in_threadpool(tool_journal, identity)
        if body.action == "claim":
            row, created = await run_in_threadpool(
                journal.claim, identity=identity, turn_id=body.turn_id, call_id=body.call_id, tool=body.tool, arguments=body.arguments
            )
            result = {"schema_version": "1.0", "action": "claim", "created": created, "receipt": public_tool_receipt(row)}
            if created:
                result["owner_token"] = row["owner_token"]
        else:
            row = await run_in_threadpool(
                journal.settle,
                identity=identity,
                call_id=body.call_id,
                owner_token=body.owner_token,
                status=body.status,
                content=body.content,
                is_error=body.is_error,
            )
            result = {"schema_version": "1.0", "action": "settle", "receipt": public_tool_receipt(row)}
        if await authenticate_task_attempt(request) != identity:
            raise HTTPException(403, "Task tool identity changed")
        return result
    except (TaskToolPolicyError, TaskServicePolicyError, WorkBindingError):
        raise HTTPException(403, "Task tool authority unavailable") from None
    except TaskStoreError:
        raise HTTPException(409, "Task tool operation could not be confirmed") from None


class RepositorySourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"]
    attempt: TaskAttemptBody
    index: int = Field(default=0, ge=0, le=63, strict=True)


@router.post("/repository-source")
async def repository_source(body: RepositorySourceBody, request: Request, db=Depends(get_db)):
    """Trusted-host transfer; the SDK receives workspace tools, not this route."""
    from botocore.exceptions import BotoCoreError, ClientError

    from src.agentauth.github_operations import OperationRefusedError
    from src.agentauth.task_repository_source import authorize_source_connection, fetch_task_source
    from src.agentauth.task_source_staging import TaskSourceStaging
    from src.tasks.routes import get_store

    identity = await authenticate_task_attempt(request)
    require_body_attempt(identity, body.attempt)
    artifacts = get_store()
    repo = artifacts.repository
    policies = TaskServicePolicyStore(table_name=repo.authority_table_name, client=repo._client)
    staging = TaskSourceStaging(
        repo, s3=artifacts.s3, bucket=artifacts.bucket, authorize=lambda current, tool: authorize_tool(repo, policies, current, tool)
    )

    async def reauthorize():
        if await authenticate_task_attempt(request) != identity:
            raise HTTPException(403, "Task source identity changed")
        await run_in_threadpool(authorize_tool, repo, policies, identity, "repository.read")

    try:
        row = await run_in_threadpool(staging.read, identity)
        if row is None:
            if body.index != 0:
                raise HTTPException(409, "Task source must be initialized first")
            authorization = await run_in_threadpool(authorize_tool, repo, policies, identity, "repository.read")
            archive = await fetch_task_source(
                db=db, tenant=identity.tenant, frozen=authorization["task"]["repository_binding"], reauthorize=reauthorize
            )
            await run_in_threadpool(staging.stage, identity, archive)
        authorization = await run_in_threadpool(authorize_tool, repo, policies, identity, "repository.read")
        await authorize_source_connection(db=db, tenant=identity.tenant, binding=authorization["task"]["repository_binding"]["binding"])
        result = await run_in_threadpool(staging.chunk, identity, index=body.index)
        await reauthorize()
        return result
    except (TaskStoreError, WorkBindingError, TaskToolPolicyError, TaskServicePolicyError, OperationRefusedError, BotoCoreError, ClientError):
        raise HTTPException(409, "Task source transfer could not be confirmed") from None


class RepositoryPublicationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["1.0"]
    attempt: TaskAttemptBody
    artifact_id: str = Field(pattern=r"^art_[0-9a-f-]{36}$")
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    title: str = Field(min_length=1, max_length=255)
    body: str = Field(max_length=16384)


@router.post("/repository-publication")
async def repository_publication(body: RepositoryPublicationBody, request: Request, db=Depends(get_db)):
    """Publish only a trusted host manifest backed by configured check receipts."""
    from botocore.exceptions import BotoCoreError, ClientError

    from src.agentauth.github_operations import OperationRefusedError
    from src.agentauth.task_publication_service import TaskPublicationService
    from src.agentauth.task_repository_publication import publish_task_change
    from src.agentauth.task_source_staging import TaskSourceStaging
    from src.agentauth.task_validation_evidence import TaskValidationEvidence
    from src.tasks.read_store import TaskStoreError as ArtifactStoreError
    from src.tasks.routes import get_store

    identity = await authenticate_task_attempt(request)
    require_body_attempt(identity, body.attempt)
    artifacts = get_store()
    repo = artifacts.repository
    policies = TaskServicePolicyStore(table_name=repo.authority_table_name, client=repo._client)

    def authorize(current, tool):
        return authorize_tool(repo, policies, current, tool)

    async def publisher(**kwargs):
        check = kwargs["reauthorize"]

        async def reauthorize():
            if await authenticate_task_attempt(request) != identity:
                raise HTTPException(403, "Task publication identity changed")
            await check()

        return await publish_task_change(db=db, **{**kwargs, "reauthorize": reauthorize})

    service = TaskPublicationService(
        repo,
        artifacts=artifacts,
        staging=TaskSourceStaging(repo, s3=artifacts.s3, bucket=artifacts.bucket, authorize=authorize),
        validations=TaskValidationEvidence(repo, artifacts=artifacts, authorize=authorize),
        authorize=authorize,
        publisher=publisher,
    )
    try:
        result = await service.execute(identity, **body.model_dump(exclude={"schema_version", "attempt"}))
        if await authenticate_task_attempt(request) != identity:
            raise HTTPException(403, "Task publication identity changed")
        return result
    except (
        TaskStoreError,
        ArtifactStoreError,
        WorkBindingError,
        TaskToolPolicyError,
        TaskServicePolicyError,
        OperationRefusedError,
        BotoCoreError,
        ClientError,
    ):
        raise HTTPException(409, "Task publication could not be confirmed; do not replay") from None


class RepositoryCompletionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["1.0"]
    attempt: TaskAttemptBody


async def verify_developer_completion(request, identity, db):
    from src.agentauth.task_completion_service import TaskCompletionService
    from src.agentauth.task_publication_service import TaskPublicationService
    from src.agentauth.task_repository_publication import observe_task_change
    from src.agentauth.task_source_staging import TaskSourceStaging
    from src.agentauth.task_validation_evidence import TaskValidationEvidence
    from src.tasks.routes import get_store

    artifacts = get_store()
    repo = artifacts.repository
    policies = TaskServicePolicyStore(table_name=repo.authority_table_name, client=repo._client)

    def authorize(current, tool):
        return authorize_tool(repo, policies, current, tool)

    async def observe(**kwargs):
        check = kwargs["reauthorize"]

        async def reauthorize():
            if await authenticate_task_attempt(request) != identity:
                raise HTTPException(403, "Completion identity changed")
            await check()

        return await observe_task_change(db=db, **{**kwargs, "reauthorize": reauthorize})

    publication = TaskPublicationService(
        repo,
        artifacts=artifacts,
        staging=TaskSourceStaging(repo, s3=artifacts.s3, bucket=artifacts.bucket, authorize=authorize),
        validations=TaskValidationEvidence(repo, artifacts=artifacts, authorize=authorize),
        authorize=authorize,
        publisher=None,
    )
    result = await TaskCompletionService(publication, observe=observe).execute(identity)
    if await authenticate_task_attempt(request) != identity:
        raise HTTPException(403, "Completion identity changed")
    return result


@router.post("/repository-completion")
async def repository_completion(body: RepositoryCompletionBody, request: Request, db=Depends(get_db)):
    from src.agentauth.github_operations import OperationRefusedError

    identity = await authenticate_task_attempt(request)
    require_body_attempt(identity, body.attempt)
    try:
        return await verify_developer_completion(request, identity, db)
    except (TaskStoreError, OperationRefusedError):
        return {"schema_version": "1.0", "task_id": identity.task_id, "status": "unverified"}
