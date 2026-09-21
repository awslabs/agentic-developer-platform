"""Operator recovery and journey use verified, current binding evidence."""

import json
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from src.activity.schemas import InvocationChainResponse
from src.orchestration.models import DecisionKind, NodeState, OrchestrationDecision, OrchestrationPullRequestBinding
from src.orchestration.pr_bindings import PullRequestIdentity
from src.orchestration.results import observe_results
from tests.orchestration import test_read_api as read
from tests.orchestration.test_story_reconciliation import (
    HEAD,
    INSTALLATION,
    PR_NODE,
    PR_NUMBER,
    REPO,
    REPO_ID,
    StubSource,
    _bind,
    _green,
    _story,
)

session = read.session
app_with_router = read.app_with_router


@pytest.fixture(autouse=True)
def providers(monkeypatch):
    identity = PullRequestIdentity(REPO_ID, PR_NODE, REPO, PR_NUMBER, HEAD)
    resolver = AsyncMock(return_value=identity)
    monkeypatch.setattr("src.orchestration.routes.resolve_pr_identity", resolver)
    for module in ("routes", "results", "pr_bindings"):
        monkeypatch.setattr(f"src.orchestration.{module}.resolve_installation_id", AsyncMock(return_value=INSTALLATION))
    activity = Mock()
    activity.get_chain.side_effect = lambda **kw: InvocationChainResponse(correlation_id=kw["correlation_id"], items=[], total_count=0)
    monkeypatch.setattr("src.orchestration.node_activity._activity_service", lambda: activity)
    return resolver


def recovery_url(node):
    return f"/orchestration/flows/{node.flow_id}/nodes/{node.id}/pull-request-recovery"


def body(**updates):
    return {"repo": REPO, "pr_number": PR_NUMBER, "reason": "Verified historical story and pull request association", **updates}


async def test_recovered_historical_story_shows_current_hold_then_completed_pr(session, app_with_router):
    node, dispatch = await _story(session, binding_marker=False)
    client = read.client_for(app_with_router)
    response = client.post(recovery_url(node), json=body())
    assert response.status_code == 200, response.text
    assert "pending" in response.json()["remaining_hold"]
    assert response.json()["bound_pull_request"]["pr_number"] == PR_NUMBER

    class Store:
        def get(self, *_):
            return {"tenant_id": node.org_id, "engine_node_id": node.id, "engine_attempt": 1, "status": "complete"}

    source = StubSource(evidence=_green(review_approved=False))
    report = await observe_results(session, run_store=Store(), evidence=source)
    assert report.errors == 0
    card = client.get(read.route(node.flow_id)).json()["nodes"][0]
    assert card["state"] == "awaiting_merge"
    assert "no verified approval for its current head" in card["binding_hold"]
    assert card["bound_pull_request"]["pr_number"] == PR_NUMBER
    assert source.merged_story_calls == 0

    source.evidence = _green()
    report = await observe_results(session, run_store=Store(), evidence=source)
    assert report.errors == 0
    assert report.advanced == 1
    card = client.get(read.route(node.flow_id)).json()["nodes"][0]
    assert card["state"] == "passed"
    assert card["binding_hold"] is None
    assert card["bound_pull_request"]["pr_number"] == PR_NUMBER
    records = (await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == DecisionKind.RESULT_OBSERVED.value))).all()
    assert any(json.loads(record.reason).get("merge_receipt") for record in records)


class MissingRunStore:
    def get(self, *_):
        return None


async def test_recovered_story_can_reconcile_without_a_run_receipt(session, app_with_router):
    node, _ = await _story(session, binding_marker=True)
    node.state = NodeState.RUNNING.value
    await session.flush()
    client = read.client_for(app_with_router)
    assert client.post(recovery_url(node), json=body()).status_code == 200

    source = StubSource(evidence=_green())
    report = await observe_results(session, run_store=MissingRunStore(), evidence=source)

    assert report.errors == 0
    assert report.advanced == 1
    card = client.get(read.route(node.flow_id)).json()["nodes"][0]
    assert card["state"] == "passed"
    records = (await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == DecisionKind.RESULT_OBSERVED.value))).all()
    payload = json.loads(records[-1].reason)
    assert payload["recovered_without_run_record"] is True
    assert payload["merge_receipt"]["head_sha"] == HEAD


