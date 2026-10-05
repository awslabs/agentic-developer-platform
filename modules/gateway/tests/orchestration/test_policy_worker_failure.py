"""Authenticated worker failures remain recoverable under delivery policy."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from src.admin.config import AdminRole
from src.orchestration.controls import ResumeRequest, resume_node
from src.orchestration.models import OrchestrationAction, OrchestrationDecision, OrchestrationExecution, OrchestrationNode, OrchestrationWorkClaim
from src.orchestration.results import observe_results
from src.orchestration.run_reports import OrchestrationRunReport, prepare_run_report, record_terminal
from tests.orchestration.test_review_cycle import ORG, REPO, cycle, pg_server, pg_url, store  # noqa: F401


@pytest.mark.parametrize(
    "evidence",
    [
        "failed",
        "protected_failed",
        "protected_failed_after_handoff",
        "protected_failed_with_invalid_handoff",
        "protected_released_failed",
        "protected_released_startup_cancelled",
        "protected_released_other_run",
        "protected_released_new_generation",
        "protected_released_completed",
        "unfinished",
        "wrong_attempt",
        "successor_active",
        "successor_after_observation",
        "successor_queued",
        "pending_action",
        "release_refused",
        "claim_changed",
        "advisory_failed",
        "advisory_complete",
    ],
)
async def test_policy_failure_requires_current_authenticated_terminal_and_preserves_resume(cycle, monkeypatch, evidence):  # noqa: F811
    monkeypatch.setenv("AGENT_RUN_CREDENTIAL_KEY", "test-only-signing-key")
    # This test exercises ordinary engine-owned recovery, with no lane adoption.
    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "false")
    envelope = {
        "message_id": cycle.root,
        "tenant_id": ORG,
        "persona": "developer",
        "source_ref": {"repo": REPO, "installation_id": 42, "provider_repository_id": 123},
        "orchestration": {"node_id": cycle.node.id, "flow_id": cycle.flow.id, "attempt": 1},
        "pr_binding_required": True,
    }
    async with cycle.factory() as db:
        execution = await db.get(OrchestrationExecution, cycle.execution.id)
        # The ordinary cases failed before a handoff. Only the explicit late
        # cleanup case retains the fixture's real committed handoff receipt.
        if evidence != "protected_failed_after_handoff":
            execution.handoff_receipt_ref = "invalid-receipt" if evidence == "protected_failed_with_invalid_handoff" else None
        db.add(
            OrchestrationDecision(
                org_id=ORG,
                flow_id=cycle.flow.id,
                node_id=cycle.node.id,
                kind="node_dispatched",
                actor_id="system:orchestration-dispatch",
                actor_kind="service",
                actor_role="engine",
                reason=json.dumps({"attempt": 1, "run_id": cycle.root, "arrived_at": "2026-09-21T00:00:00Z"}),
            )
        )
        if not evidence.startswith(("advisory", "protected")):
            report = await prepare_run_report(db, envelope)
            if evidence != "unfinished":
                record_terminal(report, "failed")
            if evidence == "wrong_attempt":
                report.terminal_receipt = {**report.terminal_receipt, "attempt": 2}
        if evidence in {"successor_active", "claim_changed"}:
            claim = await db.get(OrchestrationWorkClaim, cycle.identity.claim_id)
            if evidence == "successor_active":
                claim.active_run_id = "orch:successor-reviewer"
            else:
                claim.generation += 1
        if evidence.startswith("protected_released"):
            # Seed the state left by the protected terminal callback. The cycle
            # fixture also has a handoff, which is absent in the failed live run.
            claim = await db.get(OrchestrationWorkClaim, cycle.identity.claim_id)
            claim.state = "released"
            claim.active_run_id = None
            claim.release_reason = "failed"
            if evidence == "protected_released_other_run":
                claim.claim_event_id = "orch:other"
            if evidence == "protected_released_new_generation":
                claim.generation += 1
            if evidence == "protected_released_completed":
                claim.release_reason = "completed"
        if evidence == "pending_action":
            execution = await db.get(OrchestrationExecution, cycle.execution.id)
            execution.pending_action_key = "unknown-effect"
        if evidence == "successor_queued":
            db.add(
                OrchestrationAction(
                    org_id=ORG,
                    execution_id=cycle.execution.id,
                    operation_key="queued-successor",
                    kind="review_cycle_dispatch",
                    status="succeeded",
                    attempt=1,
                )
            )
        await db.commit()
    if evidence.startswith("protected"):
        monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
        protected = Mock()
        protected._read.return_value = {
            "status": {"S": "completed"},
            "terminal_outcome": {"S": "failed"},
            "tenant_id": {"S": ORG},
            "invocation_id": {"S": cycle.root},
            "flow_id": {"S": cycle.flow.id},
            "orchestration_node_id": {"S": cycle.node.id},
            "orchestration_node_attempt": {"N": "1"},
        }
        if evidence == "protected_released_startup_cancelled":
            protected._read.return_value.update(status={"S": "cancelled"}, work_claim_cancellation={"S": "startup_deadline_exceeded"})
            protected._read.return_value.pop("terminal_outcome")
        monkeypatch.setattr("src.agentauth.engine.get_engine_authority_writer", lambda: SimpleNamespace(store=protected))
    advisory = Mock()
    advisory.get.return_value = {
        "tenant_id": ORG,
        "engine_node_id": cycle.node.id,
        "engine_attempt": 1,
        "status": "complete" if evidence == "advisory_complete" else "failed",
        # Even a forged field in advisory storage cannot make it authenticated.
        "status_source": "authenticated_run_report",
    }
    merged = AsyncMock(return_value=("https://github.com/org/repo/pull/77", ""))
    monkeypatch.setattr("src.orchestration.results._story_evidence", merged)
    if evidence == "release_refused":
        from src.orchestration.work_claims import Disposition

        monkeypatch.setattr("src.orchestration.work_claims.release_work", AsyncMock(return_value=SimpleNamespace(disposition=Disposition.BLOCKED)))
    if evidence == "successor_after_observation":
        from src.orchestration import results

        resolve_identity = results.current_identity

        async def transfer_before_authority_lock(session, **kwargs):
            identity = await resolve_identity(session, **kwargs)
            async with cycle.factory() as concurrent:
                claim = await concurrent.get(OrchestrationWorkClaim, identity.claim_id)
                claim.active_run_id = "orch:concurrent-successor"
                await concurrent.commit()
            return identity

        monkeypatch.setattr(results, "current_identity", transfer_before_authority_lock)
    async with cycle.factory() as db:
        result = await observe_results(db, run_store=advisory)
        await db.commit()
        node = await db.get(OrchestrationNode, cycle.node.id)
        assert node.attempts == 1
        assert result.errors == int(evidence in {"wrong_attempt", "release_refused"})
        assert result.advanced == int(
            evidence
            in {
                "failed",
                "protected_failed",
                "protected_failed_with_invalid_handoff",
                "protected_released_failed",
                "protected_released_startup_cancelled",
            }
        )
        assert node.state == (
            "failed"
            if evidence
            in {
                "failed",
                "protected_failed",
                "protected_failed_with_invalid_handoff",
                "protected_released_failed",
                "protected_released_startup_cancelled",
            }
            else "running"
        )
        merged.assert_not_called()
        if evidence == "protected_failed_after_handoff":
            execution = await db.get(OrchestrationExecution, cycle.execution.id)
            claim = await db.get(OrchestrationWorkClaim, cycle.identity.claim_id)
            assert result.waiting == 1
            assert execution.handoff_receipt_ref and execution.status == "awaiting_external"
            assert execution.next_check_at is not None and claim.state == "held"
        if not evidence.startswith("advisory"):
            advisory.get.assert_not_called()
        if evidence == "release_refused":
            execution = await db.get(OrchestrationExecution, cycle.execution.id)
            claim = await db.get(OrchestrationWorkClaim, cycle.identity.claim_id)
            assert execution.status != "concluded" and claim.state == "held"
        if evidence not in {
            "failed",
            "protected_failed",
            "protected_failed_with_invalid_handoff",
            "protected_released_failed",
            "protected_released_startup_cancelled",
        }:
            return
        decisions = list((await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "result_observed"))).all())
        assert len(decisions) == 1
        assert decisions[0].from_state == "running" and decisions[0].to_state == "failed"
        assert json.loads(decisions[0].reason)["run_id"] == cycle.root
        if evidence == "protected_released_startup_cancelled":
            assert "Worker did not start" in decisions[0].reason
        access = SimpleNamespace(check_permission=AsyncMock(), get_user_role=AsyncMock(return_value=(AdminRole.ORG_ADMIN, None)))
        resumed = await resume_node(
            node.id,
            ResumeRequest(reason="Provider startup defect repaired; retry original story"),
            SimpleNamespace(org_id=ORG, user_id="human"),
            access,
            db,
            Mock(),
        )
        assert resumed.from_state == "failed" and resumed.state == "ready"
        assert resumed.actor_kind == "human"
        assert node.attempts == 1  # Only dispatch can consume the next attempt.
        original = await db.get(OrchestrationRunReport, cycle.root)
        if evidence.startswith("protected"):
            assert original is None
        else:
            assert original.terminal_receipt["outcome"] == "failed"
            assert original.terminal_receipt["attempt"] == 1
        execution = await db.get(OrchestrationExecution, cycle.execution.id)
        assert execution.status == "concluded" and execution.next_check_at is None
        claim = await db.get(OrchestrationWorkClaim, cycle.identity.claim_id)
        assert claim.state == "released" and claim.release_reason == "failed"
        # Exercise real next-run lane admission: READY alone must not leave an
        # outstanding execution holding the original worker's claim forever.
        from src.orchestration.dispatch_pass import attempt_run_id
        from src.orchestration.work_admission import admit
        from src.orchestration.work_claims import ClaimOwner, OwnerKind

        next_run = attempt_run_id(node.id, 2)
        admitted = await admit(
            db,
            org_id=ORG,
            repository_id=123,
            issue=43,
            owner=ClaimOwner(OwnerKind.ENGINE_FLOW, cycle.flow.id),
            invocation_id=next_run,
        )
        assert admitted["invocation_id"] == next_run
        assert admitted["generation"] == cycle.identity.claim_generation + 1
        assert claim.active_run_id == next_run


@pytest.mark.parametrize(
    "mutation",
    [
        {"terminal_outcome": {"S": "complete"}},
        {"status": {"S": "active"}},
        {"orchestration_node_attempt": {"N": "2"}},
        {"flow_id": {"S": "other"}},
        {"tenant_id": {"S": "other"}},
    ],
)
async def test_protected_failure_rejects_success_active_and_wrong_scope(mutation):
    from src.orchestration.results import protected_failure_for_assignment

    node = SimpleNamespace(org_id="tenant", flow_id="flow", id="node", attempts=1)
    authority_store = Mock()
    authority_store._read.return_value = {
        "status": {"S": "completed"},
        "terminal_outcome": {"S": "failed"},
        "tenant_id": {"S": "tenant"},
        "invocation_id": {"S": "run"},
        "flow_id": {"S": "flow"},
        "orchestration_node_id": {"S": "node"},
        "orchestration_node_attempt": {"N": "1"},
        **mutation,
    }
    call = protected_failure_for_assignment(node=node, dispatch={"run_id": "run"}, store=authority_store)
    if "status" in mutation or "terminal_outcome" in mutation:
        assert await call is None
    else:
        with pytest.raises(ValueError, match="protected failure"):
            await call


async def test_released_failure_authority_only_allows_conclusion(cycle):  # noqa: F811
    from src.orchestration.execution_state import ExecutionPhase, ExecutionStatus
    from src.orchestration.execution_store import ExecutionStoreError, PhaseAdvance, advance_execution, load_execution

    async with cycle.factory() as db:
        claim = await db.get(OrchestrationWorkClaim, cycle.identity.claim_id)
        claim.state = "released"
        claim.active_run_id = None
        claim.release_reason = "failed"
        await db.flush()
        ordinary = await load_execution(db, identity=cycle.identity)
        assert ordinary.reason == "claim_not_held"
        with pytest.raises(ExecutionStoreError, match="only permits terminal conclusion"):
            await advance_execution(
                db,
                identity=cycle.identity,
                released_failure_run_id=cycle.root,
                advance=PhaseAdvance(
                    phase=ExecutionPhase.ADMITTED, status=ExecutionStatus.AWAITING_EXTERNAL, expected_revision=cycle.execution.revision
                ),
            )


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ({}, True),
        ({"workload_binding": {"S": "pod"}}, False),
        ({"work_claim_cancellation": {"S": "other"}}, False),
        ({"status": {"S": "pending"}}, False),
        ({"orchestration_node_attempt": {"N": "2"}}, "refused"),
    ],
)
async def test_startup_cancellation_requires_watchdog_fence_and_exact_assignment(mutation, expected):
    from src.orchestration.results import protected_failure_for_assignment

    node = SimpleNamespace(org_id="tenant", flow_id="flow", id="node", attempts=1)
    authority_store = Mock()
    authority_store._read.return_value = {
        "status": {"S": "cancelled"},
        "work_claim_cancellation": {"S": "startup_deadline_exceeded"},
        "tenant_id": {"S": "tenant"},
        "invocation_id": {"S": "run"},
        "flow_id": {"S": "flow"},
        "orchestration_node_id": {"S": "node"},
        "orchestration_node_attempt": {"N": "1"},
        **mutation,
    }
    call = protected_failure_for_assignment(node=node, dispatch={"run_id": "run"}, store=authority_store)
    if expected == "refused":
        with pytest.raises(ValueError, match="protected failure"):
            await call
    else:
        result = await call
        assert (result is not None) == expected
        if result:
            assert result["failure_reason"] == "worker_startup_deadline_exceeded"
            assert result["terminal_outcome"] == "cancelled"
