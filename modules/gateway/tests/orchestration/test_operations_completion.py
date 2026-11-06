"""A verified operations pass must atomically close its real execution ledger."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from src.orchestration.models import (
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationEnvironmentLease,
    OrchestrationExecution,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from src.orchestration.results import observe_results
from src.orchestration.run_reports import prepare_run_report, record_terminal
from tests.orchestration.test_review_cycle import ORG, REPO, cycle, pg_server, pg_url, store  # noqa: F401

CASES = [
    "held",
    "released_completed",
    "paused",
    "evidence_arrives",
    "released_failed",
    "released_abandoned",
    "released_other_run",
    "released_new_generation",
    "successor_active",
    "successor_after_observation",
    "pending_action",
    "unknown_action",
    "prepared_action",
    "dispatched_action",
    "successor_queued",
    "handoff",
    "evidence_changed",
    "release_refused",
    "settlement_refused",
]


@pytest.mark.parametrize("cycle", [{"missing_handoff": True}], indirect=True)
@pytest.mark.parametrize(
    ("case", "native_status"),
    [(case, status) for case in CASES for status in (None, "prepared", "unknown", "succeeded")]
    + [
        (case, "prepared")
        for case in (
            "native_unverified",
            "native_snapshot_changed",
            "native_detail_changed",
            "native_wrong_generation",
            "lease_generation_changed",
            "lease_owner_changed",
            "lease_publication_pending",
        )
    ],
)
async def test_operations_completion_is_atomic_and_fenced(cycle, monkeypatch, case, native_status):  # noqa: F811
    monkeypatch.setenv("AGENT_RUN_CREDENTIAL_KEY", "test-only-signing-key")
    monkeypatch.setenv("ADP_ENGINE_OPERATIONS_EXECUTOR", "true")
    envelope = {
        "message_id": cycle.root,
        "tenant_id": ORG,
        "persona": "operations",
        "source_ref": {"repo": REPO, "installation_id": 42, "provider_repository_id": 123},
        "orchestration": {"node_id": cycle.node.id, "flow_id": cycle.flow.id, "attempt": 1, "executor_role": "operate"},
    }
    async with cycle.factory() as db:
        report = await prepare_run_report(db, envelope)
        record_terminal(report, "complete")
        db.add(
            OrchestrationDecision(
                org_id=ORG,
                flow_id=cycle.flow.id,
                node_id=cycle.node.id,
                kind="node_dispatched",
                actor_id="system:orchestration-dispatch",
                actor_kind="service",
                actor_role="engine",
                reason=json.dumps({"attempt": 1, "run_id": cycle.root, "conclusion_authority": "evidence"}),
            )
        )
        execution = await db.get(OrchestrationExecution, cycle.execution.id)
        claim = await db.get(OrchestrationWorkClaim, cycle.claim.id)
        native = None
        if native_status is not None:
            from src.orchestration.deployment_manifest import PhysicalTarget, TargetEvidence
            from src.orchestration.environment_leases import LeaseHolder, acquire_lease

            connection = await db.connection()
            await connection.run_sync(OrchestrationEnvironmentLease.__table__.create)
            target = PhysicalTarget(
                provider="aws",
                account_id="000000000000",
                region="us-east-1",
                resource_kind="eks-namespace",
                resource_id="fixture-cluster/fixture-namespace",
                evidence=TargetEvidence(source="registered-aws-role:fixture", verified_at=datetime.now(UTC).isoformat()),
            )
            holder = LeaseHolder(ORG, "fixture-foundation-release", claim.generation)
            leased = await acquire_lease(db, target=target, holder=holder, manifest_entry_id="fixture", release_ref="a" * 40)
            assert leased.applied
            native = OrchestrationAction(
                org_id=ORG,
                execution_id=execution.id,
                operation_key="native-release:fixture",
                kind="operations_native_release_dispatch",
                status=native_status,
                attempt=1,
                detail={
                    "correlation": cycle.root,
                    "dispatch_started": True,
                    "release_job": {"run_id": "42", "run_attempt": "1"},
                    "shared_release": {"target_key": target.canonical_key, "owner": holder.action_id, "generation": holder.generation},
                },
            )
            db.add(native)
            if case == "native_wrong_generation":
                native.detail = {**native.detail, "shared_release": {**native.detail["shared_release"], "generation": claim.generation + 1}}
            if case.startswith("lease_"):
                lease = await db.scalar(select(OrchestrationEnvironmentLease))
                if case == "lease_generation_changed":
                    lease.owner_generation += 1
                elif case == "lease_owner_changed":
                    lease.owner_action_id = "successor-release"
                else:
                    lease.publication_pending_digest = "sha256:" + "b" * 64
        if case.startswith("released"):
            claim.state, claim.active_run_id = "released", None
            claim.release_reason = {"released_failed": "failed", "released_abandoned": "abandoned"}.get(case, "completed")
            if case == "released_other_run":
                claim.claim_event_id = "orch:other"
            if case == "released_new_generation":
                claim.generation += 1
        if case == "successor_active":
            claim.active_run_id = "orch:successor"
        if case == "pending_action":
            execution.pending_action_key = "unresolved"
        if case == "handoff":
            execution.handoff_receipt_ref = "continuation-receipt"
        if case in {"unknown_action", "prepared_action", "dispatched_action", "successor_queued"}:
            db.add(
                OrchestrationAction(
                    org_id=ORG,
                    execution_id=execution.id,
                    operation_key="outstanding",
                    kind="review_cycle_dispatch"
                    if case == "successor_queued"
                    else ("operations_native_release_dispatch" if native is not None else "deployment_dispatch"),
                    status="succeeded" if case == "successor_queued" else case.removesuffix("_action"),
                    attempt=1,
                )
            )
        if case == "paused":
            from src.orchestration.models import OrchestrationFlow

            flow = await db.get(OrchestrationFlow, cycle.flow.id)
            flow.execution_paused = True
        await db.commit()
        original_revision = execution.revision
        original_state = claim.state
        original_reason = claim.release_reason
        original_generation = claim.generation
        original_run = claim.active_run_id

    # Receipt parsing is covered by test_operations_evidence. Keep its verified
    # observation boundary controlled here; SQL, authenticated terminal reporting,
    # policy, claim locks, settlement and release are the production paths.
    monkeypatch.setattr("src.orchestration.operations_evidence.operational_expectations", AsyncMock(return_value=object()))
    monkeypatch.setattr("src.orchestration.results.resolve_installation_id", AsyncMock(return_value=42))
    reads = 0

    async def runtime_evidence(session, node, dispatch, *, expected=None, observation):
        nonlocal reads
        reads += 1
        if case == "evidence_arrives" and reads == 1:
            return None, "evidence_missing"
        if case == "successor_after_observation" and reads == 2:
            async with cycle.factory() as concurrent:
                claim = await concurrent.get(OrchestrationWorkClaim, cycle.claim.id)
                claim.active_run_id = "orch:successor"
                await concurrent.commit()
        observation["evidence_ids"] = ["runtime:changed" if case == "evidence_changed" and reads == 2 else "runtime:verified"]
        if native is not None and case != "native_unverified":
            observation["native_release"] = {
                "action_id": native.id,
                "operation_key": native.operation_key,
                "detail": deepcopy(native.detail),
                "runtime_evidence_id": "runtime:verified",
            }
            if reads == 2 and case == "native_snapshot_changed":
                observation["native_release"]["detail"]["dispatch_revision"] = "changed"
            if reads == 2 and case == "native_detail_changed":
                changed = await session.get(OrchestrationAction, native.id)
                changed.detail = {**changed.detail, "dispatch_revision": "changed"}
                await session.flush()
        return "runtime:verified", ""

    monkeypatch.setattr("src.orchestration.operations_evidence.observed_operations_evidence", runtime_evidence)
    if case == "release_refused":
        from src.orchestration.work_claims import Disposition

        monkeypatch.setattr("src.orchestration.work_claims.release_work", AsyncMock(return_value=SimpleNamespace(disposition=Disposition.BLOCKED)))
    if case == "settlement_refused":
        from src.orchestration.execution_state import OutcomeKind

        monkeypatch.setattr("src.orchestration.results.advance_execution", AsyncMock(return_value=SimpleNamespace(kind=OutcomeKind.STALE)))
    advisory = Mock()
    success = case in {"held", "released_completed", "paused", "evidence_arrives"}
    async with cycle.factory() as db:
        result = await observe_results(db, run_store=advisory)
        await db.commit()
        node = await db.get(OrchestrationNode, cycle.node.id)
        execution = await db.get(OrchestrationExecution, cycle.execution.id)
        claim = await db.get(OrchestrationWorkClaim, cycle.claim.id)
        assert result.errors == int(
            case
            in {
                "release_refused",
                "settlement_refused",
                "native_detail_changed",
                "native_wrong_generation",
                "lease_generation_changed",
                "lease_owner_changed",
                "lease_publication_pending",
            }
        )
        assert node.attempts == 1
        assert claim.generation == original_generation
        assert execution.accepted_plan_version == cycle.identity.accepted_plan_version
        assert execution.claim_generation == cycle.identity.claim_generation
        advisory.get.assert_not_called()
        decisions = list((await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "result_observed"))).all())
        if success:
            assert node.state == "passed" and result.advanced == 1
            assert execution.phase == execution.status == "concluded"
            assert execution.next_check_at is None and execution.pending_action_key is None
            assert execution.revision == original_revision + 1
            assert claim.state == "released" and claim.release_reason == "completed" and claim.active_run_id is None
            assert len(decisions) == 1
            payload = json.loads(decisions[0].reason)
            assert payload["completed_execution_id"] == execution.id
            assert payload["evidence_ids"] == ["runtime:verified"]
            assert payload["run_id"] == cycle.root
            if native is not None:
                settled = await db.get(OrchestrationAction, native.id)
                lease = await db.scalar(select(OrchestrationEnvironmentLease))
                assert settled.status == "succeeded" and settled.receipt_ref == "runtime:verified"
                assert settled.observed_at is not None
                assert settled.detail == native.detail
                assert lease.state == "free" and lease.release_reason == "completed"
                assert lease.reconciled_terminal_evidence == "runtime:verified"
                assert payload["native_release_action_id"] == native.id
                assert "native_release" not in payload
            # Subsequent sweeps cannot create another completion or consume work.
            repeated = await observe_results(db, run_store=advisory)
            await db.commit()
            assert repeated.advanced == repeated.errors == 0
            assert execution.revision == original_revision + 1
        else:
            assert node.state == ("awaiting_merge" if case in {"evidence_changed", "native_snapshot_changed"} else "running")
            assert execution.status != "concluded" and execution.revision == original_revision
            assert claim.state == original_state and claim.release_reason == original_reason
            assert claim.active_run_id == ("orch:successor" if case == "successor_after_observation" else original_run)
            assert not any(decision.to_state == "passed" for decision in decisions)
            if native is not None:
                unsettled = await db.get(OrchestrationAction, native.id)
                lease = await db.scalar(select(OrchestrationEnvironmentLease))
                assert unsettled.status == native_status and unsettled.receipt_ref is None
                assert unsettled.observed_at is None
                assert lease.state == "held" and lease.release_reason is None
