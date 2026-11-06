import base64
import hashlib
import json
from uuid import UUID

import boto3
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tools_facade.executions import ADMISSION_KEY, ExecutionStore
from tools_facade.server import Decision, create_app

CALL = {"tool": "websearch___search", "arguments": {"query": "example"},
        "request_id": "00000000-0000-4000-8000-000000000099"}
HEADERS = {"Authorization": "Bearer original"}


class Gateway:
    def __init__(self):
        self.owner = "person-one"
        self.tenant = "tenant-one"
        self.run = None
        self.denied = False
        self.calls = 0

    async def authorize(self, tool, arguments, headers):
        if getattr(self, "policy_revoked", False):
            raise HTTPException(403, "tool grant revoked")
        return await self.recover(tool, headers)

    async def close(self, tool, headers):
        return await self.recover(tool, headers)

    async def recover(self, tool, headers):
        self.calls += 1
        if self.denied or headers.get("authorization") not in ("Bearer original", "Bearer refreshed"):
            raise HTTPException(403, "tool authority refused")
        return {
            "schema_version": "1.0", "allowed": True, "receipt_id": "original-gateway-receipt",
            "principal": {"user_id": self.owner, "root_human_id": self.owner,
                          "tenant_id": self.tenant, "team_id": "team-one"},
            "run_id": self.run, "workload_id": "pod-one" if self.run else None,
            "run_expires_at": 4102444800 if self.run else None,
        }


class Provider:
    def __init__(self):
        self.calls = 0

    async def invoke(self, tool, arguments, decision):
        self.calls += 1
        return {"jsonrpc": "2.0", "result": {"content": [{"text": "π" * 11000}], "exit_code": 0}}


def operation_path(receipt):
    return f'/agent/tools/executions/{receipt["execution_id"]}/operations/{receipt["request_id"]}'


def pause_admission():
    boto3.client("dynamodb", region_name="us-east-1").put_item(
        TableName="adp-tools-dev-executions",
        Item={"execution_id": {"S": ADMISSION_KEY}, "schema_version": {"S": "1.0"}},
        ConditionExpression="attribute_not_exists(execution_id)",
    )


def test_rollback_pause_keeps_recovery_artifacts_close_and_cleanup_after_restart():
    gateway, provider = Gateway(), Provider()
    first = TestClient(create_app(gateway=gateway, agentcore=provider)).post(
        "/agent/tools/call", json=CALL, headers=HEADERS,
    ).json()
    pause_admission()
    client = TestClient(create_app(gateway=gateway, agentcore=provider))
    denied = client.post("/agent/tools/call", json={**CALL,
        "request_id": "00000000-0000-4000-8000-000000000088"}, headers=HEADERS)
    assert denied.status_code == 403 and provider.calls == 1
    path = operation_path(first)
    refreshed = {"Authorization": "Bearer refreshed"}
    assert client.get(path, params={"tool": CALL["tool"]}, headers=refreshed).json() == first
    artifact = client.get(path + "/artifacts/result", params={"tool": CALL["tool"]}, headers=refreshed)
    assert artifact.status_code == 200
    data = json.dumps(first["result"]).encode("utf-8")
    assert artifact.json()["sha256"] == hashlib.sha256(data).hexdigest()
    assert base64.b64decode(artifact.json()["data_base64"]) == data[:8192]
    close = client.post(f'/agent/tools/executions/{first["execution_id"]}/close',
        params={"tool": CALL["tool"]}, headers=refreshed)
    assert close.status_code == 200 and close.json()["cleanup_status"] == "confirmed"
    ExecutionStore(clock=lambda: 4102444801).fence_due()
    assert client.get(path, params={"tool": CALL["tool"]}, headers=refreshed).json() == first
    assert client.get(path + "/artifacts/result", params={"tool": CALL["tool"]}, headers=refreshed).status_code == 200
    assert provider.calls == 1


