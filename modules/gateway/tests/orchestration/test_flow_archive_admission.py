"""Archived flows cannot enter engine admission even from a stale candidate."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.orchestration import continuation, dispatch_pass, stall, tick
from src.orchestration.execution_state import ExecutionIdentity, OutcomeKind
from src.orchestration.execution_store import create_execution
from src.orchestration.flow_execution import flow_is_paused
from src.orchestration.models import OrchestrationDecision, OrchestrationExecution, OrchestrationFlow, OrchestrationNode
from tests.orchestration import test_execution_store as fixtures

engine, session_factory, session, graph = fixtures.engine, fixtures.session_factory, fixtures.session, fixtures.graph


@pytest.mark.parametrize(
    "selector,state,kind",
    [
        ("tick", "pending", "story"),
        ("tick", "ready", "eval"),
        ("tick", "awaiting_gate", "eval"),
        ("dispatch", "ready", "story"),
        ("stall", "running", "story"),
    ],
)
async def test_archived_flow_is_not_selected_for_new_work(session, graph, selector, state, kind):
    archived = await session.get(OrchestrationFlow, graph["flow_a"])
    archived.archived_at = datetime.now(UTC)
    for node_id in (graph["node_a1"], graph["node_b1"]):
        node = await session.get(OrchestrationNode, node_id)
        node.state = state
        node.kind = kind
    await session.commit()

    if selector == "tick":
        candidates = await tick._fetch_candidate_page(session, after_id=None, limit=20)
    elif selector == "dispatch":
        candidates = await dispatch_pass._fetch_ready_nodes(session, limit=20)
    else:
        candidates = await stall._fetch_candidate_page(session, after_id=None, limit=20)
    expected = {graph["node_b1"], graph["node_b2"]} if selector == "tick" else {graph["node_b1"]}
    assert {getattr(candidate, "node_id", None) or candidate.id for candidate in candidates} == expected


@pytest.mark.parametrize("kind,state", [("story", "pending"), ("eval", "ready"), ("eval", "awaiting_gate")])
async def test_stale_tick_candidate_cannot_admit_archived_work(session, graph, kind, state):
    node = await session.get(OrchestrationNode, graph["node_a1"])
    node.kind = kind
    node.state = state
    flow = await session.get(OrchestrationFlow, graph["flow_a"])
    flow.archived_at = datetime.now(UTC)
    await session.commit()
    candidate = tick._Candidate(node_id=node.id, org_id=node.org_id, flow_id=node.flow_id, observed_state=node.state, kind=node.kind)
    report = tick.TickReport()

    await tick._advance_node(session, candidate, report)
    await session.commit()

    await session.refresh(node)
    assert node.state == state
    assert report.blocked[node.id] == ["flow_archived"]
    assert list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.flow_id == flow.id))) == []


async def test_stale_dispatch_candidate_cannot_admit_archived_work(session, graph):
    node = await session.get(OrchestrationNode, graph["node_a1"])
    node.state = "ready"
    flow = await session.get(OrchestrationFlow, graph["flow_a"])
    flow.archived_at = datetime.now(UTC)
    await session.commit()
    report = dispatch_pass.DispatchPassReport()

    await dispatch_pass._dispatch_one_unclaimed(
        session,
        node,
        config=dispatch_pass.DispatchPassConfig(queue_url="", repo=""),
        report=report,
    )
    await session.commit()

    await session.refresh(node)
    assert node.state == "ready"
    assert report.pending == []
    assert list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.flow_id == flow.id))) == []


async def test_archived_flow_refuses_execution_reservation(session, graph):
    flow = await session.get(OrchestrationFlow, graph["flow_a"])
    flow.archived_at = datetime.now(UTC)
    await session.commit()
    identity = ExecutionIdentity(
        org_id=flow.org_id,
        node_id=graph["node_a1"],
        cycle=1,
        accepted_plan_version=fixtures.PLAN_VERSION,
        claim_id=fixtures.CLAIM,
        claim_generation=1,
    )

    result = await create_execution(session, identity=identity, flow_id=flow.id)
    await session.commit()

    assert result.kind is OutcomeKind.CONFLICT
    assert result.reason == "flow_archived"
    assert list(await session.scalars(select(OrchestrationExecution).where(OrchestrationExecution.flow_id == flow.id))) == []


async def test_archived_flow_refuses_locked_continuation(session, graph):
    flow = await session.get(OrchestrationFlow, graph["flow_a"])
    flow.archived_at = datetime.now(UTC)
    await session.commit()

    with pytest.raises(continuation.ContinuationRefusedError, match="Unarchive this flow") as error:
        await continuation._snapshot(session, flow.org_id, flow.id, lock=True)
    assert error.value.code == "flow_archived"


async def test_archived_flow_blocks_recovery_despite_resume_flag(session, graph):
    flow = await session.get(OrchestrationFlow, graph["flow_a"])
    assert flow.execution_paused is False
    flow.archived_at = datetime.now(UTC)
    await session.commit()

    assert await flow_is_paused(session, org_id=flow.org_id, flow_id=flow.id, lock=True)
    assert await flow_is_paused(session, org_id=flow.org_id, flow_id=flow.id)
    await session.refresh(flow)
    assert flow.execution_paused is False
    assert list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.flow_id == flow.id))) == []
