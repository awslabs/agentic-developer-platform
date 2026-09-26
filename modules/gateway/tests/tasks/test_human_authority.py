"""Human Tasks have their own live identity and standing authorization."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.agentauth.bootstrap import BootstrapRefusedError
from src.tasks import authz, errors
from src.tasks import human_authority as human

USER = str(uuid.uuid4())
TENANT = str(uuid.uuid4())


@pytest.mark.asyncio
async def test_human_disabled_does_not_consult_service_alias(monkeypatch):
    monkeypatch.delenv("ADP_TASK_API_HUMAN_ENABLED", raising=False)
    service = AsyncMock()
    monkeypatch.setattr(authz.principal_service, "resolve_by_exact_source", service)
    with pytest.raises(errors.TaskApiError):
        await authz.resolve_caller(SimpleNamespace(account_type="human"), frozenset({"adp-tasks/submit"}), object())
    service.assert_not_called()


@pytest.mark.asyncio
async def test_human_scope_comes_from_current_policy_and_revocation_denies(monkeypatch):
    monkeypatch.setenv("ADP_TASK_API_HUMAN_ENABLED", "true")
    membership = AsyncMock(return_value=SimpleNamespace(user_id=USER, tenant_id=TENANT))
    monkeypatch.setattr(human, "authorize_human_session", membership)
    policy = Mock(return_value={"status": "active", "task_scopes": ["read"]})
    monkeypatch.setattr(human, "TaskServicePolicyStore", lambda: SimpleNamespace(get=policy))
    context = SimpleNamespace(account_type="human")
    caller = await authz.resolve_caller(context, frozenset({"adp-tasks/submit"}), object())
    assert caller.principal_id == "human:" + USER and caller.tenant_id == TENANT
    assert caller.scopes == frozenset({"adp-tasks/read"})
    with pytest.raises(errors.TaskApiError):
        caller.require("adp-tasks/submit")
    policy.return_value = {"status": "disabled", "task_scopes": ["read"]}
    with pytest.raises(errors.TaskApiError):
        await authz.resolve_caller(context, frozenset(), object())
    policy.return_value = {"status": "active", "task_scopes": ["read"]}
    membership.side_effect = BootstrapRefusedError("removed")
    with pytest.raises(errors.TaskApiError):
        await authz.resolve_caller(context, frozenset(), object())


@pytest.mark.asyncio
async def test_model_owner_retains_human_identity_and_current_membership(monkeypatch):
    from datetime import UTC, datetime

    from src.agentauth import task_model_binding as model
    from src.agentauth.model_policy import ModelPolicyError

    monkeypatch.setenv("ADP_TASK_API_HUMAN_ENABLED", "true")
    member = AsyncMock()
    monkeypatch.setattr(human, "require_live_human_membership", member)
    resolve = AsyncMock(return_value=SimpleNamespace(principal_status=None, service_policy_unavailable_reason=None))
    monkeypatch.setattr(model, "_resolve_active_allowlist_policy", resolve)
    db = SimpleNamespace(scalar=AsyncMock(return_value=None))
    with pytest.raises(ModelPolicyError, match="task_model_selection_missing"):
        await model.resolve_task_model(
            db, tenant=TENANT, principal=human.human_locator(USER), deadline=datetime.now(UTC), expected_policy_version="1"
        )
    assert resolve.call_args.kwargs["principal_kind"] == "human"
    assert resolve.call_args.kwargs["principal_id"] == USER
    member.assert_awaited_once_with(db, user_id=USER, tenant_id=TENANT)
    member.side_effect = BootstrapRefusedError("removed")
    with pytest.raises(errors.TaskApiError):
        await human.require_current_owner(db, tenant=TENANT, principal=human.human_locator(USER))


@pytest.mark.asyncio
async def test_signed_selected_tenant_wins_over_login_org_and_revocation_denies(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from src.auth import tenant_context
    from src.shared.schemas.auth import TokenContext

    selected = str(uuid.uuid4())
    user = SimpleNamespace(id=USER)
    members = {TENANT: (user, SimpleNamespace(id="member-a")), selected: (user, SimpleNamespace(id="member-b"))}
    monkeypatch.setattr(
        tenant_context,
        "get_settings",
        lambda: SimpleNamespace(token_secret_key="tenant-test-secret-key-with-enough-length", cognito_user_pool_id="pool"),
    )
    monkeypatch.setattr(tenant_context, "memberships_for_login", AsyncMock(side_effect=lambda *a, **k: (None, members)))
    monkeypatch.setattr(tenant_context, "primary_team_for_workspace", AsyncMock(return_value=None))
    context = TokenContext(
        user_id=USER,
        org_id=TENANT,
        team_id="",
        department_id="",
        account_type="human",
        auth_source="jwt",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    lease = await tenant_context.issue_context(object(), context, selected)
    context._task_tenant_lease = lease["context_token"]
    monkeypatch.setenv("ADP_TASK_API_HUMAN_ENABLED", "true")
    session = AsyncMock(side_effect=lambda ctx, db: SimpleNamespace(user_id=USER, tenant_id=ctx.org_id))
    monkeypatch.setattr(human, "authorize_human_session", session)
    policy = Mock(return_value={"status": "active", "task_scopes": ["read"]})
    monkeypatch.setattr(human, "TaskServicePolicyStore", lambda: SimpleNamespace(get=policy))
    caller = await authz.resolve_caller(context, frozenset(), object())
    assert caller.tenant_id == selected
    assert policy.call_args.kwargs["tenant_id"] == selected
    assert context.org_id == TENANT
    del members[selected]
    with pytest.raises(errors.TaskApiError):
        await authz.resolve_caller(context, frozenset(), object())


@pytest.mark.asyncio
async def test_human_budget_preflight_uses_actual_owner_and_no_new_ledger():
    from decimal import Decimal

    enforcement = SimpleNamespace(check_budget_hierarchy=AsyncMock(return_value=SimpleNamespace(allowed=False)))
    context = SimpleNamespace(user_id=USER, org_id=TENANT)
    with pytest.raises(errors.TaskApiError) as refused:
        await human.require_admission_headroom(context, "0.25", enforcement=enforcement)
    assert refused.value.status == 402 and refused.value.code == "budget_exceeded"
    enforcement.check_budget_hierarchy.assert_awaited_once_with(context, Decimal("0.25"), request_id=None)
    assert context._budget_enforcement_enabled


@pytest.mark.asyncio
async def test_human_budget_refusal_precedes_task_acceptance(monkeypatch):
    from src.agentauth.task_admission import TaskAdmission

    repository = SimpleNamespace(_read_idempotency=Mock(return_value=None), accept=Mock())
    policy = {
        "status": "active",
        "task_scopes": ["submit"],
        "allowed_personas": ["agent-task-investigator"],
        "allowed_tools": [],
        "model_policy_version": "1",
        "limits": {"max_duration_minutes": 10, "max_usd_per_task": 0.1},
    }
    policies = SimpleNamespace(get=Mock(return_value=policy))
    budget = SimpleNamespace(reserve_admission=AsyncMock())
    model = AsyncMock(return_value=({}, SimpleNamespace(context=object()), object()))
    monkeypatch.setattr(human, "require_admission_headroom", AsyncMock(side_effect=errors.TaskApiError(402, "budget_exceeded", "exhausted")))
    admission = TaskAdmission(repository, policies=policies, budget=budget, model_resolver=model)
    caller = authz.Caller("human:" + USER, TENANT, frozenset({"adp-tasks/submit"}))
    with pytest.raises(errors.TaskApiError) as refused:
        await admission.admit(
            caller=caller,
            submit={"schema_version": "1.0", "persona": "agent-task-investigator", "instructions": "inspect"},
            idempotency_key="one",
            db=object(),
        )
    assert refused.value.status == 402
    repository.accept.assert_not_called()
    budget.reserve_admission.assert_not_called()
    assert model.call_args.kwargs["include_context"] is True
