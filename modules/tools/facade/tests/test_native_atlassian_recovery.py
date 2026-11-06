import hashlib
import json
import time
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from tools_facade.executions import ExecutionStore
from tools_facade.server import AgentCoreClient, Decision, GatewayAuthorizer, create_app


class ReceiptAuthority(GatewayAuthorizer):
    def __init__(self):
        self.operation_id = str(uuid4())
        self.body = {"tool": "atlassian___create_jira_issue", "arguments": {"cloudId": "owned-site"},
                     "request_id": str(uuid4()), "execution_id": None}
        self.reply = {
            "schema_version": "1.0", "allowed": True, "receipt_id": "original-reservation",
            "principal": {"user_id": "owner", "root_human_id": "owner", "tenant_id": "tenant", "team_id": "team"},
            "run_id": str(uuid4()), "workload_id": "original-workload", "run_expires_at": int(time.time()) + 600,
            "connection_id": "original-connection",
            "atlassian_connection": {
                "id": "original-connection", "revision": "original-revision", "site_id": "owned-site"},
            "atlassian_approval": {
                "tool": self.body["tool"], "request_id": self.body["request_id"], "policy_revision": 1,
                "consent_source": "explicit", "payload_sha256": hashlib.sha256(
                    json.dumps(self.body["arguments"], sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            },
        }
        self.requests = []

    async def _request(self, payload, headers, **kwargs):
        self.requests.append(payload)
        assert payload == {"schema_version": "1.0", "action": "recover", "ttl_seconds": None, **self.body}
        return self.reply


class NoDispatch(AgentCoreClient):
    async def invoke(self, *args, **kwargs):
        raise AssertionError("Receipt recovery must never invoke a provider")


@pytest.fixture
def original(monkeypatch):
    monkeypatch.setenv("ADP_TOOL_ADAPTER_RELEASE", '{"backend_generation":"offline-generation"}')
    authority = ReceiptAuthority()
    store = ExecutionStore()
    key, operation, fresh = store.claim(None, UUID(authority.body["request_id"]), authority.body["tool"],
                                       authority.body["arguments"], Decision.model_validate(authority.reply))
    assert fresh
    store.dispatched(key)
    operation = store.confirmed(key, {"result": {"structuredContent": {"id": "generated-issue"}}})
    receipt = store.receipt(operation)
    authority.reply["native_receipt_read"] = {
        "tool": authority.body["tool"], "request_id": authority.body["request_id"],
        "execution_id": receipt["execution_id"], "original_operation_id": authority.operation_id,
    }
    return authority, receipt


def test_native_atlassian_recovers_stored_result_without_dispatch(original):
    authority, receipt = original
    response = TestClient(create_app(gateway=authority, agentcore=NoDispatch())).post(
        "/agent/tools/operations/recover", json=authority.body,
        headers={"Authorization": "Bearer offline", "x-adp-native-test-capability": "offline",
                 "x-adp-native-test-operation": authority.operation_id},
    )
    assert response.status_code == 200, response.text
    assert response.json() == receipt
    assert len(authority.requests) == 1


@pytest.mark.parametrize("changed", ["request_id", "original_operation_id", "execution_id", "tool", "owner", "run"])
def test_native_atlassian_recovery_rejects_foreign_binding(original, changed):
    authority, _ = original
    if changed == "owner":
        authority.reply["principal"].update(user_id="foreign", root_human_id="foreign")
    elif changed == "run":
        authority.reply["run_id"] = str(uuid4())
    else:
        authority.reply["native_receipt_read"][changed] = (
            "atlassian___get_jira_issue" if changed == "tool" else str(uuid4()))
    response = TestClient(create_app(gateway=authority, agentcore=NoDispatch())).post(
        "/agent/tools/operations/recover", json=authority.body,
        headers={"Authorization": "Bearer offline", "x-adp-native-test-capability": "offline",
                 "x-adp-native-test-operation": authority.operation_id},
    )
    assert response.status_code in {403, 404}
