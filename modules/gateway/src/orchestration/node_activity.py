"""Read-side story stages from the current dispatch's invocation chain.

Worker completion leaves a story awaiting merge while reviewers and repair runs
continue in the same chain. This is display information, never an engine state
transition or evidence that code has been approved or merged.
"""

import asyncio
import logging
import os
import time
from datetime import datetime
from threading import Event, local
from typing import Literal

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from src.activity.liveness import OBSERVED_TERMINAL_STATUSES, LivenessVerdict
from src.activity.schemas import InvocationChainResponse
from src.activity.service import ActivityService
from src.shared.model_admission import RuntimeBudgetWait, runtime_budget_wait

from .continuation_activity import ContinuationActivity

logger = logging.getLogger("bedrockgateway.orchestration")


class NodeActivity(BaseModel):
    invocation_id: str
    persona: Literal["developer", "agent-codex-developer", "reviewer", "agent-codex-reviewer"]
    status: str
    liveness: LivenessVerdict
    runtime_budget_wait: RuntimeBudgetWait | None = None


class StoryRun(NodeActivity):
    invoked_at: str
    stop_reason: str | None = None


class StoryExecution(BaseModel):
    """Current-attempt observations, never review verdicts or engine transitions."""

    run_id: str | None = None
    activity: NodeActivity | None = None
    runs: list[StoryRun] = Field(default_factory=list)
    history_complete: bool = False


def story_execution(chain: InvocationChainResponse) -> StoryExecution:
    pending = list(chain.items)
    seen = set()
    runs = []
    while pending:
        item = pending.pop()
        if item.invocation_id in seen:
            continue
        seen.add(item.invocation_id)
        pending.extend(item.children)
        if item.persona not in ("developer", "agent-codex-developer", "reviewer", "agent-codex-reviewer"):
            continue
        try:
            timestamp = datetime.fromisoformat(item.invoked_at.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                return StoryExecution(run_id=chain.correlation_id)
        except ValueError:
            return StoryExecution(run_id=chain.correlation_id)
        runs.append(
            (
                timestamp,
                StoryRun(
                    invocation_id=item.invocation_id,
                    persona=item.persona,
                    status=item.status or "unknown",
                    liveness=item.liveness or "unverifiable",
                    runtime_budget_wait=runtime_budget_wait(
                        status=item.model_admission_status,
                        updated_at=item.model_admission_updated_at,
                        run_status=item.status,
                        liveness=item.liveness,
                    ),
                    invoked_at=item.invoked_at,
                    stop_reason=item.stop_reason,
                ),
            )
        )
    runs.sort(key=lambda entry: (entry[0], entry[1].invocation_id))
    execution = StoryExecution(
        run_id=chain.correlation_id,
        runs=[entry[1] for entry in runs],
        history_complete=not chain.depth_capped and bool(runs),
    )
    # Truncated history can preserve observed work but cannot establish what is
    # happening now. Choose the latest run BEFORE checking status, so an older
    # unfinalized run never resurfaces after a newer review or repair finishes.
    if execution.history_complete:
        latest = execution.runs[-1]
        if latest.status not in OBSERVED_TERMINAL_STATUSES:
            execution.activity = NodeActivity(**latest.model_dump(include=set(NodeActivity.model_fields)))
    return execution


def current_activity(chain: InvocationChainResponse) -> NodeActivity | None:
    return story_execution(chain).activity


def _activity_service() -> ActivityService:
    # Optional stage detail must not hold the graph behind SDK minute-long
    # network timeouts. Give each worker its own session and resource as well.
    resource = boto3.session.Session().resource(
        "dynamodb",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1}),
    )
    return ActivityService(dynamodb_resource=resource)


