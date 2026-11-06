from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.agentauth import routes
from tests.agentauth.test_campaign_execution import native as native_fixture
from tests.agentauth.test_campaign_execution import retirement

native = native_fixture


async def request(native, *, credential=None, workload="root"):
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.require_agent_transport] = lambda: None
    app.dependency_overrides[routes.get_agent_runtime] = lambda: native.runtime
    native.runtime.validate_flow = AsyncMock()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(
            "/internal/v1/agent/campaign-binding",
            headers={
                "X-Adp-Run-Credential": native.credentials["root"] if credential is None else credential,
                "X-Adp-Workload-Token": workload,
            },
        )


async def test_binding_comes_from_verified_execution_grant_and_workload(native):
    response = await request(native)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    document = response.json()
    assert document == native.service.capture(tenant="tenant", invocation="root", human="human")
    assert document["binding"]["execution"]["workload_binding"] == {"S": "pod-root"}
    assert document["binding"]["grant"]["authority_human_id"] == {"S": "human"}
    assert native.credentials["root"] not in response.text


@pytest.mark.parametrize("credential,workload", [("human-jwt-is-not-native", "root"), ("", "root"), (None, "unrelated")])
async def test_human_token_missing_credential_and_wrong_pod_are_rejected(native, credential, workload):
    response = await request(native, credential=credential, workload=workload)
    assert response.status_code == 404
    assert "binding" not in response.json()


async def test_retired_root_and_its_descendants_cannot_obtain_new_bindings(native):
    arguments = retirement(native)
    native.service.retire(**arguments)
    assert (await request(native)).status_code == 404
    assert (await request(native, credential=native.credentials["child"], workload="child")).status_code == 404


async def test_binding_refuses_epoch_change_after_initial_authentication(native, monkeypatch):
    capture = native.service.__class__.capture

    def rotate(service, **kwargs):
        raw = service.store._read("TENANT#tenant", "EXEC#root")
        raw["current_credential_epoch"] = {"N": "2"}
        native.client.put_item(TableName=service.store.table, Item=raw)
        return capture(service, **kwargs)

    monkeypatch.setattr(native.service.__class__, "capture", rotate)
    assert (await request(native)).status_code == 404
