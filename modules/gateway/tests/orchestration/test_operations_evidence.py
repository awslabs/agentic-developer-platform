"""An operations report cannot satisfy runtime expectations without engine receipts."""

import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from src.agentauth.routes import require_agent_transport
from src.agentauth.run_report_routes import router
from src.orchestration.dispatch_pass import attempt_run_id
from src.orchestration.models import (
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationEnvironmentLease,
    OrchestrationExecution,
    OrchestrationNode,
)
from src.orchestration.operations_evidence import observed_operations_evidence
from src.orchestration.results import _story_evidence, binding_required, observe_results
from src.orchestration.run_reports import OrchestrationRunReport, prepare_run_report, record_terminal
from src.shared.models.audit import AuditLog
from tests.orchestration.test_dispatch_pass import ORG_A as ORG
from tests.orchestration.test_report_dispatch import engine, prepare, reporting, session, session_factory  # noqa: F401


@pytest.mark.parametrize(
    "invalid",
    [None, "missing_action", "wrong_target", "wrong_receipt", "changed_metadata", "different_pins", "wrong_claim", "foreign_action", "wrong_attempt"],
)
@pytest.mark.parametrize(
    ("component", "wave", "inputs"),
    [
        ("gateway", "wave", {}),
        ("tool-adapters", "w09-release", {"stage": "tool-adapters"}),
        ("gateway-backend", "release", {"release_group": "atlassian-foundation"}),
        ("invoices", "rollout", {"stage": "canary", "release_group": "billing"}),
    ],
)
async def test_refuses_invalid_receipts(session, monkeypatch, invalid, component, wave, inputs):  # noqa: F811
    from src.orchestration.deployment_workflow_provider import WorkflowContext
    from src.orchestration.operations_workflow_evidence import record_correlated_workflow

    node = await prepare(session)
    node.state, node.attempts = "running", 1
    node.wave_ref = wave
    row = OrchestrationExecution(
        org_id=ORG,
        flow_id=node.flow_id,
        node_id=node.id,
        cycle=1,
        phase="delivering",
        status="active",
        accepted_plan_version=1,
        claim_id="release-claim",
        claim_generation=1,
    )
    session.add(row)
    await session.flush()
    run_id = attempt_run_id(node.id, 1)
    pins = {"source_revision": "b" * 40, "definition_revision": "d" * 40, "default_branch": "main"}
    address = "flow/epic/wave/build"
    release = {
        "target": "dev",
        "component": component,
        "repository": "aws-e/adp",
        "workflow": ".github/workflows/deploy.yml",
        "accepted_plan_version": 1,
        "receipts": [{"address": address, "action_id": "merge-1", "merge_sha": pins["source_revision"]}],
        "pins": pins,
    }
    action = OrchestrationAction(
        org_id=ORG,
        execution_id=row.id,
        operation_key="operations-release:1",
        kind="operations_release_binding",
        status="succeeded",
        attempt=1,
        detail=copy.deepcopy(release),
    )
    session.add(action)
    metadata = {
        "message_id": run_id,
        "tenant_id": ORG,
        "persona": "operations",
        "source_ref": {"repo": "aws-e/adp", "installation_id": 42, "provider_repository_id": 123},
        "orchestration": {
            "node_id": node.id,
            "flow_id": node.flow_id,
            "attempt": 1,
            "executor_role": "operate",
            "deployment_revisions": copy.deepcopy(pins),
            "release_binding": copy.deepcopy(release),
        },
        "handoff_expect": {"accepted_plan_version": 1, "execution_id": row.id, "claim_id": row.claim_id, "claim_generation": 1},
    }
    report = await prepare_run_report(session, metadata)
    record_terminal(report, "complete")
    contract = SimpleNamespace(
        plan_version=1,
        node=SimpleNamespace(
            executor=SimpleNamespace(role="operate", persona="operations"),
            evidence_expectations=[f"runtime_verified:{component}"],
            repository_identity="aws-e/adp",
            required_inputs=[SimpleNamespace(source="deployment_workflow:deploy.yml")],
            operational_release_binding=SimpleNamespace(
                maintenance_adoption_id=None,
                repository="aws-e/adp",
                component=component,
                target="dev",
                default_branch="main",
                workflows=["deploy.yml"],
                predecessors=[SimpleNamespace(address=address)],
            ),
        ),
    )
    monkeypatch.setattr("src.orchestration.operations_workflow_evidence.accepted_node_contract", AsyncMock(return_value=contract))
    monkeypatch.setattr("src.orchestration.operations_evidence.accepted_node_contract", AsyncMock(return_value=contract))
    raw_run = {"id": 42, "run_attempt": 1, "path": ".github/workflows/deploy.yml"}
    context = WorkflowContext(
        schema_version=1,
        repository_id=123,
        run_id=42,
        run_attempt=1,
        workflow_path=raw_run["path"],
        workflow_revision=pins["definition_revision"],
        source_revision=pins["source_revision"],
        account_id="123456789012",
        region="us-east-1",
        resource_kind="test-target",
        resource_id="fictional-target",
        inputs=inputs,
        correlation=run_id,
    )
    execution = {"invocation_id": {"S": run_id}, "persona": {"S": "operations"}, **{key: {"S": value} for key, value in pins.items()}}
    if invalid == "missing_action":
        await session.delete(action)
    elif invalid == "wrong_target":
        action.detail = {**release, "target": "foreign-target"}
        report.dispatch_metadata = {**metadata, "orchestration": {**metadata["orchestration"], "release_binding": action.detail}}
    elif invalid == "wrong_receipt":
        action.detail = {**release, "receipts": [{"address": "foreign/build", "action_id": "merge-1"}]}
        report.dispatch_metadata = {**metadata, "orchestration": {**metadata["orchestration"], "release_binding": action.detail}}
    elif invalid == "changed_metadata":
        report.dispatch_metadata = {
            **metadata,
            "orchestration": {**metadata["orchestration"], "release_binding": {**release, "target": "foreign-target"}},
        }
    elif invalid == "different_pins":
        execution["source_revision"] = {"S": "e" * 40}
        context = context.model_copy(update={"source_revision": "e" * 40})
    elif invalid == "wrong_claim":
        row.claim_generation = 2
    elif invalid == "foreign_action":
        action.org_id = "foreign-org"
    elif invalid == "wrong_attempt":
        action.attempt = 2
    node_id = node.id
    await session.commit()
    session.expire_all()
    receipt_id = await record_correlated_workflow(
        session,
        execution=execution,
        binding=SimpleNamespace(repo="aws-e/adp", org_id=ORG, provider_repository_id=123),
        issued=SimpleNamespace(node_id=node_id, plan_version=1, workflow_allowlist={"deploy.yml"}),
        raw_run=raw_run,
        context=context,
        artifact_id=99,
        digest="f" * 64,
    )
    assert (receipt_id is not None) == (invalid is None)
    observations = (await session.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == "correlated_workflow_observation"))).all()
    assert len(observations) == (1 if invalid is None else 0)
    assert not (await session.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == "deployment_verification"))).all()
    assert not (await session.scalars(select(OrchestrationEnvironmentLease))).all()
    stored_report = await session.get(OrchestrationRunReport, run_id)
    assert stored_report.dispatch_metadata["orchestration"]["deployment_revisions"] == pins
    if invalid != "different_pins":
        _, reason = await observed_operations_evidence(
            session,
            await session.get(OrchestrationNode, node_id),
            {"run_id": run_id, "attempt": 1, "conclusion_authority": "evidence", "repo": "aws-e/adp"},
            expected=(contract, (component,)),
        )
        expected_reason = "evidence_missing" if invalid in {None, "wrong_claim"} else "operations_assignment_changed"
        assert reason == expected_reason


