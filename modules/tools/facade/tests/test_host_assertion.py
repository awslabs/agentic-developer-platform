"""A Dev Box host assertion is a complete, single identity that the façade forwards unchanged (#8380)."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tools_facade import server
from tools_facade.server import create_app

RUN = "11111111-2222-4333-8444-555555555555"
HOST = {"x-adp-crew-host-assertion": "host-assertion", "x-agent-runid": RUN}
CALL = {
    "tool": "websearch___search",
    "arguments": {"query": "example"},
    "request_id": "00000000-0000-4000-8000-000000000099",
}


def decision(**changes):
    return {
        "schema_version": "1.0",
        "allowed": True,
        "receipt_id": "receipt-host",
        "run_id": RUN,
        "workload_id": "i-0123456789abcdef0",
        "run_expires_at": 4102444800,
        "host_generation": 3,
        "principal": {"user_id": "owner-a", "root_human_id": "owner-a", "tenant_id": "tenant-a", "team_id": "team-a"},
        **changes,
    }


class FakeAgentCore:
    def __init__(self):
        self.calls = []

    async def invoke(self, tool, arguments, decision):
        self.calls.append((tool, arguments, decision))
        return {"jsonrpc": "2.0", "result": {"content": [{"text": "found"}]}}


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setenv("ADP_TOOLS_INTERNAL_AUTH_URL", "https://api.example.invalid/internal/v1/tools/authorize")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(
        server,
        "signed_headers",
        lambda url, body, service, region, extra_headers: {
            "authorization": "AWS4-HMAC-SHA256 signed",
            **extra_headers,
        },
    )
    seen = []

    def respond(request):
        if request.url.path.endswith("/tools/outcome"):
            return httpx.Response(200, json={"status": "recorded"})
        seen.append(request)
        return httpx.Response(200, json=decision())

    client_class = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: client_class(transport=transport, **kwargs))
    return seen


def test_host_assertion_is_forwarded_as_the_only_identity_and_receipts_stay_owner_bound(gateway):
    provider = FakeAgentCore()
    client = TestClient(create_app(agentcore=provider))
    response = client.post("/agent/tools/call", json=CALL, headers=HOST)
    assert response.status_code == 200, response.text
    assert response.json()["receipt_id"] == "receipt-host" and response.json()["root_human_id"] == "owner-a"
    assert len(gateway) == 1
    forwarded = gateway[0]
    assert forwarded.headers["x-adp-crew-host-assertion"] == "host-assertion"
    assert forwarded.headers["x-agent-runid"] == RUN
    for absent in ("x-adp-user-authorization", "x-adp-run-credential", "x-adp-workload-token", "x-adp-chat-capability"):
        assert absent not in forwarded.headers
    assert json.loads(forwarded.content)["tool"] == "websearch___search"
    assert provider.calls[0][2].run_id == RUN and provider.calls[0][2].workload_id == "i-0123456789abcdef0"
    assert provider.calls[0][2].host_generation == 3


@pytest.mark.parametrize(
    "extra",
    [
        {"authorization": "Bearer laptop-token"},
        {
            "x-adp-chat-capability": "crew",
            "x-adp-chat-run-id": "r",
            "x-adp-chat-session-id": "s",
            "x-adp-workload-token": "w",
        },
        {"x-adp-run-credential": "credential"},
        {"x-adp-workload-token": "pod"},
    ],
)
def test_host_assertion_combined_with_any_other_proof_is_ambiguous(gateway, extra):
    provider = FakeAgentCore()
    client = TestClient(create_app(agentcore=provider))
    response = client.post("/agent/tools/call", json=CALL, headers={**HOST, **extra})
    assert response.status_code == 403 and response.json()["detail"] == "ambiguous tool identity"
    assert gateway == [] and provider.calls == []


def test_host_assertion_without_its_run_or_with_a_duplicate_is_refused(gateway):
    provider = FakeAgentCore()
    client = TestClient(create_app(agentcore=provider))
    assert (
        client.post("/agent/tools/call", json=CALL, headers={"x-adp-crew-host-assertion": "host-assertion"}).status_code
        == 403
    )
    duplicate = httpx.Headers(
        [("x-adp-crew-host-assertion", "one"), ("x-adp-crew-host-assertion", "two"), ("x-agent-runid", RUN)]
    )
    assert client.post("/agent/tools/call", json=CALL, headers=duplicate).status_code == 403
    assert gateway == [] and provider.calls == []


def test_host_decision_without_run_binding_is_unavailable_not_allowed(monkeypatch):
    monkeypatch.setenv("ADP_TOOLS_INTERNAL_AUTH_URL", "https://api.example.invalid/internal/v1/tools/authorize")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(server, "signed_headers", lambda url, body, service, region, extra_headers: {**extra_headers})
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=decision(run_id=None, workload_id=None, run_expires_at=None))
    )
    client_class = httpx.AsyncClient
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: client_class(transport=transport, **kwargs))
    provider = FakeAgentCore()
    response = TestClient(create_app(agentcore=provider)).post("/agent/tools/call", json=CALL, headers=HOST)
    assert response.status_code == 503 and provider.calls == []
