"""Historical adoption verifies delivery without manufacturing worker execution."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration.delivery_adoption import adopt_delivery
from src.orchestration.models import (
    DecisionKind,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
    OrchestrationWorkClaim,
)
from src.orchestration.pr_bindings import BindingError, PullRequestIdentity, _accepted_scope
from src.orchestration.results import observe_results
from tests.orchestration import test_story_reconciliation as fixtures
from tests.orchestration.test_story_reconciliation import HEAD, INSTALLATION, ORG_A, PR_NODE, PR_NUMBER, REPO, REPO_ID, StubSource, _green

engine = fixtures.engine
session = fixtures.session


@pytest.fixture(autouse=True)
def provider(monkeypatch):
    monkeypatch.setenv("BG_ORCH_DISPATCH_REPO", REPO)
    monkeypatch.setattr("src.orchestration.delivery_adoption.resolve_installation_id", AsyncMock(return_value=INSTALLATION))


async def story(session):
    flow = OrchestrationFlow(org_id=ORG_A, slug="historical", title="Historical flow", state="running")
    session.add(flow)
    await session.flush()
    node = OrchestrationNode(
        org_id=ORG_A,
        flow_id=flow.id,
        epic_ref="epic-1",
        wave_ref="wave-1",
        node_ref="story-1",
        title="Delivered code",
        kind="story",
        state="pending",
        attempts=0,
        issue_ref="5327",
    )
    session.add(node)
    await session.flush()
    return node


async def adopt(session, node, **overrides):
    arguments = dict(
        org_id=ORG_A,
        node_id=node.id,
        pr=PullRequestIdentity(REPO_ID, PR_NODE, REPO, PR_NUMBER, HEAD),
        installation_id=INSTALLATION,
        actor_id="operator",
        reason="Verified implementation covers this historical story",
        evidence=_green(),
        expected_scope=await _accepted_scope(session, node),
    )
    arguments.update(overrides)
    return await adopt_delivery(session, **arguments)


class NoWorker:
    def get(self, *args):
        raise AssertionError("historical adoption must never read a worker")


async def test_adoption_attempt_zero_is_attributed_idempotent_and_completes_without_worker(session):
    node = await story(session)
    binding = await adopt(session, node)
    assert node.state == "awaiting_merge" and node.attempts == 0
    assert binding.run_id is None and binding.registered_by == "operator"
    assert (await adopt(session, node)).id == binding.id
    report = await observe_results(session, run_store=NoWorker(), evidence=StubSource(evidence=_green()))
    assert report.errors == 0 and report.advanced == 1
    assert node.state == "passed" and node.attempts == 0
    assert (await adopt(session, node)).id == binding.id
    decisions = (await session.scalars(select(OrchestrationDecision))).all()
    assert not any(row.kind == DecisionKind.NODE_DISPATCHED.value for row in decisions)
    assert len([row for row in decisions if row.kind == DecisionKind.PR_BINDING_CHANGED.value]) == 1


@pytest.mark.parametrize(
    "updates",
    [
        {"merged": False},
        {"checks_successful": False},
        {"review_approved": False},
        {"head_sha": "a" * 40},
        {"provider_repository_id": 999},
        {"provider_pr_node_id": "wrong"},
    ],
)
async def test_adoption_refuses_incomplete_provider_evidence_without_mutation(session, updates):
    node = await story(session)
    with pytest.raises(BindingError):
        await adopt(session, node, evidence=replace(_green(), **updates))
    assert node.state == "pending" and node.attempts == 0
    assert (await session.scalars(select(OrchestrationPullRequestBinding))).all() == []


async def test_adoption_preserves_predecessor_human_gate_and_rechecks_provider(session):
    node = await story(session)
    gate = OrchestrationNode(
        org_id=ORG_A,
        flow_id=node.flow_id,
        epic_ref="epic-1",
        wave_ref="wave-1",
        node_ref="gate-1",
        title="Approval",
        kind="gate",
        state="awaiting_gate",
        attempts=0,
    )
    session.add(gate)
    await session.flush()
    session.add(OrchestrationEdge(org_id=ORG_A, flow_id=node.flow_id, from_node_id=gate.id, to_node_id=node.id))
    await session.flush()
    await adopt(session, node)
    source = StubSource(evidence=_green())
    report = await observe_results(session, run_store=NoWorker(), evidence=source)
    assert report.errors == 0 and report.advanced == 0 and "predecessor" in report.reasons[node.id]
    assert gate.state == "awaiting_gate" and node.state == "awaiting_merge"
    gate.state = "passed"
    source.evidence = _green(review_approved=False)
    report = await observe_results(session, run_store=NoWorker(), evidence=source)
    assert report.advanced == 0 and "no verified approval" in report.reasons[node.id]
    source.evidence = _green()
    assert (await observe_results(session, run_store=NoWorker(), evidence=source)).advanced == 1


@pytest.mark.parametrize("state,attempt", [("running", 1), ("ready", 1), ("awaiting_gate", 0), ("halted", 0), ("rejected_at_gate", 0)])
async def test_adoption_refuses_active_run_or_human_hold(session, state, attempt):
    node = await story(session)
    node.state, node.attempts = state, attempt
    with pytest.raises(BindingError):
        await adopt(session, node)
    assert (node.state, node.attempts) == (state, attempt)


async def test_adoption_refuses_scope_change_wrong_repository_and_active_owner(session):
    node = await story(session)
    scope = await _accepted_scope(session, node)
    node.title = "Changed scope"
    with pytest.raises(BindingError, match="scope changed"):
        await adopt(session, node, expected_scope=scope)
    with pytest.raises(BindingError, match="configured engine repository"):
        await adopt(session, node, pr=PullRequestIdentity(REPO_ID, PR_NODE, "other/repo", PR_NUMBER, HEAD))
    session.add(
        OrchestrationWorkClaim(
            org_id=ORG_A,
            provider_repository_id=REPO_ID,
            issue_number=5327,
            owner_kind="legacy",
            owner_ref="other-session",
            state="held",
            generation=1,
        )
    )
    await session.flush()
    with pytest.raises(BindingError, match="active work owner"):
        await adopt(session, node)
