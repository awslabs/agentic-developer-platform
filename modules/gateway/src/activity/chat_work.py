"""Delegated chat projection of existing user-owned Activity and Task reads."""

import os
import re
from datetime import UTC, datetime, timedelta

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from src.activity import task_readthrough
from src.activity.external_scope import read_external_work
from src.activity.service import ActivityService, _decode_cursor, _encode_cursor
from src.activity.work_summary import summarize_work
from src.agentauth.chat_capability import ChatLaunch
from src.agentauth.task_service_policy import TaskServicePolicyError, TaskServicePolicyStore
from src.tasks import authz, errors
from src.tasks.read_store import TaskStoreError


def _cursor(opaque: str | None, launch: ChatLaunch, since: str, until: str) -> tuple[bool, dict]:
    if opaque is None:
        return True, {"direct": None, "descendants": None, "tasks": None}
    try:
        state = _decode_cursor(opaque)
        if state.get("scope") != [launch.user_id, launch.tenant_id, since, until]:
            raise ValueError("scope mismatch")
        if any(key not in state or (state[key] is not None and not isinstance(state[key], str)) for key in ("direct", "descendants", "tasks")):
            raise ValueError("invalid source cursors")
        if state["tasks"] is not None and not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z#tsk_" + task_readthrough.INVOCATION.pattern,
            state["tasks"],
        ):
            raise ValueError("invalid Task cursor")
        return False, state
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, detail={"error": "activity_cursor_invalid"}) from None


def _within_window(invoked_at: str, start: datetime, end: datetime) -> bool:
    try:
        stamp = datetime.fromisoformat(invoked_at.replace("Z", "+00:00"))
        return stamp.tzinfo is not None and start <= stamp < end
    except (ValueError, TypeError):
        return False


def _valid_instant(value: str) -> bool:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except (ValueError, TypeError):
        return False


async def read_work(
    request: Request,
    launch: ChatLaunch,
    service: ActivityService,
    *,
    since: str,
    until: str,
    page_size: int,
    last_key: str | None,
    timezone: str = "UTC",
    observed_at: datetime | None = None,
) -> dict:
    """Reuse independent user indexes and canonical Task owner/policy per page."""
    first, state = _cursor(last_key, launch, since, until)
    try:
        activity = await run_in_threadpool(
            service.query_work_by_user,
            launch.user_id,
            tenant_id=launch.tenant_id,
            page_size=page_size,
            since=since,
            until=until,
            direct_cursor=state["direct"],
            descendant_cursor=state["descendants"],
            first_page=first,
        )
    except ValueError:
        raise HTTPException(400, detail={"error": "activity_cursor_invalid"}) from None
    items = list(activity.items)
    coverage = list(activity.coverage)
    if not first:
        coverage.append({"source": "pagination", "status": "partial", "reason": "continuation_only"})
    task_cursor = None
    if first or state["tasks"] is not None:
        if (
            os.environ.get("ADP_TASK_API_READ_ENABLED", "false").lower() != "true"
            or os.environ.get("ADP_TASK_API_HUMAN_ENABLED", "false").lower() != "true"
        ):
            coverage.append({"source": "tasks", "status": "unavailable", "reason": "not_enabled"})
        else:
            try:
                principal = "human:" + launch.user_id
                policy = await run_in_threadpool(TaskServicePolicyStore().get, tenant_id=launch.tenant_id, canonical_principal_id=principal)
                if not policy or policy.get("status") != "active" or "read" not in policy.get("task_scopes", []):
                    coverage.append({"source": "tasks", "status": "unavailable", "reason": "not_enrolled"})
                else:
                    store = task_readthrough.get_store()
                    caller = authz.Caller(principal, launch.tenant_id, frozenset({authz.SCOPE_READ}))
                    bindings, task_cursor = await run_in_threadpool(
                        store.list_owned,
                        tenant=launch.tenant_id,
                        principal=principal,
                        limit=min(page_size, 20),
                        after=state["tasks"],
                    )
                    coverage.append({"source": "tasks", "status": "available", "reason": "queried"})
                    for task_id in bindings:
                        try:
                            record = await run_in_threadpool(authz.authorize_task, caller, store, task_id)
                        except errors.TaskApiError as error:
                            if error.status in (403, 404):
                                coverage.append({"source": "tasks", "status": "partial", "reason": "record_not_readable"})
                                continue
                            raise
                        items.append(task_readthrough.detail(record, request))
            except (TaskServicePolicyError, TaskStoreError, BotoCoreError, ClientError, errors.TaskApiError):
                coverage.append({"source": "tasks", "status": "unavailable", "reason": "provider_failure"})
            except HTTPException as error:
                if error.status_code != 503:
                    raise
                coverage.append({"source": "tasks", "status": "unavailable", "reason": "provider_failure"})
    start, end = (datetime.fromisoformat(value.replace("Z", "+00:00")) for value in (since, until))
    observed_at = observed_at or datetime.now(UTC)
    retention_start = observed_at - timedelta(days=30)
    if start < retention_start:
        coverage.append(
            {
                "source": "activity_retention",
                "status": "partial",
                "reason": "window_before_retention",
                "from": since,
                "to": min(end, retention_start).isoformat().replace("+00:00", "Z"),
            }
        )
    external_events = []
    if first:
        external_read = await read_external_work(request, launch, start, end)
        external_events = [item for item in external_read.events if _within_window(item.event.occurred_at, start, end)]
        coverage.extend(external_read.coverage)
    else:
        coverage.append({"source": "external", "status": "partial", "reason": "continuation_only"})
    in_window = []
    for item in items:
        if _within_window(item.invoked_at, start, end):
            in_window.append(item)
            if not item.summary and not (item.task_snapshot or {}).get("result"):
                coverage.append({"source": "summary", "status": "partial", "reason": "missing", "invocation_id": item.invocation_id})
            if (item.source_type == "activity" and not item.transcript_key) or (item.source_type == "task" and item.transcript_status != "available"):
                coverage.append({"source": "transcript", "status": "partial", "reason": "missing_or_pending", "invocation_id": item.invocation_id})
        elif not item.invoked_at or not _valid_instant(item.invoked_at):
            coverage.append({"source": item.source_type, "status": "partial", "reason": "timestamp_invalid"})
    result = summarize_work(in_window, external_events).model_dump(mode="json")
    cursors = {"direct": activity.direct_cursor, "descendants": activity.descendant_cursor, "tasks": task_cursor}
    next_cursor = _encode_cursor({**cursors, "scope": [launch.user_id, launch.tenant_id, since, until]}) if any(cursors.values()) else None
    result["last_key"] = next_cursor
    result["from"] = since
    result["to"] = until
    result["timezone"] = timezone
    result["observed_at"] = observed_at.isoformat().replace("+00:00", "Z")
    result["coverage"] = coverage
    if any(entry["status"] != "available" for entry in coverage) or next_cursor:
        has_covered_source = any(entry["status"] in {"available", "partial"} for entry in coverage)
        result["status"] = "partial" if in_window or external_events or has_covered_source else "unavailable"
    else:
        result["status"] = "ok" if in_window or external_events else "empty"
    return result
