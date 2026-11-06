"""Delivery status describes current evidence and real controller scheduling."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from src.orchestration.delivery_progress import node_progress, provider_progress
from src.orchestration.models import DecisionKind, OrchestrationAcceptedPlan, OrchestrationDecision
from src.orchestration.results import observe_results
from tests.orchestration import test_pr_binding_api as api
from tests.orchestration import test_read_api as read
from tests.orchestration.test_story_reconciliation import HEAD, PR_NODE, REPO_ID, StubSource, _bind, _green, _story

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


def test_passed_source_with_pending_deployment_still_shows_unfinished_execution():
    progress = node_progress(
        node=node(state="passed"),
        binding=binding(),
        dispatch={},
        result=None,
        execution=execution(phase="deployment_pending", progress_note="Deployment remains required"),
        policy_enabled=True,
        plan_version=3,
    )
    assert progress.stage == "deployment_pending" and progress.automation == "engine"
    assert progress.next_check_at and progress.detail == "Deployment remains required"


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


@pytest.mark.parametrize(
    "state,attempt,retry,stage,actor,detail",
    [
        (
            "ready",
            1,
            {"attempt": 1, "result": "scheduled", "reason": "stall_retry"},
            "retry",
            "engine",
            "The worker never acknowledged startup. Its assignment was fenced; the engine will retry within the accepted allowance.",
        ),
        (
            "failed",
            2,
            {"attempt": 2, "result": "failure_requires_operator", "detail": "worker vanished twice"},
            "failed",
            "operator",
            "Worker vanished twice; an operator must inspect this story before another attempt.",
        ),
    ],
)
async def test_graph_projects_fenced_stall_retry_and_repeat_operator_text(session, app_with_router, state, attempt, retry, stage, actor, detail):
    flow = await read.seed_flow(session)
    story = await read.seed_node(session, flow, node_ref="vanished-worker", state=state, attempts=attempt)
    session.add(
        OrchestrationDecision(
            org_id=story.org_id,
            flow_id=flow.id,
            node_id=story.id,
            kind="developer_retry_checked",
            actor_id="system:developer-recovery",
            actor_kind="service",
            actor_role="engine",
            reason=json.dumps(retry),
        )
    )
    await session.flush()

    response = read.client_for(app_with_router).get(read.route(flow.id))
    assert response.status_code == 200, response.text
    card = response.json()["nodes"][0]
    progress = card["delivery_progress"]
    assert card["state"] == state and card["attempts"] == attempt
    assert progress["stage"] == stage and progress["actor"] == actor
    assert progress["detail"] == detail
    if state == "ready":
        assert progress["automation"] == "engine" and progress["blocker"] is None
        assert progress["scheduled_action"] == "Dispatch the recovery attempt"
    else:
        assert progress["automation"] == "paused" and progress["blocker"] == "failure_requires_operator"
        assert progress["scheduled_action"] is None


@pytest.mark.parametrize("change", [None, "attempt", "plan", "state", "unknown"])
def test_evaluation_evidence_wait_is_current_and_typed(change):
    wait = {"attempt": 0, "plan_version": 3, "block_code": "verified_deployment_required", "observed_at": "2026-10-13T00:00:00Z"}
    target = node(kind="eval", state="ready", attempts=0)
    if change == "attempt":
        wait["attempt"] = 1
    elif change == "plan":
        wait["plan_version"] = 2
    elif change == "state":
        target.state = "awaiting_gate"
    elif change == "unknown":
        wait["block_code"] = "SECRET PROVIDER PAYLOAD"
    progress = node_progress(node=target, binding=None, dispatch={}, result={}, plan_version=3, evaluation_wait=wait)
    assert "SECRET" not in progress.model_dump_json()
    assert (progress.blocker == "verified_deployment_required") is (change is None)
    if change is None:
        assert progress.stage == "evidence_wait" and progress.actor == "flow-owner" and progress.next_action
        assert progress.observed_at == wait["observed_at"] and progress.automation == "blocked"
    assert progress.next_check_at is None and progress.scheduled_action is None


async def test_graph_projects_persisted_evaluation_refusal_and_wait(session, app_with_router):
    import json

    from src.orchestration.dispatch_pass import _admission_refused, _record_admission_refusal

    flow = await read.seed_flow(session)
    evaluation = await read.seed_node(session, flow, node_ref="evaluation", kind="eval", state="ready", attempts=0)
    await _record_admission_refusal(
        session,
        node_id=evaluation.id,
        org_id=evaluation.org_id,
        flow_id=flow.id,
        admission=_admission_refused("evaluation_specification_missing", owner="engine", required_input="SECRET", detail="SECRET"),
        scope={"attempt": 0, "accepted_plan_version": 0, "policy_hash": None},
    )
    client = read.client_for(app_with_router)
    response = client.get(read.route(flow.id))
    assert response.status_code == 200, response.text
    card = response.json()["nodes"][0]
    fixture = Path(__file__).resolve().parents[2] / "frontend/src/__tests__/fixtures/evaluation-refusal-node.json"
    expected = json.loads(fixture.read_text())
    assert {field: card[field] for field in expected if field != "delivery_progress"} == {
        field: value for field, value in expected.items() if field != "delivery_progress"
    }
    assert {field: card["delivery_progress"][field] for field in expected["delivery_progress"]} == expected["delivery_progress"]
    assert card["delivery_progress"]["stage"] == "admission"
    assert card["display_state"] == "stalled" and card["configuration_problem"] is None
    assert card["delivery_progress"]["blocker"] == "evaluation_specification_missing"
    assert card["delivery_progress"]["next_action"] and card["delivery_progress"]["next_check_at"] is None
    assert card["run_id"] is None and "SECRET" not in response.text

    session.add(
        OrchestrationDecision(
            org_id=evaluation.org_id,
            flow_id=flow.id,
            node_id=evaluation.id,
            kind="node_dispatched",
            actor_id="system:orchestration-dispatch",
            actor_role="engine",
            actor_kind="service",
            reason="{}",
        )
    )
    session.add(
        OrchestrationDecision(
            org_id=evaluation.org_id,
            flow_id=flow.id,
            node_id=evaluation.id,
            kind="evaluation_evidence_wait",
            actor_id="system:orchestration-tick",
            actor_role="engine",
            actor_kind="service",
            reason=json.dumps({"attempt": 0, "plan_version": 0, "block_code": "verified_deployment_required", "raw": "SECRET"}),
        )
    )
    await session.flush()
    response = client.get(read.route(flow.id))
    card = response.json()["nodes"][0]
    assert card["delivery_progress"]["stage"] == "evidence_wait"
    assert card["display_state"] == "stalled" and card["configuration_problem"] is None
    assert card["delivery_progress"]["blocker"] == "verified_deployment_required"
    assert card["delivery_progress"]["next_action"] and card["delivery_progress"]["next_check_at"] is None
    assert "SECRET" not in response.text

    session.add(
        OrchestrationDecision(
            org_id=evaluation.org_id,
            flow_id=flow.id,
            node_id=evaluation.id,
            kind="evaluation_evidence_ready",
            actor_id="system:orchestration-tick",
            actor_role="engine",
            actor_kind="service",
            reason="{}",
        )
    )
    await session.flush()
    card = client.get(read.route(flow.id)).json()["nodes"][0]
    assert card["delivery_progress"]["stage"] == "ready" and card["delivery_progress"]["blocker"] is None


async def test_graph_does_not_require_issue_for_ready_default_human_evaluation(session, app_with_router):
    flow = await read.seed_flow(session)
    evaluation = await read.seed_node(session, flow, node_ref="default-human", kind="eval", state="ready")
    await read.accept_policy(session, flow)
    plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == flow.id))
    plan.plan_document = {**plan.plan_document, "nodes": [{"address": read.address_of(evaluation), "kind": "eval"}]}
    await session.flush()

    client = read.client_for(app_with_router)
    response = client.get(read.route(flow.id))
    assert response.status_code == 200, response.text
    card = response.json()["nodes"][0]
    assert card["state"] == "ready" and card["issue_ref"] is None
    assert card["configuration_problem"] is None
    assert card["delivery_progress"]["stage"] == "ready"
    assert card["delivery_progress"]["automation"] == "not_started"
    assert card["run_id"] is None

    legacy = await read.seed_flow(session, slug="legacy-evaluation")
    await read.seed_node(session, legacy, node_ref="legacy", kind="eval", state="ready")
    response = client.get(read.route(legacy.id))
    assert response.status_code == 200, response.text
    assert response.json()["nodes"][0]["configuration_problem"] == ("Link an evaluation issue in the plan before this evaluation can run.")


@pytest.mark.parametrize(
    "outcome",
    ["retry_backoff", "scheduled", "attempts_exhausted", "flow_deadline_exhausted", "operations_input_required", "deterministic_failure_unchanged"],
)
def test_developer_retry_status_explains_automation_and_blockers(outcome):
    progress = node_progress(node=node(state="failed"), binding=None, dispatch={}, result={}, developer_retry={"attempt": 1, "result": outcome})
    if outcome in {"retry_backoff", "scheduled"}:
        assert progress.actor == "engine" and progress.scheduled_action
        assert progress.stage == "retry"
    else:
        assert progress.actor == "operator" and progress.blocker == outcome
        assert progress.scheduled_action is None


def test_input_hold_directs_operator_to_diagnosis_and_resume():
    progress = node_progress(
        node=node(state="failed"),
        binding=None,
        dispatch={},
        result={},
        developer_retry={"attempt": 1, "result": "operations_input_required"},
    )
    assert progress.automation == "paused"
    assert "task board" in progress.next_action and "resume" in progress.next_action
    assert "Automatic retries cannot supply it" in progress.detail


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


def test_undispatched_story_under_accepted_policy_is_not_started_rather_than_unconfigured():
    """A queued story in an accepted autonomous flow must not read as unautomated (#6850 flow UI)."""
    progress = node_progress(node=node(state="pending", attempts=0), binding=None, dispatch={}, result={}, policy_enabled=True, plan_version=2)
    assert progress.stage == "pending" and progress.automation == "not_started"
    assert progress.blockers == [] and progress.scheduled_action is None and progress.next_check_at is None


def test_undispatched_story_without_accepted_policy_stays_not_configured():
    progress = node_progress(node=node(state="pending", attempts=0), binding=None, dispatch={}, result={}, policy_enabled=False)
    assert progress.stage == "pending" and progress.automation == "not_configured"


def test_dispatched_story_without_execution_is_engine_owned_only_under_policy():
    enabled = node_progress(node=node(state="running"), binding=None, dispatch={}, result={}, policy_enabled=True, plan_version=2)
    legacy = node_progress(node=node(state="running"), binding=None, dispatch={}, result={}, policy_enabled=False)
    assert enabled.stage == legacy.stage == "development"
    assert enabled.automation == "engine" and legacy.automation == "not_configured"


def test_awaiting_merge_without_execution_under_policy_is_not_initialized_rather_than_unconfigured():
    bound = binding()
    result = observation(bound, provider_progress(bound, evidence()))
    progress = node_progress(node=node(), binding=bound, dispatch={}, result=result, policy_enabled=True, plan_version=2)
    assert progress.blockers == ["execution_not_initialized"] and progress.automation == "not_initialized"


async def test_graph_names_the_unresolved_required_inputs_that_park_a_ready_node(session, app_with_router):
    """The dispatcher's silent `undispatchable` refusal is visible on the card (#7759)."""
    flow = await read.seed_flow(session)
    upstream = await read.seed_node(session, flow, node_ref="x01", kind="story", state="ready")
    parked = await read.seed_node(session, flow, node_ref="x02", kind="story", state="ready")
    await read.accept_policy(session, flow)
    plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == flow.id))
    plan.plan_document = {
        **plan.plan_document,
        "nodes": [
            {"address": read.address_of(upstream), "kind": "story", "title": "upstream"},
            {
                "address": read.address_of(parked),
                "kind": "story",
                "title": "parked",
                "required_inputs": [
                    {"name": "platform release", "source": f"node:{read.address_of(upstream)}"},
                    {"name": "deployment branch", "source": "deployment_branch:main"},
                ],
            },
        ],
    }
    await session.flush()

    # A pending successor with the same unresolved pins is held by the tick for the
    # same reason and must say so too (tools flow TLS-X02-door, 2026-10-15).
    held = await read.seed_node(session, flow, node_ref="x02b", kind="story", state="pending")
    plan.plan_document = {
        **plan.plan_document,
        "nodes": [
            *plan.plan_document["nodes"],
            {
                "address": read.address_of(held),
                "kind": "story",
                "title": "held",
                "required_inputs": [{"name": "deployment branch", "source": "deployment_branch:main"}],
            },
        ],
    }
    await session.flush()

    client = read.client_for(app_with_router)
    cards = {card["node_ref"]: card for card in client.get(read.route(flow.id)).json()["nodes"]}
    assert cards["x01"]["configuration_problem"] is None
    assert "deployment branch (deployment_branch:main)" in cards["x02b"]["configuration_problem"]
    problem = cards["x02"]["configuration_problem"]
    assert problem.startswith("Required inputs are not available yet, so the dispatcher will not start this node:")
    assert f"platform release (node:{read.address_of(upstream)})" in problem and "deployment branch (deployment_branch:main)" in problem

    # Once the inputs are satisfied the card stops reporting a problem; a running node never does.
    upstream.state = "passed"
    plan.plan_document = {
        **plan.plan_document,
        "nodes": [
            plan.plan_document["nodes"][0],
            {
                **plan.plan_document["nodes"][1],
                "required_inputs": [
                    {"name": "platform release", "source": f"node:{read.address_of(upstream)}"},
                    {"name": "deployment branch", "source": "deployment_branch:main"},
                    {"name": "source", "source": f"deployment_source_revision:{'a' * 40}"},
                    {"name": "definition", "source": f"deployment_definition_revision:{'b' * 40}"},
                ],
            },
        ],
    }
    await session.flush()
    cards = {card["node_ref"]: card for card in client.get(read.route(flow.id)).json()["nodes"]}
    assert cards["x02"]["configuration_problem"] is None


