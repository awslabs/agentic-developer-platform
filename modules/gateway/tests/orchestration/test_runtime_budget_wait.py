"""Budget wait is a current-worker observation, never an execution transition."""

from datetime import UTC, datetime, timedelta

import pytest

from src.activity.schemas import InvocationChainResponse
from src.activity.service import _build_chain_tree
from src.budget.enforcement_settings import BudgetEnforcementSetting  # noqa: F401 -- register policy-read table before fixture create_all
from src.orchestration.delivery_progress import DeliveryProgress, with_runtime_budget_wait
from src.orchestration.node_activity import story_execution
from src.shared.model_admission import runtime_budget_wait
from tests.orchestration import test_read_api as read

session = read.session
app_with_router = read.app_with_router


def current_chain(*, status="budget_reservations_pending", age=0, terminal=False, depth_capped=False):
    now = datetime.now(UTC)
    items = [
        {
            "event_id": "current-reviewer",
            "arrived_at": (now - timedelta(minutes=5)).isoformat(),
            "status": "complete" if terminal else "in_progress",
            "status_updated_at": now.isoformat(),
            "persona": "agent-codex-reviewer",
            "model_admission_status": status,
            "model_admission_updated_at": (now - timedelta(seconds=age)).isoformat(),
        }
    ]
    return InvocationChainResponse(correlation_id="current-attempt", items=_build_chain_tree(items), total_count=1, depth_capped=depth_capped)


def progress(**changes):
    return DeliveryProgress(stage="awaiting_review", actor="reviewer", detail="Review is running", automation="engine", **changes)


def test_real_chain_projection_exposes_wait_and_clears_on_admission():
    base = progress(next_check_at="2026-10-18T02:00:00Z", scheduled_action="Reconcile this execution")
    activity = story_execution(current_chain()).activity
    waiting = with_runtime_budget_wait(base, activity)
    assert waiting.blocker == "budget_reservations_pending"
    assert "another request" in waiting.detail
    assert waiting.next_action and waiting.automation == "blocked"
    assert waiting.next_check_at == base.next_check_at
    assert waiting.stage == base.stage and base.blocker is None
    resumed = story_execution(current_chain(status="not_waiting")).activity
    assert with_runtime_budget_wait(base, resumed) is base


@pytest.mark.parametrize(
    "changes",
    [
        {"age": 121},
        {"age": -5},
        {"status": "unrecognized"},
        {"terminal": True},
        {"depth_capped": True},
    ],
)
def test_stale_unknown_finished_or_incomplete_history_cannot_project_a_wait(changes):
    base = progress()
    assert with_runtime_budget_wait(base, story_execution(current_chain(**changes)).activity) is base


@pytest.mark.parametrize("code", ["execution_stale", "budget_exhausted", "authority_unverifiable", "human_input_required"])
def test_runtime_wait_never_overrides_a_recorded_authority_or_human_hold(code):
    base = progress(blocker=code, blockers=[code])
    assert with_runtime_budget_wait(base, story_execution(current_chain()).activity) is base


def test_an_old_wait_cannot_resurface_after_a_later_reviewer_finishes():
    chain = current_chain()
    latest = chain.items[0].model_copy(
        update={
            "invocation_id": "later-reviewer",
            "invoked_at": datetime.now(UTC).isoformat(),
            "status": "complete",
            "liveness": "exited",
            "model_admission_status": "not_waiting",
        }
    )
    chain.items.append(latest)
    assert story_execution(chain).activity is None


@pytest.mark.parametrize("updated", [None, "invalid", "2026-10-18T02:00:00"])
def test_diagnostics_require_an_aware_server_timestamp(updated):
    assert runtime_budget_wait(status="budget_wait", updated_at=updated, run_status="in_progress", liveness="live") is None


def test_unavailable_meter_and_legacy_wait_do_not_claim_exhaustion_or_orphaned_usage():
    for state in ("budget_unavailable", "budget_wait"):
        observed = with_runtime_budget_wait(progress(), story_execution(current_chain(status=state)).activity)
        assert observed.blocker == state
        assert "another request" not in observed.detail and "exhaust" not in observed.detail and "orphan" not in observed.detail


async def test_graph_wire_wait_and_clear_preserve_durable_execution_and_dispatch(session, app_with_router, monkeypatch):
    import json
    from unittest.mock import MagicMock

    from src.orchestration.models import OrchestrationExecution
    from src.orchestration.repository import OrchestrationRepository

    flow = await read.seed_flow(session)
    await read.accept_policy(session, flow)
    node = await read.seed_node(session, flow, node_ref="story", state="awaiting_merge", attempts=2)
    await OrchestrationRepository(session).append_decision(
        org_id=read.ORG_A,
        flow_id=flow.id,
        node_id=node.id,
        kind="node_dispatched",
        actor_id="engine",
        actor_role="service",
        actor_kind="service",
        reason=json.dumps({"attempt": 2, "run_id": "current-attempt"}),
    )
    row = OrchestrationExecution(
        org_id=read.ORG_A,
        flow_id=flow.id,
        node_id=node.id,
        cycle=2,
        accepted_plan_version=1,
        phase="awaiting_review",
        status="awaiting_external",
        claim_id="claim",
        claim_generation=1,
        progress_note="Review is running",
        next_check_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    session.add(row)
    await session.flush()
    service = MagicMock()
    service.get_chain.return_value = current_chain()
    monkeypatch.setattr("src.orchestration.node_activity._activity_service", lambda: service)
    client = read.client_for(app_with_router)
    waiting_response = client.get(read.route(flow.id))
    assert waiting_response.status_code == 200, waiting_response.text
    waiting = waiting_response.json()["nodes"][0]
    assert waiting["delivery_progress"]["blocker"] == "budget_reservations_pending"
    assert waiting["delivery_progress"]["scheduled_action"] == "Reconcile this execution"
    service.get_chain.return_value = current_chain(status="not_waiting")
    resumed = client.get(read.route(flow.id)).json()["nodes"][0]
    assert resumed["delivery_progress"]["blocker"] is None
    assert resumed["delivery_progress"]["detail"] == "Review is running"
    assert resumed["run_id"] == waiting["run_id"] == "current-attempt"
    assert resumed["attempts"] == waiting["attempts"] == 2
    assert resumed["state"] == waiting["state"] == "awaiting_merge"
    await session.refresh(row)
    assert row.status == "awaiting_external" and row.block_code is None and row.claim_generation == 1
