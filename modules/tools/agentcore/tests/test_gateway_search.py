"""Durable search ownership, response loss and artifact recovery."""

import base64
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

import boto3
import pytest
from fastapi import HTTPException
from moto import mock_aws
from botocore.credentials import Credentials

from agentcore_tools.gateway_search import GatewaySearch
from agentcore_tools.websearch import gateway_call

EXECUTION_ID = "00000000-0000-4000-8000-000000000001"
REQUEST_ID = "00000000-0000-4000-8000-000000000002"
CLAIMS = {"principal": {"tenant_id": "tenant-one", "user_id": "user-one", "team_id": "team-one"},
          "receipt_id": "receipt-one"}
ARGS = {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID, "query": "example"}


@pytest.fixture
def store(monkeypatch):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(TableName="gateway-search", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"},
                       {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"},
                                  {"AttributeName": "arrived_at", "AttributeType": "S"}])
        monkeypatch.setenv("ADP_TOOLS_TABLE", "gateway-search")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        yield GatewaySearch(client)


def test_concurrent_search_recovery_conflict_and_artifact(store):
    started = threading.Event()
    release = threading.Event()
    calls = []

    def provider(query, *, before_search):
        before_search()
        calls.append(query)
        started.set()
        assert release.wait(5)
        return {"status": "completed", "results": [{"url": "https://example.com"}]}

    with ThreadPoolExecutor(max_workers=2) as pool:
        original = pool.submit(store.search, ARGS, CLAIMS, provider, lambda: (CLAIMS, ARGS))
        assert started.wait(5)
        resumed = GatewaySearch(store.client)
        duplicate = resumed.search(ARGS, CLAIMS, provider, lambda: (CLAIMS, ARGS))
        assert duplicate["status"] == "unknown" and duplicate["potential_query_count"] == 1
        with pytest.raises(HTTPException) as conflict:
            resumed.search({**ARGS, "query": "different"}, CLAIMS, provider, lambda: (CLAIMS, ARGS))
        assert conflict.value.status_code == 409
        release.set()
        confirmed = original.result(timeout=5)
    assert calls == [{"query": "example"}]
    assert confirmed == resumed.operation_get({"execution_id": EXECUTION_ID, "request_id": REQUEST_ID}, CLAIMS)
    assert resumed.search(ARGS, CLAIMS, provider, lambda: (CLAIMS, ARGS)) == confirmed
    artifact = resumed.artifact_read({"execution_id": EXECUTION_ID, "artifact_id": REQUEST_ID}, CLAIMS)
    content = base64.b64decode(artifact["data_base64"])
    assert hashlib.sha256(content).hexdigest() == artifact["sha256"] == confirmed["artifact"]["content_sha256"]
    assert "retention_ttl" not in store._owned(CLAIMS, EXECUTION_ID, REQUEST_ID)
    with pytest.raises(HTTPException) as foreign:
        resumed.operation_get({"execution_id": EXECUTION_ID, "request_id": REQUEST_ID},
            {**CLAIMS, "principal": {**CLAIMS["principal"], "user_id": "user-two"}})
    assert foreign.value.status_code == 404


def test_lost_provider_response_remains_unknown_without_replay(store):
    calls = []

    def lost(query, *, before_search):
        before_search()
        calls.append(query)
        raise TimeoutError("response lost after paid query")

    first = store.search(ARGS, CLAIMS, lost, lambda: (CLAIMS, ARGS))
    assert first["status"] == "unknown"
    resumed = GatewaySearch(store.client)
    assert resumed.search(ARGS, CLAIMS, lost, lambda: (CLAIMS, ARGS)) == first
    assert resumed.operation_get({"execution_id": EXECUTION_ID, "request_id": REQUEST_ID}, CLAIMS) == first
    assert len(calls) == 1
    with pytest.raises(HTTPException) as unavailable:
        resumed.artifact_read({"execution_id": EXECUTION_ID, "artifact_id": REQUEST_ID}, CLAIMS)
    assert unavailable.value.status_code == 409


def test_revalidation_refusal_does_not_query(store):
    def provider(query, *, before_search):
        before_search()
        pytest.fail("unauthorized query")

    with pytest.raises(HTTPException) as refused:
        store.search(ARGS, CLAIMS, provider, lambda: ({}, {}))
    assert refused.value.status_code == 403
    assert store.operation_get({"execution_id": EXECUTION_ID, "request_id": REQUEST_ID}, CLAIMS)["status"] == "pending"


@pytest.mark.parametrize("retry_after,expected", [("12", "12"), ("bad", "1"), ("86401", "1")])
def test_provider_http_throttle_uses_bounded_retry_header(retry_after, expected):
    class Response:
        status_code = 429
        headers = {"Retry-After": retry_after}
        closed = False

        def close(self):
            self.closed = True

    class Session:
        trust_env = True

        def post(self, *args, **kwargs):
            assert self.trust_env is False and kwargs["allow_redirects"] is False
            return response

    response = Response()
    with pytest.raises(HTTPException) as throttled:
        gateway_call("https://search.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
                     "us-east-1", "tools/call", {"params": {}}, session=Session(),
                     credentials=Credentials("fixture-access", "fixture-secret"))
    assert throttled.value.status_code == 429
    assert throttled.value.headers["Retry-After"] == expected and response.closed


def test_provider_error_stays_unknown_and_does_not_repeat_search(store):
    calls = []

    def provider(query, *, before_search):
        before_search()
        calls.append(query)
        raise ValueError("malformed MCP provider response")

    original = store.search(ARGS, CLAIMS, provider, lambda: (CLAIMS, ARGS))
    assert original["status"] == "unknown" and original["potential_query_count"] == 1
    assert GatewaySearch(store.client).search(ARGS, CLAIMS, provider, lambda: (CLAIMS, ARGS)) == original
    assert calls == [{"query": "example"}]
