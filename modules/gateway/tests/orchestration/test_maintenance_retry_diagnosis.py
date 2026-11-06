"""Source checkpoints retain relevant diagnoses and never mutate old evidence."""

import copy
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import set_committed_value

import tests.orchestration.test_maintenance_adoption as fixtures
from src.orchestration.accepted_node import accepted_node_contract
from src.orchestration.compile import plan_hash
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationAction, OrchestrationDecision, OrchestrationExecution
from src.orchestration.operations_retry_diagnosis import retry_diagnosis_blocker
from src.orchestration.proposal import LoopProposal
from tests.orchestration.test_repeated_maintenance_adoption import adopt, fail_attempt, next_review, resume

engine = fixtures.engine
session = fixtures.session
governed_work_ownership = fixtures.governed_work_ownership
healthy_policy_reservations = fixtures.healthy_policy_reservations
policy_budget_initializers = fixtures.policy_budget_initializers


async def diagnosis(session, execution, code):
    action = await session.scalar(
        select(OrchestrationAction).where(
            OrchestrationAction.execution_id == execution.id,
            OrchestrationAction.operation_key == "operations-task:1:diagnose",
        )
    )
    action.detail = {**action.detail, "state": "done", "diagnosis_code": code}
    await session.flush()
    return action


async def check(session, b):
    return await retry_diagnosis_blocker(
        session,
        node=b.node,
        contract=await accepted_node_contract(session, b.node),
        runtime_unverified=True,
        failure_category=None,
    )


@pytest.mark.parametrize("code", ["missing_protected_input.ADP_L5_DEVOPS_LIVE_CONFIG_JSON", "devops_human_evidence_required"])
async def test_real_source_adoption_does_not_clear_completed_input_hold(session, monkeypatch, governed_work_ownership, code):
    b = await fixtures.arrange(session, monkeypatch)
    action = await diagnosis(session, b.execution, code)
    old = copy.deepcopy((b.report.terminal_receipt, b.f.plan.plan_document, action.detail))
    await adopt(session, b)
    assert await check(session, b) == "operations_input_required"
    assert (b.report.terminal_receipt, b.f.plan.plan_document, action.detail) == old
    assert b.node.attempts == 1 and b.node.state == "failed"


@pytest.mark.parametrize("same_diagnosis", [True, False])
async def test_changed_source_gets_one_fresh_observation_then_same_failure_stops(session, monkeypatch, governed_work_ownership, same_diagnosis):
    b = await fixtures.arrange(session, monkeypatch)
    original = await diagnosis(session, b.execution, "release_workflow_lacks_live_qualification_job")
    before = copy.deepcopy(original.detail)
    accepted = await adopt(session, b)
    receipt = await session.get(OrchestrationDecision, accepted["decision_id"])
    receipt_before = receipt.reason
    await fail_attempt(session, b, await resume(session, b, monkeypatch))
    second = await session.scalar(
        select(OrchestrationExecution).where(
            OrchestrationExecution.node_id == b.node.id,
            OrchestrationExecution.cycle == 2,
        )
    )
    await diagnosis(session, second, "release_workflow_lacks_live_qualification_job" if same_diagnosis else "runtime_health_check_failed")
    assert await check(session, b) == ("deterministic_failure_unchanged" if same_diagnosis else None)
    if not same_diagnosis:
        assert original.detail == before and receipt.reason == receipt_before
        return
    # A second verified source adoption after the failed observation permits
    # exactly one new source context, without changing either old diagnosis.
    next_review(b, accepted)
    latest = await adopt(session, b)
    assert await check(session, b) is None
    latest_receipt = await session.get(OrchestrationDecision, latest["decision_id"])
    latest_before = latest_receipt.reason
    # Altering a receipt cannot manufacture a source-context exception.
    changed = json.loads(latest_before)
    changed["pins"]["source_revision"] = "0" * 40
    set_committed_value(latest_receipt, "reason", json.dumps(changed))
    assert await check(session, b) == "recovery_evidence_unavailable"
    set_committed_value(latest_receipt, "reason", latest_before)
    assert original.detail == before and receipt.reason == receipt_before


@pytest.mark.parametrize("substantive", [False, True])
async def test_history_crosses_unrelated_amendment_only_for_preserved_node(session, monkeypatch, governed_work_ownership, substantive):
    b = await fixtures.arrange(session, monkeypatch)
    await diagnosis(session, b.execution, "source_defect")
    original = copy.deepcopy(b.f.plan.plan_document)
    document = copy.deepcopy(original)
    address = (await accepted_node_contract(session, b.node)).node.address
    target = next(n for n in document["nodes"] if n["address"] == address)
    if substantive:
        target["criteria"] = [*target["criteria"], "Verify a new required behavior"]
    else:
        next(n for n in document["nodes"] if n["kind"] == "eval")["title"] = "Clarified evaluation title"
    receipt = OrchestrationDecision(
        org_id=b.node.org_id,
        flow_id=b.node.flow_id,
        kind="plan_amended",
        actor_kind="human",
        actor_role="platform_admin",
        actor_id=b.actor.actor_id,
        reason="Owner accepted the amended plan",
    )
    session.add(receipt)
    await session.flush()
    b.f.plan.superseded_at = datetime.now(UTC)
    session.add(
        OrchestrationAcceptedPlan(
            org_id=b.node.org_id,
            flow_id=b.node.flow_id,
            version=2,
            plan_document=document,
            plan_hash=plan_hash(LoopProposal.model_validate(document)),
            accepted_by_decision_id=receipt.id,
        )
    )
    second = OrchestrationExecution(
        org_id=b.node.org_id,
        flow_id=b.node.flow_id,
        node_id=b.node.id,
        cycle=2,
        phase="concluded",
        status="concluded",
        accepted_plan_version=2,
        claim_id=b.execution.claim_id,
        claim_generation=2,
    )
    session.add(second)
    await session.flush()
    session.add(
        OrchestrationAction(
            org_id=b.node.org_id,
            execution_id=second.id,
            kind="operations_task",
            status="succeeded",
            attempt=1,
            operation_key="operations-task:1:diagnose",
            detail={"state": "done", "diagnosis_code": "source_defect"},
        )
    )
    b.node.attempts = 2
    await session.flush()
    assert await check(session, b) == (None if substantive else "deterministic_failure_unchanged")
    assert b.f.plan.plan_document == original
