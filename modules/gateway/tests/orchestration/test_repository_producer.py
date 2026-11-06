"""Actual admission/runner recovery for a single explicitly authorized scan."""

import asyncio
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration.deployment_workflow_provider import WorkflowContext, WorkflowRun
from src.orchestration.evaluation_acceptance import EvaluationAcceptanceError
from src.orchestration.evaluation_controller import EvaluationController
from src.orchestration.execution_runner import RunnerConfig, run_execution_runner
from src.orchestration.execution_state import ExecutionIdentity, ExecutionPhase
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from src.orchestration.repository_evaluation import observe_repository_evaluation
from src.orchestration.repository_producer import CONTEXT_KIND, PRODUCER_KIND, RepositoryScanProvider
from src.orchestration.repository_producer_controller import RepositoryProducerController
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.run_reports import OrchestrationRunReport
from tests.orchestration.test_evaluation_acceptance import accept, contract_request  # noqa: F401
from tests.orchestration.test_repository_evaluation import repository_evaluation  # noqa: F401
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "wrong-run",
        "old-revision",
        "missing-artifact",
        "wrong-source",
        "wrong-target",
        "old-result",
        "missing-criterion",
        "live-revision-changed",
        "live-disappeared",
        "live-artifact-changed",
        "live-pre-run",
        "live-not-serving",
    ],
)
async def test_deployed_probe_requires_run_bound_artifact_and_exact_current_sources(monkeypatch, tmp_path, fault):
    from src.orchestration import repository_producer_controller as controller_module
    from src.orchestration.evaluation_contract import multi_repository_models
    from src.orchestration.execution_runner import ObservationKind
    from src.orchestration.repository_producer import digest
    from tests.orchestration.test_multi_repository_evaluation_contract import evaluation, golden

    models = multi_repository_models()
    declaration = evaluation("deployed")
    declaration["predecessors"][0]["evidence_kind"] = "deployment"
    for predecessor, address in zip(declaration["predecessors"], ("flow/epic/wave/a", "flow/epic/wave/b")):
        predecessor["address"] = address
    declaration["harness"].update(adapter="engine-github-actions-probe-v1", operation="probe")
    declaration["probe"] = {
        "path": ".github/workflows/integration-probe.yml",
        "definition_revision": declaration["harness"]["definition_revision"],
        "account_id": "123456789012",
        "region": "us-east-1",
        "artifact_name": "evaluation-result",
        "artifact_path": "result.json",
        "inputs": {"target_contract_hash": "d" * 64, "runtime_contract": "api-v1"},
    }
    spec = models.EvaluationSpecification.model_validate(declaration)
    sources = deepcopy(golden()["sources"])
    source_a = sources[0]
    for field in ("pull_request_node_id", "pull_request_number", "reviewed_head_sha", "merge_sha"):
        source_a.pop(field)
    source_a.update(evidence_kind="deployment", deployment_receipt_key="deployment-a", operation_key="operation-a", deployed_revision="1" * 40)
    sources[1]["deployment_receipt_key"] = "deployment-b"
    resolved = [models.DeploymentInput.model_validate(source) for source in sources]
    for source in sources:
        source["head_sha"] = source["deployed_revision"]
        source["components"] = ["qualification-api" if source["address"].endswith("/a") else "qualification-client"]
        source["runtime_artifacts"] = [
            {
                "component": source["components"][0],
                "actual_revision": source["head_sha"],
                "artifact_hash": "a" * 64 if source["address"].endswith("/a") else "b" * 64,
                "image_digest": "sha256:" + "f" * 64,
                "tick_digest": None,
                "migration_head": None,
                "asset_count": 0,
            }
        ]
        source["deployment_targets"] = [{"resource_id": "fixture-target"}]
    now = datetime.now(UTC)
    from src.orchestration.repository_producer import probe_manifest

    manifest = probe_manifest(sources, spec.environment)
    assert {tuple(item["components"]) for item in json.loads(manifest)["sources"]} == {("qualification-api",), ("qualification-client",)}
    inputs = {
        "source_manifest": manifest,
        "source_snapshot_hash": digest(sources),
        "specification_digest": models.specification_digest(spec),
        "probe_criteria": json.dumps([item.model_dump(mode="json") for item in spec.criteria]),
        "target_contract_hash": spec.environment.target_contract_hash,
    }
    input_env = {"INPUT_" + key.upper(): value for key, value in inputs.items()}
    input_env["INPUT_ADP_CORRELATION"] = "a" * 64
    input_env.update(GITHUB_RUN_ID="812", GITHUB_RUN_ATTEMPT="2")
    results = tmp_path / "integration.json"
    readbacks = [
        {
            "address": f"flow/epic/wave/{label}",
            "repository_id": repository_id,
            "revision": revision * 40,
            "components": [component],
            "deployment_receipt_key": f"deployment-{label}",
            "targets": [{"resource_id": "fixture-target"}],
            "runtime_artifacts": [
                {
                    "component": component,
                    "actual_revision": revision * 40,
                    "artifact_hash": artifact_hash * 64,
                    "image_digest": "sha256:" + "f" * 64,
                    "tick_digest": None,
                    "migration_head": None,
                    "asset_count": 0,
                }
            ],
            "serving": component != "qualification-client",
            "completed": component == "qualification-client",
            "observed_at": now.isoformat(),
            "evidence_reference": f"readback-{label}",
        }
        for label, repository_id, revision, component, artifact_hash in (
            ("a", 101, "1", "qualification-api", "a"),
            ("b", 202, "3", "qualification-client", "b"),
        )
    ]
    results.write_text(
        json.dumps(
            {
                "criteria": [{"criterion_id": "AC-01", "outcome": "pass", "evidence_references": ["fixture-integration-report"]}],
                "runtime_readbacks": readbacks,
            }
        )
    )
    artifact_path = tmp_path / "result.json"
    subprocess.run(
        [sys.executable, str(Path(__file__).parent / "fixtures" / "deployed_probe_emitter.py"), str(results), str(artifact_path)],
        env={**os.environ, **input_env},
        check=True,
    )
    result = json.loads(artifact_path.read_text())
    if fault == "wrong-run":
        result["run_id"] = 811
    elif fault == "old-revision":
        inputs["source_manifest"] = manifest.replace("3" * 40, "4" * 40)
    elif fault == "wrong-source":
        result["source_snapshot_hash"] = "f" * 64
    elif fault == "wrong-target":
        result["target_contract_hash"] = "f" * 64
    elif fault == "old-result":
        result["observed_at"] = (now - timedelta(hours=1)).isoformat()
    elif fault == "missing-criterion":
        result["criteria"] = []
    elif fault == "live-revision-changed":
        result["runtime_readbacks"][1]["revision"] = "4" * 40
    elif fault == "live-disappeared":
        result["runtime_readbacks"].pop()
    elif fault == "live-artifact-changed":
        result["runtime_readbacks"][1]["runtime_artifacts"][0]["artifact_hash"] = "e" * 64
    elif fault == "live-pre-run":
        result["runtime_readbacks"][1]["observed_at"] = (now - timedelta(minutes=1)).isoformat()
    elif fault == "live-not-serving":
        result["runtime_readbacks"][1]["completed"] = False
    node = SimpleNamespace(org_id="org-fixture", flow_id="flow-fixture", id="eval-node", attempts=1)
    plan = SimpleNamespace(version=2)
    accepted = (SimpleNamespace(id="accepted-evaluation"), spec, SimpleNamespace(policy_hash="e" * 64))
    admission = {"source_snapshot_hash": digest(sources), "evaluation_policy_hash": "e" * 64}
    state = (node, plan, spec, accepted, admission, sources, SimpleNamespace(repo="fixture/harness", provider_repository_id=303))
    context = SimpleNamespace(
        execution=SimpleNamespace(id="eval-execution", cycle=1, accepted_plan_version=2, claim_id="claim", claim_generation=1),
        identity=SimpleNamespace(claim_generation=1),
    )
    run = WorkflowRun(
        run_id=812,
        run_attempt=2,
        status="completed",
        conclusion="success",
        url="https://github.com/fixture/harness/actions/runs/812",
        context=WorkflowContext(
            schema_version=1,
            repository_id=303,
            run_id=812,
            run_attempt=2,
            workflow_path=spec.probe.path,
            workflow_revision="a" * 40,
            source_revision="a" * 40,
            account_id=spec.probe.account_id,
            region=spec.probe.region,
            resource_kind="integration_probe",
            resource_id=spec.environment.target_contract_id,
            inputs={},
            correlation="a" * 64,
        ),
        artifact_id=7,
        artifact_digest="b" * 64,
        observed_at=now.isoformat(),
        started_at=(now - timedelta(seconds=1)).isoformat(),
    )
    if fault == "missing-artifact":
        artifact = AsyncMock(side_effect=CycleBlockedError("repository_evaluation_probe_artifact_missing_or_ambiguous"))
    else:
        artifact = AsyncMock(return_value=result)
    evidence = SimpleNamespace(
        verified_multi_source=AsyncMock(
            side_effect=lambda binding, source, declared: (
                source["checks"],
                [{"repository_id": binding.provider_repository_id} for _ in source["checks"]],
            )
        ),
        multi_probe_receipt=artifact,
    )
    provider = SimpleNamespace(evidence=evidence, observe=AsyncMock(return_value=(run, False)))

    @asynccontextmanager
    async def factory():
        yield SimpleNamespace()

    bindings = {
        source.address: SimpleNamespace(full_name=f"fixture/source-{source.address[-1]}", repository_id=source.repository_id, installation_id=900)
        for source in spec.predecessors
    }
    monkeypatch.setattr(
        controller_module,
        "multi_repository_sources",
        AsyncMock(return_value=(sources, bindings, SimpleNamespace(repository_id=303, full_name="fixture/harness", installation_id=903))),
    )
    monkeypatch.setattr(controller_module, "multi_source_receipts", AsyncMock(return_value=resolved))
    monkeypatch.setattr(controller_module, "authorize", AsyncMock(return_value=(accepted[0].id, "e" * 64)))
    controller = RepositoryProducerController(factory, provider=provider)
    controller.snapshot = AsyncMock(
        return_value=(
            state,
            {
                "workflow": {"path": spec.probe.path, "definition_revision": "b" * 40, "allowed_inputs": {}},
                "definition": {},
                "source_revision": "a" * 40,
                "inputs": inputs,
                "correlation": "a" * 64,
                "operation_key": "probe-effect",
            },
        )
    )
    controller.observed_run = AsyncMock(return_value=(run, False))
    observed = await controller.observe(context)
    if fault is None:
        assert observed.kind is ObservationKind.SUCCEEDED
        assert observed.snapshot["receipt"]["input_set_digest"] == models.input_set_digest(spec, resolved)
        assert observed.snapshot["receipt"]["producer"] == {
            "adapter": "engine-github-actions-probe-v1",
            "provider_identity": "github:303",
            "run_id": "812",
            "run_attempt": 2,
        }
        assert observed.snapshot["run"]["run_id"] == 812
    else:
        assert observed.kind is ObservationKind.BLOCKED and observed.snapshot is None


