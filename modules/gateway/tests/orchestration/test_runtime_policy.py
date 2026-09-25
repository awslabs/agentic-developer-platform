"""Policy revalidation against real membership, accepted-plan and attempt rows."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select
from starlette.requests import Request

from src.agentauth.grants import AuthorityReference, DelegatedGrant
from src.orchestration.dispatch import graph_address
from src.orchestration.execution_policy import (
    COORDINATION_SCHEMA_VERSION,
    Action,
    ChildPersona,
    CoordinationScope,
    DenyReason,
)
from src.orchestration.models import DecisionKind, NodeKind, OrchestrationAcceptedPlan, OrchestrationDecision
from src.orchestration.runtime_policy import WorkerCredentialDecision, authorize_worker_credential, runtime_action
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


@pytest.mark.parametrize("action", [Action.REVIEW, Action.REPAIR])
def test_codex_continuation_uses_engine_action_not_persona_inference(action):
    execution = {
        "persona": {"S": "agent-codex-reviewer"},
        "orchestration_continuation_receipt": {"S": "committed"},
        "orchestration_continuation_action": {"S": action.value},
    }
    assert runtime_action(execution, SimpleNamespace(kind="story", attempts=1)) is action
    execution["orchestration_continuation_action"] = {"S": "merge"}
    assert runtime_action(execution, SimpleNamespace(kind="story", attempts=1)) is None


async def _assignment(session, *, policy, node_kwargs=None, execution_extra=None):
    """One running, protected assignment: flow + accepted policy + node + grant.

    Extracted from the `assignment` fixture so the coordinator cases below run on the
    identical harness. A coordinator built from a second, similar-looking fixture
    could disagree with this one about what a protected execution looks like, and the
    whole point of resolving authority from execution metadata is that this shape is
    the contract.
    """
    flow, node = await _fixture(session, policy=policy, node_kwargs={"state": NodeState.RUNNING, "attempts": 1, **(node_kwargs or {})})
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
        **(execution_extra or {}),
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


@pytest.fixture
async def assignment(session):
    return await _assignment(session, policy=_policy(limits=_limits(max_wall_clock_seconds=7200)))


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
    assert result.action is Action.DEVELOP
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
    assert getattr(request.state, "agent_installation_binding", None) is None


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
    reviewer = AsyncMock(return_value=("review-app-test", "review-test-key", INSTALLATION_A + 1))
    monkeypatch.setattr("src.internal.routes.resolve_reviewer_app_credentials", reviewer)
    mint = AsyncMock(return_value=("scoped-token", (datetime.now(UTC) + timedelta(hours=1)).isoformat()))
    monkeypatch.setattr("src.internal.routes.mint_installation_token_with_expiry", mint)
    revoke = AsyncMock()
    monkeypatch.setattr("src.internal.routes._revoke_undelivered_github_token", revoke)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[verify_internal_or_irsa] = verify_broker_worker
    app.dependency_overrides[get_db] = db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway.test") as client:
        yield SimpleNamespace(
            client=client,
            mint=mint,
            reviewer=reviewer,
            revoke=revoke,
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
    # No request asked for the reviewer identity, so none of these mints may use it —
    # including the reviewer run's. This is the bootstrap mint every run makes first,
    # and it needs the authoring App's grant (note `issues` above) to clone and drive
    # the check run. A review-only App does not hold that, so routing this mint to it
    # would fail the mint and kill the run before it reviewed anything.
    broker_client.reviewer.assert_not_awaited()
    assert broker_client.mint.await_args.args[:2] == ("app-test", "test-key")


async def test_reviewer_run_asking_for_the_review_identity_gets_the_reviewer_app(session, assignment, broker_client):
    """With the authority AND the request, the reviewer App and its own installation.

    The reviewer App is registered `pull_requests: write` + `contents: read` and
    deliberately not `issues`/`checks`, so the mint is narrowed to what it actually
    holds — GitHub refuses a token request naming an ungranted permission.
    """
    plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == assignment.flow.id))
    plan.plan_document = {**plan.plan_document, "execution_policy": {**plan.plan_document["execution_policy"], "allowed_actions": ["review"]}}
    assignment.node.kind = "story"
    assignment.execution["persona"] = {"S": "reviewer"}
    await session.flush()

    response = await broker_client.client.post(GITHUB, json={**broker_client.body, "identity": "review"})

    assert response.status_code == 200, response.text
    assert response.json()["identity"] == "review"
    broker_client.reviewer.assert_awaited_once_with(ORG_A)
    assert broker_client.mint.await_args.args[:3] == ("review-app-test", "review-test-key", INSTALLATION_A + 1)
    assert broker_client.mint.await_args.kwargs["permissions"] == {"contents": "read", "pull_requests": "write", "metadata": "read"}


async def test_develop_action_cannot_request_reviewer_credentials(assignment, broker_client):
    """An implementation worker cannot promote itself through the body hint."""
    response = await broker_client.client.post(GITHUB, json={**broker_client.body, "identity": "review"})

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"] == "review_identity_not_authorized"
    broker_client.reviewer.assert_not_awaited()
    broker_client.mint.assert_not_awaited()


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
    broker_client.revoke.assert_awaited_once_with("must-not-escape")


@pytest.mark.parametrize("during", ["key_lookup", "mint", "audit"])
async def test_cancellation_during_provider_work_withholds_token(session, assignment, broker_client, monkeypatch, during):
    async def cancel():
        assignment.grant = replace(assignment.grant, expires_at=datetime.now(UTC) - timedelta(seconds=1))

    if during == "key_lookup":

        async def key_lookup(*_):
            await cancel()
            return "app-test", "test-key"

        monkeypatch.setattr("src.internal.routes.resolve_tenant_app_credentials", key_lookup)
    elif during == "mint":

        async def mint(*_, **__):
            await cancel()
            return "scoped-token", (datetime.now(UTC) + timedelta(hours=1)).isoformat()

        broker_client.mint.side_effect = mint
    else:
        from src.internal.routes import _write_audit

        async def audit(*args, **kwargs):
            await _write_audit(*args, **kwargs)
            await cancel()

        monkeypatch.setattr("src.internal.routes._write_audit", audit)

    response = await broker_client.client.post(GITHUB, json=broker_client.body)
    assert response.status_code == 404, response.text
    assert "scoped-token" not in response.text
    if during == "key_lookup":
        broker_client.mint.assert_not_awaited()
        broker_client.revoke.assert_not_awaited()
    else:
        broker_client.revoke.assert_awaited_once_with("scoped-token")


async def test_matching_victim_invocation_and_repo_cannot_replace_authenticated_run(broker_client):
    response = await broker_client.client.post(GITHUB, json={**broker_client.body, "invocation_id": "victim-run"})
    assert response.status_code == 404
    broker_client.mint.assert_not_awaited()


# ---------------------------------------------------------------------------
# Coordinator runtime resolution (#5224)
# ---------------------------------------------------------------------------


COORDINATOR_METADATA = {"wave_coordinator": {"BOOL": True}, "coordinator_flow_id": {"S": "flow-under-coordination"}}


async def _coordinator_assignment(session, *, policy=None, persona="operations", metadata=None, node_kwargs=None):
    """A coordinator's own protected assignment, on its wave's evaluation node.

    `kind="eval"` is not incidental: the engine assigns a wave coordinator to the
    wave's evaluation node, which is exactly why `runtime_action` must not decide
    from `persona`/`kind` alone. Every test here runs on that real shape so the
    conflation would actually be reachable if the resolution order regressed.
    """
    # The accepted scope must name the address the credential boundary will actually
    # compose, and that address depends on rows this helper is about to create — so
    # the policy is accepted with a placeholder and the stored document is rewritten
    # once the real node exists, rather than the test guessing the string.
    result = await _assignment(
        session,
        policy=policy or _coordinator_policy(),
        node_kwargs={"kind": NodeKind.EVAL.value, **(node_kwargs or {})},
        execution_extra={"persona": {"S": persona}, **(COORDINATOR_METADATA if metadata is None else metadata)},
    )
    if policy is None:
        await _reassign_scope(session, result.flow, [graph_address(result.node, flow_slug=result.flow.slug)])
    return result


def _coordinator_policy():
    """A v3 policy accepting a bounded coordinator, on the standard fixture's flow."""
    return _policy(
        schema_version=COORDINATION_SCHEMA_VERSION,
        allowed_actions=[Action.DEVELOP, Action.REVIEW, Action.REPAIR, Action.EVALUATE, Action.COORDINATE],
        coordination=CoordinationScope(
            assigned_node_addresses=["placeholder/epic/wave/node"],
            allowed_child_personas=[ChildPersona.DEVELOPER, ChildPersona.REVIEWER],
            allowed_child_actions=[Action.DEVELOP, Action.REVIEW, Action.REPAIR],
        ),
        limits=_limits(max_wall_clock_seconds=7200),
    )


