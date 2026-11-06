import json
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from tools_facade import server
from tools_facade.server import create_app

HEADERS = {"x-adp-run-credential": "bound-run", "x-adp-workload-token": "bound-pod"}
CALL = {"tool": "websearch___search", "arguments": {"query": "example"},
        "request_id": "00000000-0000-4000-8000-000000000099"}


class FakeGateway:
    def __init__(self, *, allowed=True, human="person-one"):
        self.allowed = allowed
        self.human = human
        self.calls = []

    async def authorize(self, tool, arguments, headers):
        self.calls.append((tool, arguments, headers["x-adp-run-credential"], headers["x-adp-workload-token"]))
        return {
            "schema_version": "1.0", "allowed": self.allowed, "receipt_id": "receipt-from-gateway",
            "run_id": "run-one", "workload_id": "pod-one", "run_expires_at": 4102444800,
            "principal": {
                "user_id": self.human, "tenant_id": "tenant-one", "team_id": "team-one",
                "root_human_id": "person-one",
            },
        }


class FakeAgentCore:
    def __init__(self):
        self.calls = []

    async def invoke(self, tool, arguments, decision):
        self.calls.append((tool, arguments, decision))
        return {"jsonrpc": "2.0", "result": {"content": [{"text": "found"}]}}


def test_valid_run_reaches_agentcore_with_human_attribution():
    gateway, agentcore = FakeGateway(), FakeAgentCore()
    response = TestClient(create_app(gateway=gateway, agentcore=agentcore)).post(
        "/agent/tools/call", json=CALL, headers=HEADERS,
    )
    assert response.status_code == 200
    assert str(UUID(response.json()["execution_id"])) == response.json()["execution_id"]
    assert {key: value for key, value in response.json().items() if key != "execution_id"} == {
        "request_id": CALL["request_id"], "status": "confirmed", "result_status": "success",
        "receipt_id": "receipt-from-gateway", "root_human_id": "person-one",
        "result": {"jsonrpc": "2.0", "result": {"content": [{"text": "found"}]}},
    }
    assert gateway.calls == [("websearch___search", {"query": "example"}, "bound-run", "bound-pod")]
    assert len(agentcore.calls) == 1
    assert agentcore.calls[0][2].principal.root_human_id == "person-one"


