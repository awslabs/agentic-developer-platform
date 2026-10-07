"""Bounded human readback of the existing ingest/response session table (#5640)."""

from __future__ import annotations

import functools
import json
import os
import re
import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.agentauth.chat_authority import current_chat_member
from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.agentauth.chat_event_journal import ChatEventJournal
from src.agentauth.chat_session_cleanup import request_session_end
from src.agentauth.chat_session_mailbox import ChatSessionMailbox
from src.auth.dependencies import get_current_user
from src.chat_logging.scrubber import RegexScrubber
from src.features.routes import _is_enabled
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext
from src.tasks import errors


def no_store(response: Response):
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/chat", tags=["chat"], dependencies=[Depends(no_store)])
SESSION = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")
MAX_MESSAGES = 100
MAX_CHARS = 100000
scrubber = RegexScrubber()


def contract_errors(function):
    @functools.wraps(function)
    async def wrapped(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except HTTPException:
            raise
        except errors.TaskApiError as exc:
            raise HTTPException(exc.status, detail={"error": exc.code}) from None
        except Exception:
            raise unavailable() from None

    return wrapped


def unavailable():
    return HTTPException(503, detail={"error": "chat_unavailable", "message": "Hosted chat is unavailable on this deployment."})


def store():
    from src.orchestration.intake_wiring import _get_sessions_table

    return _get_sessions_table()


def mode_store():
    from src.orchestration.intake_wiring import _get_context_table

    if os.environ.get("ADP_CHAT_DATA_ENABLED") != "true":
        raise unavailable()
    table = _get_context_table()
    if table is None:
        raise unavailable()
    return ChatSessionMailbox(table)


class SessionModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["ephemeral", "persistent"]


async def owned_session(session_id: str, user: TokenContext, table, db: AsyncSession):
    human(user)
    require_store(table)
    if not SESSION.fullmatch(session_id):
        raise HTTPException(404, detail={"error": "session_not_found"})
    row = await run_in_threadpool(lambda: table.get_item(Key={"session_id": session_id}, ConsistentRead=True).get("Item"))
    if not owned(row, user) or row.get("session_id") != session_id or number(row.get("expires_at")) <= int(time.time()):
        raise HTTPException(404, detail={"error": "session_not_found"})
    if not await current_chat_member(db, user.org_id, user.user_id, user.team_id or ""):
        raise HTTPException(404, detail={"error": "session_not_found"})
    return row


async def owned_mode(session_id: str, user: TokenContext, table, db: AsyncSession):
    await owned_session(session_id, user, table, db)
    return user.org_id, user.team_id or "", user.user_id


@router.get("/sessions/{session_id}/events")
@contract_errors
async def replay_events(
    session_id: str,
    user: Annotated[TokenContext, Depends(get_current_user)],
    table: Annotated[Any, Depends(store)],
    mailbox: Annotated[ChatSessionMailbox, Depends(mode_store)],
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: Annotated[str | None, Query(pattern=r"^[a-f0-9]{32}:[0-9]{1,8}$", max_length=41)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
):
    row = await owned_session(session_id, user, table, db)
    generation = number(row.get("created_at"))
    if not generation:
        raise unavailable()
    try:
        result = await run_in_threadpool(
            ChatEventJournal(mailbox.table).replay,
            session_id=session_id,
            owner=(user.org_id, user.team_id or "", user.user_id),
            generation=generation,
            cursor=cursor,
            limit=limit,
            now=int(time.time()),
        )
        confirmed = await owned_session(session_id, user, table, db)
        if number(confirmed.get("created_at")) != generation:
            raise ChatAuthorizationRefusedError("chat replay generation changed")
        return result
    except ChatAuthorizationRefusedError:
        raise HTTPException(404, detail={"error": "session_not_found"}) from None


def resolved_session_state(mailbox, *, session_id, owner, now):
    header = mailbox._header(session_id, owner, now)
    if header and header.get("sessionPendingMode") == "persistent" and header.get("sessionMode") == "ephemeral":
        lease = header.get("chatLease", {})
        from src.agentauth.chat_data_routes import runtime

        authority, capabilities = runtime()
        launch = capabilities.launches.load(lease.get("run_id", ""))
        if (
            launch.session_run_id is not None
            or (launch.tenant_id, launch.team_id, launch.user_id, launch.session_id) != (*owner, session_id)
            or (launch.sandbox_uid, launch.lease_generation) != (lease.get("sandbox_uid"), lease.get("generation"))
        ):
            raise ChatAuthorizationRefusedError("chat pending mode binding changed")
        teardown = authority.store._read(f"CHAT-LAUNCH#{launch.run_id}", "TEARDOWN") or {}
        terminal = authority.store._read(f"CHAT-DELIVERY#{launch.run_id}", "TERMINAL") or {}
        if "removed_at" in teardown and "completion_receipt" in terminal:
            mailbox.finish_pending_mode(session_id=session_id, owner=owner, lease=lease, now=now)
    return mailbox.state(session_id=session_id, owner=owner, now=now)


@router.get("/sessions/{session_id}/mode")
@contract_errors
async def show_mode(
    session_id: str,
    user: Annotated[TokenContext, Depends(get_current_user)],
    table: Annotated[Any, Depends(store)],
    mailbox: Annotated[ChatSessionMailbox, Depends(mode_store)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    identity = await owned_mode(session_id, user, table, db)
    try:
        return await run_in_threadpool(resolved_session_state, mailbox, session_id=session_id, owner=identity, now=int(time.time()))
    except ChatAuthorizationRefusedError:
        raise HTTPException(404, detail={"error": "session_not_found"}) from None


@router.put("/sessions/{session_id}/mode")
@contract_errors
async def change_mode(
    session_id: str,
    body: SessionModeRequest,
    user: Annotated[TokenContext, Depends(get_current_user)],
    table: Annotated[Any, Depends(store)],
    mailbox: Annotated[ChatSessionMailbox, Depends(mode_store)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    identity = await owned_mode(session_id, user, table, db)
    try:
        await run_in_threadpool(mailbox.select_mode, session_id=session_id, owner=identity, mode=body.mode, now=int(time.time()))
        return await run_in_threadpool(resolved_session_state, mailbox, session_id=session_id, owner=identity, now=int(time.time()))
    except ChatAuthorizationRefusedError as error:
        if "must end" in str(error) or "mode change pending" in str(error):
            raise HTTPException(409, detail={"error": "session_mode_pending"}) from None
        raise HTTPException(404, detail={"error": "session_not_found"}) from None


@router.post("/sessions/{session_id}/end")
@contract_errors
async def end_session(
    session_id: str,
    user: Annotated[TokenContext, Depends(get_current_user)],
    table: Annotated[Any, Depends(store)],
    mailbox: Annotated[ChatSessionMailbox, Depends(mode_store)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    identity = await owned_mode(session_id, user, table, db)
    try:
        await run_in_threadpool(request_session_end, mailbox.table, session_id=session_id, owner=identity, now=int(time.time()), reason="user")
        return await run_in_threadpool(mailbox.state, session_id=session_id, owner=identity, now=int(time.time()))
    except ChatAuthorizationRefusedError:
        raise HTTPException(404, detail={"error": "session_not_found"}) from None


def human(user: TokenContext):
    if user.account_type != "human" or not user.org_id or not user.user_id:
        raise HTTPException(403, detail={"error": "human_required"})


def require_store(table):
    if not _is_enabled("FEATURE_CHAT_ENABLED") or table is None:
        raise unavailable()


def owner(user: TokenContext):
    # Canonical ingest _session_owner_principal: tenant, org, team, user, channel.
    return json.dumps([user.org_id, user.org_id, user.team_id or "", user.user_id, "webchat"], separators=(",", ":"))


def owned(row, user):
    return (
        isinstance(row, dict)
        and row.get("owner_principal") == owner(user)
        and row.get("owner_user_id") == user.user_id
        and row.get("user_workspace") == user.user_id + "#webchat"
        and row.get("tenant_id") == user.org_id
        and row.get("org_id") == user.org_id
        and row.get("channel") == "webchat"
    )


def number(value):
    try:
        result = int(value)
        if result < 0 or result != value:
            raise ValueError
        return result
    except (TypeError, ValueError, OverflowError):
        raise unavailable() from None


def text(value, limit=10000):
    if not isinstance(value, str):
        raise unavailable()
    if len(value) > 400000:
        raise unavailable()
    # Scrub before truncation so cutting a token never defeats its pattern.
    value = scrubber.scrub_text(value).content
    # Keep terminal control bytes out of both human rendering and exports.
    cleaned = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", value)
    return cleaned[:limit]


def project(row, user, transcript=True):
    expiry = number(row.get("expires_at"))
    if expiry <= int(time.time()):
        raise HTTPException(
            410, detail={"error": "history_expired", "message": "Conversation retention expired; it cannot be resumed from this history."}
        )
    sid = row.get("session_id")
    if not isinstance(sid, str) or not SESSION.fullmatch(sid):
        raise unavailable()
    threads = row.get("threads", {})
    if not isinstance(threads, dict) or len(threads) > 1000:
        raise unavailable()
    pending = []
    for thread in threads.values():
        if not isinstance(thread, dict):
            raise unavailable()
        task = thread.get("processing_task_id")
        if task:
            if not isinstance(task, str) or not SESSION.fullmatch(task):
                raise unavailable()
            pending.append(task)
    messages, used, truncated = [], 0, False
    if transcript:
        raw = row.get("messages", [])
        if not isinstance(raw, list):
            raise unavailable()
        truncated = len(raw) > MAX_MESSAGES
        for message in raw[-MAX_MESSAGES:]:
            if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
                # Tool/system metadata is not a public transcript contract.
                truncated = True
                continue
            content = message.get("content", "")
            if not isinstance(content, str):
                raise unavailable()
            if used + min(len(content), 10000) > MAX_CHARS:
                truncated = True
                break
            body = text(content)
            used += len(body)
            # The writer itself caps stored content at 10,000 characters.
            # At that boundary original completeness is unknown.
            truncated |= len(content) >= 10000
            task = message.get("task_id", "")
            if task and (not isinstance(task, str) or not SESSION.fullmatch(task)):
                raise unavailable()
            messages.append({"role": message["role"], "content": body, "task_id": task, "timestamp": number(message.get("timestamp", 0))})
    return {
        "session_id": sid,
        "tenant_id": user.org_id,
        "user_id": user.user_id,
        "expires_at": expiry,
        "updated_at": number(row.get("updated_at", 0)),
        "repository": text(row.get("intake_repository", ""), 300),
        "pending_task_ids": sorted(set(pending)),
        "messages": messages,
        "truncated": truncated,
        "redaction": "known-secret-patterns",
        "retention_seconds": 86400,
        "status": "pending" if pending else "unknown" if row.get("chat_task_persona") else "idle",
        "answer_completion_verified": False,
    }


@router.get("/capabilities")
@contract_errors
async def capabilities(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[TokenContext, Depends(get_current_user)],
    table: Annotated[Any, Depends(store)],
):
    from src.orchestration.chat_tasks import personas

    human(user)
    allowed = await personas(request, db)
    enabled = _is_enabled("FEATURE_CHAT_ENABLED")
    return {
        "tenant_id": user.org_id,
        "user_id": user.user_id,
        "enabled": enabled,
        "history_configured": table is not None,
        "history_ready": "unknown" if enabled and table is not None else "no",
        "general_turns_supported": True,
        "authorized_personas": allowed,
        "reason": "ready_for_admission_check" if allowed else "human_task_policy_or_deployment_unavailable",
        "retention_seconds": 86400,
    }


@router.get("/sessions")
def list_sessions(
    user: Annotated[TokenContext, Depends(get_current_user)],
    table: Annotated[Any, Depends(store)],
    limit: int = Query(default=20, ge=1, le=100),
    page: int = Query(default=1, ge=1, le=100),
):
    human(user)
    require_store(table)
    kwargs = dict(
        IndexName="user-workspace-index",
        KeyConditionExpression="user_workspace = :workspace",
        ExpressionAttributeValues={":workspace": user.user_id + "#webchat"},
        Limit=limit,
    )
    try:
        response = table.query(**kwargs)
        for _ in range(page - 1):
            key = response.get("LastEvaluatedKey")
            if not key:
                response = {}
                break
            kwargs["ExclusiveStartKey"] = key
            response = table.query(**kwargs)
        result = []
        # Re-read each GSI hit consistently: projection/ownership may have changed.
        for candidate in response.get("Items", []):
            sid = candidate.get("session_id")
            if not isinstance(sid, str) or not SESSION.fullmatch(sid):
                continue
            row = table.get_item(Key={"session_id": sid}, ConsistentRead=True).get("Item")
            if owned(row, user) and number(row.get("expires_at")) > int(time.time()):
                result.append(project(row, user, transcript=False))
        return {
            "tenant_id": user.org_id,
            "user_id": user.user_id,
            "items": result,
            "next_page": page + 1 if response.get("LastEvaluatedKey") and page < 100 else None,
            "scan_limit_reached": bool(response.get("LastEvaluatedKey")) and page == 100,
            "order": "session_id",
            "retention_seconds": 86400,
        }
    except HTTPException:
        raise
    except Exception:
        raise unavailable() from None


@router.get("/sessions/{session_id}")
@contract_errors
async def show_session(
    session_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[TokenContext, Depends(get_current_user)],
    table: Annotated[Any, Depends(store)],
):
    human(user)
    require_store(table)
    if not SESSION.fullmatch(session_id):
        raise HTTPException(404, detail={"error": "session_not_found"})
    try:
        row = table.get_item(Key={"session_id": session_id}, ConsistentRead=True).get("Item")
    except Exception:
        raise unavailable() from None
    if not owned(row, user) or row.get("session_id") != session_id:
        raise HTTPException(404, detail={"error": "session_not_found"})
    if row.get("chat_task_persona"):
        from src.orchestration.chat_tasks import enrich

        return await enrich(row, user, request, db)
    return project(row, user)
