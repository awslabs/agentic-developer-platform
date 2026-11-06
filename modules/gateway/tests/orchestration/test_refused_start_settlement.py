"""A worker that was refused at /started for a non-retryable reason has failed its attempt (#7419 AC-04)."""

import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration.dispatch_pass import attempt_run_id
from src.orchestration.execution_state import ExecutionIdentity, OutcomeKind
from src.orchestration.execution_store import create_execution
from src.orchestration.models import DecisionKind, OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationWorkClaim
from src.orchestration.policy_admission import AdmissionInputs
from src.orchestration.results import observe_results
from tests.orchestration.test_dispatch_pass import _make_flow, _make_node, _make_org
from tests.orchestration.test_dispatch_pass import engine as engine_fixture
from tests.orchestration.test_dispatch_pass import session as session_fixture
from tests.orchestration.test_dispatch_pass import session_factory as session_factory_fixture
from tests.orchestration.test_policy_admission import _policy

engine = engine_fixture
session_factory = session_factory_fixture
session = session_fixture


async def _running_story(session, *, claim_state="held", release_reason=None):
    await _make_org(session)
    flow = await _make_flow(session)
    node = await _make_node(session, flow, node_ref="ops", issue_ref="7349")
    node.state, node.attempts = "running", 4
    run_id = attempt_run_id(node.id, 4)
    session.add(OrchestrationAcceptedPlan(org_id=flow.org_id, flow_id=flow.id, version=1, plan_document={"flow_slug": flow.slug}, plan_hash="h1"))
    claim = OrchestrationWorkClaim(
        org_id=flow.org_id,
        provider_repository_id=12345,
        issue_number=7349,
        owner_kind="engine_flow",
        owner_ref=flow.id,
        active_run_id=run_id,
        state="held",
        generation=4,
    )
    session.add(claim)
    await session.flush()
    created = await create_execution(session, identity=ExecutionIdentity(node.org_id, node.id, 4, 1, claim.id, 4), flow_id=flow.id)
    assert created.kind is OutcomeKind.APPLIED
    if claim_state == "released":
        # The engine released the claim once the pod was proven gone (abandoned);
        # the run id stays on the row as its last owner.
        claim.state, claim.release_reason, claim.claim_event_id, claim.active_run_id = "released", release_reason, run_id, None
        await session.flush()
    session.add(
        OrchestrationDecision(
            org_id=flow.org_id,
            flow_id=flow.id,
            node_id=node.id,
            kind=DecisionKind.NODE_DISPATCHED.value,
            actor_id="system:orchestration-dispatch",
            actor_role="engine",
            actor_kind="service",
            from_state="ready",
            to_state="running",
            reason=json.dumps({"run_id": run_id, "attempt": 4, "arrived_at": "2026-10-12T10:25:31Z", "issue": 7349}),
        )
    )
    await session.flush()
    return node, run_id


def _refused_row(node, *, block_code, retryable):
    return {
        "tenant_id": node.org_id,
        "engine_node_id": node.id,
        "engine_attempt": node.attempts,
        "status": "in_progress",
        "status_source": "authenticated_run_report",
        "worker_started": False,
        "block_code": block_code,
        "retryable": retryable,
    }


@pytest.mark.parametrize(
    ("block_code", "retryable", "expected_state"),
    [
        ("membership_revoked", False, "failed"),
        ("shared_worker_continuation_disabled", False, "failed"),
        ("budget_unavailable", True, "running"),  # a retryable refusal keeps waiting for the redelivered pod
        (None, None, "running"),  # no refusal recorded: the worker may still be starting
    ],
)
async def test_refused_start_settles_the_attempt_only_for_non_retryable_refusals(session, monkeypatch, block_code, retryable, expected_state):
    node, run_id = await _running_story(session)
    monkeypatch.setattr("src.orchestration.results.observe_historical_delivery", AsyncMock(return_value=None), raising=False)
    monkeypatch.setattr("src.orchestration.delivery_adoption.observe_historical_delivery", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "src.orchestration.results.run_result_for_assignment", AsyncMock(return_value=_refused_row(node, block_code=block_code, retryable=retryable))
    )
    monkeypatch.setattr("src.orchestration.results.protected_failure_for_assignment", AsyncMock(return_value=None))
    settle = AsyncMock(return_value=True)
    monkeypatch.setattr("src.orchestration.results.settle_unstarted_assignment", settle)
    monkeypatch.setattr("src.orchestration.policy_admission.load_in_force_policy", AsyncMock(return_value=AdmissionInputs(_policy(), 1)))
    report = await observe_results(session, run_store=object())
    await session.refresh(node)
    assert node.state == expected_state, report.reasons
    if expected_state == "failed":
        assert report.advanced == 1
        assert not settle.called  # the worker's own non-retryable block stays on the row
        decision = (
            await session.scalars(
                select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id, OrchestrationDecision.to_state == "failed")
            )
        ).first()
        assert decision is not None and block_code in (decision.reason or "")
    else:
        assert report.waiting == 1


async def test_refused_start_is_settled_after_the_abandoned_claim_was_released(session, monkeypatch):
    """Production shape of #7349 attempt 4: the pod exited, the engine released the claim as abandoned, the row still named the run."""
    node, run_id = await _running_story(session, claim_state="released", release_reason="abandoned")
    monkeypatch.setattr("src.orchestration.delivery_adoption.observe_historical_delivery", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "src.orchestration.results.run_result_for_assignment",
        AsyncMock(return_value=_refused_row(node, block_code="membership_revoked", retryable=False)),
    )
    monkeypatch.setattr("src.orchestration.results.protected_failure_for_assignment", AsyncMock(return_value=None))
    monkeypatch.setattr("src.orchestration.policy_admission.load_in_force_policy", AsyncMock(return_value=AdmissionInputs(_policy(), 1)))
    report = await observe_results(session, run_store=object())
    await session.refresh(node)
    assert node.state == "failed", report.reasons


@pytest.mark.parametrize(
    ("block_code", "retryable", "expected_state"),
    [
        ("delivery_recovery_required", False, "failed"),  # a redelivered pod found the vanished owner's spool (#7511)
        ("delivery_recovery_required", True, "running"),
        ("report_spool_unavailable", False, "running"),  # another refusal after start is not liveness evidence
        (None, None, "running"),
    ],
)
async def test_started_worker_that_vanished_is_settled_by_the_redelivered_refusal(session, monkeypatch, block_code, retryable, expected_state):
    node, run_id = await _running_story(session)
    row = {**_refused_row(node, block_code=block_code, retryable=retryable), "worker_started": True}
    monkeypatch.setattr("src.orchestration.delivery_adoption.observe_historical_delivery", AsyncMock(return_value=None))
    monkeypatch.setattr("src.orchestration.results.run_result_for_assignment", AsyncMock(return_value=row))
    monkeypatch.setattr("src.orchestration.results.protected_failure_for_assignment", AsyncMock(return_value=None))
    monkeypatch.setattr("src.orchestration.policy_admission.load_in_force_policy", AsyncMock(return_value=AdmissionInputs(_policy(), 1)))
    report = await observe_results(session, run_store=object())
    await session.refresh(node)
    assert node.state == expected_state, report.reasons
    if expected_state == "failed":
        decision = (
            await session.scalars(
                select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id, OrchestrationDecision.to_state == "failed")
            )
        ).first()
        assert "disappeared" in (decision.reason or "")
