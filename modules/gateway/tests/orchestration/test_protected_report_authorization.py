"""A protected report-bearing run is authorized by the in-force policy and its fences (#7419)."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import fakeredis.aioredis
import pytest

from src.budget.config import budget_config
from src.budget.reservations import ReservationStore
from src.orchestration import flow_budget, shared_policy
from src.orchestration.dispatch_pass import attempt_run_id
from src.orchestration.execution_policy import Action
from src.orchestration.execution_state import ExecutionIdentity, OutcomeKind
from src.orchestration.execution_store import create_execution
from src.orchestration.flow_meter import meter_target
from src.orchestration.models import OrchestrationDecision, OrchestrationWorkClaim
from src.orchestration.report_dispatch import validate_report_start
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.run_reports import OrchestrationRunReport, RunReportError
from src.orchestration.shared_cycle import validate_current_report_assignment
from tests.orchestration.test_policy_admission import (
    APPROVER,
    INSTALLATION_A,
    REPO,
    _accept_policy,
    _limits,
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

ACTIONS = [Action.DEVELOP, Action.REPAIR, Action.REVIEW, Action.MERGE, Action.OPERATE]


@pytest.fixture
async def protected(session, monkeypatch):
    for key, value in {
        "AGENT_AUTHORITY_ENABLED": "true",
        "ADP_SHARED_WORKER_CONTINUATION_ENABLED": "false",
        "ADP_ENGINE_OPERATIONS_EXECUTOR": "true",
        "BUDGET_ENFORCEMENT_ENABLED": "true",
        "ADP_WORK_CLAIMS_ENABLED": "true",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(budget_config, "budget_reservation_enabled", True)
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = ReservationStore(redis_url=None, ttl_seconds=120, client=client)
    monkeypatch.setattr(flow_budget, "_reservations", store)
    await _make_org(session)
    await _make_member(session)
    flow = await _make_flow(session)
    policy = _policy(allowed_actions=ACTIONS, limits=_limits(max_concurrent_actions=1, max_wall_clock_seconds=3600))
    plan = await _accept_policy(session, flow, policy)
    acceptance = OrchestrationDecision(
        org_id=flow.org_id, flow_id=flow.id, kind="plan_accepted", actor_id=APPROVER, actor_role="org_admin", actor_kind="human", reason="accepted"
    )
    session.add(acceptance)
    await session.flush()
    plan.accepted_by_decision_id = acceptance.id
    node = await _make_node(session, flow)
    node.state, node.attempts = "running", 1
    run_id = attempt_run_id(node.id, 1)
    claim = OrchestrationWorkClaim(
        org_id=flow.org_id,
        provider_repository_id=12345,
        issue_number=int(node.issue_ref),
        owner_kind="engine_flow",
        owner_ref=flow.id,
        active_run_id=run_id,
        state="held",
        generation=1,
    )
    session.add(claim)
    await session.flush()
    # The meter the engine prepares at admission: an anchor and no spend.
    target = meter_target(org_id=flow.org_id, flow_id=flow.id, policy=policy)
    assert (await store.reserve("__initialized__", Decimal(0), [replace(target, require_initialization=False)])).admitted
    identity = ExecutionIdentity(node.org_id, node.id, 1, 1, claim.id, 1)
    created = await create_execution(session, identity=identity, flow_id=flow.id)
    assert created.kind is OutcomeKind.APPLIED
    session.add(
        OrchestrationDecision(
            org_id=flow.org_id,
            flow_id=flow.id,
            node_id=node.id,
            kind="node_dispatched",
            actor_id="system:orchestration-dispatch",
            actor_role="engine",
            actor_kind="service",
            reason=json.dumps({"run_id": run_id, "attempt": 1, "root_decision_id": acceptance.id}),
        )
    )
    await session.flush()
    yield SimpleNamespace(
        session=session, flow=flow, plan=plan, node=node, claim=claim, policy=policy, execution=created.record, run_id=run_id, root=acceptance.id
    )
    await client.aclose()


async def assignment(p, *, persona="operations", executor_role="operate", **fence_overrides):
    """The row prepare_run_report persists for a protected dispatch: fences, no continuation marker."""
    fences = {
        "contract_version": 1,
        "execution_id": p.execution.id,
        "org_id": p.node.org_id,
        "flow_id": p.flow.id,
        "node_id": p.node.id,
        "cycle": 1,
        "accepted_plan_version": 1,
        "claim_id": p.claim.id,
        "claim_generation": 1,
        **fence_overrides,
    }
    row = OrchestrationRunReport(
        run_id=p.run_id,
        credential_hash=p.run_id.ljust(64, "0"),
        org_id=p.node.org_id,
        flow_id=p.flow.id,
        node_id=p.node.id,
        attempt=1,
        persona=persona,
        repo=REPO,
        installation_id=INSTALLATION_A,
        provider_repository_id=12345,
        dispatch_metadata={"orchestration": {"root_decision_id": p.root, "executor_role": executor_role}, "handoff_expect": fences},
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    p.session.add(row)
    await p.session.flush()
    return row


async def test_protected_operations_run_starts_from_the_in_force_policy(protected):
    row = await assignment(protected)
    assert await shared_policy.is_protected_assignment(protected.session, row)
    await validate_report_start(protected.session, row)
    policy, principal, node, flow = await shared_policy.authorize_shared_model(protected.session, row)
    assert policy.policy_id == protected.policy.policy_id or policy.org_id == protected.flow.org_id
    assert principal == APPROVER and node is protected.node and flow is protected.flow
    execution, identity = await validate_current_report_assignment(protected.session, row)
    assert execution.id == protected.execution.id and identity.claim_id == protected.claim.id


@pytest.mark.parametrize("persona", ["developer", "agent-codex-developer"])
async def test_protected_developer_run_is_authorized_without_a_declared_action(protected, persona):
    row = await assignment(protected, persona=persona, executor_role="develop")
    policy, principal, node, flow = await shared_policy.authorize_shared_model(protected.session, row)
    assert node is protected.node


async def test_protected_operations_run_must_declare_the_operate_executor(protected):
    row = await assignment(protected, executor_role="develop")
    with pytest.raises(CycleBlockedError, match="model_action_unverifiable"):
        await shared_policy.authorize_shared_model(protected.session, row)


async def test_protected_inputs_refuse_without_protected_authority(protected, monkeypatch):
    row = await assignment(protected)
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "false")
    with pytest.raises(CycleBlockedError, match="protected_authority_disabled"):
        await shared_policy.protected_inputs(protected.session, org_id=row.org_id, flow_id=row.flow_id, assignment=row)


@pytest.mark.parametrize("change", ["cycle", "node_id", "flow_id"])
async def test_protected_fences_must_name_this_assignment(protected, change):
    row = await assignment(protected, **{change: 2 if change == "cycle" else "elsewhere"})
    with pytest.raises(CycleBlockedError, match="protected_assignment_unverifiable"):
        await shared_policy.authorize_shared_model(protected.session, row)


@pytest.mark.parametrize("change", ["flow_halted", "policy_expired", "wall_clock", "plan_amended", "claim_moved", "meter_lost"])
async def test_stale_protected_authority_cannot_start_or_spend(protected, change, monkeypatch):
    row = await assignment(protected)
    if change == "flow_halted":
        protected.flow.state = "halted"
    elif change == "policy_expired":
        document = dict(protected.plan.plan_document)
        document["execution_policy"] = {**document["execution_policy"], "expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}
        protected.plan.plan_document = document
    elif change == "wall_clock":
        monkeypatch.setattr(
            "src.orchestration.runtime_policy.flow_started_at",
            _started(datetime.now(UTC) - timedelta(seconds=protected.policy.limits.max_wall_clock_seconds + 1)),
        )
    elif change == "plan_amended":
        protected.plan.version = 2
    elif change == "claim_moved":
        protected.claim.active_run_id = "another-run"
    else:
        await flow_budget.get_flow_reservations()._get_client()
        await (await flow_budget.get_flow_reservations()._get_client()).flushdb()
    await protected.session.flush()
    with pytest.raises((CycleBlockedError, RunReportError)):
        await shared_policy.authorize_shared_model(protected.session, row)


def _started(value):
    async def started(session, *, org_id, flow_id):
        return value

    return started


async def test_a_plan_with_a_shared_continuation_keeps_the_shared_contract(protected):
    """Shared review-cycle dispatches also carry handoff_expect; the plan's marker decides the path."""
    row = await assignment(protected)
    protected.plan.plan_document = {
        **protected.plan.plan_document,
        "execution_continuation": {"mode": "shared_worker_role", "contract_version": 1, "budget_scope": "authenticated_gateway_calls"},
    }
    await protected.session.flush()
    assert not await shared_policy.is_protected_assignment(protected.session, row)
    with pytest.raises(CycleBlockedError, match="shared_worker_continuation_disabled"):
        await shared_policy.authorize_shared_model(protected.session, row)


