import base64
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tools_facade import server

ENDPOINT = "https://tools.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
TOKEN = "synthetic-user-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
SEARCH = {"name": "websearch___search", "description": "Search ☃", "inputSchema": {
    "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"],
}}
OTHER = {"name": "internal___tool", "inputSchema": {"type": "object"}}


class Authority:
    allowed = True

    async def list_tools(self, headers):
        return {**await self.authorize(None, None, headers), "tools": [SEARCH]}

    async def authorize(self, tool, arguments, headers):
        if headers.get("authorization") != HEADERS["Authorization"]:
            raise HTTPException(403, "invalid user")
        return {"schema_version": "1.0", "allowed": self.allowed, "receipt_id": "authorized-receipt",
                "principal": {"user_id": "user-one", "root_human_id": "user-one",
                              "tenant_id": "tenant-one", "team_id": "team-one"}}


@pytest.fixture
def live_wire(monkeypatch):
    monkeypatch.setenv("ADP_TOOLS_AGENTCORE_URL", ENDPOINT)
    monkeypatch.setenv("ADP_TOOLS_FACADE_ROLE_ARN", "arn:aws:iam::123456789012:role/example-facade")
    monkeypatch.setenv("ADP_TOOLS_ASSERTION_KEY_ARN", "arn:aws:kms:us-east-1:123456789012:key/example")
    original_client = server.boto3.client
    state = {"calls": [], "pages": [{"tools": [SEARCH, OTHER]}], "search_error": False}

    def client(service, **kwargs):
        if service == "kms":
            return SimpleNamespace(sign=lambda **arguments: {"Signature": b"synthetic-signature"})
        return original_client(service, **kwargs)

    class Transport:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, content, headers):
            assert url == ENDPOINT
            assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
            packet = json.loads(content)
            claims = json.loads(base64.urlsafe_b64decode(headers["x-adp-assertion"] + "=="))["claims"]
            assert claims["method"] == packet["method"]
            assert claims["principal"]["tenant_id"] == headers["x-adp-tenant"] == "tenant-one"
            assert claims["receipt_id"] == "authorized-receipt"
            arguments = packet["params"] if packet["method"] == "tools/list" else packet["params"]["arguments"]
            assert claims["arguments_sha256"] == hashlib.sha256(
                json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
            state["calls"].append(packet)
            if packet["method"] == "tools/list":
                assert claims["tool"] == "tools/list"
                index = 1 if arguments.get("cursor") else 0
                result = {**state["pages"][index], "_meta": {"adp_receipt_id": claims["receipt_id"]}}
                if state.get("bad_receipt"):
                    result["_meta"]["adp_receipt_id"] = "wrong-receipt"
            else:
                assert claims["tool"] == packet["params"]["name"] == "websearch___search"
                search_result = {"status": "completed", "results": [{
                    "url": "https://example.com/report", "text": "synthetic source"}],
                    "citations": ["https://example.com/report"], "query_count": 1,
                    "estimated_search_usd": 0.007,
                    "pricing_source": "https://aws.amazon.com/bedrock/agentcore/pricing/"}
                content = server.canonical(search_result)
                result = {"structuredContent": {"result": {
                    "status": "confirmed", "execution_id": arguments["execution_id"],
                    "request_id": arguments["request_id"], "receipt_id": claims["receipt_id"],
                    "result": search_result, "artifact": {"artifact_id": arguments["request_id"],
                        "content_type": "application/json", "byte_length": len(content),
                        "content_sha256": hashlib.sha256(content).hexdigest()},
                }}}
                if state["search_error"]:
                    result = {"isError": True, "content": [{"type": "text", "text": "provider failed"}]}
                if state.get("change_after_search"):
                    state["pages"] = [{"tools": [SEARCH]}]
            envelope = {"jsonrpc": "2.0", "id": packet["id"], "result": result}
            if state.get("bad_id"):
                envelope["id"] = "wrong-id"
            if state.get("rpc_error"):
                envelope = {"jsonrpc": "2.0", "id": packet["id"], "error": {"code": -32000}}
            return httpx.Response(200, json=envelope, request=httpx.Request("POST", url))

    monkeypatch.setattr(server.boto3, "client", client)
    monkeypatch.setattr(server.httpx, "AsyncClient", Transport)
    return state


def test_live_discovery_hashes_raw_pages_without_exposing_unauthorized_tools(live_wire):
    live_wire["pages"] = [{"tools": [SEARCH], "nextCursor": "second"}, {"tools": [OTHER]}]
    client = TestClient(server.create_app(gateway=Authority()))
    response = client.get("/api/agent/tools?live=true", headers=HEADERS)
    assert response.status_code == 200
    result = response.json()
    assert result["tools"] == [server.client_tool_description(server.ToolDescription.model_validate(SEARCH))]
    assert result["discovery"] == {
        "gateway_url_sha256": hashlib.sha256(ENDPOINT.encode()).hexdigest(),
        "tools_list_sha256": hashlib.sha256(json.dumps(
            [SEARCH, OTHER], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(),
    }
    assert [call["params"] for call in live_wire["calls"]] == [{}, {"cursor": "second"}]


@pytest.mark.parametrize("headers,allowed", [({}, True), ({"Authorization": "Bearer foreign"}, True),
                                             (HEADERS, False)])
def test_live_discovery_requires_existing_user_authority(live_wire, headers, allowed):
    authority = Authority()
    authority.allowed = allowed
    response = TestClient(server.create_app(gateway=authority)).get("/api/agent/tools?live=true", headers=headers)
    assert response.status_code == 403
    assert live_wire["calls"] == []


@pytest.mark.parametrize("pages", [
    [{"tools": []}], [{"tools": [{"name": SEARCH["name"], "inputSchema": "broken"}]}],
    [{"tools": [SEARCH, SEARCH]}], [{"tools": [SEARCH], "nextCursor": "again"},
                                   {"tools": [OTHER], "nextCursor": "again"}],
])
def test_broken_schema_and_incomplete_discovery_fail_closed(live_wire, pages):
    live_wire["pages"] = pages
    response = TestClient(server.create_app(gateway=Authority())).get("/api/agent/tools?live=true", headers=HEADERS)
    assert response.status_code == 503
    assert "discovery" not in response.json()


@pytest.mark.parametrize("failure", ["bad_receipt", "bad_id", "rpc_error"])
def test_discovery_rejects_unbound_or_failed_mcp_responses(live_wire, failure):
    live_wire[failure] = True
    response = TestClient(server.create_app(gateway=Authority())).get("/api/agent/tools?live=true", headers=HEADERS)
    assert response.status_code == 503


CALL = {"tool": SEARCH["name"], "arguments": {"query": "explicit offline search fixture"},
        "request_id": "00000000-0000-4000-8000-000000000099"}
CALL_HEADERS = {**HEADERS, "x-adp-expected-gateway-sha256": hashlib.sha256(ENDPOINT.encode()).hexdigest()}


def test_search_returns_bound_success_receipt_from_signed_dispatch(live_wire):
    response = TestClient(server.create_app(gateway=Authority())).post(
        "/api/agent/tools/call", headers=CALL_HEADERS, json=CALL)
    assert response.status_code == 200
    receipt = response.json()
    assert receipt["status"] == "confirmed" and receipt["result_status"] == "success"
    assert receipt["request_id"] == CALL["request_id"]
    assert receipt["receipt_id"] == "authorized-receipt"
    assert receipt["gateway_url_sha256"] == CALL_HEADERS["x-adp-expected-gateway-sha256"]
    assert str(UUID(receipt["execution_id"])) == receipt["execution_id"]
    packet, = live_wire["calls"]
    assert packet["method"] == "tools/call"
    assert packet["params"] == {"name": SEARCH["name"], "arguments": {
        **CALL["arguments"], "execution_id": receipt["execution_id"], "request_id": CALL["request_id"],
    }}


def test_search_cannot_run_on_a_facade_bound_to_another_gateway(live_wire):
    response = TestClient(server.create_app(gateway=Authority())).post(
        "/api/agent/tools/call", headers={**HEADERS, "x-adp-expected-gateway-sha256": "f" * 64}, json=CALL)
    assert response.status_code == 409
    assert response.json() == {"detail": "deployed gateway mismatch"}
    assert live_wire["calls"] == []


def test_search_provider_failure_preserves_unconfirmed_original_receipt(live_wire):
    live_wire["search_error"] = True
    response = TestClient(server.create_app(gateway=Authority())).post(
        "/api/agent/tools/call", headers=CALL_HEADERS, json=CALL)
    assert response.status_code == 202
    receipt = response.json()
    assert receipt["status"] == "unknown"
    assert receipt["request_id"] == CALL["request_id"]
    assert receipt["receipt_id"] == "authorized-receipt"
    assert str(UUID(receipt["execution_id"])) == receipt["execution_id"]
    assert "result_status" not in receipt and "result" not in receipt
    assert [call["method"] for call in live_wire["calls"]] == ["tools/call"]


def test_live_discovery_digest_reflects_current_provider_schema(live_wire):
    client = TestClient(server.create_app(gateway=Authority()))
    before = client.get("/api/agent/tools?live=true", headers=HEADERS)
    assert before.status_code == 200
    live_wire["change_after_search"] = True
    searched = client.post("/api/agent/tools/call", headers=CALL_HEADERS, json=CALL)
    assert searched.status_code == 200
    after = client.get("/api/agent/tools?live=true", headers=HEADERS)
    assert after.status_code == 200
    # The visible entitlement set is unchanged, but removing another raw
    # provider tool changes the discovery fingerprint returned by the server.
    assert before.json()["tools"] == after.json()["tools"]
    assert before.json()["discovery"]["tools_list_sha256"] == hashlib.sha256(json.dumps(
        [SEARCH, OTHER], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert after.json()["discovery"]["tools_list_sha256"] == hashlib.sha256(json.dumps(
        [SEARCH], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert [call["method"] for call in live_wire["calls"]] == ["tools/list", "tools/call", "tools/list"]
