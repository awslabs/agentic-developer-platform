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
        if not evidence.startswith("advisory"):
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
        assert result.advanced == int(evidence == "failed")
        assert node.state == ("failed" if evidence == "failed" else "running")
        merged.assert_not_called()
        if not evidence.startswith("advisory"):
            advisory.get.assert_not_called()
        if evidence == "release_refused":
            execution = await db.get(OrchestrationExecution, cycle.execution.id)
            claim = await db.get(OrchestrationWorkClaim, cycle.identity.claim_id)
            assert execution.status != "concluded" and claim.state == "held"
        if evidence != "failed":
            return
        decisions = list((await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "result_observed"))).all())
        assert len(decisions) == 1
        assert decisions[0].from_state == "running" and decisions[0].to_state == "failed"
        assert json.loads(decisions[0].reason)["run_id"] == cycle.root
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
