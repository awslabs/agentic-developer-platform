"""Preview and acceptance share graph and evidence preservation guards."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.orchestration.amend import amend_plan
from src.orchestration.amendment_impact import assess_amendment
from src.orchestration.compile import ProposalRejectedError
from src.orchestration.execution_policy import ExecutionPolicy, PolicyLimits
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationNode
from src.orchestration.pending_amendments import accept_amendment, register_amendment_draft
from src.orchestration.proposal import NamedInputPrerequisite, ProposedEdge, ProposedNode, validate_proposal
from src.orchestration.repository_evaluation_contract import harness_digest
from tests.orchestration import test_pending_amendments as fixtures
from tests.orchestration.legacy_plan import persist_legacy_plan

session = fixtures.session


def evaluation_plan():
    proposal = fixtures.base_proposal()
    evaluation = next(node for node in proposal.nodes if node.kind == "eval")
    evaluation.criteria = ["cleanup"]
    evaluation.evaluation = {"criteria": [{"criterion_id": "cleanup", "kind": "api", "required": True}]}
    return proposal


@pytest.mark.parametrize(
    "case,rule",
    [
        ("missing-spec", "criterion_producer_missing"),
        ("missing-criterion", "criterion_producer_missing"),
        ("optional-criterion", "criterion_producer_missing"),
        ("cycle", "cycle"),
        ("input-cycle", "cycle"),
        ("self-input", "cycle"),
        ("own-result-gate", "validation_behind_result_gate"),
        ("second-eval", "wave_eval_cardinality"),
    ],
)
async def test_invalid_graph_is_explained_in_preview_and_refused_at_acceptance(session, case, rule):
    base = fixtures.base_proposal()
    compiled = await persist_legacy_plan(session, base, fixtures.approval())
    proposal = evaluation_plan()
    evaluation = next(node for node in proposal.nodes if node.kind == "eval")
    if case == "missing-spec":
        evaluation.evaluation = None
    elif case == "missing-criterion":
        evaluation.criteria.append("live-cleanup")
    elif case == "optional-criterion":
        evaluation.evaluation["criteria"][0]["required"] = False
    elif case == "cycle":
        proposal.edges.append(ProposedEdge(from_address=fixtures.address("eval"), to_address=fixtures.address("story-a")))
    elif case == "input-cycle":
        proposal.nodes[0].required_inputs = [NamedInputPrerequisite(name="own result", source=f"node:{fixtures.address('eval')}")]
    elif case == "self-input":
        evaluation.required_inputs = [NamedInputPrerequisite(name="own result", source=f"node:{evaluation.address}")]
    elif case == "own-result-gate":
        gate = ProposedNode(address=fixtures.address("result-gate"), kind="gate", title="Accept validation results")
        proposal.nodes.append(gate)
        proposal.edges.append(ProposedEdge(from_address=gate.address, to_address=evaluation.address))
    else:
        proposal.nodes.append(ProposedNode(address=fixtures.address("second-eval"), kind="eval", title="Another eval", issue_ref="7201"))
    assert rule in {violation.rule for violation in validate_proposal(proposal)}
    preview = await assess_amendment(session, org_id=base.org_id, flow_id=compiled.flow_id, proposal=proposal)
    assert rule in {violation.rule for violation in preview.blocking}
    request = await fixtures.open_request(session, compiled.flow_id)
    draft = await register_amendment_draft(session, org_id=base.org_id, request=request, author_run_id=request.author_run_id, proposal=proposal)
    with pytest.raises(ProposalRejectedError) as refused:
        await accept_amendment(session, draft_id=draft.draft_id, actor=fixtures.amender(), flow_id=compiled.flow_id)
    assert rule in {violation.rule for violation in refused.value.violations}
    assert len(list(await session.scalars(select(OrchestrationAcceptedPlan)))) == 1
    assert len(list(await session.scalars(select(OrchestrationNode)))) == len(base.nodes)


@pytest.mark.parametrize("state", ["running", "passed", "awaiting_gate"])
@pytest.mark.parametrize("change", ["incoming-edge", "required-input", "remove-input", "criteria", "evaluation", "repository"])
async def test_started_execution_cannot_reinterpret_preserved_history(session, state, change):
    base = evaluation_plan()
    if change == "remove-input":
        base.nodes[1].required_inputs = [NamedInputPrerequisite(name="target", source="environment:target")]
    compiled = await persist_legacy_plan(session, base, fixtures.approval())
    current = await session.scalar(
        select(OrchestrationNode).where(OrchestrationNode.flow_id == compiled.flow_id, OrchestrationNode.node_ref == "eval")
    )
    current.state, current.attempts = state, 2
    node_id = current.id
    proposal = base.model_copy(deep=True)
    evaluation = next(node for node in proposal.nodes if node.kind == "eval")
    if change == "incoming-edge":
        proposal.nodes.append(ProposedNode(address=fixtures.address("prerequisite", wave="inputs"), kind="gate", title="New prerequisite"))
        proposal.edges.append(ProposedEdge(from_address=fixtures.address("prerequisite", wave="inputs"), to_address=evaluation.address))
    elif change == "required-input":
        evaluation.required_inputs = [NamedInputPrerequisite(name="target", source="environment:new-target")]
    elif change == "remove-input":
        evaluation.required_inputs = []
    elif change == "criteria":
        evaluation.criteria.append("new-requirement")
        evaluation.evaluation["criteria"].append({"criterion_id": "new-requirement", "kind": "api", "required": True})
    elif change == "evaluation":
        evaluation.evaluation["criteria"][0]["kind"] = "control"
    else:
        evaluation.repository_identity = "other/repository"
    await session.commit()
    preview = await assess_amendment(session, org_id=base.org_id, flow_id=compiled.flow_id, proposal=proposal)
    rule = "amendment_started_node_prerequisites" if change == "incoming-edge" else "amendment_definition_change"
    violations = [violation for violation in preview.blocking if violation.rule == rule]
    assert violations and "new node address" in violations[0].message
    request = await fixtures.open_request(session, compiled.flow_id)
    draft = await register_amendment_draft(session, org_id=base.org_id, request=request, author_run_id=request.author_run_id, proposal=proposal)
    with pytest.raises(ProposalRejectedError):
        await accept_amendment(session, draft_id=draft.draft_id, actor=fixtures.amender(), flow_id=compiled.flow_id)
    with pytest.raises(ProposalRejectedError):
        await amend_plan(session, flow_id=compiled.flow_id, proposal=proposal, actor=fixtures.amender())
    await session.refresh(current)
    assert (current.id, current.state, current.attempts) == (node_id, state, 2)
    plan = (await session.scalars(select(OrchestrationAcceptedPlan))).one()
    assert plan.plan_hash == compiled.plan_hash


@pytest.mark.parametrize("state", ["pending", "ready"])
async def test_unstarted_evaluation_can_gain_new_requirements(session, state):
    base = evaluation_plan()
    compiled = await persist_legacy_plan(session, base, fixtures.approval())
    current = await session.scalar(
        select(OrchestrationNode).where(OrchestrationNode.flow_id == compiled.flow_id, OrchestrationNode.node_ref == "eval")
    )
    current.state = state
    proposal = base.model_copy(deep=True)
    evaluation = next(node for node in proposal.nodes if node.kind == "eval")
    evaluation.criteria.append("new-requirement")
    evaluation.evaluation["criteria"].append({"criterion_id": "new-requirement", "kind": "api", "required": True})
    preview = await assess_amendment(session, org_id=base.org_id, flow_id=compiled.flow_id, proposal=proposal)
    assert not preview.blocking
    accepted = await amend_plan(session, flow_id=compiled.flow_id, proposal=proposal, actor=fixtures.amender())
    assert accepted.plan_version == 2
    assert (current.state, current.attempts) == (state, 0)


@pytest.mark.parametrize("expectation", ["trusted-evaluation", "trusted-live-observation"])
def test_repository_evidence_cannot_claim_live_qualification(expectation):
    proposal = evaluation_plan()
    evaluation = proposal.nodes[1]
    proposal.execution_policy = ExecutionPolicy(
        org_id=proposal.org_id,
        repository_ids=["example/project"],
        allowed_actions=["evaluate"],
        evaluation_acceptance={evaluation.address: "machine"},
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        limits=PolicyLimits(max_wall_clock_seconds=3600, max_spend_usd=10, max_attempts_per_node=3, max_concurrent_actions=1),
    )
    evaluation.evidence_expectations = [expectation]
    evaluation.criteria = [fixtures.address("story-a")]
    evaluation.evaluation = dict(
        evidence_schema="repository-evaluation/v1",
        runner=dict(adapter="engine-repository-evidence-v1", repository="example/project", repository_id=123, harness_sha256=harness_digest()),
        predecessors=[dict(address=fixtures.address("story-a"), required_checks=[dict(name="Tests", app_id=1)])],
    )
    violations = validate_proposal(proposal)
    assert {violation.rule for violation in violations} == (
        {"evidence_expectation_unsupported"} if expectation == "trusted-live-observation" else set()
    )


async def test_presentation_edits_do_not_reinterpret_completed_execution(session):
    base = evaluation_plan()
    base.nodes[1].required_inputs = [NamedInputPrerequisite(name="target", source="environment:target", description="Original description")]
    compiled = await persist_legacy_plan(session, base, fixtures.approval())
    current = await session.scalar(
        select(OrchestrationNode).where(OrchestrationNode.flow_id == compiled.flow_id, OrchestrationNode.node_ref == "eval")
    )
    current.state, current.attempts = "passed", 1
    proposal = base.model_copy(deep=True)
    proposal.nodes[1].title = "Clearer evaluation title"
    proposal.nodes[1].required_inputs[0].description = "Clearer prerequisite description"
    preview = await assess_amendment(session, org_id=base.org_id, flow_id=compiled.flow_id, proposal=proposal)
    assert not preview.blocking
    accepted = await amend_plan(session, flow_id=compiled.flow_id, proposal=proposal, actor=fixtures.amender())
    assert accepted.plan_version == 2 and current.state == "passed" and current.attempts == 1


def repository_evidence_plan(harness: str):
    proposal = evaluation_plan()
    evaluation = proposal.nodes[1]
    proposal.execution_policy = ExecutionPolicy(
        org_id=proposal.org_id,
        repository_ids=["example/project"],
        allowed_actions=["evaluate"],
        evaluation_acceptance={evaluation.address: "machine"},
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        limits=PolicyLimits(max_wall_clock_seconds=3600, max_spend_usd=10, max_attempts_per_node=3, max_concurrent_actions=1),
    )
    evaluation.criteria = [fixtures.address("story-a")]
    evaluation.evaluation = dict(
        evidence_schema="repository-evaluation/v1",
        runner=dict(adapter="engine-repository-evidence-v1", repository="example/project", repository_id=123, harness_sha256=harness),
        predecessors=[dict(address=fixtures.address("story-a"), required_checks=[dict(name="Tests", app_id=1)])],
    )
    return proposal


async def test_concluded_evaluation_keeps_its_verifier_digest_when_the_plan_is_amended(session, monkeypatch):
    """A gateway deploy that changes the installed verifier must not make a plan with passed evaluations un-amendable."""
    base = repository_evidence_plan(harness_digest())
    base.nodes[1].evaluation["runner"]["harness_policy"] = "exact"
    compiled = await persist_legacy_plan(session, base, fixtures.approval())
    current = await session.scalar(
        select(OrchestrationNode).where(OrchestrationNode.flow_id == compiled.flow_id, OrchestrationNode.node_ref == "eval")
    )
    current.state, current.attempts = "passed", 1
    await session.commit()
    # The deploy ships a different verifier; the accepted document still pins the old one.
    monkeypatch.setattr("src.orchestration.repository_evaluation_contract.harness_digest", lambda: "f" * 64)
    proposal = base.model_copy(deep=True)
    proposal.nodes[2].title = "Deploy wave 2?"
    assert any(violation.rule == "evaluation_harness_mismatch" for violation in validate_proposal(proposal))
    preview = await assess_amendment(session, org_id=base.org_id, flow_id=compiled.flow_id, proposal=proposal)
    assert not [violation for violation in preview.blocking if violation.rule == "evaluation_harness_mismatch"]
    accepted = await amend_plan(session, flow_id=compiled.flow_id, proposal=proposal, actor=fixtures.amender())
    assert accepted.plan_version == 2 and current.state == "passed"


@pytest.mark.parametrize("state", ["pending", "ready"])
@pytest.mark.parametrize("policy", ["contract", "exact"])
async def test_unconcluded_evaluation_uses_its_compatibility_policy(session, state, policy, monkeypatch):
    base = repository_evidence_plan(harness_digest())
    if policy == "exact":
        base.nodes[1].evaluation["runner"]["harness_policy"] = policy
    compiled = await persist_legacy_plan(session, base, fixtures.approval())
    current = await session.scalar(
        select(OrchestrationNode).where(OrchestrationNode.flow_id == compiled.flow_id, OrchestrationNode.node_ref == "eval")
    )
    current.state = state
    await session.commit()
    monkeypatch.setattr("src.orchestration.repository_evaluation_contract.harness_digest", lambda: "f" * 64)
    proposal = base.model_copy(deep=True)
    proposal.nodes[2].title = "Deploy wave 2?"
    preview = await assess_amendment(session, org_id=base.org_id, flow_id=compiled.flow_id, proposal=proposal)
    if policy == "contract":
        assert not [violation for violation in preview.blocking if violation.rule == "evaluation_harness_mismatch"]
        accepted = await amend_plan(session, flow_id=compiled.flow_id, proposal=proposal, actor=fixtures.amender())
        assert accepted.plan_version == 2 and current.state == state
        return
    assert [violation for violation in preview.blocking if violation.rule == "evaluation_harness_mismatch"]
    with pytest.raises(ProposalRejectedError):
        await amend_plan(session, flow_id=compiled.flow_id, proposal=proposal, actor=fixtures.amender())