async def test_report_only_prose_holds_without_runtime_receipt(session, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ADP_ENGINE_OPERATIONS_EXECUTOR", "true")
    node = await prepare(session)
    node.state = "running"
    node.attempts = 1
    await session.flush()
    execution = OrchestrationExecution(
        org_id=ORG,
        flow_id=node.flow_id,
        node_id=node.id,
        cycle=1,
        phase="delivering",
        status="active",
        accepted_plan_version=1,
        claim_id="accepted-claim",
        claim_generation=1,
    )
    session.add(execution)
    await session.flush()
    session.add(
        OrchestrationAction(
            org_id=ORG,
            execution_id=execution.id,
            operation_key="unverified-worker-claim",
            kind="deployment_verification",
            status="succeeded",
            attempt=1,
            detail={"runtime_verified": True, "deployment_receipt": {"components": [{"component": "tools-facade"}]}},
        )
    )
    await session.flush()
    run_id = "operations-attempt"
    metadata = {
        "message_id": run_id,
        "tenant_id": ORG,
        "persona": "operations",
        "source_ref": {"repo": "aws-e/adp", "installation_id": 42, "provider_repository_id": 123},
        "orchestration": {
            "node_id": node.id,
            "flow_id": node.flow_id,
            "attempt": 1,
            "executor_role": "operate",
            "deployment_revisions": {"source_revision": "a" * 40, "definition_revision": "b" * 40, "default_branch": "main"},
        },
        "handoff_expect": {"accepted_plan_version": 1, "execution_id": execution.id, "claim_id": "accepted-claim", "claim_generation": 1},
    }
    report = await prepare_run_report(session, metadata)
    record_terminal(report, "complete")
    report.operations_report_receipt = {"recorded": True, "run_id": run_id, "report": "Deployed tools-facade"}
    expected = SimpleNamespace(
        node=SimpleNamespace(
            operational_release_binding=None,
            executor=SimpleNamespace(role="operate", persona="operations"),
            evidence_expectations=["runtime_verified:tools-facade"],
            repository_identity="aws-e/adp",
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
        plan_version=1,
    )
    monkeypatch.setattr("src.orchestration.operations_evidence.accepted_node_contract", AsyncMock(return_value=expected))
    dispatch = {"run_id": run_id, "attempt": 1, "conclusion_authority": "evidence", "repo": "aws-e/adp"}
    assert not binding_required(dispatch)
    assert await observed_operations_evidence(session, node, dispatch) == (None, "evidence_missing")
    source = SimpleNamespace(merged_story=AsyncMock(side_effect=AssertionError("merge cannot prove runtime")))
    assert await _story_evidence(session, node=node, dispatch=dispatch, source=source, installation_id=42) == (None, "evidence_missing")
    source.merged_story.assert_not_awaited()
    expected.node.evidence_expectations.append("verified-merge")
    assert await _story_evidence(session, node=node, dispatch=dispatch, source=source, installation_id=42) == (None, "operations_assignment_changed")
    source.merged_story.assert_not_awaited()
    monkeypatch.setenv("ADP_ENGINE_OPERATIONS_EXECUTOR", "false")
    assert binding_required(dispatch)


@pytest.mark.parametrize("repository_identity", ["aws-e/adp", None])
async def test_receipt_backed_result_records_ids_after_authenticated_report(session, session_factory, monkeypatch, repository_identity):  # noqa: F811
    # A node that leaves its repository identity unset still receives the
    # report of the repository the engine dispatched (#7349, attempt 8).
    monkeypatch.setenv("ADP_ENGINE_OPERATIONS_EXECUTOR", "true")
    node = await prepare(session)
    node.state, node.attempts = "running", 1
    execution = OrchestrationExecution(
        org_id=ORG,
        flow_id=node.flow_id,
        node_id=node.id,
        cycle=1,
        phase="delivering",
        status="active",
        accepted_plan_version=1,
        claim_id="accepted-claim",
        claim_generation=1,
    )
    session.add(execution)
    await session.flush()
    run_id = attempt_run_id(node.id, 1)
    metadata = {
        "message_id": run_id,
        "tenant_id": ORG,
        "persona": "operations",
        "source_ref": {"repo": "aws-e/adp", "installation_id": 42, "provider_repository_id": 123},
        "orchestration": {
            "node_id": node.id,
            "flow_id": node.flow_id,
            "attempt": 1,
            "executor_role": "operate",
            "deployment_revisions": {"source_revision": "a" * 40, "definition_revision": "b" * 40, "default_branch": "main"},
        },
        "handoff_expect": {"accepted_plan_version": 1, "execution_id": execution.id, "claim_id": "accepted-claim", "claim_generation": 1},
    }
    await prepare_run_report(session, metadata)
    dispatch = {
        "run_id": run_id,
        "attempt": 1,
        "arrived_at": datetime.now(UTC).isoformat(),
        "repo": "aws-e/adp",
        "conclusion_authority": "evidence",
        "issue": 4196,
    }
    session.add(
        OrchestrationDecision(
            org_id=ORG,
            flow_id=node.flow_id,
            node_id=node.id,
            kind="node_dispatched",
            actor_id="engine",
            actor_role="engine",
            actor_kind="service",
            reason=json.dumps(dispatch),
        )
    )
    contract = SimpleNamespace(
        node=SimpleNamespace(
            operational_release_binding=None,
            executor=SimpleNamespace(role="operate", persona="operations"),
            evidence_expectations=["runtime_verified:tools-facade"],
            repository_identity=repository_identity,
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
        plan_version=1,
    )
    monkeypatch.setattr("src.orchestration.accepted_node.accepted_node_contract", AsyncMock(return_value=contract))
    monkeypatch.setattr("src.orchestration.operations_evidence.accepted_node_contract", AsyncMock(return_value=contract))
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: session_factory)
    await session.commit()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_agent_transport] = lambda: None
    headers = {"X-Adp-Report-Credential": metadata["run_report"]["credential"]}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://gateway.test") as client:
        endpoint = "/internal/v1/agent/report"
        assert (await client.post(endpoint + "/operations", headers=headers, json={"report": "💡" * 4001})).status_code == 422
        assert (await client.post(endpoint + "/terminal", headers=headers, json={"outcome": "complete"})).status_code == 409
        attached = await client.post(endpoint + "/operations", headers=headers, json={"report": "Observed gateway-backend"})
        assert attached.status_code == 200, attached.text
        receipt_id = attached.json()["operations_report_receipt"]["receipt_id"]
        assert "report" not in attached.json()["operations_report_receipt"]
        assert (await client.post(endpoint + "/operations", headers=headers, json={"report": "Observed gateway-backend"})).status_code == 200
        assert (await client.post(endpoint + "/operations", headers=headers, json={"report": "A different report"})).status_code == 409
        assert (await client.post(endpoint + "/terminal", headers=headers, json={"outcome": "complete"})).status_code == 200
    await session.rollback()
    observation = await observe_results(session)
    assert (await session.get(OrchestrationNode, node.id)).state == "awaiting_merge"
    assert observation.reasons[node.id] == "evidence_missing"
    hold = await session.scalar(
        select(OrchestrationDecision).where(
            OrchestrationDecision.node_id == node.id,
            OrchestrationDecision.kind == "result_observed",
            OrchestrationDecision.to_state == "awaiting_merge",
        )
    )
    assert json.loads(hold.reason)["hold_reason"] == "evidence_missing"
    workflow = OrchestrationAction(
        org_id=ORG,
        execution_id=execution.id,
        operation_key="workflow-run-1",
        kind="deployment_workflow",
        status="succeeded",
        attempt=1,
        detail={"runtime_verified": True},
    )
    session.add(workflow)
    await session.flush()
    now = datetime.now(UTC)
    receipt = {
        "org_id": ORG,
        "execution_id": execution.id,
        "node_id": node.id,
        "flow_id": node.flow_id,
        "cycle": 1,
        "accepted_plan_version": 1,
        "claim_id": "accepted-claim",
        "claim_generation": 1,
        "operation_key": "runtime-receipt-1",
        "repo": "aws-e/adp",
        "source_revision": "a" * 40,
        "actual_revision": "a" * 40,
        "merge_operation_key": "merge-1",
        "workflow_operation_keys": [workflow.operation_key],
        "manifest_entry_ids": ["tools-facade"],
        "targets": [{"component": "tools-facade"}],
        "delivery_complete": True,
        "observed_at": now.isoformat(),
        "valid_until": (now + timedelta(hours=1)).isoformat(),
        "components": [
            {
                "component": "tools-facade",
                "actual_revision": "a" * 40,
                "artifact_hash": "f" * 64,
                "image_digest": "sha256:" + "f" * 64,
                "healthy": True,
                "evidence_ref": "github/actions/runs/1/artifacts/1",
                "observed_at": now.isoformat(),
                "pod_uids": ["test-pod"],
            }
        ],
    }
    receipt_action = OrchestrationAction(
        org_id=ORG,
        execution_id=execution.id,
        operation_key="runtime-receipt-1",
        kind="deployment_verification",
        status="succeeded",
        attempt=1,
        detail={"deployment_receipt": receipt},
        receipt_ref="unverified",
    )
    session.add(receipt_action)
    await session.flush()
    assert await observed_operations_evidence(session, node, dispatch) == (None, "evidence_missing")
    receipt_action.receipt_ref = receipt_action.operation_key
    workflow.kind = "unverified_workflow"
    assert await observed_operations_evidence(session, node, dispatch) == (None, "evidence_missing")
    workflow.kind = "deployment_workflow"
    evidence_ids = sorted([receipt_id, str(receipt_action.id)])
    await observe_results(session)
    assert (await session.get(OrchestrationNode, node.id)).state == "passed"
    decision = await session.scalar(
        select(OrchestrationDecision).where(
            OrchestrationDecision.node_id == node.id,
            OrchestrationDecision.kind == "result_observed",
            OrchestrationDecision.to_state == "passed",
        )
    )
    assert json.loads(decision.reason)["evidence_ids"] == evidence_ids
    from src.orchestration.deployment_workflow_provider import WorkflowContext
    from src.orchestration.operations_workflow_evidence import record_correlated_workflow

    monkeypatch.setattr("src.orchestration.operations_workflow_evidence.accepted_node_contract", AsyncMock(return_value=contract))
    await session.delete(receipt_action)
    await session.flush()
    raw_run = {"id": 42, "run_attempt": 1, "path": ".github/workflows/deploy.yml"}
    context = WorkflowContext(
        schema_version=1,
        repository_id=123,
        run_id=42,
        run_attempt=1,
        workflow_path=raw_run["path"],
        workflow_revision="b" * 40,
        source_revision="a" * 40,
        account_id="123456789012",
        region="us-east-1",
        resource_kind="test-target",
        resource_id="fictional-target",
        inputs={},
        correlation=run_id,
    )
    issued = SimpleNamespace(node_id=node.id, plan_version=1, workflow_allowlist={"deploy.yml"})
    binding = SimpleNamespace(repo="aws-e/adp", org_id=ORG, provider_repository_id=123)
    protected = {
        "invocation_id": {"S": run_id},
        "persona": {"S": "operations"},
        "source_revision": {"S": "a" * 40},
        "definition_revision": {"S": "b" * 40},
        "default_branch": {"S": "main"},
    }
    arguments = dict(execution=protected, binding=binding, issued=issued, raw_run=raw_run, context=context, artifact_id=99, digest="f" * 64)
    workflow_evidence_id = await record_correlated_workflow(session, **arguments)
    assert workflow_evidence_id is not None
    assert await record_correlated_workflow(session, **arguments) == workflow_evidence_id
    assert (
        await record_correlated_workflow(
            session, **{**arguments, "context": context.model_copy(update={"inputs": {"release_group": "atlassian-foundation"}})}
        )
        == workflow_evidence_id
    )
    assert (
        await record_correlated_workflow(session, **{**arguments, "context": context.model_copy(update={"inputs": {"stage": "tool-adapters"}})})
        == workflow_evidence_id
    )
    assert (
        await record_correlated_workflow(
            session, **{**arguments, "binding": SimpleNamespace(repo="other/repo", org_id=ORG, provider_repository_id=123)}
        )
        is None
    )
    assert (
        await record_correlated_workflow(
            session, **{**arguments, "binding": SimpleNamespace(repo="aws-e/adp", org_id=ORG, provider_repository_id=456)}
        )
        is None
    )
    report = await session.get(OrchestrationRunReport, run_id)
    report.dispatch_metadata["handoff_expect"]["claim_generation"] = 2
    assert await record_correlated_workflow(session, **arguments) is None
    report.dispatch_metadata["handoff_expect"]["claim_generation"] = 1
    assert await record_correlated_workflow(session, **{**arguments, "context": context.model_copy(update={"correlation": "wrong"})}) is None
    observation = {}
    source = SimpleNamespace(merged_story=AsyncMock(side_effect=AssertionError("merge cannot prove runtime")))
    assert await _story_evidence(session, node=node, dispatch=dispatch, source=source, installation_id=42, observation=observation) == (
        None,
        "evidence_missing",
    )
    assert "evidence_ids" not in observation
    source.merged_story.assert_not_awaited()


def test_operational_expectations_change_preview_only_when_accepted(monkeypatch):
    from src.orchestration.executor_assignment import ExecutorAssignment
    from src.orchestration.preview import ConclusionAuthority, derive_nodes
    from src.orchestration.proposal import validate_proposal
    from tests.orchestration.test_proposal import make_proposal

    monkeypatch.setenv("ADP_ENGINE_OPERATIONS_EXECUTOR", "true")
    proposal = make_proposal()
    story = proposal.nodes[0]
    story.executor = ExecutorAssignment(kind="agent", role="operate", persona="operations")
    story.evidence_expectations = ["runtime_verified:tools-facade"]
    assert not any(item.rule == "evidence_expectation_unsupported" for item in validate_proposal(proposal))
    assert derive_nodes(proposal)[story.address].concluded_by is ConclusionAuthority.EVIDENCE
    story.evidence_expectations.append("verified-merge")
    assert any(item.rule == "evidence_expectation_unsupported" for item in validate_proposal(proposal))
    story.evidence_expectations.pop()
    story.executor = ExecutorAssignment(kind="agent", role="develop", persona="developer")
    assert any(item.rule == "evidence_expectation_unsupported" for item in validate_proposal(proposal))
    assert derive_nodes(proposal)[story.address].concluded_by is ConclusionAuthority.MERGED_PULL_REQUEST
    story.evidence_expectations = []
    assert derive_nodes(proposal)[story.address].concluded_by is ConclusionAuthority.MERGED_PULL_REQUEST


@pytest.mark.parametrize("repository_scope", [False, True])
async def test_three_repair_merges_advance_pins_durably_without_changing_accepted_plan(session, monkeypatch, repository_scope):  # noqa: F811
    from src.orchestration.operations_repair import record_repair_merge, repair_revisions

    node = await prepare(session)
    node.state, node.attempts = "running", 1
    row = OrchestrationExecution(
        org_id=ORG,
        flow_id=node.flow_id,
        node_id=node.id,
        cycle=1,
        phase="delivering",
        status="active",
        accepted_plan_version=1,
        claim_id="claim",
        claim_generation=1,
    )
    session.add(row)
    await session.flush()
    assignment = SimpleNamespace(
        tenant_id=ORG,
        node_id=node.id,
        accepted_plan_version=1,
        attempt=1,
        claim_generation=1,
        invocation_id="ops-run",
        repository="example/service",
        default_branch="main",
    )
    original = {"source_revision": "a" * 40, "definition_revision": "b" * 40, "default_branch": "main"}
    policy = SimpleNamespace(
        policy_hash="accepted-policy",
        repair_paths=[] if repository_scope else ["src/service.py"],
        repair_scope="repository" if repository_scope else None,
        repository_ids=["example/service"],
    )
    authorized = SimpleNamespace(policy=policy, workflow_allowlist={"deploy.yml"})
    accepted = SimpleNamespace(
        plan_version=1,
        node=SimpleNamespace(
            operational_release_binding=None,
            repository_identity="example/service",
            required_inputs=[
                SimpleNamespace(source=prefix + value)
                for prefix, value in (
                    ("deployment_source_revision:", original["source_revision"]),
                    ("deployment_definition_revision:", original["definition_revision"]),
                    ("deployment_branch:", "main"),
                    ("deployment_workflow:", "deploy.yml"),
                )
            ],
        ),
    )
    from unittest.mock import AsyncMock

    monkeypatch.setattr("src.orchestration.operations_repair.accepted_node_contract", AsyncMock(return_value=accepted))
    monkeypatch.setattr(
        "src.orchestration.operations_repair.load_in_force_policy",
        AsyncMock(return_value=SimpleNamespace(policy=policy, plan_version=1, refusal=None)),
    )
    for number, letter in enumerate("cde", 1):
        args = dict(pull_number=number, head=str(number) * 40, merged={"merged": True, "sha": letter * 40})
        await record_repair_merge(session, assignment, authorized=authorized, **args)
        await session.commit()
        # A lost response/re-entry observes the same merge, never a second journal entry.
        await record_repair_merge(session, assignment, authorized=authorized, **args)
        pins = await repair_revisions(session, node_id=node.id, org_id=ORG, plan_version=1, cycle=1, pins=original)
        assert pins == {**original, "source_revision": letter * 40, "definition_revision": letter * 40}
    rows = (await session.scalars(select(OrchestrationAction).where(OrchestrationAction.execution_id == row.id))).all()
    assert len(rows) == 3
    assert all(row.detail.get("repair_scope") == policy.repair_scope for row in rows)
    assert rows[0].detail["before_source_revision"] == original["source_revision"]
    assert rows[1].detail["before_source_revision"] == "c" * 40
    for field, changed in (
        ("repository", "another/repo"),
        ("default_branch", "release"),
        ("workflows", ["other.yml"]),
        ("repair_paths", ["src/credentials.py"]),
        ("repair_scope", None if repository_scope else "repository"),
        ("policy_hash", "expanded-credentials-or-target"),
        ("before_source_revision", "f" * 40),
        ("claim_generation", 2),
    ):
        original_detail = rows[0].detail
        rows[0].detail = {**original_detail, field: changed}
        assert await repair_revisions(session, node_id=node.id, org_id=ORG, plan_version=1, cycle=1, pins=original) is None
        rows[0].detail = original_detail
    policy.policy_hash = "changed-policy-and-approval-authority"
    assert await repair_revisions(session, node_id=node.id, org_id=ORG, plan_version=1, cycle=1, pins=original) is None
    policy.policy_hash = "accepted-policy"
    saved_inputs = list(accepted.node.required_inputs)
    accepted.node.required_inputs.append(SimpleNamespace(source="deployment_workflow:expanded.yml"))
    assert await repair_revisions(session, node_id=node.id, org_id=ORG, plan_version=1, cycle=1, pins=original) is None
    accepted.node.required_inputs = saved_inputs
    policy.repository_ids = ["another/repo"]
    assert await repair_revisions(session, node_id=node.id, org_id=ORG, plan_version=1, cycle=1, pins=original) is None
    policy.repository_ids = ["example/service"]
    policy.repair_paths = ["src/service.py", "src/secrets.py"]
    assert await repair_revisions(session, node_id=node.id, org_id=ORG, plan_version=1, cycle=1, pins=original) is None
    policy.repair_paths = [] if repository_scope else ["src/service.py"]
    execution_before = row.claim_generation
    row.claim_generation += 1
    assert (await repair_revisions(session, node_id=node.id, org_id=ORG, plan_version=1, cycle=1, pins=original))["source_revision"] == "e" * 40
    row.claim_generation = execution_before
    assert original["source_revision"] == "a" * 40
    # An unaccepted version cannot discard the recorded repairs and return their base.
    assert await repair_revisions(session, node_id=node.id, org_id=ORG, plan_version=2, cycle=1, pins=original) is None
    assignment.claim_generation = 2
    import pytest

    from src.agentauth.github_operations import OperationRefusedError

    with pytest.raises(OperationRefusedError):
        await record_repair_merge(session, assignment, pull_number=4, head="4" * 40, merged={"merged": True, "sha": "f" * 40}, authorized=authorized)


async def test_bound_repair_preserves_source_definition_and_scope(session, monkeypatch):  # noqa: F811
    from src.agentauth.github_operation_service import authorize_operations_grant
    from src.agentauth.github_operations import OperationRefusedError
    from src.orchestration.execution_policy import Action
    from src.orchestration.operations_repair import record_repair_merge, repair_revisions

    monkeypatch.setenv("ADP_ENGINE_OPERATIONS_EXECUTOR", "true")
    node = await prepare(session)
    node.state, node.attempts = "running", 1
    execution = OrchestrationExecution(
        org_id=ORG,
        flow_id=node.flow_id,
        node_id=node.id,
        cycle=1,
        phase="delivering",
        status="active",
        accepted_plan_version=1,
        claim_id="accepted-claim",
        claim_generation=1,
    )
    session.add(execution)
    await session.flush()
    run_id = attempt_run_id(node.id, 1)
    pins = {"source_revision": "b" * 40, "definition_revision": "d" * 40, "default_branch": "main"}
    address = "flow/epic/wave/build"
    resolution = {
        "target": "dev",
        "component": "gateway",
        "repository": "aws-e/adp",
        "workflow": ".github/workflows/deploy.yml",
        "accepted_plan_version": 1,
        "receipts": [{"address": address, "action_id": "merge-1", "merge_sha": pins["source_revision"]}],
        "pins": pins,
    }
    release_action = OrchestrationAction(
        org_id=ORG,
        execution_id=execution.id,
        operation_key="operations-release:1",
        kind="operations_release_binding",
        status="succeeded",
        attempt=1,
        detail=copy.deepcopy(resolution),
    )
    session.add(release_action)
    metadata = {
        "message_id": run_id,
        "tenant_id": ORG,
        "persona": "operations",
        "source_ref": {"repo": "aws-e/adp", "installation_id": 42, "provider_repository_id": 123},
        "orchestration": {
            "node_id": node.id,
            "flow_id": node.flow_id,
            "attempt": 1,
            "executor_role": "operate",
            "deployment_revisions": pins,
            "release_binding": copy.deepcopy(resolution),
        },
        "handoff_expect": {
            "accepted_plan_version": 1,
            "execution_id": execution.id,
            "claim_id": execution.claim_id,
            "claim_generation": 1,
        },
    }
    report = await prepare_run_report(session, metadata)
    record_terminal(report, "complete")
    contract = SimpleNamespace(
        plan_version=1,
        node=SimpleNamespace(
            executor=SimpleNamespace(role="operate", persona="operations"),
            repository_identity="aws-e/adp",
            evidence_expectations=["runtime_verified:gateway"],
            required_inputs=[SimpleNamespace(source="deployment_workflow:deploy.yml")],
            operational_release_binding=SimpleNamespace(
                maintenance_adoption_id=None,
                repository="aws-e/adp",
                component="gateway",
                target="dev",
                default_branch="main",
                workflows=["deploy.yml"],
                predecessors=[SimpleNamespace(address=address)],
            ),
        ),
    )
    monkeypatch.setattr("src.orchestration.operations_repair.accepted_node_contract", AsyncMock(return_value=contract))
    monkeypatch.setattr("src.orchestration.operations_evidence.accepted_node_contract", AsyncMock(return_value=contract))
    monkeypatch.setattr("src.orchestration.accepted_node.accepted_node_contract", AsyncMock(return_value=contract))
    policy = SimpleNamespace(policy_hash="accepted-policy", repair_paths=["src/service.py"], repository_ids=["aws-e/adp"])
    monkeypatch.setattr(
        "src.orchestration.operations_repair.load_in_force_policy",
        AsyncMock(return_value=SimpleNamespace(policy=policy, plan_version=1, refusal=None)),
    )
    assignment = SimpleNamespace(
        tenant_id=ORG,
        node_id=node.id,
        accepted_plan_version=1,
        attempt=1,
        claim_generation=1,
        invocation_id=run_id,
        repository="aws-e/adp",
        default_branch="main",
    )
    authorized = SimpleNamespace(policy=policy, workflow_allowlist={"deploy.yml"})
    grant = SimpleNamespace(tenant_id=ORG, flow_id=node.flow_id, repo_scope={"aws-e/adp"}, principal="fictional-worker")
    decision = SimpleNamespace(permitted=True, action=Action.OPERATE, policy=policy, plan_version=1)
    protected = {
        "orchestration_node_id": {"S": node.id},
        "orchestration_node_attempt": {"N": "1"},
        "repo": {"S": "aws-e/adp"},
        "persona": {"S": "operations"},
        "invocation_id": {"S": run_id},
    }
    await session.commit()
    issued = await authorize_operations_grant(session, execution=protected, grant=grant, decision=decision)
    assert issued.workflow_allowlist == frozenset({"deploy.yml"})
    assert await session.scalar(select(AuditLog).where(AuditLog.event_type == "operations_grant_issued")) is not None
    await record_repair_merge(
        session,
        assignment,
        pull_number=1,
        head="1" * 40,
        merged={"merged": True, "sha": "e" * 40},
        authorized=authorized,
    )
    repaired = {**pins, "source_revision": "e" * 40, "definition_revision": "e" * 40}
    assert await repair_revisions(session, node_id=node.id, org_id=ORG, plan_version=1, cycle=1, pins=pins) == repaired
    assert await observed_operations_evidence(
        session,
        node,
        {"run_id": run_id, "attempt": 1, "conclusion_authority": "evidence", "repo": "aws-e/adp"},
        expected=(contract, ("gateway",)),
    ) == (None, "evidence_missing")
    repair = await session.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "operations_repair_merge"))
    assert (repair.detail["before_source_revision"], repair.detail["before_definition_revision"]) == (
        pins["source_revision"],
        pins["definition_revision"],
    )
    await session.commit()
    release_action.detail = {**resolution, "target": "other-environment"}
    assert await repair_revisions(session, node_id=node.id, org_id=ORG, plan_version=1, cycle=1, pins=pins) is None
    with pytest.raises(OperationRefusedError, match="operations release binding changed"):
        await authorize_operations_grant(session, execution=protected, grant=grant, decision=decision)
    with pytest.raises(OperationRefusedError, match="repair scope changed"):
        await record_repair_merge(
            session,
            assignment,
            pull_number=2,
            head="2" * 40,
            merged={"merged": True, "sha": "f" * 40},
            authorized=authorized,
        )
    assert (await session.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == "operations_repair_merge"))).all() == [repair]


