import asyncio
import base64
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from botocore.credentials import Credentials
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tools_facade import server
from tools_facade.encoding import canonical
from tools_facade.executions import ExecutionStore
from tools_facade.server import GatewayAuthorizer, ToolDescription, client_tool_description, create_app

TOOL = {"name": "websearch___search", "description": "Search the web", "inputSchema": {"type": "object"}}
CALL = {"tool": TOOL["name"], "arguments": {"query": "example"},
        "request_id": "00000000-0000-4000-8000-000000000099"}
BEARER = {"Authorization": "Bearer laptop-access-token"}


class FakeGateway:
    def __init__(self, *, allowed=True):
        self.allowed = allowed
        self.calls = []

    def decision(self, headers):
        self.calls.append(headers.get("authorization"))
        if headers.get("authorization") != "Bearer laptop-access-token":
            raise HTTPException(403, "different tenant or invalid token")
        return {
            "schema_version": "1.0", "allowed": self.allowed, "receipt_id": "gateway-receipt",
            "principal": {
                "user_id": "person-one", "root_human_id": "person-one",
                "tenant_id": "tenant-one", "team_id": "team-one",
            },
        }

    async def authorize(self, tool, arguments, headers):
        assert tool == TOOL["name"] and arguments == CALL["arguments"]
        return self.decision(headers)

    async def list_tools(self, headers):
        return {**self.decision(headers), "tools": [TOOL]}


class FakeAgentCore:
    def __init__(self):
        self.calls = []

    async def invoke(self, tool, arguments, decision):
        self.calls.append((tool, arguments, decision))
        return {"jsonrpc": "2.0", "result": {"content": [{"text": "found"}]}}


@pytest.mark.parametrize("path", ["/agent/tools", "/api/agent/tools"])
def test_laptop_lists_only_gateway_authorized_tools(path):
    gateway, provider = FakeGateway(), FakeAgentCore()
    response = TestClient(create_app(gateway=gateway, agentcore=provider)).get(path, headers=BEARER)
    assert response.status_code == 200
    assert response.json() == {"tools": [client_tool_description(ToolDescription.model_validate(TOOL))],
                               "receipt_id": "gateway-receipt", "root_human_id": "person-one"}
    assert gateway.calls == ["Bearer laptop-access-token"]
    assert provider.calls == []


def test_discovery_hides_provider_authority_and_requires_owned_state_reference():
    class SchemaGateway(FakeGateway):
        async def list_tools(self, headers):
            listing = await super().list_tools(headers)
            provider_schema = {"type": "object", "properties": {"execution_id": {"type": "string"},
                "request_id": {"type": "string"}, "adp_assertion": {"type": "string"},
                "query": {"type": "string"}}, "required": ["execution_id", "request_id", "query"]}
            listing["tools"] = [{**TOOL, "inputSchema": provider_schema},
                {"name": "code_interpreter___start", "description": "Start", "inputSchema": provider_schema},
                {"name": "code_interpreter___execute", "description": "Execute", "inputSchema": provider_schema},
                {"name": "browser___start", "description": "Start browser", "inputSchema": provider_schema},
                {"name": "browser___step", "description": "Step", "inputSchema": provider_schema}]
            return listing

    gateway, provider = SchemaGateway(), FakeAgentCore()
    response = TestClient(create_app(gateway=gateway, agentcore=provider)).get("/agent/tools", headers=BEARER)
    assert response.status_code == 200
    schemas = {tool["name"]: tool["inputSchema"] for tool in response.json()["tools"]}
    assert schemas["websearch___search"]["required"] == ["query"]
    assert schemas["code_interpreter___start"]["required"] == []
    assert schemas["browser___start"]["required"] == ["url"]
    for name in ("code_interpreter___execute", "browser___step"):
        assert "execution_ref" in schemas[name]["required"]
    for schema in schemas.values():
        assert schema["x-adp-schema-version"] == "1.0"
        assert schema["additionalProperties"] is False
        assert not {"execution_id", "request_id", "adp_assertion"} & set(schema["properties"])
    assert provider.calls == []


def test_search_schema_is_query_only_and_forwards_it():
    class SearchGateway(FakeGateway):
        async def authorize(self, tool, arguments, headers):
            assert tool == TOOL["name"] and arguments == options
            return self.decision(headers)

    class SearchProvider(server.AgentCoreClient):
        def __init__(self):
            self.calls = []

        async def invoke(self, tool, arguments, decision):
            self.calls.append((tool, arguments, decision.receipt_id))
            result = {"status": "completed", "results": [{"url": "https://example.com", "text": "sample"}],
                      "citations": ["https://example.com"], "query_count": 1, "estimated_search_usd": 0.007}
            content = canonical(result)
            return {"result": {"result": {"status": "confirmed",
                "execution_id": arguments["execution_id"], "request_id": arguments["request_id"],
                "receipt_id": decision.receipt_id, "result": result, "artifact": {
                    "artifact_id": arguments["request_id"], "content_type": "application/json",
                    "byte_length": len(content), "content_sha256": hashlib.sha256(content).hexdigest()}}}}

    options = {"query": "example"}
    provider = SearchProvider()
    client = TestClient(create_app(gateway=SearchGateway(), agentcore=provider))
    listing = client.get("/agent/tools", headers=BEARER)
    schema = listing.json()["tools"][0]["inputSchema"]
    assert listing.status_code == 200
    assert schema["required"] == ["query"]
    assert set(schema["properties"]) == {"query"}
    assert schema["properties"]["query"]["maxLength"] == 200
    assert schema["additionalProperties"] is False
    response = client.post("/agent/tools/call", json={**CALL, "arguments": options}, headers=BEARER)
    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"
    assert response.json()["receipt_id"] == "gateway-receipt"
    assert provider.calls == [(TOOL["name"], {**options, "execution_id": response.json()["execution_id"],
                               "request_id": CALL["request_id"]}, "gateway-receipt")]


def test_facade_invalid_search_rejects_before_provider_dispatch():
    class SearchGateway(FakeGateway):
        async def authorize(self, tool, arguments, headers):
            return self.decision(headers)

    class SearchProvider(server.AgentCoreClient):
        def __init__(self):
            self.calls = []

        async def invoke(self, tool, arguments, decision):
            self.calls.append(arguments)

    gateway, provider = SearchGateway(), SearchProvider()
    response = TestClient(create_app(gateway=gateway, agentcore=provider)).post("/agent/tools/call",
        json={**CALL, "arguments": {"query": "example", "execution_id": CALL["request_id"]}}, headers=BEARER)
    assert response.status_code == 422
    assert provider.calls == []


@pytest.mark.parametrize("path", [
    "/agent/tools/call", "/api/agent/tools/call", "/tools/websearch___search", "/api/tools/websearch___search",
])
def test_laptop_call_forwards_only_positive_gateway_decision(path):
    gateway, provider = FakeGateway(), FakeAgentCore()
    response = TestClient(create_app(gateway=gateway, agentcore=provider)).post(path, json=CALL, headers=BEARER)
    assert response.status_code == 200
    assert response.json()["receipt_id"] == "gateway-receipt"
    assert response.json()["root_human_id"] == "person-one"
    assert gateway.calls == ["Bearer laptop-access-token"]
    assert len(provider.calls) == 1
    assert provider.calls[0][2].principal.tenant_id == "tenant-one"


