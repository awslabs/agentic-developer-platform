"""SCN-8066-02: independently observe scheduled cleanup across a worker restart."""

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import boto3
import pytest
from moto import mock_aws

from tests.e2e.tools import runner
from tests.e2e.tools.test_cleanup_ac01 import GATEWAY_IMAGE, IMAGE, IdleClient, SHA, TOOLS
from tools_facade.executions import ExecutionStore
from tools_facade.reconciler import run_once

TABLE = "fixture-scheduled-executions"


def authority():
    return SimpleNamespace(principal=SimpleNamespace(tenant_id="fixture-tenant", team_id="fixture-team",
        user_id="fixture-owner"), run_id=None, workload_id=None, run_expires_at=None,
        receipt_id="fixture-receipt")


class Provider:
    def __init__(self):
        self.states = {}
        self.dispatches = {}
        self.stops = {}

    def stop(self, item):
        execution_id = item["execution_id"]["S"]
        assert self.states[execution_id] == "READY"
        self.stops[execution_id] += 1
        self.states[execution_id] = "TERMINATED"
        return {"execution_id": execution_id, "status": "confirmed",
                "termination_confirmed": True, "provider_status": "TERMINATED"}


class Driver:
    isolated = True

    def __init__(self, provider):
        self.provider = provider
        self.attempts = {}

    def prepare(self, tool, *, attempt_id):
        store = ExecutionStore(table=TABLE, clock=lambda: 1000)
        root = TOOLS[tool]
        if tool == "search":
            operation_key, operation, _ = store.claim(None, uuid4(), root, {"query": "fixture"}, authority())
            execution_id = operation["operation_execution_id"]["S"]
            store.dispatched(operation_key)
            store.confirmed(operation_key, {"jsonrpc": "2.0", "result": {"result": {"status": "confirmed"}}})
        else:
            execution_id = store.admit(None, root, authority())
        self.provider.states[execution_id] = "not-required" if tool == "search" else "READY"
        self.provider.dispatches[execution_id] = 1
        self.provider.stops[execution_id] = 0
        self.attempts[attempt_id] = (tool, execution_id)
        if tool != "browser":
            store.close(UUID(execution_id), root, authority())
        return execution_id

    def first_pass(self, tool, *, attempt_id):
        if tool == "interpreter":
            def interrupted(_item):
                raise OSError("provider cleanup response lost")

            stop = interrupted
        else:
            stop = self.provider.stop
        run_once(ExecutionStore(table=TABLE, clock=lambda: 1900 if tool == "browser" else 1001),
                 stop_provider=stop)

    def restart(self, tool, *, attempt_id):
        now = 1960 if tool == "browser" else 1061 if tool == "interpreter" else 1002
        run_once(ExecutionStore(table=TABLE, clock=lambda: now), stop_provider=self.provider.stop)

    def cleanup_owned(self, *, attempt_id):
        tool, execution_id = self.attempts[attempt_id]
        if self.provider.states[execution_id] == "READY":
            store = ExecutionStore(table=TABLE, clock=lambda: 2000)
            if tool != "browser":
                store.close(UUID(execution_id), TOOLS[tool], authority())
            run_once(store, stop_provider=self.provider.stop)


class Observer:
    def __init__(self, provider, driver):
        self.provider, self.driver = provider, driver

    def readback(self, tool, execution_id, *, attempt_id):
        assert self.driver.attempts[attempt_id] == (tool, execution_id)
        item = boto3.client("dynamodb", region_name="us-east-1").get_item(
            TableName=TABLE, Key={"execution_id": {"S": execution_id}}, ConsistentRead=True)["Item"]
        return {"source": "isolated-provider-and-ledger-readback", "execution_state": item["state"]["S"],
                "cleanup_status": item.get("cleanup_status", {}).get("S"),
                "cleanup_last_outcome": item.get("cleanup_last_outcome", {}).get("S"),
                "cleanup_evidence": item.get("cleanup_evidence", {}).get("S"),
                "next_cleanup_at": item.get("next_cleanup_at", {}).get("N"),
                "cleanup_partition": item.get("cleanup_partition", {}).get("S"),
                "cleanup_attempts": int(item.get("cleanup_attempts", {}).get("N", "0")),
                "accepted_operations": int(item.get("accepted_operations", {}).get("N", "0")),
                "unresolved_operations": int(item.get("unresolved_operations", {}).get("N", "0")),
                "provider_state": self.provider.states[execution_id],
                "dispatch_count": self.provider.dispatches[execution_id],
                "stop_count": self.provider.stops[execution_id]}

    def verify_cleanup(self, tool, *, attempt_id):
        _, execution_id = self.driver.attempts[attempt_id]
        observed = self.readback(tool, execution_id, attempt_id=attempt_id)
        return (observed["execution_state"] == "closed" and
                observed["provider_state"] == ("not-required" if tool == "search" else "TERMINATED"))


