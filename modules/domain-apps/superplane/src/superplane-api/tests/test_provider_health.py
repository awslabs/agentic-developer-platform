"""An idle registered workspace receives real health without an execution grant."""

import json
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import skypilot_proxy
from app.config import settings
from app.models.cluster import Cluster
from app.models.workspace import Workspace
from app.services import provider_health
from tests.conftest import async_session_test
from tests.test_controller_management import grant
from tests.test_organization_grants import seed

ORIGIN = "http://skypilot-api.skypilot.svc.cluster.local:46580"
HEALTH_TOKEN = "h" * 48


def configure(monkeypatch, org, workspaces):
    monkeypatch.setattr(settings, "superplane_provider_health_url", ORIGIN)
    monkeypatch.setattr(settings, "superplane_provider_health_token", HEALTH_TOKEN)
    monkeypatch.setattr(settings, "superplane_provider_health_org_id", str(org))
    monkeypatch.setattr(
        settings, "superplane_provider_health_workspace_ids", workspaces
    )


@pytest.mark.parametrize(
    "response,expected",
    [
        (httpx.Response(200, json={"status": "healthy"}), True),
        (httpx.Response(200, json={"status": "unhealthy"}), False),
        (httpx.Response(200, json={}), False),
        (httpx.Response(200, json=None), False),
        (httpx.Response(200, content=b"broken"), False),
        (httpx.Response(200, content=b"x" * 4097), False),
        (httpx.Response(401), False),
        (httpx.Response(503), False),
        (httpx.Response(302, headers={"Location": "https://different.invalid"}), False),
        (httpx.ReadTimeout("fixture timeout"), False),
    ],
)
async def test_actual_authenticated_response_determines_health(
    monkeypatch, response, expected
):
    configure(monkeypatch, uuid.uuid4(), [])
    calls = []

    async def transport(request):
        calls.append(request)
        assert request.method == "GET" and str(request.url) == ORIGIN + "/api/health"
        assert request.headers["Authorization"] == "Bearer " + HEALTH_TOKEN
        if isinstance(response, Exception):
            raise response
        return response

    original = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs == {"timeout": 3, "follow_redirects": False, "trust_env": False}
        return original(transport=httpx.MockTransport(transport), **kwargs)

    monkeypatch.setattr(provider_health.httpx, "AsyncClient", client)
    assert await provider_health.installed_skypilot_health() is expected
    assert len(calls) == 1


async def test_failed_read_replaces_previous_assignment_health_and_scopes_targets(
    monkeypatch,
):
    org, workspace = str(uuid.uuid4()), str(uuid.uuid4())
    configure(monkeypatch, org, [workspace])
    target = {
        "workspace_id": workspace,
        "cluster_id": str(uuid.uuid4()),
        "workspace_status": "active",
        "cluster_status": "Ready",
        "execution_assignments": [{"operation_id": "unchanged"}],
        "provider_observations": [
            {"skypilot_healthy": True, "checked_at": datetime.now(UTC).isoformat()}
        ],
    }
    excluded = [
        {**deepcopy(target), "workspace_id": str(uuid.uuid4())},
        {**deepcopy(target), "provisional": True},
        {**deepcopy(target), "shared_membership": True},
        {**deepcopy(target), "workspace_status": "Teardown"},
    ]
    before = deepcopy(excluded)
    reads = 0

    async def unhealthy():
        nonlocal reads
        reads += 1
        return False

    monkeypatch.setattr(provider_health, "installed_skypilot_health", unhealthy)
    await provider_health.project_provider_health(
        [target, *excluded], org_id=str(uuid.uuid4())
    )
    assert reads == 0
    await provider_health.project_provider_health([target, *excluded], org_id=org)
    assert reads == 1 and excluded == before
    assert target["execution_assignments"] == [{"operation_id": "unchanged"}]
    assert len(target["provider_observations"]) == 1
    observation = target["provider_observations"][0]
    assert observation["skypilot_healthy"] is False
    assert datetime.now(UTC) - datetime.fromisoformat(
        observation["checked_at"]
    ) < timedelta(seconds=1)


async def test_idle_registry_observes_through_health_only_proxy(client, monkeypatch):
    org, _, _ = await seed(monkeypatch)
    workspace_id = uuid.uuid4()
    configure(monkeypatch, org, [str(workspace_id)])
    headers = grant(monkeypatch, org)
    monkeypatch.setenv("SKYPILOT_SERVICE_TOKEN", "s" * 48)
    monkeypatch.setenv("SKYPILOT_HEALTH_TOKEN", HEALTH_TOKEN)
    async with async_session_test() as db:
        cluster = Cluster(id=uuid.uuid4(), org_id=org, name="workspace", status="Ready")
        db.add(cluster)
        await db.flush()
        db.add(
            Workspace(
                id=workspace_id,
                org_id=org,
                name="workspace",
                cluster_id=cluster.id,
                namespace_name="tenant-a",
                status="active",
                isolation_mode="dedicated",
            )
        )
        await db.commit()
    calls = []
    proxy = httpx.ASGITransport(app=skypilot_proxy.app)

    async def transport(request):
        calls.append((str(request.url), request.headers.get("Authorization")))
        if str(request.url) == ORIGIN + "/api/health":
            return await proxy.handle_async_request(request)
        assert str(request.url) == "http://127.0.0.1:46580/api/health"
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"status": "healthy"})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        provider_health.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(transport), **kw),
    )
    response = await client.post(
        "/internal/controller/reconcile",
        headers=headers,
        json={"org_id": str(org), "instance_id": str(uuid.uuid4())},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["governed_provisioning"] is False and body["fence_token"] >= 1
    target = next(t for t in body["targets"] if t["workspace_id"] == str(workspace_id))
    assert target["execution_assignments"] == []
    assert target["provider_observations"][0]["skypilot_healthy"] is True
    assert calls == [
        (ORIGIN + "/api/health", "Bearer " + HEALTH_TOKEN),
        ("http://127.0.0.1:46580/api/health", None),
    ]
    assert HEALTH_TOKEN not in json.dumps(body) and "s" * 48 not in json.dumps(body)


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/launch"),
        ("POST", "/down"),
        ("GET", "/internal/provider-identity"),
        ("POST", "/api/health"),
        ("GET", "/api/health?other=route"),
        ("GET", "/api/status"),
    ],
)
def test_health_key_cannot_reach_execution_or_other_provider_routes(
    monkeypatch, method, path
):
    monkeypatch.setenv("SKYPILOT_SERVICE_TOKEN", "s" * 48)
    monkeypatch.setenv("SKYPILOT_HEALTH_TOKEN", HEALTH_TOKEN)
    with TestClient(skypilot_proxy.app) as proxy:
        response = proxy.request(
            method, path, headers={"Authorization": "Bearer " + HEALTH_TOKEN}
        )
    assert response.status_code == 403


def test_health_credential_cannot_equal_provider_token(monkeypatch):
    monkeypatch.setenv("SKYPILOT_SERVICE_TOKEN", HEALTH_TOKEN)
    monkeypatch.setenv("SKYPILOT_HEALTH_TOKEN", HEALTH_TOKEN)
    with pytest.raises(RuntimeError, match="distinct"):
        skypilot_proxy.health_token()