@pytest.mark.parametrize("fault", [None, "extra-input", "other-trigger", "cancellable"])
async def test_probe_preflight_requires_pinned_dispatch_and_non_cancellable_correlation(fault):
    from src.orchestration.deployment_workflow_provider import WorkflowDefinition
    from src.orchestration.evaluation_contract import multi_repository_models
    from tests.orchestration.test_multi_repository_evaluation_contract import evaluation

    declaration = evaluation("deployed")
    declaration["probe"] = {
        "path": ".github/workflows/probe.yml",
        "definition_revision": declaration["harness"]["definition_revision"],
        "account_id": "123456789012",
        "region": "us-east-1",
        "artifact_name": "receipt",
        "artifact_path": "receipt.json",
        "inputs": {"target_contract_hash": "d" * 64, "runtime_contract": "api-v1"},
    }
    spec = multi_repository_models().EvaluationSpecification.model_validate(declaration)
    defaults = dict.fromkeys(
        (
            *spec.probe.inputs,
            "source_snapshot_hash",
            "source_manifest",
            "specification_digest",
            "probe_criteria",
            "adp_correlation",
            "adp_source_revision",
            "adp_definition_revision",
        ),
        "",
    )
    if fault == "extra-input":
        defaults["unapproved"] = "default"
    definition = WorkflowDefinition("b" * 40, "a" * 40, "c" * 40, defaults, True, "main", "a" * 40)
    document = {
        "on": {"workflow_dispatch": {}},
        "run-name": "${{ format('ADP deployment {0}', inputs.adp_correlation) }}",
        "concurrency": {"group": "fixture-probe", "cancel-in-progress": fault == "cancellable"},
    }
    if fault == "other-trigger":
        document["on"]["push"] = {}
    provider = RepositoryScanProvider(evidence=SimpleNamespace(definition_blob=AsyncMock(return_value=("blob", json.dumps(document).encode()))))
    provider.definition = AsyncMock(return_value=definition)
    sources = [
        {
            "address": declared.address,
            "repository_id": declared.repository_id,
            "head_sha": revision * 40,
            "deployment_receipt_key": f"receipt-{declared.address}",
            "components": [component],
            "runtime_artifacts": [
                {
                    "component": component,
                    "actual_revision": revision * 40,
                    "artifact_hash": "f" * 64,
                    "image_digest": "sha256:" + "f" * 64,
                    "tick_digest": None,
                    "migration_head": "head",
                    "asset_count": 0,
                }
            ],
            "deployment_targets": [{"resource_id": "compatible-fixture"}],
        }
        for declared, revision, component in zip(spec.predecessors, ("1", "2"), ("api-container", "client-cli"), strict=True)
    ]
    if fault is None:
        prepared = await provider.probe_preflight(SimpleNamespace(repo="fixture/harness"), spec, "e" * 64, sources)
        assert prepared["inputs"]["source_snapshot_hash"] == "e" * 64
        assert prepared["inputs"]["specification_digest"] == multi_repository_models().specification_digest(spec)
        assert json.loads(prepared["inputs"]["source_manifest"])["sources"] == [
            {
                "address": source["address"],
                "repository_id": source["repository_id"],
                "revision": source["head_sha"],
                "deployment_receipt_key": source["deployment_receipt_key"],
                "components": source["components"],
                "runtime_artifacts": source["runtime_artifacts"],
                "targets": source["deployment_targets"],
            }
            for source in sources
        ]
        assert json.loads(prepared["inputs"]["probe_criteria"]) == [item.model_dump(mode="json") for item in spec.criteria]
        assert prepared["workflow"]["path"] == spec.probe.path
    else:
        with pytest.raises(CycleBlockedError, match="probe_"):
            await provider.probe_preflight(SimpleNamespace(repo="fixture/harness"), spec, "e" * 64, sources)


