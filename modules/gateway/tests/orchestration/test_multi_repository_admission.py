"""Readable new evaluation versions cannot enter any legacy execution lane."""

import json
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration.compile import compile_proposal
from src.orchestration.dispatch_pass import DispatchPassConfig, DispatchPassReport, _dispatch_one
from src.orchestration.evaluation_acceptance import EvaluationAcceptanceError, EvaluationAcceptanceRequest, preview_evaluation
from src.orchestration.evaluation_plan import accepted_evaluation, human_evaluation
from src.orchestration.execution_policy import AcceptanceMode
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationFlow, OrchestrationNode
from src.orchestration.proposal import RepositoryConnection, validate_proposal
from tests.orchestration.test_compile import address, approval, session, valid_proposal  # noqa: F401
from tests.orchestration.test_execution_policy_acceptance import a_policy
from tests.orchestration.test_multi_repository_evaluation_contract import evaluation
from tests.orchestration.test_multi_repository_evaluation_contract import verified_repositories as verified_repositories


async def bound_plan(db, actor, *, mode="repository", acceptance_mode="machine"):
    proposal = valid_proposal()
    for story, repository_id, installation_id, issue_number in zip(proposal.nodes[:2], [101, 202], [901, 902], [17, 18]):
        story.issue_ref = str(issue_number)
        story.repository_identity = f"fixture/source-{story.address[-1]}"
        story.provider_repository_id = repository_id
        story.repository_connection = RepositoryConnection(provider="github", installation_id=installation_id)
    node = next(item for item in proposal.nodes if item.kind == "eval")
    node.repository_identity = "fixture/harness"
    node.provider_repository_id = 303
    node.repository_connection = RepositoryConnection(provider="github", installation_id=903)
    node.criteria = ["AC-01"]
    node.evaluation = evaluation(mode)
    node.evaluation["acceptance_mode"] = acceptance_mode
    for source, story in zip(node.evaluation["predecessors"], proposal.nodes[:2]):
        source["address"] = story.address
    proposal.execution_policy = a_policy(
        repository_ids=["fixture/source-a", "fixture/source-b", "fixture/harness"],
        environment_connection_ids=["target-connection"],
        evaluation_acceptance={address("eval"): AcceptanceMode(acceptance_mode)},
    )
    assert not validate_proposal(proposal)
    accepted = await compile_proposal(db, proposal, actor)
    flow = await db.get(OrchestrationFlow, accepted.flow_id)
    flow.execution_paused = False
    stored = (
        await db.scalars(select(OrchestrationNode).where(OrchestrationNode.flow_id == accepted.flow_id, OrchestrationNode.kind == "eval"))
    ).one()
    stored.state = "ready"
    await db.flush()
    return stored


@pytest.mark.parametrize("mode", ["repository", "deployed"])
@pytest.mark.parametrize("acceptance_mode", ["machine", "human"])
@pytest.mark.usefixtures("verified_repositories")
async def test_dispatch_refuses_unavailable_adapter_without_claim_or_human_fallback(session, approval, monkeypatch, mode, acceptance_mode):  # noqa: F811
    node = await bound_plan(session, approval, mode=mode, acceptance_mode=acceptance_mode)
    claim = AsyncMock(side_effect=AssertionError("no legacy worker may claim a new evaluation"))
    monkeypatch.setattr("src.orchestration.work_admission.admit", claim)
    monkeypatch.setattr("src.orchestration.dispatch_pass.product_binding_pending_verification", AsyncMock(return_value=False))
    selected = await accepted_evaluation(session, node)
    assert selected is not None and selected[1].mode == mode
    human = await human_evaluation(session, node)
    assert (human is not None) == (acceptance_mode == "human")
    report = DispatchPassReport(enabled=True)
    await _dispatch_one(session, node, config=DispatchPassConfig(queue_url="queue", repo="fixture/harness"), report=report)
    assert node.state == "ready" and node.attempts == 0 and report.pending == []
    claim.assert_not_awaited()
    decisions = list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id)))
    assert any(json.loads(item.rejection_reason or "{}").get("block_code") == "evaluation_adapter_unavailable" for item in decisions)


