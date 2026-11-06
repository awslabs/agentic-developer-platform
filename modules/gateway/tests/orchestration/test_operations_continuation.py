"""A retained repair resumes without erasing failures or admitting another worker."""

# ruff: noqa: F811
import copy
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.agentauth.grants import AgentAction
from src.agentauth.run_report_routes import FailureDetails
from src.orchestration import operations_continuation_authority as authority
from src.orchestration.dispatch_pass import attempt_run_id
from src.orchestration.execution_policy import Action, AuthorizationContext, CredentialScope, ExecutionPolicy, PolicyLimits
from src.orchestration.execution_runner import DecisionKind, EffectOutcome, ObservationKind, RunnerContext
from src.orchestration.execution_state import ExecutionIdentity, OutcomeKind
from src.orchestration.execution_store import load_execution
from src.orchestration.models import OrchestrationAction, OrchestrationDecision, OrchestrationExecution, OrchestrationFlow
from src.orchestration.operations_continuation import DEPLOY, MERGE, OperationsContinuationHandler, OperationsObservation
from src.orchestration.operations_evidence import observed_operations_evidence
from src.orchestration.policy_admission import AdmissionInputs
from src.orchestration.results import observe_results
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.run_reports import OrchestrationRunReport
from tests.orchestration.test_operations_recovery import engine, exited, reporting, session, session_factory, work  # noqa: F401


@pytest.fixture
async def retained(session, session_factory, work, monkeypatch):
    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
    claim, row = await exited(session, work, 3, failure=FailureDetails(category="unknown", exit_code=1).model_dump())
    assert (await observe_results(session)).advanced == 1
    assert row.status == "concluded" and claim.state == "released"
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
    run = attempt_run_id(work.node.id, 3)
    report = await session.get(OrchestrationRunReport, run)
    before = copy.deepcopy(report.terminal_receipt)
    work.contract.node.operational_release_binding = None
    work.contract.environment_connection_ids = ["dev"]
    policy = ExecutionPolicy(
        schema_version=3,
        org_id=work.node.org_id,
        principal_id="human",
        policy_id="policy",
        policy_hash="b" * 64,
        repository_ids=["aws-e/adp"],
        allowed_actions=[Action.OPERATE, Action.REPAIR, Action.MERGE, Action.DEPLOY],
        repair_paths=["tests/e2e/repair.py"],
        repair_required_checks=["unit"],
        environment_connection_ids=["dev"],
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        limits=PolicyLimits(max_attempts_per_node=3, max_wall_clock_seconds=14400, max_spend_usd=Decimal(2000), max_concurrent_actions=8),
    )
    monkeypatch.setattr(authority, "accepted_node_contract", AsyncMock(return_value=work.contract))
    monkeypatch.setattr("src.orchestration.operations_repair.load_in_force_policy", AsyncMock(return_value=AdmissionInputs(policy, 1)))
    raw = {
        "status": {"S": "completed"},
        "terminal_outcome": {"S": "failed"},
        "tenant_id": {"S": work.node.org_id},
        "flow_id": {"S": work.node.flow_id},
        "orchestration_node_id": {"S": work.node.id},
        "orchestration_node_attempt": {"N": "3"},
        "invocation_id": {"S": run},
        "persona": {"S": "operations"},
        "repo": {"S": "aws-e/adp"},
        "installation_id": {"N": "42"},
        "provider_repository_id": {"N": "123"},
        "issue_number": {"N": "4196"},
        "workload_binding": {"S": "finished-pod"},
        "current_attempt": {"N": "1"},
    }
    grant = SimpleNamespace(
        flow_id=work.node.flow_id,
        repo_scope={"aws-e/adp"},
        delegable_actions={AgentAction.MONITOR},
        authority=SimpleNamespace(kind="gate_decision", reference_id="approval", human_id="human"),
        expires_at=policy.expires_at,
    )
    monkeypatch.setattr(
        authority,
        "resolve_engine_genesis",
        AsyncMock(return_value=SimpleNamespace(flow_id=work.node.flow_id, root_human_id="human", decision_id="approval")),
    )
    monkeypatch.setattr(authority, "approval_authorizes_node", AsyncMock(return_value=True))

    async def policy_context(*args, **kwargs):
        auth = AuthorizationContext(
            policy=policy,
            accepted_plan_version=1,
            in_force_plan_version=1,
            principal_id="human",
            member_org_id=work.node.org_id,
            principal_can_authorize=True,
            now=datetime.now(UTC),
            credential_scope=CredentialScope.SCOPED,
            observed_spend_usd=Decimal(1666),
            observed_concurrency=0,
            work_owned_by_policy_flow=True,
        )
        return raw, grant, AdmissionInputs(policy, 1), "human", SimpleNamespace(total_usd=Decimal(1666)), auth

    service = authority.OperationsContinuationServices(
        session_factory,
        authority=SimpleNamespace(
            protected=AsyncMock(return_value=raw),
            writer=SimpleNamespace(store=SimpleNamespace(live_grant=lambda **kw: grant)),
            policy_context=policy_context,
        ),
    )
    repair = OrchestrationAction(
        org_id=work.node.org_id,
        execution_id=row.id,
        kind="operations_repair_pr",
        status="succeeded",
        attempt=1,
        operation_key="operations-repair-pr:77:" + "c" * 40 + ":" + "a" * 40,
        receipt_ref="github/aws-e/adp/pull/77",
        detail={"pull_number": 77, "head": "c" * 40, "base": "a" * 40},
    )
    session.add(repair)
    await session.commit()
    return SimpleNamespace(
        node=work.node,
        row=row,
        claim=claim,
        report=report,
        before=before,
        service=service,
        policy=policy,
        repair=repair,
        raw=raw,
        grant=grant,
        factory=session_factory,
    )