def test_pause_between_claim_and_dispatch_retains_pending_reference_without_provider_call(monkeypatch):
    gateway, provider = Gateway(), Provider()
    original_dispatch = ExecutionStore.dispatched

    def pause_before_dispatch(self, operation_key):
        pause_admission()
        return original_dispatch(self, operation_key)

    monkeypatch.setattr(ExecutionStore, "dispatched", pause_before_dispatch)
    client = TestClient(create_app(gateway=gateway, agentcore=provider))
    refused = client.post("/agent/tools/call", json=CALL, headers=HEADERS)
    assert refused.status_code == 403 and provider.calls == 0
    item = boto3.client("dynamodb", region_name="us-east-1").get_item(
        TableName="adp-tools-dev-executions",
        Key={"execution_id": {"S": f'OP#tenant-one#person-one#{CALL["tool"]}#{CALL["request_id"]}'}},
        ConsistentRead=True,
    )["Item"]
    assert item["dispatch_intent"]["S"] == "claimed" and item["status"]["S"] == "pending"
    path = operation_path({"execution_id": item["operation_execution_id"]["S"],
                           "request_id": CALL["request_id"]})
    restarted = TestClient(create_app(gateway=gateway, agentcore=provider))
    receipt = restarted.get(path, params={"tool": CALL["tool"]}, headers=HEADERS)
    assert receipt.status_code == 200 and receipt.json()["status"] == "pending"
    assert receipt.json()["receipt_id"] == item["receipt_id"]["S"]
    assert restarted.get(path + "/artifacts/result", params={"tool": CALL["tool"]}, headers=HEADERS).status_code == 404
    retry = restarted.post("/agent/tools/call", json=CALL, headers=HEADERS)
    assert retry.status_code == 202 and retry.json() == receipt.json() and provider.calls == 0


def test_restart_polling_and_bounded_result_artifact_never_call_provider_twice():
    gateway, provider = Gateway(), Provider()
    first = TestClient(create_app(gateway=gateway, agentcore=provider)).post(
        "/agent/tools/call", json=CALL, headers=HEADERS,
    )
    assert first.status_code == 200
    client = TestClient(create_app(gateway=gateway, agentcore=provider))
    path = operation_path(first.json())
    receipt = client.get(path, params={"tool": CALL["tool"]}, headers={"Authorization": "Bearer refreshed"})
    assert receipt.status_code == 200 and receipt.json() == first.json()
    artifact = path + "/artifacts/result"
    content = json.dumps(first.json()["result"]).encode("utf-8")
    parts = []
    for offset in range(0, len(content), 8192):
        response = client.get(artifact, params={"tool": CALL["tool"], "offset": offset}, headers=HEADERS)
        assert response.status_code == 200
        chunk = response.json()
        assert chunk["offset"] == offset and chunk["length"] <= 8192
        assert chunk["total_length"] == len(content) and chunk["content_type"] == "application/json"
        assert chunk["sha256"] == hashlib.sha256(content).hexdigest()
        parts.append(base64.b64decode(chunk["data_base64"]))
    assert b"".join(parts) == content
    assert provider.calls == 1


@pytest.mark.parametrize("provider_result", [
    {"jsonrpc": "2.0", "error": {"code": -32000, "message": "failed"}},
    {"jsonrpc": "2.0", "result": {"isError": True, "content": [{"type": "text", "text": "failed"}]}},
])
def test_provider_error_receipt_survives_restart_without_becoming_success(provider_result):
    class ErrorProvider(Provider):
        async def invoke(self, tool, arguments, decision):
            self.calls += 1
            return provider_result

    gateway, provider = Gateway(), ErrorProvider()
    original = TestClient(create_app(gateway=gateway, agentcore=provider))
    first = original.post(
        "/agent/tools/call", json=CALL, headers=HEADERS,
    )
    assert first.status_code == 200
    assert first.json()["status"] == "confirmed" and first.json()["result_status"] == "error"
    assert first.json()["result"] == provider_result
    duplicate = original.post("/agent/tools/call", json=CALL, headers=HEADERS)
    assert duplicate.status_code == 200 and duplicate.json() == first.json()
    client = TestClient(create_app(gateway=gateway, agentcore=provider))
    receipt = client.get(operation_path(first.json()), params={"tool": CALL["tool"]}, headers=HEADERS)
    assert receipt.status_code == 200 and receipt.json() == first.json()
    assert receipt.json()["status"] == "confirmed" and receipt.json()["result_status"] == "error"
    duplicate = client.post("/agent/tools/call", json=CALL, headers=HEADERS)
    assert duplicate.status_code == 200 and duplicate.json() == first.json()
    assert provider.calls == 1


