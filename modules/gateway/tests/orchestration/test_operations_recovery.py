"""An exited operations worker can retry only with a settled claim and in-force policy."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.agentauth.run_report_routes import FailureDetails
from src.orchestration import operations_repair
from src.orchestration.accepted_node import operational_revision_inputs
from src.orchestration.developer_recovery import recover_developer, recovery_id, retry_context
from src.orchestration.dispatch_pass import attempt_run_id
from src.orchestration.execution_policy import Action
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationWorkClaim,
)
from src.orchestration.policy_admission import AdmissionInputs
from src.orchestration.results import observe_results
from src.orchestration.run_reports import OrchestrationRunReport, prepare_run_report, record_terminal
from src.orchestration.state import ActorKind, NodeState, transition
from tests.orchestration.test_report_dispatch import engine, prepare, reporting, session, session_factory  # noqa: F401


@pytest.fixture
async def work(session, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ADP_ENGINE_OPERATIONS_EXECUTOR", "true")
    node = await prepare(session)
    node.state, node.attempts = "running", 1
    flow = await session.get(OrchestrationFlow, node.flow_id)
    flow.state = "running"
    session.add(OrchestrationAcceptedPlan(org_id=node.org_id, flow_id=node.flow_id, version=1, plan_hash="b" * 64, plan_document={}))
    policy = SimpleNamespace(
        allowed_actions={Action.OPERATE, Action.REPAIR},
        human_gates=set(),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        limits=SimpleNamespace(max_attempts_per_node=5, max_wall_clock_seconds=4 * 3600),
        policy_hash="b" * 64,
        repository_ids={"aws-e/adp"},
        repair_paths={"deploy/**"},
    )
    monkeypatch.setattr("src.orchestration.policy_admission.load_in_force_policy", AsyncMock(return_value=AdmissionInputs(policy, 1)))
    monkeypatch.setattr("src.orchestration.developer_recovery.load_in_force_policy", AsyncMock(return_value=AdmissionInputs(policy, 1)))
    monkeypatch.setattr(operations_repair, "load_in_force_policy", AsyncMock(return_value=AdmissionInputs(policy, 1)))
    monkeypatch.setattr("src.orchestration.results.resolve_installation_id", AsyncMock(return_value=42))
    contract = SimpleNamespace(
        plan_version=1,
        node=SimpleNamespace(
            executor=SimpleNamespace(role="operate", persona="operations"),
            repository_identity="aws-e/adp",
            operational_release_binding=None,
            evidence_expectations=["runtime_verified:tools-facade"],
            required_inputs=[
                SimpleNamespace(source=prefix + value)
                for prefix, value in (
                    ("deployment_workflow:", "deploy.yml"),
                    ("deployment_source_revision:", "a" * 40),
                    ("deployment_definition_revision:", "b" * 40),
                    ("deployment_branch:", "main"),
                )
            ],
        ),
    )
    monkeypatch.setattr("src.orchestration.operations_evidence.accepted_node_contract", AsyncMock(return_value=contract))
    monkeypatch.setattr(operations_repair, "accepted_node_contract", AsyncMock(return_value=contract))
    monkeypatch.setattr("src.orchestration.developer_recovery.accepted_node_contract", AsyncMock(return_value=contract), raising=False)
    monkeypatch.setattr("src.orchestration.accepted_node.accepted_node_contract", AsyncMock(return_value=contract))
    return SimpleNamespace(node=node, flow=flow, policy=policy, contract=contract)


async def exited(database, work, attempt, *, diagnosis=None, repair=False, failure=None):
    node = work.node
    node.state, node.attempts = "running", attempt
    run_id = attempt_run_id(node.id, attempt)
    claim = await database.get(OrchestrationWorkClaim, "operations-claim")
    if claim is None:
        claim = OrchestrationWorkClaim(
            id="operations-claim",
            org_id=node.org_id,
            provider_repository_id=123,
            issue_number=4196,
            owner_kind="engine_flow",
            owner_ref=node.flow_id,
            state="held",
            generation=attempt,
            active_run_id=run_id,
            claim_event_id=run_id,
        )
        database.add(claim)
    else:
        claim.state, claim.release_reason, claim.active_run_id = "held", None, run_id
        claim.generation, claim.claim_event_id = attempt, run_id
    execution = OrchestrationExecution(
        org_id=node.org_id,
        flow_id=node.flow_id,
        node_id=node.id,
        cycle=attempt,
        phase="delivering",
        status="runnable",
        next_check_at=datetime.now(UTC),
        accepted_plan_version=1,
        claim_id=claim.id,
        claim_generation=attempt,
    )
    database.add(execution)
    await database.flush()
    database.add(
        OrchestrationDecision(
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            kind="node_dispatched",
            actor_id="engine",
            actor_role="engine",
            actor_kind="service",
            reason=json.dumps(
                {
                    "attempt": attempt,
                    "run_id": run_id,
                    "repo": "aws-e/adp",
                    "issue": 4196,
                    "arrived_at": datetime.now(UTC).isoformat(),
                    "conclusion_authority": "evidence",
                }
            ),
        )
    )
    row = await prepare_run_report(
        database,
        {
            "message_id": run_id,
            "tenant_id": node.org_id,
            "persona": "operations",
            "source_ref": {"repo": "aws-e/adp", "installation_id": 42, "provider_repository_id": 123},
            "orchestration": {
                "node_id": node.id,
                "flow_id": node.flow_id,
                "attempt": attempt,
                "executor_role": "operate",
                "deployment_revisions": {
                    "source_revision": work.contract.node.required_inputs[1].source.split(":", 1)[1],
                    "definition_revision": work.contract.node.required_inputs[2].source.split(":", 1)[1],
                    "default_branch": "main",
                },
            },
            "handoff_expect": {"accepted_plan_version": 1, "execution_id": execution.id, "claim_id": claim.id, "claim_generation": attempt},
        },
    )
    report = f"Observed cycle {attempt}"
    row.operations_report_receipt = {
        "recorded": True,
        "run_id": run_id,
        "attempt": attempt,
        "node_id": node.id,
        "receipt_id": f"operations-report:{run_id}",
        "report": report,
        "sha256": hashlib.sha256(report.encode()).hexdigest(),
    }
    record_terminal(row, "failed" if failure else "complete", failure=failure)
    if diagnosis:
        database.add(
            OrchestrationAction(
                org_id=node.org_id,
                execution_id=execution.id,
                kind="operations_task",
                operation_key="operations-task:1:diagnose",
                status="succeeded",
                attempt=1,
                detail={"state": "done", "diagnosis_code": diagnosis},
            )
        )
    if repair:
        pins = await operations_repair.repair_revisions(
            database, node_id=node.id, org_id=node.org_id, plan_version=1, cycle=attempt, pins=operational_revision_inputs(work.contract)
        )
        before_source, before_definition = pins["source_revision"], pins["definition_revision"]
        head = f"{attempt:x}" * 40
        database.add(
            OrchestrationAction(
                org_id=node.org_id,
                execution_id=execution.id,
                kind="operations_repair_pr",
                operation_key=f"operations-repair-pr:{attempt}:{head}:{before_source}",
                status="succeeded",
                attempt=1,
                detail={"pull_number": attempt, "head": head, "base": before_source, "checks_verified": True, "repair_cycle": 1},
                receipt_ref=f"github/aws-e/adp/pull/{attempt}",
            )
        )
        database.add(
            OrchestrationAction(
                org_id=node.org_id,
                execution_id=execution.id,
                kind="operations_repair_merge",
                operation_key=f"operations-repair:{attempt}:{head}",
                status="succeeded",
                attempt=1,
                detail={
                    "pull_number": attempt,
                    "head": head,
                    "merge_sha": head,
                    "repository": "aws-e/adp",
                    "default_branch": "main",
                    "policy_hash": work.policy.policy_hash,
                    "repair_paths": ["deploy/**"],
                    "workflows": ["deploy.yml"],
                    "before_source_revision": before_source,
                    "before_definition_revision": before_definition,
                    "claim_generation": attempt,
                    "run_id": run_id,
                },
                receipt_ref=f"github/aws-e/adp/pull/{attempt}",
            )
        )
    await database.flush()
    return claim, execution


async def test_unmerged_repair_blocks_dispatch_until_qualified_merge_of_latest_head(session, work):  # noqa: F811
    _, execution = await exited(session, work, 1, diagnosis="cli_verifier", repair=True)
    pins = operational_revision_inputs(work.contract)

    async def revisions(*, mutate=True, cycle=1, plan_version=1):
        return await operations_repair.repair_revisions(
            session,
            node_id=work.node.id,
            org_id=work.node.org_id,
            plan_version=plan_version,
            cycle=cycle,
            pins=pins,
            require_merged=mutate,
        )

    assert (await revisions())["source_revision"] == "1" * 40
    rows = (await session.scalars(select(OrchestrationAction).where(OrchestrationAction.execution_id == execution.id))).all()
    merge = next(row for row in rows if row.kind == "operations_repair_merge")
    merge.status = "failed"
    await session.flush()
    # A published/green PR or worker progress report is not merge authority.
    session.add(
        OrchestrationAction(
            org_id=work.node.org_id,
            execution_id=execution.id,
            kind="operations_task",
            operation_key="operations-task:1:publish",
            status="succeeded",
            attempt=1,
            detail={"state": "done"},
        )
    )
    await session.flush()
    with pytest.raises(operations_repair.RepairMergeRequiredError):
        await revisions()
    assert await revisions(mutate=False) == pins
    # A retry inherits the unresolved publication.
    with pytest.raises(operations_repair.RepairMergeRequiredError):
        await revisions(cycle=2)
    # Advancing a version cannot silently discard the outstanding repair.
    with pytest.raises(operations_repair.RepairMergeRequiredError):
        await revisions(plan_version=2)
    merge.status = "succeeded"
    session.add(
        OrchestrationAction(
            org_id=work.node.org_id,
            execution_id=execution.id,
            kind="operations_repair_pr",
            operation_key=f"operations-repair-pr:1:{'2' * 40}:{'a' * 40}",
            status="succeeded",
            attempt=1,
            receipt_ref="github/aws-e/adp/pull/1",
            detail={"pull_number": 1, "head": "2" * 40, "base": "a" * 40},
        )
    )
    await session.flush()
    with pytest.raises(operations_repair.RepairMergeRequiredError):
        await revisions()
    merge.detail = {**merge.detail, "head": "2" * 40, "merge_sha": "3" * 40}
    merge.operation_key = f"operations-repair:1:{'2' * 40}"
    await session.flush()
    assert (await revisions())["source_revision"] == "3" * 40
    # A latest-head match cannot bypass the existing merge qualification fences.
    merge.detail = {**merge.detail, "policy_hash": "different-policy"}
    await session.flush()
    assert await revisions() is None


async def test_three_report_cycles_retry_then_observe_runtime_receipt(session, session_factory, work, monkeypatch):  # noqa: F811
    now = datetime.now(UTC)
    accepted_pins = operational_revision_inputs(work.contract)
    monkeypatch.setattr("src.orchestration.developer_recovery.utcnow", lambda: now + timedelta(minutes=40))
    for attempt in (1, 2, 3):
        claim, execution = await exited(session, work, attempt, diagnosis=f"runtime_{attempt}", repair=True)
        observed = await observe_results(session)
        assert observed.advanced == 1, observed
        assert work.node.state == "failed" and execution.status == "concluded"
        assert claim.state == "released" and claim.release_reason == "failed"
        await session.commit()
        async with session_factory() as restarted:
            node = await restarted.get(type(work.node), work.node.id)
            assert await recover_developer(restarted, node, now=now) == "retry_backoff"
            assert await recover_developer(restarted, node, now=now + timedelta(minutes=40)) == "scheduled"
            assert await recover_developer(restarted, node, now=now + timedelta(minutes=40)) == "not_a_failed_developer"
            assert await operations_repair.repair_revisions(
                restarted, node_id=node.id, org_id=node.org_id, plan_version=1, cycle=attempt + 1, pins=accepted_pins
            ) == {**accepted_pins, "source_revision": f"{attempt:x}" * 40, "definition_revision": f"{attempt:x}" * 40}
            assert operational_revision_inputs(work.contract) == accepted_pins
            await restarted.commit()
        work.node = await session.get(type(work.node), work.node.id)
        await session.refresh(work.node)
        assert work.node.state == "ready"
        assert (await retry_context(session, work.node, attempt))["reason"] == "operations_runtime_unverified"
        assert await session.get(OrchestrationDecision, recovery_id(work.node.id, attempt, operations=True))
    claim, execution = await exited(session, work, 4)
    observed_at = datetime.now(UTC)
    session.add(
        OrchestrationAction(
            org_id=work.node.org_id,
            execution_id=execution.id,
            kind="deployment_workflow",
            operation_key="verified-workflow",
            status="succeeded",
            attempt=1,
            detail={"runtime_verified": True},
        )
    )
    revision = "3" * 40
    session.add(
        OrchestrationAction(
            org_id=work.node.org_id,
            execution_id=execution.id,
            kind="deployment_verification",
            operation_key="runtime-receipt",
            receipt_ref="runtime-receipt",
            status="succeeded",
            attempt=1,
            detail={
                "deployment_receipt": {
                    "org_id": work.node.org_id,
                    "execution_id": execution.id,
                    "node_id": work.node.id,
                    "flow_id": work.node.flow_id,
                    "cycle": 4,
                    "accepted_plan_version": 1,
                    "claim_id": claim.id,
                    "claim_generation": 4,
                    "operation_key": "runtime-receipt",
                    "repo": "aws-e/adp",
                    "source_revision": revision,
                    "actual_revision": revision,
                    "merge_operation_key": "merge-3",
                    "workflow_operation_keys": ["verified-workflow"],
                    "manifest_entry_ids": ["tools-facade"],
                    "targets": [{"component": "tools-facade"}],
                    "delivery_complete": True,
                    "observed_at": observed_at.isoformat(),
                    "valid_until": (observed_at + timedelta(hours=1)).isoformat(),
                    "components": [
                        {
                            "component": "tools-facade",
                            "actual_revision": revision,
                            "artifact_hash": "f" * 64,
                            "image_digest": "sha256:" + "f" * 64,
                            "healthy": True,
                            "evidence_ref": "github/actions/runs/1/artifacts/1",
                            "observed_at": observed_at.isoformat(),
                            "pod_uids": ["test-pod"],
                        }
                    ],
                }
            },
        )
    )
    assert (await observe_results(session)).advanced == 1
    assert work.node.state == "passed"
    assert len((await session.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == "operations_repair_merge"))).all()) == 3
    checks = (await session.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == "operations_repair_pr"))).all()
    assert len(checks) == 3 and all(action.detail["checks_verified"] for action in checks)


@pytest.mark.parametrize("category", ["transport", "policy", "provider_refusal", "contract", "cancelled"])
async def test_failed_operations_worker_recovers_after_restart_with_backoff(session, session_factory, work, category):  # noqa: F811
    failure = FailureDetails(category=category, exit_code=75).model_dump()
    claim, execution = await exited(session, work, 1, failure=failure)
    assert (await observe_results(session)).advanced == 1
    assert work.node.state == "failed" and execution.status == "concluded"
    assert claim.state == "released" and claim.release_reason == "failed"
    await session.commit()
    async with session_factory() as restarted:
        node = await restarted.get(type(work.node), work.node.id)
        if category != "transport":
            assert await recover_developer(restarted, node, now=datetime.now(UTC) + timedelta(minutes=31)) == "failure_requires_operator"
            assert node.state == "failed"
            return
        assert await recover_developer(restarted, node, now=datetime.now(UTC)) == "retry_backoff"
        assert await recover_developer(restarted, node, now=datetime.now(UTC) + timedelta(minutes=31)) == "scheduled"
        assert node.state == "ready" and node.attempts == 1
        await restarted.commit()
    async with session_factory() as restarted:
        decision = await restarted.get(OrchestrationDecision, recovery_id(work.node.id, 1, operations=True))
        assert decision.kind == "operations_retry_scheduled"
        assert json.loads(decision.reason)["failure_category"] == "transport"
        assert json.loads(decision.reason)["previous_exit_code"] == 75
        assert await restarted.get(OrchestrationDecision, recovery_id(work.node.id, 1)) is None
        actions = (await restarted.scalars(select(OrchestrationAction))).all()
        assert not any(action.kind == "review_cycle_dispatch" for action in actions)


async def test_automatic_recovery_switch_holds_settled_attempt_without_erasing_progress(session, work):  # noqa: F811
    from src.orchestration.recovery_control import DISABLED

    _, execution = await exited(session, work, 1, failure=FailureDetails(category="transport", exit_code=1).model_dump())
    assert (await observe_results(session)).advanced == 1
    session.add(
        OrchestrationDecision(
            org_id=work.node.org_id,
            flow_id=work.flow.id,
            kind=DISABLED,
            actor_id="owner",
            actor_kind="human",
            actor_role="owner",
            reason="Automatic recovery off",
        )
    )
    await session.flush()
    assert await recover_developer(session, work.node, now=datetime.now(UTC) + timedelta(minutes=31)) == "automatic_recovery_disabled"
    assert work.node.state == "failed" and work.node.attempts == 1
    assert execution.status == "concluded"


@pytest.mark.parametrize("diagnostic", ["turn_limit", "context_limit"])
@pytest.mark.parametrize(
    "condition,expected",
    [
        ("valid", "scheduled"),
        ("corrupt", "checkpoint_requires_reconciliation"),
        ("cap", "attempts_exhausted"),
        ("paused", "flow_not_active"),
    ],
)
async def test_runtime_boundary_continues_from_verified_checkpoint_with_original_limits(session, work, diagnostic, condition, expected):  # noqa: F811
    from src.orchestration.story_checkpoints import TaskBoardWrite, report_scope, store_checkpoint

    await exited(session, work, 1, failure=FailureDetails(category="unknown", exit_code=1).model_dump())
    report = await session.get(OrchestrationRunReport, attempt_run_id(work.node.id, 1))
    report.dispatch_metadata = {
        **report.dispatch_metadata,
        "source_ref": {**report.dispatch_metadata["source_ref"], "issue": int(work.node.issue_ref)},
    }
    text = f"Operations story ended without a success result (error_max_turns). Diagnostic: {diagnostic}. Last assistant text: Saved work."
    report.operations_report_receipt = {**report.operations_report_receipt, "report": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}
    scope = await report_scope(session, report)
    payload = TaskBoardWrite.model_validate(
        {
            "generation": 1,
            "sequence": 1,
            "observed_at": datetime.now(UTC).isoformat(),
            "text": "Completed C1; C2 remains",
            "story_checkpoint": {
                "scope": scope.model_dump(),
                "delta": {
                    "version": 1,
                    "progress_only": True,
                    "source": {
                        "issue": scope.issue,
                        "repository_id": scope.repository_id,
                        "branch": scope.branch,
                        "path": f".adp/tasks/{scope.issue}.json",
                        "sha256": "a" * 64,
                        "confirmed_source_revision": "b" * 40,
                    },
                    "updates": {"C1": {"base_task_hash": "c" * 64, "status": "done"}, "C2": {"base_task_hash": "d" * 64, "status": "open"}},
                    "additions": {},
                    "conflicts": [],
                    "history": [],
                },
            },
        }
    )
    saved = await store_checkpoint(session, report, payload)
    report.task_board_snapshot = {"story_checkpoint": saved}
    if condition == "corrupt":
        report.task_board_snapshot = {"story_checkpoint": {**saved, "revision": "f" * 64}}
    assert (await observe_results(session)).advanced == 1
    if condition == "cap":
        work.policy.limits.max_attempts_per_node = 1
    elif condition == "paused":
        work.flow.execution_paused = True
    assert await recover_developer(session, work.node, now=datetime.now(UTC) + timedelta(minutes=31)) == expected
    assert work.node.attempts == 1
    if expected == "scheduled":
        context = await retry_context(session, work.node, 1)
        assert context["reason"] == "operations_checkpoint_continuation"
        assert context["checkpoint_revision"] == saved["revision"]
        assert context["runtime_diagnostic"] == diagnostic
        assert report.task_board_snapshot["story_checkpoint"]["delta"]["updates"]["C1"]["status"] == "done"
    else:
        assert work.node.state == "failed"


@pytest.mark.parametrize(
    "block,reason",
    [
        ("claim", "work_owner_changed"),
        ("pending_effect", "execution_not_settled"),
        ("attempts", "attempts_exhausted"),
        ("expired", "policy_expired"),
        ("deadline", "flow_deadline_exhausted"),
        ("repair_gate", "repair_requires_approval"),
        ("operate_gate", "operations_requires_approval"),
        ("executor", "operations_assignment_changed"),
        ("repository", "operations_assignment_changed"),
        ("nonretryable", "failure_requires_operator"),
    ],
)
async def test_transient_operations_failure_keeps_authority_and_limit_fences(session, session_factory, work, block, reason):  # noqa: F811
    claim, execution = await exited(session, work, 1, failure=FailureDetails(category="transport", exit_code=75).model_dump())
    assert (await observe_results(session)).advanced == 1
    if block == "claim":
        claim.generation += 1
    elif block == "pending_effect":
        execution.pending_action_key = "uncertain-effect"
    elif block == "attempts":
        work.policy.limits.max_attempts_per_node = 1
    elif block == "expired":
        work.policy.expires_at = datetime.now(UTC)
    elif block == "deadline":
        work.policy.limits.max_wall_clock_seconds = 1
    elif block == "repair_gate":
        work.policy.human_gates.add(Action.REPAIR)
    elif block == "operate_gate":
        work.policy.human_gates.add(Action.OPERATE)
    elif block == "executor":
        work.contract.node.executor.persona = "agent-codex-operations"
    elif block == "repository":
        work.contract.node.repository_identity = "another/repository"
    elif block == "nonretryable":
        report = await session.get(OrchestrationRunReport, attempt_run_id(work.node.id, 1))
        report.block_code, report.retryable = "authority_refused", False
    await session.commit()
    async with session_factory() as restarted:
        node = await restarted.get(type(work.node), work.node.id)
        assert await recover_developer(restarted, node, now=datetime.now(UTC) + timedelta(minutes=31)) == reason
        assert node.state == "failed"


async def test_repeated_transport_faults_retry_without_treating_them_as_deterministic(session, session_factory, work):  # noqa: F811
    for attempt in (1, 2):
        await exited(session, work, attempt, diagnosis="transport", failure=FailureDetails(category="transport", exit_code=75).model_dump())
        assert (await observe_results(session)).advanced == 1
        await session.commit()
        async with session_factory() as restarted:
            node = await restarted.get(type(work.node), work.node.id)
            assert await recover_developer(restarted, node, now=datetime.now(UTC)) == "retry_backoff"
            assert await recover_developer(restarted, node, now=datetime.now(UTC) + timedelta(minutes=31)) == "scheduled"
            await restarted.commit()
        await session.refresh(work.node)


@pytest.mark.parametrize(
    "field,value",
    [
        ("claim_generation", 2),
        ("policy_hash", "different-policy"),
        ("workflows", ["unapproved.yml"]),
        ("repair_paths", ["unapproved/**"]),
        ("repository", "another/repo"),
        ("before_source_revision", "f" * 40),
    ],
)
async def test_historical_repair_receipts_are_validated_against_their_own_scope(session, session_factory, work, field, value):  # noqa: F811
    _, first = await exited(session, work, 1, repair=True)
    assert (await observe_results(session)).advanced == 1
    await session.commit()
    await exited(session, work, 2)
    action = await session.scalar(
        select(OrchestrationAction).where(OrchestrationAction.execution_id == first.id, OrchestrationAction.kind == "operations_repair_merge")
    )
    original = action.detail
    action.detail = {**original, field: value}
    await session.commit()
    async with session_factory() as restarted:
        assert (
            await operations_repair.repair_revisions(
                restarted, node_id=work.node.id, org_id=work.node.org_id, plan_version=1, cycle=2, pins=operational_revision_inputs(work.contract)
            )
            is None
        )
    action.detail = original
    await session.commit()
    async with session_factory() as restarted:
        pins = await operations_repair.repair_revisions(
            restarted, node_id=work.node.id, org_id=work.node.org_id, plan_version=1, cycle=2, pins=operational_revision_inputs(work.contract)
        )
        assert pins["source_revision"] == pins["definition_revision"] == "1" * 40


async def test_recovery_refuses_changed_claim_diagnosis_and_attempt_limit(session, session_factory, work):  # noqa: F811
    claim, execution = await exited(session, work, 1, diagnosis="deterministic")
    claim.active_run_id = "another-worker"
    observed = await observe_results(session)
    assert observed.advanced == 0 and work.node.state == "running"
    claim.active_run_id = attempt_run_id(work.node.id, 1)
    assert (await observe_results(session)).advanced == 1
    assert work.node.state == "failed" and claim.state == "released"
    now = datetime.now(UTC) + timedelta(minutes=31)
    await session.commit()
    work.policy.limits.max_attempts_per_node = 1
    async with session_factory() as restarted:
        node = await restarted.get(type(work.node), work.node.id)
        assert await recover_developer(restarted, node, now=now) == "attempts_exhausted"
        work.policy.limits.max_attempts_per_node = 5
        assert await recover_developer(restarted, node, now=now) == "scheduled"
        await restarted.commit()
    await session.refresh(work.node)
    claim, execution = await exited(session, work, 2, diagnosis="deterministic")
    assert (await observe_results(session)).advanced == 1
    await session.commit()
    async with session_factory() as restarted:
        node = await restarted.get(type(work.node), work.node.id)
        assert await recover_developer(restarted, node, now=now + timedelta(minutes=31)) == "deterministic_failure_unchanged"
        assert node.state == "failed"


def test_operations_retry_cannot_reopen_halt_or_bypass_settlement():
    assert not transition(NodeState.FAILED, NodeState.READY, actor_kind=ActorKind.SERVICE, reason="unverified").allowed
    assert transition(NodeState.FAILED, NodeState.READY, actor_kind=ActorKind.SERVICE, reason="settled", operations_retry_authorized=True).allowed
    assert not transition(NodeState.HALTED, NodeState.READY, actor_kind=ActorKind.SERVICE, reason="settled", operations_retry_authorized=True).allowed
