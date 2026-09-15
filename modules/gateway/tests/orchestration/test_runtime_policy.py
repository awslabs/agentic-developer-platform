"""Policy revalidation against real membership, accepted-plan and attempt rows."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select
from starlette.requests import Request

from src.agentauth.grants import AuthorityReference, DelegatedGrant
from src.orchestration.execution_policy import DenyReason
from src.orchestration.models import DecisionKind, OrchestrationAcceptedPlan, OrchestrationDecision
from src.orchestration.runtime_policy import authorize_worker_credential
from src.orchestration.state import NodeState
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from tests.orchestration.test_policy_admission import (
    APPROVER,
    ORG_A,
    REPO,
    _accept_policy,
    _authorize,
    _fixture,
    _policy,
)
from tests.orchestration.test_policy_admission import (
    engine as engine_fixture,
)
from tests.orchestration.test_policy_admission import (
    healthy_policy_reservations as reservations_fixture,
)
from tests.orchestration.test_policy_admission import (
    policy_budget_initializers as initializers_fixture,
)
from tests.orchestration.test_policy_admission import (
    session as session_fixture,
)

engine = engine_fixture
healthy_policy_reservations = reservations_fixture
session = session_fixture
policy_budget_initializers = initializers_fixture

GITHUB = "/internal/v1/github-installation-token"


@pytest.fixture
async def assignment(session):
    flow, node = await _fixture(session, policy=_policy(), node_kwargs={"state": NodeState.RUNNING, "attempts": 1})
    approval = await session.scalar(select(OrchestrationDecision).where(OrchestrationDecision.flow_id == flow.id))
    dispatch = OrchestrationDecision(
        org_id=ORG_A,
        flow_id=flow.id,
        node_id=node.id,
        kind=DecisionKind.NODE_DISPATCHED.value,
        actor_id="system:orchestration-dispatch",
        actor_role="engine",
        actor_kind="service",
        created_at=datetime.now(UTC) - timedelta(seconds=10),
    )
    session.add(dispatch)
    await session.flush()
    execution = {
        "invocation_id": {"S": "worker"},
        "tenant_id": {"S": ORG_A},
        "flow_id": {"S": flow.id},
        "orchestration_node_id": {"S": node.id},
        "orchestration_node_attempt": {"N": "1"},
        "persona": {"S": "developer"},
        "repo": {"S": REPO},
    }
    grant = DelegatedGrant(
        grant_id="grant:worker:1",
        tenant_id=ORG_A,
        principal="worker#1",
        authority=AuthorityReference("gate_decision", approval.id, APPROVER, ORG_A),
        allowed_actions=frozenset(),
        flow_id=flow.id,
        repo_scope=frozenset({REPO}),
        expires_at=datetime.now(UTC) + timedelta(hours=2),
    )
    return SimpleNamespace(flow=flow, node=node, execution=execution, grant=grant, dispatch=dispatch)


async def check(session, assignment, path=GITHUB):
    return await authorize_worker_credential(session, execution=assignment.execution, grant=assignment.grant, broker_path=path)


async def test_current_assignment_can_obtain_repository_scoped_credential(session, assignment):
    assert (await check(session, assignment)).permitted


@pytest.mark.parametrize("path", ["/internal/v1/credential-assume-role", "/internal/v1/credential-raw-read"])
async def test_unscopable_brokers_refuse_policy_flow(session, assignment, path):
    assert (await check(session, assignment, path)).reason is DenyReason.CREDENTIAL_SCOPE_UNAVAILABLE


@pytest.mark.parametrize("field,value", [("repo", "other/repo"), ("tenant_id", "other-tenant"), ("orchestration_node_id", "other-node")])
async def test_forged_assignment_is_refused(session, assignment, field, value):
    assignment.execution[field] = {"S": value}
    assert not (await check(session, assignment)).permitted


async def test_revoked_live_grant_blocks_next_credential(session, assignment):
    assignment.grant = replace(assignment.grant, revoked=True)
    assert (await check(session, assignment)).reason is DenyReason.GRANT_REVOKED


async def test_membership_removal_blocks_existing_worker(session, assignment):
    await session.execute(delete(TenantMembership).where(TenantMembership.user_id == APPROVER))
    await session.execute(delete(User).where(User.id == APPROVER))
    assert (await check(session, assignment)).reason is DenyReason.MEMBERSHIP_REVOKED


async def test_amendment_does_not_silently_upgrade_old_worker(session, assignment):
    plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == assignment.flow.id))
    plan.superseded_at = datetime.now(UTC)
    await _accept_policy(session, assignment.flow, _policy(), version=2)
    assert (await check(session, assignment)).reason is DenyReason.STALE_POLICY_VERSION


async def test_expired_policy_blocks_next_credential(session, assignment):
    plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == assignment.flow.id))
    document = dict(plan.plan_document)
    document["execution_policy"] = dict(document["execution_policy"], expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    plan.plan_document = document
    await session.flush()
    assert (await check(session, assignment)).reason is DenyReason.POLICY_EXPIRED


@pytest.mark.parametrize("next_boundary", ["credential", "dispatch"])
async def test_deadline_survives_retries_and_blocks_next_action(session, assignment, next_boundary, monkeypatch):
    later = datetime.now(UTC) + timedelta(days=2)
    monkeypatch.setattr("src.orchestration.runtime_policy.utcnow", lambda: later)
    monkeypatch.setattr("src.orchestration.policy_admission.utcnow", lambda: later)
    if next_boundary == "credential":
        result = await check(session, assignment)
    else:
        assignment.node.state = NodeState.READY.value
        result = await _authorize(session, assignment.node)
    assert result.reason is DenyReason.WALL_CLOCK_LIMIT_EXCEEDED


async def test_reviewer_cannot_reuse_develop_permission(session, assignment):
    assignment.execution["persona"] = {"S": "reviewer"}
    assert (await check(session, assignment)).reason is DenyReason.ACTION_NOT_PERMITTED


async def test_second_attempt_requires_repair_permission(session, assignment):
    assignment.node.attempts = 2
    assignment.execution["orchestration_node_attempt"] = {"N": "2"}
    assert (await check(session, assignment)).reason is DenyReason.ACTION_NOT_PERMITTED


async def test_broker_rechecks_policy_before_granting_binding(session, assignment, monkeypatch):
    """Exercise the production dependency, including body/identity binding."""
    from fastapi import HTTPException

    from src.agentauth.broker_identity import verify_broker_worker

    assignment.execution["persona"] = {"S": "reviewer"}
    caller = SimpleNamespace(tenant_id=ORG_A, invocation_id="worker", principal="worker#1")
    runtime = SimpleNamespace(
        authenticate=lambda *_: (None, caller, None, assignment.grant),
        validate_flow=AsyncMock(),
        store=SimpleNamespace(_read=lambda *_: assignment.execution),
    )

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr("src.agentauth.routes.get_agent_runtime", lambda: runtime)
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: SessionContext)
    request = Request({"type": "http", "path": GITHUB, "headers": []})
    request._json = {"invocation_id": "worker", "installation_id": 1, "repo_owner": "aws-e", "repo_name": "adp"}
    with pytest.raises(HTTPException) as exc:
        await verify_broker_worker(request)
    assert exc.value.status_code == 404
    assert not hasattr(request.state, "agent_installation_binding")
