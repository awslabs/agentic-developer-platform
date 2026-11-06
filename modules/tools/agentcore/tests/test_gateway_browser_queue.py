"""The Gateway browser claim and FIFO consumer use owned operation/view fences."""

import base64
import hashlib
import json
import os

import boto3
import pytest
from fastapi import HTTPException
from moto import mock_aws

from agentcore_tools.browser_http import BrowserConsumer
from agentcore_tools.gateway_browser import GatewayBrowser, GatewayBrowserConsumer, GatewayBrowserProvider
from agentcore_tools.mcp_adapter import dispatch_stateful

EXECUTION = "00000000-0000-4000-8000-000000000101"
START_ID = "00000000-0000-4000-8000-000000000102"
STEP_ID = "00000000-0000-4000-8000-000000000103"
SESSION = "a" * 64
CLAIMS = {"principal": {"tenant_id": "tenant-one", "user_id": "user-one", "team_id": "team-one"},
          "receipt_id": "start-receipt"}
START = {"execution_id": EXECUTION, "request_id": START_ID, "url": "https://example.com"}
STEP = {"execution_id": EXECUTION, "request_id": STEP_ID, "session_id": SESSION,
        "view_id": "view-one", "action": "screenshot"}


@pytest.fixture
def browser(monkeypatch):
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamodb.create_table(TableName="browser-operations", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"},
                       {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"},
                                  {"AttributeName": "arrived_at", "AttributeType": "S"}])
        queue = boto3.client("sqs", region_name="us-east-1")
        url = queue.create_queue(QueueName="browser.fifo", Attributes={"FifoQueue": "true"})["QueueUrl"]
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("BROWSER_QUEUE_URL", url)
        monkeypatch.setenv("BROWSER_OPERATIONS_TABLE", table.name)
        yield GatewayBrowser(table=table, queue=queue, clock=lambda: 1000)


def submit(browser, operation, arguments, claims=CLAIMS):
    return browser.submit(operation, arguments, claims, lambda: (claims, arguments, "browser", operation),
                          "signed-original")


def receive(browser):
    response = browser.queue.receive_message(QueueUrl=os.environ["BROWSER_QUEUE_URL"])
    assert len(response.get("Messages", [])) == 1
    message = response["Messages"][0]
    browser.queue.delete_message(QueueUrl=os.environ["BROWSER_QUEUE_URL"],
                                 ReceiptHandle=message["ReceiptHandle"])
    return message["Body"]


def test_owner_view_and_generation_flow_through_fifo_after_restart(browser):
    queued = submit(browser, "start", START)
    assert queued["status"] == "pending" and queued["generation"] == 1
    message = receive(browser)
    assert json.loads(message)["generation"] == 1
    restarted = GatewayBrowser(table=browser.table, queue=browser.queue, clock=lambda: 1000)
    dispatched = []

    def dispatch(operation, arguments, claims):
        dispatched.append((operation, arguments, claims))
        return {"status": "confirmed", "session_id": SESSION,
                "view_id": "view-one" if operation == "start" else "view-two"}

    consumer = GatewayBrowserConsumer(restarted, lambda envelope: envelope["claims"], dispatch)
    consumer.consume(message)
    consumer.consume(message)
    assert len(dispatched) == 1
    assert submit(restarted, "start", START)["status"] == "confirmed"
    foreign = {**CLAIMS, "principal": {**CLAIMS["principal"], "user_id": "user-two"}}
    with pytest.raises(HTTPException) as missing:
        submit(restarted, "step", STEP, foreign)
    assert missing.value.status_code == 404
    with pytest.raises(HTTPException) as conflict:
        submit(restarted, "start", {**START, "url": "https://example.org"})
    assert conflict.value.status_code == 409
    step = submit(restarted, "step", STEP)
    assert step["generation"] == 1
    step_message = receive(restarted)
    consumer.consume(step_message)
    assert len(dispatched) == 2
    assert submit(restarted, "step", STEP)["status"] == "confirmed"
    with pytest.raises(HTTPException) as stale:
        submit(restarted, "inspect", {**STEP, "request_id": "00000000-0000-4000-8000-000000000104"})
    assert stale.value.status_code == 404
    assert restarted._read(restarted._partition(CLAIMS, EXECUTION), "SESSION")["generation"] == 2