@pytest.mark.parametrize("interrupt", [False, True])
async def test_repository_collector_binds_distinct_product_sources_and_harness(monkeypatch, interrupt):
    from contextlib import asynccontextmanager

    from src.orchestration import repository_producer_controller as controller_module
    from src.orchestration.evaluation_contract import multi_repository_models, verify_multi_repository_receipt
    from src.orchestration.execution_runner import ObservationKind, OperationIdentity
    from src.orchestration.repository_producer import digest
    from src.orchestration.work_claims import Disposition
    from tests.orchestration.test_multi_repository_evaluation_contract import golden

    vector = golden()
    contract = multi_repository_models()
    specification = vector["specification"]
    specification["predecessors"][1]["evidence_kind"] = "merge"
    specification["harness"].update(
        adapter="engine-repository-evidence-v1",
        operation="collect",
        harness_digest="1685f7d60a29195abfef438e060dc8cdf6e1b2afa029be91449ee408b2304d93",
    )
    spec = contract.EvaluationSpecification.model_validate(specification)
    node = SimpleNamespace(org_id="org-fixture", flow_id="flow-fixture", id="eval-node", attempts=1)
    plan = SimpleNamespace(version=2, plan_hash="d" * 64, plan_document={"product_id": "product-fixture"})
    sources = [
        dict(
            address="flow/epic/wave/a",
            repository_id=101,
            node_id="node-a",
            attempt=1,
            execution_id="source-a",
            accepted_plan_version=2,
            binding_id="binding-a",
            binding_revision=1,
            provider_pr_node_id="PR-fixture-a",
            pr_number=17,
            head_sha="1" * 40,
            merge_sha="2" * 40,
        ),
        dict(
            address="flow/epic/wave/b",
            repository_id=202,
            node_id="node-b",
            attempt=1,
            execution_id="source-b",
            accepted_plan_version=2,
            binding_id="binding-b",
            binding_revision=1,
            provider_pr_node_id="PR-fixture-b",
            pr_number=17,
            head_sha="3" * 40,
            merge_sha="4" * 40,
        ),
    ]
    executions = {
        source["execution_id"]: SimpleNamespace(
            id=source["execution_id"],
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=source["node_id"],
            cycle=1,
            accepted_plan_version=2,
            claim_generation=1,
        )
        for source in sources
    }
    db = SimpleNamespace(
        get=AsyncMock(side_effect=lambda model, identity: executions[identity]),
        scalar=AsyncMock(
            return_value=SimpleNamespace(detail={"acceptance_decision_id": "accepted-evaluation", "source_snapshot_hash": digest(sources)})
        ),
    )

    @asynccontextmanager
    async def factory():
        yield db

    accepted = (SimpleNamespace(id="accepted-evaluation"), spec, SimpleNamespace(policy_hash="e" * 64))
    pre_probe_admission_hash = "14733118fc6616e500b1de7ecc26d660eef3ec235522a295b6c62cf51cf06d39"
    assert digest(spec.model_dump(mode="json")) == pre_probe_admission_hash
    admission = dict(specification_hash=pre_probe_admission_hash, source_snapshot_hash=digest(sources), evaluation_policy_hash="e" * 64)
    state = (node, plan, spec, accepted, admission, sources, SimpleNamespace(repo="fixture/harness"))
    context = SimpleNamespace(
        execution=SimpleNamespace(
            id="eval-execution",
            cycle=1,
            accepted_plan_version=2,
            claim_id="harness-claim",
            claim_generation=1,
            deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
        identity=SimpleNamespace(claim_id="harness-claim", claim_generation=1),
    )
    bindings = {
        "flow/epic/wave/a": SimpleNamespace(full_name="fixture/source-a", installation_id=901, repository_id=101),
        "flow/epic/wave/b": SimpleNamespace(full_name="fixture/source-b", installation_id=902, repository_id=202),
    }
    harness = SimpleNamespace(full_name="fixture/harness", installation_id=903, repository_id=303)
    evidence = SimpleNamespace(token=AsyncMock(return_value="read-only"), deployed_revision=AsyncMock(return_value={}))
    observed_bindings = []
    interrupted = interrupt

    async def verified(binding, source, declared):
        nonlocal interrupted
        observed_bindings.append((binding.repo, binding.provider_repository_id, declared.address))
        if interrupted and declared.address.endswith("/b"):
            interrupted = False
            raise CycleBlockedError("repository_evaluation_source_incomplete")
        checks = [dict(check) for check in vector["sources"][0]["checks"]] if declared.address.endswith("/a") else []
        return checks, [
            dict(repository_id=binding.provider_repository_id, pr_node_id=source.get("provider_pr_node_id"), check_run_id=index + 50)
            for index in range(len(checks))
        ]

    evidence.verified_multi_source = AsyncMock(side_effect=verified)
    monkeypatch.setattr(controller_module, "multi_repository_sources", AsyncMock(return_value=(sources, bindings, harness)))
    monkeypatch.setattr(controller_module, "authorize", AsyncMock(return_value=("accepted-evaluation", "e" * 64)))
    monkeypatch.setattr("src.orchestration.plan_lineage.ancestor_plan", AsyncMock(return_value=plan))
    controller = RepositoryProducerController(factory, provider=SimpleNamespace(evidence=evidence))
    operation_key = OperationIdentity.from_context(context, "multi_repository_context", accepted[0].id).key
    controller.snapshot = AsyncMock(return_value=(state, {**admission, "operation_key": operation_key}))
    if interrupt:
        partial = await controller.observe(context)
        assert partial.kind is ObservationKind.BLOCKED and partial.snapshot is None
        assert partial.block.detail == "repository_evaluation_source_incomplete:flow/epic/wave/b:202"
        assert len(observed_bindings) == 2
        observed_bindings.clear()
    observed = await controller.observe(context)
    assert observed.kind is ObservationKind.SUCCEEDED and observed.stage == "repository_settle"
    receipt = observed.snapshot["receipt"]
    assert [(row["repository_id"], row["node_id"]) for row in receipt["sources"]] == [(101, "node-a"), (202, "node-b")]
    assert [item[0] for item in observed_bindings] == ["fixture/source-a", "fixture/source-b"]
    assert evidence.deployed_revision.await_count == (4 if interrupt else 2)
    assert receipt["input_set_digest"] != digest([source["address"] for source in sources])
    assert receipt["verdict"] == "pass" and receipt["producer"]["provider_identity"] == "fixture/harness"
    assert [(row["pull_request_number"], row["pull_request_node_id"]) for row in receipt["sources"]] == [
        (17, "PR-fixture-a"),
        (17, "PR-fixture-b"),
    ]
    verify_multi_repository_receipt(specification, receipt, receipt["sources"], now=datetime.now(UTC))
    monkeypatch.setattr(controller_module, "record", AsyncMock())
    released = AsyncMock(return_value=SimpleNamespace(disposition=Disposition.ADMITTED))
    monkeypatch.setattr(controller_module, "release_work", released)
    monkeypatch.setattr("src.orchestration.tick.release_satisfied_successors", AsyncMock())
    node.state = "running"
    await controller.settle_multi(db, context, state, observed.snapshot)
    assert node.state == "passed" and released.await_args.kwargs["claim_id"] == "harness-claim"


async def gateway_product_bound_multi_repository_case(repository_evaluation, monkeypatch, human_contract, blocked_input=None, *, recover=False):  # noqa: F811
    from copy import deepcopy

    from sqlalchemy import inspect

    from src.orchestration import evaluation_plan, repository_binding
    from src.orchestration import repository_evaluation as repository_module
    from src.orchestration.adapters.github_comments import GateAnswerStatus, InputPath, apply_gate_answer_for_context
    from src.orchestration.compile import ApprovalContext
    from src.orchestration.dispatch import graph_address
    from src.orchestration.dispatch_pass import DispatchPassConfig, DispatchPassReport, _dispatch_one
    from src.orchestration.evaluation_acceptance import EvaluationAcceptanceRequest, accept_evaluation, preview_evaluation
    from src.orchestration.evaluation_plan import human_evaluation, human_gate_evidence, validate_human_gate_evidence
    from src.orchestration.execution_runner import RunnerContext
    from src.orchestration.execution_store import load_execution
    from src.orchestration.gate_execution import prepare_gate_execution
    from src.orchestration.merge_controller import MERGE_KIND
    from src.orchestration.models import OrchestrationEdge, OrchestrationPullRequestBinding
    from src.orchestration.repository_evaluation_contract import harness_digest
    from src.orchestration.tick import run_tick
    from tests.orchestration.test_evaluation_controller import gate_access

    ctx = repository_evaluation
    async with ctx.factory() as db:
        parent = await db.get(OrchestrationNode, ctx.node.id)
        evaluation_node = await db.get(OrchestrationNode, ctx.eval_id)
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        flow = await db.get(OrchestrationFlow, ctx.flow.id)
        flow.product_id = "product-8404"
        binding_a = await db.get(OrchestrationPullRequestBinding, ctx.binding.id)
        parent.repository_identity = binding_a.repo
        parent.provider_repository_id = binding_a.provider_repository_id
        parent.repository_connection = {"provider": "github", "installation_id": 901}
        second = OrchestrationNode(
            org_id=parent.org_id,
            flow_id=parent.flow_id,
            epic_ref=parent.epic_ref,
            wave_ref=parent.wave_ref,
            node_ref="SOURCEB",
            kind="story",
            title="Second source",
            state="passed",
            attempts=parent.attempts,
            issue_ref="18",
            repository_identity="fixture/source-b",
            provider_repository_id=202,
            repository_connection={"provider": "github", "installation_id": 902},
        )
        db.add(second)
        await db.flush()
        db.add(OrchestrationEdge(org_id=parent.org_id, flow_id=parent.flow_id, from_node_id=second.id, to_node_id=evaluation_node.id))

        def clone(row, **updates):
            values = {
                column.key: deepcopy(getattr(row, column.key))
                for column in inspect(row).mapper.column_attrs
                if column.key not in {"id", "created_at", "updated_at"}
            }
            values.update(updates)
            return type(row)(**values)

        binding_b = clone(
            binding_a,
            node_id=second.id,
            repo="fixture/source-b",
            provider_repository_id=202,
            provider_pr_node_id="PR-source-b",
            accepted_scope=json.dumps({"node": {"kind": second.kind, "issue_ref": second.issue_ref, "title": second.title}}),
        )
        db.add(binding_b)
        source_action = (
            await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == MERGE_KIND, OrchestrationAction.status == "succeeded"))
        ).one()
        source_execution = await db.get(OrchestrationExecution, source_action.execution_id)
        execution_b = clone(source_execution, node_id=second.id)
        db.add(execution_b)
        await db.flush()
        detail = deepcopy(source_action.detail)
        detail["merge_receipt"].update(
            node_id=second.id,
            execution_id=execution_b.id,
            repo=binding_b.repo,
            provider_repository_id=202,
            provider_pr_node_id=binding_b.provider_pr_node_id,
        )
        db.add(clone(source_action, execution_id=execution_b.id, detail=detail))
        evaluation_node.issue_ref = "19"
        evaluation_node.repository_identity = "fixture/harness"
        evaluation_node.provider_repository_id = 303
        evaluation_node.repository_connection = {"provider": "github", "installation_id": 903}
        spec_document = {
            "evidence_schema": "multi-repository-evaluation/v1",
            "mode": "repository",
            "acceptance_mode": "human" if human_contract else "machine",
            "harness": {
                "repository": "fixture/harness",
                "repository_id": 303,
                "installation_id": 903,
                "source_revision": "a" * 40,
                "definition_revision": "b" * 40,
                "harness_digest": harness_digest(),
                "adapter": "engine-repository-evidence-v1",
                "operation": "collect",
            },
            "predecessors": [
                {
                    "address": graph_address(story, flow_slug=flow.slug),
                    "repository_id": repository_id,
                    "evidence_kind": "merge",
                    "required_checks": [{"name": "Unit tests", "app_id": 15368}],
                }
                for story, repository_id in ((parent, parent.provider_repository_id), (second, 202))
            ],
            "criteria": [{"criterion_id": "AC-01"}],
        }
        if blocked_input in {"source_repository", "pull_request", "workflow_repository"}:
            assert binding_a.pr_number == binding_b.pr_number
            assert spec_document["predecessors"][0]["required_checks"][0]["name"] == spec_document["predecessors"][1]["required_checks"][0]["name"]
        if blocked_input == "workflow_repository":
            spec_document["predecessors"][1]["required_checks"][0]["workflow_path"] = ".github/workflows/ci.yml"
        document = deepcopy(plan.plan_document)
        document["product_id"] = flow.product_id
        document["execution_policy"]["repository_ids"] = [binding_a.repo, binding_b.repo, "fixture/harness"]
        if human_contract:
            document["execution_policy"]["evaluation_acceptance"][graph_address(evaluation_node, flow_slug=flow.slug)] = "human"
        document["nodes"][-1].update(
            issue_ref="19",
            repository_identity="fixture/harness",
            provider_repository_id=303,
            repository_connection=evaluation_node.repository_connection,
            evaluation=None if human_contract == "attached" else spec_document,
        )
        document["nodes"].append(
            dict(address=graph_address(second, flow_slug=flow.slug), kind="story", title=second.title, issue_ref=second.issue_ref)
        )
        plan.plan_document = document
        await db.commit()

    async def resolve(db, **kwargs):
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
    actor = ApprovalContext(org_id=parent.org_id, actor_id="human", actor_role="org_admin", reason="Approve exact A/B sources")
    request = EvaluationAcceptanceRequest(
        node_id=ctx.eval_id,
        expected_plan_version=plan.version,
        expected_plan_hash=plan.plan_hash,
        specification=spec_document,
        authorize_evaluate=True,
        reason="Verify both repository sources and harness",
    )
    async with ctx.factory() as db:
        preview = await preview_evaluation(db, flow_id=ctx.flow.id, actor=actor, request=request)
        receipt = await accept_evaluation(
            db, flow_id=ctx.flow.id, actor=actor, request=request.model_copy(update={"expected_snapshot": preview["snapshot"]})
        )
        assert receipt["decision_id"]
        await db.commit()
    controller = RepositoryProducerController(ctx.factory)
    evidence = controller.provider.evidence
    evidence.token = AsyncMock(return_value="scoped-read")
    evidence.deployed_revision = AsyncMock(return_value={})
    seen = []
    interruptions_remaining = 2 if recover else 0
    api_reads = []

    async def substituted(binding, source, expected):
        import httpx

        from src.orchestration.repository_evaluation_provider import RepositoryEvidenceProvider

        prefix = "/repos/fixture/source-b"

        def read(request):
            assert request.url.path.startswith(prefix)
            assert request.headers["Authorization"] == "Bearer scoped-source-b"
            api_reads.append(request.url.path)
            if request.url.path == prefix + f"/pulls/{source['pr_number']}":
                return httpx.Response(
                    200,
                    json=dict(
                        number=source["pr_number"],
                        node_id=seen[0][2] if blocked_input == "pull_request" else source["provider_pr_node_id"],
                        merged=True,
                        merged_at="2026-10-21T00:00:00Z",
                        merge_commit_sha=source["merge_sha"],
                        base={"repo": {"id": 101 if blocked_input == "source_repository" else 202}},
                        head={"sha": source["head_sha"], "repo": {"id": 202}},
                    ),
                )
            if request.url.path == prefix + f"/commits/{source['head_sha']}/check-runs":
                return httpx.Response(
                    200,
                    json={
                        "check_runs": [
                            dict(
                                id=71,
                                name="Unit tests",
                                app={"id": 15368},
                                check_suite={"id": 31},
                                head_sha=source["head_sha"],
                                status="completed",
                                conclusion="success",
                            )
                        ]
                    },
                )
            if request.url.path == prefix + "/actions/runs":
                return httpx.Response(
                    200,
                    json={
                        "workflow_runs": [
                            dict(
                                id=30,
                                workflow_id=40,
                                check_suite_id=31,
                                run_attempt=1,
                                head_sha=source["head_sha"],
                                repository={"id": 101},
                                head_repository={"id": 202},
                                path=".github/workflows/ci.yml",
                                status="completed",
                                conclusion="success",
                            )
                        ]
                    },
                )
            pytest.fail(f"unexpected provider read: {request.url.path}")

        async with httpx.AsyncClient(transport=httpx.MockTransport(read)) as client:
            provider = RepositoryEvidenceProvider(client=client)
            provider.token = AsyncMock(return_value="scoped-source-b")
            provider.reviews.bound_pull_request = AsyncMock(return_value=SimpleNamespace(head_sha=source["head_sha"], review_approved=True))
            return await provider.verified_multi_source(binding, source, expected)

    async def verify(binding, source, expected):
        nonlocal interruptions_remaining
        seen.append((binding.repo, binding.provider_repository_id, source["provider_pr_node_id"]))
        if binding.repo == "fixture/source-b":
            if interruptions_remaining:
                interruptions_remaining -= 1
                raise ConnectionError("source B read interrupted")
            if blocked_input in {"source_repository", "pull_request", "workflow_repository"}:
                return await substituted(binding, source, expected)
            if blocked_input == "failed":
                raise CycleBlockedError("repository_evaluation_required_check_not_successful")
            if blocked_input == "missing":
                raise CycleBlockedError("repository_evaluation_required_check_missing_or_ambiguous")
            if blocked_input == "interrupted":
                raise ConnectionError("provider read interrupted")
            if blocked_input == "timeout":
                await asyncio.sleep(0.5)
        checks = [
            dict(
                name=check.name,
                app_id=check.app_id,
                head_sha=source["head_sha"],
                workflow_path=None,
                workflow_source_revision=None,
                workflow_definition_revision=None,
                conclusion="success",
            )
            for check in expected.required_checks
        ]
        return checks, [dict(repository_id=binding.provider_repository_id, pr_node_id=source["provider_pr_node_id"], check_run_id=70 + len(seen))]

    evidence.verified_multi_source = AsyncMock(side_effect=verify)

    if human_contract:
        async with ctx.factory() as db:
            node = await db.get(OrchestrationNode, ctx.eval_id)
            selected = await human_evaluation(db, node)
            assert selected is not None and selected[1].acceptance_mode == "human"
            preview = await prepare_gate_execution(db, gate=node, actor=None)
            assert preview["ready"] is False and preview["evidence_id"] is None
            with pytest.raises(CycleBlockedError, match="evaluation_repository_receipt_not_collected"):
                await human_gate_evidence(db, node, *selected, now=datetime.now(UTC))
            assert (
                await db.scalar(
                    select(OrchestrationDecision.id).where(
                        OrchestrationDecision.node_id == node.id, OrchestrationDecision.kind == "human_evaluation_evidence"
                    )
                )
                is None
            )
            node.state = "pending"
            await db.flush()
            tick = await run_tick(db)
            assert tick.errors == 0
            assert node.id not in tick.blocked
            assert node.state == "ready" and node.attempts == 0
            assert (
                await db.scalar(
                    select(OrchestrationDecision.id).where(
                        OrchestrationDecision.node_id == node.id, OrchestrationDecision.kind == "human_evaluation_evidence"
                    )
                )
                is None
            )

    monkeypatch.setattr(repository_module, "RepositoryEvidenceProvider", lambda: evidence)
    monkeypatch.setattr("src.orchestration.dispatch_pass.product_binding_pending_verification", AsyncMock(return_value=False))
    async with ctx.factory() as db:
        node = await db.get(OrchestrationNode, ctx.eval_id)
        report = DispatchPassReport(enabled=True)
        await _dispatch_one(db, node, config=DispatchPassConfig(queue_url="queue", repo="fixture/harness"), report=report)
        assert node.state == "running" and report.pending == []
        ctx.claim_spy.assert_not_awaited()
        await db.commit()
        execution = await db.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == ctx.eval_id))
    from src.orchestration.execution_state import ExecutionStatus

    if blocked_input == "timeout":
        monkeypatch.setattr(repository_module, "OBSERVATION_TIMEOUT_SECONDS", 0.1)
    handler = EvaluationController(ctx.factory)
    handler.repository_producer = controller
    runner = await run_execution_runner(
        ctx.factory,
        handlers={ExecutionPhase.EVALUATION_PENDING: handler},
        config=RunnerConfig(enabled=True, max_attempts=8, io_timeout_seconds=10),
        notifier=AsyncMock(return_value="test-notice"),
    )
    assert runner.errors == 0 and runner.blocked == int(blocked_input is not None or recover) and runner.examined == 1
    if recover:
        assert runner.blocked == 1 and interruptions_remaining == 1
        async with ctx.factory() as db:
            blocked = await db.get(OrchestrationExecution, execution.id)
            initial = (blocked.claim_id, blocked.claim_generation, blocked.cycle, blocked.deadline_at, blocked.attempts)
            context_action = await db.scalar(
                select(OrchestrationAction).where(
                    OrchestrationAction.execution_id == execution.id, OrchestrationAction.kind == "multi_repository_context"
                )
            )
            initial_hash = context_action.detail["source_snapshot_hash"]
            blocked.next_check_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
        replay = await run_execution_runner(
            ctx.factory,
            handlers={ExecutionPhase.EVALUATION_PENDING: handler},
            config=RunnerConfig(enabled=True, max_attempts=8, io_timeout_seconds=10),
            notifier=AsyncMock(return_value="test-notice"),
        )
        assert replay.errors == 0 and replay.blocked == 1 and replay.examined == 1 and interruptions_remaining == 0
        async with ctx.factory() as db:
            blocked = await db.get(OrchestrationExecution, execution.id)
            assert (blocked.claim_id, blocked.claim_generation, blocked.cycle, blocked.deadline_at, blocked.attempts) == initial
            assert (await db.get(OrchestrationNode, ctx.eval_id)).state == "running"
            assert (await db.get(OrchestrationAction, context_action.id)).detail["source_snapshot_hash"] == initial_hash
            identity = ExecutionIdentity(
                blocked.org_id, blocked.node_id, blocked.cycle, blocked.accepted_plan_version, blocked.claim_id, blocked.claim_generation
            )
            loaded = await load_execution(db, identity=identity)
            context = RunnerContext(identity, loaded.record, datetime.now(UTC))
        previous = await controller.observe(context)
        assert previous.snapshot is not None
        previous_digest = previous.snapshot["receipt"]["input_set_digest"]
        async with ctx.factory() as db:
            repaired = await db.get(OrchestrationNode, second.id)
            current_binding = await db.get(OrchestrationPullRequestBinding, binding_b.id)
            repaired_execution = await db.get(OrchestrationExecution, execution_b.id)
            repaired.attempts += 1
            next_binding = clone(current_binding, attempt=repaired.attempts, head_sha="d" * 40, provider_pr_node_id="PR-repaired-b", pr_number=78)
            db.add(next_binding)
            next_execution = clone(repaired_execution, cycle=repaired.attempts, claim_generation=repaired_execution.claim_generation + 1)
            db.add(next_execution)
            await db.flush()
            next_detail = deepcopy(detail)
            next_key = source_action.operation_key + ":repaired"
            next_detail["merge_receipt"].update(
                cycle=repaired.attempts,
                execution_id=next_execution.id,
                claim_generation=next_execution.claim_generation,
                operation_key=next_key,
                provider_pr_node_id=next_binding.provider_pr_node_id,
                pr_number=next_binding.pr_number,
                reviewed_head_sha=next_binding.head_sha,
                merge_sha="e" * 40,
            )
            db.add(clone(source_action, execution_id=next_execution.id, operation_key=next_key, detail=next_detail))
            blocked = await db.get(OrchestrationExecution, execution.id)
            blocked.next_check_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
        async with ctx.factory() as db:
            with pytest.raises(CycleBlockedError, match="repository_evaluation_predecessor_checks_changed"):
                await RepositoryProducerController(ctx.factory).settle(db, context, previous.snapshot)
            await db.rollback()
        final = await run_execution_runner(
            ctx.factory,
            handlers={ExecutionPhase.EVALUATION_PENDING: handler},
            config=RunnerConfig(enabled=True, max_attempts=8, io_timeout_seconds=10),
            notifier=AsyncMock(return_value="test-notice"),
        )
        assert final.errors == 0 and final.examined == 1 and final.advanced == 1
        async with ctx.factory() as db:
            settled = await db.get(OrchestrationExecution, execution.id)
            assert (settled.claim_id, settled.claim_generation, settled.cycle, settled.deadline_at, settled.attempts) == initial
            action = await db.get(OrchestrationAction, context_action.id)
            assert action.detail["source_snapshot_hash"] == initial_hash
            assert action.detail["settled_source_snapshot_hash"] != initial_hash
            completed = list(
                await db.scalars(
                    select(OrchestrationDecision).where(OrchestrationDecision.node_id == ctx.eval_id, OrchestrationDecision.kind == "result_observed")
                )
            )
            details = [json.loads(row.reason) for row in completed]
            receipt = next(row["receipt"] for row in details if row.get("action") == "multi_repository_completed")
            assert receipt["input_set_digest"] != previous_digest
            assert receipt["sources"][1]["reviewed_head_sha"] == "d" * 40
            assert receipt["sources"][1]["merge_sha"] == "e" * 40
            refreshed = [row for row in details if row.get("action") == "multi_repository_sources_refreshed"]
            assert len(refreshed) == 1 and refreshed[0]["previous_source_snapshot_hash"] == initial_hash
            assert refreshed[0]["source_snapshot_hash"] != initial_hash
            node = await db.get(OrchestrationNode, ctx.eval_id)
            assert node.state == ("awaiting_gate" if human_contract else "passed")
            if human_contract:
                selected = await human_evaluation(db, node)
                bound = await human_gate_evidence(db, node, *selected, now=datetime.now(UTC))
                assert bound["repository_receipt"]["input_set_digest"] == receipt["input_set_digest"]
        idle = await run_execution_runner(
            ctx.factory,
            handlers={ExecutionPhase.EVALUATION_PENDING: handler},
            config=RunnerConfig(enabled=True, max_attempts=8, io_timeout_seconds=10),
            notifier=AsyncMock(return_value="test-notice"),
        )
        assert idle.examined == 0
        return
    async with ctx.factory() as db:
        finished = await db.get(OrchestrationExecution, execution.id)
        assert finished.status == (ExecutionStatus.BLOCKED if blocked_input else ExecutionStatus.CONCLUDED)
        rows = list(
            await db.scalars(
                select(OrchestrationDecision).where(
                    OrchestrationDecision.node_id == ctx.eval_id,
                    OrchestrationDecision.kind == "result_observed",
                    OrchestrationDecision.actor_id == "system:repository-evaluation",
                )
            )
        )
        completed = [json.loads(row.reason) for row in rows if json.loads(row.reason).get("action") == "multi_repository_completed"]
        if blocked_input:
            reasons = {
                "failed": "repository_evaluation_required_check_not_successful",
                "missing": "repository_evaluation_required_check_missing_or_ambiguous",
                "interrupted": "repository_evaluation_source_unverifiable",
                "timeout": "repository_evaluation_time_budget",
                "source_repository": "repository_evaluation_pull_request_identity_or_merge_changed",
                "pull_request": "repository_evaluation_pull_request_identity_changed",
                "workflow_repository": "repository_evaluation_required_check_workflow_identity",
            }
            assert finished.block_detail == f"{reasons[blocked_input]}:{graph_address(second, flow_slug=flow.slug)}:202"
            assert not completed and (await db.get(OrchestrationNode, ctx.eval_id)).state == "running"
            assert (
                await db.scalar(
                    select(OrchestrationDecision.id).where(
                        OrchestrationDecision.node_id == ctx.eval_id, OrchestrationDecision.kind == "human_evaluation_evidence"
                    )
                )
                is None
            )
            assert seen == [
                (binding_a.repo, binding_a.provider_repository_id, binding_a.provider_pr_node_id),
                ("fixture/source-b", 202, "PR-source-b"),
            ]
            if blocked_input in {"source_repository", "pull_request", "workflow_repository"}:
                assert api_reads and all(path.startswith("/repos/fixture/source-b/") for path in api_reads)
                assert ("/repos/fixture/source-b/actions/runs" in api_reads) == (blocked_input == "workflow_repository")
            return
        assert len(completed) == 1
        snapshot = completed[0]
    assert seen == [(binding_a.repo, binding_a.provider_repository_id, binding_a.provider_pr_node_id), ("fixture/source-b", 202, "PR-source-b")]
    assert [
        (item["repository_id"], item["pull_request_number"], item["pull_request_node_id"], item["reviewed_head_sha"], item["merge_sha"])
        for item in snapshot["receipt"]["sources"]
    ] == [
        (123, binding_a.pr_number, binding_a.provider_pr_node_id, binding_a.head_sha, detail["merge_receipt"]["merge_sha"]),
        (202, binding_a.pr_number, "PR-source-b", binding_a.head_sha, detail["merge_receipt"]["merge_sha"]),
    ]
    assert [row["checks"][0]["check_run_id"] for row in snapshot["source_observations"]] == [71, 72]
    assert flow.product_id == "product-8404" and all(item["flow_id"] == flow.id for item in snapshot["receipt"]["sources"])
    async with ctx.factory() as db:
        node = await db.get(OrchestrationNode, ctx.eval_id)
        assert node.state == ("awaiting_gate" if human_contract else "passed")
        if human_contract:
            selected = await human_evaluation(db, node)
            bound = await human_gate_evidence(db, node, *selected, now=datetime.now(UTC))
            assert bound["repository_receipt"]["input_set_digest"] == snapshot["receipt"]["input_set_digest"]
            assert bound["repository_receipt"]["receipt"]["sources"] == snapshot["receipt"]["sources"]
            tick = await run_tick(db)
            assert tick.errors == 0
            gate = await db.scalar(
                select(OrchestrationDecision).where(
                    OrchestrationDecision.node_id == node.id, OrchestrationDecision.kind == "human_evaluation_evidence"
                )
            )
            assert gate is not None
            preview = await prepare_gate_execution(db, gate=node, actor=None)
            assert preview["evidence_id"] == gate.id and preview["ready"]
            assert await validate_human_gate_evidence(db, node, preview["evidence_id"]) == bound
            assert node.state == "awaiting_gate"
            expires = datetime.fromisoformat(snapshot["receipt"]["expires_at"])
            with pytest.raises((CycleBlockedError, ValueError)):
                await human_gate_evidence(db, node, *selected, now=expires)
            with monkeypatch.context() as clock:
                clock.setattr(evaluation_plan, "datetime", SimpleNamespace(now=lambda _: expires))
                with pytest.raises(CycleBlockedError, match="evaluation_repository_receipt_unverifiable"):
                    await validate_human_gate_evidence(db, node, preview["evidence_id"])
            changed = await db.get(OrchestrationPullRequestBinding, binding_b.id)
            changed.revision += 1
            await db.flush()
            with pytest.raises(CycleBlockedError, match="evaluation_repository_context_changed"):
                await validate_human_gate_evidence(db, node, preview["evidence_id"])
            human = SimpleNamespace(org_id=node.org_id, user_id="approver", account_type="human", auth_source="jwt", cognito_username="approver")
            answer = dict(
                context=human,
                node_id=node.id,
                approve=True,
                reason="Reviewed both current repository sources",
                access=gate_access(),
                input_path=InputPath.DASHBOARD,
                expected_plan_hash=plan.plan_hash,
                execution_preview=preview,
            )
            refused = await apply_gate_answer_for_context(db, **answer)
            assert refused.status is GateAnswerStatus.REFUSED_STALE_PLAN
            assert node.state == "awaiting_gate"
            changed.revision -= 1
            await db.flush()
            product_access = AsyncMock()
            with monkeypatch.context() as product:
                product.setattr("src.orchestration.compile.require_accessible_product", product_access)
                applied = await apply_gate_answer_for_context(db, **answer)
            product_access.assert_awaited_once()
            assert applied.status is GateAnswerStatus.APPLIED
            assert node.state == "passed"


