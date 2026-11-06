"""Original native cleanup through real facade/provider code; authority is offline."""

import hashlib
import time
from contextlib import asynccontextmanager
from uuid import uuid4

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from fastapi.testclient import TestClient

from tools_facade.native_admission import canonical_call
from tools_facade import server
from tools_facade.executions import ExecutionStore
from tools_facade.server import GatewayAuthorizer, create_app
from test_gateway_interpreter_facade import HEADERS, gateway  # noqa: F401

START = "code_interpreter___start"
CLOSE = "code_interpreter___close"


class OfflineNativeAuthority(GatewayAuthorizer):
    """Exercises handoff binding, not genuine approval, quote, or funding proof."""

    def __init__(self):
        self.run_id, self.original_id, self.execution_id = (str(uuid4()) for _ in range(3))
        self.owner = "user-one"
        self.reservation = "offline-original-reservation"
        self.requests = []
        self.outcomes = []
        self.attempts = 0
        self.override = {}

    def decision(self):
        return {"schema_version": "1.0", "allowed": True, "receipt_id": self.reservation,
            "principal": {"user_id": self.owner, "root_human_id": self.owner,
                          "tenant_id": "tenant-one", "team_id": "team-one"},
            "run_id": self.run_id, "workload_id": "native-test-offline", "run_expires_at": int(time.time()) + 600}

    async def _request(self, payload, headers, **kwargs):
        if kwargs.get("outcome") is True:
            assert payload["receipt_id"] == self.reservation
            assert payload["execution_id"] == self.execution_id
            assert payload["run_id"] == self.run_id
            assert payload["user_id"] == self.owner
            assert payload["tenant_id"] == "tenant-one" and payload["team_id"] == "team-one"
            assert any(
                request.get("request_id") == payload["request_id"]
                and payload["tool"] == (CLOSE if request.get("action") == "close" else request.get("tool"))
                for request in self.requests
            )
            self.outcomes.append(payload)
            return {"status": "recorded"}
        self.requests.append(payload)
        now = time.time()
        if payload.get("action") == "recover":
            return self.decision()
        if payload.get("action") == "close":
            assert payload["tool"] == START
            self.attempts += 1
            return {**self.decision(), "native_cleanup": {
                "run_id": self.run_id, "operation_id": headers["x-adp-native-test-operation"],
                "original_operation_id": self.original_id, "execution_id": self.execution_id,
                "reservation_id": self.reservation, "attempt": self.attempts,
                "status": "dispatch_started", "started_at": now, "dispatch_deadline_at": now + 30,
                "replay": False, **self.override}}
        assert payload["tool"] == START
        body = {"action": "call", **payload}
        return {**self.decision(), "native_admission": {
            "operation_id": self.original_id, "proof": "a" * 43, "reservation_id": self.reservation,
            "quoted_usd": "0.01", "reserved_usd": "0.01", "run_cap_usd": "20",
            "request_sha256": hashlib.sha256(canonical_call(body)).hexdigest(),
            "execution_id": self.execution_id, "new_execution": True,
            "session_deadline_at": int(now) + 300, "cleanup_deadline_at": int(now) + 600,
            "cleanup_attempts": 2, "frozen_grants": [START, CLOSE]}}

    async def dispatch_native(self, payload, proof, headers):
        return {"schema_version": "1.0", "operation_id": self.original_id,
                "reservation_id": self.reservation, "status": "dispatch_started",
                "dispatch_deadline_at": time.time() + 30}


@pytest.fixture
def started(gateway, monkeypatch):  # noqa: F811 - imported parametrized pytest fixture
    _, provider = gateway
    provider_client = server.httpx.AsyncClient

    class StreamClient(provider_client):
        @asynccontextmanager
        async def stream(self, method, url, **kwargs):
            assert method == "POST"
            yield await self.post(url, **kwargs)

    monkeypatch.setattr(server.httpx, "AsyncClient", StreamClient)
    authority = OfflineNativeAuthority()
    client = TestClient(create_app(gateway=authority, agentcore=provider))
    headers = {**HEADERS, "x-adp-native-test-capability": "offline-only",
               "x-adp-native-test-operation": authority.original_id}
    response = client.post("/agent/tools/call", headers=headers, json={"tool": START,
        "arguments": {}, "request_id": str(uuid4()), "ttl_seconds": 300})
    assert response.status_code == 200, response.text
    receipt = response.json()
    session = receipt["result"]["result"]["result"]["session_id"]
    return client, authority, provider, session


def direct_close(started, *, session=None, request_id=None):
    client, authority, _, original = started
    return client.post("/agent/tools/call", headers={**HEADERS,
        "x-adp-native-test-capability": "offline-only", "x-adp-native-test-operation": str(uuid4())},
        json={"tool": CLOSE, "arguments": {"session_id": session or original},
              "request_id": request_id or str(uuid4()), "execution_id": authority.execution_id})


