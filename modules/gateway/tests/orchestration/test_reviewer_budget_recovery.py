"""Only an attributed later budget decision releases a protected reviewer hold."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.orchestration.execution_state import BlockCode
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.review_cycle_dispatch import require_budget_recovery


def context():
    now = datetime.now(UTC)
    node = SimpleNamespace(org_id="org", flow_id="flow", id="node", attempts=2)
    raw = {
        key: {"S": value}
        for key, value in dict(
            tenant_id="org",
            invocation_id="run",
            orchestration_node_id="node",
            persona="agent-codex-reviewer",
            orchestration_continuation_action="review",
            orchestration_continuation_receipt="receipt",
            status="completed",
            terminal_outcome="budget_stopped",
            terminal_recorded_at=now.isoformat(),
        ).items()
    }
    decision = SimpleNamespace(org_id="org", flow_id="flow", kind="budget_increased", actor_kind="human", created_at=now + timedelta(seconds=1))
    policy = SimpleNamespace(_shared_budget_decision_id="new-budget")
    session = SimpleNamespace(get=AsyncMock(return_value=decision))
    return node, raw, decision, policy, session


async def test_later_in_force_budget_receipt_allows_normal_admission_to_retry():
    node, raw, decision, policy, session = context()
    await require_budget_recovery(session, node=node, active_run_id="run", raw=raw, policy=policy)
    assert session.get.await_count == 1
    assert raw["terminal_outcome"] == {"S": "budget_stopped"}


@pytest.mark.parametrize("change", ["absent", "older", "foreign_flow", "foreign_org", "wrong_kind", "worker_actor", "missing_time", "naive_time"])
async def test_unchanged_or_unrelated_budget_does_not_release_hold(change):
    node, raw, decision, policy, session = context()
    if change == "absent":
        policy._shared_budget_decision_id = None
    elif change == "older":
        decision.created_at -= timedelta(seconds=2)
    elif change == "foreign_flow":
        decision.flow_id = "other"
    elif change == "foreign_org":
        decision.org_id = "other"
    elif change == "wrong_kind":
        decision.kind = "plan_accepted"
    elif change == "missing_time":
        raw.pop("terminal_recorded_at")
    elif change == "naive_time":
        raw["terminal_recorded_at"] = {"S": "2026-01-01T00:00:00"}
    else:
        decision.actor_kind = "service"
    with pytest.raises(CycleBlockedError) as failure:
        await require_budget_recovery(session, node=node, active_run_id="run", raw=raw, policy=policy)
    assert failure.value.code is BlockCode.BUDGET_EXHAUSTED


@pytest.mark.parametrize("field", ["tenant_id", "orchestration_node_id", "invocation_id", "persona", "status"])
async def test_failure_receipt_must_belong_to_current_reviewer(field):
    node, raw, decision, policy, session = context()
    raw[field] = {"S": "other"}
    with pytest.raises(CycleBlockedError) as failure:
        await require_budget_recovery(session, node=node, active_run_id="run", raw=raw, policy=policy)
    assert failure.value.code is BlockCode.AUTHORITY_UNVERIFIABLE


async def test_non_budget_failure_keeps_existing_retry_semantics():
    node, raw, decision, policy, session = context()
    raw["terminal_outcome"] = {"S": "failed"}
    await require_budget_recovery(session, node=node, active_run_id="run", raw=raw, policy=policy)
    session.get.assert_not_awaited()