def test_refreshed_owner_resumes_but_foreign_identity_cannot_reuse_execution():
    class RotatingGateway(FakeGateway):
        async def authorize(self, tool, arguments, headers):
            token = headers.get("authorization")
            if token not in {"Bearer laptop-access-token", "Bearer refreshed-token", "Bearer foreign-token"}:
                raise HTTPException(403, "invalid token")
            decision = self.decision({"authorization": "Bearer laptop-access-token"})
            if token == "Bearer foreign-token":
                decision["principal"]["user_id"] = "person-two"
                decision["principal"]["root_human_id"] = "person-two"
            return decision

    gateway, provider = RotatingGateway(), FakeAgentCore()
    client = TestClient(create_app(gateway=gateway, agentcore=provider))
    first = client.post("/agent/tools/call", json=CALL, headers=BEARER)
    assert first.status_code == 200
    resumed = {**CALL, "execution_id": first.json()["execution_id"]}
    foreign = client.post("/agent/tools/call", json=resumed, headers={"Authorization": "Bearer foreign-token"})
    assert foreign.status_code == 404
    assert len(provider.calls) == 1
    refreshed = client.post("/agent/tools/call", json={**resumed,
        "request_id": "00000000-0000-4000-8000-000000000098"},
        headers={"Authorization": "Bearer refreshed-token"})
    assert refreshed.status_code == 200 and refreshed.json()["execution_id"] == resumed["execution_id"]
    assert len(provider.calls) == 2


def test_revoked_tool_permission_cannot_resume_execution():
    gateway, provider = FakeGateway(), FakeAgentCore()
    client = TestClient(create_app(gateway=gateway, agentcore=provider))
    initial = client.post("/agent/tools/call", json=CALL, headers=BEARER)
    assert initial.status_code == 200
    gateway.allowed = False
    resumed = client.post("/agent/tools/call", json={**CALL, "execution_id": initial.json()["execution_id"]},
                          headers=BEARER)
    assert resumed.status_code == 403
    assert len(provider.calls) == 1


@pytest.mark.parametrize("method,path", [("get", "/agent/tools"), ("post", "/agent/tools/call")])
@pytest.mark.parametrize("authorization", ["Bearer token-for-other-tenant", "Bearer revoked-token"])
def test_other_tenant_or_revoked_token_never_reaches_provider(method, path, authorization):
    gateway, provider = FakeGateway(), FakeAgentCore()
    response = getattr(TestClient(create_app(gateway=gateway, agentcore=provider)), method)(
        path, headers={"Authorization": authorization}, **({"json": CALL} if method == "post" else {}),
    )
    assert response.status_code == 403
    assert gateway.calls == [authorization]
    assert provider.calls == []


@pytest.mark.parametrize("method,path", [("get", "/agent/tools"), ("post", "/agent/tools/call")])
def test_gateway_denial_never_lists_or_invokes(method, path):
    gateway, provider = FakeGateway(allowed=False), FakeAgentCore()
    response = getattr(TestClient(create_app(gateway=gateway, agentcore=provider)), method)(
        path, headers=BEARER, **({"json": CALL} if method == "post" else {}),
    )
    assert response.status_code == 403
    assert provider.calls == []


@pytest.mark.parametrize("authorization", ["", "Basic other", "Bearer ", "Bearer one two"])
def test_malformed_bearer_refused_before_gateway(authorization):
    gateway, provider = FakeGateway(), FakeAgentCore()
    response = TestClient(create_app(gateway=gateway, agentcore=provider)).post(
        "/agent/tools/call", json=CALL, headers={"Authorization": authorization},
    )
    assert response.status_code == 403
    assert gateway.calls == provider.calls == []


def test_bearer_and_run_proofs_must_not_be_combined():
    gateway, provider = FakeGateway(), FakeAgentCore()
    response = TestClient(create_app(gateway=gateway, agentcore=provider)).post(
        "/agent/tools/call", json=CALL,
        headers={**BEARER, "x-adp-run-credential": "run", "x-adp-workload-token": "pod"},
    )
    assert response.status_code == 403
    assert gateway.calls == provider.calls == []


def test_duplicate_bearer_headers_must_not_select_a_user():
    gateway, provider = FakeGateway(), FakeAgentCore()
    response = TestClient(create_app(gateway=gateway, agentcore=provider)).post(
        "/agent/tools/call", json=CALL,
        headers=[("Authorization", "Bearer laptop-access-token"), ("Authorization", "Bearer other-token")],
    )
    assert response.status_code == 403
    assert gateway.calls == provider.calls == []


def test_listing_is_unavailable_until_gateway_implements_catalog_authorization():
    response = TestClient(create_app()).get("/agent/tools", headers=BEARER)
    assert response.status_code == 503


