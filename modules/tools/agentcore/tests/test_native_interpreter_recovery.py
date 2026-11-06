"""Real facade/provider code with explicit offline authority and provider substitutes."""

import hashlib
import time
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tools_facade import server
from tools_facade.native_admission import canonical_call
from tools_facade.server import create_app
from test_gateway_interpreter_facade import HEADERS, START, EXECUTE, RESULT, gateway  # noqa: F401
from test_interpreter_native_close import OfflineNativeAuthority


class RecoveryAuthority(OfflineNativeAuthority):
    def __init__(self):
        super().__init__()
        self.started_at = int(time.time())
        self.dispatched = []
        self.reject_ack = False

    async def _request(self, payload, headers, **kwargs):
        self.requests.append(payload)
        body = {"action": "call", **payload}
        operation = headers["x-adp-native-test-operation"]
        return {**self.decision(), "native_admission": {
            "operation_id": operation, "proof": "a" * 43, "reservation_id": self.reservation,
            "quoted_usd": "0.01", "reserved_usd": "0.01", "run_cap_usd": "20",
            "request_sha256": hashlib.sha256(canonical_call(body)).hexdigest(),
            "execution_id": self.execution_id, "new_execution": payload.get("execution_id") is None,
            "session_deadline_at": self.started_at + 300, "cleanup_deadline_at": self.started_at + 600,
            "cleanup_attempts": 2, "frozen_grants": [START, EXECUTE, RESULT]}}

    async def dispatch_native(self, payload, proof, headers):
        self.dispatched.append(payload)
        if self.reject_ack:
            raise ValueError("Offline lost dispatch acknowledgement")
        return {"schema_version": "1.0", "operation_id": headers["x-adp-native-test-operation"],
                "reservation_id": self.reservation, "status": "dispatch_started",
                "dispatch_deadline_at": time.time() + 30}


def headers():
    return {**HEADERS, "x-adp-native-test-capability": "offline-only",
            "x-adp-native-test-operation": str(uuid4())}


@pytest.fixture
def pending(gateway, monkeypatch):  # noqa: F811
    _, provider = gateway
    parent = server.httpx.AsyncClient

    class StreamClient(parent):
        @asynccontextmanager
        async def stream(self, method, url, **kwargs):
            yield await self.post(url, **kwargs)

    monkeypatch.setattr(server.httpx, "AsyncClient", StreamClient)
    authority = RecoveryAuthority()
    client = TestClient(create_app(gateway=authority, agentcore=provider))
    started = client.post("/agent/tools/call", headers=headers(), json={"tool": START,
        "arguments": {}, "request_id": str(uuid4()), "ttl_seconds": 300})
    assert started.status_code == 200, started.text
    body = {"tool": EXECUTE, "arguments": {"session_id": started.json()["result"]["result"]["result"]["session_id"],
        "code": "print(42)", "language": "python"}, "request_id": str(uuid4()), "execution_id": authority.execution_id}
    executed = client.post("/agent/tools/call", headers=headers(), json=body)
    assert executed.status_code == 202, executed.text
    return client, authority, provider, body


def test_native_paid_status_recovers_original_task_then_result_is_cached(pending):
    client, authority, provider, body = pending
    recovered = client.post("/agent/tools/operations/recover", headers=headers(), json=body)
    assert recovered.status_code == 200, recovered.text
    receipt = recovered.json()
    assert receipt["status"] == "confirmed" and receipt["result_status"] == "success"
    assert authority.dispatched[-1] == {"schema_version": "1.0", "action": "recover", "ttl_seconds": None, **body}
    assert provider.calls == ["start", "startCommandExecution", "getTask"]
    task = receipt["result"]["result"]["result"]["provider_id"]
    result = client.post("/agent/tools/call", headers=headers(), json={"tool": RESULT,
        "arguments": {"session_id": body["arguments"]["session_id"], "provider_id": task},
        "request_id": body["request_id"], "execution_id": body["execution_id"]})
    assert result.status_code == 200, result.text
    assert provider.calls == ["start", "startCommandExecution", "getTask"]


@pytest.mark.parametrize("changed", ("request", "execution", "code", "session", "ack", "get"))
def test_native_paid_status_never_polls_changed_original_or_lost_ack(pending, changed):
    client, authority, provider, body = pending
    body = {**body, "arguments": dict(body["arguments"])}
    if changed == "request":
        body["request_id"] = str(uuid4())
    elif changed == "execution":
        body["execution_id"] = str(uuid4())
    elif changed in {"code", "session"}:
        body["arguments"]["code" if changed == "code" else "session_id"] = "changed"
    elif changed == "ack":
        authority.reject_ack = True
    if changed == "get":
        response = client.get(f'/agent/tools/executions/{body["execution_id"]}/operations/{body["request_id"]}',
                              params={"tool": EXECUTE}, headers=headers())
    else:
        response = client.post("/agent/tools/operations/recover", headers=headers(), json=body)
    assert response.status_code in {403, 404, 503}, response.text
    assert provider.calls == ["start", "startCommandExecution"]


@pytest.mark.parametrize("field", ("request_id", "execution_id", "receipt_id", "session_id", "provider_id"))
def test_native_poll_requires_authenticated_original_operation_receipt(pending, monkeypatch, field):
    client, _, provider, body = pending
    original = provider.store.operation_get

    def changed(arguments, claims):
        return {**original(arguments, claims), field: "changed" if field != "provider_id" else "not a task"}

    monkeypatch.setattr(provider.store, "operation_get", changed)
    response = client.post("/agent/tools/operations/recover", headers=headers(), json=body)
    assert response.status_code == 200 and response.json()["status"] == "unknown", response.text
    assert provider.calls == ["start", "startCommandExecution"]


def test_ordinary_recovery_still_forwards_original_ids_for_gateway_tombstone_guard(pending, monkeypatch):
    client, authority, provider, body = pending
    observed = []

    async def guard(payload, headers, **kwargs):
        from fastapi import HTTPException
        observed.append(payload)
        raise HTTPException(403, "Offline original native tombstone guard")

    monkeypatch.setattr(authority, "_request", guard)
    response = client.post("/agent/tools/operations/recover", headers=HEADERS, json=body)
    assert response.status_code == 403
    assert observed[-1] == {"schema_version": "1.0", "action": "recover", "ttl_seconds": None, **body}
    for suffix in ("", "/artifacts/result"):
        response = client.get(f'/agent/tools/executions/{body["execution_id"]}/operations/{body["request_id"]}' + suffix,
                              params={"tool": EXECUTE}, headers=HEADERS)
        assert response.status_code == 403
        assert observed[-1] == {"schema_version": "1.0", "action": "recover", "tool": EXECUTE,
                               "execution_id": body["execution_id"], "request_id": body["request_id"]}
    assert provider.calls == ["start", "startCommandExecution"]
