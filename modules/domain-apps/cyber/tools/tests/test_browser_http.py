import base64
import io
import json
import time
import sys
from pathlib import Path
import uuid
from types import SimpleNamespace

import boto3
import pytest
from cyber_tools import browser_http
from moto import mock_aws
from test_service_boundary import CALLER, ENDPOINT, PROOFS, ROLE, attempt, authorization

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "agent-factory/agent-worker-image"))


@pytest.fixture
def setup(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setenv("ADP_TASK_AUTHORITY_ENDPOINT", ENDPOINT)
        monkeypatch.setenv("BROWSER_OPERATIONS_TABLE", "browser-ops")
        monkeypatch.setenv("ADP_TASK_BROWSER_HTTP_ENABLED", "true")
        monkeypatch.setenv("CYBER_TOOLS_WORKER_ROLES", ROLE)
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamodb.create_table(
            TableName="browser-ops",
            KeySchema=[
                {"AttributeName": "event_id", "KeyType": "HASH"},
                {"AttributeName": "arrived_at", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "event_id", "AttributeType": "S"},
                {"AttributeName": "arrived_at", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        queue = boto3.client("sqs", region_name="us-east-1")
        url = queue.create_queue(
            QueueName="browser-test.fifo", Attributes={"FifoQueue": "true"}
        )["QueueUrl"]
        monkeypatch.setenv("BROWSER_QUEUE_URL", url)
        current = [authorization()]

        class Authority:
            def __init__(self, endpoint, headers, *, region):
                assert endpoint == ENDPOINT and region == "us-east-1"
                assert {key.lower(): value for key, value in headers.items()} == {
                    key.lower(): value for key, value in PROOFS.items()
                }

            def authorize(self, *, attempt, tool, cleanup=False):
                if current[0] is None:
                    from fastapi import HTTPException

                    raise HTTPException(403, "Task grant revoked")
                if current[0].task.get("state") == "cancelled" and not cleanup:
                    from fastapi import HTTPException

                    raise HTTPException(403, "Task cancelled")
                if current[0].identity.model_dump() != {
                    **attempt["run"],
                    "runtime_attempt_id": attempt["runtime_attempt_id"],
                    "tenant": current[0].identity.tenant,
                    "canonical_principal": current[0].identity.canonical_principal,
                }:
                    from fastapi import HTTPException

                    raise HTTPException(403, "Task authority changed")
                if tool not in {
                    "cyber.browser_start",
                    "cyber.browser_step",
                    "cyber.browser_close",
                    "cyber.browser_inspect",
                    "cyber.cancel_jobs",
                }:
                    raise AssertionError(tool)
                return current[0]

            def put_run_artifact(self, *, attempt, content, content_type, digest):
                return SimpleNamespace(
                    artifact_id="art_" + str(uuid.uuid4()),
                    content_type=content_type,
                    content_sha256=digest,
                )

            def close(self):
                pass

        monkeypatch.setattr(browser_http, "TaskAuthorityClient", Authority)
        provider_calls = []

        def provider(operation, payload):
            provider_calls.append((operation, payload))
            if operation == "start":
                from PIL import Image

                image = io.BytesIO()
                Image.new("RGB", (32, 32), "white").save(image, format="PNG")
                return {
                    "session_token": "private-provider-token",
                    "session_open": True,
                    "view_id": "view1",
                    "manifest": {"session_id": "provider-s1"},
                    "observations": [
                        {
                            "id": "obs1",
                            "dom_snapshot": "<p>ok</p>",
                            "screenshot_base64": base64.b64encode(
                                image.getvalue()
                            ).decode(),
                        }
                    ],
                }
            if operation == "step":
                return {"session_open": True, "view_id": "view2", "observations": []}
            return {"session_open": False, "cleanup_status": "stopped"}

        consumer = browser_http.BrowserConsumer(table, request=provider)

        def consume():
            messages = queue.receive_message(QueueUrl=url).get("Messages", [])
            for message in messages:
                consumer.consume(message["Body"])
                queue.delete_message(
                    QueueUrl=url, ReceiptHandle=message["ReceiptHandle"]
                )

        def invoke(body, caller=CALLER):
            event = {
                "httpMethod": "POST",
                "resource": "/tools/browser",
                "body": json.dumps(body),
                "headers": PROOFS,
                "requestContext": {"identity": {"userArn": caller}},
            }
            result = browser_http.lambda_handler(event, None)
            return result["statusCode"], json.loads(result["body"])

        yield current, provider_calls, consumer, consume, invoke


def body(verified, operation, payload, operation_id=None):
    return {
        "schema_version": "1.0",
        "attempt": attempt(verified),
        "operation_id": operation_id or str(uuid.uuid4()),
        "operation": operation,
        "payload": payload,
    }


def test_http_browser_through_task_host_with_artifacts_and_cleanup(setup, monkeypatch):
    from lib.task_run_client import TaskRunClient

    current, calls, _consumer, consume, invoke = setup
    host = object.__new__(TaskRunClient)
    host._tool_routes = {
        "cyber." + name: "https://tools.example/dev/tools/browser"
        for name in (
            "browser_start",
            "browser_step",
            "browser_inspect",
            "browser_close",
            "browser_cleanup",
        )
    }
    host._publication_tool = None
    host._validation_tool = None
    host._workspace_tools = None
    host._stopping = False
    host._deadline = time.monotonic() + 200
    host._clock = time.monotonic
    host._post = lambda action, payload, **kwargs: invoke(payload)[1]
    monkeypatch.setattr("lib.task_run_client.time.sleep", lambda seconds: consume())
    start = host.tool(
        "cyber.browser_start",
        body(current[0], "browser_start", {"url": "https://example.com"}),
    )
    assert start["operation_status"] == "confirmed"
    assert "private-provider-token" not in str(start)
    sid = start["result"]["session_id"]
    assert start["result"]["evidence_artifacts"] and start["artifact"][
        "artifact_id"
    ].startswith("art_")
    ownership = browser_http.owned_sessions(
        setup[2].table,
        (current[0].identity.task_id, current[0].identity.runtime_attempt_id),
    )
    assert len(ownership) == 1 and ownership[0]["session_id"] == sid
    assert ownership[0]["expires_at"] - ownership[0]["created"] == 720
    screenshot = host.tool(
        "cyber.browser_inspect",
        body(
            current[0], "browser_inspect", {"session_id": sid, "section": "screenshot"}
        ),
    )
    assert screenshot["result"]["image"]["media_type"] == "image/jpeg"
    step = host.tool(
        "cyber.browser_step",
        body(
            current[0],
            "browser_step",
            {"session_id": sid, "view_id": "view1", "action": "screenshot"},
        ),
    )
    assert step["result"]["view_id"] == "view2"
    inspected = host.tool(
        "cyber.browser_inspect",
        body(current[0], "browser_inspect", {"session_id": sid, "section": "dom"}),
    )
    assert "<p>ok</p>" not in inspected["result"].get("text", "")
    closed = host.tool(
        "cyber.browser_close", body(current[0], "browser_close", {"session_id": sid})
    )
    assert closed["result"]["cleanup_status"] == "stopped"
    assert [operation for operation, _ in calls] == ["start", "step", "close"]
    from cyber_tools.task_report import render_report

    steps = [
        {
            "tool": "cyber." + name,
            "artifact": receipt.get("artifact"),
            "operation_status": receipt["operation_status"],
            "result": receipt["result"],
            "started_at": "2026-09-27T00:00:00Z",
            "finished_at": "2026-09-27T00:00:01Z",
        }
        for name, receipt in (
            ("browser_start", start),
            ("browser_inspect", screenshot),
            ("browser_step", step),
            ("browser_inspect", inspected),
            ("browser_close", closed),
        )
    ]
    report = render_report(
        report={
            "summary": "Verdict: inconclusive.",
            "findings": [
                {
                    "statement": "Observed a live page",
                    "evidence_refs": [start["artifact"]["artifact_id"]],
                }
            ],
        },
        context={
            "task_id": current[0].identity.task_id,
            "started_at": "2026-09-27T00:00:00Z",
            "inputs": {"url": "https://example.com"},
            "steps": steps,
        },
    )
    assert (
        b"Observed a live page" in report["content"]
        and b"browser close" in report["content"]
    )


def test_cross_task_and_disallowed_action_never_reach_provider(setup):
    current, calls, _consumer, consume, invoke = setup
    start = body(current[0], "browser_start", {"url": "https://example.com"})
    status, receipt = invoke(start)
    assert status == 200, receipt
    assert receipt["operation_status"] == "pending"
    consume()
    sid = invoke(start)[1]["result"]["session_id"]
    denied = body(
        current[0],
        "browser_step",
        {
            "session_id": sid,
            "view_id": "view1",
            "action": "navigate",
            "url": "http://127.0.0.1/",
        },
    )
    invoke(denied)
    consume()
    assert invoke(denied)[1]["operation_status"] == "rejected"
    current[0] = authorization()
    other = body(current[0], "browser_close", {"session_id": sid})
    invoke(other)
    consume()
    assert invoke(other)[1]["operation_status"] == "rejected"
    assert [operation for operation, _ in calls] == ["start"]


def test_claim_survives_restart_and_duplicate_id_never_replays(setup):
    current, calls, consumer, consume, invoke = setup
    start = body(current[0], "browser_start", {"url": "https://example.com"})
    status, receipt = invoke(start)
    assert status == 200, receipt
    assert receipt["operation_status"] == "pending"
    assert invoke(start)[1]["operation_status"] == "pending"
    consume()
    assert invoke(start)[1]["operation_status"] == "confirmed"
    restart = browser_http.BrowserConsumer(consumer.table, request=consumer.request)
    assert invoke(start)[1]["operation_status"] == "confirmed"
    another_id = {**start, "operation_id": str(uuid.uuid4())}
    assert invoke(another_id)[0] == 409
    assert [operation for operation, _ in calls] == ["start"]
    changed = {**start, "payload": {"url": "https://other.example/"}}
    assert invoke(changed)[0] == 409
    denied = {
        **start,
        "operation_id": str(uuid.uuid4()),
        "payload": {"url": "http://127.0.0.1/"},
    }
    invoke(denied)
    restart.consume(json.dumps({"body": denied, "proofs": PROOFS}))
    assert [operation for operation, _ in calls] == ["start"]


def test_gateway_rejects_unknown_caller_and_operations(setup):
    current, calls, _, _, invoke = setup
    payload = body(current[0], "browser_start", {"url": "https://example.com"})
    assert (
        invoke(payload, caller="arn:aws:sts::123456789012:assumed-role/other/pod")[0]
        == 403
    )
    assert invoke({**payload, "operation": "common_crawl_scan"})[0] == 422
    assert (
        invoke(
            {**payload, "payload": {"url": "https://example.com", "tenant": "other"}}
        )[0]
        == 422
    )
    assert not calls


def test_restart_cleanup_stops_only_owned_provider_session(setup, monkeypatch):
    current, calls, consumer, consume, invoke = setup
    start = body(current[0], "browser_start", {"url": "https://example.com"})
    invoke(start)
    consume()
    session_id = invoke(start)[1]["result"]["session_id"]
    restarted = browser_http.BrowserConsumer(consumer.table, request=consumer.request)
    stopped = []
    import isolated_browser

    monkeypatch.setattr(
        isolated_browser,
        "stop_session",
        lambda provider_id: stopped.append(provider_id) or True,
    )
    close = body(current[0], "browser_close", {"session_id": session_id})
    invoke(close)
    restarted.consume(json.dumps({"body": close, "proofs": PROOFS}))
    assert invoke(close)[1]["operation_status"] == "unknown"
    cleanup = body(current[0], "cancel_jobs", {})
    invoke(cleanup)
    restarted.consume(json.dumps({"body": cleanup, "proofs": PROOFS}))
    assert invoke(cleanup)[1]["result"] == {"status": "confirmed", "pending_jobs": []}
    assert stopped == ["provider-s1"]
    assert [operation for operation, _ in calls] == ["start"]


def test_restricted_scope_and_revocation_do_not_start_browser(setup):
    current, calls, _, consume, invoke = setup
    current[0].task["input_payload"]["inputs"]["browser_scope"] = "host"
    start = body(
        current[0],
        "browser_start",
        {"url": "https://example.com", "scope": "observed_external"},
    )
    invoke(start)
    consume()
    assert invoke(start)[1]["operation_status"] == "rejected"
    current[0] = None
    assert invoke({**start, "operation_id": str(uuid.uuid4())})[0] == 403
    assert not calls


def test_unknown_step_never_replays_even_after_consumer_restart(setup):
    current, calls, consumer, consume, invoke = setup
    start = body(current[0], "browser_start", {"url": "https://example.com"})
    invoke(start)
    consume()
    session_id = invoke(start)[1]["result"]["session_id"]

    def uncertain(operation, payload):
        calls.append((operation, payload))
        raise TimeoutError()

    consumer.tools[
        (current[0].identity.task_id, current[0].identity.runtime_attempt_id)
    ].request = uncertain
    step = body(
        current[0],
        "browser_step",
        {"session_id": session_id, "view_id": "view1", "action": "scroll"},
    )
    invoke(step)
    consume()
    assert invoke(step)[1]["operation_status"] == "unknown"
    restarted = browser_http.BrowserConsumer(consumer.table, request=uncertain)
    restarted.consume(json.dumps({"body": step, "proofs": PROOFS}))
    assert len(calls) == 2


def test_unknown_start_with_lost_artifact_still_allows_owned_cleanup(
    setup, monkeypatch
):
    current, calls, consumer, consume, invoke = setup

    def unavailable(*args, **kwargs):
        raise TimeoutError()

    monkeypatch.setattr(
        browser_http.TaskAuthorityClient, "put_run_artifact", unavailable
    )
    start = body(current[0], "browser_start", {"url": "https://example.com"})
    invoke(start)
    consume()
    assert invoke(start)[1]["operation_status"] == "unknown"
    restarted = browser_http.BrowserConsumer(consumer.table, request=consumer.request)
    stopped = []
    import isolated_browser

    monkeypatch.setattr(
        isolated_browser,
        "stop_session",
        lambda provider_id: stopped.append(provider_id) or True,
    )
    cleanup = body(current[0], "cancel_jobs", {})
    invoke(cleanup)
    restarted.consume(json.dumps({"body": cleanup, "proofs": PROOFS}))
    assert invoke(cleanup)[1]["result"]["pending_jobs"] == []
    assert stopped == ["provider-s1"] and [operation for operation, _ in calls] == [
        "start"
    ]


def test_revocation_between_enqueue_and_dispatch_refuses_provider(setup):
    current, calls, consumer, consume, invoke = setup
    start = body(current[0], "browser_start", {"url": "https://example.com"})
    assert invoke(start)[1]["operation_status"] == "pending"
    current[0] = None
    consume()
    assert (
        browser_http.read(consumer.table, start["operation_id"])["receipt"][
            "operation_status"
        ]
        == "rejected"
    )
    assert not calls


def test_cancelled_task_closes_owned_session_only(setup):
    current, calls, _consumer, consume, invoke = setup
    start = body(current[0], "browser_start", {"url": "https://example.com"})
    invoke(start)
    consume()
    session_id = invoke(start)[1]["result"]["session_id"]
    current[0].task["state"] = "cancelled"
    step = body(
        current[0],
        "browser_step",
        {"session_id": session_id, "view_id": "view1", "action": "scroll"},
    )
    assert invoke(step)[0] == 403
    cleanup = body(current[0], "cancel_jobs", {})
    invoke(cleanup)
    consume()
    assert invoke(cleanup)[1]["result"]["pending_jobs"] == []
    assert [operation for operation, _ in calls] == ["start", "close"]


def test_running_claim_after_process_loss_is_unknown_not_reexecuted(setup, monkeypatch):
    current, calls, consumer, _, invoke = setup
    start = body(current[0], "browser_start", {"url": "https://example.com"})
    invoke(start)
    consumer.table.update_item(
        Key=browser_http.key(start["operation_id"]),
        UpdateExpression="SET #state = :running",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={":running": "running"},
    )
    restarted = browser_http.BrowserConsumer(consumer.table, request=consumer.request)
    restarted.consume(json.dumps({"body": start, "proofs": PROOFS}))
    created = browser_http.read(consumer.table, start["operation_id"])["created"]
    monkeypatch.setattr(browser_http.time, "time", lambda: int(created) + 241)
    assert invoke(start)[1]["operation_status"] == "unknown"
    assert not calls


def test_start_lost_before_provider_id_does_not_claim_cleanup_success(setup):
    current, calls, consumer, consume, invoke = setup

    def lost_start(operation, payload):
        calls.append((operation, payload))
        raise TimeoutError()

    consumer.request = lost_start
    start = body(current[0], "browser_start", {"url": "https://example.com"})
    invoke(start)
    consume()
    assert invoke(start)[1]["operation_status"] == "unknown"
    restarted = browser_http.BrowserConsumer(consumer.table, request=lost_start)
    cleanup = body(current[0], "cancel_jobs", {})
    invoke(cleanup)
    restarted.consume(json.dumps({"body": cleanup, "proofs": PROOFS}))
    result = invoke(cleanup)[1]
    assert result["operation_status"] == "pending"
    assert result["result"]["pending_jobs"] == ["START#" + start["operation_id"]]
    assert len(calls) == 1


def test_cleanup_retries_pending_stop_with_same_operation_id(setup, monkeypatch):
    current, calls, consumer, consume, invoke = setup
    start = body(current[0], "browser_start", {"url": "https://example.com"})
    invoke(start)
    consume()
    restarted = browser_http.BrowserConsumer(consumer.table, request=consumer.request)
    import isolated_browser

    attempts = []
    monkeypatch.setattr(
        isolated_browser,
        "stop_session",
        lambda sid: attempts.append(sid) or len(attempts) > 1,
    )
    cleanup = body(current[0], "cancel_jobs", {})
    envelope = json.dumps({"body": cleanup, "proofs": PROOFS})
    invoke(cleanup)
    restarted.consume(envelope)
    assert invoke(cleanup)[1]["operation_status"] == "pending"
    restarted.consume(envelope)
    assert invoke(cleanup)[1]["result"] == {"status": "confirmed", "pending_jobs": []}
    assert attempts == ["provider-s1", "provider-s1"]
    assert not restarted.tools
    new_start = body(
        current[0], "browser_start", {"url": "https://example.com", "profile": "mobile"}
    )
    invoke(new_start)
    restarted.consume(json.dumps({"body": new_start, "proofs": PROOFS}))
    assert invoke(new_start)[1]["operation_status"] == "rejected"
    assert [name for name, _ in calls] == ["start"]


def test_expired_unknown_start_cleanup_finishes_without_claiming_early_closure(
    setup, monkeypatch
):
    current, calls, consumer, consume, invoke = setup

    def lost(operation, payload):
        raise TimeoutError()

    consumer.request = lost
    start = body(current[0], "browser_start", {"url": "https://example.com"})
    invoke(start)
    consume()
    cleanup = body(current[0], "cancel_jobs", {})
    invoke(cleanup)
    consume()
    assert invoke(cleanup)[1]["operation_status"] == "pending"
    owner = (current[0].identity.task_id, current[0].identity.runtime_attempt_id)
    row = browser_http.owned_sessions(consumer.table, owner)[0]
    consumer.table.update_item(
        Key={"event_id": row["event_id"], "arrived_at": row["arrived_at"]},
        UpdateExpression="SET expires_at=:expiry",
        ExpressionAttributeValues={":expiry": int(time.time()) - 1},
    )
    consumer.consume(json.dumps({"body": cleanup, "proofs": PROOFS}))
    assert invoke(cleanup)[1]["operation_status"] == "confirmed"


def test_queued_claim_recovers_a_failed_queue_publish_without_replaying_browser(
    setup, monkeypatch
):
    current, calls, consumer, consume, invoke = setup
    real_client = browser_http.boto3.client
    failed = [False]

    class Queue:
        def send_message(self, **kw):
            if not failed[0]:
                failed[0] = True
                raise TimeoutError()
            return real_client("sqs").send_message(**kw)

    monkeypatch.setattr(
        browser_http.boto3,
        "client",
        lambda service, *a, **kw: Queue()
        if service == "sqs"
        else real_client(service, *a, **kw),
    )
    start = body(current[0], "browser_start", {"url": "https://example.com"})
    assert invoke(start)[0] == 503
    assert invoke(start)[1]["operation_status"] == "pending"
    consume()
    assert invoke(start)[1]["operation_status"] == "confirmed"
    assert [name for name, _ in calls] == ["start"]


def test_host_polls_owned_cleanup_after_task_deadline_and_cancellation(
    setup, monkeypatch
):
    from lib.task_run_client import TaskRunClient

    current, calls, consumer, consume, invoke = setup
    host = object.__new__(TaskRunClient)
    host._tool_routes = {
        "cyber.browser_cleanup": "https://tools.example/dev/tools/browser"
    }
    host._publication_tool = host._validation_tool = host._workspace_tools = None
    host._stopping = True
    host._deadline = time.monotonic() - 1
    host._clock = time.monotonic
    host._post = lambda action, payload, **kwargs: invoke(payload)[1]
    monkeypatch.setattr("lib.task_run_client.time.sleep", lambda seconds: consume())
    assert (
        host.tool("cyber.browser_cleanup", body(current[0], "cancel_jobs", {}))[
            "operation_status"
        ]
        == "confirmed"
    )