def internal_plane(monkeypatch, *, status=200, decision=None, response_headers=None):
    calls = []
    monkeypatch.setenv("ADP_TOOLS_INTERNAL_AUTH_URL", "https://api.example.invalid/internal/v1/tools/authorize")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    def sign(url, body, service, region, extra_headers=None):
        assert service == "execute-api" and region == "us-east-1"
        assert url.endswith(("/internal/v1/tools/authorize", "/internal/v1/tools/outcome",
                             "/internal/v1/tools/l5-list", "/internal/v1/tools/l5-call"))
        assert "authorization" not in (extra_headers or {})
        return {"authorization": "AWS4-HMAC-SHA256 signed", **(extra_headers or {})}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, content, headers):
            if url.endswith("/tools/outcome"):
                assert "x-adp-user-authorization" not in headers
                assert not {"arguments", "result", "payload"} & json.loads(content).keys()
                return httpx.Response(200, json={"status": "recorded"}, request=httpx.Request("POST", url))
            calls.append((json.loads(content), headers))
            return httpx.Response(
                status, json=decision or {}, headers=response_headers, request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(server, "signed_headers", sign)
    monkeypatch.setattr(server.httpx, "AsyncClient", Client)
    return calls


def test_authorizer_sends_bearer_separately_from_sigv4_without_proof_spoofing(monkeypatch):
    calls = internal_plane(monkeypatch, decision={"schema_version": "1.0", "allowed": True})
    asyncio.run(GatewayAuthorizer().list_tools({
        "authorization": "Bearer laptop-access-token", "x-adp-user-authorization": "Bearer forged-token",
    }))
    assert calls == [
        ({"schema_version": "1.0", "action": "list"}, {
            "authorization": "AWS4-HMAC-SHA256 signed", "x-adp-user-authorization": "Bearer laptop-access-token",
        }),
    ]


def test_authorizer_preserves_public_operation_payload_and_native_proofs(monkeypatch):
    calls = internal_plane(monkeypatch, decision={"schema_version": "1.0", "allowed": True})
    call = server.ToolCall.model_validate({**CALL, "ttl_seconds": 300})
    headers = {"authorization": "Bearer laptop-access-token",
               "x-adp-native-test-capability": "opaque-native-capability",
               "x-adp-native-test-operation": CALL["request_id"]}
    asyncio.run(GatewayAuthorizer().authorize_call(call, headers))
    assert calls[0][0] == {"schema_version": "1.0", **call.model_dump(mode="json")}
    assert calls[0][1]["x-adp-native-test-capability"] == headers["x-adp-native-test-capability"]
    assert calls[0][1]["x-adp-native-test-operation"] == CALL["request_id"]


@pytest.mark.parametrize("name", ["x-adp-native-test-capability", "x-adp-native-test-operation"])
def test_authorizer_refuses_duplicate_native_headers_before_http(monkeypatch, name):
    from starlette.datastructures import Headers
    calls = internal_plane(monkeypatch)
    headers = Headers(raw=[(name.encode(), b"one"), (name.encode(), b"two")])
    with pytest.raises(HTTPException) as refused:
        asyncio.run(GatewayAuthorizer().list_tools(headers))
    assert refused.value.status_code == 403
    assert calls == []


@pytest.mark.parametrize("action", ["recover", "close"])
def test_ownership_actions_use_internal_authorization_plane_without_forwarding_mutation(monkeypatch, action):
    decision = FakeGateway().decision({"authorization": "Bearer laptop-access-token"})
    calls = internal_plane(monkeypatch, decision=decision)
    asyncio.run(getattr(GatewayAuthorizer(), action)(TOOL["name"], {
        "authorization": "Bearer laptop-access-token", "x-adp-user-authorization": "Bearer forged-token",
    }))
    assert calls == [
        ({"schema_version": "1.0", "action": action, "tool": TOOL["name"]}, {
            "authorization": "AWS4-HMAC-SHA256 signed", "x-adp-user-authorization": "Bearer laptop-access-token",
        }),
    ]


def test_sigv4_covers_internal_bearer_header(monkeypatch):
    credentials = Credentials("synthetic-access", "synthetic-secret")
    monkeypatch.setattr(server.boto3, "Session", lambda: SimpleNamespace(get_credentials=lambda: credentials))
    signed = server.signed_headers(
        "https://api.example.invalid/internal/v1/tools/authorize", b"{}", "execute-api", "us-east-1",
        {"x-adp-user-authorization": "Bearer synthetic-token"},
    )
    assert "x-adp-user-authorization" in signed["Authorization"]
    assert signed["x-adp-user-authorization"] == "Bearer synthetic-token"


def test_real_facade_authorizer_calls_versioned_plane_before_provider(monkeypatch):
    decision = FakeGateway().decision({"authorization": "Bearer laptop-access-token"})
    calls = internal_plane(monkeypatch, decision=decision)
    provider = FakeAgentCore()
    response = TestClient(create_app(agentcore=provider)).post("/agent/tools/call", json=CALL, headers=BEARER)
    assert response.status_code == 200 and response.json()["receipt_id"] == "gateway-receipt"
    assert calls[0][0] == {"schema_version": "1.0", **CALL, "execution_id": None, "ttl_seconds": None}
    assert calls[0][1]["x-adp-user-authorization"] == "Bearer laptop-access-token"
    assert len(provider.calls) == 1


@pytest.mark.parametrize("unknown", [False, True])
def test_facade_reports_original_result_and_cleanup_without_payload_or_second_dispatch(monkeypatch, unknown):
    decision = FakeGateway().decision({"authorization": BEARER["Authorization"]})
    decision["receipt_id"] = "00000000-0000-4000-8000-000000000088"
    outcomes = []

    async def authorize(self, body, headers, *, closing=False):
        if headers.get("authorization") != BEARER["Authorization"]:
            raise HTTPException(403, "tool authority refused")
        return decision

    async def recover(self, tool, headers, **kwargs):
        if headers.get("authorization") != BEARER["Authorization"]:
            raise HTTPException(403, "tool authority refused")
        return decision

    async def close_authority(self, tool, headers, execution_id=None):
        if headers.get("authorization") != BEARER["Authorization"]:
            raise HTTPException(403, "tool authority refused")
        return decision

    async def report(self, payload):
        outcomes.append(payload)
        return {"status": "recorded"}

    class Provider(FakeAgentCore):
        async def invoke(self, tool, arguments, authority):
            if unknown:
                self.calls.append((tool, arguments, authority))
                raise RuntimeError("provider response lost")
            return await super().invoke(tool, arguments, authority)

    monkeypatch.setattr(GatewayAuthorizer, "authorize_call", authorize)
    monkeypatch.setattr(GatewayAuthorizer, "recover", recover)
    monkeypatch.setattr(GatewayAuthorizer, "close", close_authority)
    monkeypatch.setattr(GatewayAuthorizer, "record_outcome", report)
    provider = Provider()
    client = TestClient(create_app(agentcore=provider))
    first = client.post("/agent/tools/call", json=CALL, headers=BEARER)
    assert first.status_code == (202 if unknown else 200)
    assert outcomes[0]["result_status"] == ("unknown" if unknown else "confirmed")
    assert outcomes[0]["request_id"] == CALL["request_id"]
    assert outcomes[0]["execution_id"] == first.json()["execution_id"]
    assert outcomes[0]["receipt_id"] == decision["receipt_id"]
    assert outcomes[0]["tenant_id"] == "tenant-one" and outcomes[0]["user_id"] == "person-one"
    assert "example" not in json.dumps(outcomes)
    close = client.post(f'/agent/tools/executions/{first.json()["execution_id"]}/close',
        params={"tool": CALL["tool"]}, headers=BEARER)
    assert close.status_code == (202 if unknown else 200)
    assert outcomes[-1]["cleanup_status"] == ("unknown" if unknown else "confirmed")
    assert len(provider.calls) == 1
    foreign = client.post("/agent/tools/call", json=CALL, headers={"Authorization": "Bearer foreign"})
    assert foreign.status_code == 403 and len(provider.calls) == 1


def test_lost_outcome_report_keeps_owned_result_and_retries_without_provider_replay(monkeypatch):
    decision = FakeGateway().decision({"authorization": BEARER["Authorization"]})
    decision["receipt_id"] = "00000000-0000-4000-8000-000000000088"
    delivered = []
    available = False

    async def authorize(self, body, headers, *, closing=False):
        return decision

    async def recover(self, tool, headers, **kwargs):
        if headers.get("authorization") != BEARER["Authorization"]:
            raise HTTPException(403, "tool authority refused")
        return decision

    async def report(self, payload):
        if not available:
            raise httpx.ConnectError("report transport unavailable")
        delivered.append(payload)
        return {"status": "recorded"}

    monkeypatch.setattr(GatewayAuthorizer, "authorize_call", authorize)
    monkeypatch.setattr(GatewayAuthorizer, "recover", recover)
    monkeypatch.setattr(GatewayAuthorizer, "record_outcome", report)
    provider = FakeAgentCore()
    client = TestClient(create_app(agentcore=provider))
    first = client.post("/agent/tools/call", json=CALL, headers=BEARER)
    assert first.status_code == 200 and delivered == [] and len(provider.calls) == 1
    available = True
    path = f'/agent/tools/executions/{first.json()["execution_id"]}/operations/{CALL["request_id"]}'
    recovered = TestClient(create_app(agentcore=provider)).get(path, params={"tool": CALL["tool"]}, headers=BEARER)
    assert recovered.status_code == 200 and recovered.json() == first.json()
    assert len(delivered) == 1 and delivered[0]["result_status"] == "confirmed"
    assert len(provider.calls) == 1


def test_provider_error_is_reported_as_result_failure_not_settled_cost(monkeypatch):
    decision = FakeGateway().decision({"authorization": BEARER["Authorization"]})
    decision["receipt_id"] = "00000000-0000-4000-8000-000000000088"
    outcomes = []

    async def authorize(self, body, headers, *, closing=False):
        return decision

    async def report(self, payload):
        outcomes.append(payload)
        return {"status": "recorded"}

    class ErrorProvider(FakeAgentCore):
        async def invoke(self, tool, arguments, authority):
            self.calls.append((tool, arguments, authority))
            return {"jsonrpc": "2.0", "error": {"code": -32000, "message": "provider failed"}}

    monkeypatch.setattr(GatewayAuthorizer, "authorize_call", authorize)
    monkeypatch.setattr(GatewayAuthorizer, "record_outcome", report)
    provider = ErrorProvider()
    response = TestClient(create_app(agentcore=provider)).post("/agent/tools/call", json=CALL, headers=BEARER)
    assert response.status_code == 200 and response.json()["result_status"] == "error"
    assert len(outcomes) == 1 and outcomes[0]["result_status"] == "failed"
    assert not {"settled_cost_usd", "result", "arguments"} & outcomes[0].keys()
    assert len(provider.calls) == 1


@pytest.mark.parametrize("reason", ["tool_rate_limit_exceeded", "tool_budget_exceeded"])
@pytest.mark.parametrize(
    "tool", [
        "websearch___search",
        "browser___start",
        "code_interpreter___start",
        "github___get_user",
        "jira___get_issue",
        "slack___get_user",
    ],
)
def test_gateway_tool_limit_is_returned_before_provider_dispatch(monkeypatch, reason, tool):
    calls = internal_plane(monkeypatch, status=429, decision={"detail": reason}, response_headers={"Retry-After": "12"})
    provider = FakeAgentCore()
    response = TestClient(create_app(agentcore=provider)).post(
        "/agent/tools/call", json={**CALL, "tool": tool}, headers=BEARER,
    )
    assert response.status_code == 429 and response.json() == {"detail": reason}
    assert response.headers["retry-after"] == "12"
    assert len(calls) == 1 and provider.calls == []


def test_gateway_same_id_replay_returns_pending_without_provider_dispatch(monkeypatch):
    receipt_id = "00000000-0000-4000-8000-000000000088"
    pending = {"schema_version": "1.0", "tool_status": {
        "status": "pending", "replay": True, "receipt_id": receipt_id,
        "reservation_status": "unknown", "recovery_path": "/agent/tools/operations/recover",
    }}
    calls = internal_plane(
        monkeypatch, status=202, decision=pending,
    )
    provider = FakeAgentCore()
    response = TestClient(create_app(agentcore=provider)).post("/agent/tools/call", json=CALL, headers=BEARER)
    assert response.status_code == 202
    assert response.json() == pending
    assert len(calls) == 1 and provider.calls == []


@pytest.mark.parametrize("unknown", [False, True])
def test_replay_recovers_original_without_new_dispatch_after_cap_exhaustion(monkeypatch, unknown):
    decision = FakeGateway().decision({"authorization": BEARER["Authorization"]})
    decision["receipt_id"] = "00000000-0000-4000-8000-000000000088"
    admissions = []
    recoveries = []

    async def authorize(self, body, headers, *, closing=False):
        if headers.get("authorization") != BEARER["Authorization"]:
            raise HTTPException(403, "tool authority refused")
        admissions.append(body.request_id)
        if len(admissions) == 1:
            return decision
        return {"schema_version": "1.0", "tool_status": {
            "status": "pending", "replay": True, "receipt_id": decision["receipt_id"],
            "reservation_status": "estimated_hold", "recovery_path": "/agent/tools/operations/recover",
        }}

    async def recover(self, tool, headers, **kwargs):
        recoveries.append((tool, kwargs["call"].request_id))
        return decision

    class Provider(FakeAgentCore):
        async def invoke(self, tool, arguments, authority):
            if unknown:
                self.calls.append((tool, arguments, authority))
                raise RuntimeError("provider reply lost")
            return await super().invoke(tool, arguments, authority)

    monkeypatch.setattr(GatewayAuthorizer, "authorize_call", authorize)
    monkeypatch.setattr(GatewayAuthorizer, "recover", recover)
    provider = Provider()
    client = TestClient(create_app(agentcore=provider))
    first = client.post("/agent/tools/call", json=CALL, headers=BEARER)
    assert first.status_code == (202 if unknown else 200)
    assert first.json()["receipt_id"] == decision["receipt_id"]
    repeated = TestClient(create_app(agentcore=provider)).post("/agent/tools/call", json=CALL, headers=BEARER)
    assert repeated.status_code == first.status_code and repeated.json() == first.json()
    assert len(admissions) == 2 and recoveries == [(CALL["tool"], server.UUID(CALL["request_id"]))]
    assert len(provider.calls) == 1
    foreign = client.post("/agent/tools/call", json=CALL, headers={"Authorization": "Bearer foreign"})
    assert foreign.status_code == 403 and decision["receipt_id"] not in foreign.text
    assert len(provider.calls) == 1


def test_gateway_limit_cannot_inject_retry_header_or_detail(monkeypatch):
    internal_plane(
        monkeypatch, status=429, decision={"detail": "unexpected upstream text"},
        response_headers={"Retry-After": "invalid"},
    )
    provider = FakeAgentCore()
    response = TestClient(create_app(agentcore=provider)).post("/agent/tools/call", json=CALL, headers=BEARER)
    assert response.status_code == 429 and response.json() == {"detail": "tool_limit_exceeded"}
    assert response.headers["retry-after"] == "1" and provider.calls == []


def test_real_facade_authorizer_refuses_gateway_cross_tenant_decision(monkeypatch):
    calls = internal_plane(monkeypatch, status=403)
    provider = FakeAgentCore()
    response = TestClient(create_app(agentcore=provider)).post("/agent/tools/call", json=CALL, headers=BEARER)
    assert response.status_code == 403
    assert len(calls) == 1 and provider.calls == []


def test_real_facade_surfaces_ungranted_vendor_scope_before_provider(monkeypatch):
    calls = internal_plane(monkeypatch, status=403, decision={"detail": "scope_not_granted"})
    provider = FakeAgentCore()
    response = TestClient(create_app(agentcore=provider)).post(
        "/agent/tools/call", json={**CALL, "tool": "slack___post_message", "arguments": {
            "channel": "C123", "text": "hello",
        }}, headers=BEARER,
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "scope_not_granted"}
    assert len(calls) == 1
    assert provider.calls == []


def test_interpreter_public_name_maps_to_gateway_name_in_signed_request(monkeypatch):
    monkeypatch.setenv("ADP_TOOLS_AGENTCORE_URL", "https://tools.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp")
    monkeypatch.setenv("ADP_TOOLS_FACADE_ROLE_ARN", "arn:aws:iam::123456789012:role/example-facade")
    monkeypatch.setenv("ADP_TOOLS_ASSERTION_KEY_ARN", "arn:aws:kms:us-east-1:123456789012:key/example")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(server.boto3, "client", lambda service, region_name, config: SimpleNamespace(
        sign=lambda **kwargs: {"Signature": b"synthetic-signature"},
    ))
    monkeypatch.setattr(server, "signed_headers", lambda *args: {})
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, content, headers):
            packet = json.loads(content)
            claims = json.loads(base64.urlsafe_b64decode(headers["x-adp-assertion"] + "=="))["claims"]
            calls.append((packet["params"]["name"], claims["tool"], claims["receipt_id"]))
            result = {"content": [{"type": "text", "text": "{}"}]}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": packet["id"], "result": result},
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr(server.httpx, "AsyncClient", Client)
    decision = server.Decision.model_validate(
        FakeGateway().decision({"authorization": BEARER["Authorization"]})
    )
    result = asyncio.run(server.AgentCoreClient().invoke("code_interpreter___execute", {"code": "print(42)"}, decision))
    assert result["result"] == {}
    assert calls == [("code-interpreter___execute", "code-interpreter___execute", "gateway-receipt")]
    assert server.gateway_tool_name("websearch___search") == "websearch___search"


