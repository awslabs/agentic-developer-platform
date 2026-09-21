"""Both append operations use the existing human plan-approval boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.admin.config import Permission
from src.agentauth.bootstrap import BootstrapRefusedError
from src.orchestration import shared_amendment_routes as routes
from src.orchestration.shared_amendment import SharedAppendError


@pytest.mark.parametrize("accept", [False, True])
async def test_append_rejects_nonhuman_sessions_before_read_or_write(monkeypatch, accept):
    access = SimpleNamespace(check_permission=AsyncMock())
    monkeypatch.setattr(routes, "AccessControl", lambda _: access)
    monkeypatch.setattr("src.agentauth.human_control.authorize_human_session", AsyncMock(side_effect=BootstrapRefusedError("not human")))
    handler = AsyncMock()
    monkeypatch.setattr(routes, "accept_shared_append" if accept else "preview_shared_append", handler)
    endpoint = routes.accept_append if accept else routes.preview_append
    user = SimpleNamespace(org_id="org")
    with pytest.raises(HTTPException) as error:
        await endpoint(
            flow_id="flow",
            body=SimpleNamespace(reason="append prerequisites"),
            current_user=user,
            db=SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock()),
        )
    assert error.value.status_code == 403
    access.check_permission.assert_awaited_once_with(user, Permission.PLAN_APPROVE, target_org_id="org")
    handler.assert_not_awaited()


@pytest.mark.parametrize("accept", [False, True])
async def test_append_endpoint_denies_before_read_or_write_without_plan_permission(monkeypatch, accept):
    access = SimpleNamespace(check_permission=AsyncMock(side_effect=HTTPException(403, "permission denied")))
    monkeypatch.setattr(routes, "AccessControl", lambda _: access)
    human = AsyncMock()
    monkeypatch.setattr("src.agentauth.human_control.authorize_human_session", human)
    handler = AsyncMock()
    monkeypatch.setattr(routes, "accept_shared_append" if accept else "preview_shared_append", handler)
    user = SimpleNamespace(org_id="org")
    endpoint = routes.accept_append if accept else routes.preview_append
    with pytest.raises(HTTPException) as error:
        await endpoint(
            flow_id="flow",
            body=SimpleNamespace(reason="append prerequisites"),
            current_user=user,
            db=SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock()),
        )
    assert error.value.status_code == 403
    access.check_permission.assert_awaited_once_with(user, Permission.PLAN_APPROVE, target_org_id="org")
    human.assert_not_awaited()
    handler.assert_not_awaited()


async def test_append_dispatch_contention_returns_retryable_conflict(monkeypatch):
    monkeypatch.setattr(routes, "AccessControl", lambda _: SimpleNamespace(check_permission=AsyncMock()))
    monkeypatch.setattr(
        "src.agentauth.human_control.authorize_human_session", AsyncMock(return_value=SimpleNamespace(tenant_id="org", user_id="human"))
    )
    monkeypatch.setattr("src.orchestration.routes._resolve_actor_role", AsyncMock(return_value="org_admin"))
    monkeypatch.setattr(routes, "accept_shared_append", AsyncMock(side_effect=SharedAppendError("amendment_dispatch_in_progress")))
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    with pytest.raises(HTTPException) as error:
        await routes.accept_append(
            flow_id="flow", body=SimpleNamespace(reason="append prerequisites"), current_user=SimpleNamespace(org_id="org"), db=db
        )
    assert error.value.status_code == 409
    assert error.value.detail == {"code": "amendment_dispatch_in_progress", "retryable": True}
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
