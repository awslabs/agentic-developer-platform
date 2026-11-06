"""SCN-8066-03: isolated revoked-owner recovery with independent artifact readback."""

import base64
import hashlib
import json
import time
from uuid import uuid4

import boto3
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from moto import mock_aws

from tests.e2e.tools import runner
from tests.e2e.tools.test_cleanup_ac01 import GATEWAY_IMAGE, IMAGE, IdleClient, SHA
from tools_facade.server import create_app

TABLE = "fixture-owned-artifacts"
TOOL = "websearch___search"
OWNER = {"Authorization": "Bearer owner"}
REFRESHED = {"Authorization": "Bearer refreshed"}


class Gateway:
    def __init__(self):
        self.revoked = False

    def decision(self, headers):
        identity = headers.get("authorization", "")
        if identity not in {"Bearer owner", "Bearer refreshed", "Bearer foreign-user",
                            "Bearer foreign-tenant", "Bearer foreign-run"}:
            raise HTTPException(403, "tool authority refused")
        return {"schema_version": "1.0", "allowed": True, "receipt_id": "isolated-receipt",
                "principal": {"user_id": "foreign-user" if identity == "Bearer foreign-user" else "fixture-owner",
                              "root_human_id": "foreign-user" if identity == "Bearer foreign-user" else "fixture-owner",
                              "tenant_id": "foreign-tenant" if identity == "Bearer foreign-tenant" else "fixture-tenant",
                              "team_id": "fixture-team"},
                "run_id": "foreign-run" if identity == "Bearer foreign-run" else "fixture-run",
                "workload_id": "fixture-worker", "run_expires_at": int(time.time()) + 900}

    async def authorize(self, tool, arguments, headers):
        if self.revoked:
            raise HTTPException(403, "tool grant revoked")
        return self.decision(headers)

    async def recover(self, tool, headers):
        return self.decision(headers)

    async def close(self, tool, headers):
        return self.decision(headers)


class Provider:
    def __init__(self):
        self.calls = 0

    async def invoke(self, tool, arguments, decision):
        assert tool == TOOL
        self.calls += 1
        return {"jsonrpc": "2.0", "result": {"content": [{"text": "π" * 5000}], "exit_code": 0}}


class Driver:
    isolated = True

    def __init__(self, gateway, provider):
        self.gateway, self.provider = gateway, provider
        self.receipts = {}

    def client(self):
        return TestClient(create_app(gateway=self.gateway, agentcore=self.provider))

    def paths(self, receipt):
        root = f'/agent/tools/executions/{receipt["execution_id"]}'
        operation = f'{root}/operations/{receipt["request_id"]}'
        return operation, operation + "/artifacts/result", root + "/close"

    def prepare(self, *, attempt_id):
        response = self.client().post("/agent/tools/call", json={"tool": TOOL, "arguments": {"query": "fixture"},
            "request_id": str(uuid4())}, headers=OWNER)
        if response.status_code != 200 or response.json().get("status") != "confirmed":
            raise ValueError("initial provider dispatch not confirmed")
        self.receipts[attempt_id] = response.json()
        return response.json()

    def revoke(self, *, attempt_id):
        self.gateway.revoked = True
        refused = self.client().post("/agent/tools/call", json={"tool": TOOL, "arguments": {"query": "new"},
            "request_id": str(uuid4()), "execution_id": self.receipts[attempt_id]["execution_id"]},
            headers=REFRESHED)
        if refused.status_code != 403:
            raise ValueError("revoked tool permission still permits new work")

    def foreign_access(self, *, attempt_id):
        operation, artifact, close = self.paths(self.receipts[attempt_id])
        client = self.client()
        denied = {}
        for identity in ("user", "tenant", "run"):
            headers = {"Authorization": "Bearer foreign-" + identity}
            receipt = client.get(operation, params={"tool": TOOL}, headers=headers)
            chunk = client.get(artifact, params={"tool": TOOL}, headers=headers)
            stopped = client.post(close, params={"tool": TOOL}, headers=headers)
            if "data_base64" in chunk.json() or "result" in receipt.json():
                raise ValueError("foreign identity received owned artifact")
            denied[identity] = {"operation": receipt.status_code, "artifact": chunk.status_code,
                                "close": stopped.status_code}
        return denied

    def read_owned(self, *, attempt_id):
        operation, artifact, _ = self.paths(self.receipts[attempt_id])
        client = self.client()
        receipt = client.get(operation, params={"tool": TOOL}, headers=REFRESHED)
        if receipt.status_code != 200:
            raise ValueError("owner cannot recover receipt")
        first = client.get(artifact, params={"tool": TOOL, "offset": 0}, headers=REFRESHED)
        if first.status_code != 200 or not 8192 < first.json().get("total_length", 0) <= 262144:
            raise ValueError("bounded owned artifact unavailable")
        total = first.json()["total_length"]
        data = bytearray()
        for offset in range(0, total, 8192):
            response = first if offset == 0 else client.get(
                artifact, params={"tool": TOOL, "offset": offset}, headers=REFRESHED)
            record = response.json()
            if (response.status_code != 200 or record.get("offset") != offset or
                    record.get("total_length") != total or
                    not 0 < record.get("length", 0) <= 8192):
                raise ValueError("artifact range unavailable")
            chunk = base64.b64decode(record["data_base64"], validate=True)
            if len(chunk) != record["length"]:
                raise ValueError("artifact chunk length invalid")
            data.extend(chunk)
        return receipt.json(), bytes(data)

    def close_owned(self, *, attempt_id):
        _, _, close = self.paths(self.receipts[attempt_id])
        response = self.client().post(close, params={"tool": TOOL}, headers=REFRESHED)
        if response.status_code != 200:
            raise ValueError("owner cleanup remains unproved")
        return response.json()

    def cleanup_owned(self, *, attempt_id):
        if attempt_id in self.receipts:
            self.close_owned(attempt_id=attempt_id)