@pytest.mark.parametrize("tool,arguments", [
    ("knowledge___search", {"query": "fixture"}),
    ("knowledge___remember", {"session_id": "fixture-session", "messages": [{"role": "user", "content": "fixture"}]}),
    ("jira___get_issue", {"cloudId": "00000000-0000-4000-8000-000000000001", "issueIdOrKey": "EXAMPLE-1"}),
])
@pytest.mark.parametrize("host_generation", [None, 3])
def test_private_target_signs_exact_authorized_request_id(monkeypatch, tool, arguments, host_generation):
    monkeypatch.setenv("ADP_TOOLS_AGENTCORE_URL", "https://tools.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp")
    monkeypatch.setenv("ADP_TOOLS_FACADE_ROLE_ARN", "arn:aws:iam::123456789012:role/example-facade")
    monkeypatch.setenv("ADP_TOOLS_ASSERTION_KEY_ARN", "arn:aws:kms:us-east-1:123456789012:key/example")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    signed = []

    def sign(**kwargs):
        signed.append(json.loads(kwargs["Message"]))
        return {"Signature": b"fixture-signature"}

    monkeypatch.setattr(server.boto3, "client", lambda service, region_name, **kwargs: SimpleNamespace(sign=sign))
    monkeypatch.setattr(server, "signed_headers", lambda *args: {})

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, content, headers):
            request = json.loads(content)
            assert signed[0]["rpc_id"] == request["id"]
            assert signed[0]["arguments_sha256"] == hashlib.sha256(server.canonical(arguments)).hexdigest()
            assert signed[0]["host_generation"] == host_generation
            assert json.loads(base64.urlsafe_b64decode(headers["x-adp-assertion"] + "=="))["claims"] == signed[0]
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": request["id"], "result": {"content": []}},
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr(server.httpx, "AsyncClient", Client)
    authority = FakeGateway().decision({"authorization": BEARER["Authorization"]})
    if host_generation is not None:
        authority.update(run_id="fixture-host-run", workload_id="fixture-instance", run_expires_at=4102444800,
                         host_generation=host_generation)
    decision = server.Decision.model_validate(authority)
    if tool == "knowledge___remember":
        decision._knowledge_memory = {"schema_version": "1.0", "request_id": str(uuid4()),
                                      "execution_id": str(uuid4()), "requested_execution_id": None}
    if tool == "jira___get_issue":
        decision.jira_connection = {
            "connection_id": "owned-id", "account_id": "fixture-account", "resources_sha256": "a" * 64,
        }
    asyncio.run(server.AgentCoreClient().invoke(tool, arguments, decision))
    assert signed[0]["jira_connection"] == decision.jira_connection
    if tool == "knowledge___remember":
        assert signed[0]["knowledge_memory"] == decision._knowledge_memory
    assert signed[0]["principal"]["user_id"] == "person-one"


