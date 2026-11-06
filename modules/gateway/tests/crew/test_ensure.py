"""Crew admission never exposes disabled or unauthorized target kinds."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.crew.proxy import current_crew_owner, router
from src.crew.targets import CrewOwner
from src.shared.database import get_db


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.delenv("FEATURE_CREW_DASHBOARD", raising=False)
    monkeypatch.setenv("FEATURE_ASSISTANT_UI_ENABLED", "true")
    monkeypatch.setenv("CREW_PROXY_ENABLED", "true")
    monkeypatch.setenv("CREW_DASHBOARD_ORIGIN", "https://crew.example.test")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[current_crew_owner] = lambda: CrewOwner("tenant-a", "user-a")
    db = AsyncMock()
    db.scalar.return_value = {"crew": {"allowed_targets": ["eks-pod"]}}
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http, db


async def test_flag_off_hides_ensure_before_any_policy_access(client):
    http, db = client
    result = await http.post("/crew/ensure", json={})
    assert result.status_code == 404
    db.scalar.assert_not_awaited()


async def test_wrong_runtime_and_tenant_policy_refuse_ensure(client, monkeypatch):
    http, db = client
    monkeypatch.setenv("FEATURE_CREW_DASHBOARD", "true")
    result = await http.post("/crew/ensure", json={"runtime": "adp-assistant"})
    assert result.status_code == 404
    db.scalar.assert_not_awaited()

    db.scalar.return_value = {"crew": {"allowed_targets": ["ec2-instance"]}}
    result = await http.post("/crew/ensure", json={})
    assert result.status_code == 404
    assert result.json()["detail"]["error"] == "crew_runtime_unavailable"
    assert (await http.get("/crew/runtime")).json() == {"allowed_targets": ["ec2-instance"]}


async def test_allowed_runtime_refuses_unimplemented_wake_without_registering(client, monkeypatch):
    http, db = client
    monkeypatch.setenv("FEATURE_CREW_DASHBOARD", "true")
    result = await http.post("/crew/ensure", json={})
    assert result.status_code == 503
    assert result.json()["detail"]["error"] == "crew_admission_unavailable"
    db.add.assert_not_called()


async def test_malformed_policy_fails_closed(client, monkeypatch):
    http, db = client
    monkeypatch.setenv("FEATURE_CREW_DASHBOARD", "true")
    db.scalar.return_value = {"crew": {"allowed_targets": ["unknown"]}}
    result = await http.post("/crew/ensure", json={})
    assert result.status_code == 503
    assert result.json()["detail"]["error"] == "crew_policy_unavailable"
