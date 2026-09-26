"""Standing human enrollment cannot create a service or unsupported executable."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from src.admin.persona_models import human_task_routes as routes
from src.admin.persona_models.schemas import TaskPolicyPutRequest
from src.agentauth.bootstrap import BootstrapRefusedError

USER = str(uuid.uuid4())
TENANT = str(uuid.uuid4())


def body(persona="agent-task-investigator"):
    return TaskPolicyPutRequest.model_validate(
        {
            "expected_version": 0,
            "status": "active",
            "allowed_personas": [persona],
            "task_scopes": ["submit", "read"],
            "model_policy_version": "1",
            "limits": {"max_duration_minutes": 10, "max_turns": 2, "max_output_tokens_per_turn": 100, "max_usd_per_task": 0.1},
        }
    )


@pytest.mark.asyncio
async def test_enrollment_checks_current_admin_and_exact_member(monkeypatch):
    admin = AsyncMock()
    member = AsyncMock()
    monkeypatch.setattr(routes, "_require_human_org_admin", admin)
    monkeypatch.setattr(routes, "authorize_human_session", AsyncMock(return_value=SimpleNamespace(user_id=USER, tenant_id=TENANT)))
    monkeypatch.setattr(routes, "require_live_human_membership", member)
    context, db = object(), object()
    _, locator = await routes.enrolled_target(db, context, USER)
    assert locator == "human:" + USER
    admin.assert_awaited_once_with(db, context)
    member.assert_awaited_once_with(db, user_id=USER, tenant_id=TENANT)
    member.side_effect = BootstrapRefusedError("foreign")
    with pytest.raises(HTTPException) as denied:
        await routes.enrolled_target(db, context, USER)
    assert denied.value.status_code == 404


@pytest.mark.asyncio
async def test_unsupported_persona_never_creates_authority(monkeypatch):
    monkeypatch.setattr(routes, "enrolled_target", AsyncMock(return_value=(SimpleNamespace(user_id=USER, tenant_id=TENANT), "human:" + USER)))
    store = Mock()
    with pytest.raises(HTTPException) as refused:
        await routes.put_policy(USER, body("developer"), object(), object(), store)
    assert refused.value.status_code == 422
    store.put.assert_not_called()
