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
from pydantic import BaseModel
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


def current_activity(chain: InvocationChainResponse) -> NodeActivity | None:
    # A truncated chain contains the oldest runs, so it cannot establish the
    # current stage. In particular, never resurrect an earlier reviewer.
    if chain.depth_capped:
        return None
    candidates = []
    pending = list(chain.items)
    seen = set()
    while pending:
        item = pending.pop()
        if item.invocation_id in seen:
            continue
        seen.add(item.invocation_id)
        pending.extend(item.children)
        if item.persona in ("developer", "reviewer"):
            try:
                timestamp = datetime.fromisoformat(item.invoked_at.replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    return None
            except ValueError:
                return None
            candidates.append((timestamp, item.invocation_id, item))
    if not candidates:
        return None
    latest = max(candidates, key=lambda candidate: candidate[:2])[2]
    # Choose the latest run BEFORE checking status: an older unfinalized run
    # must not appear active after a newer review or repair has finished.
    if latest.status in OBSERVED_TERMINAL_STATUSES:
        return None
    return NodeActivity(
        invocation_id=latest.invocation_id,
        persona=latest.persona,
        status=latest.status or "unknown",
        liveness=latest.liveness or "unverifiable",
    )


def _activity_service() -> ActivityService:
    # Optional stage detail must not hold the graph behind SDK minute-long
    # network timeouts. Give each worker its own session and resource as well.
    resource = boto3.session.Session().resource(
        "dynamodb",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1}),
    )
    return ActivityService(dynamodb_resource=resource)


def _read_activity(org_id: str, run_id: str) -> NodeActivity | None:
    try:
        # Dispatch sets correlation_id = run_id. The caller supplies only the
        # committed current-attempt dispatch, never a browser-provided chain ID.
        # Each thread owns its boto3 resource; resources are not thread-safe.
        chain = _activity_service().get_chain(correlation_id=run_id, tenant_id=org_id)
        return current_activity(chain)
    except (BotoCoreError, ClientError):
        logger.warning("Story activity unavailable", extra={"org_id": org_id, "run_id": run_id}, exc_info=True)
        return None


async def load_story_activity(*, org_id: str, run_ids: list[str]) -> dict[str, NodeActivity | None]:
    # Avoid blocking the async graph route on DynamoDB. Only executing stories
    # are enriched, with bounded concurrency and one query per distinct attempt.
    semaphore = asyncio.Semaphore(4)

    async def read(run_id: str):
        async with semaphore:
            return run_id, await run_in_threadpool(_read_activity, org_id, run_id)

    return dict(await asyncio.gather(*(read(run_id) for run_id in dict.fromkeys(run_ids))))