@pytest.fixture
def isolated_cleanup(monkeypatch):
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
        driver = Driver(provider)
        yield driver, Observer(provider, driver)


def run_case(output, monkeypatch, driver=None, observer=None, *, live=False):
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    return runner.evaluate("SCN-8066-02", output, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE,
        command=["adp", "mcp"], client_factory=IdleClient,
        cleanup_driver_factory=(lambda *_: driver) if driver else None,
        cleanup_observer=observer, live=live)


def test_scheduled_cleanup_recovers_client_death_expiry_and_restart(tmp_path, monkeypatch, isolated_cleanup):
    driver, observer = isolated_cleanup
    monkeypatch.setenv("GITHUB_RUN_ID", "local-cleanup-ac02")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    assert run_case(tmp_path, monkeypatch, driver, observer)
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert (report["github_run_id"], report["github_run_attempt"]) == ("local-cleanup-ac02", "1")
    assert [row["tool"] for row in report["scenarios"]] == ["search", "interpreter", "browser"]
    for row in report["scenarios"]:
        assert row["tags"] == ["@scenario SCN-8066-02", "@story 8066", "@acceptance AC-02"]
        assert row["status"] == "pass" and row["cleanup"] == {"status": "confirmed"}
        assert row["observations"]["recovered"]["dispatch_count"] == 1
    assert report["scenarios"][1]["observations"]["pending"]["cleanup_last_outcome"] == "provider_error"
    assert report["scenarios"][2]["observations"]["pending"]["execution_state"] == "closing"


def test_scheduled_cleanup_requires_oracle_and_refuses_live_attestation(tmp_path, monkeypatch, isolated_cleanup):
    driver, observer = isolated_cleanup
    assert not run_case(tmp_path, monkeypatch, driver, None)
    assert all(row["status"] == "not-run" for row in
               json.loads((tmp_path / "evidence.json").read_text())["scenarios"])
    assert not driver.attempts
    assert not run_case(tmp_path, monkeypatch, driver, observer, live=True)
    assert all(row["status"] == "blocked" for row in
               json.loads((tmp_path / "evidence.json").read_text())["scenarios"])
    assert not driver.attempts


def test_scheduled_cleanup_rejects_false_termination_readback(tmp_path, monkeypatch, isolated_cleanup):
    driver, observer = isolated_cleanup
    original = observer.readback

    def unproved(tool, execution_id, *, attempt_id):
        found = original(tool, execution_id, attempt_id=attempt_id)
        return {**found, "provider_state": "READY"} if tool == "browser" else found

    monkeypatch.setattr(observer, "readback", unproved)
    assert not run_case(tmp_path, monkeypatch, driver, observer)
    rows = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert [row["status"] for row in rows] == ["pass", "pass", "blocked"]
    assert rows[2]["reason"] == "ValueError"