@pytest.mark.parametrize("tool,arguments", [
    ("credentials___list", {"service": "fixture"}),
    ("credentials___proxy_request", {"service": "fixture", "method": "GET", "url": "https://fixture.example.invalid/status"}),
])
def test_broker_request_carries_only_signed_human_identity(monkeypatch, tool, arguments):
    monkeypatch.setenv("ADP_TOOLS_AGENTCORE_URL", "https://tools.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp")
    monkeypatch.setenv("ADP_TOOLS_FACADE_ROLE_ARN", "arn:aws:iam::123456789012:role/example-facade")
    monkeypatch.setenv("ADP_TOOLS_ASSERTION_KEY_ARN", "arn:aws:kms:us-east-1:123456789012:key/example")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    def kms_client(service, region_name, config):
        assert service == "kms" and region_name == "us-east-1"
        assert config.connect_timeout == 2 and config.read_timeout == 3
        assert config.retries == {"total_max_attempts": 1}
        return SimpleNamespace(sign=lambda **kwargs: {"Signature": b"synthetic-signature"})

    monkeypatch.setattr(server.boto3, "client", kms_client)

    def sign_gateway(endpoint, body, service, region):
        assert service == "bedrock-agentcore" and region == "us-east-1"
        return {"Authorization": "synthetic-sigv4"}

    monkeypatch.setattr(server, "signed_headers", sign_gateway)

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, content, headers):
            request = json.loads(content)
            claims = json.loads(base64.urlsafe_b64decode(headers["x-adp-assertion"] + "=="))["claims"]
            assert headers["Authorization"] == "synthetic-sigv4"
            assert claims["tool"] == tool
            assert claims["arguments_sha256"] == hashlib.sha256(server.canonical(arguments)).hexdigest()
            assert claims["principal"]["user_id"] == "person-one" and claims["root_human_id"] == "person-one"
            assert claims["run_id"] is None and claims["workload_id"] is None
            assert not {"user_id", "agent_id", "task_id"} & set(request["params"]["arguments"])
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": request["id"], "result": {"content": []}},
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr(server.httpx, "AsyncClient", Client)
    decision = server.Decision.model_validate(FakeGateway().decision({"authorization": BEARER["Authorization"]}))
    asyncio.run(server.AgentCoreClient().invoke(tool, arguments, decision))


@pytest.mark.parametrize("tool", ["credentials___assume_role", "credentials___materialize"])
def test_private_credentials_never_enter_generic_tool_call_or_listing(tool):
    class PrivateGateway(FakeGateway):
        async def authorize(self, requested_tool, arguments, headers):
            assert requested_tool == tool
            return self.decision(headers)

        async def list_tools(self, headers):
            return {**self.decision(headers), "tools": [
                {"name": tool, "description": "Private", "inputSchema": {"type": "object"}},
            ]}

    provider = FakeAgentCore()
    client = TestClient(create_app(gateway=PrivateGateway(), agentcore=provider))
    listed = client.get("/agent/tools", headers=BEARER)
    assert listed.status_code == 200 and listed.json()["tools"] == []
    assert listed.json()["unavailable_tools"] == [
        {"name": tool, "reason": "private_credential_delivery_required"},
    ]
    response = client.post("/agent/tools/call", json={"tool": tool, "arguments": {},
                          "request_id": "00000000-0000-4000-8000-000000000099"}, headers=BEARER)
    assert response.status_code == 409 and response.json()["detail"] == "private_credential_delivery_required"
    assert provider.calls == []


@pytest.mark.parametrize("tool", ["credentials___assume_role", "credentials___materialize", None])
def test_retained_private_result_cannot_be_downloaded_from_generic_receipts(tool):
    private = "synthetic-private-value"
    operation = {
        "operation_execution_id": {"S": "execution"}, "request_id": {"S": "request"},
        "receipt_id": {"S": "receipt"}, "owner_id": {"S": "owner"},
        "status": {"S": "confirmed"}, "result_status": {"S": "success"},
        "result_json": {"S": json.dumps({"result": {"secret_access_key": private}})},
        **({"tool": {"S": tool}} if tool is not None else {}),
    }
    receipt = ExecutionStore.receipt(operation)
    assert receipt["result_status"] == "restricted" and private not in str(receipt)
    with pytest.raises(HTTPException, match="private_credential_delivery_required"):
        ExecutionStore.result_chunk(operation, 0, 100)


@pytest.mark.parametrize("tool", ["credentials___assume_role", "credentials___materialize"])
def test_private_result_cannot_be_claimed_or_persisted_in_generic_execution_store(tool):
    store_client = MagicMock()
    store_client.get_item.return_value = {"Item": {"tool": {"S": tool}}}
    store = ExecutionStore(client=store_client, table="fixture-operations")
    decision = server.Decision.model_validate(FakeGateway().decision({"authorization": BEARER["Authorization"]}))
    with pytest.raises(HTTPException, match="private_credential_delivery_required"):
        store.claim(None, uuid4(), tool, {}, decision)
    with pytest.raises(HTTPException, match="private_credential_delivery_required"):
        store.dispatched("existing-private-operation")
    with pytest.raises(HTTPException, match="private_credential_delivery_required"):
        store.confirmed("existing-private-operation", {"result": {"secret_access_key": "synthetic-private-value"}})
    store_client.transact_write_items.assert_not_called()
    store_client.update_item.assert_not_called()


def test_vendor_decision_scopes_are_in_signed_tool_claims(monkeypatch):
    monkeypatch.setenv("ADP_TOOLS_AGENTCORE_URL", "https://tools.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp")
    monkeypatch.setenv("ADP_TOOLS_FACADE_ROLE_ARN", "arn:aws:iam::123456789012:role/example-facade")
    monkeypatch.setenv("ADP_TOOLS_ASSERTION_KEY_ARN", "arn:aws:kms:us-east-1:123456789012:key/example")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    signed = []

    def sign(**kwargs):
        signed.append(json.loads(kwargs["Message"]))
        return {"Signature": b"synthetic-signature"}

    monkeypatch.setattr(server.boto3, "client", lambda service, region_name, config: SimpleNamespace(sign=sign))
    monkeypatch.setattr(server, "signed_headers", lambda *args: {})

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, content, headers):
            packet = json.loads(content)
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": packet["id"], "result": {"issue": {"key": "TEST-123"}}},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(server.httpx, "AsyncClient", Client)
    decision = FakeGateway().decision({"authorization": BEARER["Authorization"]})
    decision["connection_scopes"] = ["read:jira-work"]
    decision["connection_id"] = "connection-alice"
    result = asyncio.run(server.AgentCoreClient().invoke("jira___get_issue", {"issueIdOrKey": "TEST-123"},
                                                         server.Decision.model_validate(decision)))
    assert result["result"]["issue"]["key"] == "TEST-123"
    assert signed[0]["tool"] == "jira___get_issue"
    assert signed[0]["connection_scopes"] == ["read:jira-work"]
    assert signed[0]["connection_id"] == "connection-alice"


