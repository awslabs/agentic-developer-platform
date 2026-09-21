"""A service's role or attribution cannot become human continuation authority."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.orchestration.continuation_routes import _call
from src.shared.schemas.auth import TokenContext


@pytest.mark.parametrize(
    "account_type,auth_source,expired", [("service", "jwt", False), ("service", "iam", False), ("human", "iam", False), ("human", "jwt", True)]
)
async def test_service_and_expired_contexts_cannot_accept_continuation(account_type, auth_source, expired):
    context = TokenContext(
        user_id="operator",
        org_id="org",
        team_id="",
        department_id="",
        account_type=account_type,
        auth_source=auth_source,
        expires_at=datetime.now(UTC) + timedelta(hours=-1 if expired else 1),
    )
    db = AsyncMock()
    with pytest.raises(HTTPException) as error:
        await _call(accept=True, flow_id="flow", body=SimpleNamespace(), current_user=context, access=AsyncMock(), db=db, resolver=AsyncMock())
    assert error.value.status_code == 403
    db.execute.assert_not_called()
    db.commit.assert_not_called()


async def test_human_canonical_identity_is_stamped_after_session_verification(monkeypatch):
    human = SimpleNamespace(user_id="canonical-user", tenant_id="org")
    verify = AsyncMock(return_value=human)
    preview = AsyncMock(return_value={"flow_id": "flow", "ready": True})
    monkeypatch.setattr("src.agentauth.human_control.authorize_human_session", verify)
    monkeypatch.setattr("src.orchestration.routes._resolve_actor_role", AsyncMock(return_value="owner"))
    monkeypatch.setattr("src.orchestration.continuation_routes.preview_continuation", preview)
    context = TokenContext(
        user_id="cognito-sub", org_id="org", team_id="", department_id="", account_type="human", expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    await _call(
        accept=False,
        flow_id="flow",
        body=SimpleNamespace(reconciliation_evidence="reconciled"),
        current_user=context,
        access=AsyncMock(),
        db=AsyncMock(),
        resolver=AsyncMock(),
    )
    actor = preview.await_args.kwargs["actor"]
    assert actor.actor_id == "canonical-user" and actor.org_id == "org" and actor.actor_kind.value == "human"