def test_tampered_queue_and_revocation_cannot_dispatch(browser):
    submit(browser, "start", START)
    message = receive(browser)
    dispatched = []
    consumer = GatewayBrowserConsumer(browser, lambda envelope: {"principal": {"user_id": "revoked"}},
                                       lambda *args: dispatched.append(args))
    tampered = json.loads(message)
    tampered["arguments"]["url"] = "https://other.example"
    tampered["generation"] = 9
    consumer.reauthorize = lambda envelope: envelope["claims"]
    consumer.consume(json.dumps(tampered))
    assert dispatched == []
    assert browser._read(browser._partition(CLAIMS, EXECUTION), "OP#" + START_ID)["state"] == "queued"
    def revoked(envelope):
        raise HTTPException(403, "grant revoked")

    consumer.reauthorize = revoked
    consumer.consume(message)
    assert dispatched == []
    assert browser._read(browser._partition(CLAIMS, EXECUTION), "OP#" + START_ID)["state"] == "failed"


def test_lost_consumer_response_does_not_repeat_uncertain_navigation(browser):
    submit(browser, "start", START)
    start_message = receive(browser)
    consumer = GatewayBrowserConsumer(browser, lambda envelope: envelope["claims"],
        lambda *args: {"status": "confirmed", "session_id": SESSION, "view_id": "view-one"})
    consumer.consume(start_message)
    submit(browser, "step", STEP)
    message = receive(browser)
    called = []

    def navigation(*args):
        called.append(args)
        raise TimeoutError("response lost after navigation")

    consumer.dispatch = navigation
    consumer.consume(message)
    consumer.consume(message)
    assert len(called) == 1
    assert submit(browser, "step", STEP)["status"] == "unknown"
    with pytest.raises(HTTPException) as duplicate:
        submit(browser, "step", {**STEP, "request_id": "00000000-0000-4000-8000-000000000106"})
    assert duplicate.value.status_code == 409
    assert browser._read(browser._partition(CLAIMS, EXECUTION), "SESSION")["view_id"] == "view-one"



def test_lost_provider_navigation_reply_is_never_redispatched_after_restart(browser, monkeypatch):
    calls = []

    def request(operation, payload):
        calls.append(operation)
        if operation == "start":
            return {"session_token": "private-token", "view_id": "view-one",
                    "manifest": {"session_id": "private-provider"},
                    "observations": [{"page_title": "Inert fixture"}]}
        assert operation == "step" and payload["session_token"] == "private-token"
        raise TimeoutError("provider reply lost after one navigation")

    consumer = BrowserConsumer(browser.table, gateway_consumer=GatewayBrowserConsumer(browser,
        lambda envelope: envelope["claims"], GatewayBrowserProvider(browser, request=request)))
    submit(browser, "start", START)
    consumer.consume(receive(browser))
    opened = browser.operation_get({"execution_id": EXECUTION, "request_id": START_ID}, CLAIMS)
    assert opened["status"] == "confirmed"
    handle = opened["result"]["session_id"]
    step = {**STEP, "session_id": handle, "action": "back"}
    submit(browser, "step", step)
    original_message = receive(browser)
    consumer.consume(original_message)
    assert calls == ["start", "step"]
    restarted = GatewayBrowser(table=browser.table, queue=browser.queue, clock=lambda: 1000)
    resumed_consumer = BrowserConsumer(browser.table, gateway_consumer=GatewayBrowserConsumer(restarted,
        lambda envelope: envelope["claims"], GatewayBrowserProvider(restarted, request=request)))
    resumed_consumer.consume(original_message)
    resumed_consumer.consume(original_message)
    original = restarted.operation_get({"execution_id": EXECUTION, "request_id": STEP_ID}, CLAIMS)
    assert original["status"] == "unknown" and original["request_id"] == STEP_ID
    assert submit(restarted, "step", step)["status"] == "unknown"
    with pytest.raises(HTTPException) as repeat:
        submit(restarted, "step", {**step, "request_id": "00000000-0000-4000-8000-000000000106"})
    assert repeat.value.status_code == 409 and calls == ["start", "step"]
    start_artifact = restarted.artifact_read({"execution_id": EXECUTION, "artifact_id": START_ID}, CLAIMS)
    content = base64.b64decode(start_artifact["data_base64"])
    assert hashlib.sha256(content).hexdigest() == opened["result"]["artifact"]["content_sha256"]
    assert json.loads(content)["manifest"]["session_id"] == "private-provider"
    terminated = []
    monkeypatch.setattr("agentcore_tools.browser_runtime.isolated_browser.stop_session",
                        lambda provider_id: terminated.append(provider_id) or provider_id == "private-provider")
    binding = {"execution_id": EXECUTION}
    close = restarted.execution_close(binding, CLAIMS,
        lambda: (CLAIMS, binding, "browser", "execution_close"))
    assert close["status"] == "confirmed" and close["termination_confirmed"] is True
    assert close["provider_status"] == "TERMINATED" and terminated == ["private-provider"]
    assert calls == ["start", "step"]