def test_foreign_owner_tenant_run_and_execution_handle_do_not_reveal_receipt():
    gateway, provider = Gateway(), Provider()
    client = TestClient(create_app(gateway=gateway, agentcore=provider))
    first = client.post("/agent/tools/call", json=CALL, headers=HEADERS).json()
    path = operation_path(first)
    original = {"owner": gateway.owner, "tenant": gateway.tenant, "run": gateway.run}
    for attribute, foreign in (("owner", "person-two"), ("tenant", "tenant-two"),
                               ("run", "run-two")):
        setattr(gateway, attribute, foreign)
        assert client.get(path, params={"tool": CALL["tool"]}, headers=HEADERS).status_code == 404
        assert client.get(path + "/artifacts/result", params={"tool": CALL["tool"]},
                          headers=HEADERS).status_code == 404
        setattr(gateway, attribute, original[attribute])
    wrong = path.replace(first["execution_id"], "00000000-0000-4000-8000-000000000088")
    assert client.get(wrong, params={"tool": CALL["tool"]}, headers=HEADERS).status_code == 404
    assert client.get(path, params={"tool": "other___tool"}, headers=HEADERS).status_code == 404
    gateway.denied = True
    assert client.get(path, params={"tool": CALL["tool"]}, headers=HEADERS).status_code == 403
    assert provider.calls == 1


def test_unknown_receipt_is_pollable_without_invented_result_or_artifact():
    class TimeoutProvider(Provider):
        async def invoke(self, tool, arguments, decision):
            self.calls += 1
            raise RuntimeError("provider response lost")

    gateway, provider = Gateway(), TimeoutProvider()
    first = TestClient(create_app(gateway=gateway, agentcore=provider)).post(
        "/agent/tools/call", json=CALL, headers=HEADERS,
    )
    assert first.status_code == 202 and first.json()["status"] == "unknown"
    client = TestClient(create_app(gateway=gateway, agentcore=provider))
    path = operation_path(first.json())
    assert client.get(path, params={"tool": CALL["tool"]}, headers=HEADERS).json() == first.json()
    assert client.get(path + "/artifacts/result", params={"tool": CALL["tool"]}, headers=HEADERS).status_code == 404
    assert provider.calls == 1


def test_refreshed_owner_closes_after_policy_revocation_without_provider_replay():
    gateway, provider = Gateway(), Provider()
    client = TestClient(create_app(gateway=gateway, agentcore=provider))
    original = client.post("/agent/tools/call", json=CALL, headers=HEADERS).json()
    path = f'/agent/tools/executions/{original["execution_id"]}/close'
    gateway.policy_revoked = True
    refreshed = {"Authorization": "Bearer refreshed"}
    first = client.post(path, params={"tool": CALL["tool"]}, headers=refreshed)
    second = TestClient(create_app(gateway=gateway, agentcore=provider)).post(
        path, params={"tool": CALL["tool"]}, headers=refreshed,
    )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {
        "execution_id": original["execution_id"], "state": "closed", "cleanup_status": "confirmed",
    }
    assert client.get(operation_path(original), params={"tool": CALL["tool"]}, headers=refreshed).json() == original
    gateway.policy_revoked = False
    later = client.post("/agent/tools/call", json={**CALL, "execution_id": original["execution_id"],
        "request_id": "00000000-0000-4000-8000-000000000098"}, headers=refreshed)
    assert later.status_code == 403 and provider.calls == 1
    gateway.owner = "person-two"
    assert client.post(path, params={"tool": CALL["tool"]}, headers=refreshed).status_code == 404
    assert provider.calls == 1