class Observer:
    def __init__(self, provider, driver):
        self.provider, self.driver = provider, driver

    def readback(self, execution_id, *, attempt_id):
        receipt = self.driver.receipts[attempt_id]
        assert receipt["execution_id"] == execution_id
        client = boto3.client("dynamodb", region_name="us-east-1")
        execution = client.get_item(TableName=TABLE, Key={"execution_id": {"S": execution_id}},
                                    ConsistentRead=True)["Item"]
        key = f'OP#fixture-tenant#fixture-owner#{TOOL}#{receipt["request_id"]}'
        operation = client.get_item(TableName=TABLE, Key={"execution_id": {"S": key}},
                                    ConsistentRead=True)["Item"]
        data = operation["result_json"]["S"].encode("utf-8")
        return {"source": "independent-isolated-ledger-and-provider-readback",
                "state": execution["state"]["S"], "cleanup_status": execution.get("cleanup_status", {}).get("S"),
                "cleanup_evidence": execution.get("cleanup_evidence", {}).get("S"),
                "cleanup_partition": execution.get("cleanup_partition", {}).get("S"),
                "tenant": execution["tenant_id"]["S"], "owner": execution["owner_id"]["S"],
                "binding": execution["binding"]["S"], "provider_calls": self.provider.calls,
                "sha256": hashlib.sha256(data).hexdigest(), "total_length": len(data)}

    def verify_cleanup(self, *, attempt_id):
        observed = self.readback(self.driver.receipts[attempt_id]["execution_id"], attempt_id=attempt_id)
        return (observed["state"] == "closed" and observed["cleanup_status"] == "confirmed" and
                observed["cleanup_evidence"] == "all_operations_settled" and
                observed["cleanup_partition"] is None and observed["provider_calls"] == 1)


@pytest.fixture
def isolated_owner(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ADP_TOOLS_EXECUTION_TABLE", TABLE)
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "execution_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "execution_id", "AttributeType": "S"},
                                  {"AttributeName": "cleanup_partition", "AttributeType": "S"},
                                  {"AttributeName": "next_cleanup_at", "AttributeType": "N"}],
            GlobalSecondaryIndexes=[{"IndexName": "cleanup-due",
                                     "KeySchema": [{"AttributeName": "cleanup_partition", "KeyType": "HASH"},
                                                   {"AttributeName": "next_cleanup_at", "KeyType": "RANGE"}],
                                     "Projection": {"ProjectionType": "ALL"}}])
        provider = Provider()
        driver = Driver(Gateway(), provider)
        yield driver, Observer(provider, driver)


def run_case(output, monkeypatch, driver=None, observer=None, *, live=False):
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    return runner.evaluate("SCN-8066-03", output, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
        command=["adp", "mcp"], client_factory=IdleClient,
        cleanup_driver_factory=(lambda *_: driver) if driver else None,
        cleanup_observer=observer, live=live)


def test_revoked_owner_artifact_scenario_verifies_independent_hash_and_binding(tmp_path, monkeypatch, isolated_owner):
    driver, observer = isolated_owner
    monkeypatch.setenv("GITHUB_RUN_ID", "local-cleanup-ac03")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    assert run_case(tmp_path, monkeypatch, driver, observer)
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert (report["github_run_id"], report["github_run_attempt"]) == ("local-cleanup-ac03", "1")
    row = report["scenarios"][0]
    assert row["tags"] == ["@scenario SCN-8066-03", "@story 8066", "@acceptance AC-03"]
    assert row["status"] == "pass" and row["cleanup"] == {"status": "confirmed"}
    assert row["observations"]["after"]["sha256"] == row["observations"]["artifact_sha256"]
    assert row["observations"]["artifact_length"] > 8192


def test_revoked_owner_scenario_requires_oracle_and_refuses_live(tmp_path, monkeypatch, isolated_owner):
    driver, observer = isolated_owner
    assert not run_case(tmp_path, monkeypatch, driver, None)
    assert json.loads((tmp_path / "evidence.json").read_text())["scenarios"][0]["status"] == "not-run"
    assert not run_case(tmp_path, monkeypatch, driver, observer, live=True)
    assert json.loads((tmp_path / "evidence.json").read_text())["scenarios"][0]["status"] == "blocked"
    assert driver.receipts == {}


