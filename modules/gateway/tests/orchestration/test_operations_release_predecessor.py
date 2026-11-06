"""A completed agent deployment supplies release lineage for the next operation."""

import copy
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import tests.orchestration.test_operations_release_resolution as release_fixtures
from src.orchestration.delivery_contract import DeliveryContract
from src.orchestration.deployment_runtime_contract import DeploymentReceipt
from src.orchestration.models import OrchestrationAction, OrchestrationDecision, OrchestrationExecution, OrchestrationNode
from src.orchestration.operations_workflow_evidence import CorrelatedWorkflowReceipt
from src.orchestration.proposal import OperationalReleaseBinding
from src.orchestration.run_reports import OrchestrationRunReport
from tests.orchestration.test_operations_release_resolution import (
    BASELINE,
    DEFINITION,
    FIRST,
    ORG_A,
    REPO,
    SOURCE,
    _dispatched_release,
    _make_node,
)

REPAIRED = "e" * 40
engine = release_fixtures.engine
session = release_fixtures.session
governed_work_ownership = release_fixtures.governed_work_ownership
healthy_policy_reservations = release_fixtures.healthy_policy_reservations
policy_budget_initializers = release_fixtures.policy_budget_initializers


async def _deployed(session, flow, child, plan, manifest, provider, invalid):
    parent = await _make_node(session, flow, node_ref="deployed", state="passed", attempts=1)
    await release_fixtures._make_usage(session, parent)
    address = f"{flow.slug}/{parent.epic_ref}/{parent.wave_ref}/{parent.node_ref}"
    document = copy.deepcopy(plan.plan_document)
    operating = document["nodes"][-1]
    predecessor = {
        "address": address,
        "kind": "story",
        "title": "Deploy the repaired release",
        "repository_identity": REPO,
        "executor": {"kind": "agent", "role": "operate", "persona": "operations"},
        "required_inputs": [{"name": "workflow", "source": "deployment_workflow:gateway-deploy.yml"}],
        "evidence_expectations": ["runtime_verified:gateway-backend"],
        "operational_release_binding": copy.deepcopy(operating["operational_release_binding"]),
    }
    document["nodes"].insert(-1, predecessor)
    operating["operational_release_binding"]["predecessors"].append({"address": address, "receipt_kind": "verified_operations_release"})
    operating["required_inputs"].append({"name": "repaired_release", "source": f"node:{address}"})
    document["edges"].append({"source": address, "target": operating["address"]})
    if invalid == "definition_from_operations":
        operating["operational_release_binding"]["definition_predecessor"] = address
    if invalid == "parent_not_operations":
        predecessor["executor"]["role"] = "develop"
    if invalid == "frontend_after_backend":
        operating["operational_release_binding"].update(component="gateway-frontend", target="frontend", workflows=["gateway-frontend-deploy.yml"])
        operating["required_inputs"][0]["source"] = "deployment_workflow:gateway-frontend-deploy.yml"
        operating["evidence_expectations"] = ["runtime_verified:gateway-frontend"]
        backend = manifest.entries[0]
        frontend = replace(
            backend,
            entry_id="frontend",
            component_selectors=("gateway-frontend",),
            workflow=replace(backend.workflow, path=".github/workflows/gateway-frontend-deploy.yml"),
        )
        manifest = replace(manifest, entries=(*manifest.entries, frontend))
    plan.plan_document = document
    from src.orchestration.review_cycle import CycleBlockedError

    async def contains(_binding, source, candidate):
        order = {BASELINE: 0, FIRST: 1, SOURCE: 2, REPAIRED: 3}
        if order[source] > order[candidate]:
            raise CycleBlockedError("not_an_ancestor")

    provider.contains.side_effect = contains
    provider.definition.return_value = SimpleNamespace(
        approved_revision=REPAIRED if invalid == "definition_from_operations" else DEFINITION,
        source_revision=REPAIRED,
        dispatch_ref="main",
        dispatch_revision=REPAIRED,
    )
    when = datetime.now(UTC) - timedelta(hours=3)
    execution = OrchestrationExecution(
        org_id=ORG_A,
        flow_id=flow.id,
        node_id=parent.id,
        cycle=1,
        accepted_plan_version=1,
        claim_id="operations-claim",
        claim_generation=3,
        phase="concluded",
        status="concluded",
    )
    session.add(execution)
    await session.flush()

    identity = dict(
        org_id=ORG_A,
        flow_id=flow.id,
        node_id=parent.id,
        execution_id=execution.id,
        cycle=1,
        accepted_plan_version=1,
        claim_id=execution.claim_id,
        claim_generation=3,
        repo=REPO,
    )
    context = CorrelatedWorkflowReceipt(
        **identity,
        operation_key="correlated-workflow:42:1",
        component="gateway-backend",
        workflow="gateway-deploy.yml",
        run_id=42,
        run_attempt=1,
        source_revision=REPAIRED,
        definition_revision=DEFINITION,
        context_artifact_id=17,
        context_digest="f" * 64,
        observed_at=when,
        valid_until=when + timedelta(hours=1),
    )
    context_action = OrchestrationAction(
        org_id=ORG_A,
        execution_id=execution.id,
        operation_key=context.operation_key,
        kind="correlated_workflow_observation",
        status="succeeded",
        attempt=1,
        receipt_ref="github/actions/runs/42/artifacts/17",
        detail={"correlated_workflow_receipt": context.model_dump(mode="json")},
    )
    session.add(context_action)
    await session.flush()
    workflow = OrchestrationAction(
        org_id=ORG_A,
        execution_id=execution.id,
        operation_key="operations-workflow:42:1",
        kind="deployment_workflow",
        status="succeeded",
        attempt=1,
        receipt_ref=context_action.receipt_ref,
        detail={"runtime_verified": True, "context_action_id": context_action.id},
    )
    receipt = DeploymentReceipt(
        **identity,
        operation_key="operations-runtime:42:1",
        source_revision=REPAIRED,
        actual_revision=REPAIRED,
        merge_operation_key=f"operations-source:{REPAIRED}",
        workflow_operation_keys=[workflow.operation_key],
        manifest_entry_ids=["env-1"],
        delivery_complete=True,
        observed_at=when,
        valid_until=when + timedelta(hours=1),
        targets=[
            {
                "provider": "aws",
                "account_id": "123456789012",
                "region": "us-east-1",
                "resource_kind": "eks-namespace",
                "resource_id": "cluster-dev/adp",
                "evidence": {"source": "registered-aws-role:conn-1", "verified_at": when.isoformat()},
            }
        ],
        components=[
            {
                "component": "gateway-backend",
                "actual_revision": REPAIRED,
                "artifact_hash": "f" * 64,
                "image_digest": "sha256:" + "f" * 64,
                "tick_digest": "sha256:" + "f" * 64,
                "migration_head": "schema-123",
                "healthy": True,
                "evidence_ref": "signed-release:42:1",
                "observed_at": when,
                "pod_uids": ["pod-1"],
            }
        ],
    )
    runtime = OrchestrationAction(
        org_id=ORG_A,
        execution_id=execution.id,
        operation_key=receipt.operation_key,
        kind="deployment_verification",
        status="succeeded",
        attempt=1,
        receipt_ref=receipt.operation_key,
        detail={"deployment_receipt": receipt.model_dump(mode="json")},
    )
    session.add_all([workflow, runtime])
    await session.flush()
    run_id = f"completed-operations:{parent.id}"
    body = "Verified the correlated workflow and serving runtime."
    report_id = f"operations-report:{run_id}"
    report = OrchestrationRunReport(
        run_id=run_id,
        credential_hash=hashlib.sha256(run_id.encode()).hexdigest(),
        org_id=ORG_A,
        flow_id=flow.id,
        node_id=parent.id,
        attempt=1,
        repo=REPO,
        persona="operations",
        installation_id=55_501,
        provider_repository_id=12345,
        expires_at=when + timedelta(hours=1),
        terminal_receipt={"outcome": "complete", "run_id": run_id, "attempt": 1},
        dispatch_metadata={
            "orchestration": {"executor_role": "operate"},
            "handoff_expect": {
                "execution_id": execution.id,
                "accepted_plan_version": 1,
                "claim_id": execution.claim_id,
                "claim_generation": 3,
            },
        },
        operations_report_receipt={
            "run_id": run_id,
            "attempt": 1,
            "node_id": parent.id,
            "recorded": True,
            "receipt_id": report_id,
            "report": body,
            "sha256": hashlib.sha256(body.encode()).hexdigest(),
        },
    )
    accepted = dict(attempt=1, run_id=run_id, conclusion_authority="evidence", evidence_ids=[str(runtime.id), str(context_action.id), report_id])
    decision = OrchestrationDecision(
        org_id=ORG_A,
        flow_id=flow.id,
        node_id=parent.id,
        kind="result_observed",
        actor_id="system:orchestration-results",
        actor_kind="service",
        actor_role="engine",
        from_state="running",
        to_state="passed",
        reason=json.dumps(accepted),
        created_at=when + timedelta(minutes=1),
    )
    session.add_all([report, decision])
    if invalid == "not_passed":
        parent.state = "failed"
    elif invalid == "unfinished_execution":
        execution.status = "awaiting_external"
    elif invalid == "wrong_claim":
        execution.claim_generation = 4
    elif invalid == "unknown_effect":
        session.add(
            OrchestrationAction(org_id=ORG_A, execution_id=execution.id, operation_key="unknown", kind="dispatch", status="unknown", attempt=1)
        )
    elif invalid == "failed_report":
        report.terminal_receipt = {**report.terminal_receipt, "outcome": "failed"}
    elif invalid == "wrong_repository_id":
        report.provider_repository_id += 1
    elif invalid == "wrong_installation":
        report.installation_id += 1
    elif invalid == "invalid_report_digest":
        report.operations_report_receipt = {**report.operations_report_receipt, "sha256": "0" * 64}
    elif invalid == "forged_pass":
        decision.actor_kind = "human"
    elif invalid == "receipt_not_accepted":
        decision.reason = json.dumps({**accepted, "evidence_ids": [report_id]})
    elif invalid == "expired_before_acceptance":
        decision.created_at = when + timedelta(hours=2)
    elif invalid == "context_link_changed":
        workflow.detail = {**workflow.detail, "context_action_id": "other"}
    elif invalid == "wrong_workflow":
        context_action.detail = {"correlated_workflow_receipt": {**context.model_dump(mode="json"), "workflow": "another.yml"}}
    elif invalid == "wrong_source":
        context_action.detail = {"correlated_workflow_receipt": {**context.model_dump(mode="json"), "source_revision": FIRST}}
    elif invalid in {"wrong_target", "wrong_connection"}:
        changed = copy.deepcopy(runtime.detail)
        if invalid == "wrong_target":
            changed["deployment_receipt"]["targets"][0]["resource_id"] = "different/adp"
        else:
            changed["deployment_receipt"]["targets"][0]["evidence"]["source"] = "registered-aws-role:other"
        runtime.detail = changed
    await session.flush()

    return manifest