async def _reassign_scope(session, flow, addresses):
    """Rewrite the accepted document's assigned node set in place.

    Edits the stored `plan_document` the way the other tests in this file rewrite
    `allowed_actions` — going through the persisted document rather than the Python
    object proves the value is read back from acceptance, not from the fixture.
    """
    plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == flow.id))
    policy_document = dict(plan.plan_document["execution_policy"])
    policy_document["coordination"] = {**policy_document["coordination"], "assigned_node_addresses": addresses}
    plan.plan_document = {**plan.plan_document, "execution_policy": policy_document}
    await session.flush()


class TestCoordinatorRuntimeResolution:
    """`runtime_action` resolves the coordinator from engine-written metadata."""

    async def test_coordinator_assignment_resolves_coordinate(self, session) -> None:
        work = await _coordinator_assignment(session)
        assert runtime_action(work.execution, work.node) is Action.COORDINATE

    async def test_coordinator_is_not_read_as_the_evaluation_it_coordinates(self, session) -> None:
        """The load-bearing ordering assertion.

        Without the coordinator branch first, this same execution — persona
        `operations` on an `eval` node — resolves to `EVALUATE`, which is authority to
        *conclude* the evaluation. A coordinator must never acquire that by being
        assigned to the node it coordinates.
        """
        work = await _coordinator_assignment(session)
        assert work.node.kind == "eval"
        assert work.execution["persona"] == {"S": "operations"}
        assert runtime_action(work.execution, work.node) is not Action.EVALUATE

    async def test_operations_on_an_eval_node_still_evaluates_without_coordinator_metadata(self, session) -> None:
        """The pre-existing reading is unchanged for a non-coordinator."""
        work = await _coordinator_assignment(session, metadata={})
        assert runtime_action(work.execution, work.node) is Action.EVALUATE

    @pytest.mark.parametrize(
        "metadata",
        [
            {"wave_coordinator": {"BOOL": True}},
            {"coordinator_flow_id": {"S": "flow-under-coordination"}},
            {"wave_coordinator": {"BOOL": False}, "coordinator_flow_id": {"S": "flow-under-coordination"}},
            {"wave_coordinator": {"S": "true"}, "coordinator_flow_id": {"S": "flow-under-coordination"}},
            {"wave_coordinator": {"BOOL": True}, "coordinator_flow_id": {"S": ""}},
        ],
    )
    async def test_partial_or_malformed_coordinator_metadata_is_not_a_coordinator(self, session, metadata) -> None:
        """Both engine-written fields are required, in their engine-written shapes.

        A half-written record must not confer coordination authority, and a string
        `"true"` where the engine writes a DynamoDB boolean is not the engine's write.
        These all fall through to the ordinary persona/kind reading instead.
        """
        work = await _coordinator_assignment(session, metadata=metadata)
        assert runtime_action(work.execution, work.node) is not Action.COORDINATE

    @pytest.mark.parametrize("persona", ["developer", "reviewer", "", "operations-admin"])
    async def test_coordinator_metadata_with_an_unassigned_persona_refuses(self, session, persona) -> None:
        """A mismatch between the two is refused, not resolved to either reading."""
        work = await _coordinator_assignment(session, persona=persona)
        assert runtime_action(work.execution, work.node) is None

    async def test_a_coordinator_persona_cannot_be_forged_without_the_metadata(self, session) -> None:
        """`persona` alone is a request-supplied string; it grants nothing."""
        work = await _coordinator_assignment(session, persona="operations", metadata={}, node_kwargs={"kind": NodeKind.STORY.value})
        assert runtime_action(work.execution, work.node) not in {Action.COORDINATE, Action.EVALUATE}

    async def test_the_genesis_coordinator_lane_does_not_acquire_coordinate(self, session) -> None:
        """`bind_coordinator`'s lane is a different shape, and stays out of scope.

        `agentauth/coordinator.py` binds a human-launched Operations run to its
        approved flow by writing `coordinator_flow_id` *without* `wave_coordinator`,
        and that execution carries no assigned node at all. Requiring both fields
        keeps this story's authority to the per-wave assignment the engine dispatches,
        rather than turning the existing genesis lane into the resident coordinator
        #5224 design point 4 says not to create. That lane keeps whatever it had.
        """
        work = await _coordinator_assignment(session, metadata={"coordinator_flow_id": {"S": "flow-under-coordination"}})
        assert runtime_action(work.execution, work.node) is not Action.COORDINATE


