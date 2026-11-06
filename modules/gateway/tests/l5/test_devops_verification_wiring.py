"""The authenticated gateway passes its configured verification reader into L5 admission."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src import l5_integration


@pytest.mark.asyncio
@pytest.mark.parametrize("service_id,configured", [("devops", True), ("devops", False), ("security", True)])
async def test_gateway_verification_reader_is_server_selected(monkeypatch, service_id, configured):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    selected_reader = object()
    if configured:
        app.state.l5_devops_verification_reader = selected_reader
    request = Request({"type": "http", "app": app})
    actor = SimpleNamespace(org_id="tenant-one", user_id="owner-one", account_type="service")
    calls = []
    receipt = object()

    async def authority(self, product_id, project_id, selected_service, body):
        assert (product_id, project_id, selected_service) == ("product-one", "project-one", service_id)
        return {"scope": {"tenant_id": "tenant-one", "project_id": "project-one"}}, None

    async def record(session, scoped_sessions, source_client, publish, body, verified, key, **kwargs):
        calls.append((body, kwargs["verification_reader"], verified["scope"], key))
        return receipt

    monkeypatch.setattr(l5_integration.GatewayConfigurationBackend, "admission_authority", authority)
    monkeypatch.setattr(l5_integration, "submit_and_dispatch", record)
    async with sessions() as session:
        gateway = l5_integration.backend(service_id, request, db=session, actor=actor, x_l5_delegation_id=None)
        result = await gateway.submit("product-one", "project-one", service_id, {"capability": "release_verification"}, "offline-idempotency")
    assert result is receipt
    assert calls == [
        (
            {"capability": "release_verification"},
            selected_reader if configured and service_id == "devops" else None,
            {"tenant_id": "tenant-one", "project_id": "project-one"},
            "offline-idempotency",
        )
    ]
    await engine.dispose()
