"""Real PostgreSQL archive/dispatch serialization, not SQLite lock simulation."""

import asyncio
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.orchestration import dispatch_pass, flow_controls
from src.orchestration.execution_state import ExecutionPhase, ExecutionStatus
from src.orchestration.models import OrchestrationDecision, OrchestrationExecution, OrchestrationFlow, OrchestrationNode
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401
from tests.orchestration import test_flow_archive_postgres as fixtures
from tests.orchestration.test_controls import ORG_A, seed_flow

session, controls = fixtures.session, fixtures.controls
pytestmark = pytest.mark.integration


async def test_reserved_execution_prevents_archive_after_flow_lock_released(session, controls):
    flow = await seed_flow(session)
    node = OrchestrationNode(
        org_id=ORG_A,
        flow_id=flow.id,
        epic_ref="epic",
        wave_ref="wave",
        node_ref="story",
        kind="story",
        title="Post-merge deployment",
        state="passed",
    )
    session.add(node)
    await session.commit()
    entered_permission = asyncio.Event()
    controls[2].check_permission.side_effect = lambda *args, **kwargs: entered_permission.set()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)

    async with factory() as writer:
        await writer.scalar(select(OrchestrationFlow).where(OrchestrationFlow.id == flow.id).with_for_update())
        writer.add(
            OrchestrationExecution(
                org_id=ORG_A,
                flow_id=flow.id,
                node_id=node.id,
                cycle=1,
                phase=ExecutionPhase.DEPLOYMENT_PENDING.value,
                status=ExecutionStatus.RUNNABLE.value,
                accepted_plan_version=1,
                claim_id="deployment",
                claim_generation=1,
            )
        )
        await writer.flush()
        archive = asyncio.create_task(controls[0].post(f"/orchestration/flows/{flow.id}/archive", json={"reason": "Deployment was completed"}))
        try:
            await asyncio.wait_for(entered_permission.wait(), timeout=5)
            assert not archive.done()
        finally:
            await writer.commit()
        response = await asyncio.wait_for(archive, timeout=5)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "flow_has_active_work"
    await session.refresh(flow)
    assert flow.archived_at is None
    assert list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.flow_id == flow.id))) == []


async def test_archive_wins_before_stale_dispatch_reservation(session, controls, monkeypatch):
    flow = await seed_flow(session)
    flow.execution_paused = False
    node = OrchestrationNode(
        org_id=ORG_A,
        flow_id=flow.id,
        epic_ref="epic",
        wave_ref="wave",
        node_ref="story",
        kind="story",
        title="Ready to dispatch",
        state="ready",
    )
    session.add(node)
    await session.commit()
    flow_locked = asyncio.Event()
    resume_archive = asyncio.Event()
    dispatch_started = asyncio.Event()
    original_check = flow_controls._has_active_flow_work

    async def hold_archive(db, *, org_id, flow_id):
        flow_locked.set()
        await asyncio.wait_for(resume_archive.wait(), timeout=5)
        return await original_check(db, org_id=org_id, flow_id=flow_id)

    monkeypatch.setattr(flow_controls, "_has_active_flow_work", hold_archive)
    factory = async_sessionmaker(session.bind, expire_on_commit=False)

    async def dispatch_stale_candidate():
        async with factory() as writer:
            candidate = await writer.get(OrchestrationNode, node.id)
            report = dispatch_pass.DispatchPassReport()
            dispatch_started.set()
            await dispatch_pass._dispatch_one_unclaimed(
                writer,
                candidate,
                config=dispatch_pass.DispatchPassConfig(queue_url="", repo=""),
                report=report,
            )
            await writer.commit()
            return report

    archive = asyncio.create_task(controls[0].post(f"/orchestration/flows/{flow.id}/archive", json={"reason": "No active work remains"}))
    try:
        await asyncio.wait_for(flow_locked.wait(), timeout=5)
        dispatch = asyncio.create_task(dispatch_stale_candidate())
        await asyncio.wait_for(dispatch_started.wait(), timeout=5)
    finally:
        resume_archive.set()
    response = await asyncio.wait_for(archive, timeout=5)
    report = await asyncio.wait_for(dispatch, timeout=5)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "archived"
    assert datetime.fromisoformat(response.json()["archived_at"]).tzinfo is not None
    assert report.pending == []
    async with factory() as reader:
        stored_node = await reader.get(OrchestrationNode, node.id)
        decisions = list(await reader.scalars(select(OrchestrationDecision).where(OrchestrationDecision.flow_id == flow.id)))
        assert stored_node.state == "ready"
        assert [(decision.kind, decision.actor_kind) for decision in decisions] == [("flow_archived", "human")]
        assert (await reader.get(OrchestrationFlow, flow.id)).archived_at is not None
