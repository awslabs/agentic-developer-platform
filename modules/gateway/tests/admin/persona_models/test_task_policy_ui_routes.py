from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from src.admin.persona_models import task_policy_ui_routes as routes
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext


def app_for(account_type="human"):
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: TokenContext(
        user_id="admin",
        org_id="home",
        team_id="",
        department_id="",
        account_type=account_type,
        auth_source="jwt",
        is_admin=False,
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[routes.task_policy_store] = lambda: object()
    return app, db


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/admin/organizations/other/task-policies/p", "/admin/organizations/other/task-policy-identity?source=cognito&identity_id=c"]
)
async def test_service_cannot_read_admin_task_policy(path):
    app, db = app_for("service")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get(path)
    assert result.status_code == 403
    db.scalars.assert_not_called()


@pytest.mark.asyncio
async def test_target_organization_authorized_before_identity_lookup(monkeypatch):
    app, db = app_for()
    checked = []

    class Access:
        def __init__(self, db):
            pass

        async def check_permission(self, user, permission, *, target_org_id):
            checked.append(target_org_id)
            raise HTTPException(403, "denied")

    monkeypatch.setattr(routes, "AccessControl", Access)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get("/admin/organizations/other/task-policy-identity?source=cognito&identity_id=c")
    assert result.status_code == 403
    assert checked == ["other"]
    db.scalars.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("ids,status", [([], 404), (["p", "q"], 409), (["p", "p"], 200)])
async def test_alias_consensus_never_selects_arbitrary_principal(monkeypatch, ids, status):
    app, db = app_for()
    monkeypatch.setattr(routes, "authorize", AsyncMock())
    target = AsyncMock()
    monkeypatch.setattr(routes, "target", target)
    db.scalars.return_value = ids
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get("/admin/organizations/selected/task-policy-identity?source=cognito&identity_id=c")
    assert result.status_code == status
    query = str(db.scalars.call_args.args[0].compile(compile_kwargs={"literal_binds": True}))
    assert "selected" in query and "cognito_m2m" in query and "is_active IS true" in query
    if status == 200:
        assert result.json() == {"tenant_id": "selected", "canonical_principal_id": "p"}
        target.assert_awaited_once_with(db, "p", "selected")
    else:
        target.assert_not_called()


@pytest.mark.asyncio
async def test_reservation_preview_preserves_full_context_bound(monkeypatch):
    from decimal import Decimal
    from types import SimpleNamespace

    import pricing_policy
    import src.budget.pricing_v2_reader as reader

    model = "anthropic.claude-sonnet-4-6"
    row = SimpleNamespace(
        model_id=model,
        service_tier="standard",
        input_price_per_1k_tokens=Decimal(".0055"),
        output_price_per_1k_tokens=Decimal(".0275"),
        cache_write_price_per_1k_tokens=Decimal(".006875"),
        cache_write_1h_price_per_1k_tokens=Decimal(".011"),
    )
    monkeypatch.setattr(
        pricing_policy,
        "load_snapshot",
        lambda: SimpleNamespace(snapshot_version="test", models={model: {"context_max_input_tokens": 1000000}}, rates=(row,)),
    )
    monkeypatch.setattr(reader, "cached_rate_state", lambda: SimpleNamespace(rows=(), source="test", generation_id=1, pointer_revision=1))
    app, _ = app_for()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get("/task-reservation-preview", params={"model": model, "max_output_tokens": 4096})
        missing = await client.get("/task-reservation-preview", params={"model": "unknown"})
    assert result.status_code == 200
    assert result.json()["reservation_usd"] == "11.112640"
    assert result.json()["max_input_tokens"] == 1000000
    assert missing.json()["status"] == "unavailable"
    assert "reservation_usd" not in missing.json()


@pytest.mark.asyncio
async def test_policy_view_projects_revisions_without_granting_missing_policy(monkeypatch):
    from unittest.mock import Mock

    store = Mock()
    store.get.return_value = None
    monkeypatch.setenv("ADP_TASK_MAX_USD_PER_TASK", "20")
    monkeypatch.setenv("ADP_TASK_PERSONA_TOOLS", '{"agent-task-cyber":["cyber.browser_start"]}')
    monkeypatch.setattr(
        routes.service,
        "build_preference_list",
        AsyncMock(
            return_value=[
                {"persona_key": "agent-task-cyber", "effective_model_id": "opus", "revision": 3},
                {"persona_key": "agent-task-investigator", "effective_model_id": "default"},
            ]
        ),
    )
    result = await routes.view(object(), store, "selected", "service_account", "canonical")
    assert result["policy"] is None
    assert result["platform_limits"]["max_usd_per_task"] == 20
    assert result["models"][0]["revision"] == "3"
    assert result["models"][1]["revision"] is None
    assert result["persona_tools"]["agent-task-cyber"] == ["cyber.browser_start"]
    store.get.assert_called_once_with(tenant_id="selected", canonical_principal_id="canonical")
    store.put.assert_not_called()


@pytest.mark.asyncio
async def test_policy_save_keeps_selected_tenant_actor_and_version(monkeypatch):
    from unittest.mock import Mock

    from src.agentauth.task_service_policy import TaskServicePolicyError

    app, db = app_for()
    authorize = AsyncMock()
    monkeypatch.setattr(routes, "authorize", authorize)
    monkeypatch.setattr(routes, "target", AsyncMock())
    monkeypatch.setattr(routes.service, "validate_human_principal", AsyncMock(return_value="home-admin"))
    store = Mock()
    store.put.side_effect = TaskServicePolicyError("version_conflict")
    app.dependency_overrides[routes.task_policy_store] = lambda: store
    body = {
        "expected_version": 4,
        "status": "active",
        "allowed_personas": ["agent-task-cyber"],
        "allowed_tools": [],
        "task_scopes": ["submit"],
        "model_policy_version": "1",
        "model_policy_versions": {"agent-task-cyber": "3"},
        "limits": {"max_duration_minutes": 60, "max_turns": 8, "max_output_tokens_per_turn": 4096, "max_usd_per_task": "1"},
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.put("/admin/organizations/selected/task-policies/canonical", json=body)
    assert result.status_code == 409
    assert authorize.call_args.args[2] == "selected"
    call = store.put.call_args.kwargs
    assert (call["tenant_id"], call["canonical_principal_id"], call["updated_by"], call["expected_version"]) == (
        "selected",
        "canonical",
        "home-admin",
        4,
    )
    assert call["policy"]["model_policy_versions"] == {"agent-task-cyber": "3"}
