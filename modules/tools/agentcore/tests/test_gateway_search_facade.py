"""Recover a signed search outcome across the façade and Lambda provider boundary."""

import base64
import hashlib
import json

import boto3
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from moto import mock_aws

from agentcore_tools.gateway_search import GatewaySearch
from tools_facade.encoding import canonical
from tools_facade.server import AgentCoreClient, create_app

CALL = {"tool": "websearch___search", "arguments": {"query": "example"},
        "request_id": "00000000-0000-4000-8000-000000000099"}
HEADERS = {"Authorization": "Bearer original"}


class Authority:
    def __init__(self):
        self.denied = False

    async def authorize(self, tool, arguments, headers):
        return await self.recover(tool, headers)

    async def recover(self, tool, headers):
        if self.denied or headers.get("authorization") not in {"Bearer original", "Bearer refreshed"}:
            raise HTTPException(403, "tool authority refused")
        return {"schema_version": "1.0", "allowed": True, "receipt_id": "receipt-one",
                "principal": {"user_id": "user-one", "root_human_id": "user-one",
                              "tenant_id": "tenant-one", "team_id": "team-one"}}

    async def close(self, tool, headers):
        return await self.recover(tool, headers)


@pytest.fixture
def search_table(monkeypatch):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(TableName="gateway-search", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"},
                       {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"},
                                  {"AttributeName": "arrived_at", "AttributeType": "S"}])
        client.create_table(TableName="facade-executions", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "execution_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "execution_id", "AttributeType": "S"},
                                  {"AttributeName": "cleanup_partition", "AttributeType": "S"},
                                  {"AttributeName": "next_cleanup_at", "AttributeType": "N"}],
            GlobalSecondaryIndexes=[{"IndexName": "cleanup-due",
                "KeySchema": [{"AttributeName": "cleanup_partition", "KeyType": "HASH"},
                              {"AttributeName": "next_cleanup_at", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"}}])
        monkeypatch.setenv("ADP_TOOLS_TABLE", "gateway-search")
        monkeypatch.setenv("ADP_TOOLS_EXECUTION_TABLE", "facade-executions")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        yield client


def test_gateway_search_lost_reply_recovers_confirmed_result_without_requery(search_table):
    class SearchProvider(AgentCoreClient):
        def __init__(self):
            self.calls = []
            self.store = GatewaySearch(search_table)

        async def invoke(self, tool, arguments, decision):
            claims = {"principal": decision.principal.model_dump(exclude={"root_human_id"}),
                      "receipt_id": decision.receipt_id}
            if tool == "websearch___search":
                def paid_search(query, *, before_search):
                    before_search()
                    self.calls.append(query)
                    return {"status": "completed", "results": [{"url": "https://example.com", "text": "sample" * 3000}]}
                receipt = self.store.search(arguments, claims, paid_search, lambda: (claims, arguments))
                assert receipt["status"] == "confirmed"
                raise RuntimeError("reply lost after provider confirmation")
            assert tool == "websearch___operation_get"
            receipt = self.store.operation_get(arguments, claims)
            return {"jsonrpc": "2.0", "result": {"result": receipt}}

    authority, provider = Authority(), SearchProvider()
    first = TestClient(create_app(gateway=authority, agentcore=provider)).post(
        "/agent/tools/call", json=CALL, headers=HEADERS)
    assert first.status_code == 202 and first.json()["status"] == "unknown"
    restarted = TestClient(create_app(gateway=authority, agentcore=provider))
    path = f'/agent/tools/executions/{first.json()["execution_id"]}/operations/{CALL["request_id"]}'
    recovered = restarted.get(path, params={"tool": CALL["tool"]},
                              headers={"Authorization": "Bearer refreshed"})
    assert recovered.status_code == 200 and recovered.json()["status"] == "confirmed"
    assert recovered.json()["receipt_id"] == first.json()["receipt_id"]
    result = recovered.json()["result"]["result"]["result"]["result"]
    assert result["results"][0]["url"] == "https://example.com"
    provider_receipt = recovered.json()["result"]["result"]["result"]
    assert provider_receipt["artifact"]["content_sha256"] == hashlib.sha256(canonical(result)).hexdigest()
    content = bytearray()
    expected_digest = None
    expected_length = None
    while True:
        artifact = restarted.get(path + "/artifacts/result", params={"tool": CALL["tool"],
            "offset": len(content)}, headers=HEADERS)
        assert artifact.status_code == 200
        chunk = artifact.json()
        assert chunk["offset"] == len(content) and chunk["length"] <= 8192
        assert chunk["total_length"] > 8192
        decoded = base64.b64decode(chunk["data_base64"], validate=True)
        assert chunk["length"] == len(decoded)
        if expected_digest is None:
            expected_digest, expected_length = chunk["sha256"], chunk["total_length"]
        assert chunk["sha256"] == expected_digest and chunk["total_length"] == expected_length
        content.extend(decoded)
        if len(content) == chunk["total_length"]:
            break
    assert hashlib.sha256(content).hexdigest() == chunk["sha256"]
    assert json.loads(content) == recovered.json()["result"]
    repeated = restarted.post("/agent/tools/call", json=CALL, headers=HEADERS)
    assert repeated.status_code == 200 and repeated.json() == recovered.json()
    assert provider.calls == [{"query": "example"}]
    closed = restarted.post(f'/agent/tools/executions/{first.json()["execution_id"]}/close',
                             params={"tool": CALL["tool"]}, headers=HEADERS)
    assert closed.status_code == 200 and closed.json()["cleanup_status"] == "confirmed"
    execution = search_table.get_item(TableName="facade-executions",
        Key={"execution_id": {"S": first.json()["execution_id"]}}, ConsistentRead=True)["Item"]
    assert execution["state"]["S"] == "closed" and execution["cleanup_status"]["S"] == "confirmed"
    assert provider.calls == [{"query": "example"}]
    authority.denied = True
    assert restarted.get(path, params={"tool": CALL["tool"]}, headers=HEADERS).status_code == 403


def test_gateway_search_malformed_response_cannot_confirm_operation(search_table):
    class MalformedProvider(AgentCoreClient):
        async def invoke(self, tool, arguments, decision):
            return {"jsonrpc": "2.0", "result": {"isError": True}}

    response = TestClient(create_app(gateway=Authority(), agentcore=MalformedProvider())).post(
        "/agent/tools/call", json=CALL, headers=HEADERS)
    assert response.status_code == 202 and response.json()["status"] == "unknown"


@pytest.mark.parametrize("corrupt", ["content_sha256", "byte_length"])
def test_recovered_search_with_corrupt_provider_artifact_cannot_confirm(search_table, corrupt):
    class CorruptProvider(AgentCoreClient):
        def __init__(self):
            self.calls = 0
            self.store = GatewaySearch(search_table)

        async def invoke(self, tool, arguments, decision):
            claims = {"principal": decision.principal.model_dump(exclude={"root_human_id"}),
                      "receipt_id": decision.receipt_id}
            if tool == "websearch___search":
                def paid_search(query, *, before_search):
                    before_search()
                    self.calls += 1
                    return {"status": "completed", "results": [{"url": "https://example.com", "text": "sample"}]}

                self.store.search(arguments, claims, paid_search, lambda: (claims, arguments))
                raise RuntimeError("reply lost after provider confirmation")
            receipt = self.store.operation_get(arguments, claims)
            receipt["artifact"][corrupt] = "0" * 64 if corrupt == "content_sha256" else 1
            return {"result": {"result": receipt}}

    provider = CorruptProvider()
    client = TestClient(create_app(gateway=Authority(), agentcore=provider))
    original = client.post("/agent/tools/call", json=CALL, headers=HEADERS)
    path = f'/agent/tools/executions/{original.json()["execution_id"]}/operations/{CALL["request_id"]}'
    assert original.status_code == 202
    assert client.get(path, params={"tool": CALL["tool"]}, headers=HEADERS).json()["status"] == "unknown"
    assert client.get(path + "/artifacts/result", params={"tool": CALL["tool"]}, headers=HEADERS).status_code == 404
    assert provider.calls == 1


@pytest.mark.parametrize("arguments", [
    {"query": " "}, {"query": "x" * 201}, {"query": 42},
    {"query": "example", "maxResults": 1}, {"query": "example", "filters": {}},
])
def test_search_rejects_malformed_public_arguments_without_dispatch(search_table, arguments):
    class TrackingProvider(AgentCoreClient):
        calls = 0

        async def invoke(self, tool, args, decision):
            self.calls += 1
            pytest.fail("invalid search dispatched")

    provider = TrackingProvider()
    response = TestClient(create_app(gateway=Authority(), agentcore=provider)).post(
        "/agent/tools/call", json={**CALL, "arguments": arguments}, headers=HEADERS)
    assert response.status_code == 422 and provider.calls == 0


def test_denied_search_never_dispatches(search_table):
    class TrackingProvider(AgentCoreClient):
        async def invoke(self, tool, args, decision):
            pytest.fail("denied search dispatched")

    authority = Authority()
    authority.denied = True
    response = TestClient(create_app(gateway=authority, agentcore=TrackingProvider())).post(
        "/agent/tools/call", json=CALL, headers=HEADERS)
    assert response.status_code == 403


def test_provider_throttle_keeps_retry_guidance_and_original_request_id(search_table):
    class ThrottledProvider(AgentCoreClient):
        def __init__(self):
            self.calls = 0
            self.store = GatewaySearch(search_table)

        async def invoke(self, tool, arguments, decision):
            claims = {"principal": decision.principal.model_dump(exclude={"root_human_id"}),
                      "receipt_id": decision.receipt_id}
            if tool == "websearch___operation_get":
                return {"result": {"result": self.store.operation_get(arguments, claims)}}
            assert tool == "websearch___search"

            def throttled(query, *, before_search):
                before_search()
                self.calls += 1
                raise HTTPException(429, "provider throttled", headers={"Retry-After": "12"})

            receipt = self.store.search(arguments, claims, throttled, lambda: (claims, arguments))
            return {"result": {"result": receipt}}

    provider = ThrottledProvider()
    client = TestClient(create_app(gateway=Authority(), agentcore=provider))
    first = client.post("/agent/tools/call", json=CALL, headers=HEADERS)
    assert first.status_code == 202 and first.json()["status"] == "unknown"
    assert first.headers["Retry-After"] == "12" and first.json()["retry_after_seconds"] == 12
    assert first.json()["request_id"] == CALL["request_id"]
    repeated = client.post("/agent/tools/call", json=CALL, headers=HEADERS)
    assert repeated.status_code == 202 and repeated.json() == first.json()
    assert repeated.headers["Retry-After"] == "12" and provider.calls == 1
    changed = client.post("/agent/tools/call", json={**CALL, "arguments": {"query": "changed"}}, headers=HEADERS)
    assert changed.status_code == 409 and provider.calls == 1
