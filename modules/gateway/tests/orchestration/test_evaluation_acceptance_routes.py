"""The narrow evidence API uses the existing human plan-approval boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.admin.config import Permission
from src.agentauth.bootstrap import BootstrapRefusedError
from src.orchestration import evaluation_acceptance_routes as routes


@pytest.mark.parametrize("accept", [False, True])
async def test_contract_api_requires_plan_permission_and_resolves_human_server_side(monkeypatch, accept):
    access = SimpleNamespace(check_permission=AsyncMock())
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    user = SimpleNamespace(org_id="org")
    monkeypatch.setattr(routes, "AccessControl", lambda db: access)
    human = AsyncMock(return_value=SimpleNamespace(tenant_id="org", user_id="resolved-human"))
    monkeypatch.setattr("src.agentauth.human_control.authorize_human_session", human)
    monkeypatch.setattr("src.orchestration.routes._resolve_actor_role", AsyncMock(return_value="org_admin"))
    handler = AsyncMock(return_value={"accepted": accept})
    monkeypatch.setattr(routes, "accept_evaluation" if accept else "preview_evaluation", handler)
    await routes.call(accept=accept, flow_id="flow", body=SimpleNamespace(reason="Accept these exact checks"), current_user=user, db=db)
    access.check_permission.assert_awaited_once_with(user, Permission.PLAN_APPROVE, target_org_id="org")
    assert handler.call_args.kwargs["actor"].actor_id == "resolved-human"
    assert db.commit.await_count == int(accept)


async def test_service_session_cannot_accept_contract_even_with_generic_permission(monkeypatch):
    monkeypatch.setattr(routes, "AccessControl", lambda db: SimpleNamespace(check_permission=AsyncMock()))
    monkeypatch.setattr("src.agentauth.human_control.authorize_human_session", AsyncMock(side_effect=BootstrapRefusedError()))
    handler = AsyncMock()
    monkeypatch.setattr(routes, "accept_evaluation", handler)
    with pytest.raises(HTTPException) as error:
        await routes.call(
            accept=True,
            flow_id="flow",
            body=SimpleNamespace(reason="accept"),
            current_user=SimpleNamespace(org_id="org"),
            db=SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock()),
        )
    assert error.value.status_code == 403
    handler.assert_not_awaited()