async def recover(db, ctx):
    node = await db.get(type(ctx.node), ctx.node.id)
    decision = await ctx.service.recover(db, node)
    await db.commit()
    row = await db.get(OrchestrationExecution, ctx.row.id)
    identity = ExecutionIdentity(node.org_id, node.id, 3, 1, row.claim_id, row.claim_generation)
    loaded = await load_execution(db, identity=identity)
    assert loaded.kind is OutcomeKind.APPLIED
    return decision, RunnerContext(identity, loaded.record, datetime.now(UTC))


async def test_transfer_preserves_failure_budget_and_attempts(session, retained):
    decision, context = await recover(session, retained)
    assert context.identity.claim_generation == 4
    assert retained.node.attempts == 3
    assert retained.report.terminal_receipt == retained.before
    assert retained.policy.limits.max_attempts_per_node == 3
    assert retained.policy.limits.max_spend_usd == 2000
    assert retained.claim.active_run_id == authority.controller_run(decision)
    assert retained.row.next_check_at is not None
    assert await authority.transfer(session, node=retained.node, execution=retained.row, report=retained.report)
    # A second tick cannot claim the same failure again.
    with pytest.raises(CycleBlockedError):
        await retained.service.recover(session, retained.node)
    # The original failed worker is not a controller completion receipt.
    dispatch = {"run_id": retained.report.run_id, "attempt": 3, "conclusion_authority": "evidence"}
    assert await observed_operations_evidence(session, retained.node, dispatch, controller=True) == (None, "evidence_missing")


@pytest.mark.parametrize("assignment_shape", ["legacy", "frozen"])
@pytest.mark.parametrize("invalid_transfer", [False, True])
async def test_protected_replay_skips_only_validated_controller_transfer(session, retained, monkeypatch, invalid_transfer, assignment_shape):
    from unittest.mock import Mock

    from src.orchestration import dispatch_pass
    from src.orchestration.protected_dispatch_recovery import recover_one

    await recover(session, retained)
    if invalid_transfer:
        retained.claim.active_run_id = "unrelated-controller"
        await session.commit()
    dispatch = {
        "run_id": retained.report.run_id,
        "attempt": 3,
        "root_decision_id": "approval",
        "repo": retained.report.repo,
        "installation_id": retained.report.installation_id,
        "provider_repository_id": retained.report.provider_repository_id,
    }
    envelope = {
        "message_id": retained.report.run_id,
        "tenant_id": retained.node.org_id,
        "orchestration": {
            "flow_id": retained.node.flow_id,
            "node_id": retained.node.id,
            "attempt": 3,
            "root_decision_id": "approval",
        },
        "source_ref": {
            "repo": retained.report.repo,
            "issue": int(str(retained.node.issue_ref).lstrip("#")),
            "installation_id": retained.report.installation_id,
            "provider_repository_id": retained.report.provider_repository_id,
        },
        "handoff_expect": retained.report.dispatch_metadata["handoff_expect"],
    }
    if assignment_shape == "frozen":
        flow = await session.get(OrchestrationFlow, retained.node.flow_id)
        graph_address = f"{flow.slug}/{retained.node.epic_ref}/{retained.node.wave_ref}/{retained.node.node_ref}"
        envelope["orchestration"]["graph_address"] = graph_address
        envelope["work_claim_required"] = True
        handoff = envelope["handoff_expect"]
        source = envelope["source_ref"]
        dispatch["assignment"] = {
            "tenant_id": retained.node.org_id,
            "flow_id": retained.node.flow_id,
            "node_id": retained.node.id,
            "graph_address": graph_address,
            "accepted_plan_version": 1,
            "accepted_plan_hash": "b" * 64,
            "execution_id": handoff["execution_id"],
            "invocation_id": envelope["message_id"],
            "attempt": retained.node.attempts,
            "issue": source["issue"],
            "repo": source["repo"],
            "provider_repository_id": source["provider_repository_id"],
            "installation_id": source["installation_id"],
            "claim_id": handoff["claim_id"],
            "claim_generation": handoff["claim_generation"],
        }
    original_checks = AsyncMock(side_effect=ValueError("original dispatch checks still required"))
    monkeypatch.setattr(dispatch_pass, "_latest_approval_decision_id", original_checks)
    store = SimpleNamespace(_read=Mock(side_effect=AssertionError("No protected worker read is authorized")))
    if assignment_shape == "frozen":
        changed = copy.deepcopy(dispatch)
        changed["assignment"]["repo"] = "another/repository"
        with pytest.raises(ValueError, match="protected_dispatch_assignment_changed"):
            await recover_one(
                session, node=retained.node, dispatch=changed, envelope=envelope, config=SimpleNamespace(repo=retained.report.repo), store=store
            )
        original_checks.assert_not_awaited()
    if invalid_transfer:
        refusal = "protected_dispatch_claim_changed" if assignment_shape == "frozen" else "original dispatch checks still required"
        with pytest.raises(ValueError, match=refusal):
            await recover_one(
                session, node=retained.node, dispatch=dispatch, envelope=envelope, config=SimpleNamespace(repo=retained.report.repo), store=store
            )
        if assignment_shape == "legacy":
            original_checks.assert_awaited_once()
        else:
            original_checks.assert_not_awaited()
    else:
        assert (
            await recover_one(
                session, node=retained.node, dispatch=dispatch, envelope=envelope, config=SimpleNamespace(repo=retained.report.repo), store=store
            )
            is None
        )
        original_checks.assert_not_awaited()
    store._read.assert_not_called()