@pytest.mark.parametrize("human_contract", ["original", "attached", None])
async def test_product_bound_multi_repository_admission_and_verdict(repository_evaluation, monkeypatch, human_contract):  # noqa: F811
    await gateway_product_bound_multi_repository_case(repository_evaluation, monkeypatch, human_contract)


@pytest.mark.parametrize("blocked_input", ["failed", "missing", "interrupted", "timeout"])
async def test_gateway_multi_repository_blocked_source_is_identified(repository_evaluation, monkeypatch, blocked_input):  # noqa: F811
    await gateway_product_bound_multi_repository_case(repository_evaluation, monkeypatch, None, blocked_input)


@pytest.mark.parametrize("substitution", ["source_repository", "pull_request", "workflow_repository"])
async def test_gateway_rejects_cross_repository_identity_substitution(repository_evaluation, monkeypatch, substitution):  # noqa: F811
    await gateway_product_bound_multi_repository_case(repository_evaluation, monkeypatch, None, substitution)


@pytest.mark.parametrize("human_contract", [None, "original", "attached"])
async def test_gateway_replays_interrupted_source_and_settles_repaired_revision(repository_evaluation, monkeypatch, human_contract):  # noqa: F811
    await gateway_product_bound_multi_repository_case(repository_evaluation, monkeypatch, human_contract, recover=True)


