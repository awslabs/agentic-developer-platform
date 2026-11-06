"""E2 acceptance uses real PostgreSQL and the actual protected D3 handoff."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.admin.exceptions import AccessDeniedError
from src.orchestration.adapters.github_comments import GateAnswerStatus, InputPath, apply_gate_answer_for_context
from src.orchestration.deployment_manifest import TargetEvidence
from src.orchestration.evaluation_contract import specification
from src.orchestration.evaluation_controller import CONTEXT_KIND, EVIDENCE_KIND, EvaluationController, EvaluationServices
from src.orchestration.evaluation_evidence import ProviderEvidence, specification_hash, validate_evaluation_evidence
from src.orchestration.evaluation_runtime import EvaluationRuntime
from src.orchestration.execution_policy import Action
from src.orchestration.execution_runner import RunnerConfig, run_execution_runner
from src.orchestration.execution_state import ExecutionPhase, ExecutionStatus, PhaseAdvance
from src.orchestration.execution_store import advance_execution
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
)
from tests.orchestration.test_deployment_controller import cycle, deployment, finish, merge, pg_server, pg_url, runtime, store  # noqa: F401

pytestmark = pytest.mark.parametrize("cycle", [{"delivery": True}], indirect=True)


@pytest.mark.parametrize("missing", [False, True])
async def test_machine_evaluation_never_claims_or_dispatches_legacy_worker(evaluation, monkeypatch, missing):
    from src.orchestration.dispatch_pass import DispatchPassConfig, DispatchPassReport, _dispatch_one
    from src.orchestration.evaluation_plan import managed_evaluation

    claim = AsyncMock(side_effect=AssertionError("machine observer must not claim an issue lane"))
    monkeypatch.setattr("src.orchestration.work_admission.admit", claim)
    async with evaluation.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, evaluation.plan.id)
        if missing:
            document = dict(plan.plan_document)
            document["nodes"] = [{**row, "evaluation": None} for row in document["nodes"]]
            plan.plan_document = document
            await db.flush()
        node = await db.get(OrchestrationNode, evaluation.eval_id)
        assert await managed_evaluation(db, node)
        report = DispatchPassReport(enabled=True)
        await _dispatch_one(db, node, config=DispatchPassConfig(queue_url="queue", repo="o/r"), report=report)
        assert node.state == "ready" and node.attempts == 0 and report.pending == []
        claim.assert_not_awaited()
        refusals = list(await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id)))
        assert any("evaluation_specification_missing" in (row.rejection_reason or "") for row in refusals) is missing


async def test_ready_machine_evaluation_stays_ready_for_its_controller(evaluation):
    from src.orchestration.tick import run_tick

    ctx = evaluation
    async with ctx.factory() as db:
        node = await db.get(OrchestrationNode, ctx.eval_id)
        report = await run_tick(db)
        assert report.success and report.errors == 0
        assert node.state == "ready"
        assert not list(
            await db.scalars(
                select(OrchestrationDecision).where(
                    OrchestrationDecision.node_id == ctx.eval_id, OrchestrationDecision.kind == "human_evaluation_evidence"
                )
            )
        )


async def test_pending_machine_evaluation_reports_missing_spec_without_tick_error(evaluation):
    from src.orchestration.tick import run_tick

    ctx = evaluation
    async with ctx.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        plan.plan_document = {**plan.plan_document, "nodes": [{**row, "evaluation": None} for row in plan.plan_document["nodes"]]}
        node = await db.get(OrchestrationNode, ctx.eval_id)
        node.state = "pending"
        await db.commit()
        report = await run_tick(db)
        assert report.success and report.errors == 0
        assert report.blocked[ctx.eval_id] == ["evaluation_specification_missing"]
        assert node.state == "pending" and node.attempts == 0
        assert await db.scalar(select(OrchestrationExecution.id).where(OrchestrationExecution.node_id == node.id)) is None


async def test_deployed_story_reports_missing_dependent_spec_without_provider_failure(evaluation):
    ctx = evaluation
    async with ctx.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        plan.plan_document = {**plan.plan_document, "nodes": [{**row, "evaluation": None} for row in plan.plan_document["nodes"]]}
        await db.commit()
    report = await tick(ctx)
    assert report.blocked == 1
    async with ctx.factory() as db:
        record = await db.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == ctx.node.id))
        assert record.block_detail == "evaluation_specification_missing"
        assert record.block_code != "provider_unavailable"
        evaluation_node = await db.get(OrchestrationNode, ctx.eval_id)
        assert evaluation_node.state == "ready" and evaluation_node.attempts == 0
    ctx.evaluation_provider.find.assert_not_awaited()


@pytest.fixture
async def evaluation(runtime):  # noqa: F811
    ctx = runtime
    ctx.target = replace(ctx.target, evidence=TargetEvidence("registered-aws-role:delivery-connection", datetime.now(UTC).isoformat()))
    await finish(ctx)
    ctx.eval_address = "cycle/E1/W1/EVAL"
    ctx.spec = dict(
        acceptance_mode="machine",
        runner=dict(
            adapter="github-orchestration-harness-v1",
            repository=ctx.binding.repo,
            repository_id=123,
            workflow_path=".github/workflows/orchestration-live-tests.yml",
            harness_revision="d" * 40,
        ),
        environment_connection_id="delivery-connection",
        target=dict(
            provider="aws",
            account_id=ctx.target.account_id,
            region=ctx.target.region,
            resource_kind=ctx.target.resource_kind,
            resource_id=ctx.target.resource_id,
        ),
        fixtures=dict(fixture_set_id="suite", definition_hash="a" * 64, org_refs=[ctx.node.org_id], roles=["member"], minimum_rows_per_org=1),
        criteria=[dict(criterion_id="API-1", kind="api", required=True)],
    )
    async with ctx.factory() as db:
        connection = await db.connection()
        await connection.run_sync(lambda sync: OrchestrationEdge.__table__.create(sync, checkfirst=True))
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        policy = ctx.policy.model_dump(mode="json")
        policy["allowed_actions"].append(Action.EVALUATE.value)
        # E2-only fixtures leave corrective effects to the E3 suite.
        policy["allowed_actions"] = [action for action in policy["allowed_actions"] if action != Action.REPAIR.value]
        policy["user_credentials"]["actions"].append(Action.EVALUATE.value)
        policy["evaluation_acceptance"] = {ctx.eval_address: "machine"}
        node = OrchestrationNode(
            org_id=ctx.node.org_id,
            flow_id=ctx.node.flow_id,
            epic_ref="E1",
            wave_ref="W1",
            node_ref="EVAL",
            kind="eval",
            title="Evaluate deployed code",
            state="ready",
            attempts=0,
        )
        gate = OrchestrationNode(
            org_id=ctx.node.org_id,
            flow_id=ctx.node.flow_id,
            epic_ref="E1",
            wave_ref="W2",
            node_ref="GATE",
            kind="gate",
            title="Explicit human gate",
            state="awaiting_gate",
            attempts=0,
        )
        successor = OrchestrationNode(
            org_id=ctx.node.org_id,
            flow_id=ctx.node.flow_id,
            epic_ref="E1",
            wave_ref="W2",
            node_ref="NEXT",
            kind="story",
            title="Dependent story",
            state="pending",
            attempts=0,
        )
        db.add_all([node, gate, successor])
        await db.flush()
        db.add_all(
            [
                OrchestrationEdge(org_id=node.org_id, flow_id=node.flow_id, from_node_id=ctx.node.id, to_node_id=node.id),
                OrchestrationEdge(org_id=node.org_id, flow_id=node.flow_id, from_node_id=node.id, to_node_id=successor.id),
                OrchestrationEdge(org_id=node.org_id, flow_id=node.flow_id, from_node_id=gate.id, to_node_id=successor.id),
            ]
        )
        plan.plan_document = {
            **plan.plan_document,
            "execution_policy": policy,
            "nodes": [
                dict(address=ctx.eval_address, kind="eval", title=node.title, evaluation=ctx.spec),
            ],
        }
        await db.commit()
        ctx.eval_id, ctx.gate_id, ctx.next_id = node.id, gate.id, successor.id
    ctx.evaluation_runtime = EvaluationRuntime(ctx.factory, deployments=ctx.runtime_services)
    ctx.evaluation_runtime.verify = AsyncMock(return_value=datetime.now(UTC))
    ctx.failed_criterion = False

    async def find(binding, expected):
        now = datetime.now(UTC)
        spec = specification(expected.specification)
        payload = b'{"observed":true}'
        import hashlib

        receipt = dict(
            **vars(expected.identity),
            execution_id=expected.execution_id,
            flow_id=expected.flow_id,
            policy_hash=expected.policy_hash,
            deployment_operation_key=expected.deployment.operation_key,
            actual_revision=expected.deployment.actual_revision,
            harness_revision=spec.runner.harness_revision,
            specification_hash=specification_hash(spec),
            target=spec.target.model_dump(),
            fixtures=dict(fixture_set_id="suite", definition_hash="a" * 64, roles=["member"], row_counts={ctx.node.org_id: 1}),
            producer=dict(
                repository_id=123, workflow_path=spec.runner.workflow_path, run_id=100, run_attempt=1, producer_id="github-actions:123:100:1"
            ),
            criteria=[dict(criterion_id="API-1", outcome="fail" if ctx.failed_criterion else "pass", artifact_paths=["api.json"])],
            artifacts=[dict(path="api.json", kind="api", sha256=hashlib.sha256(payload).hexdigest())],
            started_at=now.isoformat(),
            completed_at=now.isoformat(),
            expires_at=(now + timedelta(minutes=5)).isoformat(),
            live=True,
        )
        return validate_evaluation_evidence(
            expected,
            ProviderEvidence(
                123,
                spec.runner.workflow_path,
                spec.runner.harness_revision,
                100,
                1,
                "github-actions:123:100:1",
                "github/actions/runs/100/artifacts/101",
                "a" * 64,
                json.dumps(receipt).encode(),
                {"api.json": payload},
                now,
            ),
        )

    ctx.evaluation_provider = SimpleNamespace(find=AsyncMock(side_effect=find))
    ctx.evaluation_services = EvaluationServices(ctx.factory, runtime=ctx.evaluation_runtime, provider=ctx.evaluation_provider)
    return ctx


async def tick(ctx):
    from src.orchestration.evaluation_plan import identity_for

    async with ctx.factory() as db:
        records = list((await db.scalars(select(OrchestrationExecution).where(OrchestrationExecution.status != "concluded"))).all())
        for record in records:
            await advance_execution(
                db,
                identity=identity_for(record),
                advance=PhaseAdvance(
                    phase=ExecutionPhase(record.phase),
                    status=ExecutionStatus(record.status),
                    expected_revision=record.revision,
                    next_check_at=datetime.now(UTC),
                ),
            )
        await db.commit()
    return await run_execution_runner(
        ctx.factory,
        handlers={ExecutionPhase.EVALUATION_PENDING: EvaluationController(ctx.factory, ctx.evaluation_services)},
        config=RunnerConfig(enabled=True, max_attempts=8, io_timeout_seconds=20),
        notifier=AsyncMock(return_value="notice"),
    )


async def nodes(ctx):
    async with ctx.factory() as db:
        return {row.id: row.state for row in (await db.scalars(select(OrchestrationNode))).all()}


async def test_machine_receipt_accepts_eval_once_and_preserves_explicit_human_gate(evaluation):
    ctx = evaluation
    report = await tick(ctx)
    async with ctx.factory() as db:
        requests = list((await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == CONTEXT_KIND))).all())
        assert len(requests) == 1, report
    report = await tick(ctx)
    async with ctx.factory() as db:
        debug = [(r.phase, r.block_detail, r.progress_note) for r in (await db.scalars(select(OrchestrationExecution))).all()]
    assert (await nodes(ctx))[ctx.eval_id] == "passed", (report, debug)
    assert (await nodes(ctx))[ctx.gate_id] == "awaiting_gate"
    assert (await nodes(ctx))[ctx.next_id] == "pending"
    await tick(ctx)
    async with ctx.factory() as db:
        evidence = list((await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == EVIDENCE_KIND))).all())
        assert len(evidence) == 1
        decisions = list(
            (
                await db.scalars(
                    select(OrchestrationDecision).where(OrchestrationDecision.node_id == ctx.eval_id, OrchestrationDecision.to_state == "passed")
                )
            ).all()
        )
        assert len(decisions) == 1
        from src.orchestration.story_acceptance import current_story_acceptance

        source = await db.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == ctx.node.id))
        records = await current_story_acceptance(db, org_id=source.org_id, executions={source.node_id: source})
        assert source.status == "concluded" and records[source.id].completed_at is not None
    ctx.evaluation_runtime.verify.assert_awaited_once()


async def test_authentic_failure_is_retained_and_blocks_without_reopening_predecessor(evaluation):
    ctx = evaluation
    ctx.failed_criterion = True
    await tick(ctx)
    await tick(ctx)
    await tick(ctx)
    await tick(ctx)
    current = await nodes(ctx)
    assert current[ctx.eval_id] == "running" and current[ctx.node.id] == "passed" and current[ctx.next_id] == "pending"
    async with ctx.factory() as db:
        evidence = list((await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == EVIDENCE_KIND))).all())
        assert len(evidence) == 1 and evidence[0].detail["required_failures"] == ["API-1"]
        record = await db.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == ctx.eval_id))
        assert record.status == "blocked" and "correction" in record.block_detail


async def evaluation_record(ctx):
    async with ctx.factory() as db:
        return await db.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == ctx.eval_id))


async def evidence_rows(ctx):
    async with ctx.factory() as db:
        return list((await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == EVIDENCE_KIND))).all())


async def test_runner_request_export_uses_current_protected_scope(evaluation):
    from src.orchestration.evaluation_request import export_request
    from src.orchestration.review_cycle import CycleBlockedError

    ctx = evaluation
    await tick(ctx)
    row = await evaluation_record(ctx)
    request = await export_request(ctx.factory, org_id=ctx.node.org_id, execution_id=row.id, services=ctx.evaluation_services)
    assert request.node_id == ctx.eval_id and request.node_id != ctx.node.id
    assert request.claim_id == ctx.identity.claim_id and request.actual_revision == ctx.component.actual_revision
    assert request.specification.acceptance_mode == "machine"
    ctx.evaluation_provider.find.assert_not_awaited()
    with pytest.raises(CycleBlockedError, match="unavailable"):
        await export_request(ctx.factory, org_id="foreign", execution_id=row.id, services=ctx.evaluation_services)


async def test_acceptance_reuses_actual_runtime_reader_with_evaluate_authority(evaluation):
    ctx = evaluation
    del ctx.evaluation_runtime.verify
    await tick(ctx)
    report = await tick(ctx)
    assert (await nodes(ctx))[ctx.eval_id] == "passed", report
    assert ctx.targets.resolve.await_args.kwargs["action"] is Action.EVALUATE


async def test_only_satisfied_successor_releases_in_acceptance_transaction(evaluation):
    ctx = evaluation
    async with ctx.factory() as db:
        successor = OrchestrationNode(
            org_id=ctx.node.org_id,
            flow_id=ctx.node.flow_id,
            epic_ref="E1",
            wave_ref="W2",
            node_ref="FREE",
            kind="story",
            title="Satisfied dependency",
            state="pending",
            attempts=0,
        )
        db.add(successor)
        await db.flush()
        db.add(OrchestrationEdge(org_id=ctx.node.org_id, flow_id=ctx.node.flow_id, from_node_id=ctx.eval_id, to_node_id=successor.id))
        await db.commit()
        successor_id = successor.id
    await tick(ctx)
    await tick(ctx)
    states = await nodes(ctx)
    assert states[successor_id] == "ready" and states[ctx.next_id] == "pending"


@pytest.mark.parametrize("state", ["awaiting_gate", "rejected_at_gate", "halted", "failed"])
async def test_human_and_terminal_states_never_auto_approve(evaluation, state):
    ctx = evaluation
    async with ctx.factory() as db:
        node = await db.get(OrchestrationNode, ctx.eval_id)
        node.state = state
        await db.commit()
    await tick(ctx)
    assert (await nodes(ctx))[ctx.eval_id] == state
    assert not await evidence_rows(ctx)
    ctx.evaluation_provider.find.assert_not_awaited()


async def test_human_suite_presents_existing_gate_without_machine_execution(evaluation):
    ctx = evaluation
    async with ctx.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["nodes"][0]["evaluation"] = {"acceptance_mode": "human"}
        document["execution_policy"]["evaluation_acceptance"] = {}
        plan.plan_document = document
        await db.commit()
    await tick(ctx)
    assert (await nodes(ctx))[ctx.eval_id] == "awaiting_gate"
    assert await evaluation_record(ctx) is None
    assert not await evidence_rows(ctx)
    ctx.evaluation_provider.find.assert_not_awaited()


@pytest.mark.parametrize("specified", [False, True])
async def test_human_evaluation_presents_gate_in_one_tick_without_dispatch(evaluation, monkeypatch, specified):
    from src.orchestration.dispatch_pass import DispatchPassConfig, DispatchPassReport, _dispatch_one
    from src.orchestration.routes import get_flow_graph
    from src.orchestration.tick import run_tick
    from src.shared.models.usage import UsageLog

    ctx = evaluation
    claim = AsyncMock(side_effect=AssertionError("a human gate must not claim a worker"))
    monkeypatch.setattr("src.orchestration.work_admission.admit", claim)
    async with ctx.factory() as db:
        parent = await db.get(OrchestrationNode, ctx.node.id)
        parent.state = "passed"
        db.add(
            OrchestrationDecision(
                org_id=parent.org_id,
                flow_id=parent.flow_id,
                node_id=parent.id,
                kind="result_observed",
                actor_id="system:test-delivery",
                actor_role="engine",
                actor_kind="service",
                from_state="awaiting_merge",
                to_state="passed",
            )
        )
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["nodes"][0]["evaluation"] = {"acceptance_mode": "human"} if specified else None
        document["execution_policy"]["evaluation_acceptance"] = {}
        plan.plan_document = document
        await db.commit()

        node = await db.get(OrchestrationNode, ctx.eval_id)
        assert node.state == "ready"
        dispatch_report = DispatchPassReport(enabled=True)
        await _dispatch_one(db, node, config=DispatchPassConfig(queue_url="queue", repo="o/r"), report=dispatch_report)
        assert dispatch_report.pending == [] and dispatch_report.admission_refused == 0
        report = await run_tick(db)
        assert report.success, report.blocked
        assert node.state == "awaiting_gate"
        await db.commit()

    claim.assert_not_awaited()
    async with ctx.factory() as db:
        node = await db.get(OrchestrationNode, ctx.eval_id)
        assert node.state == "awaiting_gate" and node.attempts == 0
        decisions = list(await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.node_id == ctx.eval_id)))
        evidence = [row for row in decisions if row.kind == "human_evaluation_evidence"]
        assert len(evidence) == 1
        bound = json.loads(evidence[0].reason)
        assert bound["plan_version"] == ctx.plan.version
        assert bound["predecessors"][0]["node_id"] == ctx.node.id
        assert bound["predecessors"][0]["decision_id"]
        assert bound["predecessors"][0]["head_sha"] == ctx.binding.head_sha
        assert bound["predecessors"][0]["merge_sha"]
        assert len([row for row in decisions if row.to_state == "awaiting_gate"]) == 1
        assert await db.scalar(select(OrchestrationExecution.id).where(OrchestrationExecution.node_id == ctx.eval_id)) is None
        connection = await db.connection()
        await connection.run_sync(lambda sync: UsageLog.__table__.create(sync, checkfirst=True))
        # Graph reads use a separate repeatable-read connection; publish fixture DDL first.
        await db.commit()
        monkeypatch.setattr("src.orchestration.routes.load_story_execution", AsyncMock(return_value={}))
        graph = await get_flow_graph(
            flow_id=node.flow_id,
            current_user=SimpleNamespace(org_id=node.org_id, user_id="test-graph-reader"),
            access=SimpleNamespace(check_permission=AsyncMock()),
            db=db,
        )
        eval_card = next(item for item in graph.nodes if item.id == node.id)
        assert eval_card.state == "awaiting_gate" and eval_card.display_state == "gate"
        assert eval_card.delivery_progress.stage == "awaiting_gate"
        assert eval_card.delivery_progress.actor == "flow-owner"
        assert eval_card.configuration_problem is None
        assert eval_card.run_id is None and eval_card.attempts == 0
        second = await run_tick(db)
        assert second.success
        assert (
            len(
                list(
                    await db.scalars(
                        select(OrchestrationDecision).where(
                            OrchestrationDecision.node_id == ctx.eval_id, OrchestrationDecision.kind == "human_evaluation_evidence"
                        )
                    )
                )
            )
            == 1
        )


async def test_human_evaluation_presents_gate_and_approval_releases_successors(evaluation):
    from src.orchestration.gate_execution import prepare_gate_execution
    from src.orchestration.tick import run_tick

    ctx = evaluation
    async with ctx.factory() as db:
        parent = await db.get(OrchestrationNode, ctx.node.id)
        parent.state = "passed"
        other_gate = await db.get(OrchestrationNode, ctx.gate_id)
        other_gate.state = "passed"
        db.add(
            OrchestrationDecision(
                org_id=parent.org_id,
                flow_id=parent.flow_id,
                node_id=parent.id,
                kind="result_observed",
                actor_id="system:test-delivery",
                actor_role="engine",
                actor_kind="service",
                from_state="awaiting_merge",
                to_state="passed",
            )
        )
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["nodes"][0]["evaluation"] = None
        document["execution_policy"]["evaluation_acceptance"] = {}
        plan.plan_document = document
        await db.commit()

        report = await run_tick(db)
        assert report.success and (await db.get(OrchestrationNode, ctx.eval_id)).state == "awaiting_gate"
        assert (await db.get(OrchestrationNode, ctx.next_id)).state == "pending"
        preview = await prepare_gate_execution(db, gate=await db.get(OrchestrationNode, ctx.eval_id), actor=None)
        assert preview["ready"] and preview["evidence"]["predecessors"]
        access = SimpleNamespace(
            get_user_role=AsyncMock(return_value=(SimpleNamespace(value="owner"), None, None)),
            check_permission=AsyncMock(),
        )
        result = await apply_gate_answer_for_context(
            db,
            context=SimpleNamespace(org_id=parent.org_id, user_id="approver", account_type="human", auth_source="jwt"),
            node_id=ctx.eval_id,
            approve=True,
            reason="Accept predecessor evidence",
            access=access,
            input_path=InputPath.DASHBOARD,
            expected_plan_hash=plan.plan_hash,
            execution_preview=preview,
        )
        assert result.status is GateAnswerStatus.APPLIED
        access.check_permission.assert_awaited_once()
        await db.commit()

    async with ctx.factory() as db:
        assert (await db.get(OrchestrationNode, ctx.eval_id)).state == "passed"
        assert (await db.get(OrchestrationNode, ctx.next_id)).state == "ready"
        decisions = list(await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.node_id == ctx.eval_id)))
        assert len([row for row in decisions if row.to_state == "passed"]) == 1
        assert len([row for row in decisions if row.kind == "human_evaluation_evidence"]) == 1


@pytest.fixture
async def reviewable_human_gate(evaluation):
    from src.orchestration.tick import run_tick

    ctx = evaluation
    async with ctx.factory() as db:
        parent = await db.get(OrchestrationNode, ctx.node.id)
        parent.state = "passed"
        (await db.get(OrchestrationNode, ctx.gate_id)).state = "passed"
        db.add(
            OrchestrationDecision(
                org_id=parent.org_id,
                flow_id=parent.flow_id,
                node_id=parent.id,
                kind="result_observed",
                actor_id="system:test-delivery",
                actor_role="engine",
                actor_kind="service",
                from_state="awaiting_merge",
                to_state="passed",
            )
        )
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["nodes"][0]["evaluation"] = None
        document["execution_policy"]["evaluation_acceptance"] = {}
        plan.plan_document = document
        await db.commit()
        report = await run_tick(db)
        assert report.success
        assert (await db.get(OrchestrationNode, ctx.eval_id)).state == "awaiting_gate"
        await db.commit()
    return ctx


def gate_access(*, authorized=True):
    return SimpleNamespace(
        get_user_role=AsyncMock(return_value=(SimpleNamespace(value="owner"), None, None)),
        check_permission=AsyncMock(side_effect=None if authorized else AccessDeniedError("plan:approve denied")),
    )


async def answer_human_gate(ctx, db, preview, *, authorized=True):
    access = gate_access(authorized=authorized)
    result = await apply_gate_answer_for_context(
        db,
        context=SimpleNamespace(org_id=ctx.node.org_id, user_id="approver", account_type="human", auth_source="jwt"),
        node_id=ctx.eval_id,
        approve=True,
        reason="Reviewed current evaluation evidence",
        access=access,
        input_path=InputPath.DASHBOARD,
        expected_plan_hash=ctx.plan.plan_hash,
        execution_preview=preview,
    )
    return result, access


@pytest.mark.parametrize(
    "fault",
    [
        "missing_preview",
        "predecessor_changed",
        "plan_changed",
        "empty_policy",
        "proposed_policy",
        "policy_removed",
        "policy_null",
        "evidence_removed",
        "unauthorized",
    ],
)
async def test_human_approval_refuses_stale_or_unauthorized_gate(reviewable_human_gate, fault):
    from src.orchestration.gate_execution import prepare_gate_execution

    ctx = reviewable_human_gate
    async with ctx.factory() as db:
        gate = await db.get(OrchestrationNode, ctx.eval_id)
        preview = await prepare_gate_execution(db, gate=gate, actor=None)
        assert preview["evidence_id"] and preview["evidence"]["predecessors"]
        if fault == "missing_preview":
            preview = None
        elif fault == "predecessor_changed":
            db.add(
                OrchestrationDecision(
                    org_id=ctx.node.org_id,
                    flow_id=ctx.node.flow_id,
                    node_id=ctx.node.id,
                    kind="result_observed",
                    actor_id="system:test-new-evidence",
                    actor_role="engine",
                    actor_kind="service",
                    from_state="awaiting_merge",
                    to_state="passed",
                )
            )
            await db.flush()
        elif fault == "plan_changed":
            (await db.get(OrchestrationAcceptedPlan, ctx.plan.id)).plan_hash = "f" * 64
            await db.flush()
        elif fault in {"empty_policy", "proposed_policy", "policy_removed"}:
            plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
            document = json.loads(json.dumps(plan.plan_document))
            if fault == "proposed_policy":
                document["proposed_execution_policy"] = document.pop("execution_policy")
            elif fault == "policy_removed":
                document.pop("execution_policy")
            else:
                document["execution_policy"] = {}
            plan.plan_document = document
            await db.flush()
        elif fault == "policy_null":
            plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
            plan.plan_document = {**plan.plan_document, "execution_policy": None}
            await db.flush()
        elif fault == "evidence_removed":
            await db.delete(await db.get(OrchestrationDecision, preview["evidence_id"]))
            await db.flush()
        result, access = await answer_human_gate(ctx, db, preview, authorized=fault != "unauthorized")
        expected = GateAnswerStatus.REFUSED_NO_PERMISSION if fault == "unauthorized" else GateAnswerStatus.REFUSED_STALE_PLAN
        assert result.status is expected
        access.check_permission.assert_awaited_once()
        await db.commit()

    async with ctx.factory() as db:
        assert (await db.get(OrchestrationNode, ctx.eval_id)).state == "awaiting_gate"
        assert (await db.get(OrchestrationNode, ctx.next_id)).state == "pending"
        refusals = list(
            await db.scalars(
                select(OrchestrationDecision).where(
                    OrchestrationDecision.node_id == ctx.eval_id,
                    OrchestrationDecision.kind == "transition_rejected",
                )
            )
        )
        assert len(refusals) == 1 and refusals[0].rejection_reason
        assert not list(
            await db.scalars(
                select(OrchestrationDecision).where(
                    OrchestrationDecision.node_id == ctx.eval_id,
                    OrchestrationDecision.kind == "gate_approved",
                )
            )
        )


@pytest.mark.parametrize("fault", ["removed", "malformed"])
async def test_human_gate_policy_cannot_downgrade_to_legacy(reviewable_human_gate, fault):
    from src.orchestration.continuation import digest
    from src.orchestration.evaluation_plan import legacy_worker_gate, validate_human_gate_evidence
    from src.orchestration.gate_execution import prepare_gate_execution
    from src.orchestration.review_cycle import CycleBlockedError

    ctx = reviewable_human_gate
    async with ctx.factory() as db:
        gate = await db.get(OrchestrationNode, ctx.eval_id)
        preview = await prepare_gate_execution(db, gate=gate, actor=None)
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = dict(plan.plan_document)
        if fault == "removed":
            document.pop("execution_policy")
        else:
            document["execution_policy"] = {"unrecognized": True}
        plan.superseded_at = datetime.now(UTC)
        amended = OrchestrationAcceptedPlan(
            org_id=plan.org_id,
            flow_id=plan.flow_id,
            version=plan.version + 1,
            plan_document=document,
            plan_hash=digest(document),
            accepted_by_decision_id=plan.accepted_by_decision_id,
        )
        db.add(amended)
        await db.flush()
        ctx.plan = amended
        assert await legacy_worker_gate(db, gate) is False
        retained = await prepare_gate_execution(db, gate=gate, actor=None)
        assert retained["evidence_id"] == preview["evidence_id"]
        with pytest.raises(CycleBlockedError, match="evaluation_policy_unverifiable"):
            await validate_human_gate_evidence(db, gate, preview["evidence_id"])
        result, access = await answer_human_gate(ctx, db, retained)
        assert result.status is GateAnswerStatus.REFUSED_STALE_PLAN
        assert "evaluation_policy_unverifiable" in result.message
        access.check_permission.assert_awaited_once()
        assert gate.state == "awaiting_gate"
        assert (await db.get(OrchestrationNode, ctx.next_id)).state == "pending"


async def test_policy_history_prevents_legacy_gate_without_retained_evidence(reviewable_human_gate):
    from src.orchestration.gate_execution import prepare_gate_execution
    from src.orchestration.tick import run_tick

    ctx = reviewable_human_gate
    async with ctx.factory() as db:
        gate = await db.get(OrchestrationNode, ctx.eval_id)
        original_preview = await prepare_gate_execution(db, gate=gate, actor=None)
        original_plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        original_plan.superseded_at = datetime.now(UTC)
        amended = OrchestrationAcceptedPlan(
            org_id=original_plan.org_id,
            flow_id=original_plan.flow_id,
            version=original_plan.version + 1,
            plan_document={key: value for key, value in original_plan.plan_document.items() if key != "execution_policy"},
            plan_hash="f" * 64,
            accepted_by_decision_id=original_plan.accepted_by_decision_id,
        )
        db.add(amended)
        await db.delete(await db.get(OrchestrationDecision, original_preview["evidence_id"]))
        await db.commit()
        ctx.plan = amended

        preview = await prepare_gate_execution(db, gate=gate, actor=None)
        assert preview["ready"] is False
        assert preview["evidence_id"] is None
        report = await run_tick(db)
        assert report.success
        assert report.blocked[gate.id] == ["evaluation_human_contract_changed"]
        result, access = await answer_human_gate(ctx, db, preview)
        assert result.status is GateAnswerStatus.REFUSED_STALE_PLAN
        assert "evaluation_gate_evidence_not_reviewed" in result.message
        access.check_permission.assert_awaited_once()
        await db.commit()

    async with ctx.factory() as db:
        assert (await db.get(OrchestrationNode, ctx.eval_id)).state == "awaiting_gate"
        assert (await db.get(OrchestrationNode, ctx.next_id)).state == "pending"


async def test_archived_flow_refuses_reviewed_evaluation_gate(reviewable_human_gate):
    from src.orchestration.compile import PolicyNotAcceptableError
    from src.orchestration.gate_execution import authorize_gate_execution, prepare_gate_execution

    ctx = reviewable_human_gate
    async with ctx.factory() as db:
        gate = await db.get(OrchestrationNode, ctx.eval_id)
        preview = await prepare_gate_execution(db, gate=gate, actor=None)
        assert preview["ready"] is True
        flow = await db.get(OrchestrationFlow, gate.flow_id)
        flow.archived_at = datetime.now(UTC)
        await db.commit()

        with pytest.raises(PolicyNotAcceptableError, match="Unarchive this flow"):
            await authorize_gate_execution(db, gate=gate, actor=None, reviewed=preview)
        assert gate.state == "awaiting_gate"
        assert (await db.get(OrchestrationNode, ctx.next_id)).state == "pending"


async def test_changed_human_evidence_refreshes_gate_before_approval(reviewable_human_gate):
    from src.orchestration.gate_execution import prepare_gate_execution
    from src.orchestration.tick import run_tick

    ctx = reviewable_human_gate
    async with ctx.factory() as db:
        gate = await db.get(OrchestrationNode, ctx.eval_id)
        old_preview = await prepare_gate_execution(db, gate=gate, actor=None)
        db.add(
            OrchestrationDecision(
                org_id=ctx.node.org_id,
                flow_id=ctx.node.flow_id,
                node_id=ctx.node.id,
                kind="result_observed",
                actor_id="system:test-new-evidence",
                actor_role="engine",
                actor_kind="service",
                from_state="awaiting_merge",
                to_state="passed",
            )
        )
        await db.commit()
        refused, _ = await answer_human_gate(ctx, db, old_preview)
        assert refused.status is GateAnswerStatus.REFUSED_STALE_PLAN
        await db.commit()
        tick_report = await run_tick(db)
        assert tick_report.success and tick_report.transitions_effected == 1
        await db.commit()
        refreshed = await prepare_gate_execution(db, gate=gate, actor=None)
        assert refreshed["evidence_id"] != old_preview["evidence_id"]
        assert refreshed["evidence"]["predecessors"] != old_preview["evidence"]["predecessors"]
        refused, _ = await answer_human_gate(ctx, db, old_preview)
        assert refused.status is GateAnswerStatus.REFUSED_STALE_PLAN
        accepted, _ = await answer_human_gate(ctx, db, refreshed)
        assert accepted.status is GateAnswerStatus.APPLIED
        await db.commit()

    async with ctx.factory() as db:
        assert (await db.get(OrchestrationNode, ctx.eval_id)).state == "passed"
        assert (await db.get(OrchestrationNode, ctx.next_id)).state == "ready"
        rows = list(await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.node_id == ctx.eval_id)))
        assert len([row for row in rows if row.kind == "gate_refreshed"]) == 1
        assert len([row for row in rows if row.kind == "human_evaluation_evidence"]) == 2
        assert len([row for row in rows if row.kind == "gate_approved"]) == 1


async def test_duplicate_human_approval_replays_without_releasing_twice(reviewable_human_gate):
    from src.orchestration.gate_execution import prepare_gate_execution

    ctx = reviewable_human_gate
    async with ctx.factory() as db:
        preview = await prepare_gate_execution(db, gate=await db.get(OrchestrationNode, ctx.eval_id), actor=None)
        first, _ = await answer_human_gate(ctx, db, preview)
        assert first.status is GateAnswerStatus.APPLIED
        await db.commit()
        replay, _ = await answer_human_gate(ctx, db, preview)
        assert replay.status is GateAnswerStatus.IDEMPOTENT_REPLAY and replay.decision_id == first.decision_id
        await db.commit()

    async with ctx.factory() as db:
        assert (await db.get(OrchestrationNode, ctx.next_id)).state == "ready"
        rows = list(await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.node_id == ctx.eval_id)))
        assert len([row for row in rows if row.kind == "gate_approved"]) == 1


async def test_dashboard_route_rejects_stale_human_evidence_and_accepts_fresh_gate(reviewable_human_gate):
    from fastapi import HTTPException

    from src.orchestration.controls import _answer_gate
    from src.orchestration.gate_execution import prepare_gate_execution
    from src.orchestration.tick import run_tick

    ctx = reviewable_human_gate
    async with ctx.factory() as db:
        gate = await db.get(OrchestrationNode, ctx.eval_id)
        old = await prepare_gate_execution(db, gate=gate, actor=None)
        db.add(
            OrchestrationDecision(
                org_id=ctx.node.org_id,
                flow_id=ctx.node.flow_id,
                node_id=ctx.node.id,
                kind="result_observed",
                actor_id="system:test-new-evidence",
                actor_role="engine",
                actor_kind="service",
                from_state="awaiting_merge",
                to_state="passed",
            )
        )
        await db.commit()
        user = SimpleNamespace(org_id=ctx.node.org_id, user_id="approver", account_type="human", auth_source="jwt")
        with pytest.raises(HTTPException) as refused:
            await _answer_gate(
                ctx.eval_id,
                approve=True,
                reason="Reviewed",
                current_user=user,
                access=gate_access(),
                db=db,
                expected_plan_hash=ctx.plan.plan_hash,
                execution_preview=old,
            )
        assert refused.value.status_code == 409
        assert (await db.get(OrchestrationNode, ctx.eval_id)).state == "awaiting_gate"
        report = await run_tick(db)
        assert report.success
        await db.commit()
        current = await prepare_gate_execution(db, gate=gate, actor=None)
        result = await _answer_gate(
            ctx.eval_id,
            approve=True,
            reason="Reviewed refreshed evidence",
            current_user=user,
            access=gate_access(),
            db=db,
            expected_plan_hash=ctx.plan.plan_hash,
            execution_preview=current,
        )
        assert result.state == "passed"

    async with ctx.factory() as db:
        assert (await db.get(OrchestrationNode, ctx.next_id)).state == "ready"
        decisions = list(await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.node_id == ctx.eval_id)))
        assert len([row for row in decisions if row.kind == "gate_approved"]) == 1
        assert len([row for row in decisions if row.kind == "transition_rejected"]) == 1


async def test_human_approval_refuses_stale_declared_runtime_evidence(evaluation):
    from src.orchestration.gate_execution import prepare_gate_execution
    from src.orchestration.tick import run_tick

    ctx = evaluation
    async with ctx.factory() as db:
        parent = await db.get(OrchestrationNode, ctx.node.id)
        parent.state = "passed"
        db.add(
            OrchestrationDecision(
                org_id=parent.org_id,
                flow_id=parent.flow_id,
                node_id=parent.id,
                kind="result_observed",
                actor_id="system:test-delivery",
                actor_role="engine",
                actor_kind="service",
                from_state="awaiting_merge",
                to_state="passed",
            )
        )
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["nodes"][0]["evaluation"] = {"acceptance_mode": "human", "target": ctx.spec["target"]}
        document["execution_policy"]["evaluation_acceptance"] = {}
        plan.plan_document = document
        await db.commit()
        tick_report = await run_tick(db)
        assert tick_report.success and (await db.get(OrchestrationNode, ctx.eval_id)).state == "awaiting_gate"
        await db.commit()
        gate = await db.get(OrchestrationNode, ctx.eval_id)
        preview = await prepare_gate_execution(db, gate=gate, actor=None)
        assert preview["evidence"]["deployments"]
        parent.attempts += 1
        await db.commit()
        result, _ = await answer_human_gate(ctx, db, preview)
        assert result.status is GateAnswerStatus.REFUSED_STALE_PLAN
        assert "verified_deployment_required" in result.message
        await db.commit()

    async with ctx.factory() as db:
        assert (await db.get(OrchestrationNode, ctx.eval_id)).state == "awaiting_gate"
        assert (await db.get(OrchestrationNode, ctx.next_id)).state == "pending"
        assert not list(
            await db.scalars(
                select(OrchestrationDecision).where(
                    OrchestrationDecision.node_id == ctx.eval_id,
                    OrchestrationDecision.kind == "gate_approved",
                )
            )
        )


@pytest.mark.parametrize("fault", ["stale_deployment", "paused"])
@pytest.mark.parametrize("explicit_target", [False, True])
async def test_human_evaluation_waits_for_declared_deployment_evidence(evaluation, fault, explicit_target):
    from src.orchestration.tick import run_tick

    ctx = evaluation
    async with ctx.factory() as db:
        parent = await db.get(OrchestrationNode, ctx.node.id)
        parent.state = "passed"
        if fault == "stale_deployment":
            parent.attempts += 1
        else:
            flow = await db.get(OrchestrationFlow, parent.flow_id)
            flow.execution_paused = True
        db.add(
            OrchestrationDecision(
                org_id=parent.org_id,
                flow_id=parent.flow_id,
                node_id=parent.id,
                kind="result_observed",
                actor_id="system:test-delivery",
                actor_role="engine",
                actor_kind="service",
                from_state="awaiting_merge",
                to_state="passed",
            )
        )
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["nodes"][0]["evaluation"] = (
            {
                "acceptance_mode": "human",
                "target": ctx.spec["target"],
            }
            if explicit_target
            else None
        )
        document["execution_policy"]["evaluation_acceptance"] = {}
        if not explicit_target and "deploy" not in document["execution_policy"]["allowed_actions"]:
            document["execution_policy"]["allowed_actions"].append("deploy")
        plan.plan_document = document
        await db.commit()

        report = await run_tick(db)
        expected = "verified_deployment_required" if fault == "stale_deployment" else "evaluation_flow_paused"
        assert report.success and report.blocked[ctx.eval_id] == [expected]
        node = await db.get(OrchestrationNode, ctx.eval_id)
        assert node.state == "ready"
        assert not list(
            await db.scalars(
                select(OrchestrationDecision).where(
                    OrchestrationDecision.node_id == ctx.eval_id, OrchestrationDecision.kind == "human_evaluation_evidence"
                )
            )
        )
        assert not list(
            await db.scalars(
                select(OrchestrationDecision).where(OrchestrationDecision.node_id == ctx.eval_id, OrchestrationDecision.kind == "transition_rejected")
            )
        )
        waits = list(
            await db.scalars(
                select(OrchestrationDecision).where(
                    OrchestrationDecision.node_id == ctx.eval_id,
                    OrchestrationDecision.kind == "evaluation_evidence_wait",
                )
            )
        )
        assert len(waits) == 1
        assert json.loads(waits[0].reason) == {"attempt": 0, "plan_version": plan.version, "block_code": expected}
        again = await run_tick(db)
        assert again.success and again.blocked[ctx.eval_id] == [expected]
        assert (
            len(
                list(
                    await db.scalars(
                        select(OrchestrationDecision).where(
                            OrchestrationDecision.node_id == ctx.eval_id,
                            OrchestrationDecision.kind == "evaluation_evidence_wait",
                        )
                    )
                )
            )
            == 1
        )
        if fault == "stale_deployment":
            parent.attempts -= 1
        else:
            flow.execution_paused = False
        await db.flush()
        resumed = await run_tick(db)
        assert resumed.success and ctx.eval_id not in resumed.blocked
        assert (await db.get(OrchestrationNode, ctx.eval_id)).state == "awaiting_gate"
        assert (
            len(
                list(
                    await db.scalars(
                        select(OrchestrationDecision).where(
                            OrchestrationDecision.node_id == ctx.eval_id,
                            OrchestrationDecision.kind == "evaluation_evidence_ready",
                        )
                    )
                )
            )
            == 1
        )
        assert (
            len(
                list(
                    await db.scalars(
                        select(OrchestrationDecision).where(
                            OrchestrationDecision.node_id == ctx.eval_id,
                            OrchestrationDecision.kind == "human_evaluation_evidence",
                        )
                    )
                )
            )
            == 1
        )


async def test_code_only_human_gate_needs_merge_checks_but_not_deployment(evaluation, monkeypatch):
    from src.orchestration.tick import run_tick

    ctx = evaluation
    deployment_read = AsyncMock(side_effect=AssertionError("code-only evaluation must not require deployment"))
    monkeypatch.setattr("src.orchestration.evaluation_plan.predecessor_deployments", deployment_read)
    async with ctx.factory() as db:
        parent = await db.get(OrchestrationNode, ctx.node.id)
        parent.state = "passed"
        db.add(
            OrchestrationDecision(
                org_id=parent.org_id,
                flow_id=parent.flow_id,
                node_id=parent.id,
                kind="result_observed",
                actor_id="system:test-merge",
                actor_role="engine",
                actor_kind="service",
                from_state="awaiting_merge",
                to_state="passed",
            )
        )
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["nodes"][0]["evaluation"] = {"acceptance_mode": "human"}
        policy = document["execution_policy"]
        policy["evaluation_acceptance"] = {}
        policy["allowed_actions"] = [action for action in policy["allowed_actions"] if action != "deploy"]
        policy["user_credentials"]["actions"] = [action for action in policy["user_credentials"]["actions"] if action != "deploy"]
        policy["human_gates"] = [action for action in policy["human_gates"] if action != "deploy"]
        policy["environment_connection_ids"] = []
        plan.plan_document = document
        await db.commit()
        report = await run_tick(db)
        assert report.success and (await db.get(OrchestrationNode, ctx.eval_id)).state == "awaiting_gate"
        bound = await db.scalar(
            select(OrchestrationDecision).where(
                OrchestrationDecision.node_id == ctx.eval_id,
                OrchestrationDecision.kind == "human_evaluation_evidence",
            )
        )
        snapshot = json.loads(bound.reason)
        assert snapshot["deployments"] == []
        assert snapshot["predecessors"][0]["merge_sha"]
        deployment_read.assert_not_awaited()


@pytest.mark.parametrize("fault", ["mode", "plan", "claim", "cycle", "halt", "expiry", "runtime", "revoke_during_runtime"])
async def test_stale_observation_or_authority_rolls_back_acceptance(evaluation, fault):
    from src.orchestration.models import OrchestrationWorkClaim
    from src.orchestration.review_cycle import CycleBlockedError

    ctx = evaluation
    await tick(ctx)
    original = ctx.evaluation_provider.find.side_effect

    async def changed(binding, expected):
        validated = await original(binding, expected)
        async with ctx.factory() as db:
            if fault in {"mode", "plan"}:
                plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
                if fault == "plan":
                    plan.version += 1
                else:
                    document = json.loads(json.dumps(plan.plan_document))
                    document["execution_policy"]["evaluation_acceptance"][ctx.eval_address] = "human"
                    plan.plan_document = document
            elif fault == "claim":
                claim = await db.get(OrchestrationWorkClaim, ctx.identity.claim_id)
                claim.generation += 1
            elif fault in {"cycle", "halt"}:
                node = await db.get(OrchestrationNode, ctx.eval_id)
                if fault == "cycle":
                    node.attempts += 1
                else:
                    node.state = "halted"
            await db.commit()
        if fault == "expiry":
            validated = replace(validated, receipt=validated.receipt.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}))
        return validated

    ctx.evaluation_provider.find.side_effect = changed
    if fault == "runtime":
        ctx.evaluation_runtime.verify.side_effect = CycleBlockedError("evaluation_runtime_revision_changed")
    elif fault == "revoke_during_runtime":

        async def revoked(*args):
            authority = ctx.store._read(f"TENANT#{ctx.node.org_id}", f"AUTHORITY#{ctx.approval.id}")
            authority["status"] = {"S": "revoked"}
            ctx.store.client.put_item(TableName=ctx.store.table, Item=authority)
            return datetime.now(UTC)

        ctx.evaluation_runtime.verify.side_effect = revoked
    report = await tick(ctx)
    assert report.errors == 0
    assert (await nodes(ctx))[ctx.eval_id] != "passed"
    assert not await evidence_rows(ctx)
    assert (await nodes(ctx))[ctx.next_id] == "pending"


async def test_concurrent_observers_record_one_acceptance(evaluation):
    import asyncio

    from src.orchestration.evaluation_plan import identity_for
    from src.orchestration.execution_runner import RunnerContext
    from src.orchestration.execution_store import load_execution

    ctx = evaluation
    await tick(ctx)
    row = await evaluation_record(ctx)
    async with ctx.factory() as db:
        record = (await load_execution(db, identity=identity_for(row))).record
    context = RunnerContext(identity_for(row), record, datetime.now(UTC))
    first, second = await asyncio.gather(ctx.evaluation_services.evaluate(context), ctx.evaluation_services.evaluate(context))

    async def commit(observation):
        async with ctx.factory() as db:
            moved = await advance_execution(
                db,
                identity=context.identity,
                advance=PhaseAdvance(
                    phase=ExecutionPhase.EVALUATION_PENDING,
                    status=ExecutionStatus.RUNNABLE,
                    expected_revision=record.revision,
                    next_check_at=datetime.now(UTC),
                ),
            )
            if moved.kind.value == "applied":
                await ctx.evaluation_services.settle(db, RunnerContext(context.identity, moved.record, datetime.now(UTC)), observation)
            await db.commit()
            return moved.kind.value

    outcomes = await asyncio.gather(commit(first), commit(second))
    assert outcomes.count("applied") == 1
    assert len(await evidence_rows(ctx)) == 1
    assert (await nodes(ctx))[ctx.eval_id] == "passed"


async def test_readback_requires_recorded_decision_and_complete_failure_metadata(evaluation):
    from src.orchestration.evaluation_controller import bounded_evaluation_summary
    from src.orchestration.execution_read import _action_view

    ctx = evaluation
    await tick(ctx)
    await tick(ctx)
    row = (await evidence_rows(ctx))[0]
    summary = _action_view(row).evidence_summary
    assert summary["mandatory_passed"] and summary["criteria"] == [{"criterion_id": "API-1", "outcome": "pass"}]
    assert "claim" not in json.dumps(summary)
    for key in ("required_failures", "decision_id", "specification"):
        detail = dict(row.detail)
        detail.pop(key)
        assert bounded_evaluation_summary(detail) is None


async def test_missing_final_d3_cannot_conclude_story_or_start_evaluation(evaluation):
    from src.orchestration.deployment_controller import DEPLOYMENT_KIND

    ctx = evaluation
    async with ctx.factory() as db:
        for row in (await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == DEPLOYMENT_KIND))).all():
            if row.detail["deployment_receipt"]["delivery_complete"]:
                row.status = "failed"
        await db.commit()
    await tick(ctx)
    assert await evaluation_record(ctx) is None
    async with ctx.factory() as db:
        story = await db.get(OrchestrationExecution, ctx.execution.id)
        assert story.status == "blocked" and "final_deployment" in story.block_detail


async def test_worker_dispatch_and_result_cannot_replace_evidence(evaluation):
    from src.orchestration.dispatch import dispatch_node
    from src.orchestration.genesis import resolve_engine_genesis
    from src.orchestration.results import observe_results

    ctx = evaluation
    async with ctx.factory() as db:
        genesis = await resolve_engine_genesis(db, org_id=ctx.node.org_id, decision_id=ctx.approval.id)
        result = await dispatch_node(db, node=ctx.eval_id, genesis=genesis)
        assert result.status.value == "rejected"
    await tick(ctx)
    async with ctx.factory() as db:
        db.add(
            OrchestrationDecision(
                org_id=ctx.node.org_id,
                flow_id=ctx.node.flow_id,
                node_id=ctx.eval_id,
                kind="node_dispatched",
                actor_id="system:orchestration-dispatch",
                actor_kind="service",
                actor_role="engine",
                reason=json.dumps({"attempt": 1, "run_id": "legacy-eval", "arrived_at": "2026-09-20T00:00:00Z"}),
            )
        )
        await db.commit()
    run_store = SimpleNamespace(
        get=lambda *args: {
            "tenant_id": ctx.node.org_id,
            "engine_node_id": ctx.eval_id,
            "engine_attempt": 1,
            "status": "complete",
            "transcript": "all tests passed",
        }
    )
    async with ctx.factory() as db:
        report = await observe_results(db, run_store=run_store)
        await db.commit()
    assert report.advanced == 0
    assert (await nodes(ctx))[ctx.eval_id] == "running"
    assert not await evidence_rows(ctx)


async def test_all_mandatory_work_and_human_gates_keep_rollup_incomplete(evaluation):
    from src.orchestration.display_state import derive_flow_status

    ctx = evaluation
    await tick(ctx)
    await tick(ctx)
    states = list((await nodes(ctx)).values())
    status = derive_flow_status(
        queued=states.count("pending"),
        in_progress=states.count("running"),
        gate=states.count("awaiting_gate"),
        stalled=0,
        complete=states.count("passed"),
    )
    assert status.value != "complete"


@pytest.mark.parametrize("second_kind", ["runtime", "docs", "wrong_target", "missing_receipt"])
async def test_all_predecessors_must_have_compatible_final_deployments(evaluation, second_kind):
    from src.orchestration.deployment_controller import DEPLOYMENT_KIND
    from src.orchestration.execution_state import ActionIntent, ExecutionIdentity, Observation, ObservedOutcome
    from src.orchestration.execution_store import create_execution, prepare_action, record_observation

    ctx = evaluation
    async with ctx.factory() as db:
        original = next(
            row.detail["deployment_receipt"]
            for row in (await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == DEPLOYMENT_KIND))).all()
            if row.detail["deployment_receipt"]["delivery_complete"]
        )
        parent = OrchestrationNode(
            org_id=ctx.node.org_id,
            flow_id=ctx.node.flow_id,
            epic_ref="E1",
            wave_ref="W1",
            node_ref="SECOND",
            kind="story",
            title="Other required predecessor",
            state="passed",
            attempts=1,
        )
        db.add(parent)
        await db.flush()
        db.add(OrchestrationEdge(org_id=ctx.node.org_id, flow_id=ctx.node.flow_id, from_node_id=parent.id, to_node_id=ctx.eval_id))
        identity = ExecutionIdentity(ctx.node.org_id, parent.id, 1, 1, ctx.identity.claim_id, ctx.identity.claim_generation)
        created = await create_execution(
            db, identity=identity, flow_id=parent.flow_id, phase=ExecutionPhase.EVALUATION_PENDING, next_check_at=datetime.now(UTC)
        )
        receipt = json.loads(json.dumps(original))
        receipt.update(
            node_id=parent.id,
            execution_id=created.record.id,
            source_revision="9" * 40,
            operation_key="second-deployment",
            observed_at=(datetime.fromisoformat(original["observed_at"]) - timedelta(seconds=5)).isoformat(),
        )
        if second_kind == "docs":
            receipt.update(docs_only=True, components=[], targets=[], workflow_operation_keys=[], actual_revision=receipt["source_revision"])
        elif second_kind == "wrong_target":
            receipt["targets"][0]["resource_id"] = "different/namespace"
        if second_kind != "missing_receipt":
            await prepare_action(
                db,
                identity=identity,
                intent=ActionIntent(operation_key="second-deployment", kind=DEPLOYMENT_KIND, detail={"deployment_receipt": receipt}),
            )
            await record_observation(
                db, identity=identity, observation=Observation("second-deployment", ObservedOutcome.SUCCEEDED, receipt_ref="second-deployment")
            )
        await db.commit()
    await tick(ctx)
    await tick(ctx)
    if second_kind in {"wrong_target", "missing_receipt"}:
        assert (await nodes(ctx))[ctx.eval_id] == "ready"
        assert not await evidence_rows(ctx)
    else:
        assert (await nodes(ctx))[ctx.eval_id] == "passed"
        if second_kind == "runtime":
            assert any(call.args[1:] == ("9" * 40, original["actual_revision"]) for call in ctx.provider.contains.await_args_list)


async def test_changed_release_is_rejected_by_final_actual_runtime_read(evaluation):
    ctx = evaluation
    del ctx.evaluation_runtime.verify
    await tick(ctx)
    ctx.component = ctx.component.model_copy(update={"actual_revision": "f" * 40})
    report = await tick(ctx)
    assert report.errors == 0
    assert (await nodes(ctx))[ctx.eval_id] == "running"
    assert (await evaluation_record(ctx)).status == "blocked"
    assert not await evidence_rows(ctx)