@pytest.mark.parametrize("code", ["operations_release_unverified", "operations_release_recovery_required", "operations_release_changed"])
@pytest.mark.parametrize("stale", [False, True])
def test_current_release_refusal_overrides_queued_retry(code, stale):
    from src.orchestration.admission_diagnostics import CONTRACT

    refusal = {
        "contract": CONTRACT,
        "attempt": 0 if stale else 1,
        "accepted_plan_version": 3,
        "policy_hash": "a" * 64,
        "block_code": code,
        "detail": "SECRET PROVIDER PAYLOAD",
        "observed_at": "2026-10-21T12:27:55Z",
    }
    progress = node_progress(
        node=node(state="ready"),
        binding=None,
        dispatch={},
        result={},
        policy_enabled=True,
        plan_version=3,
        policy_hash="a" * 64,
        admission_refusal=refusal,
        developer_retry={"attempt": 1, "result": "scheduled", "reason": "operations_runtime_unverified"},
    )
    assert "SECRET" not in progress.model_dump_json()
    if stale:
        assert progress.stage == "retry" and progress.blocker is None
    else:
        assert progress.stage == "admission" and progress.blocker == code
        assert progress.automation == "blocked" and progress.scheduled_action is None


def test_new_safe_admission_reason_remains_visible_without_a_frontend_release():
    from src.orchestration.admission_diagnostics import CONTRACT
    from src.orchestration.validation_diagnostics import with_validation_checks

    target = node(state="ready", attempts=0)
    progress = node_progress(
        node=target,
        binding=None,
        dispatch={},
        result={},
        policy_enabled=True,
        plan_version=3,
        policy_hash="a" * 64,
        admission_refusal={
            "contract": CONTRACT,
            "attempt": 0,
            "accepted_plan_version": 3,
            "policy_hash": "a" * 64,
            "block_code": "new_provider_requirement",
            "detail": "SECRET PROVIDER PAYLOAD",
        },
    )
    result = with_validation_checks(progress, checks=[])
    assert result.blocker == "new_provider_requirement"
    assert len(result.validations) == 1
    assert result.validations[0].next_action
    assert "SECRET" not in result.model_dump_json()


def test_release_diagnostic_exposes_underlying_check_and_dependency_wait():
    from src.orchestration.admission_diagnostics import CONTRACT
    from src.orchestration.validation_diagnostics import with_validation_checks

    progress = node_progress(
        node=node(state="ready", attempts=0),
        binding=None,
        dispatch={},
        result={},
        policy_enabled=True,
        plan_version=3,
        policy_hash="a" * 64,
        admission_refusal={
            "contract": CONTRACT,
            "attempt": 0,
            "accepted_plan_version": 3,
            "policy_hash": "a" * 64,
            "block_code": "operations_release_unverified",
            "detail": "operations_release_ancestry_missing_or_ambiguous",
        },
    )
    result = with_validation_checks(progress, checks=[], configuration_problem="Waiting for the implementation merge.")
    assert any(check.code == "operations_release_ancestry_missing_or_ambiguous" for check in result.validations)
    assert any(check.status == "waiting" and check.code == "required_inputs_unavailable" for check in result.validations)
    assert result.next_action == "Finish the predecessor merges and verify their commit ancestry."