def test_failed_provider_cleanup_retries_after_restart_without_replaying_start(isolated_cleanup):
    driver, observer = isolated_cleanup
    attempt_id = str(uuid4())
    execution_id = driver.prepare("interpreter", attempt_id=attempt_id)
    callbacks = []

    def failed(item):
        callbacks.append(item["execution_id"]["S"])
        raise OSError("provider response lost")

    run_once(ExecutionStore(table=TABLE, clock=lambda: 1000), stop_provider=failed)
    pending = observer.readback("interpreter", execution_id, attempt_id=attempt_id)
    assert callbacks == [execution_id]
    assert (pending["execution_state"], pending["cleanup_status"], pending["cleanup_last_outcome"]) == (
        "closing", "unknown", "provider_error")
    assert pending["next_cleanup_at"] == "1060" and pending["cleanup_attempts"] == 1
    assert pending["provider_state"] == "READY" and pending["dispatch_count"] == 1

    run_once(ExecutionStore(table=TABLE, clock=lambda: 1059), stop_provider=failed)
    assert callbacks == [execution_id]

    def unverified(item):
        callbacks.append(item["execution_id"]["S"])
        return {"execution_id": execution_id, "status": "confirmed", "termination_confirmed": False}

    run_once(ExecutionStore(table=TABLE, clock=lambda: 1060), stop_provider=unverified)
    pending = observer.readback("interpreter", execution_id, attempt_id=attempt_id)
    assert callbacks == [execution_id, execution_id]
    assert (pending["execution_state"], pending["cleanup_status"], pending["cleanup_last_outcome"]) == (
        "closing", "unknown", "provider_unknown")
    assert pending["next_cleanup_at"] == "1180" and pending["cleanup_attempts"] == 2
    assert pending["provider_state"] == "READY" and pending["dispatch_count"] == 1

    run_once(ExecutionStore(table=TABLE, clock=lambda: 1179), stop_provider=unverified)
    assert len(callbacks) == 2
    run_once(ExecutionStore(table=TABLE, clock=lambda: 1180), stop_provider=driver.provider.stop)
    recovered = observer.readback("interpreter", execution_id, attempt_id=attempt_id)
    assert recovered["execution_state"] == "closed" and recovered["cleanup_status"] == "confirmed"
    assert recovered["provider_state"] == "TERMINATED" and recovered["cleanup_evidence"] == "provider_terminated"
    assert recovered["cleanup_attempts"] == 3 and recovered["stop_count"] == 1
    assert recovered["dispatch_count"] == 1
    assert recovered["next_cleanup_at"] is None and recovered["cleanup_partition"] is None
    run_once(ExecutionStore(table=TABLE, clock=lambda: 1300), stop_provider=failed)
    assert len(callbacks) == 2


def test_unknown_paid_search_operation_never_closes_or_replays_after_restart(isolated_cleanup):
    driver, observer = isolated_cleanup
    store = ExecutionStore(table=TABLE, clock=lambda: 1000)
    confirmed_key, confirmed, _ = store.claim(None, uuid4(), TOOLS["search"], {"query": "first"}, authority())
    execution_id = confirmed["operation_execution_id"]["S"]
    store.dispatched(confirmed_key)
    store.confirmed(confirmed_key, {"jsonrpc": "2.0", "result": {"result": {"status": "confirmed"}}})
    unknown_key, _, _ = store.claim(UUID(execution_id), uuid4(), TOOLS["search"],
                                    {"query": "unknown"}, authority())
    store.dispatched(unknown_key)
    store.close(UUID(execution_id), TOOLS["search"], authority())
    attempt_id = str(uuid4())
    driver.attempts[attempt_id] = ("search", execution_id)
    driver.provider.states[execution_id] = "not-required"
    driver.provider.dispatches[execution_id] = 2
    driver.provider.stops[execution_id] = 0
    provider_calls = []
    for timestamp, expected_retry in ((1000, "1060"), (1060, "1180"), (1180, "1300")):
        run_once(ExecutionStore(table=TABLE, clock=lambda: timestamp), stop_provider=provider_calls.append)
        observed = observer.readback("search", execution_id, attempt_id=attempt_id)
        assert observed["execution_state"] == "closing" and observed["cleanup_status"] == "unknown"
        assert observed["accepted_operations"] == 2 and observed["unresolved_operations"] == 1
        assert observed["next_cleanup_at"] == expected_retry and observed["cleanup_partition"] == "due"
        assert observed["dispatch_count"] == 2 and observed["cleanup_evidence"] is None
    unknown = boto3.client("dynamodb", region_name="us-east-1").get_item(
        TableName=TABLE, Key={"execution_id": {"S": unknown_key}}, ConsistentRead=True)["Item"]
    assert unknown["status"]["S"] == "unknown" and provider_calls == []