@pytest.mark.parametrize("change", ["pause", "halt", "human_failure", "unsettled", "claim_taken", "missing_pr", "revoked", "wrong_persona"])
async def test_recovery_refuses_changed_authority(session, retained, change):
    from src.agentauth.bootstrap import BootstrapRefusedError
    from src.orchestration.models import OrchestrationFlow

    if change in {"pause", "halt"}:
        flow = await session.get(OrchestrationFlow, retained.node.flow_id)
        if change == "pause":
            flow.execution_paused = True
        else:
            flow.state = "halted"
    elif change == "human_failure":
        event = await session.scalar(select(OrchestrationDecision).where(OrchestrationDecision.to_state == "failed"))
        session.add(
            OrchestrationDecision(
                org_id=event.org_id,
                flow_id=event.flow_id,
                node_id=event.node_id,
                kind="node_failed",
                actor_kind="human",
                actor_id="owner",
                actor_role="owner",
                to_state="failed",
                reason=event.reason,
            )
        )
    elif change == "unsettled":
        session.add(
            OrchestrationAction(
                org_id=retained.node.org_id,
                execution_id=retained.row.id,
                operation_key="uncertain-deploy",
                kind=DEPLOY,
                status="unknown",
                attempt=1,
                detail={},
            )
        )
    elif change == "claim_taken":
        retained.claim.generation += 1
    elif change == "missing_pr":
        await session.delete(retained.repair)
    elif change == "revoked":

        def revoked(**kw):
            raise BootstrapRefusedError("revoked")

        retained.service.authority.writer.store.live_grant = revoked
    else:
        retained.raw["persona"] = {"S": "developer"}
    await session.flush()
    with pytest.raises(CycleBlockedError):
        await retained.service.recover(session, retained.node)
    assert retained.node.state == "failed" and retained.node.attempts == 3
    assert retained.report.terminal_receipt == retained.before


@pytest.mark.parametrize("change", ["actor", "terminal", "generation", "owner", "run", "policy"])
async def test_transfer_is_revalidated_before_effects(session, retained, change):
    decision, context = await recover(session, retained)
    if change in {"actor", "policy"}:
        event = await session.get(OrchestrationDecision, decision)
        data = json.loads(event.reason)
        if change == "policy":
            data["policy_hash"] = "changed"
        forged = OrchestrationDecision(
            org_id=event.org_id,
            flow_id=event.flow_id,
            node_id=event.node_id,
            kind=event.kind,
            actor_id="untrusted-service" if change == "actor" else event.actor_id,
            actor_kind="service",
            actor_role="engine",
            reason=json.dumps(data),
        )
        session.add(forged)
        await session.flush()
        retained.row.handoff_receipt_ref = authority.controller_run(forged.id)
        retained.claim.active_run_id = retained.claim.claim_event_id = retained.row.handoff_receipt_ref
    elif change == "terminal":
        retained.report.terminal_receipt = {**retained.before, "outcome": "complete"}
    elif change == "generation":
        retained.claim.generation += 1
    elif change == "owner":
        retained.claim.owner_ref = "different-flow"
    elif change == "run":
        retained.claim.active_run_id = retained.report.run_id
    await session.flush()
    with pytest.raises(CycleBlockedError):
        await retained.service.authorize(session, context, Action.MERGE)


async def test_wait_rechecks_without_another_worker_or_effect(session, retained, monkeypatch):
    _, context = await recover(session, retained)
    await session.commit()
    handler = OperationsContinuationHandler(retained.factory, retained.service)
    check = AsyncMock(return_value={"merged": False, "ready": False})
    monkeypatch.setattr(handler, "merge", check)
    for _ in range(3):
        observed = await handler.observe(context)
        decision = handler.decide(context, observed)
        assert decision.kind is DecisionKind.WAIT
        assert decision.effect is None and decision.next_check_at > context.now
    check.return_value = {"merged": False, "ready": True}
    observed = await handler.observe(context)
    decision = handler.decide(context, observed)
    assert decision.kind is DecisionKind.EFFECT
    assert decision.effect.intent.kind == MERGE
    assert decision.effect.action is Action.MERGE
    assert retained.node.attempts == 3


async def test_deploy_cannot_use_unmerged_repair(session, retained):
    _, context = await recover(session, retained)
    from src.orchestration.operations_repair import RepairMergeRequiredError

    with pytest.raises(RepairMergeRequiredError):
        await retained.service.authorize(session, context, Action.DEPLOY)


async def test_terminal_missing_image_requests_only_fenced_retry(session, retained, monkeypatch):
    from src.orchestration import operations_repair
    from src.orchestration.operations_publication_recovery import ImageAbsence
    from src.orchestration.operations_release_job import NativeReleaseJob

    _, context = await recover(session, retained)
    await session.commit()
    async with retained.factory() as db:
        state = await retained.service.authorize(db, context, Action.MERGE)
        await operations_repair.record_repair_merge(
            db,
            state.authorized.assignment,
            pull_number=77,
            head="c" * 40,
            merged={"merged": True, "sha": "d" * 40},
            authorized=state.authorized,
        )
        db.add(
            OrchestrationAction(
                org_id=retained.node.org_id,
                execution_id=retained.row.id,
                kind="operations_native_release_dispatch",
                status="prepared",
                attempt=1,
                operation_key="native-release:prior",
                detail={"workflow": "deploy.yml", "dispatch_started": True},
            )
        )
        await db.commit()
    absence = ImageAbsence("000000000000", "us-east-1", "adp-tools-dev-adapters", "a" * 40, "sha256:" + "b" * 64, datetime.now(UTC))
    recovery = AsyncMock(return_value=absence)
    monkeypatch.setattr(NativeReleaseJob, "recover_terminal_publication", recovery)
    handler = OperationsContinuationHandler(retained.factory, retained.service)
    observed = await handler.observe(context)
    decision = handler.decide(context, observed)
    assert observed.kind is ObservationKind.READY
    assert decision.kind is DecisionKind.EFFECT and decision.effect.intent.detail == {"workflow": "deploy.yml", "native_retry": True}
    perform = AsyncMock(return_value={"dispatched": True})
    monkeypatch.setattr("src.agentauth.actions_routes._perform", perform)
    result = await handler.perform(context, decision.effect)
    assert result.outcome is EffectOutcome.SUCCEEDED
    assert perform.await_args.kwargs["native_retry"] == (context, retained.service)
    recovery.assert_awaited_once()