@pytest.mark.parametrize("context_transport", ["github", "s3"])
@pytest.mark.parametrize(
    ("repaired_push", "bound_release", "product_selected", "proof_selected", "maintained_gateway", "foundation_identity"),
    [
        (False, False, False, False, False, None),
        (True, False, False, False, False, None),
        (False, True, False, False, False, None),
        (False, True, True, False, False, None),
        (False, True, "browser", False, False, None),
        (False, True, False, True, False, None),
        (False, True, False, False, True, None),
        (False, True, True, False, False, ("atlassian-adapters-8445-parallel", "w09-release")),
        (False, True, True, False, False, ("billing-service", "release-v2")),
    ],
)
async def test_no_pr_runtime_producer_reaches_evidence_conclusion(
    session,  # noqa: F811
    session_factory,  # noqa: F811
    monkeypatch,
    repaired_push,
    bound_release,
    product_selected,  # noqa: F811
    proof_selected,
    maintained_gateway,
    context_transport,
    foundation_identity,
):
    import hashlib
    from dataclasses import asdict, is_dataclass
    from dataclasses import replace as dataclass_replace

    from src.orchestration.deployment_manifest import EntryStatus, ManifestEntry, PhysicalTarget, TargetEvidence, WorkflowRef
    from src.orchestration.deployment_runtime_contract import ReleaseArtifact, RuntimeComponent
    from src.orchestration.deployment_workflow_provider import WorkflowContext, WorkflowDefinition, WorkflowProvider
    from src.orchestration.operations_runtime_evidence import verify_operations_runtime
    from src.orchestration.operations_workflow_evidence import record_correlated_workflow
    from src.orchestration.review_cycle import CycleBlockedError

    monkeypatch.setenv("ADP_ENGINE_OPERATIONS_EXECUTOR", "true")
    backend = product_selected is True or proof_selected or maintained_gateway
    component = "gateway-backend" if backend else "gateway-frontend"
    workflow_name = "gateway-deploy.yml" if backend else "gateway-frontend-deploy.yml" if product_selected else "deploy.yml"
    target_name = "adp-dev-gateway" if maintained_gateway else "gateway" if backend else "frontend"
    proof_phase = "stage.identity-proof" if proof_selected else "none"
    selected_cases = (
        "live_browser_create,live_browser_conflict_denial,live_browser_evidence"
        if product_selected == "browser"
        else "api_product_lifecycle,api_denial_oracle,api_recovery_cleanup"
    )
    node = await prepare(session)
    node.state, node.attempts = "running", 1
    node_id, flow_id = node.id, node.flow_id
    row = OrchestrationExecution(
        org_id=ORG,
        flow_id=node.flow_id,
        node_id=node.id,
        cycle=1,
        phase="delivering",
        status="active",
        accepted_plan_version=1,
        claim_id="accepted-claim",
        claim_generation=1,
    )
    session.add(row)
    await session.flush()
    run_id = attempt_run_id(node.id, 1)
    manifest_revisions = {"source_revision": "a" * 40, "definition_revision": "b" * 40, "default_branch": "main"}
    accepted_revisions = {
        **manifest_revisions,
        **({"source_revision": "e" * 40, "definition_revision": "d" * 40} if bound_release else {}),
    }
    revisions = {**accepted_revisions, **({"source_revision": "c" * 40, "definition_revision": "c" * 40} if repaired_push else {})}
    if repaired_push:
        for module in (
            "src.orchestration.operations_repair",
            "src.orchestration.operations_runtime_evidence",
        ):
            monkeypatch.setattr(f"{module}.repair_revisions", AsyncMock(return_value=revisions))
    metadata = {
        "message_id": run_id,
        "tenant_id": ORG,
        "persona": "operations",
        "source_ref": {"repo": "aws-e/adp", "installation_id": 42, "provider_repository_id": 123},
        "orchestration": {
            "node_id": node.id,
            "flow_id": node.flow_id,
            "attempt": 1,
            "executor_role": "operate",
            "deployment_revisions": accepted_revisions,
        },
        "handoff_expect": {"accepted_plan_version": 1, "execution_id": row.id, "claim_id": row.claim_id, "claim_generation": 1},
    }
    if bound_release:
        release = {
            "target": target_name,
            "connection_id": "fictional-connection",
            "resource_kind": "eks-namespace",
            "resource_id": "fictional-cluster/fictional-namespace",
            "manifest_artifact_revision": manifest_revisions["source_revision"],
            "manifest_definition_revision": manifest_revisions["definition_revision"],
            "workflow": f".github/workflows/{workflow_name}",
            "component": component,
            "repository": "aws-e/adp",
            "product_cases": selected_cases if product_selected else "none",
            "proof_phase": proof_phase,
            "accepted_plan_version": 1,
            "receipts": [{"address": "flow/epic/wave/build", "action_id": "merge-1", "merge_sha": accepted_revisions["source_revision"]}],
            "pins": accepted_revisions,
        }
        metadata["orchestration"]["release_binding"] = release
        session.add(
            OrchestrationAction(
                org_id=ORG,
                execution_id=row.id,
                operation_key="operations-release:1",
                kind="operations_release_binding",
                status="succeeded",
                attempt=1,
                detail=copy.deepcopy(release),
            )
        )
    report = await prepare_run_report(session, metadata)
    report_text = "Verified the gateway frontend revision at the reviewed target"
    record_terminal(report, "complete")
    report.operations_report_receipt = {
        "recorded": True,
        "run_id": run_id,
        "attempt": 1,
        "node_id": node.id,
        "receipt_id": f"operations-report:{run_id}",
        "report": report_text,
        "sha256": hashlib.sha256(report_text.encode()).hexdigest(),
    }
    contract = SimpleNamespace(
        plan_version=1,
        node=SimpleNamespace(
            executor=SimpleNamespace(role="operate", persona="operations"),
            evidence_expectations=[f"runtime_verified:{component}"],
            repository_identity="aws-e/adp",
            required_inputs=[
                SimpleNamespace(source=prefix + value)
                for prefix, value in (
                    ("deployment_workflow:", workflow_name),
                    *(
                        []
                        if bound_release
                        else [
                            ("deployment_source_revision:", accepted_revisions["source_revision"]),
                            ("deployment_definition_revision:", accepted_revisions["definition_revision"]),
                            ("deployment_branch:", "main"),
                        ]
                    ),
                )
            ],
            operational_release_binding=SimpleNamespace(
                repository="aws-e/adp",
                component=component,
                target=target_name,
                maintenance_adoption_id=None,
                default_branch="main",
                workflows=[workflow_name],
                product_cases=selected_cases if product_selected else "none",
                proof_phase=proof_phase,
                predecessors=[SimpleNamespace(address="flow/epic/wave/build")],
            )
            if bound_release
            else None,
        ),
    )
    monkeypatch.setattr("src.orchestration.operations_workflow_evidence.accepted_node_contract", AsyncMock(return_value=contract))
    monkeypatch.setattr("src.orchestration.operations_runtime_evidence.accepted_node_contract", AsyncMock(return_value=contract))
    monkeypatch.setattr("src.orchestration.operations_evidence.accepted_node_contract", AsyncMock(return_value=contract))
    raw_run = {"id": 42, "run_attempt": 1, "path": f".github/workflows/{workflow_name}", "event": "push" if repaired_push else "workflow_dispatch"}
    if bound_release and product_selected is True and not proof_selected and context_transport == "github":
        raw_run["head_sha"] = revisions["definition_revision"]
    context = WorkflowContext(
        schema_version=1,
        repository_id=123,
        run_id=42,
        run_attempt=1,
        workflow_path=raw_run["path"],
        workflow_revision=revisions["definition_revision"],
        source_revision=revisions["source_revision"],
        account_id="123456789012",
        region="us-east-1",
        resource_kind="eks-namespace",
        resource_id="fictional-cluster/fictional-namespace",
        inputs={
            "environment": "dev",
            **({"product_cases": selected_cases} if product_selected else {}),
            **({"proof_phase": proof_phase} if proof_selected else {}),
        },
        correlation="automatic-push-42" if repaired_push else run_id,
    )
    binding = SimpleNamespace(repo="aws-e/adp", org_id=ORG, provider_repository_id=123, installation_id=42)
    issued = SimpleNamespace(
        node_id=node.id,
        plan_version=1,
        workflow_allowlist={workflow_name},
        product_cases=selected_cases if product_selected else "none",
        proof_phase=proof_phase,
    )
    execution = {"invocation_id": {"S": run_id}, "persona": {"S": "operations"}, **{key: {"S": value} for key, value in revisions.items()}}
    context_artifact_id = 99 if context_transport == "github" else None
    context_reference = (
        f"s3://fictional-evidence/deployment-evidence/v1/123/42/1/context/{workflow_name}.json?versionId=original"
        if context_transport == "s3"
        else None
    )
    context_id = await record_correlated_workflow(
        session,
        execution=execution,
        binding=binding,
        issued=issued,
        raw_run=raw_run,
        context=context,
        artifact_id=context_artifact_id,
        reference=context_reference,
        digest="f" * 64,
    )
    assert context_id is not None
    dispatch = {"run_id": run_id, "attempt": 1, "conclusion_authority": "evidence", "repo": binding.repo}
    assert await observed_operations_evidence(session, node, dispatch) == (None, "evidence_missing")
    now = datetime.now(UTC)
    physical = PhysicalTarget(
        provider="aws",
        account_id=context.account_id,
        region=context.region,
        resource_kind=context.resource_kind,
        resource_id=context.resource_id,
        evidence=TargetEvidence(source="registered-aws-role:fictional-connection", verified_at=now.isoformat()),
    )
    entry = ManifestEntry(
        entry_id=target_name,
        status=EntryStatus.ENABLED,
        connection_id="fictional-connection",
        component_selectors=(component,),
        workflow=WorkflowRef(path=raw_run["path"], definition_revision=manifest_revisions["definition_revision"]),
        artifact_revision=manifest_revisions["source_revision"],
        verification_adapter="gateway-health-verification",
        resource_kind=context.resource_kind,
        resource_id=context.resource_id,
    )
    policy = SimpleNamespace(permits=lambda _action: True, expires_at=now + timedelta(hours=1))
    authority = AsyncMock(return_value=(None, None, SimpleNamespace(policy=policy), "fictional-user", None, object()))
    monkeypatch.setattr("src.orchestration.operations_runtime_evidence.ReviewCycleServices.authority_context", authority)
    monkeypatch.setattr("src.orchestration.operations_runtime_evidence.require_current_claim", AsyncMock())
    monkeypatch.setattr(
        "src.orchestration.operations_runtime_evidence.load_in_force_policy",
        AsyncMock(return_value=SimpleNamespace(refusal=None, plan_version=1, policy=policy)),
    )
    monkeypatch.setattr(
        "src.orchestration.operations_runtime_evidence.replace",
        lambda item, **updates: dataclass_replace(item, **updates) if is_dataclass(item) else item,
    )
    monkeypatch.setattr("src.orchestration.operations_runtime_evidence.authorize_action", lambda *_args, **_kwargs: SimpleNamespace(permitted=True))
    reference = "github/actions/runs/42/artifacts/88"
    artifact = ReleaseArtifact(
        schema_version=1,
        repository_id=123,
        run_id=42,
        run_attempt=1,
        source_revision=revisions["source_revision"],
        workflow_revision=revisions["definition_revision"],
        workflow_path=raw_run["path"],
        account_id=context.account_id,
        region=context.region,
        resource_id=context.resource_id,
        component=component,
        image_digest="sha256:" + "d" * 64 if backend else None,
        assets={} if backend else {"index.html": "f" * 64},
        produced_at=now,
        **(
            {
                "product_profile_enabled": True,
                "product_onboarding_enabled": True,
                "product_profile_schema_version": "1.0",
                "migration_head": "head",
                "cli_manifest_sha256": "c" * 64,
            }
            if maintained_gateway
            else {}
        ),
    )
    runtime_component = RuntimeComponent(
        component=component,
        actual_revision=revisions["source_revision"],
        artifact_hash="f" * 64,
        healthy=True,
        evidence_ref=reference,
        observed_at=now,
        asset_count=0 if backend else 1,
        image_digest="sha256:" + "d" * 64 if backend else None,
        tick_digest="sha256:" + "d" * 64 if backend else None,
        migration_head="head" if backend else None,
        pod_uids=["serving-pod"] if backend else [],
        **({"product_profile_enabled": True, "product_onboarding_enabled": True, "cli_manifest_sha256": "c" * 64} if maintained_gateway else {}),
    )
    targets = SimpleNamespace(
        resolve=AsyncMock(
            side_effect=[
                SimpleNamespace(physical=physical),
                SimpleNamespace(physical=physical, observation=runtime_component),
            ]
        )
    )
    run = SimpleNamespace(
        status="completed",
        conclusion="success",
        run_attempt=1,
        artifact_id=context_artifact_id,
        artifact_ref=context_reference,
        artifact_digest="f" * 64,
        context=context,
    )
    latest_head = AsyncMock(return_value=revisions["source_revision"])
    monkeypatch.setattr("src.orchestration.operations_runtime_evidence.current_push_head", latest_head)
    provider = SimpleNamespace(
        definition=AsyncMock(return_value=object()),
        observe=AsyncMock(return_value=(run, None)),
        release=AsyncMock(return_value=(artifact, "f" * 64, reference)),
    )
    if proof_selected:
        # Exercise the real workflow input comparison, not an observer stub that
        # would accept a proof run even when the verifier omitted its selector.
        raw_run.update(
            head_sha=revisions["definition_revision"],
            repository={"id": binding.provider_repository_id},
            display_title="ADP deployment " + run_id,
            status="completed",
            conclusion="success",
        )
        observer = WorkflowProvider()
        observer.request = AsyncMock(return_value=SimpleNamespace(json=lambda: raw_run))
        observer.context = AsyncMock(return_value=(context, context_artifact_id, "f" * 64))
        observer.s3_evidence = AsyncMock(return_value=(context.model_dump_json().encode(), "f" * 64, context_reference))
        provider.definition.return_value = WorkflowDefinition(
            approved_revision=revisions["definition_revision"],
            source_revision=revisions["source_revision"],
            blob_sha="a" * 40,
            defaults={"environment": "dev", "product_cases": "none", "proof_phase": "none"},
            dispatchable=True,
            dispatch_ref="main",
            dispatch_revision=revisions["definition_revision"],
            evidence_transport="s3-oidc-v1" if context_transport == "s3" else "github-artifact",
        )
        provider.observe.side_effect = observer.observe
    foundation_case = bound_release and product_selected is True and not proof_selected and context_transport == "github"
    if foundation_case:
        from src.orchestration.models import OrchestrationFlow

        context.inputs["release_group"] = "atlassian-foundation"
        stage_flow = await session.get(OrchestrationFlow, node.flow_id)
        stage_flow.slug, node.wave_ref = foundation_identity or ("atlassian-adapters-8445-parallel", "w03b-foundation-release")
        entry = dataclass_replace(
            entry,
            release_group="atlassian-foundation",
            mutation_role_arn="arn:aws:iam::000000000000:role/adp-dev-trusted-deployment",
            workflow=dataclass_replace(entry.workflow, allowed_inputs={"environment": frozenset({"dev"})}),
        )
        release = {**release, "mutation_role_arn": entry.mutation_role_arn}
        release_action = await session.scalar(
            select(OrchestrationAction).where(OrchestrationAction.execution_id == row.id, OrchestrationAction.operation_key == "operations-release:1")
        )
        release_action.detail = copy.deepcopy(release)
        metadata["orchestration"]["release_binding"] = copy.deepcopy(release)
        report.dispatch_metadata = copy.deepcopy(metadata)
        monkeypatch.setattr("src.orchestration.operations_evidence.load_packaged_manifest", lambda: SimpleNamespace(entry=lambda _: entry))
    await session.commit()
    if proof_selected:
        for changed in ("issued", "accepted", "unknown", "product"):
            issued.proof_phase = "none" if changed == "issued" else "other-phase" if changed == "unknown" else proof_phase
            contract.node.operational_release_binding.proof_phase = (
                "none" if changed == "accepted" else "other-phase" if changed == "unknown" else proof_phase
            )
            if changed == "product":
                issued.product_cases = selected_cases
                contract.node.operational_release_binding.product_cases = selected_cases
            with pytest.raises(CycleBlockedError, match="operations_runtime_proof_selection_changed"):
                await verify_operations_runtime(
                    session_factory,
                    binding=binding,
                    issued=issued,
                    execution=execution,
                    raw_run=raw_run,
                    context_action_id=context_id,
                    reauthorize=AsyncMock(),
                    provider=provider,
                    targets=targets,
                    manifest_loader=lambda: SimpleNamespace(entries=[entry]),
                )
            assert provider.observe.await_count == 0
        issued.proof_phase = contract.node.operational_release_binding.proof_phase = proof_phase
        issued.product_cases = contract.node.operational_release_binding.product_cases = "none"
        for recorded in (
            {"environment": "dev"},
            {"environment": "dev", "proof_phase": "none"},
            {"environment": "dev", "proof_phase": "other-phase"},
            {**context.inputs, "unapproved_input": "true"},
            {**context.inputs, "environment": "prod"},
        ):
            changed_context = context.model_copy(update={"inputs": recorded})
            observer.context.return_value = changed_context, context_artifact_id, "f" * 64
            observer.s3_evidence.return_value = changed_context.model_dump_json().encode(), "f" * 64, context_reference
            targets.resolve.side_effect = [SimpleNamespace(physical=physical)]
            with pytest.raises(CycleBlockedError, match="deployment_run_inputs_mismatch"):
                await verify_operations_runtime(
                    session_factory,
                    binding=binding,
                    issued=issued,
                    execution=execution,
                    raw_run=raw_run,
                    context_action_id=context_id,
                    reauthorize=AsyncMock(),
                    provider=provider,
                    targets=targets,
                    manifest_loader=lambda: SimpleNamespace(entries=[entry]),
                )
            assert provider.release.await_count == 0
        observer.context.return_value = context, context_artifact_id, "f" * 64
        observer.s3_evidence.return_value = context.model_dump_json().encode(), "f" * 64, context_reference
        targets.resolve.reset_mock()
        targets.resolve.side_effect = [
            SimpleNamespace(physical=physical),
            SimpleNamespace(physical=physical, observation=runtime_component),
        ]
        provider.observe.reset_mock()
        authority.reset_mock()
    if product_selected:
        for change in ("issued", "accepted"):
            if change == "issued":
                issued.product_cases = "none"
            else:
                contract.node.operational_release_binding.product_cases = "none"
            with pytest.raises(CycleBlockedError, match="operations_runtime_product_selection_changed"):
                await verify_operations_runtime(
                    session_factory,
                    binding=binding,
                    issued=issued,
                    execution=execution,
                    raw_run=raw_run,
                    context_action_id=context_id,
                    reauthorize=AsyncMock(),
                    provider=provider,
                    targets=targets,
                    runtime=SimpleNamespace(inspect=lambda *_args, **_kwargs: runtime_component),
                    manifest_loader=lambda: SimpleNamespace(entries=[entry]),
                )
            assert provider.observe.await_count == 0
            issued.product_cases = selected_cases
            contract.node.operational_release_binding.product_cases = selected_cases
        authority.reset_mock()
        run.context = context.model_copy(update={"inputs": {"environment": "dev", "product_cases": "none"}})
        with pytest.raises(CycleBlockedError, match="operations_runtime_run_changed"):
            await verify_operations_runtime(
                session_factory,
                binding=binding,
                issued=issued,
                execution=execution,
                raw_run=raw_run,
                context_action_id=context_id,
                reauthorize=AsyncMock(),
                provider=provider,
                targets=targets,
                runtime=SimpleNamespace(inspect=lambda *_args, **_kwargs: runtime_component),
                manifest_loader=lambda: SimpleNamespace(entries=[entry]),
            )
        run.context = context
        targets.resolve.reset_mock()
        targets.resolve.side_effect = [
            SimpleNamespace(physical=physical),
            SimpleNamespace(physical=physical, observation=runtime_component),
        ]
        provider.observe.reset_mock()
        authority.reset_mock()
    if product_selected == "browser":
        # A signed frontend publication alone cannot complete the node while
        # the child browser checks run, or when verification/upload fails.
        for status, conclusion in (("in_progress", None), ("completed", "failure"), ("completed", "cancelled")):
            run.status, run.conclusion = status, conclusion
            with pytest.raises(CycleBlockedError, match="operations_runtime_run_changed"):
                await verify_operations_runtime(
                    session_factory,
                    binding=binding,
                    issued=issued,
                    execution=execution,
                    raw_run=raw_run,
                    context_action_id=context_id,
                    reauthorize=AsyncMock(),
                    provider=provider,
                    targets=targets,
                    runtime=SimpleNamespace(inspect=lambda *_args, **_kwargs: runtime_component),
                    manifest_loader=lambda: SimpleNamespace(entries=[entry]),
                )
            assert provider.release.await_count == 0
            targets.resolve.reset_mock()
            targets.resolve.side_effect = [
                SimpleNamespace(physical=physical),
                SimpleNamespace(physical=physical, observation=runtime_component),
            ]
            authority.reset_mock()
        run.status, run.conclusion = "completed", "success"
    if foundation_case:
        from src.orchestration.environment_leases import LeaseHolder, acquire_lease, shared_release_target

        targets.resolve.side_effect = [
            SimpleNamespace(physical=physical),
            SimpleNamespace(physical=physical, observation=runtime_component),
        ]
        with pytest.raises(CycleBlockedError, match="operations_shared_release_lease_missing"):
            await verify_operations_runtime(
                session_factory,
                binding=binding,
                issued=issued,
                execution=execution,
                raw_run=raw_run,
                context_action_id=context_id,
                reauthorize=AsyncMock(),
                provider=provider,
                targets=targets,
                runtime=SimpleNamespace(inspect=lambda *_args, **_kwargs: runtime_component),
                manifest_loader=lambda: SimpleNamespace(entries=[entry]),
            )
        authority.reset_mock()
        targets.resolve.reset_mock()
        latest_head.reset_mock()
        for method in (provider.definition, provider.observe, provider.release):
            method.reset_mock()
        targets.resolve.side_effect = [
            SimpleNamespace(physical=physical),
            SimpleNamespace(physical=physical, observation=runtime_component),
        ]
        group = shared_release_target(physical, environment="dev", group="atlassian-foundation")
        async with session_factory() as lease_session:
            acquired = await acquire_lease(
                lease_session,
                target=group,
                holder=LeaseHolder(ORG, f"foundation:{node.id}:gateway-backend:{revisions['source_revision']}", 1),
                manifest_entry_id=entry.entry_id,
                release_ref=revisions["source_revision"],
            )
            assert acquired.applied
            await lease_session.commit()
        raw_run["created_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        with pytest.raises(CycleBlockedError, match="operations_shared_release_lease_missing"):
            await verify_operations_runtime(
                session_factory,
                binding=binding,
                issued=issued,
                execution=execution,
                raw_run=raw_run,
                context_action_id=context_id,
                reauthorize=AsyncMock(),
                provider=provider,
                targets=targets,
                runtime=SimpleNamespace(inspect=lambda *_args, **_kwargs: runtime_component),
                manifest_loader=lambda: SimpleNamespace(entries=[entry]),
            )
        authority.reset_mock()
        targets.resolve.reset_mock()
        targets.resolve.side_effect = [
            SimpleNamespace(physical=physical),
            SimpleNamespace(physical=physical, observation=runtime_component),
        ]
        raw_run["created_at"] = datetime.now(UTC).isoformat()
        runtime_component = runtime_component.model_copy(update={"observed_at": datetime.now(UTC)})
        with pytest.raises(CycleBlockedError, match="operations_shared_release_run_unpinned"):
            await verify_operations_runtime(
                session_factory,
                binding=binding,
                issued=issued,
                execution=execution,
                raw_run=raw_run,
                context_action_id=context_id,
                reauthorize=AsyncMock(),
                provider=provider,
                targets=targets,
                runtime=SimpleNamespace(inspect=lambda *_args, **_kwargs: runtime_component),
                manifest_loader=lambda: SimpleNamespace(entries=[entry]),
            )
        targets.resolve.reset_mock()
        targets.resolve.side_effect = [
            SimpleNamespace(physical=physical),
            SimpleNamespace(physical=physical, observation=runtime_component),
        ]
        reserved_target = asdict(physical)
        reserved_target["evidence"]["verified_at"] = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
        native = OrchestrationAction(
            org_id=ORG,
            execution_id=row.id,
            operation_key="operations-native-release:fixture",
            kind="operations_native_release_dispatch",
            status="prepared",
            attempt=1,
            detail={
                "correlation": run_id,
                "repo": binding.repo,
                "provider_repository_id": binding.provider_repository_id,
                "workflow": workflow_name,
                "source_revision": revisions["source_revision"],
                "dispatch_revision": raw_run["head_sha"],
                "dispatch_started": True,
                "release_job": {"run_id": "43", "run_attempt": "1"},
                "shared_release": {
                    "owner": f"foundation:{node.id}:{component}:{revisions['source_revision']}",
                    "target_key": group.canonical_key,
                    "target": reserved_target,
                    "manifest_entry_id": entry.entry_id,
                    "mutation_role_arn": entry.mutation_role_arn,
                    "generation": 1,
                    "environment": "dev",
                },
            },
        )
        session.add(native)
        await session.commit()
        with pytest.raises(CycleBlockedError, match="operations_shared_release_run_unpinned"):
            await verify_operations_runtime(
                session_factory,
                binding=binding,
                issued=issued,
                execution=execution,
                raw_run=raw_run,
                context_action_id=context_id,
                reauthorize=AsyncMock(),
                provider=provider,
                targets=targets,
                runtime=SimpleNamespace(inspect=lambda *_args, **_kwargs: runtime_component),
                manifest_loader=lambda: SimpleNamespace(entries=[entry]),
            )
        native.detail = {**native.detail, "release_job": {"run_id": str(raw_run["id"]), "run_attempt": "1"}}
        await session.commit()
        authority.reset_mock()
        targets.resolve.reset_mock()
        targets.resolve.side_effect = [
            SimpleNamespace(physical=physical),
            SimpleNamespace(physical=physical, observation=runtime_component),
        ]
    receipt_id = await verify_operations_runtime(
        session_factory,
        binding=binding,
        issued=issued,
        execution=execution,
        raw_run=raw_run,
        context_action_id=context_id,
        reauthorize=AsyncMock(),
        provider=provider,
        targets=targets,
        runtime=SimpleNamespace(inspect=lambda *_args, **_kwargs: runtime_component),
        manifest_loader=lambda: SimpleNamespace(entries=[entry]),
    )
    assert receipt_id is not None
    if product_selected:
        assert provider.observe.await_args.kwargs["inputs"] == {
            "product_cases": selected_cases,
            **({"release_group": "atlassian-foundation"} if foundation_case else {}),
        }
    if proof_selected:
        assert provider.observe.await_args.kwargs["inputs"] == {"proof_phase": proof_phase}
    targets.resolve.side_effect = [
        SimpleNamespace(physical=physical),
        SimpleNamespace(physical=physical, observation=runtime_component),
    ]
    assert (
        await verify_operations_runtime(
            session_factory,
            binding=binding,
            issued=issued,
            execution=execution,
            raw_run=raw_run,
            context_action_id=context_id,
            reauthorize=AsyncMock(),
            provider=provider,
            targets=targets,
            runtime=SimpleNamespace(inspect=lambda *_args, **_kwargs: runtime_component),
            manifest_loader=lambda: SimpleNamespace(entries=[entry]),
        )
        == receipt_id
    )
    await session.rollback()
    if foundation_case:
        native = await session.scalar(
            select(OrchestrationAction).where(
                OrchestrationAction.execution_id == row.id,
                OrchestrationAction.kind == "operations_native_release_dispatch",
            )
        )
        original_job = native.detail["release_job"]
        native.detail = {**native.detail, "release_job": {"run_id": "43", "run_attempt": "1"}}
        await session.commit()
        assert await observed_operations_evidence(session, node, dispatch) == (None, "evidence_missing")
        native.detail = {**native.detail, "release_job": original_job}
        await session.commit()
        assert await observed_operations_evidence(session, node, dispatch) == (run_id, "")
        drifted = dataclass_replace(entry, mutation_role_arn="arn:aws:iam::000000000000:role/adp-other-trusted-deployment")
        monkeypatch.setattr("src.orchestration.operations_evidence.load_packaged_manifest", lambda: SimpleNamespace(entry=lambda _: drifted))
        assert await observed_operations_evidence(session, node, dispatch) == (None, "evidence_missing")
        monkeypatch.setattr("src.orchestration.operations_evidence.load_packaged_manifest", lambda: SimpleNamespace(entry=lambda _: entry))
        native.detail = {**native.detail, "shared_release": {**native.detail["shared_release"], "mutation_role_arn": None}}
        await session.commit()
        assert await observed_operations_evidence(session, node, dispatch) == (None, "evidence_missing")
        native.detail = {**native.detail, "shared_release": {**native.detail["shared_release"], "mutation_role_arn": entry.mutation_role_arn}}
        await session.commit()
        original_release = native.detail["shared_release"]
        native.detail = {
            **native.detail,
            "shared_release": {
                **original_release,
                "target": {**reserved_target, "resource_id": "fictional-cluster/other"},
            },
        }
        await session.commit()
        assert await observed_operations_evidence(session, node, dispatch) == (None, "evidence_missing")
        native.detail = {**native.detail, "shared_release": original_release}
        await session.commit()
        lease = await session.scalar(
            select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.canonical_target_key == group.canonical_key)
        )
        generation = lease.owner_generation
        lease.owner_generation = generation + 1
        await session.commit()
        assert await observed_operations_evidence(session, node, dispatch) == (None, "evidence_missing")
        lease.owner_generation = generation
        await session.commit()
        expiry = lease.lease_expires_at
        lease.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
        assert await observed_operations_evidence(session, node, dispatch) == (None, "evidence_missing")
        lease.lease_expires_at = expiry
        await session.commit()
    observation = {}
    source = SimpleNamespace(merged_story=AsyncMock(side_effect=AssertionError("merge cannot prove runtime")))
    assert await _story_evidence(
        session,
        node=node,
        dispatch=dispatch,
        source=source,
        installation_id=42,
        observation=observation,
    ) == (run_id, "")
    source.merged_story.assert_not_awaited()
    assert str(receipt_id) in observation["evidence_ids"]
    assert str(context_id) in observation["evidence_ids"]
    assert authority.await_count == 2
    assert targets.resolve.await_count == 4

    async def settle_exact_evidence():
        await session.rollback()
        session.add(
            OrchestrationDecision(
                org_id=ORG,
                flow_id=flow_id,
                node_id=node_id,
                kind="node_dispatched",
                actor_id="engine",
                actor_role="engine",
                actor_kind="service",
                reason=json.dumps({**dispatch, "arrived_at": datetime.now(UTC).isoformat(), "issue": 4196}),
            )
        )
        await session.commit()
        runtime_action = await session.get(OrchestrationAction, receipt_id)
        workflow_action = await session.get(OrchestrationAction, context_id)
        original_runtime = copy.deepcopy(runtime_action.detail)
        original_workflow = copy.deepcopy(workflow_action.detail)
        changes = [
            (
                runtime_action,
                "deployment_receipt",
                {
                    "actual_revision": "f" * 40,
                    "components": [{**original_runtime["deployment_receipt"]["components"][0], "actual_revision": "f" * 40}],
                },
            ),
            (runtime_action, "deployment_receipt", {"workflow_operation_keys": ["operations-workflow:42:2"]}),
            (workflow_action, "correlated_workflow_receipt", {"run_attempt": 2}),
            (workflow_action, "correlated_workflow_receipt", {"definition_revision": "f" * 40}),
            (
                workflow_action,
                "correlated_workflow_receipt",
                {"context_reference": context_reference.replace("versionId=original", "versionId=other")}
                if context_transport == "s3"
                else {"context_artifact_id": 100},
            ),
        ]
        if bound_release:
            changes.append(
                (
                    runtime_action,
                    "deployment_receipt",
                    {"targets": [{**original_runtime["deployment_receipt"]["targets"][0], "resource_id": "other-target"}]},
                )
            )
        for action, receipt_name, changed in changes:
            original = original_runtime if action.id == receipt_id else original_workflow
            action.detail = {**original, receipt_name: {**original[receipt_name], **changed}}
            await session.commit()
            result = await observe_results(session)
            assert result.reasons[node_id] == "evidence_missing"
            assert (await session.get(OrchestrationNode, node_id)).state == "awaiting_merge"
            assert not (
                await session.scalars(
                    select(OrchestrationDecision).where(
                        OrchestrationDecision.node_id == node_id,
                        OrchestrationDecision.to_state == "passed",
                    )
                )
            ).all()
            action.detail = original
            await session.commit()
        original_reference = workflow_action.receipt_ref
        workflow_action.receipt_ref = "github/actions/runs/42/artifacts/other"
        await session.commit()
        assert (await observe_results(session)).reasons[node_id] == "evidence_missing"
        assert (await session.get(OrchestrationNode, node_id)).state == "awaiting_merge"
        workflow_action.receipt_ref = original_reference
        await session.commit()
        runtime_workflow = await session.scalar(
            select(OrchestrationAction).where(
                OrchestrationAction.execution_id == execution_id,
                OrchestrationAction.operation_key == "operations-workflow:42:1",
            )
        )
        workflow_detail = copy.deepcopy(runtime_workflow.detail)
        runtime_workflow.detail = {**workflow_detail, "context_action_id": "other-attempt"}
        await session.commit()
        assert (await observe_results(session)).reasons[node_id] == "evidence_missing"
        assert (await session.get(OrchestrationNode, node_id)).state == "awaiting_merge"
        runtime_workflow.detail = workflow_detail
        await session.commit()
        await session.refresh(node)
        assert await observed_operations_evidence(session, node, {**dispatch, "attempt": 2}) == (None, "operations_report_missing")
        if foundation_case:
            from src.orchestration.execution_policy import ExecutionPolicy, PolicyLimits
            from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationWorkClaim

            # Use the governed completion path, with a real released worker claim.
            # Legacy completion skips ledger settlement and hid the prepared native
            # dispatch that strands an otherwise verified deployment.
            completion_policy = ExecutionPolicy(
                schema_version=1,
                org_id=ORG,
                principal_id="fictional-user",
                policy_id="fixture-policy",
                policy_hash="b" * 64,
                repository_ids=[binding.repo],
                allowed_actions=["operate"],
                expires_at=now + timedelta(hours=1),
                limits=PolicyLimits(max_wall_clock_seconds=3600, max_spend_usd=25, max_attempts_per_node=3, max_concurrent_actions=1),
            )
            session.add(
                OrchestrationAcceptedPlan(
                    org_id=ORG,
                    flow_id=node.flow_id,
                    version=1,
                    plan_hash="b" * 64,
                    plan_document={"execution_policy": completion_policy.model_dump(mode="json")},
                )
            )
            session.add(
                OrchestrationWorkClaim(
                    id=row.claim_id,
                    org_id=ORG,
                    provider_repository_id=123,
                    issue_number=4196,
                    owner_kind="engine_flow",
                    owner_ref=node.flow_id,
                    state="released",
                    generation=row.claim_generation,
                    active_run_id=None,
                    claim_event_id=run_id,
                    release_reason="completed",
                )
            )

            row.phase, row.status = "admitted", "awaiting_external"
            await session.commit()
        await observe_results(session)
        assert (await session.get(OrchestrationNode, node_id)).state == "passed"
        passed = await session.scalar(
            select(OrchestrationDecision).where(
                OrchestrationDecision.node_id == node_id,
                OrchestrationDecision.to_state == "passed",
            )
        )
        assert str(receipt_id) in json.loads(passed.reason)["evidence_ids"]
        assert str(context_id) in json.loads(passed.reason)["evidence_ids"]
        if foundation_case:
            await session.refresh(native)
            await session.refresh(row)
            await session.refresh(lease)
            assert row.phase == row.status == "concluded"
            assert native.status == "succeeded" and native.receipt_ref == receipt_id
            assert lease.state == "free" and lease.release_reason == "completed"
            assert lease.reconciled_terminal_evidence == receipt_id
            assert json.loads(passed.reason)["native_release_action_id"] == native.id

    execution_id = row.id
    if not repaired_push:
        await session.rollback()
        assert (
            await verify_operations_runtime(
                session_factory,
                binding=binding,
                issued=issued,
                execution=execution,
                raw_run={**raw_run, "event": "push"},
                context_action_id=context_id,
                reauthorize=AsyncMock(),
                provider=provider,
                targets=targets,
                runtime=SimpleNamespace(inspect=lambda *_args, **_kwargs: runtime_component),
                manifest_loader=lambda: SimpleNamespace(entries=[entry]),
            )
            is None
        )
    if repaired_push:
        assert latest_head.await_count == 4
        assert provider.definition.await_args.args[2] == revisions["source_revision"]
        assert provider.observe.await_args.kwargs["workflow"].definition_revision == revisions["definition_revision"]
        assert provider.observe.await_args.kwargs["workflow"].path == entry.workflow.path
        assert targets.resolve.await_args.kwargs["entry"].connection_id == entry.connection_id
        assert targets.resolve.await_args.kwargs["entry"].resource_id == entry.resource_id
        await session.rollback()
        latest_head.return_value = "d" * 40
        with pytest.raises(CycleBlockedError, match="operations_runtime_push_revision_changed"):
            await verify_operations_runtime(
                session_factory,
                binding=binding,
                issued=issued,
                execution=execution,
                raw_run=raw_run,
                context_action_id=context_id,
                reauthorize=AsyncMock(),
                provider=provider,
                targets=targets,
                runtime=SimpleNamespace(inspect=lambda *_args, **_kwargs: runtime_component),
                manifest_loader=lambda: SimpleNamespace(entries=[entry]),
            )
        latest_head.return_value = revisions["source_revision"]
        run.run_attempt = 3
        targets.resolve.side_effect = [SimpleNamespace(physical=physical)]
        with pytest.raises(CycleBlockedError, match="operations_runtime_run_changed"):
            await verify_operations_runtime(
                session_factory,
                binding=binding,
                issued=issued,
                execution=execution,
                raw_run=raw_run,
                context_action_id=context_id,
                reauthorize=AsyncMock(),
                provider=provider,
                targets=targets,
                runtime=SimpleNamespace(inspect=lambda *_args, **_kwargs: runtime_component),
                manifest_loader=lambda: SimpleNamespace(entries=[entry]),
            )
        run.run_attempt = 1
        run.context = context.model_copy(update={"resource_id": "different-target"})
        targets.resolve.side_effect = [SimpleNamespace(physical=physical)]
        with pytest.raises(CycleBlockedError, match="operations_runtime_run_changed"):
            await verify_operations_runtime(
                session_factory,
                binding=binding,
                issued=issued,
                execution=execution,
                raw_run=raw_run,
                context_action_id=context_id,
                reauthorize=AsyncMock(),
                provider=provider,
                targets=targets,
                runtime=SimpleNamespace(inspect=lambda *_args, **_kwargs: runtime_component),
                manifest_loader=lambda: SimpleNamespace(entries=[entry]),
            )
        await settle_exact_evidence()
        return

    if bound_release:
        provider_calls = provider.definition.await_count
        target_calls = targets.resolve.await_count
        for change in (
            {"entry_id": "other-environment"},
            {"connection_id": "other-connection"},
            {"resource_id": "other-cluster/namespace"},
            {"artifact_revision": "f" * 40},
            {"workflow": dataclass_replace(entry.workflow, definition_revision="f" * 40)},
        ):
            await session.rollback()
            with pytest.raises(CycleBlockedError, match="operations_runtime_manifest_unavailable"):
                await verify_operations_runtime(
                    session_factory,
                    binding=binding,
                    issued=issued,
                    execution=execution,
                    raw_run=raw_run,
                    context_action_id=context_id,
                    reauthorize=AsyncMock(),
                    provider=provider,
                    targets=targets,
                    runtime=SimpleNamespace(inspect=lambda *_args, **_kwargs: runtime_component),
                    manifest_loader=lambda change=change: SimpleNamespace(entries=[dataclass_replace(entry, **change)]),
                )
            assert provider.definition.await_count == provider_calls
            assert targets.resolve.await_count == target_calls
        await settle_exact_evidence()
        return

    async def merge_during_verification():
        async with session_factory() as merge_session:
            merge_session.add(
                OrchestrationAction(
                    org_id=ORG,
                    execution_id=execution_id,
                    operation_key="operations-repair:99:" + "c" * 40,
                    kind="operations_repair_merge",
                    status="succeeded",
                    attempt=1,
                    detail={"merge_sha": "c" * 40},
                )
            )
            await merge_session.commit()

    await session.rollback()
    targets.resolve.side_effect = [
        SimpleNamespace(physical=physical),
        SimpleNamespace(physical=physical, observation=runtime_component),
    ]
    with pytest.raises(CycleBlockedError, match="operations_runtime_authority_changed"):
        await verify_operations_runtime(
            session_factory,
            binding=binding,
            issued=issued,
            execution=execution,
            raw_run=raw_run,
            context_action_id=context_id,
            reauthorize=merge_during_verification,
            provider=provider,
            targets=targets,
            runtime=SimpleNamespace(inspect=lambda *_args, **_kwargs: runtime_component),
            manifest_loader=lambda: SimpleNamespace(entries=[entry]),
        )
    await session.rollback()
    injected_repair = await session.scalar(
        select(OrchestrationAction).where(
            OrchestrationAction.execution_id == execution_id,
            OrchestrationAction.operation_key == "operations-repair:99:" + "c" * 40,
        )
    )
    await session.delete(injected_repair)
    await session.commit()
    await settle_exact_evidence()