def test_queued_step_survives_consumer_restart_without_replaying_provider_action(browser, monkeypatch):
    calls = []

    def request(operation, payload):
        calls.append(operation)
        if operation == "start":
            return {"session_token": "private-token", "view_id": "view-one",
                    "manifest": {"session_id": "private-provider"}}
        assert operation == "step" and payload["session_token"] == "private-token"
        assert payload["view_id"] == "view-one"
        return {"view_id": "view-two", "observations": [{"text": "Linked page"}]}

    initial = BrowserConsumer(browser.table, gateway_consumer=GatewayBrowserConsumer(browser,
        lambda envelope: envelope["claims"], GatewayBrowserProvider(browser, request=request)))
    submit(browser, "start", START)
    initial.consume(receive(browser))
    opened = browser.operation_get({"execution_id": EXECUTION, "request_id": START_ID}, CLAIMS)
    assert opened["status"] == "confirmed" and calls == ["start"]
    step = {**STEP, "session_id": opened["result"]["session_id"], "action": "back"}
    pending = submit(browser, "step", step)
    queued_message = receive(browser)
    partition = browser._partition(CLAIMS, EXECUTION)
    assert pending["status"] == "pending" and calls == ["start"]
    assert browser._read(partition, "OP#" + STEP_ID)["state"] == "queued"

    restarted = GatewayBrowser(table=browser.table, queue=browser.queue, clock=lambda: 1000)
    resumed = BrowserConsumer(browser.table, gateway_consumer=GatewayBrowserConsumer(restarted,
        lambda envelope: envelope["claims"], GatewayBrowserProvider(restarted, request=request)))
    resumed.consume(queued_message)
    another_restart = GatewayBrowser(table=browser.table, queue=browser.queue, clock=lambda: 1000)
    redelivered = BrowserConsumer(browser.table, gateway_consumer=GatewayBrowserConsumer(another_restart,
        lambda envelope: envelope["claims"], GatewayBrowserProvider(another_restart, request=request)))
    redelivered.consume(queued_message)
    receipt = another_restart.operation_get({"execution_id": EXECUTION, "request_id": STEP_ID}, CLAIMS)
    assert receipt["status"] == "confirmed" and receipt["result"]["view_id"] == "view-two"
    assert submit(another_restart, "step", step) == receipt
    assert another_restart._read(partition, "SESSION")["generation"] == pending["generation"] + 1
    assert calls == ["start", "step"]

    stopped = []
    monkeypatch.setattr("agentcore_tools.browser_runtime.isolated_browser.stop_session",
                        lambda provider_id: stopped.append(provider_id) or True)
    binding = {"execution_id": EXECUTION}
    closed = another_restart.execution_close(binding, CLAIMS,
        lambda: (CLAIMS, binding, "browser", "execution_close"))
    assert closed["termination_confirmed"] is True and stopped == ["private-provider"]
    assert calls == ["start", "step"]


def test_stale_queued_view_is_rejected_without_second_action(browser):
    submit(browser, "start", START)
    consumer = GatewayBrowserConsumer(browser, lambda envelope: envelope["claims"],
        lambda operation, *args: {"status": "confirmed", "session_id": SESSION,
                                  "view_id": "view-one" if operation == "start" else "view-two"})
    consumer.consume(receive(browser))
    submit(browser, "step", STEP)
    another = {**STEP, "request_id": "00000000-0000-4000-8000-000000000105",
               "action": "back"}
    submit(browser, "step", another)
    consumer.consume(receive(browser))
    consumer.consume(receive(browser))
    assert submit(browser, "step", another)["status"] == "failed"
    assert browser._read(browser._partition(CLAIMS, EXECUTION), "SESSION")["view_id"] == "view-two"