@pytest.mark.parametrize("tool", [
    "websearch___search", "code_interpreter___operation_get", "browser___execution_close",
])
@pytest.mark.parametrize("envelope", ["content", "structuredContent"])
def test_lambda_mcp_result_decodes_without_changing_receipt_or_owner(tool, envelope):
    payload = {"result": {"status": "confirmed", "execution_id": "execution", "request_id": "request"},
               "receipt_id": "original-receipt", "user_id": "original-owner", "tenant_id": "original-tenant"}
    wrapped = {"content": [{"type": "text", "text": json.dumps(payload)}]} if envelope == "content" else {
        "structuredContent": payload, "content": [{"type": "text", "text": "human-readable fallback"}]}
    wrapped["_meta"] = {"adp_receipt_id": "transport-receipt"}
    response = {"jsonrpc": "2.0", "id": "request", "result": wrapped}
    assert server.normalize_lambda_result(response, tool) == {
        "jsonrpc": "2.0", "id": "request", "result": {**payload, "_meta": wrapped["_meta"]}}


@pytest.mark.parametrize("result", [
    None, [], {}, {"structuredContent": []},
    {"content": [{"type": "text", "text": "not JSON"}]},
    {"content": [{"type": "text", "text": "[]"}]},
    {"content": [{"type": "text", "text": "{}"}, {"type": "text", "text": "{}"}]},
    {"content": [{"type": "image", "text": "{}"}]},
    {"structuredContent": {}, "isError": "false"},
])
def test_lambda_mcp_result_rejects_malformed_payload(result):
    with pytest.raises(ValueError):
        server.normalize_lambda_result({"jsonrpc": "2.0", "id": "request", "result": result}, "websearch___search")


@pytest.mark.parametrize("response", [
    {"jsonrpc": "2.0", "id": "request", "error": {"code": -32000, "message": "refused"}},
    {"jsonrpc": "2.0", "id": "request", "result": {
        "isError": True, "content": [{"type": "text", "text": "refused"}]}},
    {"jsonrpc": "2.0", "id": "request", "result": {
        "isError": True, "structuredContent": {"result": {"status": "confirmed"}}}},
])
def test_lambda_mcp_errors_are_never_decoded_as_success(response):
    assert server.normalize_lambda_result(response, "code_interpreter___execution_close") == response


def test_non_lambda_mcp_output_is_preserved():
    response = {"jsonrpc": "2.0", "id": "request", "result": {"content": [{"type": "text", "text": "knowledge"}]}}
    assert server.normalize_lambda_result(response, "knowledge___search") == response


@pytest.mark.parametrize("tool", ["credentials___assume_role", "credentials___materialize"])
def test_expired_private_execution_cannot_recover_from_generic_receipt_or_download(tool):
    synthetic_value = "synthetic-expired-private-value"
    expired = {
        "operation_execution_id": {"S": "original-execution"}, "request_id": {"S": "original-request"},
        "receipt_id": {"S": "original-receipt"}, "owner_id": {"S": "original-owner"},
        "tool": {"S": tool}, "status": {"S": "confirmed"}, "result_status": {"S": "success"},
        "retention_ttl": {"N": "1999999999"},
        "result_json": {"S": json.dumps({"result": {"secret_access_key": synthetic_value}})},
    }
    store = ExecutionStore(client=MagicMock(), table="fixture-operations", clock=lambda: 2000000000)
    receipt = store.receipt(expired)
    assert receipt == {
        "execution_id": "original-execution", "request_id": "original-request", "receipt_id": "original-receipt",
        "root_human_id": "original-owner", "status": "confirmed", "result_status": "restricted",
        "result_unavailable": "private_credential_delivery_required",
    }
    with pytest.raises(HTTPException, match="private_credential_delivery_required"):
        store.result_chunk(expired, 0, 128)
    assert synthetic_value not in str(receipt)


def test_atlassian_read_claim_carries_exact_owner_site_payload_and_release(monkeypatch):
    monkeypatch.setenv("ADP_TOOLS_AGENTCORE_URL", "https://tools.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp")
    monkeypatch.setenv("ADP_TOOLS_FACADE_ROLE_ARN", "arn:aws:iam::123456789012:role/example-facade")
    monkeypatch.setenv("ADP_TOOLS_ASSERTION_KEY_ARN", "arn:aws:kms:us-east-1:123456789012:key/example")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    release = {"contract_version": "1.0", "source_revision": "a" * 40, "image_digest": "sha256:" + "b" * 64,
               "group": "tool-connectors", "group_generation": 1, "backend_generation": 1}
    monkeypatch.setenv("ADP_TOOL_ADAPTER_RELEASE", json.dumps(release))
    signed = []

    def sign(**kwargs):
        signed.append(json.loads(kwargs["Message"]))
        return {"Signature": b"synthetic-signature"}

    monkeypatch.setattr(server.boto3, "client", lambda service, region_name, config: SimpleNamespace(sign=sign))
    monkeypatch.setattr(server, "signed_headers", lambda *args: {})

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, content, headers):
            packet = json.loads(content)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": packet["id"],
                                             "result": {"structuredContent": {"id": "JIRA-ISSUE-42"}}},
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr(server.httpx, "AsyncClient", Client)
    decision = FakeGateway().decision({"authorization": BEARER["Authorization"]})
    connection = {"id": "connection-alice", "revision": "revision-one", "site_id": "site-one"}
    decision.update(connection_scopes=["read:jira:agent-interface"], connection_id=connection["id"],
                    atlassian_connection=connection)
    arguments = {"cloudId": "site-one", "id": "fixture-id"}
    result = asyncio.run(server.AgentCoreClient().invoke("atlassian___get_jira_issue", arguments,
                                                         server.Decision.model_validate(decision)))
    assert result["result"]["structuredContent"]["id"] == "JIRA-ISSUE-42"
    assert len(signed) == 1
    claim = signed[0]
    assert claim["tool"] == "atlassian___get_jira_issue" and claim["connection"] == connection
    assert (claim["audience"], claim["purpose"], claim["connector"]) == (
        "tool-adapters:authorize", "tool_call", "atlassian"
    )
    assert claim["binding"] == {
        "operation_id": str(claim["rpc_id"]), "receipt_id": claim["receipt_id"],
        "payload_sha256": hashlib.sha256(server.canonical(arguments)).hexdigest(), "release": release,
    }

    for changed in (
        {**decision, "connection_id": "connection-other"},
        {**decision, "atlassian_connection": {**connection, "revision": ""}},
        {**decision, "atlassian_connection": {**connection, "unverified": "value"}},
    ):
        with pytest.raises(ValueError, match="Atlassian connection required"):
            asyncio.run(server.AgentCoreClient().invoke(
                "atlassian___get_jira_issue", arguments, server.Decision.model_validate(changed)
            ))
    assert len(signed) == 1


