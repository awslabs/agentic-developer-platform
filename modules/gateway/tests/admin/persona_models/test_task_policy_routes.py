from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.admin.persona_models import routes
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

BODY = {
    "expected_version": 0,
    "status": "active",
    "allowed_personas": ["agent-task-investigator"],
    "task_scopes": ["submit", "read"],
    "model_policy_version": "models-v1",
    "limits": {
        "max_duration_minutes": 30,
        "max_turns": 8,
        "max_output_tokens_per_turn": 4096,
        "max_usd_per_task": 1,
    },
}


class Store:
    def __init__(self):
        self.calls = []

    def get(self, **kwargs):
        return None

    def put(self, **kwargs):
        self.calls.append(kwargs)
        return {
            **kwargs["policy"],
            "schema_version": "1.0",
            "tenant_id": kwargs["tenant_id"],
            "canonical_principal_id": kwargs["canonical_principal_id"],
            "version": kwargs["expected_version"] + 1,
            "updated_at": datetime(2026, 9, 24, tzinfo=UTC),
            "updated_by": kwargs["updated_by"],
        }


def context(account_type="human", org_id="tenant-1"):
    return TokenContext(
        user_id="user-1", org_id=org_id, team_id="", department_id="",
        account_type=account_type, auth_source="jwt", is_admin=False,
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )


def app_for(current, store):
    app = FastAPI()
    app.include_router(routes.router)

    async def db():
        yield object()

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_current_user] = lambda: current
    app.dependency_overrides[routes.task_policy_store] = lambda: store
    return app


@pytest.mark.asyncio
async def test_service_principal_cannot_self_grant_task_policy():
    store = Store()
    app = app_for(context("service"), store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put("/service-principals/principal-1/task-policy", json=BODY)
    assert response.status_code == 403
    assert store.calls == []


@pytest.mark.asyncio
async def test_human_org_admin_write_uses_same_tenant_canonical_validation(monkeypatch):
    store = Store()
    checked = []

    class Access:
        def __init__(self, db):
            pass

        async def check_permission(self, current, permission, *, target_org_id):
            checked.append(target_org_id)

    async def validate_target(db, *, canonical_id, org_id):
        checked.append((canonical_id, org_id))
        return object()

    async def validate_human(db, *, user_id, org_id):
        checked.append((user_id, org_id))
        return "canonical-human-1"

    monkeypatch.setattr(routes, "AccessControl", Access)
    monkeypatch.setattr(routes.service, "validate_target_service_principal", validate_target)
    monkeypatch.setattr(routes.service, "validate_human_principal", validate_human)
    app = app_for(context(), store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put("/service-principals/principal-1/task-policy", json=BODY)
    assert response.status_code == 200
    assert response.json()["version"] == 1
    assert checked == ["tenant-1", ("principal-1", "tenant-1"), ("user-1", "tenant-1")]
    assert store.calls[0]["expected_version"] == 0
    assert store.calls[0]["updated_by"] == "canonical-human-1"


@pytest.mark.asyncio
async def test_policy_body_is_closed_and_enforces_platform_ceiling():
    store = Store()
    app = app_for(context("service"), store)
    invalid = {**BODY, "tenant_id": "attacker", "limits": {**BODY["limits"], "max_usd_per_task": 2}}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put("/service-principals/principal-1/task-policy", json=invalid)
    assert response.status_code == 422
    assert store.calls == []
