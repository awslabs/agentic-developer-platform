"""Local façade-to-browser FIFO receipts use one owned session and bounded evidence."""

import base64
from decimal import Decimal
import hashlib
import json
import os
import time
from uuid import uuid4

import boto3
import pytest
from agentcore_tools.browser_http import BrowserConsumer
from agentcore_tools.gateway_browser import GatewayBrowser, GatewayBrowserConsumer, GatewayBrowserProvider
from agentcore_tools.mcp_adapter import dispatch_stateful
from fastapi import HTTPException
from fastapi.testclient import TestClient
from moto import mock_aws
from tools_facade.server import AgentCoreClient, create_app

START = "browser___start"
STEP = "browser___step"
CLOSE = "browser___close"
HEADERS = {"Authorization": "Bearer original"}
FIRST = "00000000-0000-4000-8000-000000000201"
SECOND = "00000000-0000-4000-8000-000000000202"
THIRD = "00000000-0000-4000-8000-000000000203"
FOURTH = "00000000-0000-4000-8000-000000000204"
FIFTH = "00000000-0000-4000-8000-000000000205"


class Authority:
    def __init__(self):
        self.owner = "user-one"
        self.grants = {START, STEP, CLOSE, "browser___inspect"}

    def decision(self, tool):
        return {"schema_version": "1.0", "allowed": True, "receipt_id": "receipt-" + tool,
                "principal": {"user_id": self.owner, "root_human_id": self.owner,
                              "tenant_id": "tenant-one", "team_id": "team-one"}}

    async def list_tools(self, headers):
        return {**self.decision(START), "tools": [{"name": tool, "description": tool,
                "inputSchema": {"type": "object"}} for tool in sorted(self.grants)]}

    async def authorize(self, tool, arguments, headers):
        if tool not in self.grants:
            raise HTTPException(403, "grant revoked")
        return self.decision(tool)

    async def recover(self, tool, headers):
        return self.decision(tool)

    async def close(self, tool, headers):
        return self.decision(tool)


class Provider(AgentCoreClient):
    async def invoke(self, tool, arguments, decision):
        target, operation = tool.split("___")
        claims = {"principal": decision.principal.model_dump(exclude={"root_human_id"}),
                  "receipt_id": decision.receipt_id,
                  **({"execution_deadline_at": decision.execution_deadline_at} if tool == START else {})}
        result = dispatch_stateful(target, operation, arguments, claims,
            revalidate=lambda: (claims, arguments, target, operation), assertion="signed-original")
        return {"jsonrpc": "2.0", "result": {"result": result,
                "user_id": decision.principal.user_id, "tenant_id": decision.principal.tenant_id}}