@pytest.mark.parametrize("run_status", [None, "in_progress", "failure"])
async def test_native_dispatch_reservation_is_observed_before_continuation(session, retained, monkeypatch, run_status):
    from src.orchestration import operations_repair
    from src.orchestration.operations_release_job import NativeReleaseJob

    _, context = await recover(session, retained)
    await session.commit()
    async with retained.factory() as db:
        state = await retained.service.authorize(db, context, Action.MERGE)
        await operations_repair.record_repair_merge(
            db,
            state.authorized.assignment,
            pull_number=77,
            head="c" * 40,
            merged={"merged": True, "sha": "d" * 40},
            authorized=state.authorized,
        )
        native = OrchestrationAction(
            org_id=retained.node.org_id,
            execution_id=retained.row.id,
            kind="operations_native_release_dispatch",
            status="prepared",
            attempt=1,
            operation_key="native-release:retained",
            detail={"workflow": "deploy.yml", "dispatch_started": True},
        )
        db.add(native)
        await db.commit()
    monkeypatch.setattr(NativeReleaseJob, "recover_terminal_publication", AsyncMock(return_value=False))
    handler = OperationsContinuationHandler(retained.factory, retained.service)
    listing = AsyncMock(
        return_value=None
        if run_status is None
        else {
            "id": 42,
            "status": "completed" if run_status == "failure" else run_status,
            "conclusion": "failure" if run_status == "failure" else None,
        }
    )
    monkeypatch.setattr(handler, "runs", listing)
    observed = await handler.observe(context)
    assert handler.decide(context, observed).effect is None
    assert observed.kind is (ObservationKind.BLOCKED if run_status == "failure" else ObservationKind.WAITING)
    listing.assert_awaited_once()
    assert listing.await_args.args[1].id == native.id


@pytest.mark.parametrize("wave_ref", ["w03b-foundation-release", "release-v2"])
async def test_queued_controller_handoff_retries_only_before_reservation(session, retained, monkeypatch, wave_ref):
    from src.orchestration import deployment_manifest, operations_repair
    from src.orchestration.operations_release_job import NativeReleaseJob

    _, context = await recover(session, retained)
    await session.commit()
    async with retained.factory() as db:
        state = await retained.service.authorize(db, context, Action.MERGE)
        await operations_repair.record_repair_merge(
            db,
            state.authorized.assignment,
            pull_number=77,
            head="c" * 40,
            merged={"merged": True, "sha": "d" * 40},
            authorized=state.authorized,
        )
        pending = OrchestrationAction(
            org_id=retained.node.org_id,
            execution_id=retained.row.id,
            kind=DEPLOY,
            status="unknown",
            attempt=1,
            operation_key="controller:queued",
            detail={"workflow": "deploy.yml"},
        )
        db.add(pending)
        row = await db.get(OrchestrationExecution, retained.row.id)
        row.pending_action_key = pending.operation_key
        row.status = "awaiting_external"
        row.revision += 1
        await db.commit()
        loaded = await load_execution(db, identity=context.identity)
        context = RunnerContext(context.identity, loaded.record, datetime.now(UTC))
    handler = OperationsContinuationHandler(retained.factory, retained.service)
    state = await handler.state(context)
    state.node.wave_ref = wave_ref
    state.contract.node.operational_release_binding = SimpleNamespace(target="foundation")
    state.report.dispatch_metadata["orchestration"]["release_binding"] = {"mutation_role_arn": "registered-role"}
    monkeypatch.setattr(handler, "state", AsyncMock(return_value=state))
    monkeypatch.setattr(
        deployment_manifest,
        "load_packaged_manifest",
        lambda: SimpleNamespace(entry=lambda key: SimpleNamespace(status=deployment_manifest.EntryStatus.ENABLED, release_group="foundation")),
    )
    monkeypatch.setattr(NativeReleaseJob, "recover_terminal_publication", AsyncMock(return_value=False))
    listing = AsyncMock()
    monkeypatch.setattr(handler, "runs", listing)
    observed = await handler.observe(context)
    decision = handler.decide(context, observed)
    assert observed.kind is ObservationKind.READY
    assert decision.kind is DecisionKind.EFFECT and decision.effect.intent.operation_key == pending.operation_key
    assert decision.effect.intent.detail == {"workflow": "deploy.yml"}
    listing.assert_not_awaited()
    pending.detail = {**pending.detail, "dispatch_started": True}
    async with retained.factory() as db:
        saved = await db.get(OrchestrationAction, pending.id)
        saved.detail = pending.detail
        await db.commit()
    observed = await handler.observe(context)
    assert observed.kind is ObservationKind.BLOCKED and handler.decide(context, observed).effect is None
    listing.assert_not_awaited()


