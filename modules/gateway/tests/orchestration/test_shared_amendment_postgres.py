"""A real dispatch winning the flow lock makes append recheck and refuse."""

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationAmendmentRequest,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
    OrchestrationWorkClaim,
)
from src.orchestration.run_reports import OrchestrationRunReport
from src.orchestration.shared_amendment import SharedAppendError, accept_shared_append
from src.shared.models.base import Base
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401
from tests.orchestration.test_amend import address, approval  # noqa: F401
from tests.orchestration.test_shared_amendment import appendable, preview  # noqa: F401


@pytest.fixture
async def session(pg_url):  # noqa: F811
    engine = create_async_engine(to_async_url(pg_url))
    models = [
        OrchestrationFlow,
        OrchestrationNode,
        OrchestrationEdge,
        OrchestrationAcceptedPlan,
        OrchestrationDecision,
        OrchestrationWorkClaim,
        OrchestrationExecution,
        OrchestrationAction,
        OrchestrationRunReport,
        OrchestrationAmendmentRequest,
        OrchestrationPullRequestBinding,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda c: Base.metadata.create_all(c, tables=[m.__table__ for m in models]))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.info["factory"] = factory
        yield db
    await engine.dispose()


async def test_dispatch_committing_while_append_waits_cannot_cross_version_boundary(session, appendable):  # noqa: F811
    ctx = appendable
    expected = await preview(session, ctx)
    request = ctx.request.model_copy(update={"expected_snapshot": expected["snapshot"]})
    stale = await session.get(OrchestrationNode, ctx.nodes[address("story-a")])
    assert stale.state == "pending"
    await session.commit()
    async with session.info["factory"]() as dispatcher:
        await dispatcher.scalar(select(OrchestrationFlow).where(OrchestrationFlow.id == ctx.flow.id).with_for_update())
        running = await dispatcher.scalar(select(OrchestrationNode).where(OrchestrationNode.id == stale.id).with_for_update())
        task = asyncio.create_task(accept_shared_append(session, flow_id=ctx.flow.id, actor=ctx.actor, request=request))
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
            running.state, running.attempts = "running", 1
            await dispatcher.commit()
            with pytest.raises(SharedAppendError, match="active_nodes_cross_plan_boundary"):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert len(list(await session.scalars(select(OrchestrationAcceptedPlan)))) == 1
    current = await session.get(OrchestrationNode, ctx.nodes[address("story-a")], populate_existing=True)
    assert current.state == "running" and current.attempts == 1
