"""Retry approvals serialize against other approvals and graph changes."""

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import select

from src.orchestration.models import OrchestrationDecision, OrchestrationFlow
from src.orchestration.shared_retry import RetryIncreaseError, RetryIncreaseRequest, accept_retry_increase, preview_retry_increase
from tests.orchestration.test_shared_amendment_postgres import (  # noqa: F401
    appendable,
    approval,
    pg_server,
    pg_url,
    session,
)


@pytest.mark.parametrize("competing", ["approval", "graph"])
async def test_accept_rechecks_after_waiting_for_current_flow_lock(session, appendable, monkeypatch, competing):  # noqa: F811
    ctx = appendable
    actor = replace(ctx.actor, actor_role="platform_admin")
    request = RetryIncreaseRequest(
        expected_plan_version=ctx.plan.version,
        expected_plan_hash=ctx.plan.plan_hash,
        max_attempts_per_node=20,
        reason="Platform owner approves this flow's retry increase.",
    )
    preview = await preview_retry_increase(session, flow_id=ctx.flow.id, actor=actor, request=request)
    request = request.model_copy(update={"expected_snapshot": preview["snapshot"]})
    await session.commit()
    async with session.info["factory"]() as writer:
        await writer.scalar(select(OrchestrationFlow).where(OrchestrationFlow.id == ctx.flow.id).with_for_update())
        if competing == "approval":
            smaller = request.model_copy(update={"max_attempts_per_node": 10})
            preview = await preview_retry_increase(writer, flow_id=ctx.flow.id, actor=actor, request=smaller)
            smaller = smaller.model_copy(update={"expected_snapshot": preview["snapshot"]})
            await accept_retry_increase(writer, flow_id=ctx.flow.id, actor=actor, request=smaller)
        else:
            plan = await writer.get(type(ctx.plan), ctx.plan.id)
            plan.version += 1
            await writer.flush()
        task = asyncio.create_task(accept_retry_increase(session, flow_id=ctx.flow.id, actor=actor, request=request))
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
            await writer.commit()
            with pytest.raises(RetryIncreaseError, match="retry_preview_changed" if competing == "approval" else "accepted_plan_changed"):
                await asyncio.wait_for(task, timeout=3)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    receipts = list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "retry_limit_increased")))
    assert len(receipts) == (1 if competing == "approval" else 0)