def test_jira_write_claim_requires_original_durable_dispatch(monkeypatch):
    monkeypatch.setenv("ADP_TOOLS_AGENTCORE_URL", "https://tools.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp")
    monkeypatch.setenv("ADP_TOOLS_FACADE_ROLE_ARN", "arn:aws:iam::123456789012:role/example-facade")
    monkeypatch.setenv("ADP_TOOLS_ASSERTION_KEY_ARN", "arn:aws:kms:us-east-1:123456789012:key/example")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    release = {"contract_version": "1.0", "source_revision": "a" * 40, "image_digest": "sha256:" + "b" * 64,
               "group": "tool-connectors", "group_generation": 1, "backend_generation": 1}
    monkeypatch.setenv("ADP_TOOL_ADAPTER_RELEASE", json.dumps(release))
    signed = []

    def sign(**kwargs):
        signed.append(json.loads(kwargs["Message"]))
        return {"Signature": b"synthetic-signature"}

    monkeypatch.setattr(server.boto3, "client", lambda service, region_name, config: SimpleNamespace(sign=sign))
    monkeypatch.setattr(server, "signed_headers", lambda *args: {})

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, content, headers):
            packet = json.loads(content)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": packet["id"], "result": {"id": "JIRA-42"}},
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr(server.httpx, "AsyncClient", Client)
    original = FakeGateway().decision({"authorization": BEARER["Authorization"]})
    connection = {"id": "connection-alice", "revision": "revision-one", "site_id": "site-one"}
    original.update(connection_scopes=["write:jira:agent-interface"], connection_id=connection["id"],
                    atlassian_connection=connection)
    decision = server.Decision.model_validate(original)
    arguments = {"cloudId": "site-one", "fields": {"summary": "fixture"}}
    with pytest.raises(ValueError, match="original Jira write dispatch required"):
        asyncio.run(server.AgentCoreClient().invoke("atlassian___create_jira_issue", arguments, decision))
    assert signed == []
    request_id = "00000000-0000-4000-8000-000000000099"
    decision.atlassian_approval = {
        "tool": "atlassian___create_jira_issue", "request_id": request_id,
        "payload_sha256": hashlib.sha256(server.canonical(arguments)).hexdigest(),
        "policy_revision": 1, "consent_source": "explicit",
    }
    decision._atlassian_operation = {
        "tool": "atlassian___create_jira_issue", "operation_id": f"OP#{decision.principal.tenant_id}#"
        f"{decision.principal.user_id}#atlassian___create_jira_issue#{request_id}",
        "execution_id": "00000000-0000-4000-8000-000000000098", "request_id": request_id,
        "receipt_id": decision.receipt_id, "payload_sha256": hashlib.sha256(server.canonical(arguments)).hexdigest(),
        "dispatch_intent": "dispatched", "connection": connection, "release": release,
        "approval": decision.atlassian_approval,
    }
    assert asyncio.run(server.AgentCoreClient().invoke("atlassian___create_jira_issue", arguments, decision))[
        "result"]["id"] == "JIRA-42"
    assert signed[-1]["atlassian_operation"] == decision._atlassian_operation
    assert signed[-1]["atlassian_approval"] == decision.atlassian_approval
    decision._atlassian_operation["release"] = {**release, "backend_generation": 2}
    with pytest.raises(ValueError, match="original Jira write dispatch required"):
        asyncio.run(server.AgentCoreClient().invoke("atlassian___create_jira_issue", arguments, decision))
    assert len(signed) == 1


@pytest.mark.parametrize("connector,operation,arguments", [
    ("github", "get_user", {}),
    ("jira", "get_issue", {"cloudId": "site-one", "issueIdOrKey": "EX-1"}),
    ("slack", "get_user", {"user": "U12345678"}),
    ("slack", "post_message", {"channel": "C12345678", "text": "fixture", "run_marker": "marker-one"}),
])
def test_legacy_adapter_claim_is_signed_only_with_owned_connection(monkeypatch, connector, operation, arguments):
    monkeypatch.setenv("ADP_TOOLS_AGENTCORE_URL", "https://tools.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp")
    monkeypatch.setenv("ADP_TOOLS_FACADE_ROLE_ARN", "arn:aws:iam::123456789012:role/example-facade")
    monkeypatch.setenv("ADP_TOOLS_ASSERTION_KEY_ARN", "arn:aws:kms:us-east-1:123456789012:key/example")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    release = {"contract_version": "1.0", "source_revision": "a" * 40, "image_digest": "sha256:" + "b" * 64,
               "group": "tool-connectors", "group_generation": 1, "backend_generation": 1}
    monkeypatch.setenv("ADP_TOOL_ADAPTER_RELEASE", json.dumps(release))
    monkeypatch.setenv("ADP_TOOL_ADAPTER_LEGACY_ENABLED", "true")
    signed = []

    def sign(**kwargs):
        signed.append(json.loads(kwargs["Message"]))
        return {"Signature": b"fixture-signature"}

    monkeypatch.setattr(server.boto3, "client", lambda service, region_name, config: SimpleNamespace(sign=sign))
    monkeypatch.setattr(server, "signed_headers", lambda *args: {})

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, content, headers):
            packet = json.loads(content)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": packet["id"], "result": {"fixture": "ok"}},
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr(server.httpx, "AsyncClient", Client)
    decision = FakeGateway().decision({"authorization": BEARER["Authorization"]})
    connection = {"connection_id": "connection-alice", "revision": "revision-one", "account_id": "account-one"}
    decision.update(
        connection_id=connection["connection_id"],
        connection_scopes=["read:user", "read:jira-work", "users:read", "chat:write"],
        **{connector + "_connection": connection},
    )
    tool = f"{connector}___{operation}"
    result = asyncio.run(server.AgentCoreClient().invoke(tool, arguments, server.Decision.model_validate(decision)))
    assert result["result"] == {"fixture": "ok"}
    claim = signed[-1]
    assert claim["connector"] == connector and claim["connection"] == connection
    assert claim["binding"]["receipt_id"] == claim["receipt_id"]
    assert claim["binding"]["payload_sha256"] == hashlib.sha256(server.canonical(arguments)).hexdigest()
    assert claim["binding"]["release"] == release
    if connector == "jira":
        assert claim["site_id"] == arguments["cloudId"]
    if operation == "post_message":
        assert claim["run_marker"] == arguments["run_marker"]
    for forged in ({**decision, "connection_id": "foreign"},
                   {**decision, connector + "_connection": {**connection, "revision": ""}}):
        with pytest.raises(ValueError, match="Legacy connection required"):
            asyncio.run(server.AgentCoreClient().invoke(tool, arguments, server.Decision.model_validate(forged)))
    assert len(signed) == 1
    monkeypatch.delenv("ADP_TOOL_ADAPTER_LEGACY_ENABLED")
    asyncio.run(server.AgentCoreClient().invoke(tool, arguments, server.Decision.model_validate(decision)))
    assert signed[-1].get("binding") is None


