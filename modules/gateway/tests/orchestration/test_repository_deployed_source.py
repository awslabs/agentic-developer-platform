"""Deployed source identity comes from a verified operations action in PostgreSQL."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration import evaluation_plan
from src.orchestration.adapters.github_comments import GateAnswerStatus, InputPath, apply_gate_answer_for_context
from src.orchestration.delivery_progress import node_progress
from src.orchestration.deployment_controller import DEPLOYMENT_KIND, DeploymentServices
from src.orchestration.deployment_manifest import DeploymentManifest, PathRule, TargetEvidence
from src.orchestration.deployment_runtime_contract import DeploymentReceipt, ReleaseArtifact, RuntimeComponent
from src.orchestration.deployment_runtime_reader import PilotRuntimeReader
from src.orchestration.evaluation_contract import multi_repository_models
from src.orchestration.evaluation_plan import human_evaluation, human_gate_evidence, validate_human_gate_evidence
from src.orchestration.gate_execution import prepare_gate_execution
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationFlow,
    OrchestrationNode,
)
from src.orchestration.repository_evaluation import deployment_source, multi_repository_sources
from src.orchestration.repository_evaluation_diagnostics import current_diagnostic
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.tick import run_tick
from tests.orchestration.test_deployment_controller import cycle, deployment, finish, merge, pg_server, pg_url, runtime, store  # noqa: F401
from tests.orchestration.test_deployment_controller import tick as runtime_tick
from tests.orchestration.test_deployment_target import target  # noqa: F401
from tests.orchestration.test_deployment_workflows import tick as workflow_tick
from tests.orchestration.test_evaluation_controller import gate_access
from tests.orchestration.test_multi_repository_evaluation_contract import evaluation

pytestmark = pytest.mark.parametrize("cycle", [{"delivery": True}], indirect=True)


@pytest.mark.parametrize("fault", [None, "future-observation", "extended-validity"])
async def test_verified_deployment_exports_exact_runtime_artifacts_for_probe(runtime, fault):  # noqa: F811
    context = runtime
    context.target = replace(context.target, evidence=TargetEvidence("registered-aws-role:delivery-connection", datetime.now(UTC).isoformat()))
    await finish(context)
    async with context.factory() as session:
        parent = await session.get(OrchestrationNode, context.node.id)
        parent.repository_identity = context.binding.repo
        parent.provider_repository_id = context.binding.provider_repository_id
        action = await session.scalar(
            select(OrchestrationAction).where(
                OrchestrationAction.execution_id == context.execution.id,
                OrchestrationAction.kind == DEPLOYMENT_KIND,
                OrchestrationAction.status == "succeeded",
                OrchestrationAction.detail["deployment_receipt"]["delivery_complete"].as_boolean().is_(True),
            )
        )
        assert action is not None
        if fault is not None:
            changed = dict(action.detail["deployment_receipt"])
            if fault == "future-observation":
                changed["observed_at"] = (datetime.now(UTC) + timedelta(minutes=2)).isoformat()
                changed["valid_until"] = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
            else:
                changed["valid_until"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
            action.detail = {**action.detail, "deployment_receipt": changed}
            await session.flush()
        receipt = DeploymentReceipt.model_validate(action.detail["deployment_receipt"])
        plan = await session.get(OrchestrationAcceptedPlan, context.plan.id)
        declaration = evaluation("deployed")
        declaration["predecessors"] = [{"address": "cycle/E1/W1/N1", "repository_id": parent.provider_repository_id, "evidence_kind": "deployment"}]
        declaration["environment"]["deployment_receipt_keys"] = [receipt.operation_key]
        specification = multi_repository_models().EvaluationSpecification.model_validate(declaration)

        async def resolve():
            return await deployment_source(
                session,
                node=SimpleNamespace(org_id=parent.org_id, flow_id=parent.flow_id),
                parent=parent,
                plan=plan,
                spec=specification,
                expected=specification.predecessors[0],
            )

        if fault is not None:
            with pytest.raises(CycleBlockedError, match="predecessor_deployment_expired"):
                await resolve()
            return
        source = await resolve()
        assert source["deployment_receipt_key"] == action.operation_key
        assert source["head_sha"] == receipt.actual_revision
        assert source["deployment_targets"] == receipt.targets
        assert source["runtime_artifacts"] == [
            {
                "component": component.component,
                "actual_revision": component.actual_revision,
                "artifact_hash": component.artifact_hash,
                "image_digest": component.image_digest,
                "tick_digest": component.tick_digest,
                "migration_head": component.migration_head,
                "asset_count": component.asset_count,
            }
            for component in receipt.components
        ]


async def issue_qualification(context, component_name, target_fixture):
    client = component_name == "qualification-client"
    resource = "cluster/namespace" if client else "cluster/namespace/sample-api"
    resource_kind = "eks-namespace" if client else "eks-deployment"
    workflow = ".github/workflows/client-deploy.yml" if client else ".github/workflows/api-deploy.yml"
    command = {"image": "registry.example/client@sha256:" + "f" * 64, "command": ["python", "client.py"], "args": ["--health"]}
    command_digest = hashlib.sha256(json.dumps(command, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    client_release = {"qualification_job": "client-run-42", "cli_manifest_sha256": command_digest} if client else {}
    context.target = replace(
        context.target,
        resource_kind=resource_kind,
        resource_id=resource,
        evidence=TargetEvidence("registered-aws-role:delivery-connection", datetime.now(UTC).isoformat()),
    )
    context.entry = replace(
        context.entry,
        resource_kind=resource_kind,
        resource_id=resource,
        component_selectors=(component_name,),
        verification_adapter="qualification-client-job-verification" if client else "qualification-api-runtime-verification",
        workflow=replace(context.entry.workflow, path=workflow),
    )
    context.provider.changed_files = AsyncMock(return_value=("client/cli.py",) if client else ("api/server.py",))
    context.workflow_services.manifest_loader = lambda: DeploymentManifest(
        1, (context.entry,), (PathRule("client/*" if client else "api/*", component_name),)
    )
    target_fixture.credential.id = "delivery-connection"
    target_fixture.credential.label = "test-role"
    target_fixture.resolver.secrets.get_secret.return_value = json.dumps({"role_arn": "arn:aws:iam::123456789012:role/test"})
    target_fixture.session_factory.return_value.region_name = context.target.region
    context.workflow_services.targets = target_fixture.resolver
    original_run = context.make_run

    def api_run():
        run = original_run()
        return replace(run, context=run.context.model_copy(update={"workflow_path": workflow}))

    context.make_run = api_run
    context.runs.append(api_run())
    await workflow_tick(context)
    await workflow_tick(context)
    context.provider.release = AsyncMock(
        return_value=(
            ReleaseArtifact(
                schema_version=1,
                repository_id=context.binding.provider_repository_id,
                run_id=42,
                run_attempt=1,
                source_revision=context.entry.artifact_revision,
                workflow_revision=context.entry.workflow.definition_revision,
                workflow_path=workflow,
                account_id=context.target.account_id,
                region=context.target.region,
                resource_id=resource,
                component=component_name,
                image_digest="sha256:" + "f" * 64,
                produced_at=datetime.now(UTC),
                **client_release,
            ),
            "f" * 64,
            "github/actions/runs/42/artifacts/88",
        )
    )
    context.provider.contains = AsyncMock()
    component = RuntimeComponent(
        component=component_name,
        actual_revision=context.entry.artifact_revision,
        artifact_hash="f" * 64,
        image_digest="sha256:" + "f" * 64,
        healthy=True,
        evidence_ref="github/actions/runs/42/artifacts/88",
        observed_at=datetime.now(UTC),
        pod_uids=["pod-uid"],
        cli_manifest_sha256=command_digest if client else None,
    )

    kube = SimpleNamespace(qualification_api=lambda _: ["pod-uid"], qualification_client=lambda _: ["pod-uid"], close=lambda: None)
    context.runtime_services = DeploymentServices(
        context.factory,
        authority=context.service,
        provider=context.provider,
        targets=target_fixture.resolver,
        runtime=PilotRuntimeReader(kubernetes=lambda *args: kube),
        manifest_loader=context.workflow_services.manifest_loader,
    )
    await finish(context)
    for _ in range(2):
        await runtime_tick(context)
    async with context.factory() as session:
        parent = await session.get(OrchestrationNode, context.node.id)
        parent.repository_identity = context.binding.repo
        parent.provider_repository_id = context.binding.provider_repository_id
        actions = list(
            await session.scalars(
                select(OrchestrationAction).where(
                    OrchestrationAction.execution_id == context.execution.id,
                    OrchestrationAction.kind == DEPLOYMENT_KIND,
                    OrchestrationAction.status == "succeeded",
                )
            )
        )
        final = [row for row in actions if row.detail["deployment_receipt"]["delivery_complete"]]
        assert len(final) == 1
        receipt = DeploymentReceipt.model_validate(final[0].detail["deployment_receipt"])
        assert receipt.repo == context.binding.repo
        assert len(receipt.components) == 1
        actual_component = receipt.components[0]
        assert actual_component.component == component.component
        assert actual_component.actual_revision == component.actual_revision
        assert actual_component.image_digest == component.image_digest
        assert actual_component.pod_uids == component.pod_uids
        assert actual_component.cli_manifest_sha256 == component.cli_manifest_sha256
        declaration = evaluation("deployed")
        declaration["predecessors"] = [{"address": "cycle/E1/W1/N1", "repository_id": parent.provider_repository_id, "evidence_kind": "deployment"}]
        declaration["environment"]["deployment_receipt_keys"] = [final[0].operation_key]
        spec = multi_repository_models().EvaluationSpecification.model_validate(declaration)
        source = await deployment_source(
            session,
            node=SimpleNamespace(org_id=parent.org_id, flow_id=parent.flow_id),
            parent=parent,
            plan=await session.get(OrchestrationAcceptedPlan, context.plan.id),
            spec=spec,
            expected=spec.predecessors[0],
        )
        assert source["runtime_artifacts"][0]["component"] == component_name
        assert source["runtime_artifacts"][0]["image_digest"] == component.image_digest
        assert source["deployment_targets"] == receipt.targets
        await session.commit()
        return receipt, source


@pytest.mark.parametrize("component_name", ["qualification-api", "qualification-client"])
async def test_qualification_verification_issues_scoped_deployment_source(deployment, component_name, target):  # noqa: F811
    await issue_qualification(deployment, component_name, target)


async def second_qualification_story(context):
    from copy import deepcopy

    from sqlalchemy import inspect

    from src.orchestration.deployment_workflows import WorkflowServices
    from src.orchestration.dispatch_pass import DispatchPassConfig, PendingPublish, _build_envelope, attempt_run_id
    from src.orchestration.execution_state import ExecutionIdentity, ExecutionPhase, ExecutionStatus
    from src.orchestration.genesis import resolve_engine_genesis
    from src.orchestration.merge_controller import MERGE_KIND
    from src.orchestration.models import OrchestrationExecution

    def clone(row, **changes):
        values = {
            column.key: deepcopy(getattr(row, column.key))
            for column in inspect(row).mapper.column_attrs
            if column.key not in {"id", "created_at", "updated_at"}
        }
        return type(row)(**{**values, **changes})

    async with context.factory() as session:
        parent = await session.get(OrchestrationNode, context.node.id)
        source = OrchestrationNode(
            org_id=parent.org_id,
            flow_id=parent.flow_id,
            epic_ref=parent.epic_ref,
            wave_ref=parent.wave_ref,
            node_ref="B",
            kind="story",
            title="Client source",
            issue_ref="18",
            state="passed",
            attempts=parent.attempts,
            repository_identity="fixture/source-b",
            provider_repository_id=202,
            repository_connection={"provider": "github", "installation_id": 42},
        )
        session.add(source)
        await session.flush()
        root = attempt_run_id(source.id, source.attempts)
        claim = clone(context.claim, id="cycle-claim-b", provider_repository_id=202, issue_number=18, active_run_id=root, claim_event_id=root)
        binding = clone(
            context.binding,
            node_id=source.id,
            repo=source.repository_identity,
            provider_repository_id=202,
            provider_pr_node_id="PR_client",
            installation_id=42,
            accepted_scope=json.dumps({"node": {"kind": "story", "issue_ref": "18", "title": source.title}}),
        )
        session.add_all([claim, binding])
        previous = await session.get(OrchestrationExecution, context.execution.id)
        execution = clone(previous, node_id=source.id, claim_id=claim.id, phase=ExecutionPhase.DEPLOYMENT_PENDING, status=ExecutionStatus.RUNNABLE)
        session.add(execution)
        await session.flush()
        merge_action = await session.scalar(
            select(OrchestrationAction).where(OrchestrationAction.execution_id == context.execution.id, OrchestrationAction.kind == MERGE_KIND)
        )
        assert merge_action is not None and merge_action.status == "succeeded"
        merge_detail = deepcopy(merge_action.detail)
        merge_detail["merge_receipt"].update(
            node_id=source.id,
            execution_id=execution.id,
            claim_id=claim.id,
            repo=binding.repo,
            provider_repository_id=202,
            provider_pr_node_id=binding.provider_pr_node_id,
        )
        session.add(clone(merge_action, execution_id=execution.id, detail=merge_detail))
        plan = await session.get(OrchestrationAcceptedPlan, context.plan.id)
        document = deepcopy(plan.plan_document)
        document["execution_policy"]["repository_ids"] = [context.binding.repo, binding.repo, "fixture/harness"]
        document["product_id"] = "00000000-0000-4000-8000-000000000001"
        plan.plan_document = document
        flow = await session.get(OrchestrationFlow, parent.flow_id)
        flow.product_id = document["product_id"]
        from src.products.models import Product
        from src.shared.models.base import Base
        from src.shared.models.organization import Team, TeamMembership, User
        from tests.orchestration.product_fixture import seed_product

        connection = await session.connection()
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=[User.__table__, Team.__table__, TeamMembership.__table__, Product.__table__])
        )
        await seed_product(session, org_id=source.org_id, actor_id="human", product_id=flow.product_id)
        genesis = await resolve_engine_genesis(session, org_id=parent.org_id, decision_id=context.approval.id)
        await session.commit()

    config = DispatchPassConfig(queue_url="https://sqs.test/cycle.fifo", repo=binding.repo)
    envelope = _build_envelope(
        node=source,
        genesis=genesis,
        graph_address="cycle/E1/W1/B",
        installation_id=42,
        issue=18,
        config=config,
        user_id="human",
        cognito_sub="sub",
    )
    envelope["source_ref"]["provider_repository_id"] = 202
    envelope["handoff_required"] = True
    context.service.writer.provision(
        PendingPublish(
            node_id=source.id,
            org_id=source.org_id,
            envelope=envelope,
            group_id="cycle",
            deduplication_id=root,
            genesis=genesis,
            node_attempt=source.attempts,
        )
    )
    await context.finish(root)
    second = SimpleNamespace(**vars(context))
    second.node, second.claim, second.binding, second.execution = source, claim, binding, execution
    second.identity = ExecutionIdentity(source.org_id, source.id, source.attempts, context.plan.version, claim.id, claim.generation)
    second.root = root
    second.runs, second.dispatches = [], []
    previous_run = context.make_run

    def make_run():
        run = previous_run()
        return replace(
            run,
            url=f"https://github.com/{binding.repo}/actions/runs/{run.run_id}",
            context=run.context.model_copy(update={"repository_id": 202}),
        )

    second.make_run = make_run
    second.provider = SimpleNamespace(
        definition=context.provider.definition,
        changed_files=AsyncMock(return_value=("client/cli.py",)),
        observe=AsyncMock(side_effect=lambda *args, **kwargs: (second.runs[0] if second.runs else None, False)),
    )

    async def dispatch(*args, **kwargs):
        await kwargs["reauthorize"]()
        second.dispatches.append(kwargs["correlation"])
        second.runs.append(second.make_run())

    second.provider.dispatch = AsyncMock(side_effect=dispatch)
    second.workflow_services = WorkflowServices(
        context.factory,
        authority=context.service,
        provider=second.provider,
        targets=context.workflow_services.targets,
        manifest_loader=context.workflow_services.manifest_loader,
    )
    return second


async def test_product_bound_deployed_evaluation_requires_both_server_receipts(runtime, monkeypatch):  # noqa: F811
    context = runtime
    context.target = replace(context.target, evidence=TargetEvidence("registered-aws-role:delivery-connection", datetime.now(UTC).isoformat()))
    await finish(context)
    async with context.factory() as session:
        connection = await session.connection()
        await connection.run_sync(lambda sync: OrchestrationEdge.__table__.create(sync, checkfirst=True))
        first = await session.get(OrchestrationNode, context.node.id)
        first.repository_identity = context.binding.repo
        first.provider_repository_id = context.binding.provider_repository_id
        first.repository_connection = {"provider": "github", "installation_id": context.binding.installation_id}
        second = OrchestrationNode(
            org_id=first.org_id,
            flow_id=first.flow_id,
            epic_ref="E1",
            wave_ref="W1",
            node_ref="B",
            kind="story",
            title="Second deployed source",
            issue_ref="18",
            state="passed",
            attempts=1,
            repository_identity="fixture/source-b",
            provider_repository_id=202,
            repository_connection={"provider": "github", "installation_id": 902},
        )
        evaluation_node = OrchestrationNode(
            org_id=first.org_id,
            flow_id=first.flow_id,
            epic_ref="E1",
            wave_ref="W1",
            node_ref="EVAL",
            kind="eval",
            title="Combined deployment evaluation",
            issue_ref="19",
            state="ready",
            attempts=0,
            repository_identity="fixture/harness",
            provider_repository_id=303,
            repository_connection={"provider": "github", "installation_id": 903},
        )
        session.add_all([second, evaluation_node])
        await session.flush()
        session.add_all(
            OrchestrationEdge(org_id=first.org_id, flow_id=first.flow_id, from_node_id=source.id, to_node_id=evaluation_node.id)
            for source in (first, second)
        )
        flow = await session.get(OrchestrationFlow, first.flow_id)
        flow.product_id = "00000000-0000-4000-8000-000000000001"
        plan = await session.get(OrchestrationAcceptedPlan, context.plan.id)
        plan.plan_document = {**plan.plan_document, "product_id": flow.product_id}
        first_receipt = next(
            row.detail["deployment_receipt"]
            for row in await session.scalars(
                select(OrchestrationAction).where(
                    OrchestrationAction.execution_id == context.execution.id,
                    OrchestrationAction.kind == DEPLOYMENT_KIND,
                    OrchestrationAction.status == "succeeded",
                )
            )
            if row.detail["deployment_receipt"]["delivery_complete"]
        )
        document = evaluation("deployed")
        document["predecessors"] = [
            {"address": f"cycle/E1/W1/{source.node_ref}", "repository_id": source.provider_repository_id, "evidence_kind": "deployment"}
            for source in (first, second)
        ]
        document["environment"]["connection_id"] = "delivery-connection"
        document["environment"]["deployment_receipt_keys"] = [first_receipt["operation_key"], "missing-second-receipt"]
        specification = multi_repository_models().EvaluationSpecification.model_validate(document)
        from src.orchestration import repository_binding

        resolve = AsyncMock(side_effect=AssertionError("missing B must block before repository I/O"))
        monkeypatch.setattr(repository_binding, "resolve_story_repository", resolve)
        with pytest.raises(CycleBlockedError, match="predecessor_deployment_receipt_missing"):
            await multi_repository_sources(session, evaluation_node, plan, specification)
        resolve.assert_not_awaited()


async def product_bound_deployed_probe_case(
    deployment,  # noqa: F811
    target,  # noqa: F811
    monkeypatch,
    tmp_path,
    fault,
    *,
    expiring_a=False,
    human_mode=False,
    product_gate_ready=False,  # noqa: F811
):
    if expiring_a:
        from src.orchestration import deployment_controller

        with monkeypatch.context() as bounded:
            bounded.setattr(deployment_controller, "RECEIPT_LIFETIME", timedelta(seconds=35))
            first_receipt, first_source = await issue_qualification(deployment, "qualification-api", target)
    else:
        first_receipt, first_source = await issue_qualification(deployment, "qualification-api", target)
    second = await second_qualification_story(deployment)
    second_receipt, second_source = await issue_qualification(second, "qualification-client", target)
    assert first_receipt.flow_id == second_receipt.flow_id
    assert first_receipt.node_id != second_receipt.node_id
    assert first_receipt.repo != second_receipt.repo
    assert first_receipt.operation_key != second_receipt.operation_key
    assert {first_source["runtime_artifacts"][0]["component"], second_source["runtime_artifacts"][0]["component"]} == {
        "qualification-api",
        "qualification-client",
    }

    from src.orchestration import repository_binding
    from src.orchestration.repository_evaluation_contract import canonical, harness_digest

    async with deployment.factory() as session:
        connection = await session.connection()
        await connection.run_sync(lambda sync: OrchestrationEdge.__table__.create(sync, checkfirst=True))
        parent = await session.get(OrchestrationNode, deployment.node.id)
        client = await session.get(OrchestrationNode, second.node.id)
        parent.repository_connection = {"provider": "github", "installation_id": 42}
        eval_node = OrchestrationNode(
            org_id=parent.org_id,
            flow_id=parent.flow_id,
            epic_ref="E1",
            wave_ref="W1",
            node_ref="EVAL",
            kind="eval",
            title="Evaluate API and client",
            issue_ref="19",
            state="ready",
            attempts=0,
            repository_identity="fixture/harness",
            provider_repository_id=303,
            repository_connection={"provider": "github", "installation_id": 903},
        )
        session.add(eval_node)
        await session.flush()
        session.add_all(
            OrchestrationEdge(org_id=parent.org_id, flow_id=parent.flow_id, from_node_id=story.id, to_node_id=eval_node.id)
            for story in (parent, client)
        )
        declaration = evaluation("deployed")
        if human_mode:
            declaration["acceptance_mode"] = "human"
        declaration["harness"].update(adapter="engine-github-actions-probe-v1", operation="probe", harness_digest=harness_digest())
        declaration["predecessors"] = [
            {"address": f"cycle/E1/W1/{story.node_ref}", "repository_id": story.provider_repository_id, "evidence_kind": "deployment"}
            for story in (parent, client)
        ]
        environment = declaration["environment"]
        environment.update(connection_id="delivery-connection", deployment_receipt_keys=[first_receipt.operation_key, second_receipt.operation_key])
        targets = {
            canonical({key: candidate[key] for key in ("provider", "account_id", "region", "resource_kind", "resource_id")}): {
                key: candidate[key] for key in ("provider", "account_id", "region", "resource_kind", "resource_id")
            }
            for receipt in (first_receipt, second_receipt)
            for candidate in receipt.targets
        }
        environment["target_contract_hash"] = hashlib.sha256(
            canonical({"runtime_contract": environment["runtime_contract"], "targets": [targets[key] for key in sorted(targets)]}).encode()
        ).hexdigest()
        declaration["probe"] = {
            "path": ".github/workflows/integration-probe.yml",
            "definition_revision": declaration["harness"]["definition_revision"],
            "account_id": "123456789012",
            "region": "us-east-1",
            "artifact_name": "evaluation-result",
            "artifact_path": "result.json",
            "inputs": {"target_contract_hash": environment["target_contract_hash"], "runtime_contract": environment["runtime_contract"]},
        }
        if fault == "failed-multiple":
            declaration["criteria"].append({"criterion_id": "AC-02", "required": True})
        specification = multi_repository_models().EvaluationSpecification.model_validate(declaration)
        plan = await session.get(OrchestrationAcceptedPlan, deployment.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["execution_policy"]["allowed_actions"].append("evaluate")
        document["execution_policy"]["user_credentials"]["actions"].append("evaluate")
        if fault == "failed-b-budget":
            document["execution_policy"]["limits"]["max_attempts_per_node"] = 1
        document["execution_policy"]["evaluation_acceptance"] = {"cycle/E1/W1/EVAL": "human" if human_mode else "machine"}
        document["nodes"] = [
            {"address": "cycle/E1/W1/EVAL", "kind": "eval", "title": eval_node.title, "issue_ref": eval_node.issue_ref, "evaluation": declaration}
        ]
        if fault in {"failed-b", "failed-b-budget", "failed-unattributed", "failed-multiple"}:
            document["nodes"].extend(
                {
                    "address": f"cycle/E1/W1/{story.node_ref}",
                    "kind": "story",
                    "title": story.title,
                    "issue_ref": story.issue_ref,
                    **(
                        {
                            "repository_identity": story.repository_identity,
                            "provider_repository_id": story.provider_repository_id,
                            "repository_connection": story.repository_connection,
                        }
                        if product_gate_ready
                        else {}
                    ),
                }
                for story in (parent, client)
            )
        plan.plan_document = document
        await session.flush()

        async def resolve(_session, **kwargs):
            return repository_binding.VerifiedRepository(
                "github",
                kwargs["repository_id"],
                kwargs["name"],
                kwargs["installation_id"],
                kwargs["name"],
                kwargs["issue"],
                kwargs["operation"],
                issue_id=kwargs["issue"],
            )

        monkeypatch.setattr(repository_binding, "resolve_story_repository", resolve)
        sources, bindings, harness = await multi_repository_sources(session, eval_node, plan, specification)
        assert len(sources) == 2
        assert [source["deployment_receipt_key"] for source in sources] == [first_receipt.operation_key, second_receipt.operation_key]
        assert [source["repository_id"] for source in sources] == [parent.provider_repository_id, client.provider_repository_id]
        assert {source["components"][0] for source in sources} == {"qualification-api", "qualification-client"}
        assert {binding.full_name for binding in bindings.values()} == {parent.repository_identity, client.repository_identity}
        assert harness.full_name == eval_node.repository_identity
        await session.commit()

    from src.orchestration.compile import ApprovalContext
    from src.orchestration.evaluation_acceptance import EvaluationAcceptanceRequest, accept_evaluation, preview_evaluation

    actor = ApprovalContext(org_id=deployment.node.org_id, actor_id="human", actor_role="org_admin", reason="Approve deployed A/B")
    request = EvaluationAcceptanceRequest(
        node_id=eval_node.id,
        expected_plan_version=deployment.plan.version,
        expected_plan_hash=deployment.plan.plan_hash,
        specification=declaration,
        authorize_evaluate=True,
        reason="Verify both deployed sources in the accepted target",
    )
    async with deployment.factory() as session:
        preview = await preview_evaluation(session, flow_id=deployment.flow.id, actor=actor, request=request)
        accepted = await accept_evaluation(
            session,
            flow_id=deployment.flow.id,
            actor=actor,
            request=request.model_copy(update={"expected_snapshot": preview["snapshot"]}),
        )
        assert accepted["decision_id"]
        await session.commit()

    if fault in {"merge-only", "wrong-environment", "expired", "missing-producer-proof"}:
        async with deployment.factory() as session:
            source_action = await session.scalar(
                select(OrchestrationAction).where(
                    OrchestrationAction.execution_id == second.execution.id,
                    OrchestrationAction.kind == DEPLOYMENT_KIND,
                    OrchestrationAction.operation_key == second_receipt.operation_key,
                )
            )
            assert source_action is not None and source_action.status == "succeeded"
            if fault == "merge-only":
                source_action.status = "failed"
            elif fault == "missing-producer-proof":
                source_action.receipt_ref = None
            else:
                receipt_document = dict(source_action.detail["deployment_receipt"])
                if fault == "wrong-environment":
                    receipt_document["targets"] = [{**receipt_document["targets"][0], "resource_id": "other-cluster/namespace"}]
                elif fault == "expired":
                    receipt_document["observed_at"] = (datetime.now(UTC) - timedelta(minutes=3)).isoformat()
                    receipt_document["valid_until"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
                source_action.detail = {**source_action.detail, "deployment_receipt": receipt_document}
            await session.commit()

    from src.orchestration.deployment_workflow_provider import WorkflowDefinition
    from src.orchestration.dispatch_pass import DispatchPassConfig, DispatchPassReport, _dispatch_one
    from src.orchestration.models import OrchestrationExecution
    from src.orchestration.repository_producer import RepositoryScanProvider

    evidence = SimpleNamespace(
        token=AsyncMock(return_value="scoped-test-token"),
        definition_blob=AsyncMock(
            return_value=(
                "blob",
                json.dumps(
                    {
                        "on": {"workflow_dispatch": {}},
                        "run-name": "${{ format('ADP deployment {0}', inputs.adp_correlation) }}",
                        "concurrency": {"group": "integration-probe", "cancel-in-progress": False},
                    }
                ).encode(),
            )
        ),
    )
    provider = RepositoryScanProvider(evidence=evidence)
    provider.definition = AsyncMock(
        side_effect=lambda binding, workflow, revision: WorkflowDefinition(
            specification.harness.definition_revision,
            revision,
            "c" * 40,
            dict.fromkeys((*workflow.allowed_inputs, "adp_correlation", "adp_source_revision", "adp_definition_revision"), ""),
            True,
            "main",
            specification.harness.source_revision,
        )
    )
    from src.orchestration.execution_policy import AuthorizationContext

    async def authorization(session, **kwargs):
        return AuthorizationContext(
            policy=kwargs["policy"],
            accepted_plan_version=deployment.plan.version,
            in_force_plan_version=deployment.plan.version,
            principal_id="human",
            member_org_id=deployment.node.org_id,
            principal_can_authorize=True,
            now=datetime.now(UTC),
            credential_scope=kwargs["credential_scope"],
            observed_spend_usd=2,
            observed_attempts=0 if fault == "failed-b-budget" else deployment.node.attempts,
            observed_concurrency=0,
            work_owned_by_policy_flow=True,
        )

    monkeypatch.setattr("src.orchestration.shared_policy.resolve_authorization_context", authorization)
    monkeypatch.setattr("src.orchestration.shared_policy._active_count", AsyncMock(return_value=0))
    monkeypatch.setattr("src.orchestration.shared_policy.read_flow_meter", AsyncMock(return_value=SimpleNamespace(total_usd=2)))
    monkeypatch.setattr("src.orchestration.repository_evaluation.RepositoryEvidenceProvider", lambda: evidence)
    monkeypatch.setattr("src.orchestration.dispatch_pass.product_binding_pending_verification", AsyncMock(return_value=False))
    async with deployment.factory() as session:
        eval_node = await session.get(OrchestrationNode, eval_node.id)
        report = DispatchPassReport(enabled=True)
        await _dispatch_one(session, eval_node, config=DispatchPassConfig(queue_url="queue", repo="fixture/harness"), report=report)
        assert report.pending == []
        if fault in {"merge-only", "wrong-environment", "expired", "missing-producer-proof"}:
            reasons = {
                "merge-only": "predecessor_deployment_receipt_missing",
                "wrong-environment": "predecessor_target_contract_mismatch",
                "expired": "predecessor_deployment_expired",
                "missing-producer-proof": "deployment_receipt_scope_changed",
            }
            assert eval_node.state == "ready" and eval_node.attempts == 0
            assert await session.scalar(select(OrchestrationExecution.id).where(OrchestrationExecution.node_id == eval_node.id)) is None
            records = list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.node_id == eval_node.id)))
            blocked = [row for row in records if json.loads(row.reason).get("action") == "repository_evaluation_blocked"]
            assert len(blocked) == 1
            refusal = blocked[0]
            code = "repository_evaluation_" + reasons[fault]
            payload = json.loads(refusal.rejection_reason)
            assert refusal.kind == "transition_rejected" and payload["block_code"] == code
            assert payload["contract"] == "repository-evaluation-refusal/v1" and payload["diagnostic_code"] == code
            assert payload["scope"]["node_id"] == eval_node.id and payload["scope"]["accepted_plan_version"] == deployment.plan.version
            diagnostic = await current_diagnostic(session, eval_node, refusal)
            progress = node_progress(node=eval_node, binding=None, dispatch={}, result={}, repository_refusal=diagnostic)
            assert diagnostic["code"] == code and progress.stage == "evidence_wait" and progress.blocker == code
            assert progress.actor == "evidence-producer" and progress.automation == "blocked"
            assert progress.required_input and progress.next_action
            evidence.token.assert_not_awaited()
            provider.definition.assert_not_awaited()
            await session.commit()
            return
        assert eval_node.state == "running"
        execution = await session.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == eval_node.id))
        assert execution is not None
        await session.commit()

    import os
    import subprocess
    import sys
    from pathlib import Path

    from src.orchestration.deployment_workflow_provider import WorkflowContext, WorkflowRun
    from src.orchestration.evaluation_controller import EvaluationController
    from src.orchestration.execution_runner import RunnerConfig, run_execution_runner
    from src.orchestration.execution_state import ExecutionPhase, ExecutionStatus
    from src.orchestration.repository_producer_controller import RepositoryProducerController

    dispatched = []
    probe_run = None

    async def send_probe(binding, **kwargs):
        assert binding.repo == "fixture/harness"
        await kwargs["reauthorize"]()
        dispatched.append(kwargs)

    async def observe_probe(binding, **kwargs):
        return probe_run, False

    provider.dispatch = AsyncMock(side_effect=send_probe)
    provider.observe = AsyncMock(side_effect=observe_probe)
    controller = RepositoryProducerController(deployment.factory, provider=provider)
    handler = EvaluationController(deployment.factory)
    handler.repository_producer = controller
    deployment.evaluation_controller = handler

    async def advance():
        return await run_execution_runner(
            deployment.factory,
            handlers={ExecutionPhase.EVALUATION_PENDING: deployment.evaluation_controller},
            config=RunnerConfig(enabled=True, max_attempts=8, io_timeout_seconds=10),
            notifier=AsyncMock(return_value="test-notice"),
        )

    initial = await advance()
    assert initial.errors == 0 and len(dispatched) == 1
    probe = dispatched[0]
    inputs = probe["inputs"]
    manifest = json.loads(inputs["source_manifest"])
    assert {item["repository_id"] for item in manifest["sources"]} == {
        deployment.binding.provider_repository_id,
        second.binding.provider_repository_id,
    }
    assert {item["deployment_receipt_key"] for item in manifest["sources"]} == {first_receipt.operation_key, second_receipt.operation_key}
    assert {tuple(item["components"]) for item in manifest["sources"]} == {("qualification-api",), ("qualification-client",)}
    started = datetime.now(UTC) - timedelta(seconds=2)
    probe_run = WorkflowRun(
        run_id=812,
        run_attempt=1,
        status="completed",
        conclusion="failure" if fault in {"failed-b", "failed-b-budget", "failed-unattributed", "failed-multiple"} else "success",
        url="https://github.com/fixture/harness/actions/runs/812",
        context=WorkflowContext(
            schema_version=1,
            repository_id=303,
            run_id=812,
            run_attempt=1,
            workflow_path=specification.probe.path,
            workflow_revision=specification.harness.source_revision,
            source_revision=specification.harness.source_revision,
            account_id=specification.probe.account_id,
            region=specification.probe.region,
            resource_kind="integration_probe",
            resource_id=specification.environment.target_contract_id,
            inputs={},
            correlation=probe["correlation"],
        ),
        artifact_id=7,
        artifact_digest="e" * 64,
        observed_at=datetime.now(UTC).isoformat(),
        started_at=started.isoformat(),
    )
    observed = {
        "criteria": [
            {
                "criterion_id": "AC-01",
                "outcome": "fail" if fault in {"failed-b", "failed-b-budget", "failed-unattributed", "failed-multiple"} else "pass",
                "evidence_references": ["fixture-integration-report"],
                **(
                    {"affected_source_address": f"cycle/E1/W1/{second.node.node_ref}"}
                    if fault in {"failed-b", "failed-b-budget", "failed-multiple"}
                    else {}
                ),
            }
        ]
        + (
            [
                {
                    "criterion_id": "AC-02",
                    "outcome": "fail",
                    "evidence_references": ["fixture-integration-report"],
                    "affected_source_address": f"cycle/E1/W1/{deployment.node.node_ref}",
                }
            ]
            if fault == "failed-multiple"
            else []
        ),
        "runtime_readbacks": [
            {
                **{key: item[key] for key in ("address", "repository_id", "components", "deployment_receipt_key", "targets", "runtime_artifacts")},
                "revision": item["revision"],
                "serving": item["components"] == ["qualification-api"],
                "completed": item["components"] == ["qualification-client"],
                "observed_at": datetime.now(UTC).isoformat(),
                "evidence_reference": "fixture-live-readback",
            }
            for item in manifest["sources"]
        ],
    }
    results_path = tmp_path / "integration.json"
    artifact_path = tmp_path / "result.json"
    results_path.write_text(json.dumps(observed))
    subprocess.run(
        [sys.executable, str(Path(__file__).parent / "fixtures" / "deployed_probe_emitter.py"), str(results_path), str(artifact_path)],
        check=True,
        env={
            **os.environ,
            **{"INPUT_" + key.upper(): value for key, value in inputs.items()},
            "INPUT_ADP_CORRELATION": probe["correlation"],
            "GITHUB_RUN_ID": "812",
            "GITHUB_RUN_ATTEMPT": "1",
        },
    )
    evidence.verified_multi_source = AsyncMock(return_value=([], []))
    evidence.multi_probe_receipt = AsyncMock(return_value=json.loads(artifact_path.read_text()))
    async with deployment.factory() as session:
        record = await session.get(OrchestrationExecution, execution.id)
        record.next_check_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    settled = await advance()
    assert settled.errors == 0 and settled.blocked == 0
    if fault in {"failed-b", "failed-b-budget", "failed-unattributed", "failed-multiple"}:
        async with deployment.factory() as session:
            node = await session.get(OrchestrationNode, eval_node.id)
            record = await session.get(OrchestrationExecution, execution.id)
            action = await session.scalar(
                select(OrchestrationAction).where(
                    OrchestrationAction.execution_id == execution.id, OrchestrationAction.kind == "multi_repository_context"
                )
            )
            assert node.state == "running" and record.status == ExecutionStatus.RUNNABLE
            assert action.detail["failed_probe"]["receipt"]["verdict"] == "fail"
        return deployment, second, handler, advance, execution, eval_node
    async with deployment.factory() as session:
        node = await session.get(OrchestrationNode, eval_node.id)
        record = await session.get(OrchestrationExecution, execution.id)
        assert node.state == ("awaiting_gate" if human_mode else "passed") and record.status == ExecutionStatus.CONCLUDED
        decisions = list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id)))
        completed = [json.loads(row.reason) for row in decisions if json.loads(row.reason).get("action") == "multi_repository_completed"]
        assert len(completed) == 1
        assert completed[0]["receipt"]["verdict"] == "pass"
    provider.dispatch.assert_awaited_once()
    if human_mode:
        return eval_node.id


async def test_product_bound_api_and_client_deployed_probe_settles_exact_sources(deployment, target, monkeypatch, tmp_path):  # noqa: F811
    await product_bound_deployed_probe_case(deployment, target, monkeypatch, tmp_path, None)


async def test_product_bound_deployed_human_gate_requires_fresh_evidence_and_approval(deployment, target, monkeypatch, tmp_path):  # noqa: F811
    node_id = await product_bound_deployed_probe_case(deployment, target, monkeypatch, tmp_path, None, human_mode=True)

    async with deployment.factory() as session:
        node = await session.get(OrchestrationNode, node_id)
        selected = await human_evaluation(session, node)
        assert selected is not None and selected[1].mode == "deployed" and selected[1].acceptance_mode == "human"
        bound = await human_gate_evidence(session, node, *selected, now=datetime.now(UTC))
        receipt = bound["repository_receipt"]["receipt"]
        assert receipt["verdict"] == "pass" and len(receipt["sources"]) == 2
        report = await run_tick(session)
        assert report.errors == 0
        gate = await session.scalar(
            select(OrchestrationDecision).where(
                OrchestrationDecision.node_id == node.id,
                OrchestrationDecision.kind == "human_evaluation_evidence",
            )
        )
        assert gate is not None
        preview = await prepare_gate_execution(session, gate=node, actor=None)
        assert preview["ready"] and preview["evidence_id"] == gate.id
        assert await validate_human_gate_evidence(session, node, gate.id) == bound
        expires = datetime.fromisoformat(receipt["expires_at"])
        with monkeypatch.context() as clock:
            clock.setattr(evaluation_plan, "datetime", SimpleNamespace(now=lambda _: expires))
            with pytest.raises(CycleBlockedError):
                await validate_human_gate_evidence(session, node, gate.id)
        assert node.state == "awaiting_gate"
        product_access = AsyncMock()
        with monkeypatch.context() as external:
            external.setattr("src.orchestration.compile.require_accessible_product", product_access)
            result = await apply_gate_answer_for_context(
                session,
                context=SimpleNamespace(org_id=node.org_id, user_id="approver", account_type="human", auth_source="jwt", cognito_username="approver"),
                node_id=node.id,
                approve=True,
                reason="Reviewed the exact deployed API/client probe",
                access=gate_access(),
                input_path=InputPath.DASHBOARD,
                expected_plan_hash=selected[0].plan_hash,
                execution_preview=preview,
            )
        assert result.status is GateAnswerStatus.APPLIED
        assert node.state == "passed"
        product_access.assert_awaited_once()


async def test_product_bound_failed_probe_retains_b_attribution_without_acceptance(deployment, target, monkeypatch, tmp_path):  # noqa: F811
    context, second, handler, advance, execution, eval_node = await product_bound_deployed_probe_case(
        deployment, target, monkeypatch, tmp_path, "failed-b"
    )
    from src.orchestration.evaluation_plan import identity_for
    from src.orchestration.execution_runner import RunnerContext
    from src.orchestration.models import OrchestrationExecution

    async with context.factory() as session:
        record = await session.get(OrchestrationExecution, execution.id)
        identity = identity_for(record)
    corrections = handler.services.correction_service()
    corrections.provider.credentials = AsyncMock(return_value=("fixture-scoped", 900))
    data, binding, action = await corrections.snapshot(RunnerContext(identity, record, datetime.now(UTC)))
    assert data["source_authorization"]["repository"] == second.binding.repo
    assert binding.repo == second.binding.repo and action is None
    corrections.provider.credentials.assert_awaited_once()


async def test_failed_b_evidence_remains_attributable_after_original_receipts_expire(deployment, target, monkeypatch, tmp_path):  # noqa: F811
    context, second, handler, _, execution, eval_node = await product_bound_deployed_probe_case(deployment, target, monkeypatch, tmp_path, "failed-b")
    from src.orchestration import repository_evaluation
    from src.orchestration.evaluation_plan import identity_for
    from src.orchestration.execution_runner import RunnerContext
    from src.orchestration.models import OrchestrationExecution

    original_datetime = repository_evaluation.datetime

    class AfterReceiptExpiry:
        @staticmethod
        def now(timezone):
            return original_datetime.now(timezone) + timedelta(hours=1)

    async with context.factory() as session:
        record = await session.get(OrchestrationExecution, execution.id)
        state = RunnerContext(identity_for(record), record, datetime.now(UTC))
        node = await session.get(OrchestrationNode, eval_node.id)
        plan = await session.get(OrchestrationAcceptedPlan, context.plan.id)
        spec = multi_repository_models().EvaluationSpecification.model_validate(
            next(item["evaluation"] for item in plan.plan_document["nodes"] if item["address"] == "cycle/E1/W1/EVAL")
        )
        monkeypatch.setattr(repository_evaluation, "datetime", AfterReceiptExpiry)
        with pytest.raises(CycleBlockedError, match="predecessor_deployment_expired"):
            await multi_repository_sources(session, node, plan, spec)
    corrections = handler.services.correction_service()
    corrections.provider.credentials = AsyncMock(return_value=("fixture-scoped", 900))
    data, binding, action = await corrections.snapshot(state)
    assert data["source_authorization"]["repository"] == binding.repo == second.binding.repo
    assert action is None
    corrections.provider.credentials.assert_awaited_once()


@pytest.mark.parametrize("fault", ["failed-unattributed", "failed-multiple"])
async def test_product_bound_failed_probe_refuses_ambiguous_source_without_fallback(deployment, target, monkeypatch, tmp_path, fault):  # noqa: F811
    context, second, handler, advance, execution, eval_node = await product_bound_deployed_probe_case(
        deployment, target, monkeypatch, tmp_path, fault
    )
    from src.orchestration.evaluation_correction_state import CORRECTION_KIND
    from src.orchestration.models import OrchestrationExecution

    provider = handler.services.correction_service().provider
    provider.credentials = AsyncMock(side_effect=AssertionError("no correction repository must be chosen"))
    async with context.factory() as session:
        record = await session.get(OrchestrationExecution, execution.id)
        record.next_check_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    refused = await advance()
    assert refused.blocked == 1 and refused.errors == 0
    assert handler.repository_producer.provider.dispatch.await_count == 1
    provider.credentials.assert_not_awaited()
    async with context.factory() as session:
        assert (
            await session.scalar(
                select(OrchestrationAction).where(OrchestrationAction.execution_id == execution.id, OrchestrationAction.kind == CORRECTION_KIND)
            )
            is None
        )
        assert (await session.get(OrchestrationNode, eval_node.id)).state == "running"


async def test_failed_b_probe_cannot_start_a_correction_after_cycle_budget_exhausts(deployment, target, monkeypatch, tmp_path):  # noqa: F811
    context, _, handler, advance, execution, eval_node = await product_bound_deployed_probe_case(
        deployment, target, monkeypatch, tmp_path, "failed-b-budget"
    )
    from src.orchestration.evaluation_correction_state import CORRECTION_KIND
    from src.orchestration.models import OrchestrationExecution

    provider = handler.services.correction_service().provider
    provider.credentials = AsyncMock(side_effect=AssertionError("budget-exhausted repair must not mint credentials"))
    async with context.factory() as session:
        record = await session.get(OrchestrationExecution, execution.id)
        record.next_check_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    blocked = await advance()
    assert blocked.errors == 0 and blocked.blocked == 1
    assert handler.repository_producer.provider.dispatch.await_count == 1
    provider.credentials.assert_not_awaited()
    async with context.factory() as session:
        assert await session.scalar(select(OrchestrationAction.id).where(OrchestrationAction.kind == CORRECTION_KIND)) is None
        node = await session.get(OrchestrationNode, eval_node.id)
        record = await session.get(OrchestrationExecution, execution.id)
        assert node.state == "running" and record.status == "blocked"
        assert "evaluation_correction_limit_exhausted" in record.block_detail


async def product_bound_failed_b_correction_case(
    deployment,  # noqa: F811
    target,  # noqa: F811
    monkeypatch,
    tmp_path,
    link_fault,
    *,
    restart=False,
    dispatch_child=False,
    expiring_a=False,  # noqa: F811
    verify_product_gate=False,
):  # noqa: F811
    from src.orchestration.dispatch_pass import product_binding_pending_verification as product_gate

    context, second, handler, advance, execution, eval_node = await product_bound_deployed_probe_case(
        deployment, target, monkeypatch, tmp_path, "failed-b", expiring_a=expiring_a, product_gate_ready=verify_product_gate
    )
    if verify_product_gate:
        monkeypatch.setattr("src.orchestration.dispatch_pass.product_binding_pending_verification", product_gate)
    from contextlib import AsyncExitStack

    import httpx

    from src.orchestration.evaluation_controller import EvaluationController, EvaluationServices
    from src.orchestration.evaluation_correction_state import CORRECTION_KIND, child_id, link_id
    from src.orchestration.evaluation_corrections import EvaluationCorrections
    from src.orchestration.evaluation_issue_provider import EvaluationIssueProvider
    from src.orchestration.evaluation_plan import identity_for
    from src.orchestration.execution_runner import ObservationKind, RunnerContext, verify_live_authority
    from src.orchestration.models import OrchestrationExecution
    from src.orchestration.repository_binding import VerifiedRepository
    from src.orchestration.review_cycle import block

    issued = []
    links = []
    link_attempts = []

    def respond(request):
        path = request.url.path
        if path == "/repos/fixture/source-b":
            return httpx.Response(200, json={"id": 202, "full_name": "fixture/source-b"})
        if path == "/repos/fixture/planning":
            return httpx.Response(200, json={"id": 404, "full_name": "fixture/planning"})
        if path == "/repos/fixture/source-b/issues":
            if request.method == "GET":
                return httpx.Response(200, json=issued)
            body = json.loads(request.content)
            issued.append(
                dict(
                    id=871,
                    number=71,
                    node_id="I_correction_b",
                    title=body["title"],
                    body=body["body"],
                    state="open",
                    html_url="https://github.com/fixture/source-b/issues/71",
                    performed_via_github_app={"id": 900},
                )
            )
            if link_fault == "issue-lost-response":
                raise httpx.ReadTimeout("response lost after issue creation")
            return httpx.Response(201, json=issued[0])
        if path == "/repos/fixture/source-b/issues/71":
            return httpx.Response(200, json=issued[0])
        if path == "/repos/fixture/source-b/issues/71/parent":
            return (
                httpx.Response(200, json={"id": 904, "repository_url": "https://api.github.com/repos/fixture/planning"})
                if links
                else httpx.Response(404)
            )
        if path == "/repos/fixture/planning/issues/4/sub_issues":
            assert request.method == "POST" and json.loads(request.content) == {"sub_issue_id": 871}
            link_attempts.append(871)
            if link_fault == "unconfirmed" and len(link_attempts) == 1:
                return httpx.Response(503)
            links.append(871)
            if link_fault == "lost-response" and len(link_attempts) == 1:
                raise httpx.ReadTimeout("response lost after native link")
            return httpx.Response(201)
        raise AssertionError(path)

    async def resolve_epic(session, *, org_id, flow_id, epic_ref, operation="link"):
        assert (org_id, flow_id, epic_ref, operation) == (context.flow.org_id, context.flow.id, "E1", "link")
        return VerifiedRepository("github", 404, "fixture/planning", 42, "fixture/planning", 4, "link", issue_id=904)

    monkeypatch.setattr("src.orchestration.tracker_projection.resolve_parent_reference", resolve_epic)
    if link_fault == "withdrawn":

        async def withdrawn(*args, **kwargs):
            return block("evaluation_correction_link_authority_changed")

        monkeypatch.setattr("src.orchestration.execution_runner.verify_live_authority", withdrawn)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client, AsyncExitStack() as clients:
        provider = EvaluationIssueProvider(client=client)
        provider.credentials = AsyncMock(return_value=("fixture-scoped", 900))
        if link_fault == "revoked-policy":
            original_parent_read = provider.read_native_parent
            saved_plan = None

            async def revoke_before_link(*args):
                nonlocal saved_plan
                if issued and not links and saved_plan is None:
                    async with context.factory() as session:
                        plan = await session.get(OrchestrationAcceptedPlan, context.plan.id)
                        saved_plan = json.loads(json.dumps(plan.plan_document))
                        changed = json.loads(json.dumps(saved_plan))
                        changed["execution_policy"]["allowed_actions"].remove("repair")
                        plan.plan_document = changed
                        await session.commit()
                return await original_parent_read(*args)

            provider.read_native_parent = revoke_before_link
        handler.services.corrections = EvaluationCorrections(handler.services, provider=provider)
        async with context.factory() as session:
            record = await session.get(OrchestrationExecution, execution.id)
            record.next_check_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        first = await advance()
        assert first.errors == 0
        if link_fault is not None:
            assert len(issued) == 1 and len(links) == (1 if link_fault == "lost-response" else 0)
            assert not link_attempts if link_fault in {"withdrawn", "revoked-policy", "issue-lost-response"} else len(link_attempts) == 1
            async with context.factory() as session:
                action = await session.scalar(
                    select(OrchestrationAction).where(OrchestrationAction.execution_id == execution.id, OrchestrationAction.kind == CORRECTION_KIND)
                )
                assert action is not None and action.status == "unknown"
                assert action.detail["creation_started"]
                action_id = action.id
                operation_key = action.operation_key
                source_authorization = action.detail["source_authorization"]
                source_digest = action.detail["source_authorization_digest"]
                assert await session.get(OrchestrationNode, child_id(action.operation_key)) is None
                record = await session.get(OrchestrationExecution, execution.id)
                record.next_check_at = datetime.now(UTC) - timedelta(seconds=1)
                await session.commit()
            monkeypatch.setattr("src.orchestration.execution_runner.verify_live_authority", verify_live_authority)
            if link_fault == "revoked-policy":
                assert saved_plan is not None
                async with context.factory() as session:
                    plan = await session.get(OrchestrationAcceptedPlan, context.plan.id)
                    plan.plan_document = saved_plan
                    await session.commit()
            if verify_product_gate:
                from tests.orchestration.test_evaluation_corrections import dispatch_correction

                context.issue_provider = SimpleNamespace(find=provider.find)
                context.extra_cost_addresses = (f"cycle/E1/W1/{second.node.node_ref}",)
                monkeypatch.setenv("ADP_PRODUCT_STORY_DISPATCH_ENABLED", "true")
                before_link = await dispatch_correction(context, monkeypatch)
                assert before_link.dispatched == 0 and before_link.pending == []
                assert len(issued) == 1 and not links
            if restart:
                resumed_client = await clients.enter_async_context(httpx.AsyncClient(transport=httpx.MockTransport(respond)))
                resumed_provider = EvaluationIssueProvider(client=resumed_client)
                resumed_provider.credentials = AsyncMock(return_value=("fixture-scoped", 900))
                from src.orchestration.repository_producer_controller import RepositoryProducerController

                context.evaluation_controller = EvaluationController(
                    context.factory,
                    services=EvaluationServices(context.factory, runtime=handler.services.runtime, provider=handler.services.provider),
                )
                context.evaluation_controller.repository_producer = RepositoryProducerController(
                    context.factory, provider=handler.repository_producer.provider
                )
                context.evaluation_controller.services.corrections = EvaluationCorrections(
                    context.evaluation_controller.services, provider=resumed_provider
                )
                handler = context.evaluation_controller
                original_find = resumed_provider.find
                resumed_provider.find = AsyncMock(return_value=None)
                unresolved = await advance()
                assert unresolved.errors == 0 and len(issued) == 1 and len(link_attempts) <= 1
                async with context.factory() as session:
                    action = await session.scalar(
                        select(OrchestrationAction).where(
                            OrchestrationAction.execution_id == execution.id, OrchestrationAction.kind == CORRECTION_KIND
                        )
                    )
                    assert action is not None and action.status == "unknown" and action.operation_key == operation_key
                    assert action.detail["source_authorization"] == source_authorization
                    record = await session.get(OrchestrationExecution, execution.id)
                    record.next_check_at = datetime.now(UTC) - timedelta(seconds=1)
                    await session.commit()
                resumed_provider.find = original_find
                retry = await advance()
            else:
                retry = await advance()
            assert retry.errors == 0
        assert len(issued) == len(links) == 1
        assert len(link_attempts) == (2 if link_fault == "unconfirmed" else 1)
        assert "Original accepted work: #18." in issued[0]["body"]
        assert "`AC-01`" in issued[0]["body"]
        async with context.factory() as session:
            record = await session.get(OrchestrationExecution, execution.id)
            record.next_check_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        second_tick = await advance()
        assert second_tick.errors == 0
        async with context.factory() as session:
            action = await session.scalar(
                select(OrchestrationAction).where(OrchestrationAction.execution_id == execution.id, OrchestrationAction.kind == CORRECTION_KIND)
            )
            assert action is not None and action.status == "succeeded"
            if restart:
                assert action.id == action_id and action.operation_key == operation_key
                assert action.detail["source_authorization"] == source_authorization
                assert action.detail["source_authorization_digest"] == source_digest
                actions = list(
                    await session.scalars(
                        select(OrchestrationAction).where(
                            OrchestrationAction.execution_id == execution.id, OrchestrationAction.kind == CORRECTION_KIND
                        )
                    )
                )
                assert len(actions) == 1
            child = await session.get(OrchestrationNode, child_id(action.operation_key))
            assert child is not None and child.flow_id == context.flow.id and child.issue_ref == "71" and child.state == "ready"
            if restart:
                link = await session.get(OrchestrationDecision, link_id(child.id))
                assert link is not None and json.loads(link.reason)["source_authorization_digest"] == source_digest
            assert action.detail["source_authorization"]["repository"] == second.binding.repo
            flow = await session.get(OrchestrationFlow, context.flow.id)
            assert action.detail["source_authorization"]["product_id"] == flow.product_id
            assert action.detail["source_authorization"]["flow_id"] == flow.id
            assert action.detail["source_authorization"]["node_id"] == second.node.id
            assert (await session.get(OrchestrationNode, eval_node.id)).state == "running"
            parent_execution = await session.get(OrchestrationExecution, execution.id)
            parent_context = RunnerContext(identity_for(parent_execution), parent_execution, datetime.now(UTC))
        waiting = await handler.services.corrections.observe(parent_context)
        assert waiting.kind is ObservationKind.WAITING and waiting.stage == "correction_wait"
        if link_fault is None or dispatch_child:
            from src.agentauth.engine import validate_engine_authority
            from src.orchestration.execution_policy import Action
            from src.orchestration.models import OrchestrationWorkClaim
            from src.orchestration.pr_bindings import BindingError, PullRequestIdentity, register_binding, resolve_registration_target
            from src.orchestration.runtime_policy import runtime_action
            from src.orchestration.state import ActorKind
            from tests.orchestration.test_evaluation_corrections import dispatch_correction

            context.issue_provider = SimpleNamespace(find=provider.find)
            context.extra_cost_addresses = (f"cycle/E1/W1/{second.node.node_ref}",)
            if verify_product_gate:
                from src.orchestration.dispatch_pass import product_binding_hold

                async with context.factory() as session:
                    if link_fault is None:
                        assert await product_binding_hold(session, org_id=child.org_id, flow_id=child.flow_id) == "repository_execution_withheld"
                    else:
                        assert await product_binding_hold(session, org_id=child.org_id, flow_id=child.flow_id) is None
                if link_fault is None:
                    without_permission = await dispatch_correction(context, monkeypatch)
                    assert without_permission.dispatched == 0 and without_permission.pending == []
                    monkeypatch.setenv("ADP_PRODUCT_STORY_DISPATCH_ENABLED", "true")
            report = await dispatch_correction(context, monkeypatch)
            assert report.dispatched == 1 and report.publish_failed == 0 and len(report.pending) == 1, report
            pending = report.pending[0]
            envelope = pending.envelope
            run_id = envelope["message_id"]
            assert pending.node_id == child.id
            assert envelope["source_ref"]["repo"] == second.binding.repo
            assert envelope["source_ref"]["provider_repository_id"] == second.binding.provider_repository_id
            assert envelope["source_ref"]["installation_id"] == second.binding.installation_id
            assert envelope["source_ref"]["issue"] == 71
            grant = context.store.live_grant(invocation_id=run_id, tenant_id=child.org_id, attempt=1, now=datetime.now(UTC))
            raw = context.store._read(f"TENANT#{child.org_id}", f"EXEC#{run_id}")
            assert runtime_action(raw, child) is Action.REPAIR
            async with context.factory() as session:
                dispatched_child = await session.get(OrchestrationNode, child.id)
                claim = await session.scalar(
                    select(OrchestrationWorkClaim).where(
                        OrchestrationWorkClaim.org_id == child.org_id,
                        OrchestrationWorkClaim.provider_repository_id == second.binding.provider_repository_id,
                        OrchestrationWorkClaim.issue_number == 71,
                    )
                )
                assert claim is not None and claim.owner_ref == context.flow.id and claim.active_run_id == run_id
                parent_grant = context.store.live_grant(
                    invocation_id=action.detail["parent_run_id"], tenant_id=child.org_id, attempt=1, now=datetime.now(UTC)
                )
                assert grant.flow_id == parent_grant.flow_id == context.flow.id
                assert grant.grant_id != parent_grant.grant_id
                assert grant.expires_at == parent_grant.expires_at and grant.max_chain_depth == parent_grant.max_chain_depth
                assert raw["parent_grant_id"]["S"] == parent_grant.grant_id
                await validate_engine_authority(session=session, execution=raw, grant=grant, store=context.store)
                registration = await resolve_registration_target(session, run_id=run_id, expected_org_id=child.org_id)
                assert (registration.repo, registration.provider_repository_id, registration.installation_id) == (
                    second.binding.repo,
                    second.binding.provider_repository_id,
                    second.binding.installation_id,
                )
                wrong = PullRequestIdentity(deployment.binding.provider_repository_id, "PR_wrong", deployment.binding.repo, 78, "e" * 40)
                with pytest.raises(BindingError):
                    await register_binding(session, target=registration, pr=wrong, actor_id=run_id, actor_kind=ActorKind.SERVICE)
                correct = PullRequestIdentity(second.binding.provider_repository_id, "PR_b", second.binding.repo, 79, "e" * 40)
                binding, created = await register_binding(session, target=registration, pr=correct, actor_id=run_id, actor_kind=ActorKind.SERVICE)
                assert created and binding.node_id == child.id and binding.flow_id == context.flow.id
                assert binding.repo == second.binding.repo and binding.provider_repository_id == second.binding.provider_repository_id
                assert json.loads(binding.accepted_scope)["evaluation_correction"]["parent_node_id"] == eval_node.id
                assert dispatched_child.state == "running" and dispatched_child.attempts == 1
                await session.commit()
            reservation = context.flow_reservation.await_args.kwargs
            assert reservation["flow_id"] == context.flow.id
            assert reservation["policy"].limits.max_spend_usd == context.policy.limits.max_spend_usd
            assert reservation["policy"].limits.max_concurrent_actions == context.policy.limits.max_concurrent_actions
            return context, second, handler, advance, execution, eval_node, child, run_id, claim, binding, correct


@pytest.mark.parametrize(
    "verdict, restart, expiring_a",
    [("pass", False, False), ("fail", False, False), ("pass", True, False), ("pass", True, True), ("fail", True, True)],
)
async def test_repaired_b_requires_a_new_exact_set_probe(deployment, target, monkeypatch, tmp_path, verdict, restart, expiring_a):  # noqa: F811
    from copy import copy

    from src.orchestration.evaluation_plan import identity_for
    from src.orchestration.execution_store import load_execution
    from src.orchestration.handoff import commit_handoff
    from src.orchestration.models import OrchestrationExecution
    from tests.orchestration.test_deployment_workflows import prepare_deployment
    from tests.orchestration.test_merge_controller import prepared_merge

    context, second, handler, advance, execution, eval_node, child_node, run_id, claim, binding, pr = await product_bound_failed_b_correction_case(
        deployment,
        target,
        monkeypatch,
        tmp_path,
        "lost-response" if restart else None,
        restart=restart,
        dispatch_child=restart,
        expiring_a=expiring_a,
    )
    child = copy(second)
    child.node, child.root, child.claim, child.binding, child.head = child_node, run_id, claim, binding, pr.head_sha
    async with context.factory() as session:
        row = await session.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == child_node.id))
        child.identity = identity_for(row)
        child.execution = (await load_execution(session, identity=child.identity)).record
        await commit_handoff(session, identity=child.identity, now=datetime.now(UTC), next_check_at=datetime.now(UTC))
        await session.commit()
    monkeypatch.setattr("src.orchestration.pr_identity.resolve_pr_identity", AsyncMock(return_value=pr))
    await context.finish(child.root)
    async with prepared_merge(child, monkeypatch, merge_sha="9" * 40):
        await prepare_deployment(child, source="9" * 40)

        def corrected_run():
            run = context.make_run()
            return replace(
                run,
                url=f"https://github.com/{child.binding.repo}/actions/runs/{run.run_id}",
                context=run.context.model_copy(
                    update={
                        "repository_id": child.binding.provider_repository_id,
                        "source_revision": child.entry.artifact_revision,
                        "workflow_revision": child.entry.workflow.definition_revision,
                    }
                ),
            )

        child.make_run = corrected_run
        receipt, _ = await issue_qualification(child, "qualification-client", target)
        assert receipt.actual_revision == "9" * 40
        handler.services.runtime.deployments = child.runtime_services
        from src.orchestration.execution_runner import RunnerContext

        async with context.factory() as session:
            prior = await session.get(OrchestrationExecution, execution.id)
            prior.next_check_at = datetime.now(UTC) - timedelta(seconds=1)
            prior_context = RunnerContext(identity_for(prior), prior, datetime.now(UTC))
            await session.commit()
        from src.orchestration.execution_runner import ObservationKind

        if expiring_a:
            import asyncio

            from src.orchestration.evaluation_runtime import EvaluationRuntime

            runtime = EvaluationRuntime(context.factory, deployments=deployment.runtime_services)
            handler.services.runtime.verify = runtime.verify
            handler.services.runtime.authority = runtime.authority
            async with context.factory() as session:
                source_actions = list(
                    await session.scalars(
                        select(OrchestrationAction).where(
                            OrchestrationAction.execution_id == deployment.execution.id,
                            OrchestrationAction.kind == DEPLOYMENT_KIND,
                            OrchestrationAction.status == "succeeded",
                        )
                    )
                )
                original_a = next(
                    DeploymentReceipt.model_validate(row.detail["deployment_receipt"])
                    for row in source_actions
                    if row.detail["deployment_receipt"]["delivery_complete"]
                )
            await asyncio.sleep(max(0, (original_a.valid_until - datetime.now(UTC)).total_seconds()) + 0.1)
            assert original_a.valid_until <= datetime.now(UTC)
            original_inspect = deployment.runtime_services.runtime.inspect
            with monkeypatch.context() as changed:
                changed.setattr(
                    deployment.runtime_services.runtime,
                    "inspect",
                    lambda *args, **kwargs: original_inspect(*args, **kwargs).model_copy(update={"image_digest": "sha256:" + "0" * 64}),
                )
                with pytest.raises(CycleBlockedError, match="evaluation_runtime_evidence_changed"):
                    await handler.services.corrections.observe(prior_context)
            async with context.factory() as session:
                plan = await session.get(OrchestrationAcceptedPlan, context.plan.id)
                saved_plan = json.loads(json.dumps(plan.plan_document))
                withdrawn = json.loads(json.dumps(saved_plan))
                withdrawn["execution_policy"]["evaluation_acceptance"].pop("cycle/E1/W1/EVAL")
                plan.plan_document = withdrawn
                await session.commit()
            try:
                with pytest.raises(CycleBlockedError, match="authority|policy|acceptance"):
                    await handler.services.corrections.observe(prior_context)
            finally:
                async with context.factory() as session:
                    plan = await session.get(OrchestrationAcceptedPlan, context.plan.id)
                    plan.plan_document = saved_plan
                    await session.commit()
            async with context.factory() as session:
                assert (
                    await session.scalar(
                        select(OrchestrationAction.id).where(
                            OrchestrationAction.execution_id == execution.id,
                            OrchestrationAction.kind == "multi_repository_source_refresh",
                        )
                    )
                    is None
                )
            waiting = await handler.services.corrections.observe(prior_context)
            assert waiting.stage == "correction_refresh"
            from src.orchestration.execution_runner import RunnerContext

            refresh_report = await advance()
            assert refresh_report.errors == refresh_report.blocked == 0, refresh_report
            async with context.factory() as session:
                current = await session.get(OrchestrationExecution, execution.id)
                assert (current.claim_id, current.claim_generation) == (execution.claim_id, execution.claim_generation)
                current.next_check_at = datetime.now(UTC) - timedelta(seconds=1)
                await session.commit()
            async with context.factory() as session:
                actions = list(
                    await session.scalars(
                        select(OrchestrationAction).where(
                            OrchestrationAction.execution_id == execution.id,
                            OrchestrationAction.kind == "multi_repository_source_refresh",
                        )
                    )
                )
                assert len(actions) == 1 and actions[0].status == "succeeded"
                refreshed_key = actions[0].operation_key
                assert refreshed_key != original_a.operation_key
                assert DeploymentReceipt.model_validate(actions[0].detail["deployment_receipt"]).actual_revision == original_a.actual_revision
            prior_context = RunnerContext(identity_for(current), current, datetime.now(UTC))
        observed = await handler.services.corrections.observe(prior_context)
        assert observed.stage == "correction_retest"
        report = await advance()
        async with context.factory() as session:
            records = list(
                await session.scalars(
                    select(OrchestrationExecution).where(OrchestrationExecution.node_id == eval_node.id).order_by(OrchestrationExecution.cycle)
                )
            )
            assert report.errors == 0 and report.blocked == 0, [(record.cycle, record.status, record.block_detail) for record in records]
            assert len(records) == 2, [(record.cycle, record.status, record.block_detail) for record in records]
            assert records[0].status == "concluded" and records[1].status == "runnable"
            assert records[1].claim_id == execution.claim_id
            assert (await session.get(OrchestrationNode, eval_node.id)).state == "running"
            prior_evidence = await session.scalar(
                select(OrchestrationAction).where(
                    OrchestrationAction.execution_id == records[0].id,
                    OrchestrationAction.kind == "multi_repository_context",
                )
            )
            assert prior_evidence.detail["failed_probe"]["receipt"]["verdict"] == "fail"
            new_context = await session.scalar(
                select(OrchestrationAction).where(
                    OrchestrationAction.execution_id == records[1].id,
                    OrchestrationAction.kind == "multi_repository_context",
                )
            )
            assert new_context.detail["correction_operation_key"]
            assert new_context.detail["receipt_keys"] == sorted(
                [
                    refreshed_key if expiring_a else prior_evidence.detail["failed_probe"]["receipt"]["sources"][0]["deployment_receipt_key"],
                    receipt.operation_key,
                ]
            )
        async with context.factory() as session:
            current = await session.get(OrchestrationExecution, records[1].id)
            current.next_check_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        provider = handler.repository_producer.provider
        provider.observe = AsyncMock(return_value=(None, False))
        started = await advance()
        assert started.errors == started.blocked == 0, started
        async with context.factory() as session:
            current = await session.get(OrchestrationExecution, records[1].id)
            debug_state = (current.phase, current.status, current.block_detail, current.progress_note)
        assert provider.dispatch.await_count == 2, (started, debug_state)
        probe = provider.dispatch.await_args.kwargs
        manifest = json.loads(probe["inputs"]["source_manifest"])
        assert {item["deployment_receipt_key"] for item in manifest["sources"]} == set(new_context.detail["receipt_keys"])
        assert next(item for item in manifest["sources"] if item["repository_id"] == second.binding.provider_repository_id)["revision"] == "9" * 40

        import os
        import subprocess
        import sys
        from pathlib import Path

        if restart:
            from src.orchestration.evaluation_controller import EvaluationController, EvaluationServices
            from src.orchestration.repository_producer_controller import RepositoryProducerController

            async with context.factory() as session:
                pending = await session.get(OrchestrationExecution, records[1].id)
                probe_actions = list(
                    await session.scalars(
                        select(OrchestrationAction).where(
                            OrchestrationAction.execution_id == pending.id, OrchestrationAction.kind == "multi_repository_probe_dispatch"
                        )
                    )
                )
                assert len(probe_actions) == 1
                operation = (probe_actions[0].id, probe_actions[0].operation_key)
                claim_identity = (pending.claim_id, pending.claim_generation)
                pending.next_check_at = datetime.now(UTC) - timedelta(seconds=1)
                await session.commit()
            handler = EvaluationController(
                context.factory,
                services=EvaluationServices(context.factory, runtime=handler.services.runtime, provider=handler.services.provider),
            )
            handler.repository_producer = RepositoryProducerController(context.factory, provider=provider)
            context.evaluation_controller = handler
            resumed = await advance()
            assert resumed.errors == resumed.blocked == 0, resumed
            assert provider.dispatch.await_count == 2
            async with context.factory() as session:
                pending = await session.get(OrchestrationExecution, records[1].id)
                probe_actions = list(
                    await session.scalars(
                        select(OrchestrationAction).where(
                            OrchestrationAction.execution_id == pending.id, OrchestrationAction.kind == "multi_repository_probe_dispatch"
                        )
                    )
                )
                assert len(probe_actions) == 1
                assert (probe_actions[0].id, probe_actions[0].operation_key) == operation
                assert (pending.claim_id, pending.claim_generation) == claim_identity == (execution.claim_id, execution.claim_generation)
        from src.orchestration.deployment_workflow_provider import WorkflowContext, WorkflowRun
        from src.orchestration.execution_state import ExecutionStatus

        async with context.factory() as session:
            plan = await session.get(OrchestrationAcceptedPlan, context.plan.id)
            specification = multi_repository_models().EvaluationSpecification.model_validate(
                next(item["evaluation"] for item in plan.plan_document["nodes"] if item["address"] == "cycle/E1/W1/EVAL")
            )
        probe_run = WorkflowRun(
            run_id=813,
            run_attempt=1,
            status="completed",
            conclusion="success" if verdict == "pass" else "failure",
            url="https://github.com/fixture/harness/actions/runs/813",
            context=WorkflowContext(
                schema_version=1,
                repository_id=303,
                run_id=813,
                run_attempt=1,
                workflow_path=specification.probe.path,
                workflow_revision=specification.harness.source_revision,
                source_revision=specification.harness.source_revision,
                account_id=specification.probe.account_id,
                region=specification.probe.region,
                resource_kind="integration_probe",
                resource_id=specification.environment.target_contract_id,
                inputs={},
                correlation=probe["correlation"],
            ),
            artifact_id=8,
            artifact_digest="f" * 64,
            observed_at=datetime.now(UTC).isoformat(),
            started_at=(datetime.now(UTC) - timedelta(seconds=2)).isoformat(),
        )
        results = {
            "criteria": [
                {
                    "criterion_id": "AC-01",
                    "outcome": verdict,
                    "evidence_references": ["fixture-retest-report"],
                    **({"affected_source_address": f"cycle/E1/W1/{second.node.node_ref}"} if verdict == "fail" else {}),
                }
            ],
            "runtime_readbacks": [
                {
                    **{
                        key: item[key] for key in ("address", "repository_id", "components", "deployment_receipt_key", "targets", "runtime_artifacts")
                    },
                    "revision": item["revision"],
                    "serving": item["components"] == ["qualification-api"],
                    "completed": item["components"] == ["qualification-client"],
                    "observed_at": datetime.now(UTC).isoformat(),
                    "evidence_reference": "fixture-retest-readback",
                }
                for item in manifest["sources"]
            ],
        }
        input_path = tmp_path / "retest-input.json"
        artifact_path = tmp_path / "retest-result.json"
        input_path.write_text(json.dumps(results))
        subprocess.run(
            [sys.executable, str(Path(__file__).parent / "fixtures" / "deployed_probe_emitter.py"), str(input_path), str(artifact_path)],
            check=True,
            env={
                **os.environ,
                **{"INPUT_" + key.upper(): value for key, value in probe["inputs"].items()},
                "INPUT_ADP_CORRELATION": probe["correlation"],
                "GITHUB_RUN_ID": "813",
                "GITHUB_RUN_ATTEMPT": "1",
            },
        )
        provider.observe = AsyncMock(return_value=(probe_run, False))
        from src.orchestration.execution_runner import RunnerContext

        async with context.factory() as session:
            current = await session.get(OrchestrationExecution, records[1].id)
            current_context = RunnerContext(identity_for(current), current, datetime.now(UTC))
        stale = await handler.repository_producer.observe(current_context)
        assert stale.kind is ObservationKind.BLOCKED
        assert stale.block.detail == "repository_evaluation_probe_producer_or_run_mismatch"
        assert provider.dispatch.await_count == 2
        provider.evidence.multi_probe_receipt = AsyncMock(return_value=json.loads(artifact_path.read_text()))
        async with context.factory() as session:
            current = await session.get(OrchestrationExecution, records[1].id)
            current.next_check_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        completed = await advance()
        assert completed.errors == completed.blocked == 0, completed
        async with context.factory() as session:
            current = await session.get(OrchestrationExecution, records[1].id)
            prior = await session.get(OrchestrationExecution, records[0].id)
            node = await session.get(OrchestrationNode, eval_node.id)
            assert prior_evidence.detail["failed_probe"]["receipt"]["verdict"] == "fail"
            decisions = list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id)))
            completed_receipts = [json.loads(row.reason) for row in decisions if json.loads(row.reason).get("action") == "multi_repository_completed"]
            if verdict == "pass":
                assert current.status == ExecutionStatus.CONCLUDED and node.state == "passed"
                assert len(completed_receipts) == 1
                final_receipt = completed_receipts[0]["receipt"]
            else:
                assert current.status == ExecutionStatus.RUNNABLE and node.state == "running"
                assert completed_receipts == []
                retest_context = await session.scalar(
                    select(OrchestrationAction).where(
                        OrchestrationAction.execution_id == current.id, OrchestrationAction.kind == "multi_repository_context"
                    )
                )
                final_receipt = retest_context.detail["failed_probe"]["receipt"]
            assert final_receipt["verdict"] == verdict
            assert final_receipt["sources"][1]["deployed_revision"] == "9" * 40
            if expiring_a:
                assert final_receipt["sources"][0]["deployment_receipt_key"] == refreshed_key
                assert prior_evidence.detail["failed_probe"]["receipt"]["sources"][0]["deployment_receipt_key"] == original_a.operation_key
            assert final_receipt["input_set_digest"] != prior_evidence.detail["failed_probe"]["receipt"]["input_set_digest"]
        if verdict == "fail":
            async with context.factory() as session:
                current = await session.get(OrchestrationExecution, records[1].id)
                next_context = RunnerContext(identity_for(current), current, datetime.now(UTC))
            corrections = handler.services.correction_service()
            corrections.provider.credentials = AsyncMock(return_value=("fixture-scoped", 900))
            data, next_binding, next_action = await corrections.snapshot(next_context)
            assert next_action is None and next_binding.repo == second.binding.repo
            assert data["source_authorization"]["node_id"] == second.node.id
            assert data["source_revision"] == "9" * 40
            if expiring_a:
                from src.orchestration.evaluation_plan import current_deployment
                from src.orchestration.repository_evaluation import verify_original_after_refresh

                async with context.factory() as session:
                    node = await session.get(OrchestrationNode, eval_node.id)
                    parent = await session.get(OrchestrationNode, deployment.node.id)
                    plan = await session.get(OrchestrationAcceptedPlan, context.plan.id)
                    anchor = await current_deployment(session, parent, plan.version, now=datetime.now(UTC))
                    assert anchor is not None
                    old_source = final_receipt["sources"][0]
                    failure_time = datetime.fromisoformat(final_receipt["observed_at"])
                    await verify_original_after_refresh(session, node, plan, parent, old_source["address"], old_source, failure_time, anchor[2])
                    with pytest.raises(CycleBlockedError, match="evaluation_source_refresh_original_changed"):
                        await verify_original_after_refresh(
                            session,
                            node,
                            plan,
                            parent,
                            old_source["address"],
                            {**old_source, "deployment_receipt_key": "wrong-refresh"},
                            failure_time,
                            anchor[2],
                        )


@pytest.mark.parametrize("link_fault", [None, "unconfirmed", "withdrawn", "revoked-policy", "lost-response", "issue-lost-response"])
async def test_product_bound_failed_b_probe_publishes_in_b_under_native_epic(deployment, target, monkeypatch, tmp_path, link_fault):  # noqa: F811
    await product_bound_failed_b_correction_case(deployment, target, monkeypatch, tmp_path, link_fault)


@pytest.mark.parametrize("link_fault", [None, "unconfirmed"])
async def test_product_bound_b_correction_obeys_real_product_dispatch_gate(deployment, target, monkeypatch, tmp_path, link_fault):  # noqa: F811
    await product_bound_failed_b_correction_case(deployment, target, monkeypatch, tmp_path, link_fault, dispatch_child=True, verify_product_gate=True)


@pytest.mark.parametrize("link_fault", ["issue-lost-response", "lost-response", "unconfirmed"])
async def test_product_bound_b_correction_restart_reconciles_issue_and_parent(deployment, target, monkeypatch, tmp_path, link_fault):  # noqa: F811
    await product_bound_failed_b_correction_case(deployment, target, monkeypatch, tmp_path, link_fault, restart=True)


@pytest.mark.parametrize("fault", ["merge-only", "wrong-environment", "expired", "missing-producer-proof"])
async def test_product_bound_deployed_inputs_block_probe_admission(deployment, target, monkeypatch, tmp_path, fault):  # noqa: F811
    await product_bound_deployed_probe_case(deployment, target, monkeypatch, tmp_path, fault)
