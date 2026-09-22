"""Regression cases from the live flows: stale stalls and blocked running nodes."""

# ruff: noqa: F811 -- imported pytest fixture is requested by the tests

from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest

from src.orchestration.display_state import FlowStatus
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationExecution, OrchestrationWorkClaim
from src.orchestration.progress_projection import CAPACITY_WAIT_NOTE
from src.orchestration.repository import OrchestrationRepository
from tests.orchestration.test_list_flows import ORG_A, ORG_B, seed_decision, seed_flow, seed_node, session  # noqa: F401


async def execution(session, node, **values):
    row = OrchestrationExecution(
        org_id=values.pop("org_id", node.org_id),
        flow_id=node.flow_id,
        node_id=node.id,
        cycle=values.pop("cycle", node.attempts),
        phase="awaiting_review",
        status=values.pop("status", "runnable"),
        accepted_plan_version=values.pop("accepted_plan_version", 0),
        claim_id="claim",
        claim_generation=1,
        **values,
    )
    session.add(row)
    await session.flush()
    return row


async def test_seven_running_stories_are_four_progressing_and_three_blocked(session):
    flow = await seed_flow(session, slug="security-agent", created_offset=0)
    for index in range(7):
        node = await seed_node(session, flow, node_ref=f"running-{index}", state="running")
        await execution(session, node, status="blocked" if index < 3 else "runnable", block_code="human_input_required" if index < 3 else None)
        await seed_decision(session, flow, node, kind="node_stalled")
    for index in range(4):
        node = await seed_node(session, flow, node_ref=f"done-{index}", state="passed")
        await seed_decision(session, flow, node, kind="node_stalled")
    await seed_node(session, flow, node_ref="gate", kind="gate", state="passed")
    await seed_node(session, flow, node_ref="eval", kind="eval")
    repo = OrchestrationRepository(session)
    summary = (await repo.list_flows_page_with_aggregates(org_id=ORG_A)).flows[0]
    expected = {"queued": 1, "in_progress": 4, "gate": 0, "stalled": 3, "complete": 5}
    assert asdict(summary.display_counts) == expected
    assert summary.stalled_count == 3
    assert summary.completed_story_count == 4
    assert summary.story_count == 11
    assert asdict(summary.waves[0].display_counts) == expected
    graph = await repo.node_display_states(org_id=ORG_A, flow_id=flow.id)
    assert {key: list(graph.values()).count(key) for key in expected} == expected
    assert summary.status == "attention_needed"
    assert (await repo.count_flows_by_status(org_id=ORG_A))[summary.status] == 1
    assert (await repo.list_flows_page_with_aggregates(org_id=ORG_A, status=FlowStatus.RUNNING)).total == 0


@pytest.mark.parametrize("state,expected", [("passed", "complete"), ("superseded", None), ("running", "in_progress"), ("ready", "queued")])
async def test_old_stalls_cannot_override_current_state(session, state, expected):
    flow = await seed_flow(session, slug="recovered", created_offset=0)
    node = await seed_node(session, flow, node_ref="story", state=state)
    await seed_decision(session, flow, node, kind="node_stalled")
    repo = OrchestrationRepository(session)
    summary = (await repo.list_flows_page_with_aggregates(org_id=ORG_A)).flows[0]
    assert summary.stalled_count == summary.display_counts.stalled == 0
    assert (await repo.node_display_states(org_id=ORG_A, flow_id=flow.id))[node.id] == expected
    assert (await repo.list_flows_page_with_aggregates(org_id=ORG_A, needs_me=True)).total == 0


async def test_capacity_wait_is_queued_and_does_not_need_attention(session):
    flow = await seed_flow(session, slug="capacity", created_offset=0)
    node = await seed_node(session, flow, node_ref="waiting", state="running")
    row = await execution(session, node, progress_note=CAPACITY_WAIT_NOTE)
    repo = OrchestrationRepository(session)
    summary = (await repo.list_flows_page_with_aggregates(org_id=ORG_A)).flows[0]
    assert asdict(summary.display_counts) == {"queued": 1, "in_progress": 0, "gate": 0, "stalled": 0, "complete": 0}
    assert summary.status == "queued"
    row.status, row.block_code = "blocked", "attempts_exhausted"
    await session.flush()
    assert (await repo.node_display_states(org_id=ORG_A, flow_id=flow.id))[node.id] == "stalled"


@pytest.mark.parametrize("mismatch", ["cycle", "tenant", "plan", "terminal"])
async def test_historical_or_foreign_execution_cannot_add_a_stall(session, mismatch):
    flow = await seed_flow(session, slug="fenced", created_offset=0)
    node = await seed_node(session, flow, node_ref="story", state="running")
    options = {"status": "blocked", "block_code": "human_input_required"}
    if mismatch == "cycle":
        options["cycle"] = node.attempts + 1
    if mismatch == "tenant":
        options["org_id"] = ORG_B
    if mismatch == "plan":
        session.add(OrchestrationAcceptedPlan(org_id=ORG_A, flow_id=flow.id, version=2, plan_document={}, plan_hash="a" * 64))
        options["accepted_plan_version"] = 1
    if mismatch == "terminal":
        options["status"] = "concluded"
    await execution(session, node, **options)
    repo = OrchestrationRepository(session)
    assert (await repo.node_display_states(org_id=ORG_A, flow_id=flow.id))[node.id] == "in_progress"


async def test_ready_work_with_a_lapsed_current_claim_needs_attention(session):
    flow = await seed_flow(session, slug="lapsed", created_offset=0)
    node = await seed_node(session, flow, node_ref="story", state="ready")
    row = await execution(session, node)
    claim = OrchestrationWorkClaim(
        id=row.claim_id,
        org_id=ORG_A,
        provider_repository_id=42,
        issue_number=5603,
        owner_kind="flow",
        owner_ref=flow.id,
        state="held",
        generation=1,
        lease_expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    session.add(claim)
    await session.flush()
    repo = OrchestrationRepository(session)
    assert (await repo.node_display_states(org_id=ORG_A, flow_id=flow.id))[node.id] == "stalled"
    claim.generation = 2
    await session.flush()
    assert (await repo.node_display_states(org_id=ORG_A, flow_id=flow.id))[node.id] == "queued"
