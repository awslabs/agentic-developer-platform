"""Real row-lock races at paused future-wave acceptance."""

import asyncio

import pytest
from sqlalchemy import select

from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationFlow, OrchestrationNode
from src.orchestration.shared_amendment import SharedAppendError
from src.orchestration.wave_amendment import accept_wave_dependencies
from tests.orchestration.test_shared_amendment_postgres import pg_server, pg_url, session  # noqa: F401
from tests.orchestration.test_wave_amendment import address, appendable, approval, paused, preview  # noqa: F401


@pytest.mark.parametrize("change", ["resume", "dispatch"])
async def test_accept_refreshes_after_waiting_for_flow_lock(session, paused, change):  # noqa: F811
    ctx = paused
    expected = await preview(session, ctx)
    request = ctx.request.model_copy(update={"expected_snapshot": expected["snapshot"]})
    await session.commit()
    async with session.info["factory"]() as other:
        flow = await other.scalar(select(OrchestrationFlow).where(OrchestrationFlow.id == ctx.flow.id).with_for_update())
        node = await other.get(OrchestrationNode, ctx.nodes[address("gate", wave="wave-2")])
        task = asyncio.create_task(accept_wave_dependencies(session, flow_id=ctx.flow.id, actor=ctx.actor, request=request))
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
            if change == "resume":
                flow.execution_paused = False
            else:
                node.state, node.attempts = "running", 1
            await other.commit()
            with pytest.raises(SharedAppendError, match="flow_must_be_paused" if change == "resume" else "started_wave_is_immutable"):
                await asyncio.wait_for(task, timeout=2)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert len(list(await session.scalars(select(OrchestrationAcceptedPlan)))) == 1


async def test_node_lock_contention_yields_and_releases_flow_lock(session, paused):  # noqa: F811
    ctx = paused
    expected = await preview(session, ctx)
    request = ctx.request.model_copy(update={"expected_snapshot": expected["snapshot"]})
    await session.commit()
    async with session.info["factory"]() as worker:
        await worker.scalar(select(OrchestrationNode).where(OrchestrationNode.id == ctx.nodes[address("story-a")]).with_for_update())
        with pytest.raises(SharedAppendError, match="amendment_dispatch_in_progress"):
            await asyncio.wait_for(accept_wave_dependencies(session, flow_id=ctx.flow.id, actor=ctx.actor, request=request), timeout=2)
        # The savepoint itself must release locks; no request rollback required.
        await asyncio.wait_for(worker.scalar(select(OrchestrationFlow).where(OrchestrationFlow.id == ctx.flow.id).with_for_update()), timeout=2)
    assert len(list(await session.scalars(select(OrchestrationAcceptedPlan)))) == 1
