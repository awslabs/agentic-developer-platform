"""A developer that committed its handoff and then exited failed is settled as a failure (#7823).

Production shape (cos-runtime COS-S15 core2, 2026-10-15 16:08Z): the worker recorded its delivery
handoff, then reported ``failed`` because its PR binding was refused. The results pass deferred to the
review controller ("committed its handoff"), the review controller refused ``worker_failed_or_halted``,
and the story sat in ``running`` with nothing but the stall detector ahead of it.
"""

from unittest.mock import AsyncMock

from sqlalchemy import select

from src.orchestration.execution_state import ExecutionIdentity
from src.orchestration.handoff import handoff_receipt_ref
from src.orchestration.models import OrchestrationAction, OrchestrationDecision, OrchestrationExecution
from src.orchestration.policy_admission import AdmissionInputs
from src.orchestration.results import observe_results
from tests.orchestration.test_dispatch_pass import engine as engine_fixture
from tests.orchestration.test_dispatch_pass import session as session_fixture
from tests.orchestration.test_dispatch_pass import session_factory as session_factory_fixture
from tests.orchestration.test_policy_admission import _policy
from tests.orchestration.test_refused_start_settlement import _running_story

engine = engine_fixture
session_factory = session_factory_fixture
session = session_fixture


async def _failed_after_handoff(session, monkeypatch, *, reviewer_dispatched, review_declined=True):
    node, run_id = await _running_story(session)
    execution = (await session.scalars(select(OrchestrationExecution).where(OrchestrationExecution.node_id == node.id))).one()
    identity = ExecutionIdentity(
        node.org_id, node.id, execution.cycle, execution.accepted_plan_version, execution.claim_id, execution.claim_generation
    )
    execution.handoff_receipt_ref = handoff_receipt_ref(identity, execution.id)
    if review_declined:
        # The review controller looked, found the protected run failed, and blocked the lane.
        execution.phase, execution.status = "awaiting_review", "blocked"
        execution.block_code, execution.block_owner = "human_input_required", "orchestration-owner"
        execution.block_required_input = "Resolve review cycle condition: worker_failed_or_halted"
        execution.block_detail = "worker_failed_or_halted"
    if reviewer_dispatched:
        session.add(
            OrchestrationAction(
                org_id=node.org_id,
                execution_id=execution.id,
                operation_key="review-cycle:1",
                kind="review_cycle_dispatch",
                status="succeeded",
                attempt=1,
                detail={},
            )
        )
    await session.flush()
    row = {
        "tenant_id": node.org_id,
        "engine_node_id": node.id,
        "engine_attempt": node.attempts,
        "status": "failed",
        "status_source": "authenticated_run_report",
        "terminal_outcome": "failed",
        "worker_started": True,
    }
    monkeypatch.setattr("src.orchestration.delivery_adoption.observe_historical_delivery", AsyncMock(return_value=None))
    monkeypatch.setattr("src.orchestration.results.run_result_for_assignment", AsyncMock(return_value=row))
    monkeypatch.setattr("src.orchestration.results.protected_failure_for_assignment", AsyncMock(return_value=None))
    monkeypatch.setattr("src.orchestration.policy_admission.load_in_force_policy", AsyncMock(return_value=AdmissionInputs(_policy(), 1)))
    return node, run_id, execution


async def test_a_failed_developer_with_a_committed_handoff_is_settled_as_a_failure(session, monkeypatch):
    node, run_id, execution = await _failed_after_handoff(session, monkeypatch, reviewer_dispatched=False)

    report = await observe_results(session, run_store=object())

    await session.refresh(node)
    await session.refresh(execution)
    assert node.state == "failed", report.reasons
    assert execution.status == "concluded"
    failed = (
        await session.scalars(
            select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id, OrchestrationDecision.to_state == "failed")
        )
    ).first()
    assert failed is not None and run_id in (failed.reason or "")


async def test_a_dispatched_reviewer_still_owns_the_failed_developer_s_handoff(session, monkeypatch):
    node, run_id, execution = await _failed_after_handoff(session, monkeypatch, reviewer_dispatched=True)

    report = await observe_results(session, run_store=object())

    await session.refresh(node)
    assert node.state == "running" and report.waiting == 1
    assert "continuation" in report.reasons[node.id]


async def test_a_handoff_the_review_controller_has_not_declined_is_still_deferred(session, monkeypatch):
    """The late-cleanup case: the developer handed off and its cleanup failed, but the review
    controller has not refused it. The continuation still owns the node (no settlement)."""
    node, run_id, execution = await _failed_after_handoff(session, monkeypatch, reviewer_dispatched=False, review_declined=False)

    report = await observe_results(session, run_store=object())

    await session.refresh(node)
    await session.refresh(execution)
    assert node.state == "running" and report.waiting == 1
    assert execution.handoff_receipt_ref and execution.status != "concluded"
    assert "committed its handoff" in report.reasons[node.id]
