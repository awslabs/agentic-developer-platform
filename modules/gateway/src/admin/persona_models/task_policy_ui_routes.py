"""Organization Task policy editor and read-only model reservation previews."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.agentauth.task_service_policy import TaskServicePolicyError, TaskServicePolicyStore, platform_limits
from src.agentauth.task_tool_policy import TaskToolPolicyError, persona_tools
from src.auth.dependencies import get_current_user
from src.orchestration.provider_quotes import AnthropicTextQuoteAdapter, QuoteRefusedError, QuoteRequest
from src.shared.database import get_db
from src.shared.models.persona_models import ServicePrincipalAlias
from src.shared.schemas.auth import TokenContext

from . import service
from .routes import task_policy_store
from .schemas import TaskPolicyPutRequest, TaskPolicyResponse
from .self_routes import _resolve_caller, get_persona_model_current_user

router = APIRouter(tags=["task-policy-ui"])
User = Annotated[TokenContext, Depends(get_current_user)]
DB = Annotated[AsyncSession, Depends(get_db)]
Store = Annotated[TaskServicePolicyStore, Depends(task_policy_store)]


async def authorize(db, user, org_id):
    if user.account_type != "human" or user.auth_source != "jwt":
        raise HTTPException(403, "Task policy administration requires a human JWT administrator")
    await AccessControl(db).check_permission(user, Permission.ORG_UPDATE, target_org_id=org_id)


async def target(db, canonical_id, org_id):
    try:
        await service.validate_target_service_principal(db, canonical_id=canonical_id, org_id=org_id)
    except service.PreferenceRejectedError:
        raise HTTPException(404, "Service principal not found in this organization") from None


async def view(db, store, org_id, kind, principal_id):
    locator = "human:" + principal_id if kind == "human" else principal_id
    try:
        limits = platform_limits()
        policy = await run_in_threadpool(store.get, tenant_id=org_id, canonical_principal_id=locator)
        entries = await service.build_preference_list(db, org_id=org_id, principal_kind=kind, principal_id=principal_id)
        tools = {e["persona_key"]: sorted(persona_tools(e["persona_key"])) for e in entries if e["persona_key"].startswith("agent-task-")}
    except (TaskServicePolicyError, TaskToolPolicyError):
        raise HTTPException(503, "Task policy configuration unavailable") from None
    from .human_task_routes import HumanTaskPolicyResponse

    schema = HumanTaskPolicyResponse if kind == "human" else TaskPolicyResponse
    return {
        "tenant_id": org_id,
        "canonical_principal_id": locator,
        "policy": schema.model_validate(policy) if policy else None,
        "platform_limits": limits,
        "platform_limit_setting": "ADP_TASK_MAX_USD_PER_TASK",
        "persona_tools": tools,
        "models": [
            {"persona": e["persona_key"], "model": e["effective_model_id"], "revision": str(e["revision"]) if e.get("revision") else None}
            for e in entries
            if e["persona_key"].startswith("agent-task-")
        ],
    }


@router.get("/admin/organizations/{org_id}/task-policy-identity")
async def resolve_identity(
    org_id: str,
    current_user: User,
    db: DB,
    source: Literal["cognito", "iam", "legacy"],
    identity_id: Annotated[str, Query(min_length=1, max_length=255)],
):
    await authorize(db, current_user, org_id)
    alias_source = {"cognito": "cognito_m2m", "iam": "agent_registry", "legacy": "sa_registration"}[source]
    names = [identity_id]
    if source == "iam":
        from src.admin.agent_registry_service import AgentRegistryService

        row = await AgentRegistryService().get_agent(identity_id)
        if row.org_id != org_id:
            raise HTTPException(404, "Service account not found")
        names = [row.agent_name]
    else:
        names.append(f"{alias_source}:{identity_id}")
    ids = set(
        await db.scalars(
            select(ServicePrincipalAlias.canonical_service_principal_id).where(
                ServicePrincipalAlias.org_id == org_id,
                ServicePrincipalAlias.alias_source == alias_source,
                ServicePrincipalAlias.alias_id.in_(names),
                ServicePrincipalAlias.is_active.is_(True),
            )
        )
    )
    if len(ids) != 1:
        raise HTTPException(409 if ids else 404, "Service account needs an unambiguous canonical registration before Task enrollment")
    canonical_id = ids.pop()
    await target(db, canonical_id, org_id)
    return {"tenant_id": org_id, "canonical_principal_id": canonical_id}


@router.get("/admin/organizations/{org_id}/task-policies/{canonical_id}")
async def get_policy_view(org_id: str, canonical_id: str, current_user: User, db: DB, store: Store):
    await authorize(db, current_user, org_id)
    await target(db, canonical_id, org_id)
    return await view(db, store, org_id, "service_account", canonical_id)


@router.put("/admin/organizations/{org_id}/task-policies/{canonical_id}", response_model=TaskPolicyResponse)
async def save_policy(org_id: str, canonical_id: str, body: TaskPolicyPutRequest, current_user: User, db: DB, store: Store):
    await authorize(db, current_user, org_id)
    await target(db, canonical_id, org_id)
    actor = await service.validate_human_principal(db, user_id=current_user.user_id, org_id=current_user.org_id)
    try:
        return await run_in_threadpool(
            store.put,
            tenant_id=org_id,
            canonical_principal_id=canonical_id,
            expected_version=body.expected_version,
            policy=body.model_dump(exclude={"expected_version"}),
            updated_by=actor,
        )
    except TaskServicePolicyError as exc:
        code = {"version_conflict": 409, "invalid_policy": 422}.get(exc.code, 503)
        raise HTTPException(code, exc.code) from None


@router.get("/me/task-policy-view")
async def self_view(current_user: Annotated[TokenContext, Depends(get_persona_model_current_user)], db: DB, store: Store):
    try:
        kind, _, pid = await _resolve_caller(db, current_user)
    except service.PreferenceRejectedError:
        raise HTTPException(403, "Task identity unavailable") from None
    return await view(db, store, current_user.org_id, kind, pid)


@router.get("/service-principals/{canonical_id}/task-policy-view")
async def service_view(canonical_id: str, current_user: User, db: DB, store: Store):
    await authorize(db, current_user, current_user.org_id)
    await target(db, canonical_id, current_user.org_id)
    return await view(db, store, current_user.org_id, "service_account", canonical_id)


@router.get("/task-reservation-preview")
async def reservation_preview(
    current_user: User, model: Annotated[str, Query(min_length=1, max_length=255)], max_output_tokens: Annotated[int, Query(ge=1, le=10000)] = 4096
):
    # Authenticated, pure local quote: no model invocation, reservation or user-selected URL.
    request = QuoteRequest(
        body=json.dumps(
            {"model": model, "max_tokens": max_output_tokens, "messages": [{"role": "user", "content": "Task reservation preview"}]}
        ).encode(),
        path="/v1/messages",
    )
    try:
        quote = AnthropicTextQuoteAdapter().bound(request)
    except QuoteRefusedError as exc:
        return {"status": "unavailable", "reason": exc.refusal.reason, "model": model}
    return {
        "status": "available",
        "model": model,
        "reservation_usd": str(quote.total_usd),
        "max_input_tokens": quote.max_input_tokens,
        "max_output_tokens": quote.max_output_tokens,
        "pricing_revision": quote.pricing_revision,
        "basis": "full_context_upper_bound",
        "notice": (
            "Conservative per-request reservation, not an actual charge. Actual request features and all budget levels are checked at dispatch."
        ),
    }
