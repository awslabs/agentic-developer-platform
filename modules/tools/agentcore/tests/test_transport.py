"""Task SDK/host frames through shared handlers with mocked AWS, no Cyber imports."""

import base64
import hashlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import boto3
import pytest
from fastapi import HTTPException
from moto import mock_aws

from adp_tools.contracts import Authorization
from adp_tools.storage import serialize, task_ops_partition
from agentcore_tools import browser_http, code_interpreter, handler, websearch
from agentcore_tools.task_browser import TaskBrowser
from lib.task_host import TaskHost
from lib.task_run_client import TaskRunClient

ROLE = "arn:aws:iam::123456789012:role/task-worker"
ENDPOINT = "https://authority.example/dev/internal/v1/agent/task"
ROUTES = {
    "websearch.search": "https://api.example/dev/tools/websearch",
    **{f"code_interpreter.{name}": "https://api.example/dev/tools/code-interpreter" for name in ("start", "execute", "result", "file", "close")},
    "cyber.browser_start": "local:agentcore_tools.task_browser.TaskBrowser",
    "cyber.browser_close": "local:agentcore_tools.task_browser.TaskBrowser",
}


def operation_id():
    return str(uuid.uuid4())


def authorized():
    identity = {
        "task_id": "tsk_" + operation_id(), "invocation_id": operation_id(),
        "runtime_attempt_id": operation_id(), "generation": 1,
        "tenant": "tenant", "canonical_principal": "principal",
    }
    return Authorization.model_validate({
        "schema_version": "1.0", "identity": identity,
        "task": {"task_id": identity["task_id"], "scope": {"tenant": "tenant", "canonical_principal": "principal"},
                 "persona": "agent-task-investigator", "generation": 1, "version": 1,
                 "runtime_attempt_id": identity["runtime_attempt_id"], "state": "running",
                 "deadline_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                 "input_payload": {"inputs": {"url": "https://example.com"}}},
    })


def attempt(verified):
    identity = verified.identity
    return {"run": {key: getattr(identity, key) for key in ("task_id", "invocation_id", "generation")},
            "runtime_attempt_id": identity.runtime_attempt_id}


class Provider:
    def __init__(self):
        self.calls = []

    def start_code_interpreter_session(self, **kwargs):
        self.calls.append(("start", kwargs))
        return {"sessionId": "private-provider-id"}

    def invoke_code_interpreter(self, **kwargs):
        self.calls.append((kwargs["name"], kwargs))
        result = {"startCommandExecution": {"taskId": "private-task-id"},
                  "getTask": {"status": "completed", "output": "5\n"},
                  "readFiles": {"content": "table,5\n"}}[kwargs["name"]]
        return {"stream": iter([{"result": {"structuredContent": result}}])}

    def stop_code_interpreter_session(self, **kwargs):
        self.calls.append(("stop", kwargs))
        return {}


@pytest.fixture
def transport(monkeypatch):
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName="cyber-operations", KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}, {"AttributeName": "arrived_at", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST",
        )
        verified = authorized()
        grants = set(ROUTES)
        grants.add("code_interpreter.close")
        frozen = set(grants)
        current_persona = set(grants)
        calls = []
        artifacts = {}
        provider = Provider()
        monkeypatch.setenv("ADP_TOOLS_TABLE", "cyber-operations")
        monkeypatch.setenv("ADP_TOOLS_WORKER_ROLES", ROLE)
        monkeypatch.setenv("ADP_TASK_AUTHORITY_ENDPOINT", ENDPOINT)
        monkeypatch.setenv("ADP_WEBSEARCH_ENABLED", "true")
        monkeypatch.setenv("ADP_CODE_INTERPRETER_ENABLED", "true")
        monkeypatch.setenv("ADP_CODE_INTERPRETER_ID", "customResource-0123456789")
        monkeypatch.setenv("ADP_TASK_TOOL_ROUTES", json.dumps(ROUTES))
        monkeypatch.setenv("ADP_AGENT_CONTROL_ENDPOINT", "https://api.example/internal/v1/agent")

        def authorize(request, tool, cleanup=False):
            if request != attempt(verified) or tool not in grants or tool not in frozen or tool not in current_persona:
                raise HTTPException(403, "Task grant refused")
            return verified

        def artifact(content, content_type, digest):
            assert hashlib.sha256(content).hexdigest() == digest
            artifact_id = "art_" + str(uuid.UUID(bytes=hashlib.sha256(
                f"{verified.identity.task_id}:{content_type}:{digest}".encode()
            ).digest()[:16], version=4))
            artifacts[artifact_id] = content
            return {"schema_version": "1.0", "artifact_id": artifact_id, "content_type": content_type,
                    "content_sha256": digest, "version": 1, "expires_at": None}

        class Authority:
            def __init__(self, endpoint, headers, *, region):
                assert endpoint == ENDPOINT and region == "us-east-1"

            def authorize(self, *, attempt, tool, cleanup=False):
                return authorize(attempt, tool, cleanup)

            def put_run_artifact(self, *, attempt, content, content_type, digest):
                return SimpleNamespace(**artifact(content, content_type, digest))

            def close(self):
                pass

        monkeypatch.setattr(handler, "TaskAuthorityClient", Authority)
        original = code_interpreter.CodeInterpreter.__init__

        def init(self, repo, authority, identifier, **kwargs):
            original(self, repo, authority, identifier, provider=provider, **kwargs)

        monkeypatch.setattr(code_interpreter.CodeInterpreter, "__init__", init)

        def search(payload, *, before_search=None):
            if before_search:
                before_search()
            calls.append("paid_search")
            return {"status": "completed", "results": [{"url": "https://source.example/report", "title": "Source", "text": "Evidence"}],
                    "query_count": 1, "estimated_search_usd": 0.007}

        monkeypatch.setattr(websearch, "search", search)
        client = TaskRunClient()
        browser_calls = []

        def browser_request(operation, payload):
            browser_calls.append(operation)
            if operation == "start":
                return {"session_token": "private-browser-token", "session_open": True, "view_id": "view1", "observations": []}
            return {"session_open": False, "cleanup_status": "stopped"}

        browser = TaskBrowser(client, request=browser_request)
        client._local_tools["agentcore_tools.task_browser.TaskBrowser"] = browser

        def dispatch(action, body, **kwargs):
            if action == "tool-authorize":
                return authorize(body["attempt"], body["tool"], body.get("cleanup", False)).model_dump()
            if action == "artifact":
                return artifact(base64.b64decode(body["content_base64"]), body["content_type"], body["content_sha256"])
            route = kwargs["tool_endpoint"].split("/dev", 1)[1]
            event = {"httpMethod": "POST", "resource": route, "body": json.dumps(body),
                     "requestContext": {"identity": {"userArn": "arn:aws:sts::123456789012:assumed-role/task-worker/run"}}}
            response = handler.lambda_handler(event, None)
            if response["statusCode"] != 200:
                raise HTTPException(response["statusCode"], json.loads(response["body"])["message"])
            return json.loads(response["body"])

        monkeypatch.setattr(client, "_post", dispatch)
        host = TaskHost.__new__(TaskHost)
        host.client = client
        host._report_renderer = None
        yield SimpleNamespace(host=host, client=client, verified=verified, grants=grants, frozen=frozen,
                              persona=current_persona, provider=provider, calls=calls, browser_calls=browser_calls,
                              artifacts=artifacts, dynamodb=dynamodb, dispatch=dispatch)


