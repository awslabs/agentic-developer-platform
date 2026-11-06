"""Retry a protected startup failure through settlement, recovery and dispatch."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import tests.orchestration.test_operations_lost_worker_retry as fixtures
from src.orchestration.dispatch_pass import run_dispatch_pass
from src.orchestration.models import OrchestrationAction, OrchestrationExecution, OrchestrationWorkClaim
from src.orchestration.results import observe_results, protected_failure_for_assignment
from src.orchestration.run_reports import OrchestrationRunReport
from tests.orchestration.test_operations_lost_worker_retry import _recover
from tests.orchestration.test_operations_release_resolution import _config, _dispatched_release

engine = fixtures.engine
session = fixtures.session
governed_work_ownership = fixtures.governed_work_ownership
healthy_policy_reservations = fixtures.healthy_policy_reservations
policy_budget_initializers = fixtures.policy_budget_initializers


@pytest.mark.parametrize("mode", ["automatic", "resume"])
@pytest.mark.parametrize(
    "changed",
    [None, "missing", "active", "success", "cancelled", "foreign_node", "foreign_attempt", "foreign_persona", "fence", "pending", "live_owner"],
)
async def test_protected_bootstrap_failure_recovery(session, monkeypatch, governed_work_ownership, mode, changed):
    fixture = await _dispatched_release(session, monkeypatch, retry=True)
    node = fixture.node
    original = fixture.first_pass.pending[0].envelope
    run_id = original["message_id"]
    report = await session.get(OrchestrationRunReport, run_id)
    assert report.worker_receipt is report.terminal_receipt is None
    execution = await session.get(OrchestrationExecution, original["handoff_expect"]["execution_id"])
    claim = await session.get(OrchestrationWorkClaim, execution.claim_id)
    raw = {
        "tenant_id": {"S": node.org_id},
        "invocation_id": {"S": run_id},
        "flow_id": {"S": node.flow_id},
        "orchestration_node_id": {"S": node.id},
        "orchestration_node_attempt": {"N": "1"},
        "status": {"S": "completed"},
        "terminal_outcome": {"S": "failed"},
        "persona": {"S": "operations"},
    }
    store = SimpleNamespace(_read=Mock(side_effect=lambda *_: raw))

    async def protected(**kwargs):
        return await protected_failure_for_assignment(**kwargs, store=store)

    monkeypatch.setattr("src.orchestration.results.protected_failure_for_assignment", protected)
    await session.commit()
    settled = await observe_results(session)
    assert settled.advanced == 1 and node.state == "failed"
    assert execution.status == execution.phase == "concluded"
    assert claim.state == "released" and claim.release_reason == "failed"
    assert report.worker_receipt is report.terminal_receipt is None

    if changed == "missing":
        raw.clear()
    elif changed == "active":
        raw["status"] = {"S": "active"}
    elif changed in {"success", "cancelled"}:
        raw["terminal_outcome"] = {"S": "complete" if changed == "success" else "cancelled"}
    elif changed == "foreign_node":
        raw["orchestration_node_id"] = {"S": "other-node"}
    elif changed == "foreign_attempt":
        raw["orchestration_node_attempt"] = {"N": "2"}
    elif changed == "foreign_persona":
        raw["persona"] = {"S": "developer"}
    elif changed == "fence":
        metadata = report.dispatch_metadata
        report.dispatch_metadata = {**metadata, "handoff_expect": {**metadata["handoff_expect"], "claim_generation": 999}}
    elif changed == "pending":
        session.add(
            OrchestrationAction(
                org_id=node.org_id,
                execution_id=execution.id,
                operation_key="uncertain-deploy",
                kind="deployment_dispatch",
                status="unknown",
                attempt=1,
                detail={},
            )
        )
    elif changed == "live_owner":
        claim.generation += 1
        claim.state, claim.release_reason, claim.active_run_id, claim.claim_event_id = "held", None, "live-successor", "live-successor"
    await session.commit()
    recovered = await _recover(session, node, mode)
    if mode == "automatic":
        assert (recovered == "scheduled") is (changed is None), recovered
    await session.commit()
    dispatched = await run_dispatch_pass(session, _config())
    if changed:
        assert dispatched.dispatched == 0 and not dispatched.pending
        assert node.attempts == 1
        await session.refresh(claim)
        assert claim.active_run_id == ("live-successor" if changed == "live_owner" else None)
        return
    assert dispatched.dispatched == 1
    replacement = dispatched.pending[0].envelope
    assert replacement["orchestration"]["deployment_revisions"] == original["orchestration"]["deployment_revisions"]
    assert replacement["orchestration"]["attempt"] == 2
    assert replacement["handoff_expect"]["claim_generation"] == execution.claim_generation + 1
    assert claim.active_run_id == replacement["message_id"]
    assert report.worker_receipt is report.terminal_receipt is None
    # The replacement can read its assigned tasks after the claim advances.
    from src.orchestration.operations_tasks import read_operations_tasks

    tasks = await read_operations_tasks(
        session,
        SimpleNamespace(
            tenant_id=node.org_id,
            node_id=node.id,
            invocation_id=replacement["message_id"],
            attempt=2,
            accepted_plan_version=replacement["handoff_expect"]["accepted_plan_version"],
            claim_generation=replacement["handoff_expect"]["claim_generation"],
        ),
    )
    assert (tasks["recovery_note"] is not None) is (mode == "resume")
