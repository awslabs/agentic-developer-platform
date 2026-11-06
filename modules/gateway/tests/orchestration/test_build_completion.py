"""Real merge/ledger recovery must retain the separate live delivery obligations."""

import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select

from src.orchestration.build_completion import (
    KIND,
    BuildCompletionError,
    BuildCompletionRequest,
    accept_build_completion,
    explicit_code_only,
    preview_build_completion,
)
from src.orchestration.compile import ApprovalContext
from src.orchestration.continuation import digest
from src.orchestration.deployment_workflows import DeploymentWorkflows, WorkflowServices
from src.orchestration.execution_runner import RunnerConfig, run_execution_runner
from src.orchestration.execution_state import ExecutionPhase, ExecutionStatus, OutcomeKind, PhaseAdvance
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
from src.orchestration.proposal import ProposedNode
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.state import ActorKind
from tests.orchestration.test_merge_controller import merge, tick  # noqa: F401
from tests.orchestration.test_review_cycle import cycle, pg_server, pg_url, state, store  # noqa: F401


def build_definition():
    return dict(
        address="cycle/E1/W1/N1",
        kind="story",
        title="Bound story",
        issue_ref="43",
        executor=dict(schema_version=1, kind="agent", role="develop", persona="agent-codex-developer"),
        evidence_expectations=["verified-merge"],
    )


@pytest.fixture
async def mixed(merge):  # noqa: F811
    ctx = merge
    async with ctx.factory.kw["bind"].begin() as connection:
        await connection.run_sync(lambda conn: OrchestrationEdge.__table__.create(conn, checkfirst=True))
    ctx.actor = ApprovalContext(org_id=ctx.node.org_id, actor_id="human", actor_role="owner", reason="Retain live deployment and evidence acceptance")
    async with ctx.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = deepcopy(plan.plan_document)
        document["nodes"] = [
            build_definition(),
            dict(
                address="cycle/E1/W2/deploy",
                kind="story",
                title="Deploy the release",
                issue_ref="44",
                executor=dict(schema_version=1, kind="agent", role="operate", persona="operations"),
            ),
            dict(address="cycle/E1/W2/accept", kind="eval", title="Review live evidence", issue_ref=None),
        ]
        document["edges"] = [
            dict(from_address="cycle/E1/W1/N1", to_address="cycle/E1/W2/deploy"),
            dict(from_address="cycle/E1/W2/deploy", to_address="cycle/E1/W2/accept"),
        ]
        document["execution_policy"]["evaluation_acceptance"] = {"cycle/E1/W2/accept": "human"}
        plan.plan_document = document
        plan.plan_hash = digest(document)
        deploy = OrchestrationNode(
            org_id=ctx.node.org_id,
            flow_id=ctx.flow.id,
            epic_ref="E1",
            wave_ref="W2",
            node_ref="deploy",
            kind="story",
            title="Deploy the release",
            issue_ref="44",
            state="pending",
        )
        accept = OrchestrationNode(
            org_id=ctx.node.org_id,
            flow_id=ctx.flow.id,
            epic_ref="E1",
            wave_ref="W2",
            node_ref="accept",
            kind="eval",
            title="Review live evidence",
            state="pending",
        )
        db.add_all([deploy, accept])
        await db.flush()
        db.add_all(
            [
                OrchestrationEdge(org_id=ctx.node.org_id, flow_id=ctx.flow.id, from_node_id=ctx.node.id, to_node_id=deploy.id),
                OrchestrationEdge(org_id=ctx.node.org_id, flow_id=ctx.flow.id, from_node_id=deploy.id, to_node_id=accept.id),
            ]
        )
        await db.commit()
        ctx.deploy_id, ctx.accept_id = deploy.id, accept.id
        ctx.saved_plan = deepcopy(document)
        ctx.completion_request = BuildCompletionRequest(
            node_id=ctx.node.id,
            deployment_node_id=deploy.id,
            acceptance_node_id=accept.id,
            expected_plan_version=plan.version,
            expected_plan_hash=plan.plan_hash,
            reason="Build completion is separate from deployed human acceptance",
        )
    return ctx


