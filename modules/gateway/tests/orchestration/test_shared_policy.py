"""Shared-role continuation uses real SQL identity and one Redis model allowance."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
from sqlalchemy import delete

from src.budget.config import budget_config
from src.budget.reservations import ReservationStore
from src.orchestration import flow_budget, shared_policy
from src.orchestration.execution_policy import Action, DenyReason
from src.orchestration.flow_meter import meter_target
from src.orchestration.models import OrchestrationDecision, OrchestrationWorkClaim
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.run_reports import OrchestrationRunReport
from src.shared.models.onboarding import TenantMembership
from tests.orchestration.test_policy_admission import (
    APPROVER,
    INSTALLATION_A,
    REPO,
    _accept_policy,
    _limits,
    _make_approval,
    _make_flow,
    _make_member,
    _make_node,
    _make_org,
    _policy,
)
from tests.orchestration.test_policy_admission import (
    engine as engine_fixture,
)
from tests.orchestration.test_policy_admission import (
    session as session_fixture,
)

engine = engine_fixture
session = session_fixture

ROLE = "arn:aws:iam::123456789012:role/shared-agent"


@pytest.fixture
async def shared(session, monkeypatch):
    for key, value in {
        "ADP_SHARED_WORKER_CONTINUATION_ENABLED": "true",
        "AGENT_WORKER_ROLE_ARN": ROLE,
        "AGENT_AUTHORITY_ENABLED": "false",
        "BUDGET_ENFORCEMENT_ENABLED": "true",
        "ADP_WORK_CLAIMS_ENABLED": "true",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(budget_config, "budget_reservation_enabled", True)
    monkeypatch.setattr(budget_config, "budget_run_cap_usd", Decimal(5))
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = ReservationStore(redis_url=None, ttl_seconds=120, client=client)
    monkeypatch.setattr(flow_budget, "_reservations", store)
    await _make_org(session)
    await _make_member(session)
    flow = await _make_flow(session)
    actions = [Action.DEVELOP, Action.REPAIR, Action.REVIEW, Action.MERGE]
    policy = _policy(
        schema_version=2,
        allowed_actions=actions,
        limits=_limits(max_concurrent_actions=1),
        user_credentials={"permission_mode": "user_configured", "lifetime": "provider_managed", "aws_role_arns": [ROLE], "actions": actions},
    )
    plan = await _accept_policy(session, flow, policy)
    decision = OrchestrationDecision(
        org_id=flow.org_id, flow_id=flow.id, kind="plan_accepted", actor_id=APPROVER, actor_role="org_admin", actor_kind="human", reason="accepted"
    )
    session.add(decision)
    await session.flush()
    plan.accepted_by_decision_id = decision.id
    marker = {
        "contract_version": 1,
        "mode": "shared_worker_role",
        "accepted_at": datetime.now(UTC).isoformat(),
        "prior_spend_usd": "20",
        "worker_role_arn": ROLE,
        "prior_attempts": {},
        "initial_runs": {},
        "budget_scope": "authenticated_gateway_calls",
    }
    plan.plan_document = {**plan.plan_document, "execution_continuation": marker}
    node = await _make_node(session, flow)
    claim = OrchestrationWorkClaim(
        org_id=flow.org_id,
        provider_repository_id=12345,
        issue_number=int(node.issue_ref),
        owner_kind="engine_flow",
        owner_ref=flow.id,
        active_run_id="run-current",
        state="held",
        generation=1,
    )
    session.add(claim)
    await session.flush()
    assert await shared_policy.initialize_shared_meter(org_id=flow.org_id, flow_id=flow.id, policy=policy, marker=marker)
    yield SimpleNamespace(
        session=session,
        flow=flow,
        plan=plan,
        marker=marker,
        node=node,
        claim=claim,
        policy=policy,
        client=client,
        store=store,
        target=meter_target(org_id=flow.org_id, flow_id=flow.id, policy=policy),
    )
    await client.aclose()


async def dispatch(s, **overrides):
    args = dict(node=s.node, principal_user_id=APPROVER, target_repository=REPO, provider_repository_id=12345, expected_invocation_id="run-current")
    args.update(overrides)
    return await shared_policy.authorize_shared_dispatch(s.session, **args)


async def report(s, node, run_id):
    row = OrchestrationRunReport(
        run_id=run_id,
        credential_hash=run_id.ljust(64, "0"),
        org_id=node.org_id,
        flow_id=node.flow_id,
        node_id=node.id,
        attempt=node.attempts,
        persona="developer",
        repo=REPO,
        installation_id=INSTALLATION_A,
        provider_repository_id=12345,
        dispatch_metadata={},
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    s.session.add(row)
    await s.session.flush()
    return row


async def test_historical_spend_shared_by_parallel_descendants(shared):
    assert (await shared.store.snapshot(shared.target)).total_usd == 20
    outcomes = await asyncio.gather(*(shared.store.reserve(run, Decimal(20), [shared.target]) for run in ("developer", "reviewer", "repair")))
    assert sum(result.admitted for result in outcomes) == 1
    assert (await shared.store.snapshot(shared.target)).total_usd == 40


async def test_initial_dispatch_uses_shared_role_and_current_claim(shared):
    assert (await dispatch(shared)).permitted
    assert shared.node.attempts == 0
    shared.claim.active_run_id = "superseding-run"
    await shared.session.flush()
    assert (await dispatch(shared)).reason == DenyReason.WORK_NOT_OWNED


@pytest.mark.parametrize("evidence", ["failed", "passed", "running", "missing", "stale", "successor", "unknown_meter", "wrong_receipt", "other_flow"])
async def test_finished_story_releases_only_its_admission_hold(shared, monkeypatch, evidence):
    # $20 historical usage + one $20 finished hold leaves no room for a second
    # $20 action under the real $50 policy. Terminal evidence must release only
    # the redundant admission hold, never the model-spend accumulator.
    monkeypatch.setattr(budget_config, "budget_run_cap_usd", Decimal(20))
    old = await _make_node(
        shared.session, shared.flow, node_ref="finished", state="running" if evidence == "running" else "failed", attempts=1, issue_ref="999"
    )
    if evidence == "passed":
        old.state = "passed"
    if evidence != "missing":
        receipt = await report(shared, old, "finished-worker")
        receipt.terminal_receipt = {
            "contract_version": 1,
            "run_id": receipt.run_id,
            "attempt": 1,
            "outcome": "complete" if evidence == "passed" else "failed",
        }
        if evidence == "stale":
            old.attempts = 2
        if evidence == "successor":
            await report(shared, old, "pending-successor")
        if evidence == "wrong_receipt":
            receipt.terminal_receipt = {**receipt.terminal_receipt, "run_id": "someone-else"}
        if evidence == "other_flow":
            receipt.flow_id = "another-flow"
    await shared.session.flush()
    assert (
        await flow_budget.reserve_flow_admission(
            org_id=old.org_id, flow_id=old.flow_id, policy=shared.policy, settled_usd=Decimal(20), node_id=old.id
        )
    ).admitted
    admission = flow_budget.flow_reservation_target(org_id=old.org_id, flow_id=old.flow_id, policy=shared.policy, settled_usd=Decimal(20))
    original_models = await shared.client.hgetall(shared.target.key())
    if evidence == "unknown_meter":
        assert (await shared.store.reserve("unsettled-provider", Decimal(1), [shared.target])).admitted
        await shared.store.mark_unknown("unsettled-provider", shared.target)
        original_models = await shared.client.hgetall(shared.target.key())
    decision = await dispatch(shared)
    assert decision.permitted == (evidence in {"failed", "passed"})
    holds = await shared.client.hgetall(admission.key())
    old_amount = Decimal(holds[flow_budget.admission_request_id(old.id)].split(":", 1)[0])
    assert old_amount == (0 if decision.permitted else 20)
    assert await shared.client.hgetall(shared.target.key()) == original_models


async def test_lost_meter_never_reinitializes_from_accepted_plan(shared):
    await shared.client.flushdb()
    assert (await dispatch(shared)).reason == DenyReason.BUDGET_UNAVAILABLE
    assert await shared.store.snapshot(shared.target) is None


async def test_truncated_meter_cannot_forget_historical_spend(shared):
    await shared.client.hdel(shared.target.key(), "__historical_spend__")
    assert (await dispatch(shared)).reason == DenyReason.BUDGET_UNAVAILABLE


async def test_historical_attempts_consume_attempt_limit(shared):
    shared.node.attempts = 3
    await shared.session.flush()
    assert (await dispatch(shared)).reason == DenyReason.ATTEMPT_LIMIT_EXCEEDED


async def test_current_run_does_not_consume_second_concurrency_slot(shared):
    shared.node.state = "running"
    shared.node.attempts = 1
    await report(shared, shared.node, "run-current")
    context = SimpleNamespace(
        identity=SimpleNamespace(
            org_id=shared.node.org_id, node_id=shared.node.id, accepted_plan_version=1, claim_id=shared.claim.id, claim_generation=1
        ),
        execution=SimpleNamespace(attempts=1),
    )
    binding = SimpleNamespace(
        org_id=shared.node.org_id,
        node_id=shared.node.id,
        repo=REPO,
        provider_repository_id=12345,
        accepted_scope=json.dumps({"node": {"kind": shared.node.kind, "issue_ref": shared.node.issue_ref, "title": shared.node.title}}),
    )
    assert await shared_policy.authorize_shared_action(shared.session, context, shared.node, binding, "run-current", Action.REPAIR)
    other = await _make_node(shared.session, shared.flow, node_ref="other", state="running", attempts=1, issue_ref="999")
    await report(shared, other, "run-other")
    with pytest.raises(CycleBlockedError, match="concurrency_limit_exceeded"):
        await shared_policy.authorize_shared_action(shared.session, context, shared.node, binding, "run-current", Action.REPAIR)
    # Full capacity cannot prevent observing/reconciling the existing execution,
    # but it still prevents either a fresh successor or an admission replay.
    assert await shared_policy.authorize_shared_action(shared.session, context, shared.node, binding, "run-current", Action.REPAIR, observation=True)
    with pytest.raises(CycleBlockedError, match="concurrency_limit_exceeded"):
        await shared_policy.authorize_shared_action(
            shared.session, context, shared.node, binding, "run-current", Action.REPAIR, observation=True, reserve=True
        )
    shared.claim.active_run_id = "superseded"
    await shared.session.flush()
    with pytest.raises(CycleBlockedError, match="active_claim_changed"):
        await shared_policy.authorize_shared_action(shared.session, context, shared.node, binding, "run-current", Action.REPAIR, observation=True)


async def test_reconciled_legacy_excluded_but_new_attempt_counted(shared):
    old = await _make_node(shared.session, shared.flow, node_ref="old", state="running", attempts=2, issue_ref="999")
    initial = {old.id: {"attempt": 2, "evidence_origin": "owner_reconciled_legacy_delivery"}}
    assert await shared_policy._active_count(shared.session, org_id=shared.flow.org_id, flow_id=shared.flow.id, initial_runs=initial) == 0
    old.attempts = 3
    await shared.session.flush()
    assert await shared_policy._active_count(shared.session, org_id=shared.flow.org_id, flow_id=shared.flow.id, initial_runs=initial) == 1


@pytest.mark.parametrize("mutation", ["role", "acceptance", "budget_scope", "future", "clock"])
async def test_acceptance_facts_remain_verifiable(shared, monkeypatch, mutation):
    if mutation == "role":
        monkeypatch.setenv("AGENT_WORKER_ROLE_ARN", ROLE + "changed")
    elif mutation == "acceptance":
        shared.plan.accepted_by_decision_id = None
    else:
        marker = dict(shared.marker)
        if mutation == "budget_scope":
            marker.pop("budget_scope")
        else:
            marker["accepted_at"] = (datetime.now(UTC) + timedelta(hours=1 if mutation == "future" else -2)).isoformat()
        shared.plan.plan_document = {**shared.plan.plan_document, "execution_continuation": marker}
    await shared.session.flush()
    assert not (await dispatch(shared)).permitted


async def test_revocation_and_wrong_principal_stop_dispatch(shared):
    assert not (await dispatch(shared, principal_user_id="other-user")).permitted
    await shared.session.execute(delete(TenantMembership))
    assert (await dispatch(shared)).reason == DenyReason.ROLE_REVOKED


@pytest.mark.parametrize("state", ["halted", "passed", "failed"])
async def test_halted_terminal_nodes_cannot_dispatch(shared, state):
    shared.node.state = state
    assert not (await dispatch(shared)).permitted


@pytest.fixture
async def model_assignment(shared):
    return await _model_assignment(shared)


async def _model_assignment(shared, *, root_id=None, dispatch_change=None):
    from src.orchestration.dispatch_pass import attempt_run_id
    from src.orchestration.execution_state import ExecutionIdentity, OutcomeKind
    from src.orchestration.execution_store import create_execution

    shared.node.state = "running"
    shared.node.attempts = 1
    run_id = attempt_run_id(shared.node.id, 1)
    shared.claim.active_run_id = run_id
    await shared.session.flush()
    identity = ExecutionIdentity(shared.node.org_id, shared.node.id, 1, 1, shared.claim.id, 1)
    created = await create_execution(shared.session, identity=identity, flow_id=shared.flow.id)
    assert created.kind is OutcomeKind.APPLIED
    row = await report(shared, shared.node, run_id)
    row.dispatch_metadata = {
        "action": "develop",
        "orchestration": {"root_decision_id": root_id or shared.plan.accepted_by_decision_id},
        "execution_continuation": {"accepted_plan_version": 1, "claim_id": shared.claim.id, "claim_generation": 1, "execution_id": created.record.id},
    }
    saved = {"run_id": run_id, "attempt": 1, "root_decision_id": root_id or shared.plan.accepted_by_decision_id}
    if dispatch_change in {"run", "attempt", "root"}:
        saved[{"run": "run_id", "attempt": "attempt", "root": "root_decision_id"}[dispatch_change]] = "different"
    decision = OrchestrationDecision(
        org_id=shared.flow.org_id,
        flow_id=shared.flow.id,
        node_id=shared.node.id,
        kind="node_dispatched",
        actor_id="unrelated-service" if dispatch_change == "actor" else "system:orchestration-dispatch",
        actor_role="engine",
        actor_kind="human" if dispatch_change == "kind" else "service",
        reason="not-json" if dispatch_change == "malformed" else json.dumps(saved),
    )
    if dispatch_change != "missing":
        shared.session.add(decision)
    await shared.session.flush()
    return row


async def test_initial_model_call_uses_current_sql_assignment(shared, model_assignment):
    policy, principal, node, flow = await shared_policy.authorize_shared_model(shared.session, model_assignment)
    assert policy.principal_id == principal == APPROVER
    assert node is shared.node and flow is shared.flow


async def test_initial_worker_and_model_preserve_later_gate_genesis(shared):
    from src.orchestration.report_dispatch import validate_report_start

    acceptance = shared.plan.accepted_by_decision_id
    gate = await _make_approval(shared.session, shared.flow)
    model_assignment = await _model_assignment(shared, root_id=gate.id)
    # A subsequent approval cannot rewrite the original run's human root.
    await _make_approval(shared.session, shared.flow)
    before = (model_assignment.run_id, model_assignment.attempt, dict(model_assignment.dispatch_metadata), shared.claim.active_run_id)

    await validate_report_start(shared.session, model_assignment)
    policy, principal, node, flow = await shared_policy.authorize_shared_model(shared.session, model_assignment)

    assert policy.principal_id == principal == APPROVER
    assert node is shared.node and flow is shared.flow
    assert shared.plan.accepted_by_decision_id == acceptance != gate.id
    assert (model_assignment.run_id, model_assignment.attempt, model_assignment.dispatch_metadata, shared.claim.active_run_id) == before


@pytest.mark.parametrize("change", ["missing", "run", "attempt", "root", "malformed", "actor", "kind"])
async def test_initial_report_requires_committed_dispatch_evidence(shared, change):
    from src.orchestration.run_reports import RunReportError

    model_assignment = await _model_assignment(shared, dispatch_change=change)
    with pytest.raises(RunReportError, match="execution_assignment_unverifiable"):
        await shared_policy.authorize_shared_model(shared.session, model_assignment)


@pytest.mark.parametrize("change", ["service", "rejection", "other_flow", "missing"])
async def test_initial_dispatch_root_must_remain_attributed_same_flow_approval(shared, change):
    from src.orchestration.run_reports import RunReportError

    root = OrchestrationDecision(
        org_id=shared.flow.org_id,
        flow_id=shared.flow.id,
        kind="gate_approved",
        actor_id=APPROVER,
        actor_role="org_admin",
        actor_kind="human",
        reason="root approval",
    )
    if change == "service":
        root.actor_kind = "service"
    elif change == "rejection":
        root.kind = "gate_rejected"
    elif change == "other_flow":
        other = await _make_flow(shared.session, slug="other-flow")
        root.flow_id = other.id
    if change != "missing":
        shared.session.add(root)
        await shared.session.flush()
    model_assignment = await _model_assignment(shared, root_id=root.id if change != "missing" else "missing-approval")
    with pytest.raises(RunReportError, match="execution_assignment_unverifiable"):
        await shared_policy.authorize_shared_model(shared.session, model_assignment)


@pytest.mark.parametrize("change", ["terminal", "claim", "attempt", "plan", "lineage", "operation", "node_halted", "flow_halted"])
async def test_stale_model_authority_cannot_spend(shared, model_assignment, change):
    from src.orchestration.run_reports import RunReportError

    if change == "terminal":
        model_assignment.terminal_receipt = {"outcome": "complete"}
    elif change == "claim":
        shared.claim.generation += 1
    elif change == "attempt":
        shared.node.attempts += 1
    elif change == "plan":
        shared.plan.version += 1
    elif change == "lineage":
        model_assignment.dispatch_metadata = {**model_assignment.dispatch_metadata, "orchestration": {"root_decision_id": "another-acceptance"}}
    elif change == "operation":
        model_assignment.dispatch_metadata = {**model_assignment.dispatch_metadata, "review_cycle_input": {"operation_key": "unassigned-operation"}}
    elif change == "node_halted":
        shared.node.state = "halted"
    else:
        shared.flow.state = "halted"
    await shared.session.flush()
    with pytest.raises((CycleBlockedError, RunReportError)):
        await shared_policy.authorize_shared_model(shared.session, model_assignment)