async def test_recovered_story_can_reconcile_a_duplicate_run_skipped_for_merged_pr(session, app_with_router):
    node, _ = await _story(session, binding_marker=True)
    node.state = NodeState.RUNNING.value
    await session.flush()
    client = read.client_for(app_with_router)
    assert client.post(recovery_url(node), json=body()).status_code == 200

    class Store:
        def get(self, *_):
            return {
                "tenant_id": node.org_id,
                "engine_node_id": node.id,
                "engine_attempt": 1,
                "status": "skipped",
                "skip_reason": "idempotency_merged_pr",
            }

    source = StubSource(evidence=_green())
    report = await observe_results(session, run_store=Store(), evidence=source)

    assert report.errors == 0
    assert report.advanced == 1
    card = client.get(read.route(node.flow_id)).json()["nodes"][0]
    assert card["state"] == "passed"
    records = (await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == DecisionKind.RESULT_OBSERVED.value))).all()
    payload = json.loads(records[-1].reason)
    assert payload["recovered_from_skipped_run"] is True
    assert payload["recovered_run_status"] == "skipped"
    assert payload["recovered_skip_reason"] == "idempotency_merged_pr"
    assert payload["merge_receipt"]["head_sha"] == HEAD


async def test_recovered_story_cannot_reconcile_an_unrelated_skipped_run(session, app_with_router):
    node, _ = await _story(session, binding_marker=True)
    client = read.client_for(app_with_router)
    assert client.post(recovery_url(node), json=body()).status_code == 200

    class Store:
        def get(self, *_):
            return {
                "tenant_id": node.org_id,
                "engine_node_id": node.id,
                "engine_attempt": 1,
                "status": "skipped",
                "skip_reason": "policy_refused",
            }

    source = StubSource(evidence=_green())
    report = await observe_results(session, run_store=Store(), evidence=source)

    assert report.errors == 0
    assert report.advanced == 0
    assert report.waiting == 1
    await session.refresh(node)
    assert node.state == NodeState.AWAITING_MERGE.value
    assert source.bound_pr_calls == []


async def test_recovered_story_without_receipt_still_requires_complete_pr_evidence(session, app_with_router):
    node, _ = await _story(session, binding_marker=True)
    client = read.client_for(app_with_router)
    assert client.post(recovery_url(node), json=body()).status_code == 200

    source = StubSource(evidence=_green(review_approved=False))
    report = await observe_results(session, run_store=MissingRunStore(), evidence=source)

    assert report.errors == 0
    assert report.advanced == 0
    card = client.get(read.route(node.flow_id)).json()["nodes"][0]
    assert card["state"] == "awaiting_merge"
    assert "no verified approval for its current head" in card["binding_hold"]


async def test_ordinary_binding_cannot_bypass_a_missing_run_receipt(session, app_with_router):
    node, dispatch = await _story(session, binding_marker=True)
    await _bind(session, node, dispatch)

    source = StubSource(evidence=_green())
    report = await observe_results(session, run_store=MissingRunStore(), evidence=source)

    assert report.errors == 0
    assert report.advanced == 0
    assert report.waiting == 1
    await session.refresh(node)
    assert node.state == NodeState.AWAITING_MERGE.value
    assert source.bound_pr_calls == []