async def merged_and_paused(ctx):
    await tick(ctx)
    await tick(ctx)
    execution, _, node, _ = await state(ctx)
    assert node.state == "passed" and execution.phase == "deployment_pending"
    async with ctx.factory() as db:
        flow = await db.get(OrchestrationFlow, ctx.flow.id)
        flow.execution_paused = True
        await db.commit()


async def accept_boundary(ctx, db):
    preview = await preview_build_completion(db, flow_id=ctx.flow.id, actor=ctx.actor, request=ctx.completion_request)
    request = ctx.completion_request.model_copy(update={"expected_snapshot": preview["snapshot"]})
    return await accept_build_completion(db, flow_id=ctx.flow.id, actor=ctx.actor, request=request), request


async def action_snapshot(ctx):
    async with ctx.factory() as db:
        rows = list(await db.scalars(select(OrchestrationAction).where(OrchestrationAction.execution_id == ctx.execution.id)))
        return {(row.id, row.kind, row.status) for row in rows}


def machine_acceptance():
    return dict(
        schema_version=1,
        acceptance_mode="machine",
        evidence_schema="repository-evaluation/v1",
        runner=dict(adapter="engine-repository-evidence-v1", repository="org/repo", repository_id=123, harness_sha256="c" * 64),
        predecessors=[dict(address="cycle/E1/W2/deploy", required_checks=[])],
        workflows=[
            dict(
                criterion_id="release",
                path=".github/workflows/release.yml",
                source=dict(predecessor="cycle/E1/W2/deploy"),
                definition=dict(predecessor="cycle/E1/W2/deploy"),
                required_jobs=["acceptance"],
                artifacts=[
                    dict(
                        name="acceptance-{run_id}-{run_attempt}",
                        path="observations.json",
                        bind_workflow_provenance=True,
                        predicates=[dict(criterion_id="acceptance", pointer="/verified", operation="equals", expected=True)],
                    )
                ],
            )
        ],
    )


async def configure_recovery(ctx, *, expectations, acceptance="machine", alter=None):
    async with ctx.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = deepcopy(plan.plan_document)
        document["nodes"][0]["evidence_expectations"] = expectations
        if acceptance == "machine":
            document["execution_policy"]["allowed_actions"].append("evaluate")
            document["execution_policy"]["evaluation_acceptance"]["cycle/E1/W2/accept"] = acceptance
            document["nodes"][2]["evaluation"] = machine_acceptance()
        if alter is not None:
            alter(document)
        plan.plan_document = document
        plan.plan_hash = digest(document)
        ctx.saved_plan = deepcopy(document)
        ctx.completion_request = ctx.completion_request.model_copy(update={"expected_plan_hash": plan.plan_hash})
        await db.commit()


