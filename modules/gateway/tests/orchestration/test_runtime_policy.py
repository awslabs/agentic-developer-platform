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
from src.shared.models.audit import AuditLog  # noqa: F401 — register before fixture creates schema
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from tests.orchestration.test_policy_admission import (
    APPROVER,
    INSTALLATION_A,
    ORG_A,
    REPO,
    _accept_policy,
    _authorize,
    _fixture,
    _limits,
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
    flow, node = await _fixture(
        session, policy=_policy(limits=_limits(max_wall_clock_seconds=7200)), node_kwargs={"state": NodeState.RUNNING, "attempts": 1}
    )
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


@pytest.mark.parametrize(
    "path",
    [
        "/internal/v1/credential-assume-role",
        "/internal/v1/credential-raw-read",
        "/internal/v1/proxy-request",
        "/internal/v1/credential-materialize",
        "/internal/v1/worker-task-credentials",
    ],
)
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


async def test_role_demotion_blocks_existing_worker(session, assignment):
    assert (await check(session, assignment)).permitted
    membership = await session.scalar(select(TenantMembership).where(TenantMembership.user_id == APPROVER))
    membership.role = "member"
    await session.flush()
    assert (await check(session, assignment)).reason is DenyReason.ROLE_REVOKED


async def test_token_lifetime_cannot_exceed_flow_deadline(session, assignment, monkeypatch):
    from src.orchestration.runtime_policy import WorkerCredentialDecision

    result = await check(session, assignment)
    assert isinstance(result, WorkerCredentialDecision)
    assert result.not_after == assignment.dispatch.created_at + timedelta(seconds=7200)
    later = datetime.now(UTC) + timedelta(minutes=65)
    monkeypatch.setattr("src.orchestration.runtime_policy.utcnow", lambda: later)
    assert (await check(session, assignment)).reason is DenyReason.CREDENTIAL_SCOPE_UNAVAILABLE


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
    plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == assignment.flow.id))
    document = dict(plan.plan_document)
    document["execution_policy"] = dict(document["execution_policy"], allowed_actions=["develop", "merge", "evaluate"])
    plan.plan_document = document
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


@pytest.fixture
async def broker_client(session, assignment, monkeypatch):
    """Real broker dependency and endpoint; only pod auth and GitHub are external."""
    import httpx
    from fastapi import FastAPI

    from src.agentauth.broker_identity import verify_broker_worker
    from src.internal.auth_deps import verify_internal_or_irsa
    from src.internal.routes import router
    from src.shared.database import get_db
    from src.shared.models.vault import ChannelTenantMap

    assignment.execution["installation_id"] = {"N": str(INSTALLATION_A)}
    session.add(ChannelTenantMap(provider="github", provider_scope_id="aws-e", org_id=ORG_A, installation_id=str(INSTALLATION_A)))
    await session.flush()
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

    async def db():
        yield session

    monkeypatch.setattr("src.agentauth.routes.get_agent_runtime", lambda: runtime)
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: SessionContext)
    monkeypatch.setattr("src.internal.routes.resolve_tenant_app_credentials", AsyncMock(return_value=("app-test", "test-key")))
    mint = AsyncMock(return_value=("scoped-token", (datetime.now(UTC) + timedelta(hours=1)).isoformat()))
    monkeypatch.setattr("src.internal.routes.mint_installation_token_with_expiry", mint)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[verify_internal_or_irsa] = verify_broker_worker
    app.dependency_overrides[get_db] = db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway.test") as client:
        yield SimpleNamespace(
            client=client,
            mint=mint,
            body={"invocation_id": "worker", "installation_id": INSTALLATION_A, "repo_owner": "aws-e", "repo_name": "adp"},
        )


@pytest.mark.parametrize(
    "persona,kind,actions,contents,pull_requests",
    [
        ("operations", "eval", ["evaluate"], "read", "read"),
        ("reviewer", "story", ["review"], "read", "write"),
        ("developer", "story", ["develop", "merge"], "write", "write"),
    ],
)
async def test_endpoint_mints_only_policy_permissions(session, assignment, broker_client, persona, kind, actions, contents, pull_requests):
    plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == assignment.flow.id))
    plan.plan_document = {**plan.plan_document, "execution_policy": {**plan.plan_document["execution_policy"], "allowed_actions": actions}}
    if kind == "eval":
        from src.orchestration.dispatch import graph_address

        plan.plan_document = {
            **plan.plan_document,
            "execution_policy": {
                **plan.plan_document["execution_policy"],
                "evaluation_acceptance": {graph_address(assignment.node, flow_slug=assignment.flow.slug): "machine"},
            },
        }
    assignment.node.kind = kind
    assignment.execution["persona"] = {"S": persona}
    await session.flush()
    response = await broker_client.client.post(GITHUB, json=broker_client.body)
    assert response.status_code == 200, response.text
    assert response.json()["token"] == "scoped-token"
    assert broker_client.mint.await_args.kwargs == {
        "repositories": ["adp"],
        "permissions": {"contents": contents, "pull_requests": pull_requests, "issues": pull_requests, "checks": "read", "metadata": "read"},
    }


@pytest.mark.parametrize("case", ["wrong_repo", "expired_grant", "short_grant", "short_deadline", "human_merge_gate"])
async def test_endpoint_refuses_without_mint_or_broad_fallback(session, assignment, broker_client, case, monkeypatch):
    body = dict(broker_client.body)
    if case == "wrong_repo":
        body["repo_name"] = "other"
    elif case in {"expired_grant", "short_grant"}:
        assignment.grant = replace(assignment.grant, expires_at=datetime.now(UTC) + timedelta(minutes=-1 if case == "expired_grant" else 30))
    elif case == "short_deadline":
        later = datetime.now(UTC) + timedelta(minutes=65)
        monkeypatch.setattr("src.orchestration.runtime_policy.utcnow", lambda: later)
    else:
        plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == assignment.flow.id))
        plan.plan_document = {**plan.plan_document, "execution_policy": {**plan.plan_document["execution_policy"], "human_gates": ["merge"]}}
    await session.flush()
    response = await broker_client.client.post(GITHUB, json=body)
    assert response.status_code == 404, response.text
    broker_client.mint.assert_not_awaited()


@pytest.mark.parametrize("expiry", ["past", "beyond_deadline", "naive", "invalid"])
async def test_endpoint_never_returns_provider_token_with_invalid_lifetime(assignment, broker_client, expiry):
    values = {
        "past": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        "beyond_deadline": (datetime.now(UTC) + timedelta(hours=3)).isoformat(),
        "naive": datetime.now(UTC).replace(tzinfo=None).isoformat(),
        "invalid": "not-a-time",
    }
    broker_client.mint.return_value = ("must-not-escape", values[expiry])
    response = await broker_client.client.post(GITHUB, json=broker_client.body)
    assert response.status_code == 502, response.text
    assert "must-not-escape" not in response.text