async def test_lost_controller_dispatch_reply_observes_reserved_action_without_repeat(session, retained, monkeypatch):
    from src.orchestration import operations_repair
    from src.orchestration.operations_release_job import NativeReleaseJob

    _, context = await recover(session, retained)
    await session.commit()
    async with retained.factory() as db:
        state = await retained.service.authorize(db, context, Action.MERGE)
        await operations_repair.record_repair_merge(
            db,
            state.authorized.assignment,
            pull_number=77,
            head="c" * 40,
            merged={"merged": True, "sha": "d" * 40},
            authorized=state.authorized,
        )
        native = OrchestrationAction(
            org_id=retained.node.org_id,
            execution_id=retained.row.id,
            kind="operations_native_release_dispatch",
            status="prepared",
            attempt=1,
            operation_key="native-release:controller",
            detail={"workflow": "deploy.yml", "dispatch_started": True, "controller_dispatch": True},
        )
        pending = OrchestrationAction(
            org_id=retained.node.org_id,
            execution_id=retained.row.id,
            kind=DEPLOY,
            status="unknown",
            attempt=1,
            operation_key="controller:pending",
            detail={"workflow": "deploy.yml"},
        )
        db.add_all([pending, native])
        await db.flush()
        pending.detail = {**pending.detail, "native_release_action_id": native.id, "dispatch_started": True}
        row = await db.get(OrchestrationExecution, retained.row.id)
        row.pending_action_key = pending.operation_key
        row.status = "awaiting_external"
        row.revision += 1
        await db.commit()
        loaded = await load_execution(db, identity=context.identity)
        context = RunnerContext(context.identity, loaded.record, datetime.now(UTC))
    monkeypatch.setattr(NativeReleaseJob, "recover_terminal_publication", AsyncMock(return_value=False))
    handler = OperationsContinuationHandler(retained.factory, retained.service)
    listing = AsyncMock(return_value=None)
    monkeypatch.setattr(handler, "runs", listing)
    observed = await handler.observe(context)
    assert observed.kind is ObservationKind.WAITING and handler.decide(context, observed).effect is None
    assert listing.await_args.args[1].id == native.id
    listing.return_value = {"id": 42, "status": "in_progress"}
    observed = await handler.observe(context)
    assert observed.kind is ObservationKind.SUCCEEDED and observed.operation_key == pending.operation_key
    assert handler.decide(context, observed).kind is DecisionKind.ADVANCE
    async with retained.factory() as db:
        saved = await db.get(OrchestrationAction, pending.id)
        saved.detail = {**saved.detail, "native_release_action_id": "different-native-action"}
        await db.commit()
    listing.reset_mock()
    observed = await handler.observe(context)
    assert observed.kind is ObservationKind.BLOCKED and handler.decide(context, observed).effect is None
    listing.assert_not_awaited()


async def test_lost_retry_reply_never_replays_dispatch(session, retained, monkeypatch):
    from src.orchestration import operations_repair
    from src.orchestration.operations_release_job import NativeReleaseJob

    _, context = await recover(session, retained)
    await session.commit()
    async with retained.factory() as db:
        state = await retained.service.authorize(db, context, Action.MERGE)
        await operations_repair.record_repair_merge(
            db,
            state.authorized.assignment,
            pull_number=77,
            head="c" * 40,
            merged={"merged": True, "sha": "d" * 40},
            authorized=state.authorized,
        )
        native = OrchestrationAction(
            org_id=retained.node.org_id,
            execution_id=retained.row.id,
            kind="operations_native_release_dispatch",
            status="prepared",
            attempt=1,
            operation_key="native-release:prior",
            detail={"workflow": "deploy.yml", "dispatch_started": True},
        )
        retry = OrchestrationAction(
            org_id=retained.node.org_id,
            execution_id=retained.row.id,
            kind=DEPLOY,
            status="unknown",
            attempt=1,
            operation_key="native-release:retry",
            detail={"workflow": "deploy.yml", "native_retry": True},
        )
        db.add_all([native, retry])
        await db.commit()
    monkeypatch.setattr(NativeReleaseJob, "recover_terminal_publication", AsyncMock(return_value=False))
    handler = OperationsContinuationHandler(retained.factory, retained.service)
    listing = AsyncMock(return_value=None)
    monkeypatch.setattr(handler, "runs", listing)
    observed = await handler.observe(context)
    assert observed.kind is ObservationKind.WAITING and handler.decide(context, observed).effect is None
    listing.assert_not_awaited()
    async with retained.factory() as db:
        saved = await db.get(OrchestrationAction, native.id)
        saved.detail = {**saved.detail, "retry_started_at": datetime.now(UTC).isoformat()}
        await db.commit()
    observed = await handler.observe(context)
    assert observed.kind is ObservationKind.WAITING and handler.decide(context, observed).effect is None
    assert listing.await_args.args[1].id == native.id