@pytest.mark.parametrize(
    ("version", "reason"),
    [
        ("multi-repository-evaluation/v2", "evaluation_contract_version_unsupported"),
    ],
)
async def test_version_negotiation_refuses_new_adapter_before_legacy_acceptance(session, approval, version, reason):  # noqa: F811
    request = EvaluationAcceptanceRequest(
        node_id="eval-fixture",
        expected_plan_version=1,
        expected_plan_hash="a" * 64,
        specification=evaluation(),
        reason="Approved fixture version is unavailable",
    )
    request.specification["evidence_schema"] = version
    with pytest.raises(EvaluationAcceptanceError, match=reason):
        await preview_evaluation(session, flow_id="flow-fixture", actor=approval, request=request)


@pytest.mark.usefixtures("verified_repositories")
async def test_tick_reports_adapter_hold_without_releasing_evaluation(session, approval):  # noqa: F811
    from src.orchestration.tick import run_tick

    node = await bound_plan(session, approval)
    node.state = "pending"
    for story in await session.scalars(select(OrchestrationNode).where(OrchestrationNode.flow_id == node.flow_id, OrchestrationNode.kind == "story")):
        story.state = "passed"
    await session.flush()
    report = await run_tick(session)
    assert report.errors == 0
    assert report.blocked[node.id] == ["evaluation_adapter_unavailable"]
    assert node.state == "pending" and node.attempts == 0


@pytest.mark.usefixtures("verified_repositories")
async def test_unknown_version_has_typed_dispatch_and_tick_refusals(session, approval, monkeypatch):  # noqa: F811
    from src.orchestration.tick import run_tick

    node = await bound_plan(session, approval)
    plan = (await session.scalars(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == node.flow_id))).one()
    document = deepcopy(plan.plan_document)
    next(item for item in document["nodes"] if item["kind"] == "eval")["evaluation"]["evidence_schema"] = "multi-repository-evaluation/v2"
    plan.plan_document = document
    await session.flush()
    monkeypatch.setattr("src.orchestration.dispatch_pass.product_binding_pending_verification", AsyncMock(return_value=False))
    report = DispatchPassReport(enabled=True)
    await _dispatch_one(session, node, config=DispatchPassConfig(queue_url="queue", repo="fixture/harness"), report=report)
    decisions = list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id)))
    assert any(json.loads(item.rejection_reason or "{}").get("block_code") == "evaluation_contract_version_unsupported" for item in decisions)
    assert node.state == "ready" and node.attempts == 0 and report.pending == []
    node.state = "pending"
    for story in await session.scalars(select(OrchestrationNode).where(OrchestrationNode.flow_id == node.flow_id, OrchestrationNode.kind == "story")):
        story.state = "passed"
    await session.flush()
    tick = await run_tick(session)
    assert tick.errors == 0
    assert tick.blocked[node.id] == ["evaluation_contract_version_unsupported"]
    assert node.state == "pending" and node.attempts == 0


async def test_controller_observation_blocks_before_any_adapter(session, monkeypatch):  # noqa: F811
    from src.orchestration.evaluation_contract import EvaluationVersionError, specification
    from src.orchestration.evaluation_controller import EvaluationController
    from src.orchestration.execution_runner import ObservationKind

    @asynccontextmanager
    async def factory():
        yield session

    services = SimpleNamespace(factory=factory, node=AsyncMock(return_value=SimpleNamespace(kind="eval")), story=AsyncMock(), evaluate=AsyncMock())
    controller = EvaluationController(factory, services=services)
    selected = AsyncMock(return_value=(None, specification(evaluation()), address("eval")))
    monkeypatch.setattr("src.orchestration.evaluation_controller.accepted_evaluation", selected)
    observation = await controller.observe(SimpleNamespace())
    assert observation.kind is ObservationKind.BLOCKED
    assert observation.block.detail == "evaluation_adapter_unavailable"
    selected.side_effect = EvaluationVersionError("evaluation_contract_version_unsupported")
    observation = await controller.observe(SimpleNamespace())
    assert observation.kind is ObservationKind.BLOCKED
    assert observation.block.detail == "evaluation_contract_version_unsupported"
    services.story.assert_not_awaited()
    services.evaluate.assert_not_awaited()