@pytest.mark.parametrize("route", ["/tools/websearch___search", "/api/tools/websearch___search"])
def test_rest_tools_routes_keep_workload_authority_and_human_receipts(route):
    gateway, provider = FakeGateway(), FakeAgentCore()
    client = TestClient(create_app(gateway=gateway, agentcore=provider))
    response = client.post(route, json=CALL, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["receipt_id"] == "receipt-from-gateway"
    assert response.json()["root_human_id"] == "person-one"
    assert gateway.calls == [("websearch___search", {"query": "example"}, "bound-run", "bound-pod")]
    assert len(provider.calls) == 1
    assert provider.calls[0][2].principal.root_human_id == "person-one"
    refused = client.post(route, json=CALL)
    assert refused.status_code == 403 and len(provider.calls) == 1


def test_same_human_cannot_resume_with_different_verified_run():
    class SwitchingRun(FakeGateway):
        run_id = "run-one"

        async def authorize(self, tool, arguments, headers):
            decision = await super().authorize(tool, arguments, headers)
            decision["run_id"] = self.run_id
            return decision

    gateway, provider = SwitchingRun(), FakeAgentCore()
    client = TestClient(create_app(gateway=gateway, agentcore=provider))
    initial = client.post("/agent/tools/call", json=CALL, headers=HEADERS)
    assert initial.status_code == 200
    gateway.run_id = "run-two"
    attempt = client.post("/agent/tools/call", json={**CALL, "execution_id": initial.json()["execution_id"]},
                          headers=HEADERS)
    assert attempt.status_code == 404
    assert len(provider.calls) == 1


def test_duplicate_request_recovers_original_receipt_without_second_provider_call():
    gateway, provider = FakeGateway(), FakeAgentCore()
    client = TestClient(create_app(gateway=gateway, agentcore=provider))
    first = client.post("/agent/tools/call", json=CALL, headers=HEADERS)
    again = client.post("/agent/tools/call", json=CALL, headers=HEADERS)
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()
    assert len(provider.calls) == 1
    conflict = client.post("/agent/tools/call", json={**CALL, "arguments": {"query": "different"}}, headers=HEADERS)
    assert conflict.status_code == 409 and len(provider.calls) == 1
    next_call = client.post("/agent/tools/call", json={**CALL,
        "execution_id": first.json()["execution_id"], "request_id": "00000000-0000-4000-8000-000000000098"},
        headers=HEADERS)
    assert next_call.status_code == 200 and len(provider.calls) == 2


def test_provider_timeout_keeps_unknown_receipt_and_restart_never_replays():
    class TimedOutProvider(FakeAgentCore):
        async def invoke(self, tool, arguments, decision):
            self.calls.append((tool, arguments, decision))
            raise httpx.ReadTimeout("outcome unknown")

    gateway, provider = FakeGateway(), TimedOutProvider()
    original = TestClient(create_app(gateway=gateway, agentcore=provider)).post(
        "/agent/tools/call", json=CALL, headers=HEADERS,
    )
    assert original.status_code == 202 and original.json()["status"] == "unknown"
    assert "result" not in original.json()
    restarted = TestClient(create_app(gateway=gateway, agentcore=provider)).post(
        "/agent/tools/call", json=CALL, headers=HEADERS,
    )
    assert restarted.status_code == 202 and restarted.json() == original.json()
    assert len(provider.calls) == 1


def test_provider_json_rpc_error_is_confirmed_as_failure():
    class ErrorProvider(FakeAgentCore):
        async def invoke(self, tool, arguments, decision):
            self.calls.append((tool, arguments, decision))
            return {"jsonrpc": "2.0", "error": {"code": -32000, "message": "provider refused"}}

    provider = ErrorProvider()
    client = TestClient(create_app(gateway=FakeGateway(), agentcore=provider))
    first = client.post("/agent/tools/call", json=CALL, headers=HEADERS)
    again = client.post("/agent/tools/call", json=CALL, headers=HEADERS)
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()
    assert first.json()["status"] == "confirmed" and first.json()["result_status"] == "error"
    assert len(provider.calls) == 1


def test_provider_mutation_requires_stable_uuid_request_id():
    provider = FakeAgentCore()
    response = TestClient(create_app(gateway=FakeGateway(), agentcore=provider)).post(
        "/agent/tools/call", json={"tool": CALL["tool"], "arguments": CALL["arguments"]}, headers=HEADERS,
    )
    assert response.status_code == 422 and provider.calls == []


def test_no_execution_table_refuses_before_provider(monkeypatch):
    monkeypatch.delenv("ADP_TOOLS_EXECUTION_TABLE")
    provider = FakeAgentCore()
    client = TestClient(create_app(gateway=FakeGateway(), agentcore=provider))
    response = client.post("/agent/tools/call", json=CALL, headers=HEADERS)
    assert response.status_code == 503
    assert provider.calls == []


def test_provider_is_never_invoked_without_positive_human_rooted_decision():
    for gateway in (FakeGateway(allowed=False), FakeGateway(human="another-person")):
        agentcore = FakeAgentCore()
        response = TestClient(create_app(gateway=gateway, agentcore=agentcore)).post(
            "/agent/tools/call", json=CALL, headers=HEADERS,
        )
        assert response.status_code == 403
        assert not agentcore.calls


def test_missing_binding_never_calls_gateway_or_provider():
    gateway, agentcore = FakeGateway(), FakeAgentCore()
    response = TestClient(create_app(gateway=gateway, agentcore=agentcore)).post(
        "/agent/tools/call", json=CALL,
    )
    assert response.status_code == 403
    assert gateway.calls == agentcore.calls == []


def test_legacy_tool_route_must_match_authorized_tool():
    gateway, agentcore = FakeGateway(), FakeAgentCore()
    response = TestClient(create_app(gateway=gateway, agentcore=agentcore)).post(
        "/tools/different___search", json=CALL, headers=HEADERS,
    )
    assert response.status_code == 400
    assert gateway.calls == agentcore.calls == []


def test_lease_end_at_internal_plane_refuses_next_call_without_forwarding(monkeypatch):
    monkeypatch.setenv("ADP_TOOLS_INTERNAL_AUTH_URL", "https://api.example.invalid/internal/v1/tools/authorize")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(server, "signed_headers", lambda url, body, service, region, extra_headers: {
        "authorization": "AWS4-HMAC-SHA256 signed", **extra_headers,
    })
    lease_active = True
    gateway_calls = []

    def gateway(request):
        if request.url.path.endswith("/tools/outcome"):
            assert "x-adp-run-credential" not in request.headers
            assert not {"arguments", "result"} & json.loads(request.content).keys()
            return httpx.Response(200, json={"status": "recorded"})
        gateway_calls.append(json.loads(request.content))
        assert request.headers["x-adp-run-credential"] == HEADERS["x-adp-run-credential"]
        if not lease_active:
            return httpx.Response(404, json={"detail": "unknown_run"})
        return httpx.Response(200, json={
            "schema_version": "1.0", "allowed": True, "receipt_id": "receipt-before-lease-end",
            "run_id": "run-one", "workload_id": "pod-one", "run_expires_at": 4102444800,
            "principal": {"user_id": "person-one", "root_human_id": "person-one",
                          "tenant_id": "tenant-one", "team_id": "team-one"},
        })

    client_class = httpx.AsyncClient
    transport = httpx.MockTransport(gateway)
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: client_class(transport=transport, **kwargs))
    provider = FakeAgentCore()
    client = TestClient(create_app(agentcore=provider))
    assert client.post("/agent/tools/call", json=CALL, headers=HEADERS).status_code == 200
    lease_active = False
    reply = client.post("/agent/tools/call", json=CALL, headers=HEADERS)
    assert reply.status_code == 403 and reply.json()["detail"] == "tool authority refused"
    assert len(gateway_calls) == 2
    assert gateway_calls[0] == gateway_calls[1]
    assert len(provider.calls) == 1


