"""SCN-8176-01/02/03: actual observer records through the public graph route."""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select

from src.orchestration.compile import ApprovalContext
from src.orchestration.evaluation_acceptance import EvaluationAcceptanceRequest, accept_evaluation, preview_evaluation
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationDecision
from src.orchestration.repository import OrchestrationRepository
from src.orchestration.repository_evaluation import observe_repository_evaluation, record
from src.orchestration.repository_evaluation_contract import harness_digest
from tests.orchestration import test_read_api as read
from tests.orchestration import test_repository_evaluation_provider as provider_fixtures

session = read.session
app_with_router = read.app_with_router
evidence = provider_fixtures.evidence
WHEN = datetime(2026, 10, 1, 12, tzinfo=UTC)


@pytest.fixture
async def target(session):
    flow = await read.seed_flow(session)
    node = await read.seed_node(session, flow, node_ref="eval-machine", kind="eval", state="ready")
    await read.accept_policy(session, flow, repository_ids=["example/project"])
    plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == flow.id))
    spec = {
        "evidence_schema": "repository-evaluation/v1",
        "runner": {
            "adapter": "engine-repository-evidence-v1",
            "repository": "example/project",
            "repository_id": 123,
            "harness_sha256": "0" * 64,
            "harness_policy": "exact",
        },
        "external_pull_requests": [
            {
                "criterion_id": "fixture-delivery",
                "issue_number": 1,
                "pr_number": 2,
                "head_sha": "a" * 40,
                "merge_sha": "b" * 40,
                "required_checks": [{"name": "Unit tests", "app_id": 123}],
            }
        ],
    }
    plan.plan_document = {
        **plan.plan_document,
        "nodes": [
            {
                "address": read.address_of(node),
                "kind": "eval",
                "title": node.title,
                "issue_ref": node.issue_ref,
                "evaluation": spec,
            }
        ],
    }
    await session.flush()
    clock = 0

    def stamp(database, _context, _instances):
        nonlocal clock
        for row in database.new:
            if isinstance(row, OrchestrationDecision) and row.actor_id == "system:repository-evaluation":
                row.created_at = WHEN + timedelta(seconds=clock)
                clock += 1

    event.listen(session.sync_session, "before_flush", stamp)
    try:
        yield flow, node, plan, spec
    finally:
        event.remove(session.sync_session, "before_flush", stamp)


async def refuse(session, target, rewrite=None):
    _, node, _, _ = target
    provider = AsyncMock()
    emitted = []

    def capture(database, _context, _instances):
        for row in database.new:
            if isinstance(row, OrchestrationDecision) and row.kind == "transition_rejected":
                if rewrite:
                    rewrite(row)
                emitted.append(row)

    event.listen(session.sync_session, "before_flush", capture)
    try:
        assert await observe_repository_evaluation(session, node, provider=provider)
    finally:
        event.remove(session.sync_session, "before_flush", capture)
    provider.observe.assert_not_awaited()
    assert len(emitted) == 1
    return emitted[0]


def card(app, flow, node):
    response = read.client_for(app).get(read.route(flow.id) + "?include_activity=false")
    assert response.status_code == 200, response.text
    assert "SECRET" not in response.text
    return next(item for item in response.json()["nodes"] if item["id"] == node.id)


async def test_actual_observer_refusal_is_actionable_and_bounded(session, app_with_router, target, monkeypatch):
    flow, node, plan, _ = target
    row = await refuse(session, target)
    payload = json.loads(row.reason)
    assert payload["action"] == "repository_evaluation_blocked"
    assert payload["block_code"] == "repository_evaluation_harness_changed"
    assert payload["scope"]["attempt"] == 0 and payload["scope"]["accepted_plan_id"] == plan.id
    assert payload["scope"]["acceptance_decision_id"] is None

    async def forbidden(*args, **kwargs):
        pytest.fail("graph must not materialize the decision ledger")

    monkeypatch.setattr(OrchestrationRepository, "list_decisions", forbidden)
    result = card(app_with_router, flow, node)
    progress = result["delivery_progress"]
    fixture = Path(__file__).parents[2] / "frontend/src/__tests__/fixtures/repository-evaluation-refusal-node.json"
    expected = json.loads(fixture.read_text())
    assert result["state"] == expected["state"] and result["display_state"] == expected["display_state"]
    assert {key: progress[key] for key in expected["delivery_progress"]} == expected["delivery_progress"]
    assert result["state"] == "ready" and result["display_state"] == "stalled" and result["attempts"] == 0
    assert progress["stage"] == "evidence_wait" and progress["automation"] == "blocked"
    assert progress["blocker"] == "repository_evaluation_harness_changed" and progress["actor"] == "flow-owner"
    assert progress["required_input"] == "An accepted specification pinned to the reviewed installed harness."
    assert progress["next_action"] == "Review and accept the current evaluation specification, then recheck evidence."
    assert progress["observed_at"] == WHEN.isoformat()
    assert progress["next_check_at"] is None and progress["scheduled_action"] is None