def invoke(transport, tool, payload, *, operation=None, identifier=None, task_attempt=None):
    frame = {"type": "tool.request", "tool": tool, "request_id": identifier or operation_id(), "payload": payload}
    if operation:
        frame["tool"] = tool.rsplit(".", 1)[0] + "." + operation
    return transport.host._cyber(SimpleNamespace(task_id=transport.verified.identity.task_id),
                                 task_attempt or attempt(transport.verified), frame)


def test_non_cyber_task_uses_all_three_host_transport_and_artifact_integrity(transport):
    assert transport.verified.task["persona"] == "agent-task-investigator"
    search = invoke(transport, "websearch.search", {"query": "site"})
    assert search["operation_status"] == "confirmed" and transport.calls == ["paid_search"]
    started = invoke(transport, "code_interpreter.start", {})
    assert started["operation_status"] == "confirmed" and len(transport.provider.calls) == 1
    session = started["result"]["session_id"]
    execution = operation_id()
    pending = invoke(transport, "code_interpreter.execute", {"session_id": session, "code": "print(5)", "language": "python"}, identifier=execution)
    assert pending["operation_status"] == "pending"
    result = invoke(transport, "code_interpreter.result", {"session_id": session, "execution_id": execution})
    assert result["result"]["output"] == "5\n"
    file = invoke(transport, "code_interpreter.file", {"session_id": session, "path": "/tmp/table.csv"})
    assert file["operation_status"] == "confirmed"
    closed = invoke(transport, "code_interpreter.close", {"session_id": session})
    assert closed["result"]["status"] == "closed"
    browser = invoke(transport, "cyber.browser_start", {"url": "https://example.com"})
    assert browser["operation_status"] == "confirmed" and transport.browser_calls == ["start"]
    for result in (search, started, result, file, closed, browser):
        artifact = result["artifact"]
        assert hashlib.sha256(transport.artifacts[artifact["artifact_id"]]).hexdigest() == artifact["content_sha256"]
    assert "private-browser-token" not in json.dumps(browser)


