import ast
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.domain_proxy import superplane as proxy


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("FEATURE_SUPERPLANE_ENABLED", raising=False)
    monkeypatch.setattr(proxy, "registration", lambda: {"namespace": "superplane", "release_id": "a" * 64})
    app = FastAPI()
    app.include_router(proxy.router)
    with TestClient(app) as value:
        yield value


def test_all_public_routes_come_from_maintained_inventory():
    root = Path(__file__).resolve().parents[4]
    inventory = root / "modules/domain-apps/superplane/src/superplane-api/app/endpoint_inventory.py"
    tree = ast.parse(inventory.read_text())
    value = next(n.value for n in tree.body if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", None) == "DOMAIN_ROUTES")
    expected = {ast.literal_eval(k) for k in value.keys} - {("POST", "/auth/token")}
    actual = {tuple(x) for x in json.loads(Path(proxy.__file__).with_name("superplane_routes.json").read_text())}
    assert actual == expected


@pytest.mark.parametrize(
    "path",
    [
        "internal/observations/clusters",
        "internal/provider-operations",
        "health",
        "docs",
        "openapi.json",
        "auth/login",
        "auth/token",
        "workspaces/%252e%252e/internal",
        "workspaces/a%2Fb",
        "workspaces/a\\b",
    ],
)
def test_private_and_ambiguous_paths_never_forward(client, path):
    assert client.get("/superplane/v1/" + path, headers={"Authorization": "Bearer test"}).status_code == 404


def test_missing_bearer_and_feature_off(client, monkeypatch):
    assert client.get("/superplane/v1/workspaces").status_code == 401
    monkeypatch.setenv("FEATURE_SUPERPLANE_ENABLED", "false")
    assert client.get("/superplane/v1/workspaces", headers={"Authorization": "Bearer test"}).status_code == 404


def test_forward_only_trusted_transport_headers_and_preserve_denial(client, monkeypatch):
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(403, json={"detail": "workspace access denied"}, headers={"set-cookie": "secret=bad", "x-org-id": "bad"})

    original = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(upstream), **kw))
    response = client.get(
        "/superplane/v1/workspaces/example?view=brief",
        headers={"Authorization": "Bearer user-token", "X-Org-Id": "spoofed", "X-Workspace-Id": "spoofed", "Cookie": "other=secret"},
    )
    assert response.status_code == 403
    assert len(calls) == 1
    assert str(calls[0].url) == "http://superplane-api.superplane.svc.cluster.local:8000/workspaces/example?view=brief"
    assert calls[0].headers["authorization"] == "Bearer user-token"
    assert "x-org-id" not in calls[0].headers and "cookie" not in calls[0].headers
    assert "set-cookie" not in response.headers and "x-org-id" not in response.headers


def test_default_has_no_registration_and_no_network(monkeypatch):
    monkeypatch.delenv("BG_ENVIRONMENT", raising=False)
    monkeypatch.setattr(proxy, "_cache", (0, {}))
    monkeypatch.setattr(proxy.boto3, "client", lambda *a, **k: pytest.fail("unexpected AWS call"))
    assert proxy.registration() == {}