def test_context_receipt_ref_names_artifact_or_s3_object():
    from src.orchestration.operations_workflow_evidence import CorrelatedWorkflowReceipt, context_receipt_ref

    assert context_receipt_ref(42, 99, None) == "github/actions/runs/42/artifacts/99"
    assert context_receipt_ref(42, None, "s3://bucket/key?versionId=v1") == "s3://bucket/key?versionId=v1"
    with pytest.raises(ValueError):
        context_receipt_ref(42, None, None)
    fields = CorrelatedWorkflowReceipt.model_fields
    assert fields["context_artifact_id"].default is None and fields["context_reference"].default is None


# ---------------------------------------------------------------------------
# #7821: an amendment that left the story untouched keeps its completed assignment
# ---------------------------------------------------------------------------


async def _fenced_run_under_two_plans(session, *, node_changed_by_amendment):  # noqa: F811
    """A completed operations run fenced to plan v1 while v2 (a human amendment) is in force."""
    from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationFlow
    from tests.orchestration.test_dispatch_pass import APPROVER

    node = await prepare(session)
    node.state, node.attempts = "running", 1
    flow = await session.get(OrchestrationFlow, node.flow_id)
    address = f"{flow.slug}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}"
    inputs = [
        {"name": "workflow", "source": "deployment_workflow:deploy.yml"},
        {"name": "source", "source": "deployment_source_revision:" + "a" * 40},
        {"name": "definition", "source": "deployment_definition_revision:" + "b" * 40},
        {"name": "branch", "source": "deployment_branch:main"},
    ]
    doc = {
        "address": address,
        "kind": "story",
        "issue_ref": node.issue_ref,
        "title": node.title,
        "executor": {"kind": "agent", "role": "operate", "persona": "operations", "schema_version": 1},
        "evidence_expectations": ["runtime_verified:tools-facade"],
        "criteria": ["c1"],
        "required_inputs": inputs,
    }
    amended = json.loads(json.dumps(doc))
    if node_changed_by_amendment:
        amended["required_inputs"] = inputs + [{"name": "extra", "source": "node:" + flow.slug + "/other/gate"}]
    else:
        amended["title"] = "Retitled by the amendment (not part of the execution definition)"
    v1 = OrchestrationAcceptedPlan(
        org_id=ORG, flow_id=flow.id, version=1, plan_document={"nodes": [doc], "edges": []}, plan_hash="h1", superseded_at=datetime.now(UTC)
    )
    amendment = OrchestrationDecision(
        org_id=ORG,
        flow_id=flow.id,
        kind="plan_amended",
        actor_id=APPROVER,
        actor_role="org_admin",
        actor_kind="human",
        reason="dropped another story",
    )
    session.add_all([v1, amendment])
    await session.flush()
    v2 = OrchestrationAcceptedPlan(
        org_id=ORG,
        flow_id=flow.id,
        version=2,
        plan_document={"nodes": [amended], "edges": []},
        plan_hash="h2",
        accepted_by_decision_id=amendment.id,
    )
    execution = OrchestrationExecution(
        org_id=ORG,
        flow_id=flow.id,
        node_id=node.id,
        cycle=1,
        phase="admitted",
        status="runnable",
        accepted_plan_version=1,
        claim_id="accepted-claim",
        claim_generation=1,
    )
    session.add_all([v2, execution])
    await session.flush()
    run_id = attempt_run_id(node.id, 1)
    metadata = {
        "message_id": run_id,
        "tenant_id": ORG,
        "persona": "operations",
        "source_ref": {"repo": "aws-e/adp", "installation_id": 42, "provider_repository_id": 123},
        "orchestration": {
            "node_id": node.id,
            "flow_id": flow.id,
            "attempt": 1,
            "executor_role": "operate",
            "deployment_revisions": {"source_revision": "a" * 40, "definition_revision": "b" * 40, "default_branch": "main"},
        },
        "handoff_expect": {"accepted_plan_version": 1, "execution_id": execution.id, "claim_id": "accepted-claim", "claim_generation": 1},
    }
    report = await prepare_run_report(session, metadata)
    report.terminal_receipt = {"run_id": run_id, "attempt": 1, "outcome": "complete", "contract_version": 1}
    await session.flush()
    contract = SimpleNamespace(
        node=SimpleNamespace(
            operational_release_binding=None,
            executor=SimpleNamespace(role="operate", persona="operations"),
            evidence_expectations=["runtime_verified:tools-facade"],
            repository_identity=None,
            required_inputs=[SimpleNamespace(source=item["source"]) for item in amended["required_inputs"]],
        ),
        plan_version=2,
    )
    dispatch = {"run_id": run_id, "attempt": 1, "repo": "aws-e/adp", "conclusion_authority": "evidence", "issue": 4196}
    return node, dispatch, (contract, ("tools-facade",))


async def test_an_amendment_that_leaves_the_story_untouched_keeps_its_completed_assignment(session, monkeypatch):  # noqa: F811
    """adp-tools-platform X05b, 2026-10-15: worker complete 15:39Z, unrelated amendment
    v47→v48 at 15:59:00Z, results pass at 15:59:22Z → parked forever under
    `operations_assignment_changed`. Lineage proves the node survived, so evaluation
    proceeds under the fenced version (and holds on the *next* real gap)."""
    monkeypatch.setenv("ADP_ENGINE_OPERATIONS_EXECUTOR", "true")
    node, dispatch, expected = await _fenced_run_under_two_plans(session, node_changed_by_amendment=False)

    result, hold = await observed_operations_evidence(session, node, dispatch, expected=expected)

    assert result is None and hold == "evidence_missing"  # no receipts yet; the assignment itself is intact


async def test_an_amendment_that_changed_the_story_still_invalidates_its_assignment(session, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ADP_ENGINE_OPERATIONS_EXECUTOR", "true")
    node, dispatch, expected = await _fenced_run_under_two_plans(session, node_changed_by_amendment=True)

    result, hold = await observed_operations_evidence(session, node, dispatch, expected=expected)

    assert result is None and hold == "operations_assignment_changed"
