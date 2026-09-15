"""Read-side story stages from the current dispatch's invocation chain.

Worker completion leaves a story awaiting merge while reviewers and repair runs
continue in the same chain. This is display information, never an engine state
transition or evidence that code has been approved or merged.
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Literal

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from src.activity.liveness import OBSERVED_TERMINAL_STATUSES, LivenessVerdict
from src.activity.schemas import InvocationChainResponse
from src.activity.service import ActivityService

logger = logging.getLogger("bedrockgateway.orchestration")


class NodeActivity(BaseModel):
    invocation_id: str
    persona: Literal["developer", "reviewer"]
    status: str
    liveness: LivenessVerdict


class StoryRun(NodeActivity):
    invoked_at: str


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
        if item.persona not in ("developer", "reviewer"):
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
                    invoked_at=item.invoked_at,
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


def _read_execution(org_id: str, run_id: str) -> StoryExecution:
    try:
        # Dispatch sets correlation_id = run_id. The caller supplies only the
        # committed current-attempt dispatch, never a browser-provided chain ID.
        # Each thread owns its boto3 resource; resources are not thread-safe.
        chain = _activity_service().get_chain(correlation_id=run_id, tenant_id=org_id)
        return story_execution(chain)
    except (BotoCoreError, ClientError):
        logger.warning("Story activity unavailable", extra={"org_id": org_id, "run_id": run_id}, exc_info=True)
        return StoryExecution()


async def load_story_execution(*, org_id: str, run_ids: list[str]) -> dict[str, StoryExecution]:
    # Avoid blocking the async graph route on DynamoDB. Only dispatched stories
    # are enriched, with bounded concurrency and one query per distinct attempt.
    semaphore = asyncio.Semaphore(4)

    async def read(run_id: str):
        async with semaphore:
            return run_id, await run_in_threadpool(_read_execution, org_id, run_id)

    return dict(await asyncio.gather(*(read(run_id) for run_id in dict.fromkeys(run_ids))))


async def load_story_activity(*, org_id: str, run_ids: list[str]) -> dict[str, NodeActivity | None]:
    executions = await load_story_execution(org_id=org_id, run_ids=run_ids)
    return {run_id: execution.activity for run_id, execution in executions.items()}
