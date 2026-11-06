"""The recovery API cannot be used through a worker or without plan authority."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.admin.config import Permission
from src.agentauth.bootstrap import BootstrapRefusedError
from src.orchestration import build_completion_routes as routes
from src.orchestration.build_completion import BuildCompletionError


@pytest.mark.parametrize("accept", [False, True])
@pytest.mark.parametrize("denied", [None, "permission", "service"])
async def test_recovery_requires_plan_permission_and_a_resolved_human(monkeypatch, accept, denied):
    access = SimpleNamespace(check_permission=AsyncMock(side_effect=HTTPException(403) if denied == "permission" else None))
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    user = SimpleNamespace(org_id="org")
    monkeypatch.setattr(routes, "AccessControl", lambda _: access)
    human = AsyncMock(
        return_value=SimpleNamespace(tenant_id="org", user_id="resolved-human"), side_effect=BootstrapRefusedError() if denied == "service" else None
    )
    monkeypatch.setattr("src.agentauth.human_control.authorize_human_session", human)
    monkeypatch.setattr("src.orchestration.routes._resolve_actor_role", AsyncMock(return_value="org_admin"))
    handler = AsyncMock(return_value={"accepted": accept})
    monkeypatch.setattr(routes, "accept_build_completion" if accept else "preview_build_completion", handler)
    endpoint = routes.accept_contract if accept else routes.preview_contract
    args = dict(flow_id="flow", body=SimpleNamespace(reason="Retain deployed human acceptance"), current_user=user, db=db)
    if denied:
        with pytest.raises(HTTPException) as error:
            await endpoint(**args)
        assert error.value.status_code == 403
        handler.assert_not_awaited()
        db.commit.assert_not_awaited()
    else:
        await endpoint(**args)
        assert handler.call_args.kwargs["actor"].actor_id == "resolved-human"
        assert db.commit.await_count == int(accept)
    access.check_permission.assert_awaited_once_with(user, Permission.PLAN_APPROVE, target_org_id="org")
    if denied == "permission":
        human.assert_not_awaited()


async def test_execution_contention_returns_retryable_conflict_without_committing(monkeypatch):
    monkeypatch.setattr(routes, "AccessControl", lambda _: SimpleNamespace(check_permission=AsyncMock()))
    monkeypatch.setattr(
        "src.agentauth.human_control.authorize_human_session", AsyncMock(return_value=SimpleNamespace(tenant_id="org", user_id="human"))
    )
    monkeypatch.setattr("src.orchestration.routes._resolve_actor_role", AsyncMock(return_value="org_admin"))
    monkeypatch.setattr(routes, "accept_build_completion", AsyncMock(side_effect=BuildCompletionError("build_completion_in_progress")))
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    with pytest.raises(HTTPException) as error:
        await routes.accept_contract(
            flow_id="flow", body=SimpleNamespace(reason="Retain delivery"), current_user=SimpleNamespace(org_id="org"), db=db
        )
    assert error.value.status_code == 409 and error.value.detail["retryable"] is True
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
