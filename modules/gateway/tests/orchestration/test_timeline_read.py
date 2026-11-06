"""Timings use immutable transitions, never mutable node timestamps."""

from datetime import timedelta
from unittest.mock import patch

import pytest

# Reuse the read API database and authorization fixtures.
# ruff: noqa: F811
from sqlalchemy import event

from src.orchestration.models import OrchestrationDecision
from src.orchestration.timeline_read import MAX_TRANSITIONS, load_flow_timeline
from tests.orchestration.test_execution_read import (
    NOW,
    ORG_A,
    ORG_B,
    app_with_router,  # noqa: F401 - pytest fixture
    client_for,
    seed_flow,
    seed_node,
    session,  # noqa: F401 - pytest fixture
)


async def transition(db, flow, node, target, minute, source="ready", rejected=False):
    db.add(
        OrchestrationDecision(
            org_id=flow.org_id,
            flow_id=flow.id,
            node_id=node.id,
            kind="transition",
            actor_id="fixture-engine",
            actor_role="engine",
            actor_kind="service",
            from_state=source,
            to_state=target,
            rejection_reason="refused" if rejected else None,
            created_at=NOW + timedelta(minutes=minute),
        )
    )
    await db.flush()


@pytest.mark.asyncio
async def test_history_boundaries_ignore_metadata_and_refused_transitions(session, app_with_router):
    flow = await seed_flow(session)
    node = await seed_node(session, flow, node_ref="story", state="passed")
    node.attempts = 2
    node.updated_at = NOW + timedelta(days=5)
    await transition(session, flow, node, "running", 0)
    await transition(session, flow, node, "failed", 5, "running")
    await transition(session, flow, node, "running", 10, "failed")
    await transition(session, flow, node, "passed", 30, "running")
    await transition(session, flow, node, "passed", 40, "running", rejected=True)
    response = client_for(app_with_router).get(f"/orchestration/flows/{flow.id}/timeline")
    assert response.status_code == 200, response.text
    row = response.json()["nodes"][0]
    assert row["started_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert row["finished_at"] == (NOW + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    assert row["attempts"] == 2
    assert len(row["transitions"]) == 4


@pytest.mark.asyncio
async def test_absent_history_reopened_story_and_non_story_controls(session):
    flow = await seed_flow(session)
    old = await seed_node(session, flow, node_ref="legacy", state="passed")
    restarted = await seed_node(session, flow, node_ref="reopened", state="running")
    await seed_node(session, flow, node_ref="gate", kind="gate")
    await seed_node(session, flow, node_ref="checkpoint", kind="eval")
    await seed_node(session, flow, node_ref="history", state="superseded")
    evaluation = await seed_node(session, flow, node_ref="eval-story", kind="eval")
    evaluation.issue_ref = "42"
    await transition(session, flow, restarted, "running", 0)
    await transition(session, flow, restarted, "passed", 10, "running")
    await transition(session, flow, restarted, "running", 20, "ready")
    view = await load_flow_timeline(session, org_id=ORG_A, flow_id=flow.id, limit=100, offset=0)
    rows = {row.node_id: row for row in view.nodes}
    assert view.total == 3
    assert rows[old.id].started_at is None and rows[old.id].finished_at is None
    assert rows[restarted.id].finished_at is None
    assert evaluation.id in rows


@pytest.mark.asyncio
async def test_bounded_per_story_history_keeps_original_start_and_paginates(session):
    flow = await seed_flow(session)
    busy = await seed_node(session, flow, node_ref="busy", state="running")
    quiet = await seed_node(session, flow, node_ref="quiet", state="running")
    for i in range(MAX_TRANSITIONS + 4):
        await transition(session, flow, busy, "running" if i % 2 == 0 else "ready", i, "ready" if i % 2 == 0 else "running")
    await transition(session, flow, quiet, "running", 0)
    reads = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        reads.append(statement)

    event.listen(session.bind.sync_engine, "before_cursor_execute", record)
    try:
        view = await load_flow_timeline(session, org_id=ORG_A, flow_id=flow.id, limit=100, offset=0)
    finally:
        event.remove(session.bind.sync_engine, "before_cursor_execute", record)
    rows = {row.node_id: row for row in view.nodes}
    assert len(reads) == 4
    assert len(rows[busy.id].transitions) == MAX_TRANSITIONS
    assert rows[busy.id].transitions_truncated is True
    assert rows[busy.id].started_at == NOW
    assert len(rows[quiet.id].transitions) == 1
    first = await load_flow_timeline(session, org_id=ORG_A, flow_id=flow.id, limit=1, offset=0)
    second = await load_flow_timeline(session, org_id=ORG_A, flow_id=flow.id, limit=1, offset=1)
    assert first.total == second.total == 2
    assert first.nodes[0].node_id != second.nodes[0].node_id


@pytest.mark.asyncio
async def test_tenant_permission_and_page_bounds(session, app_with_router):
    flow = await seed_flow(session, org_id=ORG_B)
    path = f"/orchestration/flows/{flow.id}/timeline"
    assert client_for(app_with_router).get(path).status_code == 404
    with patch("src.orchestration.routes.OrchestrationRepository.get_flow") as get_flow:
        assert client_for(app_with_router, permitted=False).get(path).status_code == 403
        get_flow.assert_not_called()
    assert client_for(app_with_router).get(path + "?limit=101").status_code == 422
    assert client_for(app_with_router).get(path + "?offset=-1").status_code == 422