@pytest.mark.parametrize("cycle", [{"delivery": True}], indirect=True)
@pytest.mark.parametrize("expectations,acceptance", [([], "human"), ([], "machine"), (["verified-merge"], "machine")])
async def test_recovery_retains_legacy_build_and_machine_delivery_obligations(mixed, expectations, acceptance):
    await configure_recovery(mixed, expectations=expectations, acceptance=acceptance)
    await merged_and_paused(mixed)
    before = await action_snapshot(mixed)
    async with mixed.factory() as db:
        result, request = await accept_boundary(mixed, db)
        await db.commit()
        assert result["acceptance_preserved"] and result["acceptance_mode"] == acceptance
        assert result["human_acceptance_preserved"] == (acceptance == "human")
        assert result["content"]["deployment_verified"] is False
        assert result["content"]["acceptance_granted"] is False
        assert await explicit_code_only(
            db, plan=await db.get(OrchestrationAcceptedPlan, mixed.plan.id), node=await db.get(OrchestrationNode, mixed.node.id)
        )
    services = WorkflowServices(mixed.factory)

    async def never_deploy(*args, **kwargs):
        raise AssertionError("retained operations must own deployment")

    services.snapshot = never_deploy
    report = await run_execution_runner(
        mixed.factory,
        handlers={ExecutionPhase.DEPLOYMENT_PENDING: DeploymentWorkflows(mixed.factory, services)},
        config=RunnerConfig(enabled=True, max_attempts=8, io_timeout_seconds=10),
    )
    assert report.errors == 0 and report.effects_attempted == 0
    execution, _, node, _ = await state(mixed)
    assert execution.status == "concluded" and node.state == "passed"
    assert await action_snapshot(mixed) == before
    async with mixed.factory() as db:
        assert (await db.get(OrchestrationAcceptedPlan, mixed.plan.id)).plan_document == mixed.saved_plan
        assert (await db.get(OrchestrationNode, mixed.deploy_id)).state == "pending"
        assert (await db.get(OrchestrationNode, mixed.accept_id)).state == "pending"
        replay = await accept_build_completion(db, flow_id=mixed.flow.id, actor=mixed.actor, request=request)
        assert not replay["created"] and replay["decision_id"] == result["decision_id"]


@pytest.mark.parametrize("cycle", [{"delivery": True}], indirect=True)
@pytest.mark.parametrize("obstruction", ["missing_spec", "checks_only", "unbound_source", "unbound_artifact", "human_spec"])
async def test_machine_recovery_requires_executable_acceptance_of_operations(mixed, obstruction):
    def alter(document):
        accept = document["nodes"][2]
        spec = accept["evaluation"]
        if obstruction == "missing_spec":
            accept["evaluation"] = None
        elif obstruction == "checks_only":
            spec["workflows"] = []
        elif obstruction == "unbound_source":
            spec["workflows"][0]["source"] = dict(revision="a" * 40)
        elif obstruction == "unbound_artifact":
            spec["workflows"][0]["artifacts"][0]["bind_workflow_provenance"] = False
        else:
            spec["acceptance_mode"] = "human"

    await configure_recovery(mixed, expectations=[], alter=alter)
    await merged_and_paused(mixed)
    async with mixed.factory() as db:
        with pytest.raises(BuildCompletionError, match="machine_delivery_acceptance_required"):
            await preview_build_completion(db, flow_id=mixed.flow.id, actor=mixed.actor, request=mixed.completion_request)
        assert await db.scalar(select(OrchestrationDecision.id).where(OrchestrationDecision.kind == KIND)) is None


@pytest.mark.parametrize("cycle", [{"delivery": True}], indirect=True)
@pytest.mark.parametrize("obligation", ["evidence", "criteria"])
async def test_legacy_recovery_cannot_discard_declared_build_obligations(mixed, obligation):
    def alter(document):
        build = document["nodes"][0]
        if obligation == "evidence":
            build["evidence_expectations"] = ["verified-merge", "runtime_verified:service"]
        else:
            build["criteria"] = ["Observe the deployed service before completing this node"]

    await configure_recovery(mixed, expectations=[], alter=alter)
    await merged_and_paused(mixed)
    async with mixed.factory() as db:
        with pytest.raises(BuildCompletionError, match="verified_merge_build_required"):
            await preview_build_completion(db, flow_id=mixed.flow.id, actor=mixed.actor, request=mixed.completion_request)
        assert await db.scalar(select(OrchestrationDecision.id).where(OrchestrationDecision.kind == KIND)) is None


@pytest.mark.parametrize("cycle", [{"delivery": True}], indirect=True)
async def test_machine_recovery_cannot_reinterpret_retained_acceptance(mixed):
    await configure_recovery(mixed, expectations=[])
    await merged_and_paused(mixed)
    async with mixed.factory() as db:
        await accept_boundary(mixed, db)
        await db.commit()
        plan = await db.get(OrchestrationAcceptedPlan, mixed.plan.id)
        document = deepcopy(plan.plan_document)
        document["nodes"][2]["evaluation"]["workflows"][0]["artifacts"][0]["predicates"][0]["expected"] = False
        plan.plan_document = document
        await db.commit()
        with pytest.raises(CycleBlockedError, match="build_completion_contract_changed"):
            await explicit_code_only(db, plan=plan, node=await db.get(OrchestrationNode, mixed.node.id))