@pytest.mark.parametrize(
    "change",
    [
        "actor",
        "actor_kind",
        "tenant",
        "ledger_node",
        "attempt",
        "bool_attempt",
        "plan_id",
        "plan_version",
        "plan_hash",
        "specification",
        "overlay",
        "scope_tenant",
        "scope_flow",
        "scope_node",
        "contract",
        "action",
        "code",
        "unscoped",
        "malformed",
    ],
)
async def test_wrong_or_unrecognized_refusal_cannot_become_current(session, app_with_router, target, change):
    flow, node, _, _ = target
    other = await read.seed_node(session, flow, node_ref="other-eval", kind="eval", state="ready") if change == "ledger_node" else None

    def rewrite(row):
        payload = json.loads(row.reason)
        if change == "actor":
            row.actor_id = "other-service"
        elif change == "actor_kind":
            row.actor_kind = "human"
        elif change == "tenant":
            row.org_id = read.ORG_B
        elif change == "ledger_node":
            row.node_id = other.id
        elif change == "unscoped":
            for key in ("contract", "scope", "diagnostic_code"):
                payload.pop(key)
        elif change in {"contract", "action", "code"}:
            payload[{"code": "diagnostic_code"}.get(change, change)] = "SECRET unrecognized payload"
        elif change != "malformed":
            key, value = {
                "attempt": ("attempt", 1),
                "bool_attempt": ("attempt", False),
                "plan_id": ("accepted_plan_id", "old-plan"),
                "plan_version": ("accepted_plan_version", 2),
                "plan_hash": ("accepted_plan_hash", "f" * 64),
                "specification": ("specification_hash", "f" * 64),
                "overlay": ("acceptance_decision_id", "old-acceptance"),
                "scope_tenant": ("org_id", "other"),
                "scope_flow": ("flow_id", "other"),
                "scope_node": ("node_id", "other"),
            }[change]
            payload["scope"][key] = value
        payload.update(owner="SECRET", required_input="SECRET", next_action="SECRET")
        row.reason = row.rejection_reason = "SECRET not-json" if change == "malformed" else json.dumps(payload)

    await refuse(session, target, rewrite=rewrite)
    result = card(app_with_router, flow, node)
    assert result["state"] == "ready" and result["delivery_progress"]["blocker"] is None
    assert result["delivery_progress"]["automation"] == "not_started"


async def test_fresh_scoped_observation_supersedes_legacy_without_unbounded_repeats(session, app_with_router, target):
    flow, node, _, _ = target
    legacy = {
        "action": "repository_evaluation_blocked",
        "block_code": "repository_evaluation_harness_changed",
        "next_action": "Recheck authenticated repository evidence on the next engine tick.",
        "owner": "evidence-producer",
        "required_input": "Provide successful evidence matching the accepted repository evaluation specification.",
    }
    await record(session, node, legacy, rejection=True)
    assert card(app_with_router, flow, node)["delivery_progress"]["blocker"] is None
    scoped = await refuse(session, target)
    await observe_repository_evaluation(session, node, provider=AsyncMock())
    rows = list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id)))
    assert len(rows) == 2
    assert card(app_with_router, flow, node)["delivery_progress"]["observed_at"] == scoped.created_at.isoformat()