def test_search_close_preserves_unknown_sibling_and_original_artifact():
    gateway, provider = Gateway(), Provider()
    client = TestClient(create_app(gateway=gateway, agentcore=provider))
    first = client.post("/agent/tools/call", json=CALL, headers=HEADERS).json()
    decision = Decision.model_validate({"schema_version": "1.0", "allowed": True,
        "receipt_id": "original-gateway-receipt", "principal": {"user_id": gateway.owner,
            "root_human_id": gateway.owner, "tenant_id": gateway.tenant, "team_id": "team-one"}})
    store = ExecutionStore()
    sibling, _, _ = store.claim(UUID(first["execution_id"]),
        UUID("00000000-0000-4000-8000-000000000088"), CALL["tool"], {"query": "second"}, decision)
    store.dispatched(sibling)
    path = f'/agent/tools/executions/{first["execution_id"]}/close'
    pending = client.post(path, params={"tool": CALL["tool"]}, headers=HEADERS)
    assert pending.status_code == 202 and pending.json()["cleanup_status"] == "unknown"
    assert TestClient(create_app(gateway=gateway, agentcore=provider)).post(
        path, params={"tool": CALL["tool"]}, headers=HEADERS).status_code == 202
    original = operation_path(first)
    assert client.get(original, params={"tool": CALL["tool"]}, headers=HEADERS).json() == first
    assert client.get(original + "/artifacts/result", params={"tool": CALL["tool"]},
                      headers=HEADERS).status_code == 200
    assert provider.calls == 1


def test_close_requires_verified_identity_and_original_run_binding():
    gateway, provider = Gateway(), Provider()
    gateway.run = "run-one"
    client = TestClient(create_app(gateway=gateway, agentcore=provider))
    original = client.post("/agent/tools/call", json=CALL, headers=HEADERS).json()
    path = f'/agent/tools/executions/{original["execution_id"]}/close'
    gateway.run = "run-two"
    assert client.post(path, params={"tool": CALL["tool"]}, headers=HEADERS).status_code == 404
    gateway.run = "run-one"
    assert client.post(path, params={"tool": CALL["tool"]}).status_code == 403
    response = client.post(path, params={"tool": CALL["tool"]}, headers=HEADERS)
    assert response.status_code == 200 and response.json()["cleanup_status"] == "confirmed"
    assert provider.calls == 1


def test_closed_execution_keeps_receipt_and_invalid_ranges_do_not_disclose_bytes():
    gateway, provider = Gateway(), Provider()
    client = TestClient(create_app(gateway=gateway, agentcore=provider))
    first = client.post("/agent/tools/call", json=CALL, headers=HEADERS).json()
    boto3.client("dynamodb", region_name="us-east-1").update_item(
        TableName="adp-tools-dev-executions", Key={"execution_id": {"S": first["execution_id"]}},
        UpdateExpression="SET #state = :closed", ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={":closed": {"S": "closed"}},
    )
    path = operation_path(first)
    assert client.get(path, params={"tool": CALL["tool"]}, headers=HEADERS).json() == first
    artifact = path + "/artifacts/result"
    for offset, length, status in ((-1, 10, 422), (0, 8193, 422), (100000, 10, 416)):
        response = client.get(artifact, params={"tool": CALL["tool"], "offset": offset, "length": length},
                              headers=HEADERS)
        assert response.status_code == status
        assert "data_base64" not in response.json()
    assert provider.calls == 1 and str(UUID(first["execution_id"])) == first["execution_id"]


