"""Versioned tools decision plane for the independently deployed façade."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID, uuid4

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from src.activity.service import ActivityService
from src.agentauth.adapter import CREDENTIAL_HEADER
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.chat_data_routes import contract_errors
from src.agentauth.chat_tools import authenticate_crew
from src.agentauth.execution import ExecutionStateError
from src.agentauth.native_test_enrollment import CAPABILITY_HEADER, OPERATION_HEADER, NativeTestStore
from src.agentauth.native_test_facade_contract import ATLASSIAN_CALLS
from src.agentauth.native_test_runtime import NativeTestRuntime
from src.agentauth.routes import get_agent_runtime, require_agent_transport
from src.agentauth.run_credential import CredentialError
from src.agentauth.store import AuthorityStoreError
from src.agentauth.test_run_admission_store import AdmissionRefusedError, canonical
from src.agentauth.workload import WORKLOAD_HEADER, WorkloadRefusedError
from src.auth.agent_registry import parse_assumed_role_arn
from src.auth.caller_provenance import verified_caller_identity
from src.auth.middleware import validate_cognito_jwt
from src.auth.tenant_context import membership
from src.budget.enforcement_service import BudgetEnforcementService
from src.crew.host_tools import ASSERTION_HEADER as HOST_ASSERTION_HEADER
from src.crew.host_tools import authenticate_crew_host
from src.internal.tool_entitlements import effective_entitlement
from src.internal.tools_admissions import ToolCallAdmission
from src.internal.tools_catalogue import gateway_catalogue
from src.internal.tools_connection import connection_scopes as owned_connection_scopes
from src.internal.tools_effective_policy import (
    GRANTED,
    MODE_DEFAULT,
    effective_tools_policy,
    entitled_tool_names,
    resolve_tenant_consent_default,
    stored_consent_mode,
)
from src.internal.tools_platform import is_platform_tool
from src.internal.tools_policy import ToolsPolicyStore
from src.ratelimit.routes import get_rate_limit_service
from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.models.organization import User
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import DenyReason

router = APIRouter(dependencies=[Depends(require_agent_transport)])


class ToolsAuthorizationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"]
    action: Literal["call", "list", "recover", "close"] = "call"
    tool: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}___[a-z][a-z0-9_]{0,63}$")
    arguments: dict = Field(default_factory=dict)
    request_id: UUID | None = None
    execution_id: UUID | None = None
    ttl_seconds: int | None = Field(default=None, strict=True, ge=1, le=900)

    @model_validator(mode="after")
    def require_operation(self):
        if (
            (self.action == "call" and self.tool is None)
            or (self.action == "list" and (self.tool or self.arguments))
            or (self.action == "close" and (self.tool is None or self.arguments))
            or (
                self.action == "recover"
                and (
                    self.tool is None
                    or self.arguments
                    and not (
                        (self.tool == "code_interpreter___execute" or self.tool in ATLASSIAN_CALLS)
                        and self.request_id is not None
                        and (self.execution_id is not None or self.tool.startswith("atlassian___"))
                        and self.ttl_seconds is None
                    )
                )
            )
        ):
            raise ValueError("invalid tools operation")
        return self


class ToolOutcomeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"]
    receipt_id: UUID
    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    team_id: str = Field(min_length=1)
    run_id: str | None = None
    tool: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}___[a-z][a-z0-9_]{0,63}$")
    request_id: UUID
    execution_id: UUID
    result_status: Literal["unknown", "confirmed", "failed"]
    cleanup_status: Literal["unknown", "confirmed"] | None = None


class ToolDescription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}___[a-z][a-z0-9_]{0,63}$")
    description: str = Field(min_length=1)
    input_schema: dict = Field(alias="inputSchema")


def require_facade(request: Request) -> None:
    configured_role = os.environ.get("ADP_TOOLS_FACADE_ROLE_ARN", "")
    verified_role = parse_assumed_role_arn(verified_caller_identity(request, settings=get_settings()) or "")
    if not configured_role or verified_role != configured_role:
        raise HTTPException(403, "tools facade identity required")


def same_principal(current, refreshed) -> bool:
    """Compare the authenticated identity, not the context object.

    TokenContext carries per-request private state (budget admission timestamps
    and reservations), so two validations of the same bearer are never ``==``.
    Comparing the model would refuse every human tool call on the re-check.
    """
    fields = ("user_id", "org_id", "team_id", "department_id", "account_type", "auth_source", "cognito_username", "is_admin", "expires_at")
    return all(getattr(current, name, None) == getattr(refreshed, name, None) for name in fields)


async def authenticate_workload(request: Request, runtime):
    try:
        context = await run_in_threadpool(
            runtime.authenticate,
            request.headers.get(CREDENTIAL_HEADER, ""),
            request.headers.get(WORKLOAD_HEADER, ""),
        )
        await runtime.validate_flow(context[2], context[3])
        if context[1].tenant_id != context[3].authority.org_id or not context[3].authority.human_id:
            raise BootstrapRefusedError("human authority does not match run")
        return context
    except (BootstrapRefusedError, CredentialError, ExecutionStateError, WorkloadRefusedError, ValueError, KeyError):
        raise HTTPException(404, "unknown_run") from None
    except (AuthorityStoreError, ClientError, BotoCoreError):
        raise HTTPException(503, "run authority unavailable") from None


async def authenticate_human(request: Request, db: AsyncSession):
    tokens = request.headers.getlist("x-adp-user-authorization")
    if len(tokens) != 1 or request.headers.get(CREDENTIAL_HEADER) or request.headers.get(WORKLOAD_HEADER) or not tokens[0].startswith("Bearer "):
        raise HTTPException(403, "tool authority refused")
    try:
        context = await validate_cognito_jwt(tokens[0])
        if context.account_type != "human" or context.auth_source != "jwt" or not context.org_id:
            raise HTTPException(403, "tool authority refused")
        user, _, team = await membership(db, context, context.org_id)
        if user.user_kind != "human" or user.is_shadow or not (team.id if team else user.team_id):
            raise HTTPException(403, "tool authority refused")
        return context, user, team.id if team else user.team_id
    except HTTPException as exc:
        if exc.status_code >= 500:
            raise HTTPException(503, "tool authority unavailable") from None
        raise HTTPException(403, "tool authority refused") from None


async def require_ordinary_tool_identity(request, *, human_id, subject_id, aliases=(), permit_native=False, operation_body=None, tenant_id=None):
    """Resolve designation only to an explicitly composed native adapter.

    Read the protected registry even when native headers are absent. Recovery
    and close can perform paid work too. A policy ceiling, model capability or
    JSON allow receipt cannot authorize any designated lifecycle effect.
    """
    try:
        runtime = getattr(request.app.state, "native_test_runtime", None)
        if type(runtime) is not NativeTestRuntime:
            raise AdmissionRefusedError("native_test_registry_unavailable")
        if runtime.store is None and not runtime.configured:
            # No registry was ever bound to this deployment, so no subject was
            # ever designated and no native funding record can exist. Ordinary
            # tool identity stands; only a caller presenting native headers is
            # refused. Mirrors NativeTestRuntimeMiddleware for the model routes.
            if any(name in request.headers for name in (CAPABILITY_HEADER, OPERATION_HEADER)):
                raise AdmissionRefusedError("native_test_designation_refused")
            return None
        if type(runtime.store) is not NativeTestStore:
            raise AdmissionRefusedError("native_test_registry_unavailable")
        if not isinstance(subject_id, str) or not subject_id:
            raise AdmissionRefusedError("native_test_subject_unavailable")
        designated = []
        for subject in sorted({subject_id, human_id, *aliases}):
            if await run_in_threadpool(runtime.store.designation, subject) is not None:
                designated.append(subject)
        if designated:
            from src.agentauth.native_test_facade_admission import NativeFacadeAdmission

            if (
                not permit_native
                or designated != [subject_id]
                or type(runtime.tool_admission) is not NativeFacadeAdmission
                or runtime.tool_admission.enrollment is not runtime.enrollment
                or runtime.enrollment.store is not runtime.store
            ):
                raise AdmissionRefusedError("native_test_all_in_tool_dispatch_unavailable")
            return runtime.tool_admission
        scoped = any(name in request.headers for name in (CAPABILITY_HEADER, OPERATION_HEADER))
        if scoped:
            from src.agentauth.native_test_facade_admission import NativeFacadeAdmission
            from src.agentauth.native_test_interpreter_runtime import NativeInterpreterRuntime

            if (
                not permit_native
                or type(runtime.tool_admission) is not NativeFacadeAdmission
                or runtime.tool_admission.enrollment is not runtime.enrollment
                or runtime.enrollment.store is not runtime.store
                or type(runtime.enrollment._effect_runtime) is not NativeInterpreterRuntime
            ):
                raise AdmissionRefusedError("native_operator_dispatch_unavailable")
            runtime.require_enrollment()
            return runtime.tool_admission
        if operation_body is not None and tenant_id:
            # These permanent original claims precede provider dispatch. Scope
            # header omission cannot change the funding of an existing UUID.
            from src.agentauth.test_run_admission_store import digest

            if operation_body.request_id is not None:
                key = runtime.store.key(tenant_id, "REQUEST", digest({"user_id": human_id, "request_id": str(operation_body.request_id)}))
                if await run_in_threadpool(runtime.store.document, key, "native_test_interpreter_request_v1") is not None:
                    raise AdmissionRefusedError("native_operator_original_funding_required")
            if operation_body.execution_id is not None:
                key = runtime.store.key(tenant_id, "EXECUTION_OWNER", str(operation_body.execution_id))
                if await run_in_threadpool(runtime.store.document, key, "native_test_interpreter_execution_v1") is not None:
                    raise AdmissionRefusedError("native_operator_original_funding_required")
        if any(name in request.headers for name in (CAPABILITY_HEADER, OPERATION_HEADER)):
            raise AdmissionRefusedError("native_test_designation_refused")
    except Exception as exc:
        reason = str(exc) if isinstance(exc, AdmissionRefusedError) else "native_test_registry_unavailable"
        raise HTTPException(403, reason, headers={"Cache-Control": "no-store"}) from None


def native_headers(request):
    values = []
    for name in (CAPABILITY_HEADER, OPERATION_HEADER):
        headers = request.headers.getlist(name)
        if len(headers) != 1 or not headers[0]:
            raise HTTPException(403, "native_test_facade_identity_refused", headers={"Cache-Control": "no-store"})
        values.append(headers[0])
    return values


class NativeDispatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    call: ToolsAuthorizationBody
    proof: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$", repr=False)


class NativeReconcileBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tenant_id: str = Field(min_length=1, max_length=255)
    run_id: str = Field(min_length=36, max_length=36)
    operation_id: str = Field(min_length=36, max_length=36)
    execution_id: str = Field(min_length=36, max_length=36)
    tool: Literal[
        "websearch___search",
        "browser___start",
        "code_interpreter___start",
        "atlassian___search_jira",
        "atlassian___get_jira_issue",
        "atlassian___search_confluence",
        "atlassian___get_confluence_page",
        "atlassian___create_jira_issue",
        "atlassian___update_jira_issue",
        "atlassian___comment_jira_issue",
        "atlassian___transition_jira_issue",
    ]


@router.post("/tools/native-cleanup")
@contract_errors
async def native_reconcile_cleanup(body: NativeReconcileBody, request: Request):
    from src.agentauth.native_test_lifecycle import NativeTestLifecycle

    role = os.environ.get("ADP_TOOLS_RECONCILER_ROLE_ARN", "")
    verified = parse_assumed_role_arn(verified_caller_identity(request, settings=get_settings()) or "")
    runtime = getattr(request.app.state, "native_test_runtime", None)
    if (
        not role
        or role != verified
        or type(runtime) is not NativeTestRuntime
        or type(runtime.tool_lifecycle) is not NativeTestLifecycle
        or runtime.tool_lifecycle.enrollment is not runtime.enrollment
    ):
        raise HTTPException(403, "native_test_cleanup_identity_refused", headers={"Cache-Control": "no-store"})
    try:
        reply = await runtime.tool_lifecycle.reconcile_cleanup(**body.model_dump())
    except AdmissionRefusedError as exc:
        raise HTTPException(403, str(exc), headers={"Cache-Control": "no-store"}) from None
    return JSONResponse(reply, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@router.post("/tools/native-dispatch")
@contract_errors
async def native_dispatch(body: NativeDispatchBody, request: Request, db: AsyncSession = Depends(get_db)):
    require_facade(request)
    context, user, _ = await authenticate_human(request, db)
    adapter = await require_ordinary_tool_identity(
        request,
        human_id=user.id,
        subject_id=context._authenticated_subject_id or context.user_id,
        aliases=(context.user_id,),
        permit_native=True,
    )
    if adapter is None:
        raise HTTPException(403, "native_test_facade_identity_refused")
    credential, operation = native_headers(request)
    try:
        reply = await adapter.dispatch(
            context=context,
            credential=credential,
            operation_id=operation,
            body=canonical(body.call.model_dump(mode="json")).encode(),
            proof=body.proof,
        )
    except AdmissionRefusedError as exc:
        raise HTTPException(403, str(exc), headers={"Cache-Control": "no-store"}) from None
    return JSONResponse(reply, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


class JiraWriteRepairBody(ToolOutcomeBody):
    model_config = ConfigDict(extra="forbid")
    tool: Literal[
        "atlassian___create_jira_issue", "atlassian___update_jira_issue", "atlassian___comment_jira_issue", "atlassian___transition_jira_issue"
    ]
    operation_id: str = Field(pattern=r"^OP#[^#]+#[^#]+#atlassian___[a-z_]+#[0-9a-f-]{36}$")
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    backend_generation: int = Field(ge=1)


@router.post("/tools/outcome-repair")
async def tools_outcome_repair(body: JiraWriteRepairBody, request: Request):
    role = os.environ.get("ADP_TOOLS_RECONCILER_ROLE_ARN", "")
    verified = parse_assumed_role_arn(verified_caller_identity(request, settings=get_settings()) or "")
    if not role or verified != role:
        raise HTTPException(403, "tools reconciler identity required")
    if body.operation_id != f"OP#{body.tenant_id}#{body.user_id}#{body.tool}#{body.request_id}":
        raise HTTPException(403, "original Jira operation required")
    return await _record_tools_outcome(body)


@router.post("/tools/outcome")
async def tools_outcome(body: ToolOutcomeBody, request: Request):
    require_facade(request)
    return await _record_tools_outcome(body)


async def _record_tools_outcome(body: ToolOutcomeBody):
    table_name = os.environ.get("WEBHOOK_EVENTS_TABLE")
    if not table_name:
        raise HTTPException(503, "tool activity unavailable")
    try:
        await run_in_threadpool(
            ActivityService(table_name=table_name).record_tool_outcome,
            receipt_id=str(body.receipt_id),
            tenant_id=body.tenant_id,
            user_id=body.user_id,
            team_id=body.team_id,
            run_id=body.run_id,
            tool=body.tool,
            request_id=str(body.request_id),
            execution_id=str(body.execution_id),
            result_status=body.result_status,
            cleanup_status=body.cleanup_status,
        )
    except ValueError:
        raise HTTPException(404, "tool activity not found") from None
    except (ClientError, BotoCoreError):
        raise HTTPException(503, "tool activity unavailable") from None
    return JSONResponse({"status": "recorded"}, headers={"Cache-Control": "no-store"})


@router.post("/tools/authorize")
@contract_errors
async def tools_authorize(
    body: ToolsAuthorizationBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    runtime=Depends(get_agent_runtime),
):
    require_facade(request)
    bearer = bool(request.headers.getlist("x-adp-user-authorization"))
    crew = bool(request.headers.getlist("x-adp-chat-capability"))
    host = bool(request.headers.getlist(HOST_ASSERTION_HEADER))
    run_id = workload_id = run_expires_at = task_id = story_id = host_generation = None
    if bearer + crew + host > 1:
        raise HTTPException(403, "ambiguous tool identity")

    async def reauthenticate():
        """The non-bearer identity re-read before a decision is released."""
        if crew:
            return await authenticate_crew(request)
        if host:
            return await authenticate_crew_host(request)
        return await authenticate_workload(request, runtime)

    if bearer:
        context, user, team_id = await authenticate_human(request, db)
        tenant_id, human_id = context.org_id, user.id
    elif crew:
        context = await authenticate_crew(request)
        launch, identity = context
        tenant_id, human_id = launch.tenant_id, launch.user_id
        run_id, workload_id, run_expires_at = identity.run_id, launch.sandbox_uid, launch.expires_at
        user = await db.scalar(select(User).where(User.id == human_id, User.org_id == tenant_id))
        if user is None or user.user_kind != "human" or user.is_shadow or not user.team_id:
            raise HTTPException(403, "tool authority refused")
        team_id = user.team_id
    elif host:
        # #8380: a Dev Box host. The gateway relay already proved the instance identity and
        # model token; the assertion carries that proof to this plane, and the live target is
        # resolved again here so tools end with the lease, like model calls do.
        context = await authenticate_crew_host(request)
        tenant_id, human_id = context.tenant_id, context.user_id
        run_id, workload_id, run_expires_at = context.run_id, context.instance_id, context.expires_at
        host_generation = context.generation
        user = await db.scalar(select(User).where(User.id == human_id, User.org_id == tenant_id))
        if user is None or user.user_kind != "human" or user.is_shadow or not user.team_id:
            raise HTTPException(403, "tool authority refused")
        team_id = user.team_id
    else:
        context = await authenticate_workload(request, runtime)
        caller, grant = context[1], context[3]
        execution = context[2]
        run_id, workload_id, run_expires_at = caller.invocation_id, context[0].uid, int(grant.expires_at.timestamp())
        task_id = execution.get("task_id", {}).get("S")
        repo = execution.get("repo", {}).get("S")
        issue = execution.get("issue_number", {}).get("N")
        if repo and issue:
            story_id = f"{repo}#{issue}"
        tenant_id, human_id = caller.tenant_id, grant.authority.human_id
        user = await db.scalar(select(User).where(User.id == human_id, User.org_id == tenant_id))
        if user is None or user.user_kind != "human" or user.is_shadow or not user.team_id:
            raise HTTPException(403, "tool authority refused")
        team_id = user.team_id

    async def check_native_designation(*, permit_native=False):
        subject = (getattr(context, "_authenticated_subject_id", None) or context.user_id) if bearer else getattr(user, "cognito_sub", None)
        aliases = (context.user_id,) if bearer else ()
        return await require_ordinary_tool_identity(
            request, human_id=human_id, subject_id=subject, aliases=aliases, permit_native=permit_native, operation_body=body, tenant_id=tenant_id
        )

    native = await check_native_designation(permit_native=bearer)
    from src.agentauth.native_test_atlassian_runtime import NativeAtlassianRuntime

    native_atlassian = native is not None and type(native.enrollment._effect_runtime) is NativeAtlassianRuntime
    if native is not None:
        credential, native_operation = native_headers(request)
        if native_atlassian and body.action == "recover":
            from src.agentauth.native_test_lifecycle import NativeTestLifecycle

            lifecycle = getattr(request.app.state.native_test_runtime, "tool_lifecycle", None)
            if type(lifecycle) is not NativeTestLifecycle or lifecycle.enrollment is not native.enrollment:
                raise HTTPException(403, "native_test_lifecycle_unavailable", headers={"Cache-Control": "no-store"})
            try:
                reply = await lifecycle.authorize_atlassian_receipt(context=context, credential=credential, operation_id=native_operation, body=body)
            except AdmissionRefusedError as exc:
                raise HTTPException(403, str(exc), headers={"Cache-Control": "no-store"}) from None
            return JSONResponse(reply, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
        if body.action == "close":
            from src.agentauth.native_test_lifecycle import NativeTestLifecycle

            lifecycle = getattr(request.app.state.native_test_runtime, "tool_lifecycle", None)
            if type(lifecycle) is not NativeTestLifecycle or lifecycle.enrollment is not native.enrollment or body.execution_id is None:
                raise HTTPException(403, "native_test_lifecycle_unavailable", headers={"Cache-Control": "no-store"})
            try:
                reply = await lifecycle.authorize_cleanup(
                    context=context, credential=credential, operation_id=native_operation, execution_id=str(body.execution_id), tool=body.tool
                )
            except AdmissionRefusedError as exc:
                raise HTTPException(403, str(exc), headers={"Cache-Control": "no-store"}) from None
            return JSONResponse(reply, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
        if body.action not in {"call", "list", "recover"}:
            raise HTTPException(403, "native_test_facade_action_unavailable", headers={"Cache-Control": "no-store"})
        if native_atlassian and (body.action not in {"call", "list"} or body.action == "call" and body.tool not in ATLASSIAN_CALLS):
            raise HTTPException(403, "native_test_atlassian_scope_refused")
        if not native_atlassian:
            try:
                rate = await get_rate_limit_service(request).consume_tool_rate_limit(context, body.tool or "websearch___search")
                if not rate.allowed:
                    raise HTTPException(429, "tool_rate_limit_exceeded", headers={"Retry-After": str(max(1, rate.retry_after_seconds or 1))})
                reply = await native.prepare(
                    context=context, credential=credential, operation_id=native_operation, body=canonical(body.model_dump(mode="json")).encode()
                )
            except AdmissionRefusedError as exc:
                raise HTTPException(403, str(exc), headers={"Cache-Control": "no-store"}) from None
            return JSONResponse(reply, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
    if body.action in ("recover", "close"):
        if bearer:
            refreshed, owner, refreshed_team = await authenticate_human(request, db)
            if not same_principal(context, refreshed) or owner.id != human_id or refreshed_team != team_id:
                raise HTTPException(403, "tool authority refused")
        elif await reauthenticate() != context:
            raise HTTPException(404, "unknown_run")
        await check_native_designation()
        return {
            "schema_version": "1.0",
            "allowed": True,
            "receipt_id": str(uuid4()),
            "principal": {"user_id": human_id, "root_human_id": human_id, "tenant_id": tenant_id, "team_id": team_id},
            "run_id": run_id,
            "workload_id": workload_id,
            "run_expires_at": run_expires_at,
        }
    stored_policy = await run_in_threadpool(
        ToolsPolicyStore(table_name=runtime.store.table, client=runtime.store.client).get,
        tenant_id=tenant_id,
        owner_id=human_id,
    )
    if stored_policy is not None and stored_policy.get("status") != "active":
        raise HTTPException(403, "tool authority refused")
    # #8387: the tenant's consent posture and entitlements turn the stored row (or no row at
    # all) into the owner's effective lists. Both reads happen only for a default-mode owner;
    # an explicit row keeps the pre-#8387 opt-in semantics without touching the database.
    tenant_default = entitled = None
    if stored_consent_mode(stored_policy) == MODE_DEFAULT:
        tenant_default = await resolve_tenant_consent_default(db, tenant_id)
        if tenant_default == GRANTED:
            entitled = await entitled_tool_names(db, tenant_id=tenant_id, team_id=team_id)
    if stored_policy is None and entitled is None:
        raise HTTPException(403, "tool authority refused")
    effective = await effective_tools_policy(
        db,
        policy=stored_policy,
        tenant_id=tenant_id,
        owner_id=human_id,
        team_id=team_id,
        include_platform=False,
        tenant_default=tenant_default,
        entitled=entitled,
    )
    policy = effective.as_policy()
    allowed_tools, consented_tools = policy.get("allowed_tools"), policy.get("consented_tools")
    if (
        not isinstance(allowed_tools, list)
        or not isinstance(consented_tools, list)
        or not all(isinstance(name, str) for name in allowed_tools + consented_tools)
    ):
        raise HTTPException(503, "tool policy unavailable")
    catalogue = await run_in_threadpool(gateway_catalogue)
    deployed = {entry["name"] for entry in catalogue["tools"]}
    entitlements = {}
    current_connection_scopes = {}
    jira_connection = None
    atlassian_connection = None
    github_connection = None
    slack_connection = None
    slack_post_ready = False

    async def entitlement(permission):
        if permission not in entitlements:
            try:
                entitlements[permission] = await effective_entitlement(db, tenant_id=tenant_id, team_id=team_id, tool_name=permission)
            except SQLAlchemyError:
                raise HTTPException(503, "tool entitlement unavailable") from None
        return entitlements[permission]

    async def eligible(tool):
        permission = tool.replace("___", ".", 1)
        if (
            tool.startswith("atlassian___")
            and tool.split("___", 1)[1] in {"create_jira_issue", "update_jira_issue", "comment_jira_issue", "transition_jira_issue"}
            and permission not in (stored_policy or {}).get("consented_tools", [])
        ):
            return None, "tool_not_consented"
        if permission in {"credentials.assume_role", "credentials.materialize"}:
            return None, "private_delivery_unavailable"
        if is_platform_tool(permission):
            # First-party read-only tools execute inside the gateway under the owner's chat
            # identity (/v1/chat/data/tools/call); the façade and AgentCore never run them (#8113).
            return None, "tool_chat_data_plane_only"
        if permission not in deployed:
            return None, "tool_undeployed"
        granted, grant_ceiling = await entitlement(permission)
        if not granted:
            return None, "tool_not_entitled"
        if permission not in allowed_tools:
            return None, "tool_not_allowed"
        if permission not in consented_tools:
            return None, "tool_not_consented"
        try:
            ceiling = Decimal(str(policy.get("tool_max_cost_usd", {}).get(permission)))
        except (AttributeError, InvalidOperation):
            return None, "tool_cost_policy_missing"
        if not ceiling.is_finite() or ceiling <= 0:
            return None, "tool_cost_policy_missing"
        if grant_ceiling is not None:
            ceiling = min(ceiling, grant_ceiling)
        from src.internal.tools_knowledge_memory import MEMORY_ESTIMATE_USD, MEMORY_TOOLS

        if tool in MEMORY_TOOLS:
            if ceiling < MEMORY_ESTIMATE_USD:
                return None, "tool_cost_ceiling_exceeded"
            ceiling = MEMORY_ESTIMATE_USD
        if tool.startswith("atlassian___"):
            if tool not in current_connection_scopes:
                return None, "scope_not_granted"
        if tool.split("___", 1)[0] in {"github", "jira", "slack"}:
            if tool in current_connection_scopes:
                scopes = current_connection_scopes[tool]
                reason = None
            else:
                scopes, reason = await owned_connection_scopes(
                    db,
                    tenant_id=tenant_id,
                    owner_id=human_id,
                    tool=tool,
                    workload_name=get_settings().oauth_workload_name,
                )
            if reason is not None:
                return None, "scope_not_granted"
            current_connection_scopes[tool] = scopes
        return ceiling, None

    if (
        os.environ.get("ADP_TOOLS_SLACK_MEDIATOR_ENABLED") == "true"
        and bearer
        and (body.action == "list" or body.tool in {"slack___get_user", "slack___post_message"})
    ):
        from src.internal.slack_oauth import selected_slack_connection
        from src.internal.slack_poster import approved_post_fixture

        try:
            slack_credential, slack_binding, slack_connection = await selected_slack_connection(db, user_id=human_id, tenant_id=tenant_id)
            current_connection_scopes["slack___get_user"] = slack_credential.scopes["oauth_scopes"]
            if "chat:write" in slack_credential.scopes["oauth_scopes"] and "chat:write" in slack_binding.observed_scopes:
                try:
                    approved_post_fixture(owner_id=human_id, team_id=slack_binding.provider_account_id.split(":", 1)[0])
                    current_connection_scopes["slack___post_message"] = slack_credential.scopes["oauth_scopes"]
                    slack_post_ready = True
                except HTTPException as exc:
                    if exc.status_code != 403:
                        raise
        except HTTPException as exc:
            if exc.status_code != 403:
                raise
    if os.environ.get("ADP_TOOLS_ATLASSIAN_MEDIATOR_ENABLED") == "true" and (
        body.action == "list" or body.tool and body.tool.startswith("atlassian___")
    ):
        from src.internal.tool_adapter_authority import current_atlassian_read

        aliases = ("search_jira", "get_jira_issue", "search_confluence", "get_confluence_page")
        if os.environ.get("ADP_TOOLS_ATLASSIAN_SYSTEM_ENABLED") == "true":
            aliases += ("get_project_context",)
        if os.environ.get("ADP_TOOLS_ATLASSIAN_WRITES_ENABLED") == "true":
            aliases += ("create_jira_issue", "update_jira_issue", "comment_jira_issue", "transition_jira_issue")
        for operation in aliases if body.action == "list" else (body.tool.split("___", 1)[1],):
            try:
                selected, scopes = await current_atlassian_read(
                    db,
                    tenant_id=tenant_id,
                    owner_id=human_id,
                    operation=operation,
                    **({"arguments": body.arguments} if operation == "get_project_context" and body.action == "call" else {}),
                )
                current_connection_scopes["atlassian___" + operation] = scopes
                atlassian_connection = selected
            except HTTPException as exc:
                if exc.status_code != 403:
                    raise
    if (
        body.action == "call"
        and body.tool.startswith("atlassian___")
        and (atlassian_connection is None or body.tool not in current_connection_scopes)
    ):
        raise HTTPException(403, "atlassian_connection_unqualified")
    if os.environ.get("ADP_TOOLS_JIRA_MEDIATOR_ENABLED") == "true" and bearer and (body.action == "list" or body.tool == "jira___get_issue"):
        from src.internal.jira_oauth import selected_jira_connection

        try:
            jira_credential, _, jira_connection = await selected_jira_connection(db, user_id=human_id, tenant_id=tenant_id)
            current_connection_scopes["jira___get_issue"] = jira_credential.scopes["oauth_scopes"]
        except HTTPException as exc:
            if exc.status_code != 403:
                raise
    if body.action == "call" and body.tool.startswith("jira___") and (body.tool != "jira___get_issue" or jira_connection is None):
        raise HTTPException(403, "jira_connection_unqualified")
    if os.environ.get("ADP_TOOLS_GITHUB_MEDIATOR_ENABLED") == "true" and bearer and (body.action == "list" or body.tool == "github___get_user"):
        from src.internal.github_oauth import selected_github_connection

        try:
            github_credential, _, github_connection = await selected_github_connection(db, user_id=human_id, tenant_id=tenant_id)
            from src.internal.github_oauth import is_github_app_grant

            current_connection_scopes["github___get_user"] = (
                ["read:user"] if is_github_app_grant(github_credential.scopes) else github_credential.scopes["oauth_scopes"]
            )
        except HTTPException as exc:
            if exc.status_code != 403:
                raise
    if body.action == "call" and body.tool.startswith("github___") and (body.tool != "github___get_user" or github_connection is None):
        raise HTTPException(403, "GitHub connection unqualified")
    if (
        body.action == "call"
        and body.tool.startswith("slack___")
        and not (slack_connection is not None and (body.tool == "slack___get_user" or body.tool == "slack___post_message" and slack_post_ready))
    ):
        raise HTTPException(403, "Slack connection unqualified")

    preflight = None
    if body.action == "call":
        from src.internal.tools_knowledge_memory import MEMORY_TOOLS, validate_arguments

        if (
            body.tool.startswith("atlassian___")
            and body.tool.split("___", 1)[1] in {"create_jira_issue", "update_jira_issue", "comment_jira_issue", "transition_jira_issue"}
            and body.request_id is None
        ):
            raise HTTPException(422, "Jira write operation id required")
        if body.tool in MEMORY_TOOLS:
            if not (bearer or host):
                raise HTTPException(403, "knowledge memory human admission required")
            if body.request_id is None:
                raise HTTPException(422, "knowledge memory request id required")
            validate_arguments(body.tool.split("___", 1)[1], body.arguments)
        preflight = await eligible(body.tool)
        if preflight[1] is not None:
            raise HTTPException(403, preflight[1])

    receipt_id = str(uuid4())
    admission_store = None
    budget_context = TokenContext(
        user_id=human_id,
        org_id=tenant_id,
        team_id=team_id,
        department_id="",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )

    async def permitted(tool):
        ceiling, denial = preflight if preflight is not None else await eligible(tool)
        if denial is not None:
            return None
        if body.action == "call":
            try:
                rate = await get_rate_limit_service(request).consume_tool_rate_limit(budget_context, tool)
            except (TimeoutError, OSError, RuntimeError):
                raise HTTPException(503, "tool rate limit unavailable") from None
            if not rate.allowed:
                if admission_store is not None:
                    await run_in_threadpool(admission_store.cancel_before_reservation)
                raise HTTPException(
                    429,
                    detail="tool_rate_limit_exceeded",
                    headers={"Retry-After": str(max(1, rate.retry_after_seconds or 1))},
                )
        if native_atlassian:
            return ceiling
        budget = await BudgetEnforcementService().check_budget_hierarchy(
            budget_context,
            ceiling,
            request_id=receipt_id,
            reserve=body.action == "call",
            tool_estimate=body.action == "call",
        )
        if body.action == "call" and not budget.allowed:
            if budget.deny_reason == DenyReason.BUDGET_EXCEEDED:
                raise HTTPException(429, "tool_budget_exceeded", headers={"Retry-After": "60"})
            raise HTTPException(503, "tool budget unavailable", headers={"Retry-After": "5"})
        return ceiling if budget.allowed else None

    connection_scopes = current_connection_scopes.get(body.tool, [])
    connection = (
        github_connection
        if body.tool == "github___get_user"
        else jira_connection
        if body.tool == "jira___get_issue"
        else atlassian_connection
        if body.tool and body.tool.startswith("atlassian___")
        else slack_connection
        if body.tool in {"slack___get_user", "slack___post_message"}
        else None
    )
    connection_id = connection.get("connection_id", connection.get("id")) if connection and body.action == "call" else None
    if body.action == "list":
        catalog = policy.get("catalog", [])
        if not isinstance(catalog, list) or len(catalog) > 64:
            raise HTTPException(503, "tool catalog unavailable")
        # Knowledge metadata comes only from the fresh authenticated producer.
        # Replace stale policy snapshots and discover newly exposed operations;
        # eligible()/permitted() still enforce existing grants and consent.
        catalog = [
            entry
            for entry in catalog
            if not isinstance(entry, dict)
            or not str(entry.get("name", "")).startswith(("knowledge___", "credentials___", "github___", "jira___", "slack___", "atlassian___"))
        ]
        catalog.extend(
            {"name": entry["name"].replace(".", "___", 1), "description": entry["description"], "inputSchema": entry["inputSchema"]}
            for entry in catalogue["tools"]
            if entry.get("connector") in {"knowledge", "credentials", "github", "jira", "slack", "atlassian"}
        )
        if effective.consent_mode == MODE_DEFAULT:
            # #8387: tools the tenant entitles without an admin per-user snapshot are described
            # from the deployed catalogue; permitted() below still decides each one.
            present = {entry.get("name") for entry in catalog if isinstance(entry, dict)}
            catalog.extend(
                {
                    "name": entry["name"].replace(".", "___", 1),
                    "description": entry.get("description", ""),
                    "inputSchema": entry.get("inputSchema", {"type": "object"}),
                }
                for entry in catalogue["tools"]
                if entry["name"] in allowed_tools
                and entry["name"] not in effective.stored_allowed
                and entry["name"].replace(".", "___", 1) not in present
                and not is_platform_tool(entry["name"])
            )
        try:
            tools = [ToolDescription.model_validate(entry) for entry in catalog]
        except ValidationError:
            raise HTTPException(503, "tool catalog unavailable") from None
        listing = [
            tool.model_dump(by_alias=True)
            for tool in tools
            if (not tool.name.startswith("atlassian___") or tool.name in current_connection_scopes)
            and (not tool.name.startswith("jira___") or jira_connection is not None)
            and (not tool.name.startswith("github___") or github_connection is not None)
            and (
                not tool.name.startswith("slack___")
                or tool.name == "slack___get_user"
                and slack_connection is not None
                or tool.name == "slack___post_message"
                and slack_post_ready
            )
            and await permitted(tool.name)
        ]
    else:
        table_name = os.environ.get("WEBHOOK_EVENTS_TABLE")
        if not table_name:
            raise HTTPException(503, "tool activity unavailable")
        if body.request_id is not None and not native_atlassian:
            admission_store = ToolCallAdmission(
                table=runtime.store.table,
                client=runtime.store.client,
                tenant_id=tenant_id,
                user_id=human_id,
                team_id=team_id,
                run_id=run_id,
                workload_id=workload_id,
                body=body,
            )
            if not await run_in_threadpool(admission_store.claim):
                return JSONResponse(
                    status_code=202,
                    content={
                        "schema_version": "1.0",
                        "tool_status": {
                            "status": "pending",
                            "replay": True,
                            "receipt_id": admission_store.receipt_id,
                            "reservation_status": "estimated_hold" if admission_store.status == "admitted" else "unknown",
                            "recovery_path": "/agent/tools/operations/recover",
                        },
                    },
                    headers={"Cache-Control": "no-store"},
                )
            receipt_id = admission_store.receipt_id
        estimated_cost = await permitted(body.tool)
        if not estimated_cost:
            raise HTTPException(403, "tool authority refused")
    if bearer:
        refreshed, owner, refreshed_team = await authenticate_human(request, db)
        if not same_principal(context, refreshed) or owner.id != human_id or refreshed_team != team_id:
            raise HTTPException(403, "tool authority refused")
    elif await reauthenticate() != context:
        raise HTTPException(404, "unknown_run")
    if native_atlassian:
        if await check_native_designation(permit_native=True) is not native:
            raise HTTPException(403, "native_test_atlassian_scope_refused")
    else:
        await check_native_designation()
    decision = {
        "schema_version": "1.0",
        "allowed": True,
        "receipt_id": receipt_id,
        "principal": {
            "user_id": human_id,
            "root_human_id": human_id,
            "tenant_id": tenant_id,
            "team_id": team_id,
        },
        "run_id": run_id,
        "workload_id": workload_id,
        "run_expires_at": run_expires_at,
        "host_generation": host_generation,
        "connection_scopes": connection_scopes,
        "connection_id": connection_id,
        "github_connection": github_connection if body.tool == "github___get_user" else None,
        "slack_connection": slack_connection if body.tool in {"slack___get_user", "slack___post_message"} else None,
        "atlassian_connection": atlassian_connection if body.tool and body.tool.startswith("atlassian___") else None,
    }
    if body.tool == "jira___get_issue":
        decision["jira_connection"] = jira_connection
    if (
        body.action == "call"
        and body.tool.startswith("atlassian___")
        and body.tool.split("___", 1)[1] in {"create_jira_issue", "update_jira_issue", "comment_jira_issue", "transition_jira_issue"}
    ):
        decision["atlassian_approval"] = {
            "tool": body.tool,
            "request_id": str(body.request_id),
            "payload_sha256": hashlib.sha256(
                json.dumps(
                    body.arguments,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest(),
            "policy_revision": effective.revision,
            "consent_source": "explicit",
        }
    if native_atlassian:
        try:
            reply = await native.prepare(
                context=context,
                credential=credential,
                operation_id=native_operation,
                body=canonical(body.model_dump(mode="json")).encode(),
            )
            if "native_status" not in reply:
                if reply["principal"] != decision["principal"]:
                    raise AdmissionRefusedError("native_test_atlassian_owner_changed")
                reply.update(
                    {
                        key: decision[key]
                        for key in (
                            "connection_scopes",
                            "connection_id",
                            "atlassian_connection",
                            "host_generation",
                        )
                    }
                )
                if "atlassian_approval" in decision:
                    reply["atlassian_approval"] = decision["atlassian_approval"]
                if body.action == "list":
                    current = {tool["name"] for tool in listing}
                    reply["tools"] = [tool for tool in reply["tools"] if tool["name"] in current]
        except AdmissionRefusedError as exc:
            raise HTTPException(403, str(exc), headers={"Cache-Control": "no-store"}) from None
        return JSONResponse(reply, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
    if body.action == "list":
        decision["tools"] = listing
        decision["dynamic_catalogue_unavailable"] = any(
            connector.get("dynamic") and connector.get("discovery_status") != "ready" for connector in catalogue.get("connectors", [])
        )
    else:
        try:
            await run_in_threadpool(
                ActivityService(table_name=table_name).record_tool_call,
                receipt_id=receipt_id,
                user_id=human_id,
                tenant_id=tenant_id,
                team_id=team_id,
                tool=body.tool,
                estimated_cost_usd=estimated_cost,
                run_id=run_id,
                request_id=str(body.request_id) if body.request_id is not None else None,
                execution_id=str(body.execution_id) if body.execution_id is not None else None,
                task_id=task_id,
                story_id=story_id,
                consent_source=effective.consent_source(body.tool.replace("___", ".", 1)),
            )
        except (ClientError, BotoCoreError):
            raise HTTPException(503, "tool activity unavailable") from None
        if admission_store is not None:
            await run_in_threadpool(admission_store.finish)
        # Activity persistence is awaited and may overlap a new designation.
        # Read again as the last awaited work before returning an allow receipt.
        await check_native_designation()
    return decision


@router.post("/tools/l5-list")
async def facade_l5_list(request: Request, db: AsyncSession = Depends(get_db)):
    from src.l5_integration import list_l5_mcp_tools

    require_facade(request)
    actor, user, _ = await authenticate_human(request, db)
    listing = await list_l5_mcp_tools(actor)
    return {**listing, "root_human_id": user.id}


@router.post("/tools/l5-call")
async def facade_l5_call(body: dict, request: Request, db: AsyncSession = Depends(get_db)):
    from src.l5_integration import L5ToolCall, call_l5_mcp_tool

    require_facade(request)
    actor, _, _ = await authenticate_human(request, db)
    return await call_l5_mcp_tool(L5ToolCall.model_validate(body), request=request, actor=actor, db=db)