@pytest.fixture
async def scan(contract_request, monkeypatch):  # noqa: F811
    ctx = contract_request
    document = deepcopy(ctx.spec)
    document["workflows"] = [
        dict(
            criterion_id="final-scan",
            path=".github/workflows/scan.yml",
            source=dict(predecessor=document["predecessors"][0]["address"]),
            definition=dict(revision="f" * 40),
            required_jobs=["Scan"],
            artifacts=[
                dict(
                    name="scan-{run_attempt}",
                    path="scan-receipt.json",
                    predicates=[dict(criterion_id="coverage", pointer="/coverage_complete", operation="equals", expected=True)],
                )
            ],
        )
    ]
    document["producer"] = dict(
        mode="dispatch_once",
        inputs=dict(expected_account_id="123456789012", region="us-east-1"),
        workflow_criterion_id="final-scan",
        target=dict(account_id="123456789012", region="us-east-1", resource_kind="repository_scan", resource_id=ctx.binding.repo),
        receipt_artifact="scan-{run_attempt}",
        receipt_path="scan-receipt.json",
        images={
            "controller": {
                "grype": dict(digest="sha256:" + "a" * 64, provenance_sha256="b" * 64),
                "syft": dict(digest="sha256:" + "c" * 64, provenance_sha256="d" * 64),
            }
        },
    )
    ctx.request = ctx.request.model_copy(update=dict(specification=document, authorize_workflow_dispatch=True))
    ctx.provider_scan = RepositoryScanProvider(evidence=ctx.provider)
    ctx.provider_scan.observe = AsyncMock(return_value=(None, False))

    async def preflight(self, binding, spec, sources):
        return dict(
            source_revision=sources[0]["merge_sha"],
            definition=dict(
                approved_revision="f" * 40,
                source_revision=sources[0]["merge_sha"],
                blob_sha="e" * 40,
                defaults=dict(adp_correlation="", adp_source_revision="", adp_definition_revision="", **spec.producer.inputs),
                dispatchable=True,
                dispatch_ref="main",
                dispatch_revision="f" * 40,
                caller_path=None,
                caller_blob_sha=None,
            ),
            workflow=dict(
                path=".github/workflows/scan.yml",
                definition_revision="f" * 40,
                allowed_inputs={key: [value] for key, value in spec.producer.inputs.items()},
                correlation_input="adp_correlation",
            ),
        )

    monkeypatch.setattr(RepositoryScanProvider, "preflight", preflight)
    ctx.controller = EvaluationController(ctx.factory)
    ctx.controller.repository_producer = RepositoryProducerController(ctx.factory, provider=ctx.provider_scan)
    return ctx


