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


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", ["agent-task-claude-developer", "agent-task-codex-developer"])
async def test_coding_requires_explicit_repository_enrollment(monkeypatch, persona):
    monkeypatch.setattr(routes, "enrolled_target", AsyncMock(return_value=(SimpleNamespace(user_id=USER, tenant_id=TENANT), "human:" + USER)))
    store = Mock()
    with pytest.raises(HTTPException) as refused:
        await routes.put_policy(USER, body(persona), object(), object(), store)
    assert refused.value.status_code == 422
    store.put.assert_not_called()


def test_coding_repository_scope_retains_dynamo_decimal_identity():
    from decimal import Decimal

    scope = routes.RepositoryScope.model_validate({"repository_id": Decimal(42), "repository": "owner/repo", "path_prefixes": ["cli"]})
    assert type(scope.repository_id) is int
    assert scope.repository_id == 42
    for path in ("../token", ".git/config", "cli/../token", "cli/bad name.py"):
        with pytest.raises(ValueError):
            routes.RepositoryScope.model_validate({"repository_id": 42, "repository": "owner/repo", "path_prefixes": [path]})


@pytest.mark.asyncio
async def test_native_developer_requires_validated_repository_binding(monkeypatch):
    monkeypatch.setattr(routes, "enrolled_target", AsyncMock(return_value=(SimpleNamespace(user_id=USER, tenant_id=TENANT), "human:" + USER)))
    store = Mock()
    request = body("agent-task-gpt-developer")
    with pytest.raises(HTTPException) as refused:
        await routes.put_policy(USER, request, object(), object(), store)
    assert refused.value.status_code == 422
    store.put.assert_not_called()


@pytest.mark.asyncio
async def test_native_developer_enrollment_preserves_repository_authority(monkeypatch):
    from datetime import UTC, datetime

    monkeypatch.setattr(routes, "enrolled_target", AsyncMock(return_value=(SimpleNamespace(user_id=USER, tenant_id=TENANT), "human:" + USER)))
    document = body("agent-task-gpt-developer").model_dump()
    document["repositories"] = {
        "adp": {
            "provider": "github",
            "connection_id": "installation:123",
            "repository_id": "42",
            "repository": "owner/repo",
            "base_branch": "main",
            "validation_checks": [{"name": "unit", "image": "registry.example/checks@sha256:" + "a" * 64, "argv": ["python3", "-m", "unittest"]}],
        }
    }
    request = TaskPolicyPutRequest.model_validate(document)
    policy = request.model_dump(exclude={"expected_version"})
    stored = {
        **policy,
        "tenant_id": TENANT,
        "canonical_principal_id": "human:" + USER,
        "version": 1,
        "updated_at": datetime.now(UTC),
        "updated_by": USER,
    }
    store = Mock()
    store.put.return_value = stored
    result = await routes.put_policy(USER, request, object(), object(), store)
    assert result.repositories["adp"] == request.repositories["adp"]
    assert store.put.call_args.kwargs["canonical_principal_id"] == "human:" + USER
    assert store.put.call_args.kwargs["expected_version"] == 0
    assert store.put.call_args.kwargs["policy"]["repositories"] == policy["repositories"]