@pytest.fixture
def gateway(monkeypatch):
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        browser_table = dynamodb.create_table(TableName="browser-operations", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"},
                       {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"},
                                  {"AttributeName": "arrived_at", "AttributeType": "S"},
                                  {"AttributeName": "cleanup_partition", "AttributeType": "S"},
                                  {"AttributeName": "next_cleanup_at", "AttributeType": "N"}],
            GlobalSecondaryIndexes=[{"IndexName": "cleanup-due", "KeySchema": [
                {"AttributeName": "cleanup_partition", "KeyType": "HASH"},
                {"AttributeName": "next_cleanup_at", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"}}])
        dynamodb.create_table(TableName="facade-executions", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "execution_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "execution_id", "AttributeType": "S"},
                                  {"AttributeName": "cleanup_partition", "AttributeType": "S"},
                                  {"AttributeName": "next_cleanup_at", "AttributeType": "N"}],
            GlobalSecondaryIndexes=[{"IndexName": "cleanup-due",
                "KeySchema": [{"AttributeName": "cleanup_partition", "KeyType": "HASH"},
                              {"AttributeName": "next_cleanup_at", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"}}])
        queue = boto3.client("sqs", region_name="us-east-1")
        url = queue.create_queue(QueueName="browser.fifo", Attributes={"FifoQueue": "true"})["QueueUrl"]
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("BROWSER_QUEUE_URL", url)
        monkeypatch.setenv("BROWSER_OPERATIONS_TABLE", browser_table.name)
        monkeypatch.setenv("ADP_TOOLS_EXECUTION_TABLE", "facade-executions")
        monkeypatch.setenv("ADP_GATEWAY_BROWSER_READY", "true")
        monkeypatch.setattr("agentcore_tools.browser_runtime.isolated_browser.stop_session",
                            lambda provider_id: provider_id == "private-provider")
        calls = []

        def request(operation, arguments):
            calls.append(operation)
            if operation == "start":
                return {"session_token": "private-token", "view_id": "view-one",
                        "manifest": {"session_id": "private-provider"}, "observations": []}
            assert arguments["session_token"] == "private-token"
            if operation == "step":
                return {"view_id": "view-two", "observations": [{"visible_text": "Example Domain"}]}
            return {"cleanup_status": "stopped", "session_open": False}

        authority = Authority()
        browser = GatewayBrowser(table=browser_table, queue=queue)
        consumer = BrowserConsumer(browser_table, gateway_consumer=GatewayBrowserConsumer(browser,
            lambda envelope: envelope["claims"], GatewayBrowserProvider(browser, request=request)))
        yield authority, browser, queue, consumer, calls


def post(client, tool, arguments, request_id, execution_id=None):
    data = {"tool": tool, "arguments": arguments, "request_id": request_id}
    if execution_id:
        data["execution_id"] = execution_id
    return client.post("/agent/tools/call", json=data, headers=HEADERS)


def consume(queue, consumer):
    message = queue.receive_message(QueueUrl=os.environ["BROWSER_QUEUE_URL"])["Messages"][0]
    consumer.consume(message["Body"])
    queue.delete_message(QueueUrl=os.environ["BROWSER_QUEUE_URL"], ReceiptHandle=message["ReceiptHandle"])


def recover(client, receipt, tool):
    return client.get(f'/agent/tools/executions/{receipt["execution_id"]}/operations/{receipt["request_id"]}',
                      params={"tool": tool}, headers=HEADERS)


def test_facade_confirms_undispatched_browser_cleanup_and_preserves_original(gateway):
    authority, browser, queue, consumer, calls = gateway
    client = TestClient(create_app(gateway=authority, agentcore=Provider()))
    start = post(client, START, {"url": "https://example.com"}, FIRST)
    assert start.status_code == 202
    execution_id = start.json()["execution_id"]
    path = f"/agent/tools/executions/{execution_id}/close"
    authority.owner = "foreign"
    assert client.post(path, params={"tool": START}, headers=HEADERS).status_code == 404
    authority.owner = "user-one"
    closed = client.post(path, params={"tool": START}, headers=HEADERS)
    assert closed.status_code == 200 and closed.json()["cleanup_status"] == "confirmed"
    consume(queue, consumer)
    restarted = TestClient(create_app(gateway=authority, agentcore=Provider()))
    assert restarted.post(path, params={"tool": START}, headers=HEADERS).json() == closed.json()
    recovered = recover(restarted, start.json(), START).json()["result"]["result"]
    assert recovered["isError"] is True and recovered["result"]["status"] == "failed"
    row = boto3.client("dynamodb", region_name="us-east-1").get_item(TableName="facade-executions",
        Key={"execution_id": {"S": execution_id}}, ConsistentRead=True)["Item"]
    assert row["state"]["S"] == "closed" and row["cleanup_evidence"]["S"] == "start_not_dispatched"
    assert calls == []


def test_facade_fifo_browser_restart_evidence_and_owned_close(gateway):
    authority, browser, queue, consumer, calls = gateway
    client = TestClient(create_app(gateway=authority, agentcore=Provider()))
    start = post(client, START, {"url": "https://example.com"}, FIRST)
    assert start.status_code == 202 and start.json()["status"] == "unknown"
    consume(queue, consumer)
    restarted = TestClient(create_app(gateway=authority, agentcore=Provider()))
    original = restarted.get(f'/agent/tools/executions/{start.json()["execution_id"]}/operations/{FIRST}',
                             params={"tool": START}, headers=HEADERS)
    assert original.status_code == 200 and original.json()["status"] == "confirmed"
    handle = original.json()["result"]["result"]["result"]["result"]["session_id"]
    step = post(restarted, STEP, {"session_id": handle, "view_id": "view-one", "action": "screenshot"},
                SECOND, start.json()["execution_id"])
    assert step.status_code == 202
    consume(queue, consumer)
    recovered = recover(restarted, step.json(), STEP)
    assert recovered.status_code == 200 and recovered.json()["status"] == "confirmed"
    artifact = browser.artifact_read({"execution_id": step.json()["execution_id"],
                                     "artifact_id": SECOND}, {"principal": {"tenant_id": "tenant-one",
                                     "team_id": "team-one", "user_id": "user-one"}})
    assert hashlib.sha256(base64.b64decode(artifact["data_base64"])).hexdigest() == artifact["sha256"]
    assert json.loads(base64.b64decode(artifact["data_base64"]))["view_id"] == "view-two"
    close = post(restarted, CLOSE, {"session_id": handle}, THIRD, start.json()["execution_id"])
    assert close.status_code == 202
    consume(queue, consumer)
    closed = recover(restarted, close.json(), CLOSE)
    assert closed.status_code == 200 and closed.json()["status"] == "confirmed"
    assert closed.json()["result"]["result"]["result"]["result"]["termination_confirmed"] is True
    root = boto3.client("dynamodb", region_name="us-east-1").get_item(TableName="facade-executions",
        Key={"execution_id": {"S": start.json()["execution_id"]}}, ConsistentRead=True)["Item"]
    assert root["state"]["S"] == "closed" and root["cleanup_evidence"]["S"] == "provider_terminated"
    assert calls == ["start", "step", "close"]
    authority.owner = "foreign"
    assert recover(restarted, step.json(), STEP).status_code == 404


def test_browser_fixture_start_inspect_follow_and_inspect_through_facade(gateway):
    authority, browser, queue, consumer, calls = gateway
    choices = [{"id": "follow-fixture-choice", "action": "follow", "text": "Next page",
                "url": "https://fixture.example/linked"}]

    def fixture_request(operation, arguments):
        calls.append(operation)
        if operation == "start":
            assert arguments["url"] == "https://fixture.example/"
            return {"session_token": "private-token", "view_id": "fixture-first",
                    "manifest": {"session_id": "private-provider"}, "choices": choices,
                    "observations": [{"page_title": "Inert fixture", "visible_text": "Next page Form"}]}
        assert arguments["session_token"] == "private-token"
        if operation == "step":
            assert arguments["view_id"] == "fixture-first"
            assert arguments["action"] == "follow"
            assert arguments["candidate_id"] == choices[0]["id"]
            return {"view_id": "fixture-second", "choices": [],
                    "observations": [{"page_title": "Linked fixture", "visible_text": "Arrived"}]}
        assert operation == "close"
        return {"cleanup_status": "stopped", "session_open": False}

    consumer.gateway_consumer.dispatch.request = fixture_request
    client = TestClient(create_app(gateway=authority, agentcore=Provider()))

    def confirmed(receipt, tool):
        assert receipt.status_code == 202
        consume(queue, consumer)
        recovered = recover(client, receipt.json(), tool)
        assert recovered.status_code == 200 and recovered.json()["status"] == "confirmed"
        return recovered.json()["result"]["result"]["result"]["result"]

    started = post(client, START, {"url": "https://fixture.example/"}, FIRST)
    initial = confirmed(started, START)
    handle = initial["session_id"]
    execution_id = started.json()["execution_id"]
    assert initial["view_id"] == "fixture-first" and initial["choices"] == choices
    inspected = confirmed(post(client, "browser___inspect", {"session_id": handle,
        "view_id": initial["view_id"]}, SECOND, execution_id), "browser___inspect")
    assert inspected["choices"] == choices and inspected["view_id"] == initial["view_id"]
    artifact = browser.artifact_read({"execution_id": execution_id, "artifact_id": SECOND},
        {"principal": {"tenant_id": "tenant-one", "team_id": "team-one", "user_id": "user-one"}})
    content = base64.b64decode(artifact["data_base64"])
    assert hashlib.sha256(content).hexdigest() == inspected["artifact"]["content_sha256"]
    assert json.loads(content)["observations"][0]["page_title"] == "Inert fixture"
    assert "Form" in json.loads(content)["observations"][0]["visible_text"]
    followed = confirmed(post(client, STEP, {"session_id": handle, "view_id": inspected["view_id"],
        "action": "follow", "candidate_id": inspected["choices"][0]["id"]}, THIRD, execution_id), STEP)
    assert followed["session_id"] == handle and followed["view_id"] == "fixture-second"
    second = confirmed(post(client, "browser___inspect", {"session_id": handle,
        "view_id": followed["view_id"]}, FOURTH, execution_id), "browser___inspect")
    artifact = browser.artifact_read({"execution_id": execution_id, "artifact_id": FOURTH},
        {"principal": {"tenant_id": "tenant-one", "team_id": "team-one", "user_id": "user-one"}})
    assert json.loads(base64.b64decode(artifact["data_base64"]))["observations"][0]["page_title"] == "Linked fixture"
    assert second["view_id"] != initial["view_id"]
    closed = confirmed(post(client, CLOSE, {"session_id": handle}, FIFTH, execution_id), CLOSE)
    assert closed["termination_confirmed"] is True
    assert calls == ["start", "step", "close"]



def test_browser_denial_scenario_uses_facade_and_independent_queue_counter(gateway):
    from tests.e2e.tools.browser_integration import DENIALS, browser_ac02

    authority, browser, queue, consumer, calls = gateway
    choice = {"id": "follow-owned-choice", "action": "follow", "text": "Next page",
              "url": "https://fixture.example/linked"}

    def fixture_request(operation, arguments):
        calls.append(operation)
        if operation == "start":
            return {"session_token": "private-token", "view_id": "fixture-view",
                    "manifest": {"session_id": "private-provider"}, "choices": [choice],
                    "observations": [{"page_title": "Inert fixture"}]}
        assert operation == "close" and arguments["session_token"] == "private-token"
        return {"cleanup_status": "stopped", "session_open": False}

    consumer.gateway_consumer.dispatch.request = fixture_request
    client = TestClient(create_app(gateway=authority, agentcore=Provider()))
    ownership = {}

    class Observer:
        def snapshot(self, *, attempt_id):
            pending = int(queue.get_queue_attributes(QueueUrl=os.environ["BROWSER_QUEUE_URL"],
                AttributeNames=["ApproximateNumberOfMessages"])["Attributes"]["ApproximateNumberOfMessages"])
            return {"source": "independent-browser-provider", "network_requests": len(calls),
                    "browser_actions": pending, "mutations": calls.count("step")}

        def terminated(self, *, attempt_id):
            partition = browser._partition({"principal": {"tenant_id": "tenant-one", "team_id": "team-one",
                "user_id": "user-one"}}, ownership["execution_id"])
            return browser._read(partition, "SESSION")["state"] == "closed"

    class Driver:
        isolated = authorized = inert_https = True

        def open_owned(self, *, attempt_id):
            start = post(client, START, {"url": "https://fixture.example/"}, FIRST)
            assert start.status_code == 202
            consume(queue, consumer)
            result = recover(client, start.json(), START)
            assert result.status_code == 200
            packet = result.json()["result"]["result"]["result"]["result"]
            ownership.update(execution_id=start.json()["execution_id"], session_id=packet["session_id"])
            return {"session_id": packet["session_id"], "view_id": packet["view_id"],
                    "candidate_id": packet["choices"][0]["id"]}

        def revoke_owned(self, *, attempt_id):
            authority.grants.remove(STEP)

        def step_denied(self, case, opened, *, attempt_id):
            request = {"session_id": opened["session_id"], "view_id": opened["view_id"],
                       "action": "follow", "candidate_id": opened["candidate_id"]}
            if case == "stale_view":
                request["view_id"] = "stale-view"
            elif case == "foreign_candidate":
                request["candidate_id"] = "follow-foreign-choice"
            elif case == "prohibited_destination":
                request = {"session_id": opened["session_id"], "view_id": opened["view_id"],
                           "action": "navigate", "url": "http://127.0.0.1/private"}
            response = post(client, STEP, request, str(uuid4()), ownership["execution_id"])
            assert response.status_code in {403, 404}
            return {"status": "rejected", "error": response.json()["detail"]}

        def close_owned(self, *, attempt_id):
            close = post(client, CLOSE, {"session_id": ownership["session_id"]}, FIFTH,
                         ownership["execution_id"])
            assert close.status_code == 202
            consume(queue, consumer)
            assert recover(client, close.json(), CLOSE).json()["status"] == "confirmed"

    result = browser_ac02(Driver(), Observer())
    assert result["status"] == "pass" and result["denials"] == DENIALS
    assert result["cleanup"]["provider_termination"] is True
    assert calls == ["start", "close"]



def test_stale_view_step_never_reenters_the_browser_queue(gateway):
    authority, browser, queue, consumer, calls = gateway
    client = TestClient(create_app(gateway=authority, agentcore=Provider()))
    started = post(client, START, {"url": "https://example.com"}, FIRST)
    assert started.status_code == 202
    consume(queue, consumer)
    original = recover(client, started.json(), START).json()["result"]["result"]["result"]["result"]
    execution_id = started.json()["execution_id"]
    session_id, old_view = original["session_id"], original["view_id"]
    changed = post(client, STEP, {"session_id": session_id, "view_id": old_view,
                                  "action": "screenshot"}, SECOND, execution_id)
    assert changed.status_code == 202
    consume(queue, consumer)
    current = recover(client, changed.json(), STEP).json()["result"]["result"]["result"]["result"]
    assert current["session_id"] == session_id and current["view_id"] != old_view

    stale = {"session_id": session_id, "view_id": old_view, "action": "back"}
    for _ in range(2):
        denied = post(client, STEP, stale, str(uuid4()), execution_id)
        assert denied.status_code == 404, denied.json()
        assert denied.json()["detail"] == "Browser session or view unavailable"
        assert calls == ["start", "step"]
        queued = queue.get_queue_attributes(QueueUrl=os.environ["BROWSER_QUEUE_URL"],
            AttributeNames=["ApproximateNumberOfMessages"])["Attributes"]
        assert int(queued["ApproximateNumberOfMessages"]) == 0

    inspected = post(client, "browser___inspect", {"session_id": session_id,
                     "view_id": current["view_id"]}, FOURTH, execution_id)
    assert inspected.status_code == 202
    consume(queue, consumer)
    assert recover(client, inspected.json(), "browser___inspect").json()["status"] == "confirmed"
    closed = post(client, CLOSE, {"session_id": session_id}, FIFTH, execution_id)
    assert closed.status_code == 202
    consume(queue, consumer)
    assert recover(client, closed.json(), CLOSE).json()["result"]["result"]["result"]["result"]["termination_confirmed"] is True
    assert calls == ["start", "step", "close"]



def test_candidate_from_another_browser_session_cannot_enqueue_a_follow(gateway, monkeypatch):
    authority, browser, queue, consumer, calls = gateway
    monkeypatch.setattr("agentcore_tools.browser_runtime.isolated_browser.stop_session",
                        lambda provider_id: provider_id in {"private-provider", "private-provider-two"})
    choices = {}

    def request(operation, arguments):
        calls.append(operation)
        if operation == "start":
            provider = "private-provider" if arguments["url"].endswith("/one") else "private-provider-two"
            candidate = "follow-" + uuid4().hex
            choices[provider] = candidate
            return {"session_token": provider, "view_id": "view-" + provider,
                    "manifest": {"session_id": provider},
                    "choices": [{"id": candidate, "action": "follow", "text": "Next page",
                                 "url": "https://fixture.example/linked"}], "observations": []}
        assert arguments["session_token"] in choices
        if operation == "step":
            assert arguments["candidate_id"] == choices[arguments["session_token"]]
            return {"view_id": "after-follow", "choices": [],
                    "observations": [{"page_title": "Linked fixture"}]}
        assert operation == "close"
        return {"cleanup_status": "stopped", "session_open": False}

    consumer.gateway_consumer.dispatch.request = request
    client = TestClient(create_app(gateway=authority, agentcore=Provider()))
    started = []
    for destination, request_id in (("one", FIRST), ("two", SECOND)):
        response = post(client, START, {"url": "https://fixture.example/" + destination}, request_id)
        assert response.status_code == 202
        consume(queue, consumer)
        packet = recover(client, response.json(), START).json()["result"]["result"]["result"]["result"]
        started.append((response.json()["execution_id"], packet))
    first_execution, first = started[0]
    second_execution, second = started[1]
    assert first_execution != second_execution
    assert first["choices"][0]["id"] != second["choices"][0]["id"]
    foreign = post(client, STEP, {"session_id": first["session_id"], "view_id": first["view_id"],
        "action": "follow", "candidate_id": second["choices"][0]["id"]}, str(uuid4()), first_execution)
    assert foreign.status_code == 404 and foreign.json()["detail"] == "Browser candidate unavailable"
    assert calls == ["start", "start"]
    queued = queue.get_queue_attributes(QueueUrl=os.environ["BROWSER_QUEUE_URL"],
        AttributeNames=["ApproximateNumberOfMessages"])["Attributes"]
    assert int(queued["ApproximateNumberOfMessages"]) == 0
    valid = post(client, STEP, {"session_id": first["session_id"], "view_id": first["view_id"],
        "action": "follow", "candidate_id": first["choices"][0]["id"]}, THIRD, first_execution)
    assert valid.status_code == 202
    consume(queue, consumer)
    packet = recover(client, valid.json(), STEP).json()["result"]["result"]["result"]["result"]
    assert packet["view_id"] == "after-follow" and packet["session_id"] == first["session_id"]
    for execution_id, handle, request_id in ((first_execution, first["session_id"], FOURTH),
                                              (second_execution, second["session_id"], FIFTH)):
        closing = post(client, CLOSE, {"session_id": handle}, request_id, execution_id)
        assert closing.status_code == 202
        consume(queue, consumer)
        closed = recover(client, closing.json(), CLOSE).json()["result"]["result"]["result"]["result"]
        assert closed["termination_confirmed"] is True and closed["session_id"] == handle
    assert calls == ["start", "start", "step", "close", "close"]



def test_revoked_step_refuses_before_dispatch_while_inspection_pages_read_only(gateway):
    authority, browser, queue, consumer, calls = gateway

    def fixture_request(operation, arguments):
        calls.append(operation)
        if operation == "start":
            return {"session_token": "private-token", "view_id": "fixture-view",
                    "manifest": {"session_id": "private-provider"},
                    "observations": [{"page_title": "Inert fixture", "visible_text": "Marker " + "x" * 2500,
                                      "screenshot_status": "not_requested"}]}
        assert operation == "close" and arguments["session_token"] == "private-token"
        return {"cleanup_status": "stopped", "session_open": False}

    consumer.gateway_consumer.dispatch.request = fixture_request
    client = TestClient(create_app(gateway=authority, agentcore=Provider()))
    start = post(client, START, {"url": "https://fixture.example/"}, FIRST)
    assert start.status_code == 202
    consume(queue, consumer)
    initial = recover(client, start.json(), START).json()["result"]["result"]["result"]["result"]
    session_id, view_id = initial["session_id"], initial["view_id"]
    execution_id = start.json()["execution_id"]
    assert initial["observations"][0]["page_title"] == "Inert fixture"
    assert initial["observations"][0]["screenshot_status"] == "not_requested"
    assert len(initial["observations"][0]["visible_text"]) == 2000

    authority.grants.remove(STEP)
    denied = post(client, STEP, {"session_id": session_id, "view_id": view_id,
                                 "action": "screenshot"}, SECOND, execution_id)
    assert denied.status_code == 403 and denied.json()["detail"] == "grant revoked"
    queued = queue.get_queue_attributes(QueueUrl=os.environ["BROWSER_QUEUE_URL"],
        AttributeNames=["ApproximateNumberOfMessages"])["Attributes"]
    assert int(queued["ApproximateNumberOfMessages"]) == 0 and calls == ["start"]

    inspected = post(client, "browser___inspect", {"session_id": session_id,
                     "view_id": view_id}, FOURTH, execution_id)
    assert inspected.status_code == 202
    consume(queue, consumer)
    result = recover(client, inspected.json(), "browser___inspect").json()
    preview = result["result"]["result"]["result"]["result"]
    assert result["status"] == "confirmed" and preview["view_id"] == view_id
    assert preview["observations"][0]["page_title"] == "Inert fixture"
    assert calls == ["start"]

    chunks = []
    offset = 0
    while True:
        chunk = client.get(f"/agent/tools/executions/{execution_id}/operations/{FOURTH}/artifacts/result",
            params={"tool": "browser___inspect", "offset": offset, "length": 113}, headers=HEADERS)
        assert chunk.status_code == 200
        page = chunk.json()
        assert page["offset"] == offset and page["length"] <= 113
        chunks.append(base64.b64decode(page["data_base64"], validate=True))
        offset += page["length"]
        if offset == page["total_length"]:
            break
    content = b"".join(chunks)
    assert len(chunks) > 2 and hashlib.sha256(content).hexdigest() == page["sha256"]
    assert json.loads(content)["observations"][0]["page_title"] == "Inert fixture"
    assert result["artifact"]["content_sha256"] == page["sha256"]
    assert result["artifact"]["byte_length"] == len(content)
    provider_artifact = browser.artifact_read({"execution_id": execution_id, "artifact_id": FOURTH},
        {"principal": {"tenant_id": "tenant-one", "team_id": "team-one", "user_id": "user-one"}})
    assert hashlib.sha256(base64.b64decode(provider_artifact["data_base64"])).hexdigest() == preview["artifact"]["content_sha256"]
    assert calls == ["start"]

    closed = post(client, CLOSE, {"session_id": session_id}, FIFTH, execution_id)
    assert closed.status_code == 202
    consume(queue, consumer)
    assert recover(client, closed.json(), CLOSE).json()["result"]["result"]["result"]["result"]["termination_confirmed"] is True
    assert calls == ["start", "close"]



def test_browser_artifact_recovery_validates_bytes_and_termination_deadline(gateway, monkeypatch):
    authority, browser, queue, consumer, calls = gateway
    screenshot = b"fixture-screenshot-bytes"

    def request(operation, arguments):
        calls.append(operation)
        if operation == "start":
            return {"session_token": "private-token", "view_id": "view-one",
                    "manifest": {"session_id": "private-provider"}, "observations": []}
        assert arguments["session_token"] == "private-token"
        if operation == "step":
            return {"view_id": "view-two", "observations": [{
                "page_title": "Screenshot fixture", "screenshot_base64": base64.b64encode(screenshot).decode(),
                "screenshot_sha256": hashlib.sha256(screenshot).hexdigest()}]}
        assert operation == "close"
        return {"cleanup_status": "stopped", "session_open": False}

    consumer.gateway_consumer.dispatch.request = request
    started_at = int(time.time())
    client = TestClient(create_app(gateway=authority, agentcore=Provider()))
    start = client.post("/agent/tools/call", json={"tool": START,
        "arguments": {"url": "https://fixture.example/"}, "request_id": FIRST, "ttl_seconds": 300},
        headers=HEADERS)
    assert start.status_code == 202
    consume(queue, consumer)
    handle = recover(client, start.json(), START).json()["result"]["result"]["result"]["result"]["session_id"]
    execution_id = start.json()["execution_id"]
    session = browser._read(browser._partition({"principal": {"tenant_id": "tenant-one",
        "team_id": "team-one", "user_id": "user-one"}}, execution_id), "SESSION")
    assert started_at < session["deadline_at"] <= started_at + 301

    step = post(client, STEP, {"session_id": handle, "view_id": "view-one",
                               "action": "screenshot"}, SECOND, execution_id)
    assert step.status_code == 202
    consume(queue, consumer)
    saved = recover(client, step.json(), STEP).json()
    assert saved["status"] == "confirmed" and saved["artifact"]["content_sha256"]
    assert saved["result"]["result"]["result"]["result"]["observations"][0]["page_title"] == "Screenshot fixture"
    path = f"/agent/tools/executions/{execution_id}/operations/{SECOND}/artifacts/result"

    class CorruptProvider(Provider):
        async def invoke(self, tool, arguments, decision):
            result = await super().invoke(tool, arguments, decision)
            if tool == "browser___artifact_read":
                result["result"]["result"]["sha256"] = "0" * 64
            return result

    corrupt = TestClient(create_app(gateway=authority, agentcore=CorruptProvider()))
    assert corrupt.get(path, params={"tool": STEP}, headers=HEADERS).status_code == 503
    pages = []
    offset = 0
    while True:
        read = client.get(path, params={"tool": STEP, "offset": offset, "length": 67}, headers=HEADERS)
        assert read.status_code == 200
        page = read.json()
        pages.append(base64.b64decode(page["data_base64"], validate=True))
        offset += page["length"]
        if offset == page["total_length"]:
            break
    content = b"".join(pages)
    assert len(pages) > 1 and hashlib.sha256(content).hexdigest() == page["sha256"] == saved["artifact"]["content_sha256"]
    observation = json.loads(content)["observations"][0]
    assert base64.b64decode(observation["screenshot_base64"]) == screenshot
    assert observation["screenshot_sha256"] == hashlib.sha256(screenshot).hexdigest()
    authority.owner = "foreign"
    assert client.get(path, params={"tool": STEP}, headers=HEADERS).status_code == 404
    authority.owner = "user-one"
    assert calls == ["start", "step"]

    monkeypatch.setattr("agentcore_tools.browser_runtime.isolated_browser.stop_session", lambda provider_id: False)
    close = post(client, CLOSE, {"session_id": handle}, THIRD, execution_id)
    assert close.status_code == 202
    consume(queue, consumer)
    assert recover(client, close.json(), CLOSE).json()["status"] == "unknown"
    monkeypatch.setattr("agentcore_tools.browser_runtime.isolated_browser.stop_session",
                        lambda provider_id: provider_id == "private-provider")
    confirmed = recover(client, close.json(), CLOSE).json()
    assert confirmed["status"] == "confirmed"
    assert confirmed["result"]["result"]["result"]["result"]["termination_confirmed"] is True
    root = boto3.client("dynamodb", region_name="us-east-1").get_item(TableName="facade-executions",
        Key={"execution_id": {"S": execution_id}}, ConsistentRead=True)["Item"]
    assert root["state"]["S"] == "closed" and root["cleanup_evidence"]["S"] == "provider_terminated"
    assert int(root["cleanup_confirmed_at"]["N"]) <= started_at + 600
    assert calls == ["start", "step", "close"]


@pytest.mark.parametrize("legacy_packet", [False, True])
def test_browser_inspect_numeric_packet_survives_storage_and_restart(gateway, legacy_packet):
    authority, browser, queue, consumer, calls = gateway
    request = consumer.gateway_consumer.dispatch.request

    def numeric_packet(operation, arguments):
        packet = request(operation, arguments)
        packet.update({"steps_used": 0, "max_steps": 20, "fraction": 0.125})
        return packet

    consumer.gateway_consumer.dispatch.request = numeric_packet
    client = TestClient(create_app(gateway=authority, agentcore=Provider()))
    started = post(client, START, {"url": "https://example.com"}, FIRST)
    consume(queue, consumer)
    start_receipt = recover(client, started.json(), START).json()
    assert start_receipt["status"] == "confirmed"
    outcome = start_receipt["result"]["result"]["result"]["result"]
    claims = {"principal": {"tenant_id": "tenant-one", "team_id": "team-one", "user_id": "user-one"}}
    execution_id = started.json()["execution_id"]
    original = browser.artifact_read({"execution_id": execution_id, "artifact_id": FIRST}, claims)
    original_bytes = base64.b64decode(original["data_base64"])
    if legacy_packet:
        packet = json.loads(original_bytes, parse_int=Decimal, parse_float=Decimal)
        packet["fraction"] = Decimal("0.125")
        packet["observations"] = [{"count": Decimal("2"), "ratio": Decimal("0.5")}]
        browser.table.update_item(Key=browser._key(browser._partition(claims, execution_id), "SESSION"),
            UpdateExpression="SET packet = :packet", ExpressionAttributeValues={":packet": packet})
    restarted = GatewayBrowser(table=browser.table, queue=queue)
    consumer.gateway_consumer = GatewayBrowserConsumer(restarted, lambda envelope: envelope["claims"],
                                                       GatewayBrowserProvider(restarted, request=numeric_packet))
    inspected = post(client, "browser___inspect", {"session_id": outcome["session_id"], "view_id": "view-one"},
                     SECOND, execution_id)
    assert inspected.status_code == 202
    consume(queue, consumer)
    receipt = recover(client, inspected.json(), "browser___inspect").json()
    assert receipt["status"] == "confirmed"
    artifact = restarted.artifact_read({"execution_id": execution_id, "artifact_id": SECOND}, claims)
    content = base64.b64decode(artifact["data_base64"])
    assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
    assert receipt["result"]["result"]["result"]["result"]["artifact"]["content_sha256"] == artifact["sha256"]
    assert json.loads(content)["steps_used"] == 0 and json.loads(content)["max_steps"] == 20
    assert json.loads(content)["fraction"] == 0.125
    if legacy_packet:
        assert json.loads(content)["observations"] == [{"count": 2, "ratio": 0.5}]
    else:
        assert content == original_bytes
    assert calls == ["start"]


def test_lost_browser_action_response_does_not_replay(gateway):
    authority, browser, queue, consumer, calls = gateway
    client = TestClient(create_app(gateway=authority, agentcore=Provider()))
    start = post(client, START, {"url": "https://example.com"}, FIRST)
    consume(queue, consumer)
    execution_id = start.json()["execution_id"]
    handle = recover(client, start.json(), START).json()["result"]["result"]["result"]["result"]["session_id"]
    request = consumer.gateway_consumer.dispatch.request

    def lose_step(operation, arguments):
        if operation == "step":
            calls.append(operation)
            raise RuntimeError("response lost after navigation")
        return request(operation, arguments)

    consumer.gateway_consumer.dispatch.request = lose_step
    step = post(client, STEP, {"session_id": handle, "view_id": "view-one", "action": "screenshot"},
                SECOND, execution_id)
    assert step.status_code == 202
    message = queue.receive_message(QueueUrl=os.environ["BROWSER_QUEUE_URL"])["Messages"][0]
    consumer.consume(message["Body"])
    consumer.consume(message["Body"])
    assert recover(client, step.json(), STEP).json()["status"] == "unknown"
    assert post(client, STEP, {"session_id": handle, "view_id": "view-one", "action": "screenshot"},
                SECOND, execution_id).status_code == 202
    assert calls == ["start", "step"]


def test_browser_close_needs_provider_termination_confirmation(gateway):
    authority, browser, queue, consumer, calls = gateway
    client = TestClient(create_app(gateway=authority, agentcore=Provider()))
    start = post(client, START, {"url": "https://example.com"}, FIRST)
    consume(queue, consumer)
    execution_id = start.json()["execution_id"]
    handle = recover(client, start.json(), START).json()["result"]["result"]["result"]["result"]["session_id"]

    def unconfirmed_stop(operation, arguments):
        calls.append(operation)
        return {"cleanup_status": "stopped", "session_open": True}

    consumer.gateway_consumer.dispatch.request = unconfirmed_stop
    close = post(client, CLOSE, {"session_id": handle}, THIRD, execution_id)
    assert close.status_code == 202
    message = queue.receive_message(QueueUrl=os.environ["BROWSER_QUEUE_URL"])["Messages"][0]
    consumer.consume(message["Body"])
    consumer.consume(message["Body"])
    assert recover(client, close.json(), CLOSE).json()["status"] == "unknown"
    assert post(client, CLOSE, {"session_id": handle}, THIRD, execution_id).status_code == 202
    assert calls == ["start", "close"]
    session = browser._read(browser._partition({"principal": {"tenant_id": "tenant-one",
        "team_id": "team-one", "user_id": "user-one"}}, execution_id), "SESSION")
    assert session["state"] == "closing"


def test_browser_facade_does_not_confirm_unmatched_provider_receipt(gateway):
    authority, browser, queue, consumer, calls = gateway

    class ForgedProvider(Provider):
        async def invoke(self, tool, arguments, decision):
            return {"result": {"result": {"status": "confirmed", "request_id": arguments["request_id"],
                "execution_id": arguments["execution_id"], "receipt_id": "foreign"},
                "user_id": decision.principal.user_id, "tenant_id": decision.principal.tenant_id}}

    client = TestClient(create_app(gateway=authority, agentcore=ForgedProvider()))
    start = post(client, START, {"url": "https://example.com"}, FIRST)
    assert start.status_code == 202 and start.json()["status"] == "unknown"
    assert recover(client, start.json(), START).json()["status"] == "unknown"
    assert calls == []


def test_interrupted_browser_close_reconciles_and_recovers_original_receipt(gateway):
    from agentcore_tools.gateway_browser import GatewayBrowserReconciler

    authority, browser, queue, consumer, calls = gateway
    client = TestClient(create_app(gateway=authority, agentcore=Provider()))
    start = post(client, START, {"url": "https://example.com"}, FIRST)
    consume(queue, consumer)
    execution_id = start.json()["execution_id"]
    handle = recover(client, start.json(), START).json()["result"]["result"]["result"]["result"]["session_id"]

    def lost_stop(operation, arguments):
        calls.append(operation)
        raise RuntimeError("stop response lost")

    consumer.gateway_consumer.dispatch.request = lost_stop
    close = post(client, CLOSE, {"session_id": handle}, THIRD, execution_id)
    consume(queue, consumer)
    assert recover(client, close.json(), CLOSE).json()["status"] == "unknown"

    def confirmed_stop(operation, arguments):
        assert operation == "close" and arguments["session_token"] == "private-token"
        calls.append(operation)
        return {"cleanup_status": "stopped", "session_open": False}

    GatewayBrowserReconciler(browser, confirmed_stop).run_once()
    recovered = recover(client, close.json(), CLOSE)
    assert recovered.status_code == 200 and recovered.json()["status"] == "confirmed"
    assert recovered.json()["result"]["result"]["result"]["result"]["termination_confirmed"] is True
    assert calls == ["start", "close", "close"]


def test_revoked_browser_action_grant_still_allows_owned_close(gateway):
    authority, browser, queue, consumer, calls = gateway
    client = TestClient(create_app(gateway=authority, agentcore=Provider()))
    start = post(client, START, {"url": "https://example.com"}, FIRST)
    consume(queue, consumer)
    execution_id = start.json()["execution_id"]
    handle = recover(client, start.json(), START).json()["result"]["result"]["result"]["result"]["session_id"]
    authority.grants.difference_update({STEP, CLOSE})
    denied = post(client, STEP, {"session_id": handle, "view_id": "view-one", "action": "screenshot"},
                  SECOND, execution_id)
    assert denied.status_code == 403
    close = post(client, CLOSE, {"session_id": handle}, THIRD, execution_id)
    assert close.status_code == 202
    consume(queue, consumer)
    assert recover(client, close.json(), CLOSE).json()["status"] == "confirmed"
    assert calls == ["start", "close"]
    authority.owner = "foreign"
    assert post(client, CLOSE, {"session_id": handle},
                "00000000-0000-4000-8000-000000000208", execution_id).status_code == 404


def test_generic_browser_close_uses_saved_provider_and_child_reference(gateway, monkeypatch):
    from agentcore_tools.browser_runtime import isolated_browser

    authority, browser, queue, consumer, calls = gateway
    client = TestClient(create_app(gateway=authority, agentcore=Provider()))
    started = post(client, START, {"url": "https://example.com"}, FIRST)
    consume(queue, consumer)
    execution_id = started.json()["execution_id"]
    handle = recover(client, started.json(), START).json()["result"]["result"]["result"]["result"]["session_id"]
    stepped = post(client, STEP, {"session_id": handle, "view_id": "view-one", "action": "screenshot"},
                   SECOND, execution_id)
    consume(queue, consumer)
    assert recover(client, stepped.json(), STEP).json()["status"] == "confirmed"
    observed = []
    terminated = False

    def stop(session_id):
        observed.append(session_id)
        return terminated

    monkeypatch.setattr(isolated_browser, "stop_session", stop)
    path = f"/agent/tools/executions/{execution_id}/close"
    pending = client.post(path, params={"tool": STEP}, headers=HEADERS)
    assert pending.status_code == 202 and pending.json()["cleanup_status"] == "unknown"
    assert browser._read(browser._partition({"principal": {"tenant_id": "tenant-one", "team_id": "team-one",
        "user_id": "user-one"}}, execution_id), "SESSION")["state"] == "closing"
    terminated = True
    confirmed = TestClient(create_app(gateway=authority, agentcore=Provider())).post(
        path, params={"tool": STEP}, headers=HEADERS)
    assert confirmed.status_code == 200 and confirmed.json()["cleanup_status"] == "confirmed"
    assert observed == ["private-provider", "private-provider"]
    assert calls == ["start", "step"]
    assert recover(client, stepped.json(), STEP).json()["status"] == "confirmed"
    authority.owner = "foreign"
    assert client.post(path, params={"tool": STEP}, headers=HEADERS).status_code == 404


def test_legacy_browser_closed_receipt_requires_fresh_provider_readback(gateway, monkeypatch):
    from agentcore_tools.browser_runtime import isolated_browser

    authority, browser, queue, consumer, calls = gateway
    client = TestClient(create_app(gateway=authority, agentcore=Provider()))
    started = post(client, START, {"url": "https://example.com"}, FIRST)
    consume(queue, consumer)
    execution_id = started.json()["execution_id"]
    claims = {"principal": {"tenant_id": "tenant-one", "team_id": "team-one", "user_id": "user-one"}}
    key = browser._key(browser._partition(claims, execution_id), "SESSION")
    browser.table.update_item(Key=key,
        UpdateExpression="SET #state = :closed, cleanup_status = :confirmed",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={":closed": "closed", ":confirmed": "confirmed"})
    db = boto3.client("dynamodb", region_name="us-east-1")
    root_key = {"execution_id": {"S": execution_id}}
    db.update_item(TableName="facade-executions", Key=root_key,
        UpdateExpression="SET #state = :closed, cleanup_status = :confirmed REMOVE cleanup_partition, next_cleanup_at",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={":closed": {"S": "closed"}, ":confirmed": {"S": "confirmed"}})
    terminated = False
    monkeypatch.setattr(isolated_browser, "stop_session", lambda session_id: terminated)
    path = f"/agent/tools/executions/{execution_id}/close"
    pending = client.post(path, params={"tool": START}, headers=HEADERS)
    assert pending.status_code == 202 and pending.json()["cleanup_status"] == "unknown"
    pending_root = db.get_item(TableName="facade-executions", Key=root_key, ConsistentRead=True)["Item"]
    assert pending_root["state"]["S"] == "closing" and pending_root["cleanup_partition"]["S"] == "due"
    assert "cleanup_evidence" not in pending_root
    terminated = True
    confirmed = client.post(path, params={"tool": START}, headers=HEADERS)
    assert confirmed.status_code == 200 and confirmed.json()["cleanup_status"] == "confirmed"
    assert db.get_item(TableName="facade-executions", Key=root_key)["Item"]["cleanup_evidence"]["S"] == "provider_terminated"
    assert calls == ["start"]


@pytest.mark.parametrize("initially_terminated", [True, False])
def test_ordinary_browser_close_requires_independent_proof_and_repairs_confirmed_retry(
        gateway, monkeypatch, initially_terminated):
    authority, browser, queue, consumer, calls = gateway

    class ImmediateProvider(Provider):
        async def invoke(self, tool, arguments, decision):
            result = await super().invoke(tool, arguments, decision)
            if tool == CLOSE:
                consume(queue, consumer)
                return await super().invoke("browser___operation_get", {
                    "execution_id": arguments["execution_id"], "request_id": arguments["request_id"]}, decision)
            return result

    terminated = initially_terminated
    checked = []

    def stop(provider_id):
        checked.append(provider_id)
        return terminated

    monkeypatch.setattr("agentcore_tools.browser_runtime.isolated_browser.stop_session", stop)
    client = TestClient(create_app(gateway=authority, agentcore=ImmediateProvider()))
    started = post(client, START, {"url": "https://example.com"}, FIRST).json()
    consume(queue, consumer)
    handle = recover(client, started, START).json()["result"]["result"]["result"]["result"]["session_id"]
    identifier = started["execution_id"]
    closed = post(client, CLOSE, {"session_id": handle}, THIRD, identifier)
    assert closed.status_code == (200 if initially_terminated else 202)
    assert closed.json()["status"] == ("confirmed" if initially_terminated else "unknown")
    db = boto3.client("dynamodb", region_name="us-east-1")
    key = {"execution_id": {"S": identifier}}
    root = db.get_item(TableName="facade-executions", Key=key, ConsistentRead=True)["Item"]
    assert root["state"]["S"] == ("closed" if initially_terminated else "closing")
    assert checked == ["private-provider"] and calls == ["start", "close"]
    # Simulate an older confirmed ordinary receipt whose root was never proved.
    db.update_item(TableName="facade-executions", Key=key,
        UpdateExpression="SET #state = :closing, cleanup_status = :unknown REMOVE cleanup_evidence",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={":closing": {"S": "closing"}, ":unknown": {"S": "unknown"}})
    terminated = True
    restarted = TestClient(create_app(gateway=authority, agentcore=Provider()))
    retry = post(restarted, CLOSE, {"session_id": handle}, THIRD, identifier)
    assert retry.status_code == 200 and retry.json()["status"] == "confirmed"
    assert retry.json()["result"] == closed.json()["result"]
    root = db.get_item(TableName="facade-executions", Key=key, ConsistentRead=True)["Item"]
    assert root["state"]["S"] == "closed" and root["cleanup_evidence"]["S"] == "provider_terminated"
    assert checked == ["private-provider", "private-provider"] and calls == ["start", "close"]