async def admit(ctx):
    async with ctx.factory() as db:
        result, _ = await accept(ctx, db)
        await db.commit()
        node = await db.get(OrchestrationNode, ctx.eval_id)
        await observe_repository_evaluation(db, node, provider=ctx.provider)
        await db.commit()
        assert node.state == "running", [
            row.reason for row in await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id))
        ]
        ctx.acceptance_id = result["decision_id"]
        ctx.scan_execution = await db.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == ctx.eval_id))


async def tick(ctx):
    async with ctx.factory() as db:
        row = await db.get(OrchestrationExecution, ctx.scan_execution.id)
        row.next_check_at = datetime.now(UTC)
        await db.commit()
    return await run_execution_runner(
        ctx.factory,
        handlers={ExecutionPhase.EVALUATION_PENDING: ctx.controller},
        config=RunnerConfig(enabled=True, max_attempts=8, io_timeout_seconds=10),
        notifier=AsyncMock(return_value="test-notice"),
    )


@pytest.mark.parametrize("entry_point", ["observer", "producer"])
async def test_required_inputs_block_producer_before_credentials_claim_or_execution(scan, monkeypatch, entry_point):
    from src.orchestration.repository_producer import admit_producer
    from tests.orchestration.test_amendment_ready_evaluation import input_gate

    preflight = AsyncMock()
    monkeypatch.setattr(RepositoryScanProvider, "preflight", preflight)
    async with scan.factory() as session:
        gate = await input_gate(scan, session)
        gate.state = "pending"
        await session.commit()
        await accept(scan, session)
        await session.commit()
    scan.provider.token.reset_mock()
    for _ in range(3):
        async with scan.factory() as session:
            node = await session.get(OrchestrationNode, scan.eval_id)
            operation = observe_repository_evaluation if entry_point == "observer" else admit_producer
            assert await operation(session, node, provider=scan.provider)
            await session.commit()
            assert node.state == "ready" and node.attempts == 0
            assert await session.scalar(select(OrchestrationExecution.id).where(OrchestrationExecution.node_id == node.id)) is None
            assert await session.scalar(select(OrchestrationWorkClaim.id).where(OrchestrationWorkClaim.issue_number == 999)) is None
            reasons = list(await session.scalars(select(OrchestrationDecision.reason).where(OrchestrationDecision.node_id == node.id)))
            assert any("node:cycle/E1/inputs/target" in reason and "Authorized target" in reason for reason in reasons)
    preflight.assert_not_awaited()
    scan.provider.token.assert_not_awaited()
    scan.provider.observe.assert_not_awaited()
    async with scan.factory() as session:
        available = await session.get(OrchestrationNode, gate.id)
        available.state = "passed"
        await session.commit()
        node = await session.get(OrchestrationNode, scan.eval_id)
        assert await operation(session, node, provider=scan.provider)
        await session.commit()
        assert node.state == "running" and node.attempts == 1
        assert await session.scalar(select(OrchestrationExecution.id).where(OrchestrationExecution.node_id == node.id)) is not None
        assert await session.scalar(select(OrchestrationWorkClaim.id).where(OrchestrationWorkClaim.issue_number == 999)) is not None
    preflight.assert_awaited_once()