@pytest.mark.parametrize("revocation", ["grants", "frozen", "persona"])
def test_current_and_frozen_grants_refuse_before_provider(transport, revocation):
    getattr(transport, revocation).discard("websearch.search")
    with pytest.raises(HTTPException) as error:
        invoke(transport, "websearch.search", {"query": "site"})
    assert error.value.status_code == 403 and not transport.calls


def test_foreign_task_and_alias_do_not_widen_frozen_grants(transport):
    forged = attempt(transport.verified)
    forged["run"]["task_id"] = "tsk_" + operation_id()
    with pytest.raises(HTTPException):
        invoke(transport, "websearch.search", {"query": "site"}, task_attempt=forged)
    transport.grants.add("browser.start")
    with pytest.raises(Exception):
        invoke(transport, "browser.start", {"url": "https://example.com"})
    assert not transport.calls and not transport.browser_calls


def test_pre_move_receipt_and_unknown_claim_never_replay_paid_search(transport):
    identity = transport.verified.identity
    payload = {"query": "site"}
    from adp_tools.storage import payload_digest
    digest = payload_digest({"operation": "search", "payload": payload, "attempt": identity.runtime_attempt_id})
    partition = task_ops_partition(identity.task_id)
    key = "CYBER_OP#" + digest
    receipt = {"schema_version": "1.0", "task_id": identity.task_id, "operation_id": operation_id(),
               "operation_status": "confirmed", "result": {"status": "completed"},
               "artifact": {"artifact_id": "art_legacy", "content_type": "application/json",
                            "content_sha256": "a" * 64, "byte_length": 10}}
    transport.dynamodb.put_item(TableName="cyber-operations", Item=serialize({
        "event_id": partition, "arrived_at": key, "request_digest": digest,
        "runtime_attempt_id": identity.runtime_attempt_id, "receipt": receipt,
    }))
    replay = invoke(transport, "websearch.search", payload)
    assert replay["operation_status"] == "confirmed" and not transport.calls
    transport.dynamodb.delete_item(TableName="cyber-operations", Key=serialize({"event_id": partition, "arrived_at": key}))
    identifier = operation_id()
    first = invoke(transport, "websearch.search", payload, identifier=identifier)
    assert first["operation_status"] == "confirmed" and transport.calls == ["paid_search"]
    replay = invoke(transport, "websearch.search", payload, identifier=identifier)
    assert replay["operation_status"] == "confirmed" and transport.calls == ["paid_search"]
    transport.dynamodb.update_item(TableName="cyber-operations", Key=serialize({"event_id": partition, "arrived_at": key}),
                                   UpdateExpression="REMOVE receipt")
    unknown = invoke(transport, "websearch.search", payload, identifier=identifier)
    assert unknown["operation_status"] == "unknown" and transport.calls == ["paid_search"]


