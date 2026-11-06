"""SCN-8066-01 exercises facade close with isolated, independent readback."""

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from tests.e2e.tools import runner
from tools_facade.executions import ExecutionStore
from tools_facade.server import AgentCoreClient, create_app

SHA = "a" * 40
IMAGE = "example.invalid/adp-tools-facade@sha256:" + "b" * 64
GATEWAY_IMAGE = "example.invalid/adp-gateway@sha256:" + "c" * 64
TOOLS = {"search": "websearch___search", "interpreter": "code_interpreter___start", "browser": "browser___start"}


class IdleClient:
    version = "local-fixture"

    def __init__(self, command):
        self.command = command

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class Authority:
    async def close(self, tool, headers):
        return {"schema_version": "1.0", "allowed": True, "receipt_id": "fixture-receipt",
                "principal": {"tenant_id": "fixture-tenant", "team_id": "fixture-team",
                              "user_id": "fixture-owner", "root_human_id": "fixture-owner"}}


class Provider(AgentCoreClient):
    def __init__(self):
        self.states = {}
        self.dispatches = {}
        self.stops = {}

    async def invoke(self, tool, arguments, decision):
        assert tool in {"code_interpreter___execution_close", "browser___execution_close"}
        execution_id = arguments["execution_id"]
        assert self.states[execution_id] == "READY"
        self.stops[execution_id] += 1
        self.states[execution_id] = "TERMINATED"
        return {"jsonrpc": "2.0", "result": {"result": {"execution_id": execution_id,
                "status": "confirmed", "termination_confirmed": True, "provider_status": "TERMINATED"}}}


class Driver:
    isolated = True

    def __init__(self, table, provider):
        self.table, self.provider = table, provider
        self.attempts = {}

    def close(self, tool, *, attempt_id):
        root = TOOLS[tool]
        decision = SimpleNamespace(principal=SimpleNamespace(tenant_id="fixture-tenant",
            team_id="fixture-team", user_id="fixture-owner"), run_id=None, workload_id=None,
            run_expires_at=None, receipt_id="fixture-receipt")
        store = ExecutionStore(table=self.table)
        operation_key, operation, _ = store.claim(None, uuid4(), root,
            {"query": "fixture"} if tool == "search" else {}, decision)
        execution_id = operation["operation_execution_id"]["S"]
        self.attempts[attempt_id] = (tool, execution_id)
        self.provider.states[execution_id] = "not-required" if tool == "search" else "READY"
        self.provider.dispatches[execution_id] = 1
        self.provider.stops[execution_id] = 0
        store.dispatched(operation_key)
        if tool == "search":
            store.confirmed(operation_key, {"jsonrpc": "2.0", "result": {"result": {"status": "confirmed"}}})
        client = TestClient(create_app(gateway=Authority(), agentcore=self.provider))
        response = client.post(f"/agent/tools/executions/{execution_id}/close",
                               params={"tool": root}, headers={"Authorization": "Bearer fixture"})
        return {"execution_id": execution_id, "status_code": response.status_code,
                "cleanup_status": response.json()["cleanup_status"]}

    def cleanup_owned(self, *, attempt_id):
        tool, execution_id = self.attempts[attempt_id]
        if self.provider.states[execution_id] == "READY":
            client = TestClient(create_app(gateway=Authority(), agentcore=self.provider))
            client.post(f"/agent/tools/executions/{execution_id}/close",
                        params={"tool": TOOLS[tool]}, headers={"Authorization": "Bearer fixture"})


class Observer:
    def __init__(self, table, provider, driver):
        self.table, self.provider, self.driver = table, provider, driver

    def readback(self, tool, execution_id, *, attempt_id):
        assert self.driver.attempts[attempt_id] == (tool, execution_id)
        row = boto3.client("dynamodb", region_name="us-east-1").get_item(
            TableName=self.table, Key={"execution_id": {"S": execution_id}}, ConsistentRead=True)["Item"]
        return {"source": "isolated-provider-and-ledger-readback",
                "execution_state": row["state"]["S"], "cleanup_status": row["cleanup_status"]["S"],
                "provider_state": self.provider.states[execution_id],
                "dispatch_count": self.provider.dispatches[execution_id],
                "stop_count": self.provider.stops[execution_id],
                "accepted_operations": int(row["accepted_operations"]["N"]),
                "unresolved_operations": int(row["unresolved_operations"]["N"])}

    def verify_cleanup(self, tool, *, attempt_id):
        _, execution_id = self.driver.attempts[attempt_id]
        observed = self.readback(tool, execution_id, attempt_id=attempt_id)
        return (observed["execution_state"] == "closed" and
                observed["provider_state"] == ("not-required" if tool == "search" else "TERMINATED"))


@pytest.fixture
def isolated_cleanup(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ADP_TOOLS_EXECUTION_TABLE", "fixture-executions")
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName="fixture-executions", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "execution_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "execution_id", "AttributeType": "S"}])
        provider = Provider()
        driver = Driver("fixture-executions", provider)
        yield driver, Observer("fixture-executions", provider, driver)


def run_case(output, monkeypatch, driver=None, observer=None, *, live=False):
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    return runner.evaluate("SCN-8066-01", output, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
        command=["adp", "mcp"], client_factory=IdleClient,
        cleanup_driver_factory=(lambda *_: driver) if driver else None,
        cleanup_observer=observer, live=live)


def test_qualified_close_scenario_independently_checks_three_owned_executions(tmp_path, monkeypatch,
                                                                                 isolated_cleanup):
    driver, observer = isolated_cleanup
    monkeypatch.setenv("GITHUB_RUN_ID", "local-cleanup-ac01")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    assert run_case(tmp_path, monkeypatch, driver, observer)
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert (report["github_run_id"], report["github_run_attempt"]) == ("local-cleanup-ac01", "1")
    assert [row["tool"] for row in report["scenarios"]] == ["search", "interpreter", "browser"]
    for row in report["scenarios"]:
        assert row["tags"] == ["@scenario SCN-8066-01", "@story 8066", "@acceptance AC-01"]
        assert row["status"] == "pass" and row["attempt_id"]
        assert UUID(row["execution_id"]) and row["cleanup"] == {"status": "confirmed"}
        assert row["observations"]["dispatch_count"] == 1
    assert len(driver.attempts) == 3


def test_close_scenario_blocks_without_oracle_and_in_live_mode(tmp_path, monkeypatch, isolated_cleanup):
    driver, observer = isolated_cleanup
    assert not run_case(tmp_path, monkeypatch, driver, None)
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert all(row["status"] == "not-run" for row in rows) and not driver.attempts
    assert not run_case(tmp_path, monkeypatch, driver, observer, live=True)
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert all(row["status"] == "blocked" for row in rows) and not driver.attempts


def test_close_scenario_rejects_false_provider_termination(tmp_path, monkeypatch, isolated_cleanup):
    driver, observer = isolated_cleanup
    actual = observer.readback

    def false_readback(tool, execution_id, *, attempt_id):
        found = actual(tool, execution_id, attempt_id=attempt_id)
        return {**found, "provider_state": "READY"} if tool == "browser" else found

    monkeypatch.setattr(observer, "readback", false_readback)
    assert not run_case(tmp_path, monkeypatch, driver, observer)
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert [row["status"] for row in rows] == ["pass", "pass", "blocked"]
    assert rows[2]["reason"] == "ValueError"