class TestCoordinatorCredentialBoundary:
    """The full credential recheck for a coordinator, against real rows."""

    async def test_assigned_coordinator_obtains_a_read_only_credential(self, session) -> None:
        work = await _coordinator_assignment(session)
        decision = await check(session, work)
        assert decision.permitted, decision.detail
        assert isinstance(decision, WorkerCredentialDecision)
        # Read-only, deliberately: a coordinator requests children through the
        # authenticated dispatch service, and a GitHub *mutation* is #5223's
        # separately authorized mediated capability, not a side effect of this one.
        assert decision.permissions == {
            "contents": "read",
            "pull_requests": "read",
            "issues": "read",
            "checks": "read",
            "metadata": "read",
        }

    async def test_coordinator_outside_its_assigned_node_set_is_refused(self, session) -> None:
        """Ownership is re-evaluated against the accepted scope at the boundary."""
        work = await _coordinator_assignment(session)
        await _reassign_scope(session, work.flow, ["other-flow/epic-1/wave-1/story-1"])
        decision = await check(session, work)
        assert decision.reason is DenyReason.COORDINATION_NODE_NOT_ASSIGNED

    async def test_a_v1_policy_denies_coordination(self, session) -> None:
        """Absence grants nothing: an older accepted policy is not upgraded.

        The coordinator resolves to `COORDINATE` and the v1 document simply does not
        permit it, so the refusal names the action rather than reporting a schema
        problem or silently reading the flow as an evaluation.
        """
        work = await _coordinator_assignment(session, policy=_policy(limits=_limits(max_wall_clock_seconds=7200)))
        assert (await check(session, work)).reason is DenyReason.ACTION_NOT_PERMITTED

    async def test_revoked_grant_still_denies_a_coordinator(self, session) -> None:
        """Coordination authority is not exempt from the lifetime checks.

        Status reads and cancellation remain available independently of this gate —
        that is the credential boundary, not the whole coordinator surface.
        """
        work = await _coordinator_assignment(session)
        work.grant = replace(work.grant, revoked=True)
        assert (await check(session, work)).reason is DenyReason.GRANT_REVOKED

    async def test_membership_removal_denies_a_coordinator(self, session) -> None:
        work = await _coordinator_assignment(session)
        await session.execute(delete(TenantMembership).where(TenantMembership.user_id == APPROVER))
        await session.execute(delete(User).where(User.id == APPROVER))
        assert (await check(session, work)).reason is DenyReason.MEMBERSHIP_REVOKED

    async def test_coordinator_cannot_reach_the_user_credential_brokers(self, session) -> None:
        """`coordinate` is not in any accepted `user_credentials.actions` here."""
        work = await _coordinator_assignment(session)
        result = await check(session, work, "/internal/v1/credential-raw-read")
        assert result.reason is DenyReason.CREDENTIAL_SCOPE_UNAVAILABLE