async def test_pending_retry_cursor_reuses_intent_only_before_reservation(session, retained, monkeypatch):
    from src.orchestration import operations_repair
    from src.orchestration.operations_publication_recovery import ImageAbsence
    from src.orchestration.operations_release_job import NativeReleaseJob

    _, context = await recover(session, retained)
    await session.commit()
    async with retained.factory() as db:
        state = await retained.service.authorize(db, context, Action.MERGE)
        await operations_repair.record_repair_merge(
            db,
            state.authorized.assignment,
            pull_number=77,
            head="c" * 40,
            merged={"merged": True, "sha": "d" * 40},
            authorized=state.authorized,
        )
        native = OrchestrationAction(
            org_id=retained.node.org_id,
            execution_id=retained.row.id,
            kind="operations_native_release_dispatch",
            status="prepared",
            attempt=1,
            operation_key="native-release:prior",
            detail={"workflow": "deploy.yml", "dispatch_started": True},
        )
        retry = OrchestrationAction(
            org_id=retained.node.org_id,
            execution_id=retained.row.id,
            kind=DEPLOY,
            status="prepared",
            attempt=1,
            operation_key="native-release:retry",
            detail={"workflow": "deploy.yml", "native_retry": True},
        )
        db.add_all([native, retry])
        row = await db.get(OrchestrationExecution, retained.row.id)
        row.pending_action_key = retry.operation_key
        row.status = "awaiting_external"
        row.revision += 1
        await db.commit()
        loaded = await load_execution(db, identity=context.identity)
        context = RunnerContext(context.identity, loaded.record, datetime.now(UTC))
    absence = ImageAbsence("000000000000", "us-east-1", "adp-tools-dev-adapters", "a" * 40, "sha256:" + "b" * 64, datetime.now(UTC))
    recovery = AsyncMock(return_value=absence)
    monkeypatch.setattr(NativeReleaseJob, "recover_terminal_publication", recovery)
    handler = OperationsContinuationHandler(retained.factory, retained.service)
    observed = await handler.observe(context)
    decision = handler.decide(context, observed)
    assert observed.kind is ObservationKind.READY
    assert decision.effect.intent.operation_key == retry.operation_key
    assert decision.effect.intent.detail == {"workflow": "deploy.yml", "native_retry": True}
    async with retained.factory() as db:
        saved = await db.get(OrchestrationAction, native.id)
        saved.detail = {**saved.detail, "retry_started_at": datetime.now(UTC).isoformat()}
        await db.commit()
    recovery.return_value = False
    listing = AsyncMock(return_value=None)
    monkeypatch.setattr(handler, "runs", listing)
    observed = await handler.observe(context)
    assert observed.kind is ObservationKind.WAITING and handler.decide(context, observed).effect is None
    listing.assert_awaited_once()


@pytest.mark.parametrize("lease_applied", [False, True])
@pytest.mark.parametrize("flow_slug,wave_ref", [("atlassian-adapters-8445-parallel", "w03b-foundation-release"), ("billing-service", "release-v2")])
async def test_verified_foundation_settlement_frees_lease_before_completion(session, retained, monkeypatch, lease_applied, flow_slug, wave_ref):
    from src.orchestration import operations_evidence, operations_repair
    from src.orchestration import operations_result_settlement as leases

    _, context = await recover(session, retained)
    await session.commit()
    async with retained.factory() as db:
        merge_state = await retained.service.authorize(db, context, Action.MERGE)
        await operations_repair.record_repair_merge(
            db,
            merge_state.authorized.assignment,
            pull_number=77,
            head="c" * 40,
            merged={"merged": True, "sha": "d" * 40},
            authorized=merge_state.authorized,
        )
        await db.commit()
    state = await OperationsContinuationHandler(retained.factory, retained.service).state(context, Action.DEPLOY)
    state.node.wave_ref = wave_ref
    state.contract.node.operational_release_binding = SimpleNamespace(target="foundation")
    release = AsyncMock(return_value=SimpleNamespace(applied=lease_applied))
    monkeypatch.setattr(leases, "release_lease", release)
    async with retained.factory() as db:
        flow = await db.get(OrchestrationFlow, state.node.flow_id)
        flow.slug = flow_slug
        native = OrchestrationAction(
            org_id=state.node.org_id,
            execution_id=state.row.id,
            kind="operations_native_release_dispatch",
            status="prepared",
            attempt=1,
            operation_key="native-release:verified",
            detail={
                "correlation": state.report.run_id,
                "shared_release": {
                    "owner": "foundation:owner",
                    "target_key": "physical-target",
                    "generation": context.identity.claim_generation,
                },
            },
        )
        db.add(native)
        await db.flush()

        async def verified_evidence(*args, observation, **kwargs):
            observation["native_release"] = {
                "action_id": native.id,
                "operation_key": native.operation_key,
                "detail": native.detail,
                "runtime_evidence_id": "operations-runtime:verified",
            }
            return "verified", ""

        monkeypatch.setattr(operations_evidence, "observed_operations_evidence", verified_evidence)
        handler = OperationsContinuationHandler(retained.factory, SimpleNamespace(authorize=AsyncMock(return_value=state)))
        observed = OperationsObservation(ObservationKind.READY, stage="settle", data={"runtime_evidence_id": "operations-runtime:verified"})
        decision = handler.decide(context, observed)
        if lease_applied:
            await decision.settlement(db, context)
            assert state.node.state == "passed"
            assert native.status == "succeeded" and native.receipt_ref == "operations-runtime:verified"
            assert release.await_args.kwargs["terminal_evidence"] == "operations-runtime:verified"
            assert release.await_args.kwargs["holder"].generation == context.identity.claim_generation
        else:
            with pytest.raises(CycleBlockedError, match="operations_release_handoff_changed"):
                await decision.settlement(db, context)
            assert state.node.state == "running"
            assert native.status == "prepared"


async def test_scheduled_pass_recovers_once(session, retained, monkeypatch):
    from src.orchestration import operations_continuation

    monkeypatch.setattr(operations_continuation, "OperationsContinuationServices", lambda factory: retained.service)
    assert await operations_continuation.recover_pending_operations(retained.factory) == 1
    assert await operations_continuation.recover_pending_operations(retained.factory) == 0
    await session.refresh(retained.node)
    assert retained.node.state == "running" and retained.node.attempts == 3