@pytest.mark.parametrize("cycle", [{"delivery": True}], indirect=True)
async def test_explicit_plan_build_completes_at_merge_in_mixed_flow(mixed):
    async with mixed.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, mixed.plan.id)
        document = deepcopy(plan.plan_document)
        document["nodes"][0]["delivery_mode"] = "code_only"
        plan.plan_document = document
        await db.commit()
    await tick(mixed)
    await tick(mixed)
    execution, _, node, actions = await state(mixed)
    assert execution.status == "concluded" and node.state == "passed"
    recorded = await action_snapshot(mixed)
    assert any(kind == "merge_pull_request" and status == "succeeded" for _, kind, status in recorded)
    assert not any(kind.startswith("deployment_") for _, kind, _ in recorded)
    async with mixed.factory() as db:
        assert (await db.get(OrchestrationNode, mixed.deploy_id)).state == "pending"
        assert (await db.get(OrchestrationNode, mixed.accept_id)).state == "pending"


@pytest.mark.parametrize("cycle", [{"delivery": True}], indirect=True)
async def test_recovery_fences_observations_and_concludes_only_verified_build(mixed):
    await merged_and_paused(mixed)
    before, claim, _, actions = await state(mixed)
    before_actions = await action_snapshot(mixed)
    async with mixed.factory() as db:
        preview = await preview_build_completion(db, flow_id=mixed.flow.id, actor=mixed.actor, request=mixed.completion_request)
        assert preview["wrote_nothing"] and preview["human_acceptance_preserved"]
        assert await db.scalar(select(OrchestrationDecision.id).where(OrchestrationDecision.kind == KIND)) is None
        result, request = await accept_boundary(mixed, db)
        await db.commit()
        assert result["content"]["deployment_verified"] is False and result["content"]["acceptance_granted"] is False
        after = await db.get(OrchestrationExecution, before.id)
        assert after.revision == before.revision + 1 and after.phase == before.phase and after.status == before.status
        assert after.attempts == before.attempts and after.claim_generation == before.claim_generation
        stale = await advance_execution(
            db,
            identity=mixed.identity,
            advance=PhaseAdvance(
                expected_revision=before.revision,
                phase=ExecutionPhase.DEPLOYMENT_PENDING,
                status=ExecutionStatus.RUNNABLE,
                next_check_at=after.next_check_at,
                consume_attempt=True,
            ),
        )
        assert stale.kind is OutcomeKind.STALE
        assert (await db.get(OrchestrationAcceptedPlan, mixed.plan.id)).plan_document == mixed.saved_plan
        assert (await db.get(OrchestrationNode, mixed.accept_id)).state == "pending"
    services = WorkflowServices(mixed.factory)

    async def never_deploy(*args, **kwargs):
        raise AssertionError("build recovery must not enter deployment selection or dispatch")

    services.snapshot = never_deploy
    report = await run_execution_runner(
        mixed.factory,
        handlers={ExecutionPhase.DEPLOYMENT_PENDING: DeploymentWorkflows(mixed.factory, services)},
        config=RunnerConfig(enabled=True, max_attempts=8, io_timeout_seconds=10),
    )
    assert report.errors == 0 and report.effects_attempted == 0
    final, after_claim, node, final_actions = await state(mixed)
    assert final.status == "concluded" and node.state == "passed"
    assert after_claim.id == claim.id and after_claim.generation == claim.generation
    assert {(a.id, a.kind, a.status) for a in final_actions} == {(a.id, a.kind, a.status) for a in actions}
    assert await action_snapshot(mixed) == before_actions
    async with mixed.factory() as db:
        replay = await accept_build_completion(db, flow_id=mixed.flow.id, actor=mixed.actor, request=request)
        assert not replay["created"] and replay["decision_id"] == result["decision_id"]
        assert (await db.get(OrchestrationNode, mixed.deploy_id)).state == "pending"
        assert (await db.get(OrchestrationNode, mixed.accept_id)).state == "pending"


