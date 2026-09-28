"""Route, Task authority and durable AgentCore operation contracts (no live AWS)."""

import hashlib
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from fastapi import HTTPException
from moto import mock_aws

from adp_tools.contracts import Authorization
from cyber_tools import handler
from cyber_tools import code_interpreter as module


def identifier():
    return str(uuid.uuid4())


class Provider:
    def __init__(self):
        self.calls = []

    def start_code_interpreter_session(self, **kwargs):
        self.calls.append(("start", kwargs))
        return {"sessionId": "private-provider-id"}

    def invoke_code_interpreter(self, **kwargs):
        self.calls.append((kwargs["name"], kwargs))
        name = kwargs["name"]
        result = {"startCommandExecution": {"taskId": "private-task-id"},
                  "getTask": {"taskStatus": "completed", "stdout": "5\n", "exitCode": 0},
                  "executeCommand": {"content": "table,5\n"}}[name]
        return {"stream": iter([{"result": {"structuredContent": result}}])}

    def stop_code_interpreter_session(self, **kwargs):
        self.calls.append(("stop", kwargs))
        return {}


@pytest.fixture
def route(monkeypatch):
    with mock_aws():
        db = boto3.client("dynamodb", region_name="us-east-1")
        db.create_table(TableName="test-ops", KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"},
                                                    {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
                        AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"},
                                              {"AttributeName": "arrived_at", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")
        ident = {"task_id": "tsk_" + identifier(), "invocation_id": identifier(), "runtime_attempt_id": identifier(),
                 "generation": 1, "tenant": "tenant", "canonical_principal": "owner"}
        grants = {"code_interpreter." + operation for operation in module.OPERATIONS}
        verified = Authorization.model_validate({"schema_version": "1.0", "identity": ident,
            "task": {"task_id": ident["task_id"], "scope": {"tenant": "tenant", "canonical_principal": "owner"},
                     "runtime_attempt_id": ident["runtime_attempt_id"], "generation": 1,
                     "version": 1, "state": "running", "persona": "agent-task-cyber",
                     "deadline_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(), "input_payload": {"inputs": {}}}})
        provider = Provider()
        artifacts = {}

        class Authority:
            def __init__(self, *args, **kwargs):
                pass

            def authorize(self, *, attempt, tool, cleanup=False):
                if (tool not in grants and not (cleanup and tool in {"code_interpreter.close", "code_interpreter.cancel_jobs"})) or attempt["run"]["task_id"] != ident["task_id"] or attempt["runtime_attempt_id"] != ident["runtime_attempt_id"]:
                    raise HTTPException(403, "Task grant refused")
                return verified

            def put_run_artifact(self, *, attempt, content, content_type, digest):
                assert content_type == "application/json" and hashlib.sha256(content).hexdigest() == digest
                artifact_id = "art_" + digest[:32]
                artifacts[artifact_id] = content
                return SimpleNamespace(artifact_id=artifact_id)

            def close(self):
                pass

        original = module.CodeInterpreter.__init__

        def init(self, repo, authority, resource, **kwargs):
            original(self, repo, authority, resource, provider=provider, **kwargs)

        monkeypatch.setattr(module.CodeInterpreter, "__init__", init)
        monkeypatch.setattr(handler, "TaskAuthorityClient", Authority)
        monkeypatch.setattr(handler.CyberBackends, "_client", lambda self, service: db)
        monkeypatch.setenv("CYBER_TOOLS_WORKER_ROLES", "arn:aws:iam::123456789012:role/worker")
        monkeypatch.setenv("CYBER_TOOLS_TABLE", "test-ops")
        monkeypatch.setenv("ADP_CODE_INTERPRETER_ID", "customResource-0123456789")
        monkeypatch.setenv("ADP_CODE_INTERPRETER_ENABLED", "true")

        def send(operation, payload, *, run=None, operation_id=None):
            identity = run or ident
            request = {"schema_version": "1.0", "attempt": {"run": {key: identity[key] for key in ("task_id", "invocation_id", "generation")},
                "runtime_attempt_id": identity["runtime_attempt_id"]}, "operation_id": operation_id or identifier(), "operation": operation, "payload": payload}
            response = handler.lambda_handler({"httpMethod": "POST", "resource": "/tools/code-interpreter", "body": json.dumps(request),
                "requestContext": {"identity": {"userArn": "arn:aws:sts::123456789012:assumed-role/worker/run"}}}, None)
            return response["statusCode"], json.loads(response["body"])

        send.verified = verified
        yield send, grants, provider, artifacts, ident


def test_task_to_route_to_artifact_and_provider_lifecycle(route):
    send, grants, provider, artifacts, identity = route
    status, started = send("start", {})
    assert status == 200 and started["operation_status"] == "confirmed"
    session = started["result"]["session_id"]
    assert "private-provider-id" not in json.dumps(started)
    assert 1 <= provider.calls[0][1]["sessionTimeoutSeconds"] <= 900
    execution = identifier()
    code = "print(sum([2, 3]))"
    status, submitted = send("execute", {"session_id": session, "code": code, "language": "python"}, operation_id=execution)
    assert status == 200 and submitted["operation_status"] == "pending"
    assert provider.calls[-1][1]["arguments"]["command"] == "python -c 'print(sum([2, 3]))'"
    _, result = send("result", {"session_id": session, "execution_id": execution})
    assert result["operation_status"] == "confirmed" and result["result"]["stdout"] == "5\n"
    assert hashlib.sha256(artifacts[result["artifact"]["artifact_id"]]).hexdigest() == result["artifact"]["content_sha256"]
    _, file = send("file", {"session_id": session, "path": "/tmp/table.csv"})
    assert file["result"]["contents"][0]["structuredContent"]["content"] == "table,5\n"
    _, closed = send("close", {"session_id": session})
    assert closed["result"]["status"] == "closed"
    assert provider.calls[-1][0] == "stop"


def test_real_task_host_consumes_route_receipts_and_artifacts(route):
    sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "agent-factory/agent-worker-image"))
    from lib.task_host import TaskHost
    from lib.task_protocol import validate_child_frame

    send, grants, provider, artifacts, identity = route
    assignment = SimpleNamespace(**identity)
    attempt = {"run": {key: identity[key] for key in ("task_id", "invocation_id", "generation")},
               "runtime_attempt_id": identity["runtime_attempt_id"]}

    class Client:
        def tool(self, name, body):
            assert name == "code_interpreter." + body["operation"]
            assert body["attempt"] == attempt
            status, receipt = send(body["operation"], body["payload"], operation_id=body["operation_id"])
            assert status == 200
            return receipt

    host = TaskHost(client=Client())

    def invoke(operation, payload):
        frame = {"protocol_version": 1, "type": "tool.request", "task_id": identity["task_id"],
                 "request_id": identifier(), "tool": "code_interpreter." + operation, "payload": payload}
        validate_child_frame(frame, identity["task_id"])
        return host._cyber(assignment, attempt, frame)

    started = invoke("start", {})
    session = started["result"]["session_id"]
    pending = invoke("execute", {"session_id": session, "code": "print(sum([2, 3]))", "language": "python"})
    assert pending["operation_status"] == "pending"
    completed = invoke("result", {"session_id": session, "execution_id": pending["request_id"]})
    assert completed["operation_status"] == "confirmed" and completed["result"]["stdout"] == "5\n"
    artifact = completed["artifact"]
    assert hashlib.sha256(artifacts[artifact["artifact_id"]]).hexdigest() == artifact["content_sha256"]
    file = invoke("file", {"session_id": session, "path": "/tmp/table.csv"})
    assert file["result"]["contents"][0]["structuredContent"]["content"] == "table,5\n"
    assert invoke("close", {"session_id": session})["result"]["status"] == "closed"


def test_grants_attempts_replay_and_invalid_inputs(route):
    send, grants, provider, artifacts, identity = route
    operation_id = identifier()
    _, started = send("start", {}, operation_id=operation_id)
    assert send("start", {}, operation_id=operation_id)[1] == started
    assert send("start", {})[1]["result"]["session_id"] == started["result"]["session_id"]
    assert len([call for call in provider.calls if call[0] == "start"]) == 1
    session = started["result"]["session_id"]
    different = {**identity, "runtime_attempt_id": identifier()}
    assert send("file", {"session_id": session, "path": "/tmp/table.csv"}, run=different)[0] == 403
    assert send("file", {"session_id": session, "path": "/tmp/.."})[0] == 422
    assert send("execute", {"session_id": session, "code": "x", "language": "javascript"})[0] == 422
    assert send("execute", {"session_id": session, "code": "x", "language": "python", "evidence_uri": "s3://another-task/secret"})[0] == 422
    assert send("execute", {"session_id": session, "code": "x" * 8193, "language": "python"})[0] == 422
    grants.remove("code_interpreter.execute")
    assert send("execute", {"session_id": session, "code": "print(1)", "language": "python"})[0] == 403
    grants.remove("code_interpreter.file")
    assert send("file", {"session_id": session, "path": "/tmp/table.csv"})[0] == 403
    assert len(provider.calls) == 1
    grants.remove("code_interpreter.close")
    assert send("close", {"session_id": session})[0] == 200
    grants.add("code_interpreter.close")
    assert send("close", {"session_id": session})[0] == 200


def test_ambiguous_start_and_execute_are_not_replayed(route, monkeypatch):
    send, grants, provider, artifacts, identity = route
    calls = []

    def lost_start(**kwargs):
        calls.append(kwargs)
        raise TimeoutError("lost provider response")

    monkeypatch.setattr(provider, "start_code_interpreter_session", lost_start)
    operation_id = identifier()
    assert send("start", {}, operation_id=operation_id)[0] == 503
    assert send("start", {}, operation_id=operation_id)[1]["operation_status"] == "unknown"
    assert len(calls) == 1
    assert send("start", {})[1]["operation_status"] == "unknown"
    assert len(calls) == 1


def test_ambiguous_execution_is_not_replayed(route, monkeypatch):
    send, grants, provider, artifacts, identity = route
    calls = []
    _, started = send("start", {})
    session = started["result"]["session_id"]
    original_invoke = provider.invoke_code_interpreter

    def lost_execution(**kwargs):
        if kwargs["name"] == "startCommandExecution":
            calls.append(kwargs)
            raise TimeoutError("provider response lost")
        return original_invoke(**kwargs)

    monkeypatch.setattr(provider, "invoke_code_interpreter", lost_execution)
    execution = identifier()
    payload = {"session_id": session, "code": "print(5)", "language": "python"}
    assert send("execute", payload, operation_id=execution)[0] == 503
    assert send("execute", payload, operation_id=execution)[1]["operation_status"] == "unknown"
    assert send("execute", payload)[1]["operation_status"] == "unknown"
    assert send("result", {"session_id": session, "execution_id": execution})[1]["operation_status"] == "unknown"
    assert len([call for call in calls if "name" in call]) == 1
    assert send("close", {"session_id": session})[0] == 200


def test_output_bound_and_session_grants(route, monkeypatch):
    send, grants, provider, artifacts, identity = route
    _, started = send("start", {})
    session = started["result"]["session_id"]
    operation = identifier()
    send("execute", {"session_id": session, "code": "print(1)", "language": "python"}, operation_id=operation)
    original = provider.invoke_code_interpreter

    def big_output(**kwargs):
        if kwargs["name"] == "getTask":
            return {"stream": iter([{"result": {"structuredContent": {"status": "completed", "output": "X" * 25000}}}])}
        return original(**kwargs)

    monkeypatch.setattr(provider, "invoke_code_interpreter", big_output)
    assert send("result", {"session_id": session, "execution_id": operation})[0] == 413
    assert not any(b"X" * 100 in value for value in artifacts.values())


def test_cleanup_closes_after_revocation_and_fences_new_work(route):
    send, grants, provider, artifacts, identity = route
    _, started = send("start", {})
    grants.clear()
    status, stopped = send("cancel_jobs", {})
    assert status == 200 and stopped["result"] == {"status": "confirmed", "pending_jobs": []}
    assert provider.calls[-1][0] == "stop"
    assert send("cancel_jobs", {})[1]["operation_status"] == "confirmed"
    assert sum(name == "stop" for name, _ in provider.calls) == 1
    grants.add("code_interpreter.execute")
    assert send("execute", {"session_id": started["result"]["session_id"], "code": "print(1)", "language": "python"})[0] == 409


def test_cleanup_before_start_blocks_new_provider_session(route):
    send, grants, provider, artifacts, identity = route
    assert send("cancel_jobs", {})[1]["operation_status"] == "confirmed"
    assert send("start", {})[0] == 409
    assert provider.calls == []


def test_unknown_start_cleanup_does_not_claim_success(route, monkeypatch):
    send, grants, provider, artifacts, identity = route
    monkeypatch.setattr(provider, "start_code_interpreter_session", lambda **kw: (_ for _ in ()).throw(TimeoutError()))
    assert send("start", {})[0] == 503
    assert send("cancel_jobs", {})[1]["operation_status"] == "pending"


def test_file_preserves_non_json_mcp_resource_content(route, monkeypatch):
    send, grants, provider, artifacts, identity = route
    _, started = send("start", {})
    monkeypatch.setattr(provider, "invoke_code_interpreter", lambda **kw: {"stream": iter([
        {"result": {"content": [{"type": "resource", "resource": {"uri": "/tmp/table.csv", "text": "column\n5\n"}}]}}])})
    status, result = send("file", {"session_id": started["result"]["session_id"], "path": "/tmp/table.csv"})
    assert status == 200
    assert result["result"]["contents"][0]["content"][0]["resource"]["text"] == "column\n5\n"


def test_provider_requests_match_pinned_sdk_contract(route):
    from botocore.validate import validate_parameters
    send, grants, provider, artifacts, identity = route
    _, started = send("start", {})
    session = started["result"]["session_id"]
    send("execute", {"session_id": session, "code": "print(1)", "language": "python"})
    send("close", {"session_id": session})
    model = boto3.Session()._session.get_service_model("bedrock-agentcore")
    for name, request in provider.calls:
        operation = {"start": "StartCodeInterpreterSession", "stop": "StopCodeInterpreterSession"}.get(name, "InvokeCodeInterpreter")
        validate_parameters(request, model.operation_model(operation).input_shape)


def test_cleanup_is_allowed_after_deadline_cancellation_and_exhausted_quota(route):
    from adp_tools.storage import serialize, task_partition
    send, grants, provider, artifacts, identity = route
    _, started = send("start", {})
    db = boto3.client("dynamodb", region_name="us-east-1")
    db.update_item(TableName="test-ops", Key=serialize({"event_id": task_partition(identity["task_id"]), "arrived_at": "META"}),
                   UpdateExpression="SET cyber_operation_count = :n", ExpressionAttributeValues=serialize({":n": 128}))
    send.verified.task.update(state="cancelled", version=2, deadline_at=(datetime.now(UTC)-timedelta(minutes=1)).isoformat())
    grants.clear()
    assert send("close", {"session_id": started["result"]["session_id"]})[0] == 200
    assert send("cancel_jobs", {})[1]["result"] == {"status": "confirmed", "pending_jobs": []}
    assert sum(name == "stop" for name, _ in provider.calls) == 1


def test_file_command_reads_tmp_with_bounds(route):
    import base64
    import subprocess
    import shlex
    import tempfile

    send, grants, provider, artifacts, identity = route
    _, started = send("start", {})
    with tempfile.NamedTemporaryFile(prefix="adp-proof-", dir="/tmp") as proof:
        proof.write(b"42\n")
        proof.flush()
        send("file", {"session_id": started["result"]["session_id"], "path": proof.name})
        name, request = provider.calls[-1]
        assert name == "executeCommand"
        command = [sys.executable, *shlex.split(request["arguments"]["command"])[1:]]
        completed = subprocess.run(command, capture_output=True, check=True)
        assert base64.b64decode(json.loads(completed.stdout)["data"]) == b"42\n"
        proof.write(b"x" * 8193)
        proof.flush()
        assert subprocess.run(command, capture_output=True).returncode != 0