@pytest.mark.parametrize("withdraw_at", ["preflight", "authorization"])
async def test_producer_rechecks_input_after_provider_preflight(scan, monkeypatch, withdraw_at):
    from tests.orchestration.test_amendment_ready_evaluation import input_gate, withdraw_input, withdraw_on_reauthorization

    async with scan.factory() as session:
        gate = await input_gate(scan, session)
        await accept(scan, session)
        await session.commit()
        preflight = RepositoryScanProvider.preflight

        async def revoke_during_preflight(*args):
            await withdraw_input(scan, gate.id)
            return await preflight(*args)

        if withdraw_at == "preflight":
            monkeypatch.setattr(RepositoryScanProvider, "preflight", revoke_during_preflight)
        else:
            withdraw_on_reauthorization(scan, gate.id)
        node = await session.get(OrchestrationNode, scan.eval_id)
        assert await observe_repository_evaluation(session, node, provider=scan.provider)
        await session.commit()
        assert node.state == "ready" and node.attempts == 0
        assert await session.scalar(select(OrchestrationExecution.id).where(OrchestrationExecution.node_id == node.id)) is None
        assert await session.scalar(select(OrchestrationWorkClaim.id).where(OrchestrationWorkClaim.issue_number == 999)) is None
        reasons = list(await session.scalars(select(OrchestrationDecision.reason).where(OrchestrationDecision.node_id == node.id)))
        assert any("node:cycle/E1/inputs/target" in reason for reason in reasons)


async def test_scan_dispatch_requires_separate_explicit_authorization(scan):
    async with scan.factory() as db:
        with pytest.raises(EvaluationAcceptanceError, match="explicit_producer_authorization_required"):
            await accept(scan, db, scan.request.model_copy(update={"authorize_workflow_dispatch": False}))
        assert await db.scalar(select(OrchestrationAction.id).where(OrchestrationAction.kind == CONTEXT_KIND)) is None


async def test_admission_creates_real_evaluation_execution_and_claim_without_worker_assignment(scan):
    await admit(scan)
    async with scan.factory() as db:
        node = await db.get(OrchestrationNode, scan.eval_id)
        claim = await db.get(OrchestrationWorkClaim, scan.scan_execution.claim_id)
        assert node.attempts == 1
        assert await db.scalar(select(OrchestrationRunReport.run_id).where(OrchestrationRunReport.node_id == node.id)) is None
        assert claim.owner_ref == node.flow_id and claim.issue_number == 999 and claim.state == "held"
        assert claim.active_run_id is None
        assert scan.scan_execution.phase == "evaluation_pending"


async def test_uncertain_post_is_never_repeated_by_subsequent_runner_ticks(scan):
    sent = []

    async def dispatch(binding, **kwargs):
        await kwargs["reauthorize"]()
        sent.append(kwargs["correlation"])
        raise TimeoutError("Response lost after provider accepted the dispatch")

    scan.provider_scan.dispatch = dispatch
    await admit(scan)
    await tick(scan)
    async with scan.factory() as db:
        rows = list(await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == PRODUCER_KIND)))
        assert len(rows) == 1 and rows[0].detail.get("dispatch_started"), [(row.status, row.detail) for row in rows]
    assert len(sent) == 1
    await tick(scan)
    await tick(scan)
    assert len(sent) == 1
    async with scan.factory() as db:
        node = await db.get(OrchestrationNode, scan.eval_id)
        assert node.state == "running" and node.attempts == 1


