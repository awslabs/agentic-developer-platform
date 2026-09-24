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


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "api/v1/research/findings"),
        ("GET", "api/v1/research/stats"),
        ("GET", "api/v1/research/proposals"),
        ("PATCH", "api/v1/research/proposals/11111111-1111-1111-1111-111111111111/approve"),
    ],
)
def test_research_routes_need_a_bearer_at_the_edge_too(client, method, path):
    """No unauthenticated caller reaches the research surface — issue #5682 (A02).

    These are the routes that were the reachability path for the A02 finding:
    the domain API served them to an anonymous caller with every tenant's rows
    merged, and all 12 are in this proxy's public allowlist, so the exposure was
    reachable from the internet rather than cluster-local.

    The API side is fixed (each handler now requires a server-derived tenant),
    and that is the fix that matters — this asserts the edge does not forward an
    anonymous request either, so the two layers agree. Pinned per-route rather
    than trusting the shared bearer check, because the allowlist is generated
    from the domain inventory and a future route could arrive without anyone
    re-reading this proxy.
    """
    assert client.request(method, "/superplane/v1/" + path).status_code == 401


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


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "workspaces/preview"),
        ("POST", "workspaces/adopt"),
        ("GET", "workspaces/ws-1/lifecycle-proposals"),
        ("POST", "workspaces/ws-1/lifecycle-proposals/artifact-1/preview"),
        ("POST", "workspaces/ws-1/lifecycle-proposals/artifact-1/continue"),
        ("GET", "operations/op-1"),
        ("GET", "operations/by-idempotency/request-1"),
        ("POST", "operation-approvals"),
        ("GET", "operation-approvals/approval-1"),
        ("POST", "operation-approvals/approval-1/decision"),
    ],
)
def test_governed_onboarding_routes_forward_exact_body_and_preserve_denial(client, monkeypatch, method, path):
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(403, json={"detail": "operation authority refused"})

    original = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(upstream), **kw))
    body = {"operation_id": "request-1", "approval_id": "approval-1"}
    assert client.request(method, "/superplane/v1/" + path).status_code == 401
    response = client.request(method, "/superplane/v1/" + path, json=body, headers={"Authorization": "Bearer user-token"})
    assert response.status_code == 403
    assert len(calls) == 1
    assert calls[0].url.path == "/" + path
    assert calls[0].method == method
    assert json.loads(calls[0].content) == body


def test_default_has_no_registration_and_no_network(monkeypatch):
    monkeypatch.delenv("BG_ENVIRONMENT", raising=False)
    monkeypatch.setattr(proxy, "_cache", (0, {}))
    monkeypatch.setattr(proxy.boto3, "client", lambda *a, **k: pytest.fail("unexpected AWS call"))
    assert proxy.registration() == {}


@pytest.fixture
def route_store(monkeypatch):
    from io import BytesIO
    from types import SimpleNamespace

    monkeypatch.setenv("BG_ENVIRONMENT", "dev")
    monkeypatch.setenv("BG_SUPERPLANE_ROUTE_BUCKET", "adp-terraform-state-123456789012")
    monkeypatch.setattr(proxy, "_cache", (0, {}))
    store = {"version": 2, "enabled": True, "namespace": "superplane", "release_id": "a" * 64, "installation_id": "b" * 24, "revision": "c" * 32}
    calls = []

    def get_object(**kwargs):
        calls.append(kwargs)
        return {"Body": BytesIO(json.dumps(store).encode())}

    def factory(service, **kwargs):
        assert service == "s3"
        return SimpleNamespace(get_object=get_object)

    monkeypatch.setattr(proxy.boto3, "client", factory)
    return store, calls


def test_registration_reads_only_the_conditional_route_object(route_store):
    store, calls = route_store
    assert proxy.registration() == store
    assert calls == [{"Bucket": "adp-terraform-state-123456789012", "Key": "domain-routes/dev/superplane/public-route.json"}]
    assert proxy.registration() == store
    assert len(calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 1),
        ("enabled", False),
        ("namespace", "adp"),
        ("namespace", "foreign/route"),
        ("installation_id", ""),
        ("release_id", "unknown"),
        ("revision", ""),
    ],
)
def test_invalid_route_registration_is_off(route_store, field, value):
    store, _ = route_store
    store[field] = value
    assert proxy.registration() == {}


def test_route_read_failure_is_off_and_never_falls_back_to_ssm(route_store, monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("private provider error")

    monkeypatch.setattr(proxy.boto3, "client", unavailable)
    assert proxy.registration() == {}


def test_unconfigured_route_store_does_not_use_ambient_aws(monkeypatch):
    monkeypatch.setenv("BG_ENVIRONMENT", "dev")
    monkeypatch.delenv("BG_SUPERPLANE_ROUTE_BUCKET", raising=False)
    monkeypatch.setattr(proxy, "_cache", (0, {}))
    monkeypatch.setattr(proxy.boto3, "client", lambda *a, **kw: pytest.fail("unexpected AWS call"))
    assert proxy.registration() == {}


async def test_transport_capability_requires_deployed_configuration(route_store, monkeypatch):
    result = await proxy.installation_support()
    assert result["version"] == 2 and result["configured"] is True
    assert result["transport"] == "s3-conditional-domain-registration"
    assert result["features"] == ["account-vault-reference-v1"]
    monkeypatch.delenv("BG_SUPERPLANE_ROUTE_BUCKET")
    assert (await proxy.installation_support())["configured"] is False