def root(authority):
    return boto3.client("dynamodb", region_name="us-east-1").get_item(TableName="facade-executions",
        Key={"execution_id": {"S": authority.execution_id}}, ConsistentRead=True)["Item"]


def test_direct_native_close_uses_original_funding_and_terminal_provider_proof(started):
    client, authority, provider, session = started
    before = root(authority)
    response = direct_close(started)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["status"] == "confirmed" and receipt["result_status"] == "success"
    assert receipt["receipt_id"] == authority.reservation
    assert receipt["execution_id"] == authority.execution_id
    assert receipt["result"]["result"]["result"]["session_id"] == session
    assert authority.requests[-1]["tool"] == START and authority.requests[-1]["action"] == "close"
    assert provider.calls == ["start", "stop"]
    after = root(authority)
    assert after["cleanup_evidence"]["S"] == "provider_terminated"
    assert after["state"]["S"] == "closed"
    for field in ("deadline_at", "native_test_run_id", "native_test_reservation_id", "native_test_operation_id"):
        assert before[field] == after[field]
    response = client.get(f'/agent/tools/executions/{authority.execution_id}/operations/{receipt["request_id"]}',
                          params={"tool": CLOSE}, headers=HEADERS)
    assert response.status_code == 200 and response.json()["status"] == "confirmed"
    assert authority.outcomes
    assert authority.outcomes[-1]["tool"] == CLOSE
    assert authority.outcomes[-1]["request_id"] == receipt["request_id"]
    assert provider.calls == ["start", "stop"]


@pytest.mark.parametrize("mutation", ("session", "owner", "original", "hold", "expired", "replay"))
def test_direct_native_close_refuses_changed_original_binding_before_provider(started, mutation):
    _, authority, provider, session = started
    if mutation == "owner":
        authority.owner = "other-owner"
    elif mutation == "original":
        authority.override["original_operation_id"] = str(uuid4())
    elif mutation == "hold":
        authority.override["reservation_id"] = "other-hold"
    elif mutation == "expired":
        authority.override["dispatch_deadline_at"] = time.time() - 1
    elif mutation == "replay":
        authority.override["replay"] = True
    response = direct_close(started, session="wrong-session" if mutation == "session" else session)
    assert response.status_code in {403, 404, 503}, response.text
    assert provider.calls == ["start"]
    assert root(authority)["state"]["S"] == "active"


def test_unknown_native_close_status_never_resubmits_provider_cleanup(started):
    client, authority, provider, _ = started
    provider.fail_stop = True
    response = direct_close(started)
    assert response.status_code == 202 and response.json()["status"] == "unknown"
    receipt = response.json()
    assert provider.calls == ["start", "stop"]
    for _ in range(2):
        response = client.get(f'/agent/tools/executions/{authority.execution_id}/operations/{receipt["request_id"]}',
                              params={"tool": CLOSE}, headers=HEADERS)
        assert response.status_code == 200 and response.json()["status"] == "unknown"
    assert provider.calls == ["start", "stop"]
    assert root(authority)["cleanup_status"]["S"] == "unknown"


def test_direct_native_close_after_dispatch_expiry_preserves_original_cleanup_window(started):
    _, authority, provider, _ = started
    boto3.client("dynamodb", region_name="us-east-1").update_item(TableName="facade-executions",
        Key={"execution_id": {"S": authority.execution_id}}, UpdateExpression="SET deadline_at = :past",
        ExpressionAttributeValues={":past": {"N": str(int(time.time()) - 1)}})
    response = direct_close(started)
    assert response.status_code == 200 and response.json()["status"] == "confirmed"
    assert provider.calls == ["start", "stop"]


def test_same_direct_close_reference_returns_saved_receipt_without_another_send(started):
    _, authority, provider, _ = started
    request_id = str(uuid4())
    first = direct_close(started, request_id=request_id)
    second = direct_close(started, request_id=request_id)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert authority.attempts == 2 and provider.calls == ["start", "stop"]


def test_lost_cleanup_journal_acknowledgement_never_sends_or_replays(started, monkeypatch):
    _, authority, provider, _ = started
    original_client = ExecutionStore._client
    lost = []

    class Client:
        def __init__(self, client):
            self.client = client

        def __getattr__(self, name):
            return getattr(self.client, name)

        def transact_write_items(self, **kwargs):
            reply = self.client.transact_write_items(**kwargs)
            if not lost and any("native_cleanup_operation_id" in item.get("Put", {}).get("Item", {})
                                for item in kwargs["TransactItems"]):
                lost.append(True)
                raise EndpointConnectionError(endpoint_url="https://offline.invalid")
            return reply

    monkeypatch.setattr(ExecutionStore, "_client", lambda store: Client(original_client(store)))
    request_id = str(uuid4())
    for _ in range(2):
        response = direct_close(started, request_id=request_id)
        assert response.status_code == 202 and response.json()["status"] == "unknown"
    assert lost == [True] and provider.calls == ["start"]
    assert root(authority)["state"]["S"] == "active"