@pytest.mark.parametrize("cycle", [{"delivery": True}], indirect=True)
@pytest.mark.parametrize(
    "obstruction,expected",
    [
        ("unpaused", "pause_flow_before_build_recovery"),
        ("pending_effect", "build_recovery_requires_unstarted_deployment"),
        ("deployment_effect", "deployment_effect_requires_reconciliation"),
        ("missing_merge", "verified_merge_receipt_unavailable"),
        ("missing_path", "delivery_dependency_path_required"),
        ("graph_path", "delivery_graph_path_changed"),
        ("machine_acceptance", "delivery_acceptance_required"),
        ("other_tenant", "flow_not_found"),
        ("service_actor", "human_plan_approver_required"),
    ],
)
async def test_recovery_refuses_unverified_or_in_flight_work(mixed, obstruction, expected):
    await merged_and_paused(mixed)
    actor = mixed.actor
    async with mixed.factory() as db:
        execution = await db.get(OrchestrationExecution, mixed.execution.id)
        if obstruction == "unpaused":
            (await db.get(OrchestrationFlow, mixed.flow.id)).execution_paused = False
        elif obstruction == "pending_effect":
            execution.pending_action_key = "uncertain-dispatch"
        elif obstruction == "deployment_effect":
            db.add(
                OrchestrationAction(
                    org_id=mixed.node.org_id, execution_id=execution.id, operation_key="recorded-deploy", kind="deployment_workflow", status="unknown"
                )
            )
        elif obstruction == "missing_merge":
            row = await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "merge_pull_request"))
            detail = dict(row.detail)
            detail.pop("merge_receipt")
            row.detail = detail
        elif obstruction == "graph_path":
            await db.execute(delete(OrchestrationEdge).where(OrchestrationEdge.flow_id == mixed.flow.id))
        elif obstruction in {"missing_path", "machine_acceptance"}:
            plan = await db.get(OrchestrationAcceptedPlan, mixed.plan.id)
            document = deepcopy(plan.plan_document)
            if obstruction == "missing_path":
                document["edges"] = []
            else:
                document["execution_policy"]["evaluation_acceptance"]["cycle/E1/W2/accept"] = "machine"
            plan.plan_document = document
        elif obstruction == "other_tenant":
            actor = replace(actor, org_id="other-org")
        elif obstruction == "service_actor":
            actor = replace(actor, actor_kind=ActorKind.SERVICE)
        await db.commit()
        with pytest.raises(BuildCompletionError, match=expected):
            await preview_build_completion(db, flow_id=mixed.flow.id, actor=actor, request=mixed.completion_request)
        assert await db.scalar(select(OrchestrationDecision.id).where(OrchestrationDecision.kind == KIND)) is None


@pytest.mark.parametrize("cycle", [{"delivery": True}], indirect=True)
async def test_changed_snapshot_cannot_recover_and_changed_downstream_cannot_settle(mixed):
    await merged_and_paused(mixed)
    async with mixed.factory() as db:
        preview = await preview_build_completion(db, flow_id=mixed.flow.id, actor=mixed.actor, request=mixed.completion_request)
        request = mixed.completion_request.model_copy(update={"expected_snapshot": preview["snapshot"]})
        execution = await db.get(OrchestrationExecution, mixed.execution.id)
        execution.revision += 1
        await db.commit()
        with pytest.raises(BuildCompletionError, match="build_completion_snapshot_changed"):
            await accept_build_completion(db, flow_id=mixed.flow.id, actor=mixed.actor, request=request)
        await accept_boundary(mixed, db)
        await db.commit()
        plan = await db.get(OrchestrationAcceptedPlan, mixed.plan.id)
        document = deepcopy(plan.plan_document)
        document["edges"] = []
        plan.plan_document = document
        await db.commit()
        with pytest.raises(CycleBlockedError, match="build_completion_contract_changed"):
            await explicit_code_only(db, plan=plan, node=await db.get(OrchestrationNode, mixed.node.id))


