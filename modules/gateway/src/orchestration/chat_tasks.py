"""Constrained hosted conversation turns through the existing Task admission service."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
import uuid
from typing import Annotated, Any

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.agentauth.task_admission import TaskAdmissionError
from src.agentauth.task_admission_routes import SubmitRequest, get_admission
from src.orchestration import chat_history as history
from src.shared.database import get_db
from src.tasks import authz, errors, http, snapshot
from src.tasks.routes import get_store
from src.tasks.task_commands import TaskCommands

router = APIRouter(prefix="/chat", tags=["chat"], dependencies=[Depends(history.no_store)])
PERSONA = "agent-task-investigator"
MAX_REQUESTS = 4


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    persona: str = PERSONA
    dry_run: bool = False
    # Required when replying to an outstanding Task clarification. No answer is
    # selected implicitly and a new clarification cannot consume a stale answer.
    reply_to: str | None = Field(default=None, max_length=36)


def enabled():
    return all(
        os.environ.get(flag, "false").lower() == "true"
        for flag in ("ADP_TASK_API_HUMAN_ENABLED", "ADP_TASK_API_ADMISSION_ENABLED", "ADP_TASK_API_READ_ENABLED")
    )


async def caller_for(request, db):
    if not enabled():
        raise errors.prerequisite_unavailable("Human Task chat is not enabled.")
    context, scopes = authz.authenticate(request)
    if context.account_type != "human":
        raise errors.disallowed_scope("Chat requires a human Task owner.")
    caller = await authz.resolve_caller(context, scopes, db)
    caller.require("adp-tasks/read")
    return context, caller


async def personas(request, db):
    if not enabled():
        return []
    try:
        _, caller = await caller_for(request, db)
        caller.require("adp-tasks/submit")
        policy = await run_in_threadpool(get_admission().policies.get, tenant_id=caller.tenant_id, canonical_principal_id=caller.principal_id)
        return [PERSONA] if policy and policy.get("status") == "active" and PERSONA in policy.get("allowed_personas", []) else []
    except errors.TaskApiError:
        return []


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def request_key(body):
    return digest(body.request_id)


def fingerprint(body):
    return digest({"message": body.message, "persona": body.persona, "reply_to": body.reply_to})


def put(table, row, version):
    if len(json.dumps(row, default=str).encode()) > 300000:
        raise errors.payload_too_large("Retained conversation has reached its size bound.")
    args = {"Item": row, "ConditionExpression": "attribute_not_exists(session_id)"}
    if version is not None:
        args.update(
            ConditionExpression="chat_version = :version AND owner_principal = :owner",
            ExpressionAttributeValues={":version": version, ":owner": row["owner_principal"]},
        )
    try:
        table.put_item(**args)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise errors.state_conflict("Conversation changed concurrently; reconcile the same request ID.") from None
        raise errors.prerequisite_unavailable("Conversation write outcome is unknown; reconcile the same request ID.") from None


def load(table, sid, user):
    row = table.get_item(Key={"session_id": sid}, ConsistentRead=True).get("Item")
    if not history.owned(row, user) or row.get("session_id") != sid or row.get("chat_task_persona") != PERSONA:
        raise errors.not_found()
    if history.number(row.get("expires_at")) <= int(time.time()):
        raise errors.TaskApiError(410, "history_expired", "Conversation retention expired.")
    return row


def task_record(caller, task_id):
    return authz.authorize_task(caller, get_store(), task_id)


def task_answer(record):
    if record.status != "completed" or not record.result or record.result.get("process_exit_validated") is not True:
        return None
    report = record.result.get("report")
    if not isinstance(report, dict):
        raise errors.prerequisite_unavailable("Completed task report is unavailable.")
    content = json.dumps(report, ensure_ascii=False, sort_keys=True)
    if len(content) > 10000:
        raise errors.payload_too_large("Task report exceeds the conversation context bound; inspect it with adp task status.")
    return {"role": "assistant", "content": content, "timestamp": int(time.time()), "task_id": record.task_id}


async def enrich(row, user, request, db):
    _, caller = await caller_for(request, db)
    if row.get("chat_task_owner") != caller.principal_id:
        raise errors.not_found()
    value = history.project(row, user)
    key = row.get("chat_active_request")
    entry = row.get("chat_requests", {}).get(key, {})
    task_id = entry.get("task_id")
    value.update(persona=PERSONA, active_request_id=entry.get("request_id"), task_id=task_id, task=None)
    if not task_id:
        value.update(status="pending", next_action="Reconcile the same request ID with unchanged content; admission outcome is unknown.")
        return value
    record = await run_in_threadpool(task_record, caller, task_id)
    value["task"] = snapshot.render(record, request_id=http.request_id(request))
    value["pending_task_ids"] = [] if record.is_terminal else [task_id]
    value["status"] = "idle" if record.is_terminal else "pending"
    answer = task_answer(record)
    if answer and not any(m.get("task_id") == task_id and m.get("role") == "assistant" for m in value["messages"]):
        answer["content"] = history.text(answer["content"])
        value["messages"].append(answer)
    # Task errors, questions and report may contain user content; use same scrubber.
    value["task"] = history.scrubber.scrub_dict(value["task"]).content
    return value


async def send(body, request, db, table, sid=None):
    history.require_store(table)
    user, caller = await caller_for(request, db)
    caller.require("adp-tasks/submit")
    if body.persona != PERSONA:
        raise errors.disallowed_scope("Only the authorized investigator hosted persona is supported.")
    allowed = await personas(request, db)
    if PERSONA not in allowed:
        raise errors.disallowed_scope("This human is not enrolled for the requested persona.")
    key = request_key(body)
    if sid is None:
        sid = "chat-" + uuid.uuid5(uuid.NAMESPACE_URL, history.owner(user) + ":" + body.request_id).hex
        existing = await run_in_threadpool(table.get_item, Key={"session_id": sid}, ConsistentRead=True)
        if existing.get("Item"):
            row = load(table, sid, user)
        else:
            now = int(time.time())
            row = dict(
                session_id=sid,
                owner_principal=history.owner(user),
                owner_user_id=user.user_id,
                user_workspace=user.user_id + "#webchat",
                tenant_id=user.org_id,
                org_id=user.org_id,
                channel="webchat",
                created_at=now,
                updated_at=now,
                expires_at=now + 86400,
                messages=[],
                threads={},
                chat_version=0,
                chat_task_persona=PERSONA,
                chat_task_owner=caller.principal_id,
                chat_requests={},
                chat_active_request="",
            )
    else:
        row = await run_in_threadpool(load, table, sid, user)
    if row.get("chat_task_owner") != caller.principal_id:
        raise errors.not_found()
    entry = row["chat_requests"].get(key)
    if entry:
        if entry["fingerprint"] != fingerprint(body):
            raise errors.TaskApiError(409, "idempotency_conflict", "Request ID was already used with different content.")
    else:
        if len(row["chat_requests"]) >= MAX_REQUESTS:
            raise errors.state_conflict("Conversation reached its four-request bound; export its history before starting a separate session.")
        row = copy.deepcopy(row)
        active = row["chat_requests"].get(row["chat_active_request"])
        kind, task_id, command_payload = "task", None, None
        if active:
            if not active.get("task_id") or not active.get("receipt"):
                raise errors.state_conflict("Previous admission is uncertain; reconcile its original request before sending another.")
            record = await run_in_threadpool(task_record, caller, active["task_id"])
            if record.status == "waiting_for_input":
                caller.require("adp-tasks/input")
                expected = (record.input_request or {}).get("input_request_id")
                if not body.reply_to or body.reply_to != expected:
                    raise errors.state_conflict("Read the pending question and supply its exact reply_to ID.")
                kind, task_id = "input", record.task_id
                command_payload = {"text": body.message, "reply_to": body.reply_to}
            elif not record.is_terminal:
                raise errors.state_conflict("A turn is pending; reconnect to the same task instead of submitting another.")
            else:
                answer = task_answer(record)
                if answer and not any(m.get("role") == "assistant" and m.get("task_id") == record.task_id for m in row["messages"]):
                    row["messages"].append(answer)
        if kind == "task" and body.reply_to:
            raise errors.state_conflict("No matching clarification is pending.")
        context = json.dumps(row["messages"], ensure_ascii=False, default=str)
        if len(context) > 16000:
            raise errors.payload_too_large("Prior conversation exceeds the bounded Task input context.")
        submit = SubmitRequest(
            schema_version="1.0", persona=PERSONA, instructions=body.message, inputs={"conversation_history": context}, external_reference=sid
        ).model_dump()
        entry = dict(
            request_id=body.request_id, fingerprint=fingerprint(body), submit=submit, kind=kind, task_id=task_id, command_payload=command_payload
        )
        if body.dry_run:
            return dict(
                status="dry_run",
                session_id=sid,
                request_id=body.request_id,
                persona=PERSONA,
                tenant_id=user.org_id,
                user_id=user.user_id,
                kind=kind,
                dispatched=False,
            )
        row["chat_requests"][key] = entry
        row["chat_active_request"] = key
        old_version = row["chat_version"]
        row["chat_version"] += 1
        row["updated_at"] = int(time.time())
        # Existing row version 0 is never persisted: the first put atomically
        # creates the session and durable frozen request. Lost ACK is replayable.
        await run_in_threadpool(put, table, row, old_version if old_version else None)
    if body.dry_run:
        return dict(
            status="dry_run",
            session_id=sid,
            request_id=body.request_id,
            persona=PERSONA,
            tenant_id=user.org_id,
            user_id=user.user_id,
            kind=entry["kind"],
            dispatched=False,
        )
    idem = "chat-" + digest([sid, key])
    if entry["kind"] == "input":
        caller.require("adp-tasks/input")
        receipt = await run_in_threadpool(
            TaskCommands(get_store().repository).admit,
            task_id=entry["task_id"],
            command_id=str(uuid.UUID(digest([sid, key])[:32], version=4)),
            kind="input",
            payload=entry["command_payload"],
            principal=caller.principal_id,
            tenant=caller.tenant_id,
            expires_at=user.expires_at,
        )
        task_id = entry["task_id"]
    else:
        try:
            receipt = await get_admission().admit(caller=caller, submit=entry["submit"], idempotency_key=idem, db=db)
        except TaskAdmissionError as exc:
            raise errors.TaskApiError(exc.status, exc.code, "Canonical Task admission refused this turn.") from None
        task_id = receipt["task_id"]
    latest = await run_in_threadpool(load, table, sid, user)
    stored = latest["chat_requests"][key]
    if not stored.get("receipt"):
        # Another retry may already have committed the same Task receipt. CAS
        # keeps transcript updates once-only; a lost CAS retries the same Task ID.
        version = latest["chat_version"]
        stored.update(task_id=task_id, receipt=receipt)
        latest["messages"].append({"role": "user", "content": body.message, "task_id": task_id, "timestamp": int(time.time())})
        latest["chat_version"] += 1
        await run_in_threadpool(put, table, latest, version)
    return dict(
        status="pending",
        session_id=sid,
        task_id=task_id,
        request_id=body.request_id,
        persona=PERSONA,
        tenant_id=user.org_id,
        user_id=user.user_id,
        receipt=receipt,
        expires_at=latest["expires_at"],
        next_action="Watch this exact task; answer only an explicit pending question.",
    )


@router.post("/sessions", status_code=202)
@history.contract_errors
async def start(body: Turn, request: Request, db: Annotated[AsyncSession, Depends(get_db)], table: Annotated[Any, Depends(history.store)]):
    return await send(body, request, db, table)


@router.post("/sessions/{session_id}/turns", status_code=202)
@history.contract_errors
async def resume(
    session_id: str, body: Turn, request: Request, db: Annotated[AsyncSession, Depends(get_db)], table: Annotated[Any, Depends(history.store)]
):
    if not history.SESSION.fullmatch(session_id):
        raise errors.not_found()
    return await send(body, request, db, table, session_id)
