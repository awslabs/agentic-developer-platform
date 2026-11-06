"""Only a human platform administrator with plan authority may adopt maintenance."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from src.admin.config import Permission
from src.agentauth.bootstrap import BootstrapRefusedError
from src.orchestration import maintenance_adoption_routes as routes
from src.orchestration.review_cycle import CycleBlockedError


@pytest.mark.parametrize("accept", [False, True])
@pytest.mark.parametrize("denied", [None, "admin", "permission", "service"])
async def test_adoption_requires_human_admin_and_plan_authority(monkeypatch, accept, denied):
    access = SimpleNamespace(
        require_platform_admin=Mock(side_effect=HTTPException(403) if denied == "admin" else None),
        check_permission=AsyncMock(side_effect=HTTPException(403) if denied == "permission" else None),
    )
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    user = SimpleNamespace(org_id="org")
    monkeypatch.setattr(routes, "AccessControl", lambda _: access)
    human = AsyncMock(
        return_value=SimpleNamespace(tenant_id="org", user_id="resolved-human"), side_effect=BootstrapRefusedError() if denied == "service" else None
    )
    monkeypatch.setattr("src.agentauth.human_control.authorize_human_session", human)
    monkeypatch.setattr("src.orchestration.routes._resolve_actor_role", AsyncMock(return_value="platform_admin"))
    handler = AsyncMock(return_value={"accepted": accept})
    monkeypatch.setattr(routes, "adopt_maintenance", handler)
    endpoint = routes.accept if accept else routes.preview
    args = dict(flow_id="flow", body=SimpleNamespace(reason="Adopt verified maintenance"), current_user=user, db=db)
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
    if denied != "admin":
        access.check_permission.assert_awaited_once_with(user, Permission.PLAN_APPROVE, target_org_id="org")


async def test_stale_preview_returns_actionable_conflict_without_commit(monkeypatch):
    monkeypatch.setattr(routes, "AccessControl", lambda _: SimpleNamespace(require_platform_admin=Mock(), check_permission=AsyncMock()))
    monkeypatch.setattr(
        "src.agentauth.human_control.authorize_human_session", AsyncMock(return_value=SimpleNamespace(tenant_id="org", user_id="human"))
    )
    monkeypatch.setattr("src.orchestration.routes._resolve_actor_role", AsyncMock(return_value="platform_admin"))
    monkeypatch.setattr(routes, "adopt_maintenance", AsyncMock(side_effect=CycleBlockedError("maintenance_preview_changed")))
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    with pytest.raises(HTTPException) as error:
        await routes.accept(flow_id="flow", body=SimpleNamespace(reason="Reviewed maintenance"), current_user=SimpleNamespace(org_id="org"), db=db)
    assert error.value.status_code == 409 and error.value.detail["code"] == "maintenance_preview_changed"
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
