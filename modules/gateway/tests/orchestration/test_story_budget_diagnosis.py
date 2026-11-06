"""A protected budget stop remains explained after the outer story fails."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.activity.schemas import InvocationChainResponse
from src.activity.service import _build_chain_tree
from src.budget.enforcement_settings import BudgetEnforcementSetting  # noqa: F401 -- register read tables
from src.orchestration.node_activity import story_execution
from src.orchestration.repository import OrchestrationRepository
from src.orchestration.story_run_insights import budget_failure_insight
from tests.orchestration import test_read_api as read

session = read.session
app_with_router = read.app_with_router


def stopped_chain(*, reason="run_cap_exceeded", status="budget_stopped", depth_capped=False, successor=False):
    rows = [
        {
            "event_id": "current-attempt",
            "arrived_at": "2026-11-02T16:38:00Z",
            "status": "complete",
            "persona": "agent-codex-developer",
        },
        {
            "event_id": "stopped-reviewer",
            "arrived_at": "2026-11-02T21:54:16Z",
            "status": status,
            "status_updated_at": "2026-11-03T03:20:18Z",
            "persona": "agent-codex-reviewer",
            "stop_reason": reason,
            "parent_invocation_id": "current-attempt",
        },
    ]
    if successor:
        rows.append({**rows[-1], "event_id": "next-reviewer", "arrived_at": "2026-11-03T04:00:00Z", "status": "complete"})
    return InvocationChainResponse(correlation_id="current-attempt", items=_build_chain_tree(rows), total_count=len(rows), depth_capped=depth_capped)


@pytest.mark.parametrize(
    "reason,scope",
    [
        ("run_cap_exceeded", "per-run budget"),
        ("chain_cap_exceeded", "invocation-chain budget"),
        ("root_user_cap_exceeded", "originating user's budget"),
        ("person_cap_exceeded", "person's platform-wide budget"),
        ("hierarchy_cap_exceeded", "budget hierarchy limit"),
        (None, "budget limit"),
        ("unknown credential=private-value", "budget limit"),
        ({"invalid": "private-value"}, "budget limit"),
    ],
)
def test_budget_reason_reaches_the_story_without_raw_worker_text(reason, scope):
    execution = story_execution(stopped_chain(reason=reason))
    result = budget_failure_insight(execution, run_id="current-attempt", attempt=5)
    assert result.run_id == "stopped-reviewer" and result.attempt == 5
    assert result.outcome == "failed" and result.persona == "agent-codex-reviewer"
    assert scope in result.failure.detail
    assert "next model request" in result.failure.detail
    assert result.failure.responsible == "budget owner"
    assert "reservation" in result.failure.next_action and "retains previous spend and attempts" in result.failure.next_action
    assert result.task_board is None and result.failure.exit_code is None and not result.failure.report_available
    assert "private-value" not in execution.model_dump_json() + result.model_dump_json()


@pytest.mark.parametrize("changes", [{"status": "failed"}, {"status": "complete"}, {"depth_capped": True}, {"successor": True}])
def test_only_latest_verified_budget_stop_is_diagnosed(changes):
    assert budget_failure_insight(story_execution(stopped_chain(**changes)), run_id="current-attempt", attempt=5) is None


def test_an_old_attempt_or_unverified_exit_cannot_supply_current_diagnosis():
    execution = story_execution(stopped_chain())
    assert budget_failure_insight(execution, run_id="other-attempt", attempt=6) is None
    execution.runs[-1].liveness = "unverifiable"
    assert budget_failure_insight(execution, run_id="current-attempt", attempt=5) is None
    assert budget_failure_insight(None, run_id="current-attempt", attempt=5) is None


@pytest.mark.parametrize("endpoint", ["graph", "activity"])
async def test_failed_story_read_explains_protected_reviewer_without_shared_report(session, app_with_router, monkeypatch, endpoint):
    flow = await read.seed_flow(session)
    await read.accept_policy(session, flow)
    node = await read.seed_node(session, flow, node_ref="story", state="failed", attempts=5)
    repository = OrchestrationRepository(session)
    for kind, reason, actor in [
        ("node_dispatched", {"attempt": 5, "run_id": "current-attempt"}, "engine"),
        ("developer_retry_checked", {"attempt": 5, "result": "failure_requires_operator"}, "system:developer-recovery"),
    ]:
        await repository.append_decision(
            org_id=read.ORG_A,
            flow_id=flow.id,
            node_id=node.id,
            kind=kind,
            actor_id=actor,
            actor_role="service",
            actor_kind="service",
            reason=json.dumps(reason),
        )
    await session.flush()
    chain = stopped_chain()
    service = MagicMock()
    service.get_chain.return_value = chain
    monkeypatch.setattr("src.orchestration.node_activity._activity_service", lambda: service)
    monkeypatch.setattr(
        "src.orchestration.routes.load_bounded_story_execution",
        AsyncMock(return_value={"current-attempt": story_execution(chain)}),
    )
    response = read.client_for(app_with_router).get(
        read.route(flow.id) + ("/activity" if endpoint == "activity" else ""),
        params={"node_ids": node.id} if endpoint == "activity" else None,
    )
    assert response.status_code == 200, response.text
    item = response.json()["items" if endpoint == "activity" else "nodes"][0]
    diagnosis = item["run_insight"]
    assert diagnosis["run_id"] == "stopped-reviewer" and diagnosis["attempt"] == 5
    assert diagnosis["failure"]["code"] == "run_cap_exceeded"
    assert "per-run budget" in diagnosis["failure"]["detail"]
    assert "story's recovery control" in diagnosis["failure"]["next_action"]
    await session.refresh(node)
    assert node.state == "failed" and node.attempts == 5