@pytest.mark.parametrize("change", [{"provider_pr_node_id": "PR_fabricated"}, {"provider_repository_id": 999}, {"head_sha": "b" * 40}])
async def test_recovery_rejects_identity_assertions_disagreeing_with_provider(session, app_with_router, change):
    node, _ = await _story(session, binding_marker=False)
    response = read.client_for(app_with_router).post(recovery_url(node), json=body(**change))
    assert response.status_code == 409, response.text
    assert list((await session.scalars(select(OrchestrationPullRequestBinding))).all()) == []


async def test_recovery_checks_flow_and_permission_before_provider_read(session, app_with_router, providers):
    node, _ = await _story(session, binding_marker=False)
    client = read.client_for(app_with_router, permitted=False)
    assert client.post(recovery_url(node), json=body()).status_code == 403
    providers.assert_not_called()
    other = await read.seed_flow(session, slug="different-flow")
    client = read.client_for(app_with_router)
    wrong_flow = recovery_url(node).replace(node.flow_id, other.id)
    assert client.post(wrong_flow, json=body()).status_code == 404
    providers.assert_not_called()


async def test_recovery_cannot_change_dispatched_repository(session, app_with_router, providers):
    node, _ = await _story(session, binding_marker=False)
    response = read.client_for(app_with_router).post(recovery_url(node), json=body(repo="other/repo"))
    assert response.status_code == 409
    providers.assert_not_called()


async def test_operator_can_explicitly_replace_abandoned_pr(session, app_with_router, providers):
    node, _ = await _story(session, binding_marker=False)
    client = read.client_for(app_with_router)
    assert client.post(recovery_url(node), json=body()).status_code == 200
    providers.return_value = PullRequestIdentity(REPO_ID, "PR_replacement", REPO, PR_NUMBER + 1, "b" * 40)
    replacement = body(pr_number=PR_NUMBER + 1)
    assert client.post(recovery_url(node), json=replacement).status_code == 409
    replacement["replaces_reason"] = "Previous implementation was abandoned; this PR delivers the accepted story"
    response = client.post(recovery_url(node), json=replacement)
    assert response.status_code == 200, response.text
    rows = (await session.scalars(select(OrchestrationPullRequestBinding))).all()
    assert len(rows) == 2
    assert {row.pr_number: row.state for row in rows} == {PR_NUMBER: "superseded", PR_NUMBER + 1: "active"}
    assert "pending" in response.json()["remaining_hold"]


async def test_explicit_historical_adoption_never_dispatches_and_reconciles_attempt_zero(session, app_with_router, monkeypatch):
    from tests.orchestration.test_delivery_adoption import NoWorker, story

    monkeypatch.setenv("BG_ORCH_DISPATCH_REPO", REPO)
    monkeypatch.setattr("src.orchestration.delivery_adoption.resolve_installation_id", AsyncMock(return_value=INSTALLATION))
    monkeypatch.setattr("src.orchestration.merge_evidence.GitHubEvidenceSource.bound_pull_request", AsyncMock(return_value=_green()))
    node = await story(session)
    client = read.client_for(app_with_router)
    response = client.post(recovery_url(node), json=body(adopt_delivery=True))
    assert response.status_code == 200, response.text
    assert node.state == "awaiting_merge" and node.attempts == 0
    assert client.post(recovery_url(node), json=body(adopt_delivery=True)).status_code == 200
    binding = (await session.scalars(select(OrchestrationPullRequestBinding))).one()
    assert binding.run_id is None
    report = await observe_results(session, run_store=NoWorker(), evidence=StubSource(evidence=_green()))
    assert report.advanced == 1 and node.state == "passed" and node.attempts == 0
    decisions = (await session.scalars(select(OrchestrationDecision))).all()
    assert not any(row.kind == DecisionKind.NODE_DISPATCHED.value for row in decisions)


async def test_historical_adoption_api_refuses_active_attempt_before_provider(session, app_with_router, providers):
    node, _ = await _story(session, binding_marker=True)
    response = read.client_for(app_with_router).post(recovery_url(node), json=body(adopt_delivery=True))
    assert response.status_code == 409
    providers.assert_not_awaited()
