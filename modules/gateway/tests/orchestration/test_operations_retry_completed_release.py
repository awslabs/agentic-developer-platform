"""An operations worker that completed and released its claim is still retried when its evidence is missing (#7826).

Production shape (adp-tools-platform X05b, 2026-10-15): the worker finished at 15:39Z and released
the issue claim as ``completed``; runtime verification had been held, so the results pass judged
``evidence_missing`` and chose the operations retry — then refused itself with "The failed worker
no longer owns the current delivery execution" because the released-claim rule only knew
``failed`` and ``abandoned``. The story sat in ``awaiting_merge`` for hours.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from src.orchestration.developer_recovery import recover_failed_developers
from src.orchestration.models import OrchestrationDecision, OrchestrationExecution, OrchestrationWorkClaim
from src.orchestration.policy_admission import AdmissionInputs
from src.orchestration.results import observe_results
from src.orchestration.run_reports import prepare_run_report
from tests.orchestration.test_dispatch_pass import engine as engine_fixture
from tests.orchestration.test_dispatch_pass import session as session_fixture
from tests.orchestration.test_dispatch_pass import session_factory as session_factory_fixture
from tests.orchestration.test_policy_admission import _policy
from tests.orchestration.test_refused_start_settlement import _running_story

engine = engine_fixture
session_factory = session_factory_fixture
session = session_fixture


async def _completed_operations_run_without_evidence(session, monkeypatch, *, last_owner):
    monkeypatch.setenv("ADP_ENGINE_OPERATIONS_EXECUTOR", "true")
    node, run_id = await _running_story(session, claim_state="released", release_reason="completed")
    node.state = "awaiting_merge"
    dispatched = (
        await session.scalars(
            select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id, OrchestrationDecision.kind == "node_dispatched")
        )
    ).one()
    # Decisions are append-only: record the evidence-authority dispatch as a newer decision.
    session.add(
        OrchestrationDecision(
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            kind="node_dispatched",
            actor_id="system:orchestration-dispatch",
            actor_role="engine",
            actor_kind="service",
            from_state="ready",
            to_state="running",
            reason=json.dumps({**json.loads(dispatched.reason), "conclusion_authority": "evidence"}),
        )
    )
    execution = (await session.scalars(select(OrchestrationExecution).where(OrchestrationExecution.node_id == node.id))).one()
    claim = await session.get(OrchestrationWorkClaim, execution.claim_id)
    if not last_owner:
        claim.claim_event_id = "orch:another-run"
    await session.flush()
    row = {
        "tenant_id": node.org_id,
        "engine_node_id": node.id,
        "engine_attempt": node.attempts,
        "status": "complete",
        "status_source": "authenticated_run_report",
        "terminal_outcome": "complete",
        "worker_started": True,
    }
    contract = SimpleNamespace(plan_version=1, node=SimpleNamespace(executor=SimpleNamespace(role="operate", persona="operations")))
    monkeypatch.setattr("src.orchestration.delivery_adoption.observe_historical_delivery", AsyncMock(return_value=None))
    monkeypatch.setattr("src.orchestration.results.run_result_for_assignment", AsyncMock(return_value=row))
    monkeypatch.setattr("src.orchestration.results.protected_failure_for_assignment", AsyncMock(return_value=None))
    monkeypatch.setattr("src.orchestration.policy_admission.load_in_force_policy", AsyncMock(return_value=AdmissionInputs(_policy(), 1)))
    monkeypatch.setattr("src.orchestration.operations_evidence.operational_expectations", AsyncMock(return_value=(contract, ("tools-platform",))))
    monkeypatch.setattr("src.orchestration.operations_evidence.observed_operations_evidence", AsyncMock(return_value=(None, "evidence_missing")))
    return node, run_id, execution


async def test_a_completed_worker_whose_evidence_is_missing_is_retried(session, monkeypatch):
    node, run_id, execution = await _completed_operations_run_without_evidence(session, monkeypatch, last_owner=True)

    report = await observe_results(session, run_store=object())

    await session.refresh(node)
    await session.refresh(execution)
    assert node.state == "failed", report.reasons
    assert execution.status == "concluded"
    settled = (
        await session.scalars(
            select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id, OrchestrationDecision.to_state == "failed")
        )
    ).first()
    payload = json.loads(settled.reason)
    assert payload["operations_retry"] is True and payload["run_id"] == run_id


async def test_a_claim_released_by_another_run_still_refuses_the_settlement(session, monkeypatch):
    node, run_id, execution = await _completed_operations_run_without_evidence(session, monkeypatch, last_owner=False)

    report = await observe_results(session, run_store=object())

    await session.refresh(node)
    assert node.state == "awaiting_merge" and report.waiting == 1
    assert "no longer owns" in report.reasons[node.id]


async def test_the_operations_retry_is_scheduled_after_a_completed_release(session, monkeypatch):
    """Recovery (#7826 second half): the settled run's claim was released `completed`, which the
    recovery owner check used to refuse as work_owner_changed (X05b and door3, 18:22Z)."""
    from datetime import UTC, datetime, timedelta

    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", "true")
    monkeypatch.setenv("AGENT_RUN_CREDENTIAL_KEY", "test-only-signing-key")
    node, run_id, execution = await _completed_operations_run_without_evidence(session, monkeypatch, last_owner=True)
    await prepare_run_report(
        session,
        {
            "message_id": run_id,
            "tenant_id": node.org_id,
            "persona": "operations",
            "source_ref": {"repo": "aws-e/adp", "installation_id": 42, "provider_repository_id": 123},
            "orchestration": {"node_id": node.id, "flow_id": node.flow_id, "attempt": node.attempts, "executor_role": "operate"},
            "handoff_expect": {
                "accepted_plan_version": 1,
                "execution_id": execution.id,
                "claim_id": execution.claim_id,
                "claim_generation": execution.claim_generation,
            },
        },
    )
    await session.flush()
    contract = SimpleNamespace(
        plan_version=1,
        node=SimpleNamespace(executor=SimpleNamespace(role="operate", persona="operations"), repository_identity="aws-e/adp"),
    )
    monkeypatch.setattr("src.orchestration.accepted_node.accepted_node_contract", AsyncMock(return_value=contract))
    monkeypatch.setattr("src.orchestration.developer_recovery.utcnow", lambda: datetime.now(UTC) + timedelta(seconds=600))
    from src.orchestration.execution_policy import Action
    from tests.orchestration.test_policy_admission import _limits

    # The fixture's story is on attempt 4; keep the accepted limit above it so the
    # recovery decision under test is the claim-owner check, not attempts_exhausted.
    monkeypatch.setattr(
        "src.orchestration.developer_recovery.load_in_force_policy",
        AsyncMock(
            return_value=AdmissionInputs(_policy(limits=_limits(max_attempts_per_node=10), allowed_actions=[Action.OPERATE, Action.REPAIR]), 1)
        ),
    )
    monkeypatch.setattr(
        "src.orchestration.developer_recovery.run_result_for_assignment",
        AsyncMock(return_value={"status": "complete", "terminal_outcome": "complete", "persona": "operations"}),
    )

    report = await observe_results(session, run_store=object())
    await session.refresh(node)
    assert node.state == "failed", report.reasons

    recovered = await recover_failed_developers(session)
    checks = (
        await session.scalars(
            select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id, OrchestrationDecision.kind == "developer_retry_checked")
        )
    ).all()
    assert recovered == 1, [check.reason for check in checks]
    await session.refresh(node)
    assert node.state == "ready"
    scheduled = (
        await session.scalars(
            select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id, OrchestrationDecision.kind == "operations_retry_scheduled")
        )
    ).first()
    assert scheduled is not None


async def test_recovery_keeps_an_operations_assignment_fenced_to_a_preserved_plan_version(session, monkeypatch):
    """#7821 second site: X05b (fenced v47, in force v49 after unrelated amendments) was refused
    operations_assignment_changed by recovery after #7822 fixed the evidence check. The lineage
    predicate is shared; here only its wiring is exercised (the predicate itself is covered in
    test_operations_evidence)."""
    from datetime import UTC, datetime, timedelta

    from src.orchestration.execution_policy import Action
    from tests.orchestration.test_policy_admission import _limits

    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", "true")
    monkeypatch.setenv("AGENT_RUN_CREDENTIAL_KEY", "test-only-signing-key")
    node, run_id, execution = await _completed_operations_run_without_evidence(session, monkeypatch, last_owner=True)
    await prepare_run_report(
        session,
        {
            "message_id": run_id,
            "tenant_id": node.org_id,
            "persona": "operations",
            "source_ref": {"repo": "aws-e/adp", "installation_id": 42, "provider_repository_id": 123},
            "orchestration": {"node_id": node.id, "flow_id": node.flow_id, "attempt": node.attempts, "executor_role": "operate"},
            "handoff_expect": {
                "accepted_plan_version": 1,  # fenced to the version it was dispatched under …
                "execution_id": execution.id,
                "claim_id": execution.claim_id,
                "claim_generation": execution.claim_generation,
            },
        },
    )
    await session.flush()
    contract = SimpleNamespace(
        plan_version=2,  # … while the in-force plan has moved on
        node=SimpleNamespace(executor=SimpleNamespace(role="operate", persona="operations"), repository_identity="aws-e/adp"),
    )
    monkeypatch.setattr("src.orchestration.accepted_node.accepted_node_contract", AsyncMock(return_value=contract))
    monkeypatch.setattr("src.orchestration.developer_recovery.utcnow", lambda: datetime.now(UTC) + timedelta(seconds=600))
    monkeypatch.setattr(
        "src.orchestration.developer_recovery.load_in_force_policy",
        AsyncMock(
            return_value=AdmissionInputs(_policy(limits=_limits(max_attempts_per_node=10), allowed_actions=[Action.OPERATE, Action.REPAIR]), 2)
        ),
    )
    monkeypatch.setattr(
        "src.orchestration.developer_recovery.run_result_for_assignment",
        AsyncMock(return_value={"status": "complete", "terminal_outcome": "complete", "persona": "operations"}),
    )
    monkeypatch.setattr("src.orchestration.plan_lineage.execution_plan_matches", AsyncMock(return_value=True))
    preserved = AsyncMock(return_value=True)
    monkeypatch.setattr("src.orchestration.operations_evidence.assignment_version_preserved", preserved)

    report = await observe_results(session, run_store=object())
    await session.refresh(node)
    assert node.state == "failed", report.reasons

    assert await recover_failed_developers(session) == 1
    preserved.assert_awaited_once()
    assert preserved.await_args.kwargs == {"fenced": 1, "in_force": 2}
    await session.refresh(node)
    assert node.state == "ready"
