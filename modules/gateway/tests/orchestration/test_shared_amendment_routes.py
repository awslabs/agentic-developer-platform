"""Both append operations use the existing human plan-approval boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.agentauth.bootstrap import BootstrapRefusedError
from src.orchestration import shared_amendment_routes as routes


@pytest.mark.parametrize("accept", [False, True])
async def test_append_rejects_nonhuman_sessions_before_read_or_write(monkeypatch, accept):
    access = SimpleNamespace(check_permission=AsyncMock())
    monkeypatch.setattr(routes, "AccessControl", lambda _: access)
    monkeypatch.setattr("src.agentauth.human_control.authorize_human_session", AsyncMock(side_effect=BootstrapRefusedError("not human")))
    handler = AsyncMock()
    monkeypatch.setattr(routes, "accept_shared_append" if accept else "preview_shared_append", handler)
    with pytest.raises(HTTPException) as error:
        await routes.call(
            accept=accept,
            flow_id="flow",
            body=SimpleNamespace(reason="append prerequisites"),
            current_user=SimpleNamespace(org_id="org"),
            db=SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock()),
        )
    assert error.value.status_code == 403
    access.check_permission.assert_awaited_once()
    handler.assert_not_awaited()
