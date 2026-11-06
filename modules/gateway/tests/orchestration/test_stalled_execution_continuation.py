"""A story the watchdog interrupted re-enters its live execution; a blocked execution has no worker clock (#7467)."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.orchestration import stall
from src.orchestration.dispatch_pass import DispatchPassReport, _continue_stalled_execution
from src.orchestration.execution_state import ExecutionIdentity, ExecutionPhase, ExecutionStatus, OutcomeKind
from src.orchestration.execution_store import create_execution
from src.orchestration.models import DecisionKind, OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationExecution, OrchestrationWorkClaim
from tests.orchestration.test_dispatch_pass import _make_flow, _make_node, _make_org
from tests.orchestration.test_dispatch_pass import engine as engine_fixture
from tests.orchestration.test_dispatch_pass import session as session_fixture
from tests.orchestration.test_dispatch_pass import session_factory as session_factory_fixture

engine = engine_fixture
session_factory = session_factory_fixture
session = session_fixture


async def _stalled_story(session, *, history=("stalled", "resumed"), execution_status=ExecutionStatus.BLOCKED, claim_state="held"):
    await _make_org(session)
    flow = await _make_flow(session)
    node = await _make_node(session, flow, node_ref="s1", issue_ref="7173")
    node.state, node.attempts = "ready", 1
    plan = OrchestrationAcceptedPlan(org_id=flow.org_id, flow_id=flow.id, version=1, plan_document={"flow_slug": flow.slug}, plan_hash="h1")
    claim = OrchestrationWorkClaim(
        org_id=flow.org_id,
        provider_repository_id=12345,
        issue_number=7173,
        owner_kind="engine_flow",
        owner_ref=flow.id,
        active_run_id="reviewer-run-that-exited",
        state="held",
        generation=1,
        lease_expires_at=datetime.now(UTC) - timedelta(hours=4),
    )
    session.add_all([plan, claim])
    await session.flush()
    identity = ExecutionIdentity(node.org_id, node.id, 1, 1, claim.id, 1)
    created = await create_execution(session, identity=identity, flow_id=flow.id)
    assert created.kind is OutcomeKind.APPLIED
    execution = await session.get(OrchestrationExecution, created.record.id)
    execution.phase, execution.status = ExecutionPhase.MERGE_READY.value, execution_status.value
    claim.state = claim_state
    kinds = {
        "stalled": (DecisionKind.NODE_STALLED.value, "running", "failed", "service"),
        "resumed": (DecisionKind.NODE_RESUMED.value, "failed", "ready", "human"),
        # What the resume route actually records for a failed node (controls._RESUMABLE_STATES).
        "resumed_as_recorded": (DecisionKind.NODE_STALLED.value, "failed", "ready", "human"),
        "resumed_by_service": (DecisionKind.NODE_RESUMED.value, "failed", "ready", "service"),
        "retry": (DecisionKind.NODE_RESUMED.value, "running", "ready", "human"),
    }
    base = datetime.now(UTC) - timedelta(minutes=10)
    for offset, key in enumerate(history):
        kind, from_state, to_state, actor = kinds[key]
        session.add(
            OrchestrationDecision(
                org_id=flow.org_id,
                flow_id=flow.id,
                node_id=node.id,
                kind=kind,
                actor_id="system:stall" if actor == "service" else "alice",
                actor_role="engine" if actor == "service" else "org_admin",
                actor_kind=actor,
                from_state=from_state,
                to_state=to_state,
                reason="x",
                created_at=base + timedelta(minutes=offset),
            )
        )
    await session.flush()
    return SimpleNamespace(flow=flow, node=node, claim=claim, execution=execution, plan=plan)


@pytest.mark.parametrize("resume_kind", ["resumed", "resumed_as_recorded"])
async def test_stalled_then_resumed_story_reenters_its_live_execution(session, resume_kind):
    s = await _stalled_story(session, history=("stalled", resume_kind))
    report = DispatchPassReport()
    assert await _continue_stalled_execution(session, s.node, report=report) is True
    await session.refresh(s.node)
    assert s.node.state == "running" and s.node.attempts == 1
    assert report.continued == 1
    decision = (
        await session.scalars(
            select_decisions(s.node.id).where(OrchestrationDecision.to_state == "running").order_by(OrchestrationDecision.created_at.desc())
        )
    ).first()
    assert decision.kind == DecisionKind.NODE_RESUMED.value and decision.actor_kind == "service"
    assert json.loads(decision.reason)["mode"] == "execution_continuation"
    assert json.loads(decision.reason)["execution_id"] == s.execution.id


def select_decisions(node_id):
    from sqlalchemy import select

    return select(OrchestrationDecision).where(OrchestrationDecision.node_id == node_id)


@pytest.mark.parametrize(
    "variant",
    ["human_retry_from_running", "no_history", "service_resume", "execution_concluded", "claim_released"],
)
async def test_other_ready_stories_still_get_a_fresh_attempt(session, variant):
    kwargs = {}
    if variant == "human_retry_from_running":
        kwargs["history"] = ("retry",)
    elif variant == "no_history":
        kwargs["history"] = ()
    elif variant == "service_resume":
        kwargs["history"] = ("stalled", "resumed_by_service")
    elif variant == "execution_concluded":
        kwargs["execution_status"] = ExecutionStatus.CONCLUDED
    else:
        kwargs["claim_state"] = "released"
    s = await _stalled_story(session, **kwargs)
    report = DispatchPassReport()
    assert await _continue_stalled_execution(session, s.node, report=report) is False
    await session.refresh(s.node)
    assert s.node.state == "ready" and report.continued == 0


@pytest.mark.parametrize(("status", "expected"), [(ExecutionStatus.BLOCKED, (True, None)), (ExecutionStatus.AWAITING_EXTERNAL, None)])
async def test_blocked_execution_has_no_worker_clock(session, status, expected):
    s = await _stalled_story(session, history=(), execution_status=status)
    s.node.state = "running"
    await session.flush()
    candidate = SimpleNamespace(org_id=s.node.org_id, flow_id=s.flow.id, node_id=s.node.id, attempts=1)
    managed, since = await stall._continuation_clock(session, candidate)
    if expected is not None:
        assert (managed, since) == expected
    else:
        # A worker may be in flight: the existing clock rules apply (here: no
        # committed dispatch for the active run, so the clock is not managed).
        assert (managed, since) == (False, None)