def test_actual_browser_consumer_routes_gateway_envelopes_only_with_reauthorization(browser):
    submit(browser, "start", START)
    message = receive(browser)
    direct = BrowserConsumer(browser.table)
    with pytest.raises(HTTPException) as unavailable:
        direct.consume(message)
    assert unavailable.value.status_code == 503
    calls = []
    authorized = GatewayBrowserConsumer(browser, lambda envelope: envelope["claims"],
        lambda operation, args, claims: (calls.append(operation) or
            {"status": "confirmed", "session_id": SESSION, "view_id": "view-one"}))
    direct.gateway_consumer = authorized
    direct.consume(message)
    direct.consume(message)
    assert calls == ["start"]
    assert submit(browser, "start", START)["status"] == "confirmed"


def test_actual_queue_provider_evidence_restart_and_owned_close(browser):
    calls = []

    def request(operation, payload):
        calls.append(operation)
        if operation == "start":
            return {"session_token": "private-token", "view_id": "view-one",
                    "manifest": {"session_id": "private-provider"}, "observations": [{"title": "Example"}]}
        assert payload["session_token"] == "private-token"
        if operation == "step":
            assert payload["view_id"] == "view-one"
            return {"view_id": "view-two", "observations": [{"text": "Example Domain"}]}
        return {"cleanup_status": "stopped", "session_open": False}

    consumer = BrowserConsumer(browser.table, gateway_consumer=GatewayBrowserConsumer(browser,
        lambda envelope: envelope["claims"], GatewayBrowserProvider(browser, request=request)))
    submit(browser, "start", START)
    consumer.consume(receive(browser))
    started = browser.operation_get({"execution_id": EXECUTION, "request_id": START_ID}, CLAIMS)
    assert started["status"] == "confirmed"
    handle = started["result"]["session_id"]
    assert len(handle) == 64
    restarted = GatewayBrowser(table=browser.table, queue=browser.queue, clock=lambda: 1000)
    step = {**STEP, "session_id": handle}
    submit(restarted, "step", step)
    restarted_consumer = BrowserConsumer(browser.table, gateway_consumer=GatewayBrowserConsumer(restarted,
        lambda envelope: envelope["claims"], GatewayBrowserProvider(restarted, request=request)))
    restarted_consumer.consume(receive(restarted))
    receipt = restarted.operation_get({"execution_id": EXECUTION, "request_id": STEP_ID}, CLAIMS)
    assert receipt["status"] == "confirmed" and receipt["result"]["view_id"] == "view-two"
    artifact = restarted.artifact_read({"execution_id": EXECUTION, "artifact_id": STEP_ID}, CLAIMS)
    content = base64.b64decode(artifact["data_base64"])
    assert hashlib.sha256(content).hexdigest() == artifact["sha256"] == receipt["result"]["artifact"]["content_sha256"]
    foreign = {**CLAIMS, "principal": {**CLAIMS["principal"], "user_id": "foreign"}}
    with pytest.raises(HTTPException) as denied:
        restarted.artifact_read({"execution_id": EXECUTION, "artifact_id": STEP_ID}, foreign)
    assert denied.value.status_code == 404
    close_id = "00000000-0000-4000-8000-000000000107"
    submit(restarted, "close", {"execution_id": EXECUTION, "request_id": close_id,
                                 "session_id": handle})
    restarted_consumer.consume(receive(restarted))
    closed = restarted._read(restarted._partition(CLAIMS, EXECUTION), "OP#" + close_id)
    assert closed["state"] == "confirmed" and closed["result"]["termination_confirmed"] is True
    assert calls == ["start", "step", "close"]


def test_gateway_dispatch_refuses_until_consumer_ready_then_queues_signed_identity(browser, monkeypatch):
    def revalidate():
        return CLAIMS, START, "browser", "start"

    with pytest.raises(HTTPException) as unavailable:
        dispatch_stateful("browser", "start", START, CLAIMS, revalidate, "signed-original")
    assert unavailable.value.status_code == 503
    assert browser._read(browser._partition(CLAIMS, EXECUTION), "OP#" + START_ID) is None
    monkeypatch.setenv("ADP_GATEWAY_BROWSER_READY", "true")
    result = dispatch_stateful("browser", "start", START, CLAIMS, revalidate, "signed-original")
    assert result["status"] == "pending" and result["request_id"] == START_ID
    envelope = json.loads(receive(browser))
    assert envelope["arguments"] == START and envelope["assertion"] == "signed-original"
    operation = dispatch_stateful("browser", "operation_get",
        {"execution_id": EXECUTION, "request_id": START_ID}, CLAIMS, lambda: None)
    assert operation["status"] == "pending" and operation["receipt_id"] == CLAIMS["receipt_id"]


