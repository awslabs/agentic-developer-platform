"""Recovered workers remain visible without borrowing sibling story activity."""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import set_committed_value

from src.activity.service import ActivityService
from src.orchestration.continuation_activity import ContinuationActivity, current_continuations
from src.orchestration.models import OrchestrationDecision, OrchestrationNode, OrchestrationWorkClaim
from src.orchestration.node_activity import load_story_execution
from tests.orchestration.test_node_activity import chain, run
from tests.orchestration.test_review_cycle import cycle, pg_server, pg_url, store, tick  # noqa: F401


async def test_real_dispatch_binds_current_reviewer_and_preserves_failed_developer(cycle, monkeypatch):  # noqa: F811
    assert (await tick(cycle)).effects_succeeded == 1
    reviewer = cycle.calls[-1]["message_id"]
    async with cycle.factory() as db:
        bindings = await current_continuations(db, org_id=cycle.node.org_id, flow_id=cycle.flow.id, node_ids=[cycle.node.id])
        before = await db.get(OrchestrationNode, cycle.node.id)
        attempts = before.attempts
        assert not db.dirty
        assert await current_continuations(db, org_id="foreign", flow_id=cycle.flow.id, node_ids=[cycle.node.id]) == {}
        assert await current_continuations(db, org_id=cycle.node.org_id, flow_id="other-flow", node_ids=[cycle.node.id]) == {}
        assert before.attempts == attempts
    assert bindings[cycle.node.id].invocation_id == reviewer
    service = MagicMock()
    service.get_chain.return_value = chain(run("attempt-1", "agent-codex-developer", "2026-10-18T02:07:00Z", status="failed", liveness="exited"))
    service.get_chain_member.return_value = run(reviewer, "agent-codex-reviewer", "2026-10-18T06:14:20Z")
    monkeypatch.setattr("src.orchestration.node_activity._activity_service", lambda: service)
    result = (await load_story_execution(org_id=cycle.node.org_id, run_ids=["attempt-1"], continuations={"attempt-1": bindings[cycle.node.id]}))[
        "attempt-1"
    ]
    assert result.activity.invocation_id == reviewer
    assert result.runs[0].status == "failed"
    assert result.run_id == "attempt-1" and not result.history_complete
    service.get_chain.assert_called_once_with(correlation_id="attempt-1", tenant_id=cycle.node.org_id)


@pytest.mark.parametrize(
    "change", ["attempt", "generation", "claim_run", "claim_state", "receipt_run", "receipt_plan", "receipt_actor", "receipt_node", "malformed"]
)
async def test_stale_or_unbound_continuation_cannot_become_current(cycle, change):  # noqa: F811
    await tick(cycle)
    async with cycle.factory() as db:
        claim = await db.get(OrchestrationWorkClaim, cycle.identity.claim_id)
        node = await db.get(OrchestrationNode, cycle.node.id)
        receipt = await db.scalar(select(OrchestrationDecision).where(OrchestrationDecision.kind == "agent_dispatched"))
        invalid = {}
        if change == "attempt":
            node.attempts += 1
        elif change == "generation":
            claim.generation += 1
        elif change == "claim_run":
            claim.active_run_id = "other-story"
        elif change == "claim_state":
            claim.state = "released"
        elif change == "receipt_actor":
            invalid = {"actor_id": "human"}
        elif change == "receipt_node":
            invalid = {"node_id": None}
        elif change == "malformed":
            invalid = {"reason": "[]"}
        else:
            data = json.loads(receipt.reason)
            data["run_id" if change == "receipt_run" else "accepted_plan_version"] = "other"
            invalid = {"reason": json.dumps(data)}
        if invalid:
            # Exercise malformed legacy read values without writing the
            # append-only ledger, even in this isolated test database.
            for key, value in invalid.items():
                set_committed_value(receipt, key, value)
        await db.flush()
        assert await current_continuations(db, org_id=cycle.node.org_id, flow_id=cycle.flow.id, node_ids=[cycle.node.id]) == {}


@pytest.mark.parametrize("mode", ["missing", "malformed", "wrong_id", "wrong_persona", "complete", "budget_stopped", "budget_wait"])
async def test_exact_recovered_status_never_resurrects_prior_worker(monkeypatch, mode):
    service = MagicMock()
    service.get_chain.return_value = chain(run("old", "reviewer", "2026-10-18T01:00:00Z"))
    member = run("current", "agent-codex-reviewer", "2026-10-18T06:00:00Z")
    if mode == "wrong_id":
        member.invocation_id = "sibling"
    elif mode == "wrong_persona":
        member.persona = "developer"
    elif mode in {"complete", "budget_stopped"}:
        member.status, member.liveness = mode, "exited"
    elif mode == "budget_wait":
        member.model_admission_status = "budget_wait"
        member.model_admission_updated_at = datetime.now(UTC).isoformat()
    service.get_chain_member.return_value = None if mode == "missing" else member
    if mode == "malformed":
        service.get_chain_member.side_effect = ValueError("invalid optional activity field")
    monkeypatch.setattr("src.orchestration.node_activity._activity_service", lambda: service)
    result = (
        await load_story_execution(
            org_id="org-a", run_ids=["attempt-1"], continuations={"attempt-1": ContinuationActivity("current", "flow-id", "agent-codex-reviewer", 1)}
        )
    )["attempt-1"]
    assert result.runs[0].invocation_id == "old"
    assert result.activity is None if mode != "budget_wait" else result.activity.invocation_id == "current"
    if mode == "budget_wait":
        assert result.activity.runtime_budget_wait is not None
    service.get_chain_member.assert_called_once_with("current", tenant_id="org-a", correlation_id="flow-id")


@pytest.mark.parametrize("change", [None, "tenant_id", "correlation_id", "invocation_id"])
def test_exact_member_read_is_tenant_scoped_and_never_expands_flow(change):
    service = ActivityService.__new__(ActivityService)
    service._table = MagicMock()
    row = {
        "event_id": "current",
        "invocation_id": "current",
        "tenant_id": "org-a",
        "correlation_id": "flow-id",
        "arrived_at": "2026-10-18T06:00:00Z",
        "persona": "agent-codex-reviewer",
        "status": "in_progress",
    }
    if change:
        row[change] = "sibling"
    service._table.query.return_value = {"Items": [row]}
    result = service.get_chain_member("current", tenant_id="org-a", correlation_id="flow-id")
    assert (result is not None) is (change is None)
    kwargs = service._table.query.call_args.kwargs
    assert kwargs["Limit"] == 1 and kwargs["ConsistentRead"] is True
    assert "IndexName" not in kwargs
