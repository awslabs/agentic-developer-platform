"""Pinned shared-harness context for verified GitHub executions.

This is an invocation adapter. It reuses the existing run credential, model
resolver and persona snapshot format; callers cannot supply identity or grants.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.agentauth.routes import AgentRuntime, ModelDecisionRequest, _agent_call, get_agent_runtime, require_agent_transport, resolved_model_response
from src.agentauth.task_harness import REVISION, validate_snapshot
from src.shared.database import get_db

router = APIRouter(prefix="/internal/v1/agent", tags=["agent-authority"], dependencies=[Depends(require_agent_transport)])


class OperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: Literal["claim", "settle"]
    kind: Literal["model", "report", "tool", "planning"]
    effect_key: str | None = Field(default=None, pattern=r"^(story-create|story-link|story-blocker|dispatch):[a-z0-9:-]{1,140}$")
    result: str | None = Field(default=None, max_length=65536)


# Only the report adapter is qualified here. Executable capabilities are added
# with their host tools, never by a definition requesting more authority.
REPORT_PERSONAS = frozenset({"agent-codex-architect", "agent-codex-product", "agent-codex-pm", "agent-codex-intent-refinement"})


def _snapshot(env, key):
    path = env.get("ADP_CODEX_PERSONA_CATALOG_FILE") or Path(__file__).with_name("codex-github-catalogue.json")
    with Path(path).open("rb") as stream:
        raw = stream.read(2097153)
    if len(raw) > 2097152:
        raise ValueError("persona catalogue exceeds bound")
    catalogue = json.loads(raw)
    if not isinstance(catalogue, dict) or catalogue.get("schemaVersion") != 1:
        raise ValueError("invalid persona catalogue")
    entries = catalogue.get("snapshots")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 64:
        raise ValueError("invalid persona catalogue")
    selected = None
    seen = set()
    for value in entries:
        snapshot, definition = validate_snapshot(value)
        if definition.key in seen:
            raise ValueError("duplicate persona")
        seen.add(definition.key)
        if definition.key == key:
            selected = snapshot, definition
    if selected is None:
        raise ValueError("persona unavailable")
    return selected


def frozen_context(store, record, grant, env, *, now=None):
    """Freeze once in the protected authority table, including on pod replacement."""
    env = os.environ if env is None else env
    now = now or datetime.now(UTC)
    pk = f"TENANT#{record.tenant_id}"
    execution = store._read(pk, f"EXEC#{record.invocation_id}") or {}
    persona = execution.get("persona", {}).get("S")
    if persona not in REPORT_PERSONAS or record.repo not in grant.repo_scope:
        raise ValueError("unsupported GitHub persona")
    enabled = {p.strip() for p in env.get("ADP_CODEX_GITHUB_PERSONAS", "").split(",") if p.strip()}
    if persona not in enabled:
        raise ValueError("GitHub persona disabled")
    if not grant.is_live(now):
        raise ValueError("expired grant")
    key = "gpt-" + persona.removeprefix("agent-codex-")
    sk = f"CODEX_GITHUB#{record.invocation_id}"
    existing = store._read(pk, sk)
    if not existing:
        snapshot, definition = _snapshot(env, key)
        completion = "report"
        if definition.completionPolicy != completion or "github" not in definition.surfaces:
            raise ValueError("unsupported completion policy or surface")
        capabilities = ["artifacts.publish"]
        if "repository.read" in definition.requiredCapabilities + definition.optionalCapabilities:
            capabilities.append("repository.read")
        if persona == "agent-codex-architect" and "story.create" in definition.optionalCapabilities:
            capabilities.append("story.create")
        if persona == "agent-codex-pm" and "agents.delegate" in definition.optionalCapabilities:
            capabilities.append("agents.delegate")
        if not set(definition.requiredCapabilities).issubset(capabilities):
            raise ValueError("required capability unavailable")
        if any(skill.requiredTools or set(skill.requiredCapabilities or []) - set(capabilities) for skill in definition.skills):
            raise ValueError("required skill tool unavailable")
        deadline = min(now + timedelta(milliseconds=definition.limits.maxDurationMs), grant.expires_at or now + timedelta(minutes=45))
        context = {
            "version": 1,
            "persona": persona,
            "repository": record.repo,
            "repositoryId": execution.get("provider_repository_id", {}).get("N", ""),
            "issue": int(execution.get("issue_number", {}).get("N", "0")),
            "snapshot": snapshot.model_dump(),
            "capabilities": capabilities,
            "deadlineMs": int(deadline.timestamp() * 1000),
            # Legacy wire fields for older workers; current direct GitHub runs
            # use these as neither model/tool admission caps nor capability grants.
            "maxTurns": min(definition.limits.maxTurns, 20),
            "maxTools": 32 if len(capabilities) > 1 else 0,
            "maxOutputTokens": 4096,
            "harnessRevision": REVISION,
        }
        if not context["repositoryId"].isdigit() or int(context["repositoryId"]) < 1:
            raise ValueError("missing protected repository identity")
        if context["issue"] < 1:
            raise ValueError("missing protected issue binding")
        raw = json.dumps(context, separators=(",", ":"))
        if len(raw.encode()) > 60000:
            raise ValueError("GitHub context exceeds bound")
        try:
            store.client.put_item(
                TableName=store.table,
                Item={"pk": {"S": pk}, "sk": {"S": sk}, "context": {"S": raw}, "attempt": {"N": str(record.current_attempt)}},
                ConditionExpression="attribute_not_exists(pk)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
        existing = store._read(pk, sk)
    context = json.loads(existing["context"]["S"])
    snapshot, definition = validate_snapshot(context["snapshot"])
    if int(existing.get("attempt", {}).get("N", "0")) != record.current_attempt:
        raise ValueError("interrupted GitHub execution requires reconciliation")
    if (
        context["persona"] != persona
        or definition.key != key
        or context["repository"] != record.repo
        or context["deadlineMs"] <= now.timestamp() * 1000
    ):
        raise ValueError("stale or incompatible GitHub session")
    return context


def operation(store, record, body):
    """Journal before effects. Unknown outcomes retain the fence across pods."""
    pk = f"TENANT#{record.tenant_id}"
    session_key = {"pk": {"S": pk}, "sk": {"S": f"CODEX_GITHUB#{record.invocation_id}"}}
    key = {"pk": {"S": pk}, "sk": {"S": f"CODEX_OPERATION#{record.invocation_id}#{body.operation_id}"}}
    session = store._read(pk, session_key["sk"]["S"])
    frozen = json.loads(session["context"]["S"])
    if body.kind == "planning":
        if not body.effect_key:
            raise ValueError("missing planning effect key")
        allowed = (
            frozen["persona"] == "agent-codex-architect" and "story.create" in frozen["capabilities"] and body.effect_key.startswith("story-")
        ) or (frozen["persona"] == "agent-codex-pm" and "agents.delegate" in frozen["capabilities"] and body.effect_key.startswith("dispatch:"))
        if not allowed:
            raise ValueError("planning effect unavailable")
        key["sk"] = {"S": f"CODEX_PLANNING#{frozen['repositoryId']}#{frozen['issue']}#{body.effect_key}"}
    elif body.effect_key is not None:
        raise ValueError("unexpected planning effect key")
    existing = store._read(pk, key["sk"]["S"])
    if existing:
        if existing["request_digest"]["S"] != body.request_digest or existing["kind"]["S"] != body.kind:
            raise ValueError("operation conflict")
        if existing["status"]["S"] == "confirmed":
            if body.action == "settle" and existing["result"]["S"] != body.result:
                raise ValueError("settlement conflict")
            return {"status": "confirmed", "result": existing["result"]["S"]}
        if body.action == "claim":
            # Never reissue an admission whose response/side effect may be lost.
            raise ValueError("operation requires reconciliation")
    elif body.action != "claim":
        raise ValueError("unknown operation")
    identifier = str(body.operation_id)
    if body.action == "claim":
        if body.result is not None:
            raise ValueError("claim cannot supply result")
        session = store._read(pk, session_key["sk"]["S"])
        frozen = json.loads(session["context"]["S"])
        if body.kind == "tool" and "repository.read" not in frozen["capabilities"]:
            raise ValueError("tool capability unavailable")
        counter = {"model": "model_count", "tool": "tool_count", "report": "report_count", "planning": "planning_count"}[body.kind]
        # Model and tool counters are accounting, not hidden run ceilings.
        # Preserve publication fencing and separately bounded planning effects.
        limit = {"report": 1, "planning": 960}.get(body.kind)
        store.client.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": store.table,
                        "Item": {**key, "request_digest": {"S": body.request_digest}, "kind": {"S": body.kind}, "status": {"S": "pending"}},
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
                {
                    "Update": {
                        "TableName": store.table,
                        "Key": session_key,
                        "UpdateExpression": "SET inflight = :operation"
                        + (", finalizing = :operation" if body.kind == "report" else "")
                        + f" ADD operation_count :one, {counter} :one",
                        "ConditionExpression": "attribute_exists(pk) AND attribute_not_exists(inflight) AND attribute_not_exists(finalizing) "
                        + (f"AND (attribute_not_exists({counter}) OR {counter} < :limit)" if limit is not None else ""),
                        "ExpressionAttributeValues": {
                            ":operation": {"S": identifier},
                            ":one": {"N": "1"},
                            **({":limit": {"N": str(limit)}} if limit is not None else {}),
                        },
                    }
                },
            ]
        )
        return {"status": "admitted"}
    if body.result is None or len(body.result.encode()) > 65536:
        raise ValueError("invalid settlement")
    store.client.transact_write_items(
        TransactItems=[
            {
                "Update": {
                    "TableName": store.table,
                    "Key": key,
                    "UpdateExpression": "SET #status = :confirmed, #result = :result",
                    "ConditionExpression": "#status = :pending",
                    "ExpressionAttributeNames": {"#status": "status", "#result": "result"},
                    "ExpressionAttributeValues": {":pending": {"S": "pending"}, ":confirmed": {"S": "confirmed"}, ":result": {"S": body.result}},
                }
            },
            {
                "Update": {
                    "TableName": store.table,
                    "Key": session_key,
                    "UpdateExpression": "REMOVE inflight",
                    "ConditionExpression": "inflight = :operation",
                    "ExpressionAttributeValues": {":operation": {"S": identifier}},
                }
            },
        ]
    )
    return {"status": "confirmed", "result": body.result}


@router.post("/codex-persona-operation")
async def codex_persona_operation(body: OperationRequest, request: Request, runtime: AgentRuntime = Depends(get_agent_runtime)):
    async def execute(_body, _credential, _workload, *, context):
        _, _, record, grant = context
        try:
            await run_in_threadpool(frozen_context, runtime.store, record, grant, runtime.env)
            return await run_in_threadpool(operation, runtime.store, record, body)
        except (ValueError, KeyError, TypeError, ClientError):
            raise HTTPException(409, "Codex operation unavailable; reconciliation may be required") from None

    return await _agent_call(request, runtime, execute, body)


@router.post("/codex-persona-session")
async def codex_persona_session(
    body: ModelDecisionRequest, request: Request, runtime: AgentRuntime = Depends(get_agent_runtime), db: AsyncSession = Depends(get_db)
):
    async def resolve(_body, _credential, _workload, *, context):
        _, _, record, grant = context
        from src.orchestration.work_admission import worker_checkpoint

        await worker_checkpoint(org_id=record.tenant_id, invocation_id=record.invocation_id, store=runtime.store)
        try:
            frozen = await run_in_threadpool(frozen_context, runtime.store, record, grant, runtime.env)
        except (ValueError, KeyError, OSError, TypeError):
            raise HTTPException(409, "Codex persona admission unavailable") from None
        # Signing binds the same current execution to both the selected model and
        # immutable persona context. This does not change existing SDK postures.
        return await resolved_model_response(
            db=db,
            runtime=runtime,
            record=record,
            grant=grant,
            nonce=body.nonce,
            client_contract=body.model_policy_contract,
            response_context={"codex_persona": frozen},
        )

    return await _agent_call(request, runtime, resolve, body)