def test_jira_write_lost_report_recovers_without_second_provider_effect(monkeypatch):
    release = {"contract_version": "1.0", "source_revision": "a" * 40, "image_digest": "sha256:" + "b" * 64,
               "group": "tool-connectors", "group_generation": 1, "backend_generation": 1}
    monkeypatch.setenv("ADP_TOOL_ADAPTER_RELEASE", json.dumps(release))
    arguments = {"cloudId": "site-one", "fields": {"summary": "owned"}}
    call = {"tool": "atlassian___create_jira_issue", "request_id": CALL["request_id"], "arguments": arguments}
    decision = FakeGateway().decision({"authorization": BEARER["Authorization"]})
    connection = {"id": "connection-one", "revision": "revision-one", "site_id": "site-one"}
    decision.update(connection_id=connection["id"], atlassian_connection=connection,
                    atlassian_approval={"tool": call["tool"], "request_id": call["request_id"],
                                        "payload_sha256": hashlib.sha256(canonical(arguments)).hexdigest(),
                                        "policy_revision": 1, "consent_source": "explicit"})
    available = False
    delivered = []

    async def authorize(self, body, headers, *, closing=False):
        return decision

    async def report(self, payload):
        if not available:
            raise httpx.ConnectError("report transport unavailable")
        delivered.append(payload)
        return {"status": "recorded"}

    class Provider(FakeAgentCore):
        async def invoke(self, tool, arguments, authority):
            self.calls.append((tool, arguments, authority))
            return {"jsonrpc": "2.0", "result": {"structuredContent": {"id": "JIRA-42"}}}

    monkeypatch.setattr(GatewayAuthorizer, "authorize_call", authorize)
    monkeypatch.setattr(GatewayAuthorizer, "record_outcome", report)
    provider = Provider()
    first = TestClient(create_app(agentcore=provider)).post("/agent/tools/call", json=call, headers=BEARER)
    assert first.status_code == 200 and first.json()["status"] == "confirmed"
    assert first.json()["report_status"] == "pending"
    assert len(provider.calls) == 1 and delivered == []
    store = ExecutionStore()
    operation = store.lookup(UUID(first.json()["execution_id"]),
                             UUID(call["request_id"]), call["tool"], server.Decision.model_validate(decision))
    assert operation["write_report_status"]["S"] == "pending"
    available = True
    again = TestClient(create_app(agentcore=provider)).post("/agent/tools/call", json=call, headers=BEARER)
    assert again.status_code == 200 and again.json()["report_status"] == "recorded"
    assert len(delivered) == 1 and delivered[0]["result_status"] == "confirmed"
    assert len(provider.calls) == 1
    changed = TestClient(create_app(agentcore=provider)).post(
        "/agent/tools/call", json={**call, "arguments": {**arguments, "fields": {"summary": "changed"}}},
        headers=BEARER)
    assert changed.status_code == 409 and len(provider.calls) == 1


def test_l5_authorizer_signs_gateway_route_and_discards_forged_authority(monkeypatch):
    calls = internal_plane(monkeypatch, decision={"tools": [], "root_human_id": "person-one"})
    authorizer = GatewayAuthorizer()
    asyncio.run(authorizer.list_l5({"authorization": "Bearer verified-login",
                                    "x-adp-user-authorization": "Bearer forged-login"}))
    call = server.ToolCall.model_validate({"tool": "l5___get_configuration",
                                           "arguments": {"product_id": "product-one"},
                                           "request_id": CALL["request_id"]})
    asyncio.run(authorizer.call_l5(call, {"authorization": "Bearer verified-login"}))
    assert calls == [({}, {"authorization": "AWS4-HMAC-SHA256 signed",
                          "x-adp-user-authorization": "Bearer verified-login"}),
                     ({"tool": "l5___get_configuration", "arguments": {"product_id": "product-one"}},
                      {"authorization": "AWS4-HMAC-SHA256 signed",
                       "x-adp-user-authorization": "Bearer verified-login"})]


@pytest.mark.parametrize("headers", [
    pytest.param({**BEARER, "x-adp-native-test-capability": "native",
                  "x-adp-native-test-operation": CALL["request_id"]}, id="native"),
    pytest.param({"x-adp-chat-capability": "chat", "x-adp-chat-run-id": "run", "x-adp-chat-session-id": "session",
                  "x-adp-workload-token": "workload", "x-agent-runid": "run"}, id="chat"),
    pytest.param({"x-adp-crew-host-assertion": "host", "x-agent-runid": "run"}, id="host"),
    pytest.param({"x-adp-run-credential": "run", "x-adp-workload-token": "workload"}, id="workload"),
])
def test_restricted_discovery_omits_l5_without_losing_authorized_tools(monkeypatch, headers):
    decision = {**FakeGateway().decision({"authorization": BEARER["Authorization"]}), "tools": [TOOL]}
    calls = internal_plane(monkeypatch, decision=decision)
    provider = FakeAgentCore()
    response = TestClient(create_app(agentcore=provider)).get("/agent/tools", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"tools": [client_tool_description(ToolDescription.model_validate(TOOL))],
                               "receipt_id": "gateway-receipt", "root_human_id": "person-one"}
    assert [payload for payload, _ in calls] == [{"schema_version": "1.0", "action": "list"}]
    for name, value in headers.items():
        proof = "x-adp-user-authorization" if name.lower() == "authorization" else name
        assert calls[0][1][proof] == value
    assert provider.calls == []


@pytest.mark.parametrize("identity_header", [
    None, "x-adp-run-credential", "x-adp-workload-token", "x-adp-chat-capability",
    "x-adp-crew-host-assertion", "x-adp-native-test-capability", "x-adp-native-test-operation",
])
@pytest.mark.parametrize("operation", ["list", "call"])
def test_l5_restricted_identity_is_denied_before_http(monkeypatch, identity_header, operation):
    calls = internal_plane(monkeypatch)
    headers = {"authorization": BEARER["Authorization"], identity_header: "restricted"} if identity_header else {}
    authorizer = GatewayAuthorizer()
    call = server.ToolCall.model_validate({**CALL, "tool": "l5___get_configuration"})
    with pytest.raises(HTTPException) as denied:
        asyncio.run(authorizer.list_l5(headers) if operation == "list" else authorizer.call_l5(call, headers))
    assert denied.value.status_code == 403
    assert calls == []


@pytest.mark.parametrize("status", [403, 503])
def test_l5_discovery_does_not_hide_supported_transport_failures(monkeypatch, status):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(GatewayAuthorizer, "list_tools", AsyncMock(return_value={
        **FakeGateway().decision({"authorization": BEARER["Authorization"]}), "tools": [TOOL],
    }))
    listing = AsyncMock(side_effect=HTTPException(status, "L5 authority unavailable"))
    monkeypatch.setattr(GatewayAuthorizer, "list_l5", listing)
    provider = FakeAgentCore()
    response = TestClient(create_app(agentcore=provider)).get("/agent/tools", headers=BEARER)
    assert response.status_code == status
    listing.assert_awaited_once()
    assert provider.calls == []


def test_l5_facade_uses_signed_gateway_and_never_invokes_provider(monkeypatch):
    from unittest.mock import AsyncMock

    provider = FakeAgentCore()
    monkeypatch.setattr(GatewayAuthorizer, "list_tools", AsyncMock(return_value={
        **FakeGateway().decision({"authorization": BEARER["Authorization"]}), "tools": [TOOL],
    }))
    l5_tool = {"name": "l5___get_configuration", "description": "Read configuration",
               "inputSchema": {"type": "object"}, "outputSchema": {"type": "object"},
               "annotations": {"readOnlyHint": True}}
    list_l5 = AsyncMock(return_value={"tools": [l5_tool], "root_human_id": "person-one"})
    call_l5 = AsyncMock(return_value={
        "structuredContent": {"http_status": 200, "data": {"revision": 1}},
        "content": [{"type": "text", "text": '{"http_status":200,"data":{"revision":1}}'}],
    })
    monkeypatch.setattr(GatewayAuthorizer, "list_l5", list_l5)
    monkeypatch.setattr(GatewayAuthorizer, "call_l5", call_l5)
    client = TestClient(create_app(agentcore=provider))
    listing = client.get("/agent/tools", headers=BEARER)
    assert listing.status_code == 200
    assert listing.json()["tools"][-1] == l5_tool
    result = client.post("/agent/tools/call", headers=BEARER, json={
        "tool": l5_tool["name"], "arguments": {"product_id": "product-one", "project_id": "project-one",
                                                "service_id": "security"}, "request_id": CALL["request_id"],
    })
    assert result.status_code == 200
    assert result.json() == call_l5.return_value
    assert call_l5.await_args.args[1].get("authorization") == BEARER["Authorization"]
    assert provider.calls == []
    denied = client.post("/agent/tools/call", headers={"x-adp-run-credential": "forged",
                      "x-adp-workload-token": "forged"}, json={
        "tool": l5_tool["name"], "arguments": {}, "request_id": CALL["request_id"],
    })
    assert denied.status_code == 403
    assert call_l5.await_count == 1
    list_l5.return_value["root_human_id"] = "different-human"
    assert client.get("/agent/tools", headers=BEARER).status_code == 403