class TestAnUnrecognizedAuthorityIsRefusedHere:
    """The fence #4529 put at this boundary, guarded.

    `authorize_worker_credential` used to read `!= AUTHORITY_GATE_DECISION -> permit`,
    which means "not the engine kind, therefore legacy, therefore unrestricted" — it
    treated the *absence* of a rule as permission, at the function fronting GitHub
    installation-token minting and model credentials. `c6064c50` replaced that with an
    explicit accept-list plus an explicit denial.

    Restoring the fail-open, however, failed **no test in the repository** — not the
    dedicated authoring-refusal suite, not the model-identity or github-operation
    suites, not the vault/assume-role route tests (all of which are happy-path and
    permit either way). The security fix was real and unguarded, which is the state
    where a later refactor silently undoes it. These two tests are that guard.

    They also document why the two fixtures repaired alongside them had to change:
    `"human_event"` is not in `RECOGNIZED_AUTHORITY_KINDS` and is minted nowhere in
    `src/`, so those fixtures were only ever green *because* of the fail-open.
    """

    async def test_an_unrecognized_kind_is_blocked_not_permitted(self, session, assignment) -> None:
        """The mutation-caught case. A kind outside the platform's vocabulary is a bug
        or a forgery; the honest answer at a credential boundary is refusal."""
        assignment.grant = replace(
            assignment.grant,
            authority=AuthorityReference("human_event", "approval", APPROVER, assignment.grant.tenant_id),
        )
        result = await check(session, assignment)
        assert not result.permitted
        assert result.reason is DenyReason.AUTHORITY_KIND_NOT_RECOGNIZED

    @pytest.mark.parametrize("kind", ["github_event", "service_policy"])
    async def test_the_two_legacy_kinds_keep_their_permit(self, session, assignment, kind: str) -> None:
        """The other half of the fence, so a future tightening that denies everything
        but `gate_decision` fails here instead of breaking real GitHub-rooted and
        service-rooted workers. `c6064c50` preserved these two deliberately; without
        this test, "deny unless gate_decision" would look like a pure improvement.
        """
        assignment.grant = replace(
            assignment.grant,
            authority=AuthorityReference(kind, "approval", APPROVER, assignment.grant.tenant_id),
        )
        assert (await check(session, assignment)).permitted
