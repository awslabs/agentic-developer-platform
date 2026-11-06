"""A fenced lost operations worker need not invent a terminal report to retry."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import tests.orchestration.test_operations_release_resolution as release_fixtures
from src.orchestration.controls import ResumeRequest, resume_node
from src.orchestration.developer_recovery import recover_developer
from src.orchestration.dispatch_pass import run_dispatch_pass
from src.orchestration.models import OrchestrationAction, OrchestrationDecision, OrchestrationExecution, OrchestrationWorkClaim
from src.orchestration.results import observe_results
from src.orchestration.run_reports import OrchestrationRunReport
from tests.orchestration.test_operations_release_resolution import (
    _config,
    _dispatched_release,
)

engine = release_fixtures.engine
session = release_fixtures.session
governed_work_ownership = release_fixtures.governed_work_ownership
healthy_policy_reservations = release_fixtures.healthy_policy_reservations
policy_budget_initializers = release_fixtures.policy_budget_initializers


async def _lost_worker(session, monkeypatch):
    fixture = await _dispatched_release(session, monkeypatch, retry=True)
    node = fixture.node
    original = fixture.first_pass.pending[0].envelope
    run_id = original["message_id"]
    report = await session.get(OrchestrationRunReport, run_id)
    report.worker_receipt = {"recorded_at": datetime.now(UTC).isoformat()}
    report.block_code, report.retryable = "delivery_recovery_required", False
    execution = await session.get(OrchestrationExecution, original["handoff_expect"]["execution_id"])
    claim = await session.get(OrchestrationWorkClaim, execution.claim_id)
    claim.state, claim.release_reason, claim.active_run_id = "released", "abandoned", None
    await session.commit()

    result = await observe_results(session)
    assert result.advanced == 1 and node.state == "failed", result.reasons
    assert report.terminal_receipt is None
    assert execution.status == "concluded"
    return node, original, report, execution, claim


async def _recover(session, node, mode):
    if mode == "automatic":
        return await recover_developer(session, node, now=datetime.now(UTC) + timedelta(minutes=5))
    else:
        await resume_node(
            node.id,
            ResumeRequest(reason="The lost worker is fenced; retain its release and retry verification."),
            SimpleNamespace(org_id=node.org_id, user_id="approver"),
            SimpleNamespace(check_permission=AsyncMock(), get_user_role=AsyncMock(return_value=[SimpleNamespace(value="org_admin")])),
            session,
            None,
        )


@pytest.mark.parametrize("mode", ["automatic", "resume"])
async def test_lost_operations_worker_retries_original_release(session, monkeypatch, governed_work_ownership, mode):
    node, original, report, execution, claim = await _lost_worker(session, monkeypatch)
    result = await _recover(session, node, mode)
    if mode == "automatic":
        assert result == "scheduled"
    await session.commit()
    from src.orchestration.developer_recovery import retry_context

    assert await retry_context(session, node, 1) is not None
    dispatched = await run_dispatch_pass(session, _config())
    rejections = (await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.rejection_reason.is_not(None)))).all()
    assert dispatched.dispatched == 1, [item.rejection_reason for item in rejections]
    replacement = dispatched.pending[0].envelope
    assert replacement["orchestration"]["deployment_revisions"] == original["orchestration"]["deployment_revisions"]
    assert replacement["orchestration"]["attempt"] == 2
    assert replacement["orchestration"]["operations_recovery"]["previous_run_id"] == original["message_id"]
    assert replacement["orchestration"]["operations_recovery"]["execution_id"] == execution.id
    assert replacement["handoff_expect"]["claim_generation"] == execution.claim_generation + 1
    assert claim.active_run_id == replacement["message_id"]
    assert report.terminal_receipt is None
    assert node.state == "running"
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
    if mode == "resume":
        assert tasks["recovery_note"] == {
            "decision_id": replacement["orchestration"]["operations_recovery"]["resume_decision_id"],
            "text": "The lost worker is fenced; retain its release and retry verification.",
            "truncated": False,
        }
    else:
        assert tasks["recovery_note"] is None


@pytest.mark.parametrize("mode", ["automatic", "resume"])
@pytest.mark.parametrize(
    "changed",
    ["no_worker", "other_failure", "retryable", "terminal", "scope", "fence", "plan", "unsettled", "pending_effect", "failure_actor"],
)
async def test_lost_worker_retry_rejects_changed_evidence(session, monkeypatch, governed_work_ownership, mode, changed):
    node, original, report, execution, claim = await _lost_worker(session, monkeypatch)
    if changed == "no_worker":
        report.worker_receipt = None
    elif changed == "other_failure":
        report.block_code = "policy_refused"
    elif changed == "retryable":
        report.retryable = True
    elif changed == "terminal":
        report.terminal_receipt = {"outcome": "complete", "run_id": report.run_id, "attempt": report.attempt}
    elif changed == "scope":
        report.node_id = "another-node"
    elif changed in {"fence", "plan"}:
        field = "claim_generation" if changed == "fence" else "accepted_plan_version"
        metadata = report.dispatch_metadata
        report.dispatch_metadata = {**metadata, "handoff_expect": {**metadata["handoff_expect"], field: 999}}
    elif changed == "unsettled":
        execution.status = "awaiting_external"
    elif changed == "pending_effect":
        session.add(
            OrchestrationAction(
                org_id=node.org_id,
                execution_id=execution.id,
                operation_key="unresolved-effect",
                kind="deployment_dispatch",
                status="unknown",
                attempt=1,
                detail={},
            )
        )
    elif changed == "failure_actor":
        failed = await session.scalar(
            select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id, OrchestrationDecision.to_state == "failed")
        )
        session.add(
            OrchestrationDecision(
                org_id=node.org_id,
                flow_id=node.flow_id,
                node_id=node.id,
                kind=failed.kind,
                actor_id="another-service",
                actor_kind="service",
                actor_role="engine",
                from_state="running",
                to_state="failed",
                reason=failed.reason,
            )
        )
    await session.commit()
    result = await _recover(session, node, mode)
    if mode == "automatic":
        assert result != "scheduled"
    dispatched = await run_dispatch_pass(session, _config())
    assert dispatched.dispatched == 0
    assert not dispatched.pending
    assert node.attempts == 1
    await session.refresh(claim)
    assert claim.active_run_id is None


async def test_lost_worker_automatic_retry_requires_released_exact_owner(session, monkeypatch, governed_work_ownership):
    node, original, report, execution, claim = await _lost_worker(session, monkeypatch)
    claim.generation += 1
    await session.commit()
    assert await _recover(session, node, "automatic") == "work_owner_changed"
    assert node.state == "failed"
    assert node.attempts == 1


async def test_lost_worker_resume_does_not_override_a_live_successor(session, monkeypatch, governed_work_ownership):
    node, original, report, execution, claim = await _lost_worker(session, monkeypatch)
    await _recover(session, node, "resume")
    claim.generation += 1
    claim.state, claim.release_reason, claim.active_run_id, claim.claim_event_id = "held", None, "live-other-run", "live-other-run"
    await session.commit()
    dispatched = await run_dispatch_pass(session, _config())
    assert dispatched.dispatched == 0
    assert not dispatched.pending
    assert claim.active_run_id == "live-other-run"
    assert node.attempts == 1
