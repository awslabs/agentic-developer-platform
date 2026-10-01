"""Exercise the real HTTP bridge: worker headers cannot select another identity."""

import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, HTTPException

from src.agentauth import knowledge_service as service
from src.agentauth.execution import ExecutionStateError
from src.agentauth.routes import get_agent_runtime, require_agent_transport
from tests.agentauth.test_run_services import GRANT, HEADERS, RECORD


def verify_identity(*args, **kwargs):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "agent-context"))
    from door.auth import verify_identity as verify

    return verify(*args, **kwargs)


SIGNER = Ed25519PrivateKey.generate()
SIGNING_ENV = {
    "AGENT_CONTROL_ENVELOPE_KEY_ID": "test",
    "AGENT_CONTROL_ENVELOPE_SIGNING_KEY": SIGNER.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode(),
}
PUBLIC_KEYS = {"test": SIGNER.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()}


URL = "/internal/v1/agent/self/knowledge/call"
KEY = "test-gateway-only-door-key"


@pytest.fixture
async def bridge(monkeypatch):
    context = (SimpleNamespace(uid="pod-one"), "caller", RECORD, GRANT)
    runtime = SimpleNamespace(
        env={"ADP_DOOR_SERVICE_URL": "https://door.internal", **SIGNING_ENV},
        authenticate=Mock(return_value=context),
        validate_flow=AsyncMock(),
    )
    identity_calls = []

    @asynccontextmanager
    async def identity(record, grant):
        identity_calls.append((record, grant))
        yield {
            "x-github-login": "actual-user",
            "x-owner-sub": "00000000-0000-0000-0000-000000000001",
            "x-tenant-id": "tenant-one",
            "x-adp-run-service": "true",
        }

    monkeypatch.setattr(service, "locked_door_identity", identity)
    upstream_requests = []

    def upstream(request):
        upstream_requests.append(request)
        return httpx.Response(200, json={"status": "ok", "results": ["own-private-data"]}, headers={"Set-Cookie": "secret-cookie"})

    real_client = httpx.AsyncClient
    factory = Mock(side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(upstream), **kwargs))
    monkeypatch.setattr(service, "httpx", SimpleNamespace(AsyncClient=factory, HTTPError=httpx.HTTPError))
    app = FastAPI()
    app.include_router(service.router)
    app.dependency_overrides[get_agent_runtime] = lambda: runtime
    app.dependency_overrides[require_agent_transport] = lambda: None
    async with real_client(transport=httpx.ASGITransport(app=app), base_url="https://gateway.test") as client:
        yield SimpleNamespace(client=client, runtime=runtime, upstream=upstream_requests, factory=factory, identities=identity_calls)


async def test_caller_cannot_forward_auth_or_another_users_identity(bridge):
    hostile = {
        **HEADERS,
        "X-GitHub-Login": "victim",
        "X-GitHub-Teams": "org/admins",
        "X-Owner-Sub": "victim",
        "X-Tenant-Id": "victim",
        "X-Internal-Api-Key": "worker-key",
        "Cookie": "victim-session",
        "Mcp-Session-Id": "victim-session",
        "Host": "attacker.test",
    }
    response = await bridge.client.post(URL, headers=hostile, json={"name": "experience", "arguments": {"action": "recall"}})
    assert response.status_code == 200
    (request,) = bridge.upstream
    assert str(request.url) == "https://door.internal/call"
    claims = verify_identity(request.headers["x-adp-door-identity"], PUBLIC_KEYS, method=request.method, path=request.url.path, body=request.content)
    assert claims["github_login"] == "actual-user"
    assert claims["tenant_id"] == RECORD.tenant_id
    assert claims["sub"] == GRANT.principal
    assert claims["owner_sub"] == "00000000-0000-0000-0000-000000000001"
    for name in ("x-github-login", "x-tenant-id", "x-owner-sub", "x-adp-run-service", "x-internal-api-key"):
        assert name not in request.headers
    for name in ("x-github-teams", "x-adp-run-credential", "x-adp-workload-token", "cookie", "mcp-session-id", "authorization"):
        assert name not in request.headers
    assert bridge.identities == [(RECORD, GRANT)]
    assert bridge.factory.call_args.kwargs == {"timeout": 30, "follow_redirects": False, "trust_env": False}
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert KEY not in response.text