async def test_without_protected_authority_the_shared_contract_applies(protected, monkeypatch):
    row = await assignment(protected)
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "false")
    assert not await shared_policy.is_protected_assignment(protected.session, row)
    with pytest.raises(CycleBlockedError, match="shared_worker_continuation_disabled"):
        await shared_policy.authorize_shared_model(protected.session, row)


async def test_policy_principal_recorded_as_a_cognito_sub_resolves_to_the_member(protected):
    """Production policies record the approver's sub; memberships are keyed by users.id."""
    document = dict(protected.plan.plan_document)
    document["execution_policy"] = {**document["execution_policy"], "principal_id": f"sub:{APPROVER}"}
    protected.plan.plan_document = document
    await protected.session.flush()
    row = await assignment(protected)
    policy, principal, node, flow = await shared_policy.authorize_shared_model(protected.session, row)
    assert principal == f"sub:{APPROVER}" and node is protected.node


async def test_unknown_policy_principal_is_refused_not_revoked(protected):
    document = dict(protected.plan.plan_document)
    document["execution_policy"] = {**document["execution_policy"], "principal_id": "nobody-here"}
    protected.plan.plan_document = document
    await protected.session.flush()
    row = await assignment(protected)
    with pytest.raises(CycleBlockedError, match="protected_principal_unresolvable|membership_revoked"):
        await shared_policy.authorize_shared_model(protected.session, row)