def test_non_cyber_http_browser_host_queue_and_owned_cleanup(transport, monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("BROWSER_OPERATIONS_TABLE", "browser-operations")
    monkeypatch.setenv("CYBER_TOOLS_WORKER_ROLES", ROLE)
    monkeypatch.setenv("ADP_TASK_BROWSER_HTTP_ENABLED", "true")
    transport.dynamodb.create_table(
        TableName="browser-operations",
        KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}, {"AttributeName": "arrived_at", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST",
    )
    queue = boto3.client("sqs", region_name="us-east-1")
    url = queue.create_queue(QueueName="shared-browser.fifo", Attributes={"FifoQueue": "true"})["QueueUrl"]
    monkeypatch.setenv("BROWSER_QUEUE_URL", url)
    table = boto3.resource("dynamodb", region_name="us-east-1").Table("browser-operations")
    calls = []

    def browser_request(operation, payload):
        calls.append(operation)
        if operation == "start":
            return {"session_token": "private-provider-token", "session_open": True, "view_id": "view1", "observations": []}
        return {"session_open": False, "cleanup_status": "stopped"}

    task_attempt = attempt

    class Authority:
        def __init__(self, *args, **kwargs):
            pass

        def authorize(self, *, attempt, tool, cleanup=False):
            if (attempt != task_attempt(transport.verified) or tool not in transport.grants
                    or tool not in transport.frozen or tool not in transport.persona):
                raise HTTPException(403, "Task grant refused")
            return transport.verified

        def put_run_artifact(self, *, attempt, content, content_type, digest):
            assert hashlib.sha256(content).hexdigest() == digest
            artifact_id = "art_" + str(uuid.UUID(bytes=hashlib.sha256(
                f"{attempt.task_id}:{content_type}:{digest}".encode()
            ).digest()[:16], version=4))
            transport.artifacts[artifact_id] = content
            return SimpleNamespace(artifact_id=artifact_id, content_type=content_type, content_sha256=digest)

        def close(self):
            pass

    monkeypatch.setattr(browser_http, "TaskAuthorityClient", Authority)
    consumer = browser_http.BrowserConsumer(table, request=browser_request)
    original_dispatch = transport.client._post

    def http_dispatch(action, body, **kwargs):
        if kwargs.get("tool_endpoint", "").endswith("/tools/browser"):
            response = original_dispatch(action, body, **kwargs)
            if response["operation_status"] == "pending":
                messages = queue.receive_message(QueueUrl=url).get("Messages", [])
                for message in messages:
                    consumer.consume(message["Body"])
                    queue.delete_message(QueueUrl=url, ReceiptHandle=message["ReceiptHandle"])
                response = original_dispatch(action, body, **kwargs)
            return response
        return original_dispatch(action, body, **kwargs)

    monkeypatch.setattr(transport.client, "_post", http_dispatch)
    transport.client._tool_routes["cyber.browser_start"] = "https://api.example/dev/tools/browser"
    transport.client._tool_routes["cyber.browser_close"] = "https://api.example/dev/tools/browser"
    transport.client._stopping = False
    transport.client._deadline = time.monotonic() + 200
    transport.client._clock = time.monotonic
    started = invoke(transport, "cyber.browser_start", {"url": "https://example.com"})
    assert started["operation_status"] == "confirmed" and calls == ["start"]
    session = started["result"]["session_id"]
    closed = invoke(transport, "cyber.browser_close", {"session_id": session})
    assert closed["result"]["cleanup_status"] == "stopped" and calls == ["start", "close"]
    assert hashlib.sha256(transport.artifacts[started["artifact"]["artifact_id"]]).hexdigest() == started["artifact"]["content_sha256"]
    assert "private-provider-token" not in json.dumps(started)


def test_lost_paid_response_and_restart_reuse_claim_without_repeating_query(transport, monkeypatch):
    attempts = []

    def lost(payload, *, before_search=None):
        before_search()
        attempts.append(payload["query"])
        raise TimeoutError("response lost")

    monkeypatch.setattr(websearch, "search", lost)
    identifier = operation_id()
    with pytest.raises(HTTPException) as error:
        invoke(transport, "websearch.search", {"query": "site"}, identifier=identifier)
    assert error.value.status_code == 503 and attempts == ["site"]
    replay = invoke(transport, "websearch.search", {"query": "site"}, identifier=identifier)
    assert replay["operation_status"] == "unknown" and replay["result"]["potential_query_count"] == 1
    alias = invoke(transport, "websearch.search", {"query": "site"})
    assert alias["operation_status"] == "unknown" and attempts == ["site"]


def test_code_cleanup_after_revoked_grant_remains_owned(transport):
    started = invoke(transport, "code_interpreter.start", {})
    session = started["result"]["session_id"]
    transport.grants.discard("code_interpreter.start")
    transport.grants.discard("code_interpreter.execute")
    with pytest.raises(HTTPException):
        invoke(transport, "code_interpreter.execute", {"session_id": session, "code": "print(5)", "language": "python"})
    assert len(transport.provider.calls) == 1
    closed = invoke(transport, "code_interpreter.close", {"session_id": session})
    assert closed["operation_status"] == "confirmed" and transport.provider.calls[-1][0] == "stop"


@pytest.mark.parametrize("tool,payload", [
    ("code_interpreter.start", {}),
    ("cyber.browser_start", {"url": "https://example.com"}),
])
def test_non_cyber_missing_grant_refuses_before_any_provider(transport, tool, payload):
    transport.frozen.discard(tool)
    with pytest.raises(HTTPException) as error:
        invoke(transport, tool, payload)
    assert error.value.status_code == 403
    assert not transport.provider.calls and not transport.browser_calls