@pytest.mark.parametrize("flow_state", ["pending", "running"])
@pytest.mark.parametrize("human", [False, True])
@pytest.mark.parametrize("success", [True, False])
@pytest.mark.parametrize("after_dispatch", [None, "budget_exhausted", "deadline_expired", "collector_upgrade"])
async def test_terminal_scan_releases_real_claim_and_only_verified_evidence_passes_node(
    scan, success, after_dispatch, human, flow_state, monkeypatch
):
    async with scan.factory() as db:
        flow = await db.get(OrchestrationFlow, scan.flow.id)
        flow.state = flow_state
        await db.commit()

    if human:
        document = deepcopy(scan.request.specification)
        document.update(evidence_schema="workflow-evaluation/v1", acceptance_mode="human")
        document["workflows"][0].update(
            path=".github/workflows/eval-cli-uplift.yml",
            source={"revision": "f" * 40},
            required_jobs=["Live evaluation (dev)", "Recovery sweep (this run, plus anything expired)"],
        )
        producer = document["producer"]
        producer.pop("images")
        producer["target"].update(resource_kind="cli-evaluation", resource_id="dev")
        producer["inputs"] = dict(
            environment="dev", expected_revision="f" * 40, mode="start", fixtures_json="{}", suites="knowledge", evaluation_id="", inject_fault="none"
        )
        scan.request = scan.request.model_copy(update={"specification": document})
        async with scan.factory() as db:
            plan = await db.get(OrchestrationAcceptedPlan, scan.plan.id)
            amended = deepcopy(plan.plan_document)
            amended["execution_policy"]["evaluation_acceptance"] = {key: "human" for key in amended["execution_policy"]["evaluation_acceptance"]}
            plan.plan_document = amended
            await db.commit()

    async def dispatch(binding, **kwargs):
        await kwargs["reauthorize"]()

    scan.provider_scan.dispatch = dispatch
    await admit(scan)
    await tick(scan)
    async with scan.factory() as db:
        action = await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == PRODUCER_KIND))
        data = action.detail
    target = scan.request.specification["producer"]["target"]
    workflow_context = WorkflowContext(
        schema_version=1,
        repository_id=123,
        run_id=10,
        run_attempt=1,
        workflow_path=".github/workflows/scan.yml",
        workflow_revision="f" * 40,
        source_revision=data["source_revision"],
        inputs={},
        correlation=data["correlation"],
        **target,
    )
    run = WorkflowRun(
        10,
        1,
        "completed",
        "success",
        "https://github.com/o/r/actions/runs/10",
        workflow_context,
        12,
        "a" * 64,
        datetime.now(UTC).isoformat(),
    )
    scan.provider_scan.observe.return_value = (run, False)

    async def verify(binding, spec, sources):
        proof = await scan.provider.observe(binding, spec, sources)
        return proof["pull_requests"], {item["address"]: item["merge_sha"] for item in sources if item.get("address")}

    scan.provider.verify_sources = AsyncMock(side_effect=verify)
    scan.provider.workflow = AsyncMock(
        return_value=dict(
            criterion_id="final-scan",
            workflow_path=".github/workflows/scan.yml",
            source_revision=data["source_revision"],
            definition_revision="f" * 40,
            workflow_blob_sha="e" * 40,
            run_id=10,
            run_attempt=1,
            event="workflow_dispatch",
            jobs=[dict(name="Scan", job_id=11)],
            artifacts=[dict(artifact_id=13, name="scan-1", digest="d" * 64, path="scan-receipt.json", sha256="e" * 64)],
            criteria=[dict(criterion_id="coverage", passed=success)],
        )
    )
    if after_dispatch == "budget_exhausted":
        scan.spend = Decimal("99999")
    elif after_dispatch == "deadline_expired":
        async with scan.factory() as db:
            execution = await db.get(OrchestrationExecution, scan.scan_execution.id)
            execution.deadline_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
    if after_dispatch == "collector_upgrade":
        from src.orchestration import repository_producer, repository_producer_controller

        original = repository_producer.harness_digest()
        monkeypatch.setattr(repository_producer, "COLLECTION_COMPATIBLE_HARNESSES", {original})
        monkeypatch.setattr(repository_producer, "harness_digest", lambda: "9" * 64)
        monkeypatch.setattr(repository_producer_controller, "harness_digest", lambda: "9" * 64)
        execution = scan.scan_execution
        identity = ExecutionIdentity(
            execution.org_id, execution.node_id, execution.cycle, execution.accepted_plan_version, execution.claim_id, execution.claim_generation
        )
        with pytest.raises(CycleBlockedError):
            await scan.controller.repository_producer.snapshot(SimpleNamespace(identity=identity, execution=execution), authorize_effect=True)
    report = await tick(scan)
    async with scan.factory() as db:
        node = await db.get(OrchestrationNode, scan.eval_id)
        execution = await db.get(OrchestrationExecution, scan.scan_execution.id)
        claim = await db.get(OrchestrationWorkClaim, scan.scan_execution.claim_id)
        if after_dispatch == "collector_upgrade" and not human:
            assert node.state == "running" and execution.status == "blocked" and claim.state == "held"
            return
        assert node.state == (("awaiting_gate" if human else "passed") if success else "failed"), (report, execution.status)
        assert execution.status == "concluded" and claim.state == "released"
        assert claim.release_reason == ("completed" if success else "failed")
        if success:
            row = await db.scalar(
                select(OrchestrationDecision)
                .where(OrchestrationDecision.node_id == node.id, OrchestrationDecision.kind == "result_observed")
                .order_by(OrchestrationDecision.created_at.desc())
                .limit(1)
            )
            assert '"live_attestation":false' in row.reason
            if after_dispatch == "collector_upgrade":
                assert '"collector_harness_sha256":"' + "9" * 64 + '"' in row.reason


async def test_budget_exhausted_after_admission_refuses_actual_provider_post(scan):
    scan.provider_scan.dispatch = AsyncMock()
    await admit(scan)
    scan.spend = Decimal("99999")
    await tick(scan)
    scan.provider_scan.dispatch.assert_not_awaited()
    async with scan.factory() as db:
        rows = list(await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == PRODUCER_KIND)))
        assert all(not row.detail.get("dispatch_started") for row in rows)
        assert (await db.get(OrchestrationNode, scan.eval_id)).state == "running"


@pytest.mark.parametrize("changed_during_preflight", [False, True])
async def test_stale_harness_is_refused_before_claim_or_execution(scan, monkeypatch, changed_during_preflight):
    async with scan.factory() as db:
        await accept(scan, db)
        await db.commit()
    if changed_during_preflight:
        original = RepositoryScanProvider.preflight

        async def change_harness(*args):
            prepared = await original(*args)
            monkeypatch.setattr("src.orchestration.repository_producer.harness_digest", lambda: "0" * 64)
            return prepared

        monkeypatch.setattr(RepositoryScanProvider, "preflight", change_harness)
    else:
        monkeypatch.setattr("src.orchestration.repository_producer.harness_digest", lambda: "0" * 64)
    async with scan.factory() as db:
        node = await db.get(OrchestrationNode, scan.eval_id)
        await observe_repository_evaluation(db, node, provider=scan.provider)
        await db.commit()
        assert node.state == "ready" and node.attempts == 0
        assert await db.scalar(select(OrchestrationExecution.id).where(OrchestrationExecution.node_id == node.id)) is None
        assert await db.scalar(select(OrchestrationWorkClaim.id).where(OrchestrationWorkClaim.issue_number == 999)) is None


async def test_failed_workflow_without_cleanup_proof_retains_claim_and_never_redispatches(scan):
    async def dispatch(binding, **kwargs):
        await kwargs["reauthorize"]()

    scan.provider_scan.dispatch = AsyncMock(side_effect=dispatch)
    await admit(scan)
    await tick(scan)
    # Failure at GitHub is not proof that the asynchronous AWS child scan ended.
    from types import SimpleNamespace

    scan.provider_scan.observe.return_value = (SimpleNamespace(run_id=10, status="completed", conclusion="failure"), False)
    await tick(scan)
    await tick(scan)
    scan.provider_scan.dispatch.assert_awaited_once()
    async with scan.factory() as db:
        node = await db.get(OrchestrationNode, scan.eval_id)
        execution = await db.get(OrchestrationExecution, scan.scan_execution.id)
        claim = await db.get(OrchestrationWorkClaim, scan.scan_execution.claim_id)
        assert node.state == "running" and execution.status == "blocked" and claim.state == "held"
        assert execution.pending_action_key


async def test_collection_upgrade_cannot_start_an_undispatched_knowledge_run(scan, monkeypatch):
    from src.orchestration import repository_producer

    document = deepcopy(scan.request.specification)
    document.update(evidence_schema="workflow-evaluation/v1", acceptance_mode="human")
    document["workflows"][0].update(
        path=".github/workflows/eval-cli-uplift.yml",
        source={"revision": "f" * 40},
        required_jobs=["Live evaluation (dev)", "Recovery sweep (this run, plus anything expired)"],
    )
    producer = document["producer"]
    producer.pop("images")
    producer["target"].update(resource_kind="cli-evaluation", resource_id="dev")
    producer["inputs"] = dict(
        environment="dev", expected_revision="f" * 40, mode="start", fixtures_json="{}", suites="knowledge", evaluation_id="", inject_fault="none"
    )
    scan.request = scan.request.model_copy(update={"specification": document})
    async with scan.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, scan.plan.id)
        amended = deepcopy(plan.plan_document)
        amended["execution_policy"]["evaluation_acceptance"] = {key: "human" for key in amended["execution_policy"]["evaluation_acceptance"]}
        plan.plan_document = amended
        await db.commit()
    await admit(scan)
    original = repository_producer.harness_digest()
    monkeypatch.setattr(repository_producer, "COLLECTION_COMPATIBLE_HARNESSES", {original})
    monkeypatch.setattr(repository_producer, "harness_digest", lambda: "9" * 64)
    scan.provider_scan.dispatch = AsyncMock()
    await tick(scan)
    scan.provider_scan.dispatch.assert_not_awaited()
    async with scan.factory() as db:
        execution = await db.get(OrchestrationExecution, scan.scan_execution.id)
        assert execution.status == "blocked"
        assert execution.block_detail == "repository_evaluation_producer_legacy_collection_requires_dispatch"