def test_revoked_owner_scenario_rejects_false_ledger_hash(tmp_path, monkeypatch, isolated_owner):
    driver, observer = isolated_owner
    actual = observer.readback

    def inaccurate(execution_id, *, attempt_id):
        readback = actual(execution_id, attempt_id=attempt_id)
        return {**readback, "sha256": "0" * 64}

    monkeypatch.setattr(observer, "readback", inaccurate)
    assert not run_case(tmp_path, monkeypatch, driver, observer)
    row = json.loads((tmp_path / "evidence.json").read_text())["scenarios"][0]
    assert row["status"] == "blocked" and row["reason"] == "ValueError"


@pytest.mark.parametrize("foreign", ["user", "tenant", "run"])
def test_foreign_identity_cannot_read_or_close_revoked_owner_before_or_after_cleanup(isolated_owner, foreign):
    driver, observer = isolated_owner
    attempt_id = str(uuid4())
    saved = driver.prepare(attempt_id=attempt_id)
    driver.revoke(attempt_id=attempt_id)
    client = driver.client()
    operation, artifact, close = driver.paths(saved)
    foreign_headers = {"Authorization": "Bearer foreign-" + foreign}
    for expected_state in ("active", "closed"):
        if expected_state == "closed":
            assert driver.close_owned(attempt_id=attempt_id)["cleanup_status"] == "confirmed"
        for endpoint in (operation, artifact):
            refused = client.get(endpoint, params={"tool": TOOL}, headers=foreign_headers)
            assert refused.status_code == 404
            assert "result" not in refused.json() and "data_base64" not in refused.json()
            assert saved["receipt_id"] not in refused.text
        stopped = client.post(close, params={"tool": TOOL}, headers=foreign_headers)
        assert stopped.status_code == 404 and saved["receipt_id"] not in stopped.text
        snapshot = observer.readback(saved["execution_id"], attempt_id=attempt_id)
        assert snapshot["state"] == expected_state
        assert (snapshot["tenant"], snapshot["owner"], snapshot["binding"]) == (
            "fixture-tenant", "fixture-owner", "run:fixture-run:fixture-worker")
        assert snapshot["provider_calls"] == 1
    receipt, content = driver.read_owned(attempt_id=attempt_id)
    assert receipt == saved and hashlib.sha256(content).hexdigest() == snapshot["sha256"]


def test_revoked_owner_scenario_refuses_forged_foreign_success(tmp_path, monkeypatch, isolated_owner):
    driver, observer = isolated_owner
    actual = driver.foreign_access

    def forged(*, attempt_id):
        results = actual(attempt_id=attempt_id)
        return {**results, "run": {**results["run"], "artifact": 200}}

    monkeypatch.setattr(driver, "foreign_access", forged)
    assert not run_case(tmp_path, monkeypatch, driver, observer)
    row = json.loads((tmp_path / "evidence.json").read_text())["scenarios"][0]
    assert row["status"] == "blocked" and row["reason"] == "ValueError"
    assert row["cleanup"] == {"status": "confirmed"}


def test_revoked_owner_invalid_ranges_never_return_artifact_bytes_or_prevent_cleanup(isolated_owner):
    driver, observer = isolated_owner
    attempt_id = str(uuid4())
    saved = driver.prepare(attempt_id=attempt_id)
    driver.revoke(attempt_id=attempt_id)
    _, artifact, _ = driver.paths(saved)
    client = driver.client()
    for expected_state in ("active", "closed"):
        if expected_state == "closed":
            assert driver.close_owned(attempt_id=attempt_id)["cleanup_status"] == "confirmed"
        observed = observer.readback(saved["execution_id"], attempt_id=attempt_id)
        total = observed["total_length"]
        for offset, length, expected_status in ((-1, 1, 422), (0, 0, 422), (0, -1, 422),
                                                (0, 8193, 422), (total, 1, 416),
                                                (total + 1, 1, 416)):
            response = client.get(artifact, params={"tool": TOOL, "offset": offset, "length": length},
                                  headers=REFRESHED)
            assert response.status_code == expected_status
            assert "data_base64" not in response.json() and saved["receipt_id"] not in response.text
        last_byte = client.get(artifact, params={"tool": TOOL, "offset": total - 1, "length": 1},
                               headers=REFRESHED)
        assert last_byte.status_code == 200
        assert last_byte.json()["length"] == 1 and last_byte.json()["offset"] == total - 1
        assert last_byte.json()["sha256"] == observed["sha256"]
        assert len(base64.b64decode(last_byte.json()["data_base64"])) == 1
        assert observed["state"] == expected_state and observed["provider_calls"] == 1
        assert (observed["tenant"], observed["owner"], observed["binding"]) == (
            "fixture-tenant", "fixture-owner", "run:fixture-run:fixture-worker")
    assert observed["cleanup_evidence"] == "all_operations_settled"
    receipt, content = driver.read_owned(attempt_id=attempt_id)
    assert receipt == saved and hashlib.sha256(content).hexdigest() == observed["sha256"]
