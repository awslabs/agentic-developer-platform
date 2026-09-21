"""Real PostgreSQL races preserve shared plans across dispatch and resubmission."""

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.orchestration.amend import AmendmentContext, amend_plan
from src.orchestration.compile import ProposalRejectedError, compile_proposal
from src.orchestration.continuation import digest
from src.orchestration.dispatch_pass import _fetch_ready_nodes
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
from src.orchestration.shared_policy import shared_inputs
from src.shared.models.base import Base
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401
from tests.orchestration.test_amend import address, approval, valid_proposal  # noqa: F401
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


async def test_worker_committing_while_append_waits_refreshes_active_node_state(session, appendable):  # noqa: F811
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


async def test_append_yields_to_production_dispatch_node_then_flow_lock_order(session, appendable):  # noqa: F811
    ctx = appendable
    ready = await session.get(OrchestrationNode, ctx.nodes[address("story-a")])
    ready.state = "ready"
    await session.commit()
    expected = await preview(session, ctx)
    request = ctx.request.model_copy(update={"expected_snapshot": expected["snapshot"]})
    await session.commit()
    async with session.info["factory"]() as dispatcher:
        # Use the actual scheduler query: it takes READY node locks BEFORE
        # authorize_shared_dispatch calls shared_inputs(lock=True).
        fetched = await _fetch_ready_nodes(dispatcher, limit=10)
        assert [node.id for node in fetched] == [ready.id]
        with pytest.raises(SharedAppendError, match="amendment_dispatch_in_progress"):
            await asyncio.wait_for(accept_shared_append(session, flow_id=ctx.flow.id, actor=ctx.actor, request=request), timeout=2)
        # Do not roll back the request session: the failed append's savepoint
        # must have released its flow/plan locks without relying on route cleanup.
        inputs, marker = await asyncio.wait_for(shared_inputs(dispatcher, org_id=ctx.actor.org_id, flow_id=ctx.flow.id, lock=True), timeout=2)
        assert inputs.plan_version == ctx.plan.version
        assert marker == ctx.original["execution_continuation"]
        fetched[0].state, fetched[0].attempts = "running", 1
        await dispatcher.commit()
    plans = list(await session.scalars(select(OrchestrationAcceptedPlan)))
    assert len(plans) == 1 and plans[0].plan_document == ctx.original
    assert await session.scalar(select(OrchestrationNode).where(OrchestrationNode.node_ref == "producer")) is None
    current = await session.get(OrchestrationNode, ready.id, populate_existing=True)
    assert current.state == "running" and current.attempts == 1


@pytest.mark.parametrize("ingress", ["compile", "amend"])
async def test_resubmission_rechecks_continuation_after_flow_lock_wait(session, appendable, ingress):  # noqa: F811
    ctx = appendable
    # Keep a cached plan without a continuation marker in the request session.
    legacy = dict(ctx.original)
    legacy.pop("execution_continuation")
    legacy["execution_policy"] = None
    ctx.plan.plan_document, ctx.plan.plan_hash = legacy, digest(legacy)
    await session.commit()
    async with session.info["factory"]() as accepter:
        await accepter.scalar(select(OrchestrationFlow).where(OrchestrationFlow.id == ctx.flow.id).with_for_update())
        current = await accepter.get(OrchestrationAcceptedPlan, ctx.plan.id)
        current.plan_document, current.plan_hash = ctx.original, digest(ctx.original)
        await accepter.flush()
        operation = (
            compile_proposal(session, valid_proposal(), ctx.actor)
            if ingress == "compile"
            else amend_plan(
                session,
                ctx.flow.id,
                valid_proposal(),
                AmendmentContext(org_id=ctx.actor.org_id, actor_id=ctx.actor.actor_id, actor_role=ctx.actor.actor_role),
            )
        )
        task = asyncio.create_task(operation)
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
            await accepter.commit()
            with pytest.raises(ProposalRejectedError, match="bounded append preview/accept"):
                await asyncio.wait_for(task, timeout=2)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    plans = list(await session.scalars(select(OrchestrationAcceptedPlan)))
    assert len(plans) == 1 and plans[0].plan_document == ctx.original