@pytest.mark.parametrize("run", [None, "run-one"])
def test_revoked_owner_recovers_after_restart_without_weakening_identity_fences(run):
    gateway, provider = Gateway(), Provider()
    gateway.run = run
    client = TestClient(create_app(gateway=gateway, agentcore=provider))
    first = client.post("/agent/tools/call", json=CALL, headers=HEADERS).json()
    operation = operation_path(first)
    close = f'/agent/tools/executions/{first["execution_id"]}/close'
    artifact = operation + "/artifacts/result"
    gateway.policy_revoked = True
    refreshed = {"Authorization": "Bearer refreshed"}
    additional = client.post("/agent/tools/call", json={**CALL,
        "execution_id": first["execution_id"], "request_id": "00000000-0000-4000-8000-000000000098"},
        headers=refreshed)
    assert additional.status_code == 403 and provider.calls == 1

    restarted = TestClient(create_app(gateway=gateway, agentcore=provider))
    original = {"owner": gateway.owner, "tenant": gateway.tenant, "run": gateway.run}
    for attribute, foreign in (("owner", "person-two"), ("tenant", "tenant-two"),
                               ("run", "run-two")):
        setattr(gateway, attribute, foreign)
        for endpoint in (operation, artifact):
            response = restarted.get(endpoint, params={"tool": CALL["tool"]}, headers=refreshed)
            assert response.status_code == 404 and "data_base64" not in response.json()
        refused = restarted.post(close, params={"tool": CALL["tool"]}, headers=refreshed)
        assert refused.status_code == 404 and "result" not in refused.json()
        setattr(gateway, attribute, original[attribute])
    row = boto3.client("dynamodb", region_name="us-east-1").get_item(
        TableName="adp-tools-dev-executions", Key={"execution_id": {"S": first["execution_id"]}},
        ConsistentRead=True)["Item"]
    assert row["state"]["S"] == "active" and provider.calls == 1

    assert restarted.get(operation, params={"tool": CALL["tool"]}, headers=refreshed).json() == first
    owned_artifact = restarted.get(artifact, params={"tool": CALL["tool"]}, headers=refreshed)
    assert owned_artifact.status_code == 200
    assert owned_artifact.json()["sha256"] == hashlib.sha256(json.dumps(first["result"]).encode()).hexdigest()
    stopped = restarted.post(close, params={"tool": CALL["tool"]}, headers=refreshed)
    assert stopped.status_code == 200 and stopped.json()["cleanup_status"] == "confirmed"
    assert restarted.get(operation, params={"tool": CALL["tool"]}, headers=refreshed).json() == first
    assert provider.calls == 1


def test_revoked_owner_artifact_remains_hash_verified_and_bounded_after_cleanup():
    gateway, provider = Gateway(), Provider()
    client = TestClient(create_app(gateway=gateway, agentcore=provider))
    saved = client.post("/agent/tools/call", json=CALL, headers=HEADERS).json()
    artifact = operation_path(saved) + "/artifacts/result"
    gateway.policy_revoked = True
    refreshed = {"Authorization": "Bearer refreshed"}
    close = client.post(f'/agent/tools/executions/{saved["execution_id"]}/close',
                        params={"tool": CALL["tool"]}, headers=refreshed)
    assert close.status_code == 200 and close.json()["cleanup_status"] == "confirmed"
    restarted = TestClient(create_app(gateway=gateway, agentcore=provider))
    content = json.dumps(saved["result"]).encode("utf-8")
    chunks = []
    for offset in range(0, len(content), 8192):
        response = restarted.get(artifact, params={"tool": CALL["tool"], "offset": offset}, headers=refreshed)
        assert response.status_code == 200
        record = response.json()
        assert record["offset"] == offset and 0 < record["length"] <= 8192
        assert record["total_length"] == len(content)
        assert record["sha256"] == hashlib.sha256(content).hexdigest()
        chunks.append(base64.b64decode(record["data_base64"]))
    assert b"".join(chunks) == content and provider.calls == 1

    missing = restarted.get(artifact, params={"tool": CALL["tool"], "offset": len(content)},
                            headers=refreshed)
    assert missing.status_code == 416 and "data_base64" not in missing.json()
    gateway.tenant = "another-tenant"
    foreign = restarted.get(artifact, params={"tool": CALL["tool"]}, headers=refreshed)
    assert foreign.status_code == 404 and "data_base64" not in foreign.json()
    row = boto3.client("dynamodb", region_name="us-east-1").get_item(
        TableName="adp-tools-dev-executions", Key={"execution_id": {"S": saved["execution_id"]}},
        ConsistentRead=True)["Item"]
    assert row["owner_id"]["S"] == "person-one" and row["tenant_id"]["S"] == "tenant-one"
    assert row["state"]["S"] == "closed" and provider.calls == 1
