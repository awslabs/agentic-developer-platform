"""SCN-8066-04: local five-helper recovery with independent synthetic readback."""

import base64
import hashlib
import importlib.util
import json
from pathlib import Path

import boto3
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from moto import mock_aws

from tests.e2e.tools import runner
from tests.e2e.tools.test_cleanup_ac01 import GATEWAY_IMAGE, IMAGE, IdleClient, SHA
from tools_facade.server import create_app

TABLE = "fixture-five-helper-executions"
TOOL = "websearch___search"
CLI = Path(__file__).resolve().parents[3] / "modules/gateway/cli"


class Gateway:
    revoked = False

    async def authorize(self, tool, arguments, headers):
        if self.revoked:
            raise HTTPException(403, "new work grant revoked")
        return await self.recover(tool, headers)

    async def recover(self, tool, headers):
        if headers.get("authorization") != "Bearer owner" or tool != TOOL:
            raise HTTPException(403, "tool authority refused")
        return {"schema_version": "1.0", "allowed": True, "receipt_id": "fixture-receipt",
                "principal": {"user_id": "fixture-owner", "root_human_id": "fixture-owner",
                              "tenant_id": "fixture-tenant", "team_id": "fixture-team"}}

    async def close(self, tool, headers):
        return await self.recover(tool, headers)


class Provider:
    def __init__(self):
        self.calls = 0

    async def invoke(self, tool, arguments, decision):
        assert tool == TOOL
        self.calls += 1
        return {"jsonrpc": "2.0", "result": {"content": [{"text": "π" * 5000}], "exit_code": 0}}


class Driver:
    isolated = True

    def __init__(self, provider, *, gateway=None, headers=None):
        self.provider = provider
        self.gateway = gateway or Gateway()
        self.headers = headers or {"Authorization": "Bearer owner"}
        self.client = TestClient(create_app(gateway=self.gateway, agentcore=provider))
        self.binding = {"identity": "fixture-owner", "tenant": "fixture-tenant", "run": "fixture-run"}
        self.lost_reply = True
        self.accepted = None
        self.shim = self.restart()

    def request(self, path, body=None, *, context=None):
        response = self.client.request("POST" if body is not None else "GET", path, json=body,
                                       headers=self.headers)
        if response.status_code >= 400:
            raise self.shim.GatewayHTTPError(response.status_code)
        if path == "/agent/tools/call" and self.lost_reply:
            self.lost_reply = False
            self.accepted = response.json()
            raise self.shim.ToolError("original reply lost after provider dispatch")
        return response.json()

    def restart(self):
        spec = importlib.util.spec_from_file_location("adp_mcp_five_helper", CLI / "adp-mcp.py")
        shim = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(shim)
        shim.gateway_context = lambda: {"binding": self.binding}
        shim.gateway_request = self.request
        self.shim = shim
        return shim

    def call(self, name, arguments):
        response = self.shim.handle({"method": "tools/call", "params": {"name": name, "arguments": arguments}})
        return response, json.loads(response["content"][0]["text"])

    def start_lost_reply(self, *, attempt_id):
        response, receipt = self.call(TOOL, {"query": "fixture"})
        if not response["isError"] or receipt.get("status") != "unknown" or not self.accepted:
            raise ValueError("the accepted original reply was not lost")
        self.reference = receipt["operation_ref"]
        return self.reference, self.accepted["execution_id"]

    def recover_original(self, *, attempt_id):
        self.restart()
        listed = self.call("adp___operations_list", {})[1]["operations"]
        reference = self.reference
        pending = self.call("adp___operation_status", {"operation_ref": reference})[1]
        recovered = self.call("adp___operation_retry", {"operation_ref": reference})[1]
        confirmed = self.call("adp___operation_status", {"operation_ref": reference})[1]
        data = bytearray()
        total = None
        digest = None
        while total is None or len(data) < total:
            response, packet = self.call("adp___artifact_read", {"operation_ref": reference,
                                                                   "offset": len(data), "length": 8192})
            if response.get("isError") or packet.get("offset") != len(data):
                raise ValueError("bounded artifact chunk unavailable")
            total, digest = packet["total_length"], packet["sha256"]
            data.extend(base64.b64decode(packet["data_base64"], validate=True))
            if len(data) > 262144:
                raise ValueError("artifact exceeded isolation bound")
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("artifact SHA256 mismatch")
        closed = self.call("adp___execution_close", {"operation_ref": reference})[1]
        self.restart()
        after = self.call("adp___operation_status", {"operation_ref": reference})[1]
        if confirmed.get("result") != after.get("result") or recovered.get("request_id") != reference:
            raise ValueError("original operation result changed after restart")
        return {"operation_ref": reference, "listed": len(listed) == 1 and listed[0]["operation_ref"] == reference,
                "pending_status": pending["status"], "recovered_status": confirmed["status"],
                "post_restart_status": after["status"], "close_status": closed.get("cleanup_status"),
                "sha256": digest, "length": len(data)}

    def cleanup_owned(self, *, attempt_id):
        if self.accepted:
            execution_id = self.accepted["execution_id"]
            response = self.client.post(f"/agent/tools/executions/{execution_id}/close",
                                        params={"tool": TOOL}, headers={"Authorization": "Bearer owner"})
            if response.status_code != 200:
                raise ValueError("isolated execution cleanup remains unconfirmed")