@pytest.mark.parametrize("path", ["credentials", "call?tenant=victim", "mcp/?url=https://evil.test", "tools/call", "mcp/victim", "../secrets"])
async def test_only_fixed_paths_without_query_are_reachable(bridge, path):
    response = await bridge.client.post("/internal/v1/agent/self/knowledge/" + path, headers=HEADERS, json={})
    assert response.status_code == 404
    assert not bridge.upstream


@pytest.mark.parametrize("missing", list(HEADERS))
async def test_both_run_and_pod_proofs_are_required(bridge, missing):
    response = await bridge.client.post(URL, headers={k: v for k, v in HEADERS.items() if k != missing}, json={})
    assert response.status_code == 404
    assert not bridge.upstream
    assert not bridge.identities


@pytest.mark.parametrize("when", ["upload", "identity", "query"])
async def test_revocation_at_slow_boundaries_refuses(bridge, monkeypatch, when):
    if when == "upload":

        async def body():
            yield b'{"name":"experience"}'
            bridge.runtime.authenticate.side_effect = ExecutionStateError("revoked")
    else:

        async def body():
            yield b"{}"

        if when == "identity":

            @asynccontextmanager
            async def identity(*_):
                bridge.runtime.authenticate.side_effect = ExecutionStateError("revoked")
                yield {}

            monkeypatch.setattr(service, "locked_door_identity", identity)
        else:
            original = service.forward_door

            async def forward(*args):
                response = await original(*args)
                bridge.runtime.authenticate.side_effect = ExecutionStateError("revoked")
                return response

            monkeypatch.setattr(service, "forward_door", forward)
    response = await bridge.client.post(URL, headers=HEADERS, content=body())
    assert response.status_code == 404
    assert "own-private-data" not in response.text
    assert len(bridge.upstream) == (1 if when == "query" else 0)


async def test_changed_authority_is_not_reused_after_body(bridge):
    first = bridge.runtime.authenticate.return_value
    changed = (*first[:3], replace(GRANT, revocation_epoch=2))
    bridge.runtime.authenticate.side_effect = [first, first, changed, changed]
    response = await bridge.client.post(URL, headers=HEADERS, json={})
    assert response.status_code == 404
    assert not bridge.upstream


async def test_identity_refusal_does_not_contact_door(bridge, monkeypatch):
    @asynccontextmanager
    async def refused(*_):
        raise HTTPException(404, "not found")
        yield

    monkeypatch.setattr(service, "locked_door_identity", refused)
    response = await bridge.client.post(URL, headers=HEADERS, json={})
    assert response.status_code == 404
    assert not bridge.upstream


async def test_request_and_response_are_bounded(bridge, monkeypatch):
    monkeypatch.setattr(service, "MAX_REQUEST_BYTES", 5)
    response = await bridge.client.post(URL, headers=HEADERS, content=b"123456")
    assert response.status_code == 413
    assert not bridge.upstream
    monkeypatch.setattr(service, "MAX_RESPONSE_BYTES", 5)
    response = await bridge.client.post(URL, headers=HEADERS, json={})
    assert response.status_code == 502
    assert "own-private-data" not in response.text


@pytest.mark.parametrize(
    "url", ["", "https://user:password@door.internal", "https://door.internal/path", "https://door.internal?target=evil", "file:///secret"]
)
async def test_invalid_service_configuration_does_not_forward(bridge, url):
    bridge.runtime.env["ADP_DOOR_SERVICE_URL"] = url
    response = await bridge.client.post(URL, headers=HEADERS, json={})
    assert response.status_code == 503
    assert not bridge.upstream


@pytest.mark.parametrize("status", [302, 401, 500])
async def test_redirects_and_upstream_errors_do_not_leak_auth(bridge, status):
    real_client = httpx.AsyncClient
    bridge.factory.side_effect = lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, text=KEY, headers={"Location": "https://evil.test", "Set-Cookie": KEY})),
        **kwargs,
    )
    response = await bridge.client.post(URL, headers=HEADERS, json={})
    assert response.status_code == 502
    assert KEY not in response.text and "location" not in response.headers


async def test_mcp_notification_and_tools_list_protocol(bridge):
    response = await bridge.client.post(URL.replace("/call", "/mcp/"), headers=HEADERS, json={"jsonrpc": "2.0", "method": "initialize", "id": 1})
    assert response.status_code == 200
    assert bridge.upstream[-1].url.path == "/mcp/"
    assert bridge.upstream[-1].headers["accept"] == "application/json, text/event-stream"
    response = await bridge.client.get(URL.replace("/call", "/tools"), headers=HEADERS)
    assert response.status_code == 200
    assert bridge.upstream[-1].url.path == "/tools"