async def test_correlated_run_listing_is_bounded_and_preserves_second_precision(retained, monkeypatch):
    from src.orchestration import operations_continuation

    state = SimpleNamespace(
        binding=SimpleNamespace(org_id=retained.node.org_id, installation_id=42, repo="aws-e/adp", provider_repository_id=123), report=retained.report
    )
    now = datetime.now(UTC).replace(microsecond=800000)
    raw = {
        "id": 42,
        "created_at": now.replace(microsecond=0).isoformat(),
        "repository": {"id": 123},
        "path": ".github/workflows/deploy.yml",
        "event": "workflow_dispatch",
        "display_title": "ADP deployment " + retained.report.run_id,
    }

    async def request(binding, method, path, **kwargs):
        assert method == "GET" and kwargs["params"]["per_page"] == 10 and kwargs["max_bytes"] == 512 * 1024
        return SimpleNamespace(json=lambda: {"workflow_runs": [raw]})

    monkeypatch.setattr(operations_continuation, "installation_token", AsyncMock(return_value="test-only"))
    monkeypatch.setattr(operations_continuation, "WorkflowProvider", lambda: SimpleNamespace(request=request))
    handler = OperationsContinuationHandler(retained.factory, retained.service)
    action = SimpleNamespace(kind=DEPLOY, created_at=now, detail={"workflow": "deploy.yml"})
    assert await handler.runs(state, action) == raw
    raw["created_at"] = (now - timedelta(minutes=1)).isoformat()
    assert await handler.runs(state, action) is None