class Observer:
    def __init__(self, provider):
        self.provider = provider

    def readback(self, execution_id, *, reference):
        self.reference = reference
        client = boto3.client("dynamodb", region_name="us-east-1")
        execution = client.get_item(TableName=TABLE, Key={"execution_id": {"S": execution_id}},
                                    ConsistentRead=True)["Item"]
        key = f"OP#fixture-tenant#fixture-owner#{TOOL}#{reference}"
        operation = client.get_item(TableName=TABLE, Key={"execution_id": {"S": key}},
                                    ConsistentRead=True)["Item"]
        data = operation["result_json"]["S"].encode()
        return {"source": "independent-isolated-ledger-and-provider-readback",
                "dispatch_count": self.provider.calls,
                "execution_state": execution["state"]["S"],
                "cleanup_status": execution.get("cleanup_status", {}).get("S"),
                "cleanup_evidence": execution.get("cleanup_evidence", {}).get("S"),
                "accepted_operations": int(execution["accepted_operations"]["N"]),
                "unresolved_operations": int(execution["unresolved_operations"]["N"]),
                "sha256": hashlib.sha256(data).hexdigest(), "byte_length": len(data)}

    def verify_cleanup(self, *, execution_id):
        observed = self.readback(execution_id, reference=self.reference)
        return observed["execution_state"] == "closed" and observed["cleanup_status"] == "confirmed"


