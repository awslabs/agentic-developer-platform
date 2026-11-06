"""An operations predecessor without a pull request sources the evaluation from its deployment receipt (#7880).

Production shape (adp-tools-platform, 2026-10-16): the L2 live evaluation's two predecessors are
operate-role stories that deployed and were runtime-verified but opened no PR. ``sources_for``
required an active PR binding for every story predecessor, so the evaluation refused every tick
with ``predecessor_binding_missing_or_changed`` after the owner approved its gate.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.orchestration.deployment_runtime_contract import DeploymentReceipt, RuntimeComponent
from src.orchestration.dispatch import graph_address
from src.orchestration.evaluation_contract import specification
from src.orchestration.execution_state import ExecutionIdentity, OutcomeKind
from src.orchestration.execution_store import create_execution
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationAction, OrchestrationEdge, OrchestrationWorkClaim
from src.orchestration.repository_evaluation import sources_for
from src.orchestration.repository_evaluation_contract import RepositoryEvaluationSpecification, harness_digest
from src.orchestration.review_cycle import CycleBlockedError
from tests.orchestration.test_dispatch_pass import _make_flow, _make_node, _make_org
from tests.orchestration.test_dispatch_pass import engine as engine_fixture
from tests.orchestration.test_dispatch_pass import session as session_fixture
from tests.orchestration.test_dispatch_pass import session_factory as session_factory_fixture

engine = engine_fixture
session_factory = session_factory_fixture
session = session_fixture
REVISION = "d" * 40
REPO = "o/r"


def _receipt(*, org_id, flow_id, node_id, execution_id, claim_id, operation_key, revision=REVISION):
    now = datetime.now(UTC)
    return DeploymentReceipt(
        org_id=org_id,
        execution_id=execution_id,
        node_id=node_id,
        flow_id=flow_id,
        cycle=1,
        accepted_plan_version=1,
        claim_id=claim_id,
        claim_generation=1,
        operation_key=operation_key,
        repo=REPO,
        source_revision=revision,
        actual_revision=revision,
        merge_operation_key=f"operations-source:{revision}",
        workflow_operation_keys=["operations-workflow:1:1"],
        manifest_entry_ids=["entry-door"],
        targets=[{"kind": "eks-namespace", "resource_id": "cluster/ns"}],
        components=[
            RuntimeComponent(
                component="knowledge-door",
                actual_revision=revision,
                artifact_hash="a" * 64,
                image_digest="sha256:" + "b" * 64,
                pod_uids=["pod-1"],
                door_identity_sha256="c" * 64,
                door_probe_path="cluster_network",
                healthy=True,
                evidence_ref="s3://evidence/door",
                observed_at=now,
            )
        ],
        delivery_complete=True,
        observed_at=now,
        valid_until=now + timedelta(minutes=10),
    )


async def _story_graph(session, *, role="operate", multi_repository=False):
    await _make_org(session)
    flow = await _make_flow(session)
    story = await _make_node(session, flow, node_ref="door", state="passed", issue_ref="7289")
    story.attempts = 1
    evaluation = await _make_node(session, flow, node_ref="live-evaluation", state="ready", kind="eval", issue_ref="7276")
    session.add(OrchestrationEdge(org_id=flow.org_id, flow_id=flow.id, from_node_id=story.id, to_node_id=evaluation.id))
    story_address, eval_address = graph_address(story, flow_slug=flow.slug), graph_address(evaluation, flow_slug=flow.slug)
    spec = dict(
        evidence_schema="repository-evaluation/v1",
        runner=dict(adapter="engine-repository-evidence-v1", repository=REPO, repository_id=123, harness_sha256=harness_digest()),
        predecessors=[dict(address=story_address, required_checks=[])],
    )
    if multi_repository:
        story.repository_identity = REPO
        story.provider_repository_id = 123
        spec = dict(
            evidence_schema="multi-repository-evaluation/v1",
            mode="repository",
            acceptance_mode="machine",
            harness=dict(
                repository="fixture/harness",
                repository_id=303,
                installation_id=903,
                source_revision="a" * 40,
                definition_revision="b" * 40,
                harness_digest="c" * 64,
                adapter="approved-harness",
                operation="evaluate",
            ),
            predecessors=[dict(address=story_address, repository_id=123, evidence_kind="deployment")],
            criteria=[dict(criterion_id="AC-01")],
        )
    plan = OrchestrationAcceptedPlan(
        org_id=flow.org_id,
        flow_id=flow.id,
        version=1,
        plan_hash="h1",
        plan_document={
            "flow_slug": flow.slug,
            "nodes": [
                {
                    "address": story_address,
                    "kind": "story",
                    "title": story.title,
                    "issue_ref": story.issue_ref,
                    "executor": {"kind": "agent", "role": role, "persona": "operations" if role == "operate" else "developer", "schema_version": 1},
                },
                {"address": eval_address, "kind": "eval", "title": evaluation.title, "issue_ref": evaluation.issue_ref, "evaluation": spec},
            ],
        },
    )
    session.add(plan)
    claim = OrchestrationWorkClaim(
        org_id=flow.org_id,
        provider_repository_id=123,
        issue_number=7289,
        owner_kind="engine_flow",
        owner_ref=flow.id,
        active_run_id="orch:door-run",
        state="held",
        generation=1,
    )
    session.add(claim)
    await session.flush()
    created = await create_execution(session, identity=ExecutionIdentity(flow.org_id, story.id, 1, 1, claim.id, 1), flow_id=flow.id)
    assert created.kind is OutcomeKind.APPLIED
    # The operations worker's terminal report released the claim once its deployment was verified.
    claim.state, claim.release_reason, claim.claim_event_id, claim.active_run_id = "released", "completed", "orch:door-run", None
    await session.flush()
    return flow, story, evaluation, plan, specification(spec), claim, created.record.id


def _verification(flow, story, claim, execution_id, *, key="operations-runtime:1:1", revision=REVISION):
    receipt = _receipt(
        org_id=flow.org_id, flow_id=flow.id, node_id=story.id, execution_id=execution_id, claim_id=claim.id, operation_key=key, revision=revision
    )
    return OrchestrationAction(
        org_id=flow.org_id,
        execution_id=execution_id,
        operation_key=key,
        kind="deployment_verification",
        status="succeeded",
        attempt=1,
        receipt_ref=key,
        detail={"deployment_receipt": receipt.model_dump(mode="json")},
    )


@pytest.mark.parametrize("multi_repository", [False, True])
async def test_operations_predecessor_without_pull_request_sources_its_deployment_receipt(session, multi_repository):
    flow, story, evaluation, plan, spec, claim, execution_id = await _story_graph(session, multi_repository=multi_repository)
    session.add(_verification(flow, story, claim, execution_id))
    await session.flush()

    (source,) = await sources_for(session, evaluation, plan, spec)

    assert source == dict(
        **({"repository_id": 123} if multi_repository else {}),
        address=graph_address(story, flow_slug=flow.slug),
        node_id=story.id,
        attempt=1,
        accepted_plan_version=1,
        execution_id=execution_id,
        deployment_receipt_key="operations-runtime:1:1",
        workflow_operation_keys=["operations-workflow:1:1"],
        components=["knowledge-door"],
        head_sha=REVISION,
        merge_sha=REVISION,
        required_checks=[],
    )


async def test_identity_proof_artifact_target_comes_from_the_verified_runtime(session):
    from copy import deepcopy

    from tests.orchestration.test_identity_proof_binding import BINDING

    flow, story, evaluation, plan, spec, claim, execution_id = await _story_graph(session)
    document = deepcopy(plan.plan_document)
    document["nodes"][0]["operational_release_binding"] = {**BINDING, "repository": REPO, "proof_phase": "stage.identity-proof"}
    plan.plan_document = document
    verification = _verification(flow, story, claim, execution_id)
    receipt = verification.detail["deployment_receipt"]
    receipt["targets"] = [dict(account_id="123456789012", region="us-east-1", resource_kind="eks-namespace", resource_id="cluster/ns")]
    component = receipt["components"][0]
    component.update(component="gateway-backend", migration_head="test-migration", tick_digest=component["image_digest"], door_probe_path=None)
    session.add(verification)
    await session.flush()
    (source,) = await sources_for(session, evaluation, plan, spec)
    assert source["identity_proof_artifact_target"] == dict(account_id="123456789012", region="us-east-1", environment="dev")
    assert source["deployment_receipt_key"] == "operations-runtime:1:1"


async def test_operations_predecessor_needs_exactly_one_agreed_deployment(session):
    flow, story, evaluation, plan, spec, claim, execution_id = await _story_graph(session)
    with pytest.raises(CycleBlockedError, match="predecessor_deployment_receipt_missing"):
        await sources_for(session, evaluation, plan, spec)
    session.add(_verification(flow, story, claim, execution_id))
    session.add(_verification(flow, story, claim, execution_id, key="operations-runtime:2:1", revision="e" * 40))
    await session.flush()
    with pytest.raises(CycleBlockedError, match="predecessor_deployment_receipt_ambiguous"):
        await sources_for(session, evaluation, plan, spec)


@pytest.mark.parametrize("case", ["foundation", "unrelated", "target", "component", "workflow"])
async def test_foundation_target_requires_explicit_evaluation_and_verified_adapter_runtime(session, case):
    from copy import deepcopy

    from tests.orchestration.test_identity_proof_binding import BINDING

    flow, story, evaluation, plan, spec, claim, execution_id = await _story_graph(session)
    document = deepcopy(plan.plan_document)
    document["nodes"][0]["operational_release_binding"] = {
        **BINDING,
        "repository": REPO,
        "component": "tool-adapters",
        "workflows": ["tools-platform-deploy.yml"],
    }
    if case == "workflow":
        document["nodes"][0]["operational_release_binding"]["workflows"] = ["other.yml"]
    plan.plan_document = document
    address = spec.predecessors[0].address
    candidate = spec.model_dump()
    if case != "unrelated":
        candidate["workflows"] = [
            dict(
                criterion_id="foundation",
                path=".github/workflows/tools-platform-deploy.yml",
                source=dict(predecessor=address),
                definition=dict(revision=REVISION),
                required_jobs=["adapter-foundation"],
                artifacts=[
                    dict(
                        name="tools-live-evaluation-{run_id}-{run_attempt}",
                        path="atlassian-stage-summary.json",
                        bind_workflow_provenance=True,
                        predicates=[dict(criterion_id="measured", pointer="/runtime_verified", operation="equals", expected=True)],
                    )
                ],
            )
        ]
    spec = RepositoryEvaluationSpecification.model_validate(candidate)
    verification = _verification(flow, story, claim, execution_id)
    receipt = verification.detail["deployment_receipt"]
    receipt["targets"] = [dict(account_id="123456789012", region="us-east-1", resource_kind="eks-namespace", resource_id="cluster/ns")]
    if case == "target":
        receipt["targets"] = []
    if case != "component":
        receipt["components"][0].update(
            component="tool-adapters",
            manifests_sha256="d" * 64,
            adapter_contract_sha256="e" * 64,
            adapter_probe_path="api_server_proxy",
            pod_uids=["pod-1", "pod-2"],
            door_identity_sha256=None,
            door_probe_path=None,
        )
    session.add(verification)
    await session.flush()
    if case in {"target", "component", "workflow"}:
        with pytest.raises(CycleBlockedError):
            await sources_for(session, evaluation, plan, spec)
    else:
        (source,) = await sources_for(session, evaluation, plan, spec)
        if case == "unrelated":
            assert "foundation_artifact_target" not in source
        else:
            assert source["foundation_artifact_target"] == dict(account_id="123456789012", region="us-east-1", environment="dev")


async def test_developer_predecessor_without_binding_is_still_refused(session):
    """The deployment path is for operate-role stories only; a developer story must bind a PR."""
    flow, story, evaluation, plan, spec, claim, execution_id = await _story_graph(session, role="develop")
    session.add(_verification(flow, story, claim, execution_id))
    await session.flush()
    with pytest.raises(CycleBlockedError, match="predecessor_binding_missing_or_changed"):
        await sources_for(session, evaluation, plan, spec)
