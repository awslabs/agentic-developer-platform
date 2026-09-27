"""Delivery status describes current evidence and real controller scheduling."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from src.orchestration.delivery_progress import node_progress, provider_progress
from src.orchestration.models import DecisionKind, OrchestrationDecision
from src.orchestration.results import observe_results
from tests.orchestration import test_pr_binding_api as api
from tests.orchestration import test_read_api as read
from tests.orchestration.test_story_reconciliation import HEAD, PR_NODE, REPO, REPO_ID, StubSource, _bind, _green, _story

session = read.session
app_with_router = read.app_with_router
providers = api.providers


def binding(**updates):
    return SimpleNamespace(
        **{
            "id": "binding-1",
            "attempt": 1,
            "revision": 1,
            "run_id": "run-1",
            "head_sha": HEAD,
            "provider_repository_id": REPO_ID,
            "provider_pr_node_id": PR_NODE,
            "registered_by_kind": "service",
            "recovery_reason": None,
            **updates,
        }
    )


def node(**updates):
    return SimpleNamespace(
        **{"id": "node-1", "org_id": "org-1", "flow_id": "flow-1", "attempts": 1, "kind": "story", "state": "awaiting_merge", **updates}
    )


def evidence(**updates):
    return replace(_green(), merged=False, mergeable="MERGEABLE", merge_state="CLEAN", **updates)


def observation(bound, diagnostic):
    return {"attempt": bound.attempt, "binding": {"id": bound.id, "revision": bound.revision}, "delivery_progress": diagnostic}


def execution(**updates):
    now = datetime(2026, 9, 21, tzinfo=UTC)
    return SimpleNamespace(
        **{
            "node_id": "node-1",
            "org_id": "org-1",
            "flow_id": "flow-1",
            "cycle": 1,
            "accepted_plan_version": 3,
            "phase": "awaiting_review",
            "status": "runnable",
            "next_check_at": now + timedelta(minutes=1),
            "progressed_at": now,
            "block_owner": None,
            "block_code": None,
            "block_detail": None,
            "block_required_input": None,
            "progress_note": "Review is pending",
            **updates,
        }
    )


@pytest.mark.parametrize(
    "updates,blocker,actor",
    [
        ({"checks_successful": False, "checks_state": "FAILURE"}, "ci_failed", "developer"),
        ({"checks_successful": False, "checks_state": "PENDING"}, "ci_pending", "github"),
        ({"checks_successful": False, "checks_state": "MISSING"}, "ci_missing", "developer"),
        ({"review_approved": False, "review_state": "changes_requested"}, "changes_requested", "developer"),
        ({"review_approved": False, "review_state": "stale"}, "review_stale", "reviewer"),
        ({"review_approved": False, "review_state": "missing"}, "review_required", "reviewer"),
        ({"review_approved": False, "review_state": "unverified"}, "review_unverified", "reviewer"),
        ({"head_sha": "f" * 40}, "head_changed", "developer"),
        ({"provider_pr_node_id": "wrong-pr"}, "pr_identity_changed", "operator"),
        ({"draft": True}, "draft", "developer"),
    ],
)
def test_provider_blockers_are_distinct_and_have_responsible_actor(updates, blocker, actor):
    progress = provider_progress(binding(), evidence(**updates))
    assert progress["blocker"] == blocker and progress["actor"] == actor
    assert progress["next_action"] and progress["next_check_at"] is None and progress["scheduled_action"] is None


def test_green_merge_ready_does_not_invent_automatic_merge_or_schedule():
    bound = binding()
    diagnosis = provider_progress(bound, evidence())
    assert diagnosis["stage"] == "merge" and diagnosis["blocker"] is None
    progress = node_progress(node=node(), binding=bound, dispatch={}, result=observation(bound, diagnosis))
    assert progress.blockers == ["automation_not_configured"] and progress.actor == "operator"
    assert progress.scheduled_action is None and progress.next_check_at is None


def test_provider_outage_and_incomplete_merge_never_look_ready():
    assert provider_progress(binding(), None)["blocker"] == "provider_unavailable"
    assert provider_progress(binding(), replace(_green(), merge_commit_sha=None))["blocker"] == "merge_evidence_incomplete"


@pytest.mark.parametrize("updates", [{"cycle": 2}, {"accepted_plan_version": 2}, {"org_id": "other"}, {"node_id": "other"}])
def test_obsolete_execution_never_overrides_current_attempt_or_invents_schedule(updates):
    progress = node_progress(
        node=node(), binding=binding(), dispatch={}, result={}, execution=execution(**updates), policy_enabled=True, plan_version=3
    )
    assert progress.blocker == "execution_stale" and progress.next_check_at is None


def test_current_durable_execution_uses_exact_stored_reconciliation_schedule():
    row = execution(status="awaiting_external")
    progress = node_progress(node=node(), binding=binding(), dispatch={}, result={}, execution=row, policy_enabled=True, plan_version=3)
    assert progress.stage == "awaiting_review" and progress.actor == "reviewer"
    assert progress.next_check_at == row.next_check_at.isoformat()
    assert progress.scheduled_action == "Reconcile this execution"
    assert progress.observed_at == row.progressed_at.isoformat()


@pytest.mark.parametrize("status", ["concluded", "superseded"])
def test_terminal_execution_has_no_schedule_even_if_database_retains_a_due_time(status):
    progress = node_progress(
        node=node(), binding=binding(), dispatch={}, result={}, execution=execution(status=status), policy_enabled=True, plan_version=3
    )
    assert progress.next_check_at is None and progress.scheduled_action is None


@pytest.mark.parametrize("state", ["halted", "failed", "rejected_at_gate", "awaiting_gate", "superseded"])
def test_human_hold_wins_over_any_execution_schedule(state):
    progress = node_progress(
        node=node(state=state), binding=binding(), dispatch={}, result={}, execution=execution(), policy_enabled=True, plan_version=3
    )
    assert progress.stage == state and progress.next_check_at is None and progress.automation == "paused"


def test_obsolete_provider_snapshot_loses_its_diagnosis_and_timestamp():
    old = binding(revision=1)
    result = observation(old, provider_progress(old, evidence(checks_successful=False, checks_state="FAILURE")))
    progress = node_progress(node=node(), binding=binding(revision=2), dispatch={}, result=result, observed_at="2026-09-21T00:00:00Z")
    assert progress.blocker == "evidence_not_observed" and "ci_failed" not in progress.blockers and progress.observed_at is None


def test_malformed_provider_snapshot_falls_back_without_breaking_graph():
    bound = binding()
    result = observation(bound, {"invalid": True})
    progress = node_progress(node=node(), binding=bound, dispatch={}, result=result)
    assert progress.blocker == "evidence_not_observed"


async def test_graph_updates_ci_to_provider_outage_and_dates_actual_observation(session, app_with_router, providers):
    story, dispatch = await _story(session, binding_marker=True)
    await _bind(session, story, dispatch)

    class Store:
        def get(self, *_):
            return {"tenant_id": story.org_id, "engine_node_id": story.id, "engine_attempt": 1, "status": "complete"}

    source = StubSource(evidence=evidence(checks_successful=False, checks_state="FAILURE"))
    await observe_results(session, run_store=Store(), evidence=source)
    client = read.client_for(app_with_router)
    progress = client.get(read.route(story.flow_id)).json()["nodes"][0]["delivery_progress"]
    assert progress["blocker"] == "ci_failed" and progress["next_check_at"] is None
    source.bound_pull_request = api.AsyncMock(side_effect=RuntimeError("provider unavailable"))
    await observe_results(session, run_store=Store(), evidence=source)
    observed = (
        await session.scalars(
            select(OrchestrationDecision)
            .where(
                OrchestrationDecision.node_id == story.id,
                OrchestrationDecision.kind == DecisionKind.RESULT_OBSERVED.value,
            )
            .order_by(OrchestrationDecision.created_at.desc())
        )
    ).first()
    session.add(
        OrchestrationDecision(
            org_id=story.org_id,
            flow_id=story.flow_id,
            node_id=story.id,
            kind=DecisionKind.RESULT_CHECKED.value,
            actor_id="engine",
            actor_kind="service",
            actor_role="engine",
            reason="Checked attempt 1",
            created_at=observed.created_at + timedelta(days=1),
        )
    )
    await session.flush()
    progress = client.get(read.route(story.flow_id)).json()["nodes"][0]["delivery_progress"]
    assert progress["blocker"] == "provider_unavailable" and "ci_failed" not in progress["blockers"]
    assert datetime.fromisoformat(progress["observed_at"]).replace(tzinfo=None) == observed.created_at.replace(tzinfo=None)


async def test_historical_adoption_shows_dependency_hold_without_missing_worker(session, app_with_router, providers, monkeypatch):
    from src.orchestration.models import OrchestrationEdge, OrchestrationNode
    from tests.orchestration.test_delivery_adoption import NoWorker, adopt, story

    monkeypatch.setenv("BG_ORCH_DISPATCH_REPO", REPO)
    monkeypatch.setattr("src.orchestration.delivery_adoption.resolve_installation_id", api.AsyncMock(return_value=4242))
    adopted = await story(session)
    await adopt(session, adopted)
    gate = OrchestrationNode(
        org_id=adopted.org_id,
        flow_id=adopted.flow_id,
        epic_ref="epic",
        wave_ref="wave",
        node_ref="gate",
        kind="gate",
        state="awaiting_gate",
        title="Approve",
    )
    session.add(gate)
    await session.flush()
    session.add(OrchestrationEdge(org_id=adopted.org_id, flow_id=adopted.flow_id, from_node_id=gate.id, to_node_id=adopted.id))
    await session.flush()
    await observe_results(session, run_store=NoWorker(), evidence=StubSource(evidence=_green()))
    card = next(item for item in read.client_for(app_with_router).get(read.route(adopted.flow_id)).json()["nodes"] if item["id"] == adopted.id)
    progress = card["delivery_progress"]
    assert progress["stage"] == "historical_delivery" and progress["blocker"] == "predecessor_pending"
    assert progress["actor"] == "operator"
    assert progress["automation"] == "reconciliation_only" and progress["next_check_at"] is None
    assert card["run_id"] is None and card["attempts"] == 0


@pytest.mark.parametrize("change", [None, "attempt", "plan", "policy", "running", "malformed", "unknown_code"])
def test_admission_status_is_scoped_to_current_ready_attempt_and_policy(change):
    from src.orchestration.admission_diagnostics import CONTRACT

    refusal = {
        "contract": CONTRACT,
        "attempt": 0,
        "accepted_plan_version": 3,
        "policy_hash": "a" * 64,
        "block_code": "budget_unavailable",
        "detail": "SECRET PROVIDER PAYLOAD",
        "observed_at": "2026-09-21T12:00:00Z",
    }
    target = node(state="ready", attempts=0)
    if change in {"attempt", "plan", "policy"}:
        refusal[{"attempt": "attempt", "plan": "accepted_plan_version", "policy": "policy_hash"}[change]] = -1
    elif change == "running":
        target.state = "running"
    elif change == "malformed":
        refusal = {"broken": True}
    elif change == "unknown_code":
        refusal["block_code"] = "SECRET PROVIDER PAYLOAD"
    progress = node_progress(
        node=target,
        binding=None,
        dispatch={},
        result={},
        policy_enabled=True,
        plan_version=3,
        policy_hash="a" * 64,
        admission_refusal=refusal,
    )
    assert "SECRET" not in progress.model_dump_json()
    assert (progress.blocker == "budget_unavailable") is (change is None)
    if change is None:
        assert progress.actor == "platform-operator" and progress.next_action and progress.observed_at == refusal["observed_at"]
    assert progress.next_check_at is None and progress.scheduled_action is None


async def test_graph_reads_durable_admission_and_discards_it_after_dispatch(session, app_with_router):
    import json

    from src.orchestration.dispatch_pass import _admission_refused, _record_admission_refusal

    flow = await read.seed_flow(session)
    story = await read.seed_node(session, flow, node_ref="queued", state="ready", attempts=0)
    await _record_admission_refusal(
        session,
        node_id=story.id,
        org_id=story.org_id,
        flow_id=flow.id,
        admission=_admission_refused("budget_unavailable", owner="platform-operator", required_input="Restore meter", detail="SECRET"),
        scope={"attempt": 0, "accepted_plan_version": 0, "policy_hash": None},
    )
    client = read.client_for(app_with_router)
    response = client.get(read.route(flow.id))
    assert response.status_code == 200, response.text
    card = response.json()["nodes"][0]
    assert card["attempts"] == 0 and card["run_id"] is None
    assert card["delivery_progress"]["stage"] == "admission"
    assert card["delivery_progress"]["blocker"] == "budget_unavailable"
    assert card["delivery_progress"]["actor"] == "platform-operator"
    assert "SECRET" not in response.text
    session.add(
        OrchestrationDecision(
            org_id=story.org_id,
            flow_id=flow.id,
            node_id=story.id,
            kind="node_dispatched",
            actor_id="system:orchestration-dispatch",
            actor_kind="service",
            actor_role="engine",
            reason=json.dumps({"attempt": 1, "run_id": "started"}),
        )
    )
    # Even if the node is now ready again, the earlier refusal is historical.
    await session.flush()
    card = client.get(read.route(flow.id)).json()["nodes"][0]
    assert card["delivery_progress"]["blocker"] is None


@pytest.mark.parametrize("outcome", ["retry_backoff", "scheduled", "attempts_exhausted", "flow_deadline_exhausted"])
def test_developer_retry_status_explains_automation_and_blockers(outcome):
    progress = node_progress(node=node(state="failed"), binding=None, dispatch={}, result={}, developer_retry={"attempt": 1, "result": outcome})
    if outcome in {"retry_backoff", "scheduled"}:
        assert progress.actor == "engine" and progress.scheduled_action
        assert progress.stage == "retry"
    else:
        assert progress.actor == "operator" and progress.blocker == outcome
        assert progress.scheduled_action is None


def test_prior_attempt_retry_diagnostic_cannot_claim_current_automation():
    progress = node_progress(
        node=node(state="failed", attempts=2), binding=None, dispatch={}, result={}, developer_retry={"attempt": 1, "result": "scheduled"}
    )
    assert progress.actor == "operator" and progress.scheduled_action is None


def test_unavailable_retry_evidence_is_rechecked_without_requesting_approval():
    progress = node_progress(
        node=node(state="failed"), binding=None, dispatch={}, result={}, developer_retry={"attempt": 1, "result": "recovery_evidence_unavailable"}
    )
    assert progress.actor == "engine"
    assert progress.automation == "reconciliation_only"
    assert progress.scheduled_action == "Recheck recovery eligibility"