@pytest.fixture
def isolated_five_helpers(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ADP_TOOLS_EXECUTION_TABLE", TABLE)
    monkeypatch.setenv("BG_CONFIG_DIR", str(tmp_path))
    monkeypatch.syspath_prepend(str(CLI))
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "execution_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "execution_id", "AttributeType": "S"}])
        provider = Provider()
        driver = Driver(provider)
        observer = Observer(provider)
        yield driver, observer


def run_case(output, monkeypatch, driver=None, observer=None, *, live=False):
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    return runner.evaluate("SCN-8066-04", output, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
        command=["adp", "mcp"], client_factory=IdleClient,
        cleanup_driver_factory=(lambda *_: driver) if driver else None,
        cleanup_observer=observer, live=live)


@pytest.mark.parametrize("lifecycle", ["expired", "closing", "closed"])
@pytest.mark.parametrize("revoked", [False, True])
def test_lost_initial_reply_recovers_original_after_deadline_and_cleanup(
        isolated_five_helpers, monkeypatch, lifecycle, revoked):
    from uuid import uuid4
    from tools_facade import executions

    driver, observer = isolated_five_helpers
    now = [1000]
    original_store = executions.ExecutionStore

    class ClockedStore(original_store):
        def __init__(self):
            super().__init__(clock=lambda: now[0])

    monkeypatch.setattr(executions, "ExecutionStore", ClockedStore)
    driver.client = TestClient(create_app(gateway=driver.gateway, agentcore=driver.provider))
    reference, identifier = driver.start_lost_reply(attempt_id=str(uuid4()))
    assert not driver.shim.load_operation(reference).get("execution_id")
    before = observer.readback(identifier, reference=reference)
    store = ClockedStore()
    now[0] = 1900
    db = boto3.client("dynamodb", region_name="us-east-1")
    key = {"execution_id": {"S": identifier}}
    row = db.get_item(TableName=TABLE, Key=key, ConsistentRead=True)["Item"]
    if lifecycle != "expired":
        row = store.resume_cleanup(row)
        assert row["state"]["S"] == "closing"
    if lifecycle == "closed":
        now[0] += 60
        row = store.resume_cleanup(row)
        assert store.confirm_due_search(row)
    driver.gateway.revoked = revoked
    result = driver.recover_original(attempt_id=str(uuid4()))
    assert result["recovered_status"] == result["post_restart_status"] == "confirmed"
    assert result["close_status"] == "confirmed" and result["sha256"] == before["sha256"]
    assert result["length"] == before["byte_length"] and driver.provider.calls == 1
    assert driver.shim.load_operation(reference)["execution_id"] == identifier


@pytest.mark.parametrize("status", [403, 404, 409, 422, 503, None])
def test_failed_recovery_read_preserves_retryable_unknown_original(isolated_five_helpers, status):
    from uuid import uuid4

    driver, _ = isolated_five_helpers
    reference, identifier = driver.start_lost_reply(attempt_id=str(uuid4()))
    shim = driver.restart()
    request = shim.gateway_request

    def unavailable(*args, **kwargs):
        assert args[0] == "/agent/tools/operations/recover"
        raise shim.GatewayHTTPError(status) if status else shim.ToolError("connection lost")

    shim.gateway_request = unavailable
    for _ in range(2):
        result = driver.call("adp___operation_retry", {"operation_ref": reference})[1]
        assert result["status"] == "unknown"
        assert shim.load_operation(reference)["status"] == "unknown"
    shim.gateway_request = request
    result = driver.call("adp___operation_retry", {"operation_ref": reference})[1]
    assert result["status"] == "confirmed" and result["execution_id"] == identifier
    assert driver.provider.calls == 1


def test_five_helpers_qualified_local_scenario_checks_independent_original(tmp_path, monkeypatch,
                                                                                isolated_five_helpers):
    driver, observer = isolated_five_helpers
    assert run_case(tmp_path, monkeypatch, driver, observer)
    report = json.loads((tmp_path / "evidence.json").read_text())
    row = report["scenarios"][0]
    assert row["tags"] == ["@scenario SCN-8066-04", "@story 8066", "@acceptance AC-04"]
    assert row["cell_id"] == "cli:search" and row["status"] == "pass"
    assert row["evidence_scope"] == "repository-integration" and report["live_attestation"] is False
    assert row["observations"]["after"]["sha256"] == row["observations"]["artifact_sha256"]
    assert driver.provider.calls == 1


def test_search_ac03_reuses_original_lost_reply_and_independent_five_helper_oracle(
        tmp_path, monkeypatch, isolated_five_helpers):
    driver, observer = isolated_five_helpers
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    assert runner.evaluate("SCN-8069-03", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
                           command=["adp", "mcp"], client_factory=IdleClient,
                           cleanup_driver_factory=lambda case, client, tool: driver,
                           cleanup_observer=observer)
    row = json.loads((tmp_path / "evidence.json").read_text())["scenarios"][0]
    assert row["tags"] == ["@scenario SCN-8069-03", "@story 8069", "@acceptance AC-03"]
    assert row["status"] == "pass" and row["evidence_scope"] == "repository-integration"
    assert row["observations"]["after"]["sha256"] == row["observations"]["artifact_sha256"]
    assert row["observations"]["after"]["cleanup_status"] == "confirmed"
    assert driver.provider.calls == 1


def test_search_ac03_does_not_claim_live_without_dedicated_fixture(tmp_path, monkeypatch,
                                                                     isolated_five_helpers):
    driver, observer = isolated_five_helpers
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    assert not runner.evaluate("SCN-8069-03", tmp_path, "https://example.invalid", SHA,
                               GATEWAY_IMAGE, IMAGE, command=["adp", "mcp"], client_factory=IdleClient,
                               cleanup_driver_factory=lambda *_: driver, cleanup_observer=observer, live=True)
    row = json.loads((tmp_path / "evidence.json").read_text())["scenarios"][0]
    assert row["status"] == "blocked" and driver.provider.calls == 0


def test_five_helpers_scenario_needs_oracle_and_live_remains_blocked(tmp_path, monkeypatch, isolated_five_helpers):
    driver, observer = isolated_five_helpers
    assert not run_case(tmp_path, monkeypatch, driver, None)
    assert json.loads((tmp_path / "evidence.json").read_text())["scenarios"][0]["status"] == "not-run"
    assert not run_case(tmp_path, monkeypatch, driver, observer, live=True)
    row = json.loads((tmp_path / "evidence.json").read_text())["scenarios"][0]
    assert row["status"] == "blocked" and driver.provider.calls == 0


def test_five_helpers_rejects_false_independent_dispatch_count(tmp_path, monkeypatch, isolated_five_helpers):
    driver, observer = isolated_five_helpers
    original = observer.readback

    def false_readback(execution_id, *, reference):
        evidence = original(execution_id, reference=reference)
        return {**evidence, "dispatch_count": 2}

    observer.readback = false_readback
    assert not run_case(tmp_path, monkeypatch, driver, observer)
    row = json.loads((tmp_path / "evidence.json").read_text())["scenarios"][0]
    assert row["status"] == "blocked" and driver.provider.calls == 1