def _with_continuation(service, execution: StoryExecution, org_id: str, binding: ContinuationActivity | None) -> StoryExecution:
    if binding is None:
        return execution
    # A committed assignment is independently current, even when recovery kept
    # a different correlation. Do not resurrect the old worker if this read fails.
    result = execution.model_copy(deep=True)
    result.activity = None
    result.history_complete = False
    try:
        member = service.get_chain_member(binding.invocation_id, tenant_id=org_id, correlation_id=binding.correlation_id)
    except (BotoCoreError, ClientError, ValueError, TypeError):
        logger.warning("Current continuation activity unavailable", extra={"org_id": org_id, "run_id": binding.invocation_id})
        return result
    if member is None or member.invocation_id != binding.invocation_id or member.persona != binding.persona:
        return result
    observed = story_execution(InvocationChainResponse(correlation_id=execution.run_id or "", items=[member], total_count=1))
    if not observed.history_complete:
        return result
    result.runs = [run for run in result.runs if run.invocation_id != binding.invocation_id] + observed.runs
    result.runs.sort(key=lambda run: (datetime.fromisoformat(run.invoked_at.replace("Z", "+00:00")), run.invocation_id))
    result.activity = observed.activity
    # Exact current activity does not claim we read every earlier continuation.
    return result


def _read_execution(org_id: str, run_id: str, continuation: ContinuationActivity | None = None) -> StoryExecution:
    try:
        # Dispatch sets correlation_id = run_id. The caller supplies only the
        # committed current-attempt dispatch, never a browser-provided chain ID.
        # Each thread owns its boto3 resource; resources are not thread-safe.
        service = _activity_service()
        chain = service.get_chain(correlation_id=run_id, tenant_id=org_id)
        return _with_continuation(service, story_execution(chain), org_id, continuation)
    except (BotoCoreError, ClientError):
        logger.warning("Story activity unavailable", extra={"org_id": org_id, "run_id": run_id}, exc_info=True)
        return StoryExecution()


async def load_story_execution(
    *, org_id: str, run_ids: list[str], continuations: dict[str, ContinuationActivity] | None = None
) -> dict[str, StoryExecution]:
    # Avoid blocking the async graph route on DynamoDB. Only dispatched stories
    # are enriched, with bounded concurrency and one query per distinct attempt.
    semaphore = asyncio.Semaphore(4)

    async def read(run_id: str):
        async with semaphore:
            return run_id, await run_in_threadpool(_read_execution, org_id, run_id, (continuations or {}).get(run_id))

    return dict(await asyncio.gather(*(read(run_id) for run_id in dict.fromkeys(run_ids))))


_bounded_services = local()


def _bounded_activity_service() -> ActivityService:
    service = getattr(_bounded_services, "service", None)
    if service is None:
        resource = boto3.session.Session().resource(
            "dynamodb",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
            config=Config(connect_timeout=1, read_timeout=1, retries={"total_max_attempts": 1}),
        )
        service = ActivityService(dynamodb_resource=resource)
        _bounded_services.service = service
    return service


def _read_bounded_execution(
    org_id: str, run_id: str, deadline: float, cancelled: Event, continuation: ContinuationActivity | None = None
) -> StoryExecution | None:
    if cancelled.is_set() or time.monotonic() + 2 >= deadline:
        return None
    try:
        service = _bounded_activity_service()
        chain = service.get_chain_bounded(run_id, tenant_id=org_id, deadline=deadline, cancelled=cancelled.is_set)
        if cancelled.is_set() or time.monotonic() >= deadline or chain.correlation_id != run_id:
            return None
        if continuation is not None and time.monotonic() + 2 >= deadline:
            return None
        result = _with_continuation(service, story_execution(chain), org_id, continuation)
        return None if cancelled.is_set() or time.monotonic() >= deadline else result
    except Exception:
        logger.warning("Bounded story activity unavailable", extra={"org_id": org_id, "run_id": run_id}, exc_info=True)
        return None


async def load_bounded_story_execution(
    *, org_id: str, run_ids: list[str], deadline: float, continuations: dict[str, ContinuationActivity] | None = None
) -> dict[str, StoryExecution | None]:
    from .activity_reads import shared_activity_reader

    return await shared_activity_reader().get_many(org_id=org_id, run_ids=run_ids, continuations=continuations or {}, deadline=deadline)


async def load_story_activity(*, org_id: str, run_ids: list[str]) -> dict[str, NodeActivity | None]:
    executions = await load_story_execution(org_id=org_id, run_ids=run_ids)
    return {run_id: execution.activity for run_id, execution in executions.items()}