async def test_native_run_listing_requires_pinned_writer_and_revision(retained, monkeypatch):
    from src.orchestration import operations_continuation

    state = SimpleNamespace(
        binding=SimpleNamespace(org_id=retained.node.org_id, installation_id=42, repo="aws-e/adp", provider_repository_id=123),
        report=retained.report,
    )
    raw = {
        "id": 42,
        "run_attempt": 1,
        "head_sha": "d" * 40,
        "created_at": datetime.now(UTC).isoformat(),
        "repository": {"id": 123},
        "path": ".github/workflows/deploy.yml",
        "event": "workflow_dispatch",
        "display_title": "ADP deployment " + retained.report.run_id,
    }
    request = AsyncMock(return_value=SimpleNamespace(json=lambda: {"workflow_runs": [raw]}))
    monkeypatch.setattr(operations_continuation, "installation_token", AsyncMock(return_value="test-only"))
    monkeypatch.setattr(operations_continuation, "WorkflowProvider", lambda: SimpleNamespace(request=request))
    handler = OperationsContinuationHandler(retained.factory, retained.service)
    action = SimpleNamespace(
        kind="operations_native_release_dispatch",
        created_at=datetime.now(UTC) - timedelta(minutes=1),
        detail={"workflow": "deploy.yml", "dispatch_revision": "d" * 40, "release_job": {"run_id": "42", "run_attempt": "1"}},
    )
    assert await handler.runs(state, action) == raw
    for changes in ({"id": 43}, {"head_sha": "e" * 40}, {"run_attempt": 2}):
        changed = {**raw, **changes}
        request.return_value = SimpleNamespace(json=lambda: {"workflow_runs": [changed]})
        assert await handler.runs(state, action) is None
    action.detail.pop("release_job")
    action.detail["retry_started_at"] = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
    previous = {**raw, "id": 41, "created_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}
    request.return_value = SimpleNamespace(json=lambda: {"workflow_runs": [raw, previous]})
    assert await handler.runs(state, action) == raw


async def test_runner_restart_continues_to_signed_runtime_without_repeating_dispatch(session, retained, monkeypatch):
    from src.orchestration import execution_runner, operations_repair
    from src.orchestration.deployment_workflow_provider import WorkflowContext
    from src.orchestration.execution_runner import RunnerConfig, run_execution_runner
    from src.orchestration.execution_state import ExecutionPhase
    from src.orchestration.operations_release_job import NativeReleaseJob
    from src.orchestration.operations_workflow_evidence import record_correlated_workflow
    from src.orchestration.stage_attempts import stage_counts
    from src.orchestration.stall import _continuation_clock

    _, context = await recover(session, retained)
    candidate = SimpleNamespace(org_id=retained.node.org_id, flow_id=retained.node.flow_id, node_id=retained.node.id, attempts=3)
    assert await _continuation_clock(session, candidate) == (True, None)
    await session.commit()
    ready, published, dispatches = False, False, []
    recoveries = []
    recover_publication = NativeReleaseJob.recover_terminal_publication

    async def observe_publication(job, current, services):
        recoveries.append(current.execution.id)
        return await recover_publication(job, current, services)

    monkeypatch.setattr(NativeReleaseJob, "recover_terminal_publication", observe_publication)
    moment = datetime.now(UTC)
    monkeypatch.setattr(execution_runner, "load_in_force_policy", AsyncMock(return_value=AdmissionInputs(retained.policy, 1)))
    monkeypatch.setattr("src.agentauth.actions_routes.get_session_factory", lambda: retained.factory)
    # The production receipt producer reads the same accepted contract.
    from src.orchestration.accepted_node import accepted_node_contract

    async with retained.factory() as db:
        contract = await accepted_node_contract(db, retained.node)
    monkeypatch.setattr("src.orchestration.operations_workflow_evidence.accepted_node_contract", AsyncMock(return_value=contract))

    async def merge(handler, current, data, *, mutate):
        if not mutate:
            return {"merged": False, "ready": ready}
        async with retained.factory() as db:
            state = await retained.service.authorize(db, current, Action.MERGE)
            await operations_repair.record_repair_merge(
                db, state.authorized.assignment, pull_number=77, head="c" * 40, merged={"merged": True, "sha": "d" * 40}, authorized=state.authorized
            )
            await db.commit()
        return {"merged": True, "sha": "d" * 40}

    async def dispatch(*args, **kwargs):
        issued, binding, execution = await kwargs["authority"]()
        assert execution["source_revision"] == {"S": "d" * 40}
        dispatches.append(execution["invocation_id"]["S"])
        # The provider accepted the request but the response was lost.
        raise TimeoutError("provider response lost")

    raw = {
        "id": 42,
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "success",
        "event": "workflow_dispatch",
        "path": ".github/workflows/deploy.yml",
        "head_branch": "main",
        "head_sha": "d" * 40,
        "repository": {"id": 123},
        "display_title": "ADP deployment " + retained.report.run_id,
    }

    async def runs(handler, state, action):
        return raw if published else None

    async def capture(*args, **kwargs):
        issued, binding, execution = await kwargs["authority"]()
        now = datetime.now(UTC)
        context_doc = WorkflowContext(
            schema_version=1,
            repository_id=123,
            run_id=42,
            run_attempt=1,
            workflow_path=raw["path"],
            workflow_revision="d" * 40,
            source_revision="d" * 40,
            account_id="123456789012",
            region="us-east-1",
            resource_kind="test-target",
            resource_id="fictional",
            inputs={},
            correlation=retained.report.run_id,
        )
        async with retained.factory() as db:
            receipt_id = await record_correlated_workflow(
                db, execution=execution, binding=binding, issued=issued, raw_run=raw, context=context_doc, artifact_id=99, digest="f" * 64
            )
            assert receipt_id
            row = await db.get(OrchestrationExecution, retained.row.id)
            workflow = OrchestrationAction(
                org_id=row.org_id,
                execution_id=row.id,
                kind="deployment_workflow",
                operation_key="operations-workflow:42:1",
                status="succeeded",
                attempt=1,
                receipt_ref="github/actions/runs/42/artifacts/99",
                detail={"runtime_verified": True, "context_action_id": receipt_id, "controller_observation": True},
            )
            receipt = dict(
                org_id=row.org_id,
                execution_id=row.id,
                node_id=row.node_id,
                flow_id=row.flow_id,
                cycle=3,
                accepted_plan_version=1,
                claim_id=row.claim_id,
                claim_generation=row.claim_generation,
                operation_key="operations-runtime:42:1",
                repo="aws-e/adp",
                source_revision="d" * 40,
                actual_revision="d" * 40,
                merge_operation_key="operations-source:" + "d" * 40,
                workflow_operation_keys=["operations-workflow:42:1"],
                manifest_entry_ids=["tools-facade"],
                targets=[{"component": "tools-facade"}],
                delivery_complete=True,
                observed_at=now.isoformat(),
                valid_until=(now + timedelta(hours=1)).isoformat(),
                components=[
                    {
                        "component": "tools-facade",
                        "actual_revision": "d" * 40,
                        "artifact_hash": "f" * 64,
                        "image_digest": "sha256:" + "f" * 64,
                        "healthy": True,
                        "evidence_ref": "github/actions/runs/42/artifacts/99",
                        "observed_at": now.isoformat(),
                        "pod_uids": ["pod"],
                    }
                ],
            )
            evidence = OrchestrationAction(
                org_id=row.org_id,
                execution_id=row.id,
                kind="deployment_verification",
                operation_key="operations-runtime:42:1",
                receipt_ref="operations-runtime:42:1",
                status="succeeded",
                attempt=1,
                detail={"deployment_receipt": receipt},
            )
            db.add_all([workflow, evidence])
            await db.flush()
            evidence_id = evidence.id
            await db.commit()
        return {"workflow_evidence_id": receipt_id, "runtime_evidence_id": evidence_id}

    monkeypatch.setattr(OperationsContinuationHandler, "merge", merge)
    monkeypatch.setattr(OperationsContinuationHandler, "runs", runs)
    monkeypatch.setattr("src.agentauth.actions_routes._perform", dispatch)
    monkeypatch.setattr("src.agentauth.actions_routes._capture_correlated_run", capture)

    async def tick():
        nonlocal moment
        moment += timedelta(minutes=2)
        # Construct a fresh handler on every tick, just like a restarted engine.
        handler = OperationsContinuationHandler(retained.factory, retained.service)
        clock = SimpleNamespace(now=lambda: moment, monotonic=lambda: 0)
        result = await run_execution_runner(
            retained.factory, handlers={ExecutionPhase.AWAITING_REVIEW: handler}, config=RunnerConfig(enabled=True), clock=clock
        )
        assert result.errors == 0, result
        return result

    assert (await tick()).reserved == 0  # CI wait
    ready = True
    assert (await tick()).effects_succeeded == 1  # merge
    assert (await tick()).effects_uncertain == 1  # dispatch accepted, response lost
    assert (await tick()).reserved == 0  # eventual provider listing is still empty
    assert len(dispatches) == 1
    assert recoveries and set(recoveries) == {retained.row.id}
    published = True
    assert (await tick()).reserved == 0  # reconcile the one dispatch
    assert (await tick()).advanced == 1  # verify and settle
    async with retained.factory() as db:
        node = await db.get(type(retained.node), retained.node.id)
        row = await db.get(OrchestrationExecution, retained.row.id)
        report = await db.get(OrchestrationRunReport, retained.report.run_id)
        claim = await db.get(type(retained.claim), retained.claim.id)
        assert node.state == "passed" and node.attempts == 3
        assert row.status == "concluded" and row.next_check_at is None
        assert report.terminal_receipt == retained.before
        assert claim.state == "released" and claim.release_reason == "completed"
        counts = await stage_counts(db, org_id=node.org_id, node_ids=[node.id])
        assert counts[node.id] == {"merge": 1, "deploy": 1}
    assert len(dispatches) == 1
