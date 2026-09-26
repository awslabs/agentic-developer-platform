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
