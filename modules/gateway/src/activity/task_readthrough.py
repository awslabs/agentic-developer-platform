"""Owner-only Activity detail/report bridge to canonical Task reads.

No Activity list projection, dispatch, worker credential, or legacy control is
created here. Run grants locate a Task; current Task policy authorizes its read.
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
from src.tasks.routes import caller_for, get_store

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


async def resolve(request: Request, db: AsyncSession, invocation_id: str) -> TaskRecord | None:
    if not INVOCATION.fullmatch(invocation_id):
        return None
    try:
        caller = await caller_for(request, db)
        # These are human Activity endpoints. Service Tasks keep their own API.
        if not caller.principal_id.startswith("human:"):
            raise errors.not_found()
        store = get_store()
        binding = await run_in_threadpool(
            store.resolve_invocation, tenant=caller.tenant_id, principal=caller.principal_id, invocation_id=invocation_id
        )
        if binding is None:
            return None
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
