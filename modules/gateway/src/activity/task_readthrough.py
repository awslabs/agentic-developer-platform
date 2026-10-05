"""Owner-only Activity detail/report bridge to canonical Task reads.

Direct-ID and owner-list discovery reuse canonical Task authorization.
No legacy GSI projection, dispatcher, worker credential, or legacy control is
created here. Discovery bindings locate Tasks; current policy authorizes reads.
"""

import json
import re

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.activity.schemas import InvocationItem
from src.tasks import authz, errors, http, snapshot
from src.tasks.read_store import TaskRecord, TaskStoreError
from src.tasks.routes import caller_for, get_optional_store, get_store

INVOCATION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
STATUS = {
    "accepted": "webhook_received",
    "queued": "webhook_received",
    "running": "in_progress",
    "waiting_for_input": "in_progress",
    "cancel_requested": "in_progress",
    "completed": "complete",
    "failed": "failed",
    "cancelled": "aborted",
}


async def resolve(request: Request, db: AsyncSession, invocation_id: str, *, canonical_user_id: str, tenant_id: str) -> TaskRecord | None:
    if not INVOCATION.fullmatch(invocation_id) or not canonical_user_id or not tenant_id:
        return None
    try:
        # Activity already authenticated and resolved this identity. Locate only
        # its own binding before requiring Task enrollment: a missing native run
        # must remain an Activity 404 for unenrolled callers too.
        principal = "human:" + canonical_user_id
        store = get_optional_store()
        if store is None:
            return None
        binding = await run_in_threadpool(store.resolve_invocation, tenant=tenant_id, principal=principal, invocation_id=invocation_id)
        if binding is None:
            return None
        caller = await caller_for(request, db)
        # The independent canonical Task authentication must resolve exactly the
        # same human and tenant. The locator itself grants no read permission.
        if caller.principal_id != principal or caller.tenant_id != tenant_id:
            raise errors.not_found()
        task_id, generation = binding
        record = await run_in_threadpool(authz.authorize_task, caller, store, task_id)
        if record.invocation_id != invocation_id or record.generation != generation:
            raise errors.not_found()
        if record.status not in STATUS:
            raise errors.prerequisite_unavailable("Task status is unavailable.")
        return record
    except errors.TaskApiError as exc:
        # Keep the Activity HTTP contract; no arbitrary backend body is exposed.
        if exc.status == 404:
            return None
        raise HTTPException(exc.status, detail={"error": exc.code, "message": exc.message}) from None
    except (TaskStoreError, BotoCoreError, ClientError):
        raise HTTPException(503, detail="Task storage is unavailable") from None


def report_status(record: TaskRecord) -> str:
    if any(value is not None and not isinstance(value, dict) for value in (record.result, record.error)):
        raise HTTPException(503, detail="Retained Task report is malformed")
    if not record.is_terminal:
        return "pending"
    return "available" if isinstance((record.result or {}).get("report"), dict) or record.error else "unavailable"


def detail(record: TaskRecord, request: Request) -> InvocationItem:
    native = snapshot.render(record, request_id=http.request_id(request))
    return InvocationItem(
        invocation_id=record.invocation_id,
        invoked_at=record.created_at,
        source_type="task",
        task_id=record.task_id,
        task_snapshot=native,
        channel="task",
        persona=record.persona,
        status=STATUS[record.status],
        status_updated_at=record.updated_at,
        # Even a recent Task update is not proof of a live process. Preserve the
        # native execution_health/recovery flags rather than inventing liveness.
        liveness="unverifiable",
        completed_at=record.updated_at if record.is_terminal else None,
        run_id=record.task_id,
        root_human_id=record.owner_principal_id.removeprefix("human:"),
        is_human_rooted=True,
        trigger_kind="human",
        transcript_kind="task_report",
        transcript_status=report_status(record),
    )


def report(record: TaskRecord) -> str:
    if report_status(record) != "available":
        raise HTTPException(404, detail="Retained Task report is pending or unavailable")
    # The canonical public result already contains the report. Do not fetch
    # arbitrary artifacts or manufacture an S3 transcript_key to show a link.
    value = {
        "task_id": record.task_id,
        "invocation_id": record.invocation_id,
        "status": record.status,
        "result": record.result,
        "error": record.error,
    }
    try:
        rendered = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    except (TypeError, ValueError):
        raise HTTPException(503, detail="Retained Task report is malformed") from None
    if len(rendered.encode()) > 5 * 1024 * 1024:
        raise HTTPException(503, detail="Retained Task report exceeds the read bound")
    # A fence longer than any contained run prevents report text breaking out.
    fence = "`" * max(3, max((len(match[0]) + 1 for match in re.finditer(r"`+", rendered)), default=3))
    return (
        "# Retained Task report\n\nThis is the committed Task result or error, not a full native agent transcript.\n\n"
        + fence
        + "json\n"
        + rendered
        + "\n"
        + fence
        + "\n"
    )


async def list_owned(request: Request, db: AsyncSession, *, canonical_user_id: str, tenant_id: str, page_size: int, after: str | None):
    from src.activity.schemas import InvocationListResponse

    if after is not None and not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z#tsk_" + INVOCATION.pattern, after):
        raise HTTPException(400, detail="Invalid Task page cursor")
    try:
        caller = await caller_for(request, db)
        if caller.principal_id != "human:" + canonical_user_id or caller.tenant_id != tenant_id:
            raise errors.not_found()
        store = get_store()
        bindings, cursor = await run_in_threadpool(store.list_owned, tenant=tenant_id, principal=caller.principal_id, limit=page_size, after=after)
        items = []
        for task_id in bindings:
            try:
                record = await run_in_threadpool(authz.authorize_task, caller, store, task_id)
            except errors.TaskApiError as exc:
                if exc.status in (403, 404):
                    continue  # Expired/removed records and revoked personas are not listable.
                raise
            if record.status not in STATUS:
                raise errors.prerequisite_unavailable("Task status is unavailable.")
            item = detail(record, request)
            # Listing carries only bounded identity/state fields. Full reports,
            # command history, and results stay on the canonical detail read.
            item.task_snapshot = {
                key: value
                for key, value in item.task_snapshot.items()
                if key in {"task_id", "invocation_id", "status", "version", "generation", "execution_health", "recovery_required"}
            }
            items.append(item)
        return InvocationListResponse(items=items, count=len(items), last_key=cursor)
    except errors.TaskApiError as exc:
        raise HTTPException(exc.status, detail={"error": exc.code, "message": exc.message}) from None
    except (TaskStoreError, BotoCoreError, ClientError):
        raise HTTPException(503, detail="Task storage is unavailable") from None