@pytest.mark.parametrize("changes", [{"kind": "eval"}, {"executor": None}, {"evidence_expectations": []}, {"delivery_mode": "live"}])
def test_completion_mode_requires_explicit_build_contract(changes):
    with pytest.raises(ValidationError):
        ProposedNode.model_validate({**build_definition(), "delivery_mode": "code_only", **changes})


@pytest.mark.parametrize("cycle", [{"delivery": True}], indirect=True)
async def test_recovery_yields_to_in_flight_execution_and_releases_locks(mixed):
    await merged_and_paused(mixed)
    async with mixed.factory() as db:
        preview = await preview_build_completion(db, flow_id=mixed.flow.id, actor=mixed.actor, request=mixed.completion_request)
        request = mixed.completion_request.model_copy(update={"expected_snapshot": preview["snapshot"]})
        await db.commit()
        async with mixed.factory() as runner:
            await runner.scalar(select(OrchestrationExecution).where(OrchestrationExecution.id == mixed.execution.id).with_for_update())
            with pytest.raises(BuildCompletionError, match="build_completion_in_progress"):
                await asyncio.wait_for(accept_build_completion(db, flow_id=mixed.flow.id, actor=mixed.actor, request=request), timeout=2)
            flow = await asyncio.wait_for(
                runner.scalar(select(OrchestrationFlow).where(OrchestrationFlow.id == mixed.flow.id).with_for_update()), timeout=2
            )
            assert flow.execution_paused
        assert await db.scalar(select(OrchestrationDecision.id).where(OrchestrationDecision.kind == KIND)) is None


def test_node_preservation_includes_the_completion_boundary():
    from src.orchestration.amend import _execution_definition

    original = build_definition()
    assert _execution_definition(original) == _execution_definition({**original, "delivery_mode": None})
    assert _execution_definition(original) != _execution_definition({**original, "delivery_mode": "code_only"})
    assert "delivery_mode" not in ProposedNode.model_validate(original).model_dump(mode="json")
    assert ProposedNode.model_validate({**original, "delivery_mode": "code_only"}).model_dump(mode="json")["delivery_mode"] == "code_only"


@pytest.mark.parametrize("cycle", [{"delivery": True}], indirect=True)
async def test_recovery_normalizes_defaults_and_refuses_superseded_replay(mixed):
    await merged_and_paused(mixed)
    async with mixed.factory() as db:
        first, original_request = await accept_boundary(mixed, db)
        await db.commit()
        plan = await db.get(OrchestrationAcceptedPlan, mixed.plan.id)
        document = deepcopy(plan.plan_document)
        document["nodes"] = [ProposedNode.model_validate(raw).model_dump(mode="json") for raw in document["nodes"]]
        plan.plan_document = document
        await db.commit()
        assert await explicit_code_only(db, plan=plan, node=await db.get(OrchestrationNode, mixed.node.id))
        mixed.completion_request = mixed.completion_request.model_copy(update={"reason": "Confirm the retained delivery boundary again"})
        second, _ = await accept_boundary(mixed, db)
        await db.commit()
        assert second["decision_id"] != first["decision_id"]
        with pytest.raises(BuildCompletionError, match="build_completion_contract_superseded"):
            await accept_build_completion(db, flow_id=mixed.flow.id, actor=mixed.actor, request=original_request)
        node = await db.get(OrchestrationNode, mixed.node.id)
        node.attempts += 1
        await db.commit()
        with pytest.raises(CycleBlockedError, match="build_completion_contract_changed"):
            await explicit_code_only(db, plan=plan, node=node)