async def test_current_plan_spec_attempt_or_pass_clears_old_refusal(session, app_with_router, target):
    flow, node, plan, _ = target
    await refuse(session, target)
    assert card(app_with_router, flow, node)["delivery_progress"]["blocker"]
    original = deepcopy(plan.plan_document)
    updated = deepcopy(original)
    updated["nodes"][0]["evaluation"]["max_age_seconds"] = 3600
    plan.plan_document = updated
    await session.flush()
    assert card(app_with_router, flow, node)["delivery_progress"]["blocker"] is None
    plan.plan_document = original
    node.attempts = 1
    await session.flush()
    assert card(app_with_router, flow, node)["delivery_progress"]["blocker"] is None
    node.attempts = 0
    node.state = "passed"
    await session.flush()
    result = card(app_with_router, flow, node)
    assert result["delivery_progress"]["stage"] == "complete" and result["delivery_progress"]["blocker"] is None


async def test_real_accepted_overlay_clears_before_another_tick(session, app_with_router, target, monkeypatch):
    flow, node, plan, spec = target
    await refuse(session, target)
    updated = deepcopy(spec)
    updated["runner"]["harness_sha256"] = harness_digest()
    actor = ApprovalContext(org_id=flow.org_id, actor_id=read.APPROVER, actor_role="org_admin", reason="Review the current evaluation specification")
    request = EvaluationAcceptanceRequest(
        node_id=node.id,
        expected_plan_version=plan.version,
        expected_plan_hash=plan.plan_hash,
        specification=updated,
        reason="Accept the reviewed installed evaluator harness",
    )
    preview = await preview_evaluation(session, flow_id=flow.id, actor=actor, request=request)
    request = request.model_copy(update={"expected_snapshot": preview["snapshot"]})
    accepted = await accept_evaluation(session, flow_id=flow.id, actor=actor, request=request)
    assert accepted["accepted"]
    result = card(app_with_router, flow, node)
    assert result["state"] == "ready" and result["delivery_progress"]["blocker"] is None
    assert result["delivery_progress"]["stage"] == "ready"
    # Reaccepting the same specification creates a new authority overlay. Its
    # predecessor's refusal cannot survive merely because the spec hash matches.
    monkeypatch.setattr("src.orchestration.dispatch_pass.resolve_installation_id", AsyncMock(return_value=None))
    await observe_repository_evaluation(session, node, provider=AsyncMock())
    assert card(app_with_router, flow, node)["delivery_progress"]["blocker"] == "repository_evaluation_installation_missing"
    request = request.model_copy(update={"reason": "Reaccept this exact specification after reviewing the connection"})
    preview = await preview_evaluation(session, flow_id=flow.id, actor=actor, request=request)
    replacement = await accept_evaluation(
        session,
        flow_id=flow.id,
        actor=actor,
        request=request.model_copy(update={"expected_snapshot": preview["snapshot"]}),
    )
    assert replacement["decision_id"] != accepted["decision_id"]
    assert card(app_with_router, flow, node)["delivery_progress"]["blocker"] is None