@pytest.mark.parametrize("generic_child", [False, True])
@pytest.mark.parametrize(
    "invalid",
    [
        None,
        "definition_from_operations",
        "frontend_after_backend",
        "not_passed",
        "parent_not_operations",
        "unfinished_execution",
        "wrong_claim",
        "unknown_effect",
        "failed_report",
        "wrong_repository_id",
        "wrong_installation",
        "invalid_report_digest",
        "forged_pass",
        "receipt_not_accepted",
        "expired_before_acceptance",
        "context_link_changed",
        "wrong_workflow",
        "wrong_source",
        "wrong_target",
        "wrong_connection",
    ],
)
async def test_next_agent_uses_accepted_operations_release(session, monkeypatch, governed_work_ownership, invalid, generic_child):
    valid = invalid in {None, "definition_from_operations", "frontend_after_backend"}

    async def prepare(*args):
        manifest = await _deployed(session, *args, invalid)
        monkeypatch.setattr("src.orchestration.operations_release_predecessor.load_packaged_manifest", lambda: manifest)
        if generic_child:
            plan = args[2]
            document = copy.deepcopy(plan.plan_document)
            child = document["nodes"][-1]
            delivery = DeliveryContract(
                schema_version=2,
                connection_id="conn-1",
                resource_kind="service",
                resource_id="test/consumer",
                baseline_revision=BASELINE,
                definition_revision=DEFINITION,
                required_checks=["consumer-smoke"],
            )
            child["operational_release_binding"].update(
                target="consumer",
                component="consumer",
                workflows=["ship-consumer.yml"],
                delivery_contract=delivery.model_dump(mode="json"),
            )
            child["required_inputs"][0]["source"] = "deployment_workflow:ship-consumer.yml"
            child["evidence_expectations"] = ["runtime_verified:consumer"]
            assert manifest.entry(child["operational_release_binding"]["target"]) is None
            assert manifest.entry(document["nodes"][-2]["operational_release_binding"]["target"]) is not None
            plan.plan_document = document
        return manifest

    fixture = await _dispatched_release(session, monkeypatch, prepare_release=prepare, expected_dispatched=int(valid))
    if not valid:
        fixture.provider.contains.assert_not_awaited()
        fixture.provider.definition.assert_not_awaited()
        actions = list(await session.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == "operations_release_binding")))
        assert actions == []
        assert (await session.get(OrchestrationNode, fixture.node.id)).attempts == 0
        return
    envelope = fixture.first_pass.pending[0].envelope["orchestration"]
    if generic_child:
        assert envelope["release_binding"]["target"] == "consumer"
        assert envelope["release_binding"]["resource_id"] == "test/consumer"
    assert envelope["deployment_revisions"] == {
        "source_revision": REPAIRED,
        "definition_revision": REPAIRED if invalid == "definition_from_operations" else DEFINITION,
        "default_branch": "main",
    }
    receipts = envelope["release_binding"]["receipts"]
    runtime = next(item for item in receipts if item.get("receipt_kind") == "verified_operations_release")
    assert runtime["source_revision"] == REPAIRED and "merge_sha" not in runtime
    assert runtime["operation_key"] == "operations-runtime:42:1" and runtime["completion_decision_id"]
    assert {item["merge_sha"] for item in receipts if "merge_sha" in item} == {FIRST, SOURCE}
    assert {call.args[1:] for call in fixture.provider.contains.await_args_list} >= {(FIRST, REPAIRED), (SOURCE, REPAIRED), (BASELINE, REPAIRED)}


def test_runtime_release_cannot_replace_a_required_build():
    address = "flow/epic/wave/build"
    with pytest.raises(ValueError, match="build prerequisites require verified merge"):
        OperationalReleaseBinding.model_validate(
            {
                "predecessors": [{"address": address, "receipt_kind": "verified_operations_release"}],
                "build_prerequisites": [address],
                "repository": REPO,
                "target": "env-1",
                "component": "gateway-backend",
                "workflows": ["gateway-deploy.yml"],
                "default_branch": "main",
            }
        )
