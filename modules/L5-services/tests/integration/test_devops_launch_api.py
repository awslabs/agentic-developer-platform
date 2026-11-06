from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from l5_services.api.router import create_router


@pytest.fixture
def client_and_backend():
    class Backend:
        def __init__(self):
            self.calls = []
            self.checks = []

        async def check_browser_launch(self, product_id, project_id, binding_id, revision, request):
            self.checks.append((product_id, project_id, binding_id, revision))
            return {"ready": True, "binding_id": binding_id}

        async def launch_browser(self, product_id, project_id, binding_id, revision, request):
            self.calls.append((product_id, project_id, binding_id, revision))
            return {"binding_id": binding_id, "launch_url": "https://example.invalid/launch",
                    "expires_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat()}

    backend = Backend()
    app = FastAPI()
    app.include_router(create_router(lambda: backend))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.local"), backend


@pytest.mark.asyncio
async def test_launch_operation_is_scoped_and_browser_only(client_and_backend):
    client, backend = client_and_backend
    async with client:
        response = await client.post(
            "/v1/products/product/projects/project/services/devops/bindings/binding/browser-launch",
            headers={"Authorization": "Bearer offline-test-token"},
            json={"configuration_revision": 2},
        )
    assert response.status_code == 200
    assert response.json()["binding_id"] == "binding"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert backend.calls == [("product", "project", "binding", 2)]
    assert not any(route.path.endswith("/browser-launch") and "bindings/{binding_id}" not in route.path
                   for route in client._transport.app.routes)


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "/check"])
@pytest.mark.parametrize("service,headers,body,status", [
    ("security", {"Authorization": "Bearer test"}, {"configuration_revision": 2}, 404),
    ("devops", {}, {"configuration_revision": 2}, 401),
    ("devops", {"Authorization": "Bearer test"}, {"configuration_revision": 2, "role_arn": "forged"}, 422),
    ("devops", {"Authorization": "Bearer test"}, {"configuration_revision": 2, "launcher_role_arn": "forged"}, 422),
    ("devops", {"Authorization": "Bearer test"}, {"configuration_revision": 2, "operator_role_arn": "forged"}, 422),
    ("devops", {"Authorization": "Bearer test"}, {"configuration_revision": 2, "user_id": "forged"}, 422),
    ("devops", {"Authorization": "Bearer test"}, {"configuration_revision": 2, "redirect": "https://example.invalid/foreign"}, 422),
    ("devops", {"Authorization": "Bearer test"}, {"configuration_revision": 2, "space_id": "foreign"}, 422),
    ("devops", {"Authorization": "Bearer test"}, {"configuration_revision": 0}, 422),
    ("devops", {"Authorization": "Bearer test"}, {"configuration_revision": 2, "padding": "x" * 300}, 413),
])
async def test_denials_do_not_reach_launch_backend(client_and_backend, service, headers, body, status, suffix):
    client, backend = client_and_backend
    async with client:
        response = await client.post(
            f"/v1/products/product/projects/project/services/{service}/bindings/binding/browser-launch{suffix}",
            headers=headers, json=body,
        )
    assert response.status_code == status
    assert backend.calls == [] and backend.checks == []


@pytest.mark.asyncio
async def test_check_reads_eligibility_without_launching(client_and_backend):
    client, backend = client_and_backend
    async with client:
        response = await client.post(
            "/v1/products/product/projects/project/services/devops/bindings/binding/browser-launch/check",
            headers={"Authorization": "Bearer offline-test-token"}, json={"configuration_revision": 2},
        )
    assert response.status_code == 200
    assert response.json() == {"ready": True, "binding_id": "binding"}
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert backend.checks == [("product", "project", "binding", 2)] and backend.calls == []