@pytest.mark.parametrize("recovery_path", ["/agent/tools/operations/recover", "/api/agent/tools/operations/recover"])
def test_crew_proofs_are_signed_for_authorization_and_receipts_remain_owner_bound(monkeypatch, recovery_path):
    headers = {"x-adp-chat-capability": "crew-capability", "x-adp-chat-run-id": "session-run",
               "x-adp-chat-session-id": "session-one", "x-agent-runid": "crew-run", "x-adp-workload-token": "crew-pod"}
    monkeypatch.setenv("ADP_TOOLS_INTERNAL_AUTH_URL", "https://api.example.invalid/internal/v1/tools/authorize")
    signed_proofs = []
    revoked = False

    def sign(url, body, service, region, extra_headers):
        if url.endswith("/tools/outcome"):
            assert extra_headers == {}
        else:
            signed_proofs.append(extra_headers)
            assert extra_headers == headers
        return {"authorization": "AWS4-HMAC-SHA256 synthetic", **extra_headers}

    monkeypatch.setattr(server, "signed_headers", sign)
    owner = "person-one"

    def authorize(request):
        assert request.headers["authorization"] == "AWS4-HMAC-SHA256 synthetic"
        assert "x-adp-user-authorization" not in request.headers
        if request.url.path.endswith("/tools/outcome"):
            assert not {"arguments", "result"} & json.loads(request.content).keys()
            return httpx.Response(200, json={"status": "recorded"})
        action = json.loads(request.content).get("action", "call")
        if revoked and action == "call":
            return httpx.Response(403, json={"detail": "tool_not_consented"})
        decision = {"schema_version": "1.0", "allowed": True, "receipt_id": "crew-receipt",
                    "run_id": "crew-run", "workload_id": "crew-pod", "run_expires_at": 4102444800,
                    "principal": {"user_id": owner, "root_human_id": owner,
                                  "tenant_id": "tenant-one", "team_id": "team-one"}}
        if json.loads(request.content).get("action") == "list":
            decision["tools"] = [{"name": "websearch___search", "description": "Search",
                                  "inputSchema": {"type": "object"}}]
        return httpx.Response(200, json=decision)

    client_class = httpx.AsyncClient
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: client_class(
        transport=httpx.MockTransport(authorize), **kwargs))
    provider = FakeAgentCore()
    client = TestClient(create_app(agentcore=provider))
    listing = client.get("/agent/tools", headers=headers)
    assert listing.status_code == 200 and listing.json()["tools"][0]["name"] == "websearch___search"
    first = client.post("/agent/tools/call", headers=headers, json=CALL)
    assert first.status_code == 200 and first.json()["root_human_id"] == "person-one"
    execution_id = first.json()["execution_id"]
    path = f"/agent/tools/executions/{execution_id}/operations/{CALL['request_id']}?tool=websearch___search"
    assert client.get(path, headers=headers).status_code == 200
    revoked = True
    assert client.post("/agent/tools/call", headers=headers, json=CALL).status_code == 403
    recovered = client.post(recovery_path, headers=headers, json=CALL)
    assert recovered.status_code == 200 and recovered.json() == first.json()
    owner = "person-two"
    assert client.get(path, headers=headers).status_code == 404
    assert client.post(recovery_path, headers=headers, json=CALL).status_code == 404
    revoked = False
    response = client.post("/agent/tools/call", headers=headers, json={**CALL, "execution_id": execution_id})
    assert response.status_code == 404
    count = len(signed_proofs)
    assert client.get("/agent/tools", headers={**headers, "x-adp-run-credential": "worker"}).status_code == 403
    assert client.get("/agent/tools", headers={**headers, "authorization": "Bearer laptop"}).status_code == 403
    assert len(signed_proofs) == count and len(provider.calls) == 1