@pytest.mark.parametrize(
    "change",
    [
        None,
        "actor",
        "actor_kind",
        "cycle",
        "bool_cycle",
        "plan",
        "plan_hash",
        "spec",
        "overlay",
        "org",
        "flow",
        "node",
        "schema",
        "harness",
        "action",
        "malformed",
        "inconsistent_verdict",
        "older",
    ],
)
async def test_new_verified_result_supersedes_only_same_scope_refusal(session, app_with_router, target, monkeypatch, evidence, change):
    """Actual artifact predicates emit a false verdict; forged results cannot clear it."""
    flow, node, plan, _ = target
    updated = deepcopy(plan.plan_document)
    updated["nodes"][0]["evaluation"] = evidence.spec.model_dump(mode="json")
    updated["nodes"][0]["evaluation"]["runner"].update(repository="example/project", harness_sha256=harness_digest())
    plan.plan_document = updated
    await session.flush()
    monkeypatch.setattr("src.orchestration.dispatch_pass.resolve_installation_id", AsyncMock(return_value=None))
    provider = AsyncMock()
    await observe_repository_evaluation(session, node, provider=provider)
    before = card(app_with_router, flow, node)["delivery_progress"]
    assert before["blocker"] == "repository_evaluation_installation_missing"
    monkeypatch.setattr("src.orchestration.dispatch_pass.resolve_installation_id", AsyncMock(return_value=42))
    monkeypatch.setattr("src.orchestration.repository_evaluation.authorize", AsyncMock(return_value=(None, "0" * 64)))
    evidence.document["coverage"] = False
    evidence.binary = provider_fixtures.archive_for(json.dumps(evidence.document).encode())
    evidence.responses["/repos/o/r/actions/runs/10/artifacts"]["artifacts"][0]["digest"] = "sha256:" + hashlib.sha256(evidence.binary).hexdigest()
    provider.observe.return_value = await provider_fixtures.observe(evidence)
    assert provider.observe.return_value["mandatory_passed"] is False

    def rewrite(database, _context, _instances):
        for row in database.new:
            if not isinstance(row, OrchestrationDecision) or row.kind != "result_observed":
                continue
            payload = json.loads(row.reason)
            if change == "actor":
                row.actor_id = "other-service"
            elif change == "actor_kind":
                row.actor_kind = "human"
            elif change == "action":
                payload["action"] = "unknown"
            elif change == "older":
                row.created_at = WHEN - timedelta(seconds=1)
            elif change not in {None, "malformed"}:
                field, value = {
                    "cycle": ("cycle", 2),
                    "bool_cycle": ("cycle", True),
                    "plan": ("accepted_plan_version", 2),
                    "plan_hash": ("accepted_plan_hash", "f" * 64),
                    "spec": ("specification_hash", "f" * 64),
                    "overlay": ("acceptance_decision_id", "stale-overlay"),
                    "org": ("org_id", "other"),
                    "flow": ("flow_id", "other"),
                    "node": ("node_id", "other"),
                    "schema": ("evidence_schema", "unknown"),
                    "harness": ("harness_sha256", "f" * 64),
                    "inconsistent_verdict": ("mandatory_passed", True),
                }[change]
                payload["receipt"][field] = value
            row.reason = "SECRET malformed" if change == "malformed" else json.dumps(payload)

    event.listen(session.sync_session, "before_flush", rewrite)
    try:
        await observe_repository_evaluation(session, node, provider=provider)
    finally:
        event.remove(session.sync_session, "before_flush", rewrite)
    provider.observe.assert_awaited_once()
    assert node.state == "ready" and node.attempts == 0
    result = card(app_with_router, flow, node)["delivery_progress"]
    if change is not None:
        assert result == before
        return
    assert result["blocker"] == "repository_evaluation_criteria_failed"
    assert result["actor"] == "evidence-producer" and result["required_input"] and result["next_action"]
    assert result["observed_at"] == (WHEN + timedelta(seconds=1)).isoformat()
    session.add(
        OrchestrationDecision(
            org_id=flow.org_id,
            flow_id=flow.id,
            node_id=node.id,
            kind="result_observed",
            actor_id="other-service",
            actor_role="engine",
            actor_kind="service",
            reason="{}",
            created_at=WHEN + timedelta(seconds=50),
        )
    )
    await session.flush()
    assert card(app_with_router, flow, node)["delivery_progress"] == result
    # A newer authenticated refusal wins again, then an actual passing receipt
    # moves the node to PASSED and clears every old diagnostic.
    monkeypatch.setattr("src.orchestration.dispatch_pass.resolve_installation_id", AsyncMock(return_value=None))
    await observe_repository_evaluation(session, node, provider=provider)
    assert card(app_with_router, flow, node)["delivery_progress"]["blocker"] == "repository_evaluation_installation_missing"
    monkeypatch.setattr("src.orchestration.dispatch_pass.resolve_installation_id", AsyncMock(return_value=42))
    evidence.document["coverage"] = True
    evidence.binary = provider_fixtures.archive_for(json.dumps(evidence.document).encode())
    evidence.responses["/repos/o/r/actions/runs/10/artifacts"]["artifacts"][0]["digest"] = "sha256:" + hashlib.sha256(evidence.binary).hexdigest()
    provider.observe.return_value = await provider_fixtures.observe(evidence)
    assert provider.observe.return_value["mandatory_passed"] is True
    await observe_repository_evaluation(session, node, provider=provider)
    result = card(app_with_router, flow, node)
    assert result["state"] == "passed" and result["attempts"] == 1
    assert result["delivery_progress"]["stage"] == "complete" and result["delivery_progress"]["blocker"] is None
