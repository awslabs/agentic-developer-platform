"""A story whose work claim was released as failed or abandoned has failed its attempt even without a terminal receipt.

Production shape (tools flow, 2026-10-14, TLS-S17/S18/S19): protected developer workers exited during a
gateway rollout. Two released their claim as ``failed`` after recording a complete protected execution and
an active PR binding; the engine released the third as ``abandoned`` once its pod was proven gone. No shared
run report or protected failure existed, the durable handler refused every execution with ``claim_not_held``,
the results pass waited for a receipt that could never arrive, and all three stories ran for hours.
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration.execution_state import ExecutionStatus
from src.orchestration.models import OrchestrationDecision, OrchestrationExecution, OrchestrationWorkClaim
from src.orchestration.policy_admission import AdmissionInputs
from src.orchestration.results import observe_results, released_claim_failure_for_assignment
from tests.orchestration.test_dispatch_pass import engine as engine_fixture
from tests.orchestration.test_dispatch_pass import session as session_fixture
from tests.orchestration.test_dispatch_pass import session_factory as session_factory_fixture
from tests.orchestration.test_policy_admission import _policy
from tests.orchestration.test_refused_start_settlement import _running_story

engine = engine_fixture
session_factory = session_factory_fixture
session = session_fixture


class _NoRunRecord:
    """An engine run store with no record for the run: the worker never wrote one."""

    def get(self, run_id, arrived_at):
        return None


def _no_other_evidence(monkeypatch):
    monkeypatch.setattr("src.orchestration.delivery_adoption.observe_historical_delivery", AsyncMock(return_value=None))
    monkeypatch.setattr("src.orchestration.results.run_result_for_assignment", AsyncMock(return_value=None))
    monkeypatch.setattr("src.orchestration.results.protected_failure_for_assignment", AsyncMock(return_value=None))
    monkeypatch.setattr("src.orchestration.results.protected_terminal_outcome", AsyncMock(return_value=None))
    monkeypatch.setattr("src.orchestration.policy_admission.load_in_force_policy", AsyncMock(return_value=AdmissionInputs(_policy(), 1)))


@pytest.mark.parametrize("release_reason", ["failed", "abandoned"])
async def test_released_claim_settles_the_governed_attempt_without_a_receipt(session, monkeypatch, release_reason):
    node, run_id = await _running_story(session, claim_state="released", release_reason=release_reason)
    _no_other_evidence(monkeypatch)

    report = await observe_results(session, run_store=_NoRunRecord())

    await session.refresh(node)
    assert node.state == "failed", report.reasons
    assert report.advanced == 1 and report.waiting == 0
    decision = (
        await session.scalars(
            select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id, OrchestrationDecision.to_state == "failed")
        )
    ).first()
    assert decision is not None
    assert "claim_released" in (decision.reason or "")
    assert release_reason in (decision.reason or "")
    execution = (await session.scalars(select(OrchestrationExecution).where(OrchestrationExecution.node_id == node.id))).one()
    assert execution.status == ExecutionStatus.CONCLUDED.value
    claim = (await session.scalars(select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.owner_ref == node.flow_id))).one()
    assert claim.state == "released" and claim.claim_event_id == run_id


async def test_a_held_claim_keeps_waiting_for_the_worker_receipt(session, monkeypatch):
    """A live worker still owns the claim: the release rule must not fail its attempt."""
    node, _run_id = await _running_story(session)
    _no_other_evidence(monkeypatch)

    report = await observe_results(session, run_store=_NoRunRecord())

    await session.refresh(node)
    assert node.state == "running", report.reasons
    assert report.waiting == 1 and report.advanced == 0


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda claim, run_id: setattr(claim, "release_reason", "completed"), "a clean release is not failure evidence"),
        (lambda claim, run_id: setattr(claim, "claim_event_id", "orch:someone-else"), "the release must name this run as its last owner"),
        (lambda claim, run_id: setattr(claim, "active_run_id", run_id), "a claim with an active run is still owned"),
        (lambda claim, run_id: setattr(claim, "generation", 99), "a different claim generation is another dispatch lane"),
    ],
)
async def test_release_evidence_is_bound_to_the_exact_run_and_claim(session, monkeypatch, mutate, reason):
    node, run_id = await _running_story(session, claim_state="released", release_reason="failed")
    claim = (await session.scalars(select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.owner_ref == node.flow_id))).one()
    mutate(claim, run_id)
    await session.flush()
    _no_other_evidence(monkeypatch)

    dispatch = {"run_id": run_id, "attempt": node.attempts}
    assert await released_claim_failure_for_assignment(session, node=node, dispatch=dispatch) is None, reason

    report = await observe_results(session, run_store=_NoRunRecord())
    await session.refresh(node)
    assert node.state == "running", (reason, report.reasons)


async def test_release_evidence_row_names_the_claim_release(session, monkeypatch):
    monkeypatch.setattr("src.orchestration.results.protected_terminal_outcome", AsyncMock(return_value=None))
    node, run_id = await _running_story(session, claim_state="released", release_reason="abandoned")
    row = await released_claim_failure_for_assignment(session, node=node, dispatch={"run_id": run_id, "attempt": node.attempts})
    assert row == {
        "tenant_id": node.org_id,
        "engine_node_id": node.id,
        "engine_attempt": node.attempts,
        "status": "failed",
        "status_source": "claim_released",
        "terminal_outcome": "failed",
        "failure_reason": "worker_claim_abandoned",
        "claim_release_reason": "abandoned",
    }


async def test_a_completed_protected_run_is_never_failed_on_its_claim_release(session, monkeypatch):
    """The run's own `complete` terminal record outranks a `failed` release label (#7764).

    Production shape (cos-runtime COS-S15 attempt 3, 2026-10-15 08:24Z): the developer
    posted `status=complete` with PR #7750 bound, the status route released the claim as
    `failed` because it compared the literal against "completed", and this rule then
    failed the attempt and retried it — discarding 4.5 hours of published work.
    """
    node, run_id = await _running_story(session, claim_state="released", release_reason="failed")
    _no_other_evidence(monkeypatch)
    monkeypatch.setattr("src.orchestration.results.protected_terminal_outcome", AsyncMock(return_value="complete"))

    dispatch = {"run_id": run_id, "attempt": node.attempts}
    assert await released_claim_failure_for_assignment(session, node=node, dispatch=dispatch) is None

    report = await observe_results(session, run_store=_NoRunRecord())
    await session.refresh(node)
    assert node.state == "running", report.reasons
    assert report.advanced == 0