def cleanup_index(browser):
    browser.table.meta.client.update_table(TableName=browser.table.name,
        AttributeDefinitions=[{"AttributeName": "cleanup_partition", "AttributeType": "S"},
                              {"AttributeName": "next_cleanup_at", "AttributeType": "N"}],
        GlobalSecondaryIndexUpdates=[{"Create": {"IndexName": "cleanup-due",
            "KeySchema": [{"AttributeName": "cleanup_partition", "KeyType": "HASH"},
                          {"AttributeName": "next_cleanup_at", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"}}}])


def test_expired_browser_session_reconciles_only_confirmed_provider_stop(browser):
    from agentcore_tools.gateway_browser import GatewayBrowserReconciler

    cleanup_index(browser)
    calls = []
    def request(operation, arguments):
        calls.append(operation)
        if operation == "start":
            return {"session_token": "owned-token", "manifest": {"session_id": "provider-one"},
                    "view_id": "view-one", "observations": []}
        return {"cleanup_status": "stopped", "session_open": False}

    provider = GatewayBrowserProvider(browser, request=request)
    original = {**CLAIMS, "run_expires_at": 1002}
    submit(browser, "start", START, original)
    GatewayBrowserConsumer(browser, lambda envelope: envelope["claims"], provider).consume(receive(browser))
    partition = browser._partition(original, EXECUTION)
    assert browser._read(partition, "SESSION")["state"] == "active"
    restarted = GatewayBrowser(table=browser.table, queue=browser.queue, clock=lambda: 1003)
    reconciler = GatewayBrowserReconciler(restarted, request)
    reconciler.run_once()
    reconciler.run_once()
    session = restarted._read(partition, "SESSION")
    assert session["state"] == "closed" and session["cleanup_status"] == "confirmed"
    assert calls == ["start", "close"]
    with pytest.raises(HTTPException) as fenced:
        submit(restarted, "step", STEP, original)
    assert fenced.value.status_code == 404


def test_ambiguous_browser_start_stays_unknown_when_cleanup_cannot_identify_provider(browser):
    from agentcore_tools.gateway_browser import GatewayBrowserReconciler

    cleanup_index(browser)
    claims = {**CLAIMS, "run_expires_at": 1002}
    submit(browser, "start", START, claims)
    def lose_start(operation, arguments):
        raise RuntimeError("response lost")

    GatewayBrowserConsumer(browser, lambda envelope: envelope["claims"],
                           GatewayBrowserProvider(browser, request=lose_start)).consume(receive(browser))
    restarted = GatewayBrowser(table=browser.table, queue=browser.queue, clock=lambda: 1003)
    calls = []
    GatewayBrowserReconciler(restarted, lambda operation, arguments: calls.append(operation)).run_once()
    session = restarted._read(restarted._partition(claims, EXECUTION), "SESSION")
    assert session["state"] == "closing" and "cleanup_status" not in session
    assert restarted.operation_get({"execution_id": EXECUTION, "request_id": START_ID}, claims)["status"] == "unknown"
    assert calls == []


def test_browser_provider_receives_original_bounded_deadline(browser):
    claims = {**CLAIMS, "execution_deadline_at": 1200}
    assert submit(browser, "start", START, claims)["status"] == "pending"
    session = browser._read(browser._partition(claims, EXECUTION), "SESSION")
    assert session["deadline_at"] == 1200
    seen = []

    def request(operation, payload):
        json.dumps(payload)
        assert type(payload["deadline_at"]) is int
        seen.append((operation, payload))
        return {"session_token": "private-token", "manifest": {"session_id": "private-provider"},
                "view_id": "view-one", "observations": []}

    GatewayBrowserConsumer(browser, lambda envelope: envelope["claims"],
                           GatewayBrowserProvider(browser, request=request)).consume(receive(browser))
    result = browser.operation_get({"execution_id": EXECUTION, "request_id": START_ID}, claims)
    assert result["status"] == "confirmed"
    assert seen == [("start", {"url": START["url"], "deadline_at": 1200})]


@pytest.mark.parametrize("deadline", [999, 1901, "1200", True])
def test_browser_refuses_expired_or_unbounded_deadline_without_provider(browser, deadline):
    claims = {**CLAIMS, "execution_deadline_at": deadline}
    with pytest.raises(HTTPException) as denied:
        submit(browser, "start", START, claims)
    assert denied.value.status_code == 403
    assert browser._read(browser._partition(claims, EXECUTION), "SESSION") is None


def test_browser_queue_delay_past_signed_deadline_does_not_dispatch(browser):
    claims = {**CLAIMS, "execution_deadline_at": 1050}
    submit(browser, "start", START, claims)
    message = receive(browser)
    browser.clock = lambda: 1050
    dispatched = []
    GatewayBrowserConsumer(browser, lambda envelope: envelope["claims"],
                           lambda *args: dispatched.append(args)).consume(message)
    assert dispatched == []
    partition = browser._partition(claims, EXECUTION)
    assert browser._read(partition, "OP#" + START_ID)["state"] == "failed"


def close_execution(browser):
    arguments = {"execution_id": EXECUTION}
    return browser.execution_close(arguments, CLAIMS,
        lambda: (CLAIMS, arguments, "browser", "execution_close"))


@pytest.mark.parametrize("timing", ["before_consumer", "during_authorization", "before_provider"])
def test_close_fences_queued_and_admitted_start_before_dispatch(browser, timing):
    submit(browser, "start", START)
    message = receive(browser)
    starts = []
    provider = GatewayBrowserProvider(browser, request=lambda *args: starts.append(args))

    def authorize(envelope):
        if timing == "during_authorization":
            assert close_execution(browser)["status"] == "confirmed"
        return envelope["claims"]

    def dispatch(*args):
        if timing == "before_provider":
            assert close_execution(browser)["status"] == "confirmed"
        return provider(*args)

    if timing == "before_consumer":
        assert close_execution(browser)["status"] == "confirmed"
    consumer = GatewayBrowserConsumer(browser, authorize, dispatch)
    consumer.consume(message)
    consumer.consume(message)
    assert starts == []
    session = browser._read(browser._partition(CLAIMS, EXECUTION), "SESSION")
    assert session["state"] == "closed" and "provider_id" not in session
    assert session["cleanup_status"] == "confirmed" and session["cleanup_proof"] == "start_not_dispatched"
    assert close_execution(browser)["provider_status"] == "NOT_STARTED"
    assert session["start_id"] == START_ID and session["generation"] == 1 and session["deadline_at"] == 1900


@pytest.mark.parametrize("field,value", [("operation", "step"), ("request_id", "different"),
    ("execution_id", "different"), ("generation", 2), ("execution_deadline_at", 2000),
    ("provider_dispatch", "sent"), ("provider_dispatch", "cancelled"),
    ("provider_id", "provider-one"), ("provider_token", "private-token"), (None, None)])
def test_cleanup_without_provider_identity_requires_matching_undispatched_start(browser, field, value):
    submit(browser, "start", START)
    partition = browser._partition(CLAIMS, EXECUTION)
    key = browser._key(partition, "OP#" + START_ID)
    if field is None:
        browser.table.delete_item(Key=key)
    else:
        browser.table.update_item(Key=key, UpdateExpression="SET #field = :value",
            ExpressionAttributeNames={"#field": field}, ExpressionAttributeValues={":value": value})
    assert close_execution(browser)["status"] == "pending"
    session = browser._read(partition, "SESSION")
    assert session["state"] == "closing" and "cleanup_proof" not in session


def test_dispatch_winning_close_race_cannot_be_cancelled(browser, monkeypatch):
    submit(browser, "start", START)
    message = receive(browser)
    partition = browser._partition(CLAIMS, EXECUTION)
    update = browser.table.update_item
    calls = []

    def lose_response(operation, arguments):
        calls.append(operation)
        raise RuntimeError("provider response lost")

    consumer = GatewayBrowserConsumer(browser, lambda envelope: envelope["claims"],
                                     GatewayBrowserProvider(browser, request=lose_response))

    def race(**kwargs):
        if kwargs["UpdateExpression"] == "SET #state = :closing, next_cleanup_at = :now":
            consumer.consume(message)
        return update(**kwargs)

    monkeypatch.setattr(browser.table, "update_item", race)
    assert close_execution(browser)["status"] == "pending"
    assert calls == ["start"]
    assert browser._read(partition, "OP#" + START_ID)["provider_dispatch"] == "sent"
    session = browser._read(partition, "SESSION")
    assert session["state"] == "closing" and "cleanup_proof" not in session


def test_cleanup_transaction_refuses_changed_binding_after_read(browser, monkeypatch):
    submit(browser, "start", START)
    partition = browser._partition(CLAIMS, EXECUTION)
    transact = browser._transact

    def race(writes):
        browser.table.update_item(Key=browser._key(partition, "OP#" + START_ID),
            UpdateExpression="SET generation = :new", ExpressionAttributeValues={":new": 2})
        return transact(writes)

    monkeypatch.setattr(browser, "_transact", race)
    with pytest.raises(HTTPException) as unavailable:
        close_execution(browser)
    assert unavailable.value.status_code == 503
    assert close_execution(browser)["status"] == "pending"
    session = browser._read(partition, "SESSION")
    assert session["state"] == "closing" and "cleanup_proof" not in session
    assert "provider_dispatch" not in browser._read(partition, "OP#" + START_ID)


def test_cleanup_recovers_original_proof_after_lost_transaction_ack(browser, monkeypatch):
    from botocore.exceptions import EndpointConnectionError

    submit(browser, "start", START)
    transact = browser._transact

    def lose_ack(writes):
        transact(writes)
        raise EndpointConnectionError(endpoint_url="https://dynamodb.test")

    monkeypatch.setattr(browser, "_transact", lose_ack)
    with pytest.raises(HTTPException) as unavailable:
        close_execution(browser)
    assert unavailable.value.status_code == 503
    restarted = GatewayBrowser(table=browser.table, queue=browser.queue, clock=browser.clock)
    assert close_execution(restarted)["provider_status"] == "NOT_STARTED"
    calls = []
    GatewayBrowserConsumer(restarted, lambda envelope: envelope["claims"],
        GatewayBrowserProvider(restarted, request=lambda *args: calls.append(args))).consume(receive(browser))
    assert calls == []
    assert close_execution(restarted)["termination_confirmed"] is True


@pytest.mark.parametrize("consume_first", [False, True])
def test_reconciler_proves_cleanup_for_expired_undispatched_start(browser, consume_first):
    from agentcore_tools.gateway_browser import GatewayBrowserReconciler

    cleanup_index(browser)
    claims = {**CLAIMS, "run_expires_at": 1002}
    submit(browser, "start", START, claims)
    browser.clock = lambda: 1003
    calls = []
    if consume_first:
        GatewayBrowserConsumer(browser, lambda envelope: envelope["claims"],
            GatewayBrowserProvider(browser, request=lambda *args: calls.append(args))).consume(receive(browser))
    reconciler = GatewayBrowserReconciler(browser, lambda *args: calls.append(args))
    reconciler.run_once()
    reconciler.run_once()
    partition = browser._partition(claims, EXECUTION)
    session = browser._read(partition, "SESSION")
    assert session["state"] == "closed" and session["cleanup_status"] == "confirmed"
    assert session["cleanup_proof"] == "start_not_dispatched"
    assert "next_cleanup_at" not in session and "cleanup_partition" not in session
    assert browser._read(partition, "OP#" + START_ID)["provider_dispatch"] == "cancelled"
    assert calls == []


@pytest.mark.parametrize("failed_copy", [None, "SESSION", "OP#" + START_ID, "both"])
def test_inflight_start_retains_identity_for_close_without_reactivation(browser, monkeypatch, failed_copy):
    from botocore.exceptions import ClientError
    from agentcore_tools.browser_runtime import isolated_browser

    submit(browser, "start", START)
    message = receive(browser)
    starts, stopped = [], []
    terminated = False
    original = browser.table.update_item

    def update(**kwargs):
        if (kwargs["UpdateExpression"].startswith("SET provider_token") and
                failed_copy in {"both", kwargs["Key"]["arrived_at"]}):
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        return original(**kwargs)

    monkeypatch.setattr(browser.table, "update_item", update)

    def request(operation, arguments):
        starts.append((operation, arguments))
        assert close_execution(browser)["status"] == "pending"
        return {"session_token": "owned-token", "manifest": {"session_id": "provider-one"},
                "view_id": "view-one", "observations": []}

    def stop(provider_id):
        stopped.append(provider_id)
        return terminated

    monkeypatch.setattr(isolated_browser, "stop_session", stop)
    consumer = GatewayBrowserConsumer(browser, lambda envelope: envelope["claims"],
                                     GatewayBrowserProvider(browser, request=request))
    consumer.consume(message)
    consumer.consume(message)
    partition = browser._partition(CLAIMS, EXECUTION)
    assert len(starts) == 1 and starts[0][1]["deadline_at"] == 1900
    assert browser._read(partition, "OP#" + START_ID)["state"] == "unknown"
    assert browser._read(partition, "SESSION")["state"] == "closing"
    assert close_execution(browser)["status"] == "pending"
    if failed_copy == "both":
        assert stopped == []
        assert "provider_id" not in browser._read(partition, "SESSION")
        return
    session = browser._read(partition, "SESSION")
    assert session["provider_id"] == "provider-one" and session["provider_token"] == "owned-token"
    assert session["start_id"] == START_ID and session["generation"] == 1 and session["deadline_at"] == 1900
    assert stopped == ["provider-one"]
    terminated = True
    restarted = GatewayBrowser(table=browser.table, queue=browser.queue, clock=browser.clock)
    assert close_execution(restarted)["termination_confirmed"] is True
    closed = browser._read(partition, "SESSION")
    assert closed["state"] == "closed" and closed["cleanup_status"] == "confirmed"
    assert stopped == ["provider-one", "provider-one"] and len(starts) == 1


@pytest.mark.parametrize("field,value", [("start_id", "different"), ("generation", 2),
                                         ("execution_id", "different"), ("deadline_at", 1000),
                                         ("deadline_at", 2000)])
def test_start_boundary_refuses_changed_original_identity(browser, field, value):
    submit(browser, "start", START)
    starts = []
    provider = GatewayBrowserProvider(browser, request=lambda *args: starts.append(args))

    def dispatch(*args):
        browser.table.update_item(Key=browser._key(browser._partition(CLAIMS, EXECUTION), "SESSION"),
            UpdateExpression="SET #field = :value", ExpressionAttributeNames={"#field": field},
            ExpressionAttributeValues={":value": value})
        return provider(*args)

    GatewayBrowserConsumer(browser, lambda envelope: envelope["claims"], dispatch).consume(receive(browser))
    assert starts == []


def test_racing_browser_close_requires_independent_get_of_retained_provider(browser, monkeypatch):
    submit(browser, "start", START)
    starts, reads, stops = [], [], []
    terminated = False
    client = boto3.client

    class Control:
        def get_browser_session(self, **kwargs):
            reads.append(kwargs)
            return {"status": "TERMINATED" if terminated else "READY"}

        def stop_browser_session(self, **kwargs):
            stops.append(kwargs)
            return {}

    monkeypatch.setattr(boto3, "client", lambda service, **kwargs:
                        Control() if service == "bedrock-agentcore" else client(service, **kwargs))

    def request(operation, arguments):
        starts.append(operation)
        close_execution(browser)
        return {"session_token": "owned-token", "manifest": {"session_id": "provider-one"},
                "view_id": "view-one", "observations": []}

    GatewayBrowserConsumer(browser, lambda envelope: envelope["claims"],
                           GatewayBrowserProvider(browser, request=request)).consume(receive(browser))
    assert close_execution(browser)["status"] == "pending"
    assert len(reads) == 2 and len(stops) == 1
    assert all(item == {"browserIdentifier": "aws.browser.v1", "sessionId": "provider-one"}
               for item in reads + stops)
    terminated = True
    assert close_execution(browser)["provider_status"] == "TERMINATED"
    assert len(reads) == 3 and len(stops) == 1 and starts == ["start"]


def test_provider_start_cannot_reuse_original_under_foreign_owner(browser):
    submit(browser, "start", START)
    starts = []
    provider = GatewayBrowserProvider(browser, request=lambda *args: starts.append(args))
    foreign = {**CLAIMS, "principal": {**CLAIMS["principal"], "user_id": "foreign"}}
    with pytest.raises(HTTPException) as missing:
        provider("start", START, foreign)
    assert missing.value.status_code == 404 and starts == []


@pytest.mark.parametrize("field,value", [("request_id", "different"), ("execution_id", "different"),
                                         ("generation", 2), ("execution_deadline_at", 2000)])
def test_provider_boundary_refuses_changed_original_operation(browser, field, value):
    submit(browser, "start", START)
    starts = []
    provider = GatewayBrowserProvider(browser, request=lambda *args: starts.append(args))

    def dispatch(*args):
        browser.table.update_item(Key=browser._key(browser._partition(CLAIMS, EXECUTION), "OP#" + START_ID),
            UpdateExpression="SET #field = :value", ExpressionAttributeNames={"#field": field},
            ExpressionAttributeValues={":value": value})
        return provider(*args)

    GatewayBrowserConsumer(browser, lambda envelope: envelope["claims"], dispatch).consume(receive(browser))
    assert starts == []
