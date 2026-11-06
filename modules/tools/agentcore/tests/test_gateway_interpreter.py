"""Provider-backed interpreter session and execution claims survive retries."""

import base64
import hashlib
import json
import os
import shlex
import subprocess
import tempfile
import time

import boto3
import pytest
from fastapi import HTTPException
from moto import mock_aws

from agentcore_tools import gateway_interpreter as gateway_interpreter_module
from agentcore_tools.gateway_interpreter import GatewayInterpreter, GatewayInterpreterReconciler

EXECUTION_ID = "00000000-0000-4000-8000-000000000001"
START_ID = "00000000-0000-4000-8000-000000000002"
EXECUTE_ID = "00000000-0000-4000-8000-000000000003"
CLAIMS = {"principal": {"tenant_id": "tenant-one", "user_id": "user-one", "team_id": "team-one"},
          "receipt_id": "receipt-one"}


class Provider:
    def __init__(self):
        self.calls = []
        self.fail_start = False
        self.fail_execution = False
        self.fail_file = False
        self.refuse_file = False
        self.fail_stop = False
        self.stopped = False
        self.read_status = None
        self.file_bytes = b"42\n"
        self.outcome = {"taskStatus": "completed", "stdout": "42\n", "stderr": "", "exitCode": 0}

    def start_code_interpreter_session(self, **kwargs):
        self.calls.append(("start", kwargs))
        if self.fail_start:
            raise TimeoutError("start response lost")
        return {"sessionId": "private-session"}

    def invoke_code_interpreter(self, **kwargs):
        self.calls.append((kwargs["name"], kwargs))
        if kwargs["name"] == "startCommandExecution" and self.fail_execution:
            raise TimeoutError("execution response lost")
        if kwargs["name"] == "executeCommand" and self.fail_file:
            raise TimeoutError("file response lost")
        if kwargs["name"] == "executeCommand" and self.refuse_file:
            return {"stream": iter([{"result": {"isError": True}}])}
        content = {"startCommandExecution": {"taskId": "private-execution"},
                   "getTask": self.outcome,
                   "executeCommand": {"stdout": json.dumps({"encoding": "base64",
                       "data": base64.b64encode(self.file_bytes).decode()})}}[kwargs["name"]]
        return {"stream": iter([{"result": {"structuredContent": content}}])}

    def stop_code_interpreter_session(self, **kwargs):
        self.calls.append(("stop", kwargs))
        if self.fail_stop:
            raise TimeoutError("stop response lost")
        self.stopped = True
        return {}

    def get_code_interpreter_session(self, **kwargs):
        self.calls.append(("get_session", kwargs))
        return {"status": self.read_status or ("TERMINATED" if self.stopped else "READY")}


@pytest.fixture
def interpreter(monkeypatch):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(TableName="interpreter-ops", BillingMode="PAY_PER_REQUEST",
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
        monkeypatch.setenv("ADP_TOOLS_TABLE", "interpreter-ops")
        monkeypatch.setenv("ADP_CODE_INTERPRETER_ID", "CodeInterpreterExample-0123456789")
        # These tests drive the explicit pending → result protocol; the in-call wait is exercised on its own.
        monkeypatch.setattr(gateway_interpreter_module, "EXECUTE_WAIT_SECONDS", 0.0)
        yield GatewayInterpreter(client, Provider())


def start_args():
    return {"execution_id": EXECUTION_ID, "request_id": START_ID}


def execute_args(session_id):
    return {"execution_id": EXECUTION_ID, "request_id": EXECUTE_ID, "session_id": session_id,
            "code": "print(42)", "language": "python"}


@pytest.mark.parametrize(("events", "expected"), [
    ([{"result": {"isError": True, "content": [{"type": "text", "text": "private provider detail"}]}}], "failed"),
    ([], "unknown"),
    ([{"unexpected": "event"}], "unknown"),
    ([{"result": {"structuredContent": {"unexpected": "response"}}}], "unknown"),
])
def test_execute_rejection_is_terminal_but_inconclusive_responses_stay_unknown(interpreter, monkeypatch,
                                                                             events, expected):
    start = interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args()))
    args = execute_args(start["session_id"])
    calls = []

    def invoke(**kwargs):
        calls.append(kwargs)
        return {"stream": iter(events)}

    monkeypatch.setattr(interpreter.provider, "invoke_code_interpreter", invoke)
    receipt = interpreter.execute(args, CLAIMS, lambda: (CLAIMS, args))
    assert receipt["status"] == expected
    assert "provider_id" not in receipt
    if expected == "failed":
        assert receipt["result"] == {"status": "failed", "error": "provider_refused"}
        assert "private provider detail" not in json.dumps(receipt)
    restarted = GatewayInterpreter(interpreter.client, interpreter.provider)
    assert restarted.operation_get({"execution_id": EXECUTION_ID, "request_id": EXECUTE_ID}, CLAIMS) == receipt
    assert restarted.execute(args, CLAIMS, lambda: (CLAIMS, args)) == receipt
    assert len(calls) == 1
    with pytest.raises(HTTPException) as foreign:
        restarted.operation_get({"execution_id": EXECUTION_ID, "request_id": EXECUTE_ID},
            {**CLAIMS, "principal": {**CLAIMS["principal"], "user_id": "foreign"}})
    assert foreign.value.status_code == 404


def test_start_execute_restart_poll_and_hash(interpreter):
    start = interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args()))
    assert start["status"] == "confirmed" and len(start["session_id"]) == 64
    assert interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args())) == start
    args = execute_args(start["session_id"])
    submitted = interpreter.execute(args, CLAIMS, lambda: (CLAIMS, args))
    assert submitted["status"] == "pending" and submitted["provider_id"] == "private-execution"
    restarted = GatewayInterpreter(interpreter.client, interpreter.provider)
    assert restarted.execute(args, CLAIMS, lambda: (CLAIMS, args)) == submitted
    assert restarted.operation_get({"execution_id": EXECUTION_ID, "request_id": EXECUTE_ID}, CLAIMS) == submitted
    outcome = restarted.result({"execution_id": EXECUTION_ID, "request_id": EXECUTE_ID,
                                "session_id": start["session_id"], "provider_id": submitted["provider_id"]}, CLAIMS)
    assert outcome["status"] == "confirmed"
    assert outcome["result"] == {"status": "completed", "stdout": "42\n", "stderr": "", "exitCode": 0}
    assert outcome["content_sha256"] == hashlib.sha256(
        interpreter._read(CLAIMS, EXECUTION_ID, "CODE_OP#" + EXECUTE_ID)["result_json"]["S"].encode()).hexdigest()
    assert [name for name, _ in interpreter.provider.calls] == ["start", "startCommandExecution", "getTask"]
    interpreter.client.update_item(TableName="interpreter-ops",
        Key=interpreter._key(CLAIMS, EXECUTION_ID, "CODE_SESSION"),
        UpdateExpression="SET deadline_at = :past", ExpressionAttributeValues={":past": {"N": "1"}})
    assert restarted.execute(args, CLAIMS, lambda: (CLAIMS, args)) == outcome
    with pytest.raises(HTTPException) as expired:
        restarted.execute({**args, "request_id": "00000000-0000-4000-8000-000000000004"},
            CLAIMS, lambda: (CLAIMS, args))
    assert expired.value.status_code == 409
    with pytest.raises(HTTPException) as changed:
        restarted.execute({**args, "code": "print(43)"}, CLAIMS, lambda: (CLAIMS, args))
    assert changed.value.status_code == 409
    with pytest.raises(HTTPException) as foreign:
        restarted.operation_get({"execution_id": EXECUTION_ID, "request_id": EXECUTE_ID},
            {**CLAIMS, "principal": {**CLAIMS["principal"], "user_id": "user-two"}})
    assert foreign.value.status_code == 404
    with pytest.raises(HTTPException) as wrong_provider:
        restarted.result({"execution_id": EXECUTION_ID, "request_id": EXECUTE_ID,
            "session_id": start["session_id"], "provider_id": "foreign"}, CLAIMS)
    assert wrong_provider.value.status_code == 404


def test_ambiguous_start_and_execution_are_never_replayed(interpreter):
    interpreter.provider.fail_start = True
    start = interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args()))
    assert start["status"] == "unknown"
    resumed = GatewayInterpreter(interpreter.client, interpreter.provider)
    interpreter.provider.fail_start = False
    assert resumed.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args())) == start
    assert [name for name, _ in interpreter.provider.calls] == ["start"]


def test_ambiguous_execute_cannot_be_retried_to_obtain_new_provider_id(interpreter):
    start = interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args()))
    args = execute_args(start["session_id"])
    interpreter.provider.fail_execution = True
    first = interpreter.execute(args, CLAIMS, lambda: (CLAIMS, args))
    assert first["status"] == "unknown" and "provider_id" not in first
    interpreter.provider.fail_execution = False
    resumed = GatewayInterpreter(interpreter.client, interpreter.provider)
    assert resumed.execute(args, CLAIMS, lambda: (CLAIMS, args)) == first
    assert [name for name, _ in interpreter.provider.calls] == ["start", "startCommandExecution"]


def test_uncertain_execute_fences_new_ids_until_scheduled_provider_termination(interpreter):
    now = int(time.time())
    claims = {**CLAIMS, "execution_deadline_at": now + 300}
    started = interpreter.start(start_args(), claims, lambda: (claims, start_args()))
    file_request = {"execution_id": EXECUTION_ID, "request_id": "00000000-0000-4000-8000-000000000050",
                    "session_id": started["session_id"], "path": "/tmp/result.txt"}
    saved = interpreter.file(file_request, claims, lambda: (claims, file_request))
    assert saved["artifact"]["content_sha256"] == hashlib.sha256(b"42\n").hexdigest()
    interpreter.provider.fail_execution = True
    arguments = execute_args(started["session_id"])
    lost = interpreter.execute(arguments, claims, lambda: (claims, arguments))
    assert lost["status"] == "unknown"
    interpreter.provider.fail_execution = False
    restarted = GatewayInterpreter(interpreter.client, interpreter.provider)
    retry = {**arguments, "request_id": "00000000-0000-4000-8000-000000000051"}
    assert restarted.execute(retry, claims, lambda: (claims, retry))["status"] == "unknown"
    assert [name for name, _ in interpreter.provider.calls].count("startCommandExecution") == 1
    session_key = interpreter._key(claims, EXECUTION_ID, "CODE_SESSION")
    assert interpreter._read(claims, EXECUTION_ID, "CODE_SESSION")["active_mutation"]["S"] == EXECUTE_ID
    interpreter.client.update_item(TableName=interpreter.table, Key=session_key,
        UpdateExpression="SET deadline_at = :past, next_cleanup_at = :past",
        ExpressionAttributeValues={":past": {"N": str(now - 300)}})
    GatewayInterpreterReconciler(restarted, clock=lambda: now).run_once()
    assert interpreter.provider.get_code_interpreter_session()["status"] == "TERMINATED"
    closed = interpreter.operation_get({"execution_id": EXECUTION_ID, "request_id": START_ID}, claims)
    assert closed["termination_confirmed"] is True
    owned = restarted.artifact_read({"execution_id": EXECUTION_ID,
                                    "artifact_id": file_request["request_id"]}, claims)
    assert base64.b64decode(owned["data_base64"]) == b"42\n"
    foreign = {**claims, "principal": {**CLAIMS["principal"], "user_id": "other"}}
    with pytest.raises(HTTPException) as refused:
        restarted.artifact_read({"execution_id": EXECUTION_ID, "artifact_id": file_request["request_id"]}, foreign)
    assert refused.value.status_code == 404
    assert [name for name, _ in interpreter.provider.calls].count("startCommandExecution") == 1


def test_provider_failure_remains_truthful(interpreter):
    start = interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args()))
    args = execute_args(start["session_id"])
    submitted = interpreter.execute(args, CLAIMS, lambda: (CLAIMS, args))
    interpreter.provider.outcome = {"taskStatus": "failed", "stdout": "", "stderr": "bad code", "exitCode": 1}
    result = interpreter.result({"execution_id": EXECUTION_ID, "request_id": EXECUTE_ID,
        "session_id": start["session_id"], "provider_id": submitted["provider_id"]}, CLAIMS)
    assert result["status"] == "failed"
    assert result["result"] == {"status": "failed", "stdout": "", "stderr": "bad code", "exitCode": 1}
    interpreter.provider.outcome = {"taskStatus": "completed"}
    assert interpreter.operation_get({"execution_id": EXECUTION_ID, "request_id": EXECUTE_ID}, CLAIMS) == result


@pytest.mark.parametrize("provider_status", ["completed", "failed", "cancelled"])
def test_nonzero_execution_stays_failed_without_duplicate_provider_poll(interpreter, provider_status):
    started = interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args()))
    arguments = execute_args(started["session_id"])
    submitted = interpreter.execute(arguments, CLAIMS, lambda: (CLAIMS, arguments))
    interpreter.provider.outcome = {"taskStatus": provider_status, "stdout": "", "stderr": "bad code", "exitCode": 2}
    poll = {"execution_id": EXECUTION_ID, "request_id": EXECUTE_ID,
            "session_id": started["session_id"], "provider_id": submitted["provider_id"]}
    result = interpreter.result(poll, CLAIMS)
    assert result["status"] == "failed"
    assert result["result"]["exitCode"] == 2 and result["result"]["stderr"] == "bad code"
    calls = list(interpreter.provider.calls)
    assert GatewayInterpreter(interpreter.client, interpreter.provider).result(poll, CLAIMS) == result
    assert interpreter.provider.calls == calls


FILE_ID = "00000000-0000-4000-8000-000000000005"


def file_args(session_id, path="/tmp/example.txt"):
    return {"execution_id": EXECUTION_ID, "request_id": FILE_ID,
            "session_id": session_id, "path": path}


@pytest.mark.parametrize("path", ["/tmp/../private", "/tmp/..", "/tmp/file/child", "relative.txt"])
def test_traversal_rejected_before_provider_file_read(interpreter, path):
    started = interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args()))
    before = list(interpreter.provider.calls)
    arguments = file_args(started["session_id"], path)
    with pytest.raises(HTTPException) as denied:
        interpreter.file(arguments, CLAIMS, lambda: (CLAIMS, arguments))
    assert denied.value.status_code == 422 and interpreter.provider.calls == before


def test_foreign_handle_cannot_read_file(interpreter):
    started = interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args()))
    before = list(interpreter.provider.calls)
    arguments = file_args("f" * 64)
    with pytest.raises(HTTPException) as denied:
        interpreter.file(arguments, CLAIMS, lambda: (CLAIMS, arguments))
    assert denied.value.status_code == 404 and interpreter.provider.calls == before
    foreign = {**CLAIMS, "principal": {**CLAIMS["principal"], "user_id": "other"}}
    arguments = file_args(started["session_id"])
    with pytest.raises(HTTPException) as denied:
        interpreter.file(arguments, foreign, lambda: (foreign, arguments))
    assert denied.value.status_code == 404 and interpreter.provider.calls == before


def test_symlink_file_refused_without_exposing_target(interpreter, monkeypatch):
    started = interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args()))
    with tempfile.NamedTemporaryFile(dir="/tmp", prefix="adp-8070-private-") as target:
        target.write(b"private fixture value")
        target.flush()
        descriptor, name = tempfile.mkstemp(dir="/tmp", prefix="adp-8070-link-")
        os.close(descriptor)
        os.unlink(name)
        os.symlink(target.name, name)
        original = interpreter.provider.invoke_code_interpreter

        def invoke(**kwargs):
            if kwargs["name"] != "executeCommand":
                return original(**kwargs)
            interpreter.provider.calls.append(("executeCommand", kwargs))
            response = subprocess.run(shlex.split(kwargs["arguments"]["command"]),
                                      capture_output=True, text=True, timeout=3, check=False)
            assert response.returncode != 0
            return {"stream": iter([{"result": {"isError": True}}])}

        monkeypatch.setattr(interpreter.provider, "invoke_code_interpreter", invoke)
        try:
            arguments = file_args(started["session_id"], name)
            refused = interpreter.file(arguments, CLAIMS, lambda: (CLAIMS, arguments))
            assert refused["status"] == "failed" and "private fixture value" not in json.dumps(refused)
            assert "artifact" not in refused
        finally:
            os.unlink(name)


def test_expired_session_denies_new_execute_and_file_before_provider_access(interpreter):
    started = interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args()))
    interpreter.client.update_item(TableName=interpreter.table,
        Key=interpreter._key(CLAIMS, EXECUTION_ID, "CODE_SESSION"),
        UpdateExpression="SET deadline_at = :past", ExpressionAttributeValues={":past": {"N": "1"}})
    before = list(interpreter.provider.calls)
    for operation, arguments in ((interpreter.execute, execute_args(started["session_id"])),
                                (interpreter.file, file_args(started["session_id"]))):
        with pytest.raises(HTTPException) as denied:
            operation(arguments, CLAIMS, lambda: (CLAIMS, arguments))
        assert denied.value.status_code == 409
    assert interpreter.provider.calls == before


def test_file_recovery_retains_bytes_hash_and_owned_close(interpreter):
    start = interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args()))
    args = file_args(start["session_id"])
    found = interpreter.file(args, CLAIMS, lambda: (CLAIMS, args))
    assert found["status"] == "confirmed" and found["result"]["path"] == args["path"]
    assert found["artifact"] == {"artifact_id": FILE_ID, "content_type": "application/octet-stream",
                                  "content_sha256": hashlib.sha256(b"42\n").hexdigest(), "byte_length": 3}
    command = interpreter.provider.calls[-1][1]["arguments"]["command"]
    assert "O_NOFOLLOW" in command and "8193" in command and "ISREG" in command
    restarted = GatewayInterpreter(interpreter.client, interpreter.provider)
    artifact = restarted.artifact_read({"execution_id": EXECUTION_ID, "artifact_id": FILE_ID, "offset": 1}, CLAIMS)
    assert artifact["sha256"] == found["artifact"]["content_sha256"]
    assert base64.b64decode(artifact["data_base64"]) == b"2\n" and artifact["total_length"] == 3
    assert restarted.operation_get({"execution_id": EXECUTION_ID, "request_id": FILE_ID}, CLAIMS) == found
    before = list(interpreter.provider.calls)
    assert restarted.file(args, CLAIMS, lambda: (CLAIMS, args)) == found
    assert interpreter.provider.calls == before
    with pytest.raises(HTTPException) as conflict:
        restarted.file(file_args(start["session_id"], "/tmp/other.txt"), CLAIMS, lambda: (CLAIMS, args))
    assert conflict.value.status_code == 409
    with pytest.raises(HTTPException) as foreign:
        restarted.artifact_read({"execution_id": EXECUTION_ID, "artifact_id": FILE_ID},
            {**CLAIMS, "principal": {**CLAIMS["principal"], "user_id": "user-two"}})
    assert foreign.value.status_code == 404
    interpreter.provider.fail_stop = True
    close_args = {"execution_id": EXECUTION_ID, "session_id": start["session_id"]}
    pending = restarted.close(close_args, CLAIMS, lambda: (CLAIMS, close_args))
    assert pending["status"] == "pending" and pending["termination_confirmed"] is False
    interpreter.provider.fail_stop = False
    confirmed = restarted.close(close_args, CLAIMS, lambda: (CLAIMS, close_args))
    assert confirmed["status"] == "confirmed" and confirmed["termination_confirmed"] is True
    assert restarted.close(close_args, CLAIMS, lambda: (CLAIMS, close_args)) == confirmed
    assert [name for name, _ in interpreter.provider.calls].count("stop") == 2
    assert interpreter.provider.calls[-1][0] == "get_session"
    assert [name for name, _ in interpreter.provider.calls].count("stop") == 2
    assert restarted.artifact_read({"execution_id": EXECUTION_ID, "artifact_id": FILE_ID}, CLAIMS)["sha256"] == found["artifact"]["content_sha256"]
    with pytest.raises(HTTPException) as closed:
        restarted.file(file_args(start["session_id"], "/tmp/late.txt"), CLAIMS,
            lambda: (CLAIMS, file_args(start["session_id"], "/tmp/late.txt")))
    assert closed.value.status_code == 409


def test_file_response_loss_and_oversized_file_do_not_replay(interpreter):
    start = interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args()))
    args = file_args(start["session_id"])
    interpreter.provider.fail_file = True
    lost = interpreter.file(args, CLAIMS, lambda: (CLAIMS, args))
    assert lost["status"] == "unknown" and "artifact" not in lost
    interpreter.provider.fail_file = False
    assert GatewayInterpreter(interpreter.client, interpreter.provider).file(args, CLAIMS, lambda: (CLAIMS, args)) == lost
    assert [name for name, _ in interpreter.provider.calls].count("executeCommand") == 1
    with pytest.raises(HTTPException) as missing:
        interpreter.artifact_read({"execution_id": EXECUTION_ID, "artifact_id": FILE_ID}, CLAIMS)
    assert missing.value.status_code == 404
    other = file_args(start["session_id"], "/tmp/big.txt")
    other["request_id"] = "00000000-0000-4000-8000-000000000006"
    interpreter.provider.file_bytes = b"a" * 8193
    oversized = interpreter.file(other, CLAIMS, lambda: (CLAIMS, other))
    assert oversized["status"] == "unknown" and "artifact" not in oversized
    assert GatewayInterpreter(interpreter.client, interpreter.provider).file(other, CLAIMS,
        lambda: (CLAIMS, other)) == oversized
    assert [name for name, _ in interpreter.provider.calls].count("executeCommand") == 2


def test_ambiguous_start_close_requires_provider_termination_evidence(interpreter):
    interpreter.provider.fail_start = True
    started = interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args()))
    assert started["status"] == "unknown"
    close_args = {"execution_id": EXECUTION_ID, "session_id": hashlib.sha256(
        json.dumps([interpreter._key(CLAIMS, EXECUTION_ID, "CODE_SESSION")["event_id"]["S"], START_ID],
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
    pending = interpreter.close(close_args, CLAIMS, lambda: (CLAIMS, close_args))
    assert pending["status"] == "pending" and pending["termination_confirmed"] is False
    assert [name for name, _ in interpreter.provider.calls] == ["start"]


def test_known_file_failure_and_denied_close_do_not_claim_termination(interpreter):
    started = interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args()))
    args = file_args(started["session_id"])
    interpreter.provider.refuse_file = True
    failed = interpreter.file(args, CLAIMS, lambda: (CLAIMS, args))
    assert failed["status"] == "failed" and failed["result"] == {"status": "failed", "error": "provider_refused"}
    assert "artifact" not in failed
    interpreter.provider.refuse_file = False
    assert GatewayInterpreter(interpreter.client, interpreter.provider).file(args, CLAIMS, lambda: (CLAIMS, args)) == failed
    assert [name for name, _ in interpreter.provider.calls].count("executeCommand") == 1
    close_args = {"execution_id": EXECUTION_ID, "session_id": started["session_id"]}
    with pytest.raises(HTTPException) as refused:
        interpreter.close(close_args, CLAIMS, lambda: ({}, {}))
    assert refused.value.status_code == 403
    assert "cleanup_state" not in interpreter._read(CLAIMS, EXECUTION_ID, "CODE_SESSION")
    foreign = {**CLAIMS, "principal": {**CLAIMS["principal"], "user_id": "user-two"}}
    with pytest.raises(HTTPException) as missing:
        interpreter.close(close_args, foreign, lambda: (foreign, close_args))
    assert missing.value.status_code == 404
    assert [name for name, _ in interpreter.provider.calls].count("stop") == 0


def test_independent_expiry_retries_owned_stop_without_losing_receipt(interpreter, monkeypatch):
    start = interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args()))
    key = interpreter._key(CLAIMS, EXECUTION_ID, "CODE_SESSION")
    interpreter.client.update_item(TableName=interpreter.table, Key=key,
        UpdateExpression="SET deadline_at = :past, next_cleanup_at = :past",
        ExpressionAttributeValues={":past": {"N": "1"}})
    interpreter.provider.fail_stop = True
    reconciler = GatewayInterpreterReconciler(
        GatewayInterpreter(interpreter.client, interpreter.provider), clock=lambda: 1000)
    reconciler.run_once()
    session = interpreter._read(CLAIMS, EXECUTION_ID, "CODE_SESSION")
    assert session["cleanup_state"]["S"] == "closing"
    assert session["provider_id"]["S"] == "private-session"
    assert interpreter.operation_get({"execution_id": EXECUTION_ID, "request_id": START_ID}, CLAIMS)["receipt_id"] == start["receipt_id"]
    with pytest.raises(HTTPException) as fenced:
        interpreter.execute(execute_args(start["session_id"]), CLAIMS,
            lambda: (CLAIMS, execute_args(start["session_id"])))
    assert fenced.value.status_code == 409
    interpreter.provider.fail_stop = False
    monkeypatch.setenv("ADP_CODE_INTERPRETER_ID", "")
    GatewayInterpreterReconciler(GatewayInterpreter(interpreter.client, interpreter.provider),
        clock=lambda: 1060).run_once()
    session = interpreter._read(CLAIMS, EXECUTION_ID, "CODE_SESSION")
    assert session["cleanup_state"]["S"] == "closed"
    assert "cleanup_partition" not in session
    assert [name for name, _ in interpreter.provider.calls].count("stop") == 2
    assert interpreter.provider.calls[-1][1]["codeInterpreterIdentifier"] == "CodeInterpreterExample-0123456789"


def test_close_fences_inflight_execution_before_owned_stop(interpreter):
    start = interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args()))
    args = execute_args(start["session_id"])
    close_args = {"execution_id": EXECUTION_ID, "session_id": start["session_id"]}
    original = interpreter.provider.invoke_code_interpreter

    def during_execute(**kwargs):
        pending = interpreter.close(close_args, CLAIMS, lambda: (CLAIMS, close_args))
        assert pending["status"] == "pending"
        assert [name for name, _ in interpreter.provider.calls].count("stop") == 0
        return original(**kwargs)

    interpreter.provider.invoke_code_interpreter = during_execute
    submitted = interpreter.execute(args, CLAIMS, lambda: (CLAIMS, args))
    assert submitted["status"] == "pending"
    with pytest.raises(HTTPException) as fenced:
        interpreter.file({"execution_id": EXECUTION_ID, "request_id": FILE_ID,
            "session_id": start["session_id"], "path": "/tmp/late.txt"}, CLAIMS, lambda: None)
    assert fenced.value.status_code == 409
    GatewayInterpreterReconciler(interpreter, clock=lambda: int(time.time()) + 1).run_once()
    assert interpreter._read(CLAIMS, EXECUTION_ID, "CODE_SESSION")["cleanup_state"]["S"] == "closed"
    assert [name for name, _ in interpreter.provider.calls].count("stop") == 1


def test_stop_acknowledgement_requires_independent_terminated_readback(interpreter):
    started = interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args()))
    arguments = {"execution_id": EXECUTION_ID, "session_id": started["session_id"]}
    interpreter.provider.read_status = "READY"
    pending = interpreter.close(arguments, CLAIMS, lambda: (CLAIMS, arguments))
    assert pending["status"] == "pending" and pending["termination_confirmed"] is False
    assert interpreter._read(CLAIMS, EXECUTION_ID, "CODE_SESSION")["cleanup_state"]["S"] == "closing"
    interpreter.provider.read_status = "TERMINATED"
    confirmed = GatewayInterpreter(interpreter.client, interpreter.provider).execution_close(
        {"execution_id": EXECUTION_ID}, CLAIMS, lambda: (CLAIMS, {"execution_id": EXECUTION_ID}))
    assert confirmed["status"] == "confirmed" and confirmed["termination_confirmed"] is True


def test_old_interpreter_closed_row_requires_provider_readback(interpreter):
    started = interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args()))
    key = interpreter._key(CLAIMS, EXECUTION_ID, "CODE_SESSION")
    interpreter.client.update_item(TableName=interpreter.table, Key=key,
        UpdateExpression="SET cleanup_state = :closed",
        ExpressionAttributeValues={":closed": {"S": "closed"}})
    arguments = {"execution_id": EXECUTION_ID, "session_id": started["session_id"]}
    interpreter.provider.read_status = "READY"
    pending = interpreter.close(arguments, CLAIMS, lambda: (CLAIMS, arguments))
    assert pending["status"] == "pending" and pending["termination_confirmed"] is False
    interpreter.provider.read_status = "TERMINATED"
    confirmed = interpreter.execution_close({"execution_id": EXECUTION_ID}, CLAIMS,
        lambda: (CLAIMS, {"execution_id": EXECUTION_ID}))
    assert confirmed["status"] == "confirmed" and confirmed["termination_confirmed"] is True


def test_lost_stop_response_with_terminated_readback_is_confirmed(interpreter):
    started = interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args()))
    arguments = {"execution_id": EXECUTION_ID, "session_id": started["session_id"]}
    interpreter.provider.fail_stop = True
    interpreter.provider.read_status = "TERMINATED"
    result = interpreter.close(arguments, CLAIMS, lambda: (CLAIMS, arguments))
    assert result["status"] == "confirmed" and result["termination_confirmed"] is True


def test_ambiguous_start_remains_unconfirmed_on_independent_expiry(interpreter):
    interpreter.provider.fail_start = True
    started = interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args()))
    assert started["status"] == "unknown"
    interpreter.client.update_item(TableName=interpreter.table,
        Key=interpreter._key(CLAIMS, EXECUTION_ID, "CODE_SESSION"),
        UpdateExpression="SET deadline_at = :past, next_cleanup_at = :past",
        ExpressionAttributeValues={":past": {"N": "1"}})
    GatewayInterpreterReconciler(interpreter, clock=lambda: 1000).run_once()
    result = interpreter.operation_get({"execution_id": EXECUTION_ID, "request_id": START_ID}, CLAIMS)
    assert result["termination_confirmed"] is False and result["cleanup_status"] == "pending"
    GatewayInterpreterReconciler(interpreter, clock=lambda: 1120).run_once()
    row = interpreter._read(CLAIMS, EXECUTION_ID, "CODE_SESSION")
    assert "cleanup_partition" not in row
    assert interpreter.operation_get({"execution_id": EXECUTION_ID, "request_id": START_ID}, CLAIMS) == result
    assert [name for name, _ in interpreter.provider.calls] == ["start"]


def test_preindex_interrupted_close_can_be_resumed_and_reindexed(interpreter):
    started = interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args()))
    args = {"execution_id": EXECUTION_ID, "session_id": started["session_id"]}
    key = interpreter._key(CLAIMS, EXECUTION_ID, "CODE_SESSION")
    interpreter.client.update_item(TableName=interpreter.table, Key=key,
        UpdateExpression="SET cleanup_state = :closing REMOVE cleanup_partition, next_cleanup_at",
        ExpressionAttributeValues={":closing": {"S": "closing"}})
    interpreter.provider.fail_stop = True
    assert interpreter.close(args, CLAIMS, lambda: (CLAIMS, args))["status"] == "pending"
    assert interpreter._read(CLAIMS, EXECUTION_ID, "CODE_SESSION")["cleanup_partition"]["S"] == "due"
    interpreter.provider.fail_stop = False
    GatewayInterpreterReconciler(interpreter, clock=lambda: int(time.time()) + 1).run_once()
    assert interpreter.close(args, CLAIMS, lambda: (CLAIMS, args))["termination_confirmed"] is True


def test_scheduled_handler_runs_cleanup_without_a_gateway_assertion(monkeypatch):
    from types import SimpleNamespace
    from agentcore_tools import handler

    observed = []
    monkeypatch.setattr("agentcore_tools.gateway_interpreter.GatewayInterpreterReconciler",
                        lambda: SimpleNamespace(run_once=lambda: observed.append("scheduled")))
    assert handler.lambda_handler({"action": "reconcile-code-interpreter"},
                                  SimpleNamespace(client_context=None)) == {"status": "ok"}
    assert observed == ["scheduled"]


def test_admission_deadline_limits_delayed_provider_start(interpreter, monkeypatch):
    from types import SimpleNamespace
    from agentcore_tools import gateway_interpreter

    current = int(time.time())
    readings = iter((current + 10, current + 18))
    monkeypatch.setattr(gateway_interpreter, "time", SimpleNamespace(time=lambda: next(readings)))
    claims = {**CLAIMS, "execution_deadline_at": current + 70}
    receipt = interpreter.start(start_args(), claims, lambda: (claims, start_args()))
    assert receipt["status"] == "confirmed"
    assert interpreter.provider.calls[0][1]["sessionTimeoutSeconds"] == 52
    assert interpreter._read(claims, EXECUTION_ID, "CODE_SESSION")["deadline_at"]["N"] == str(current + 70)


def test_expired_interpreter_start_never_calls_provider(interpreter, monkeypatch):
    from types import SimpleNamespace
    from agentcore_tools import gateway_interpreter

    current = int(time.time())
    monkeypatch.setattr(gateway_interpreter, "time", SimpleNamespace(time=lambda: current))
    claims = {**CLAIMS, "execution_deadline_at": current}
    with pytest.raises(HTTPException) as refused:
        interpreter.start(start_args(), claims, lambda: (claims, start_args()))
    assert refused.value.status_code == 403
    assert interpreter.provider.calls == []


def test_interpreter_delay_past_claimed_deadline_keeps_unknown_without_provider_start(interpreter, monkeypatch):
    from types import SimpleNamespace
    from agentcore_tools import gateway_interpreter

    current = int(time.time())
    readings = iter((current, current + 70))
    monkeypatch.setattr(gateway_interpreter, "time", SimpleNamespace(time=lambda: next(readings)))
    claims = {**CLAIMS, "execution_deadline_at": current + 60}
    receipt = interpreter.start(start_args(), claims, lambda: (claims, start_args()))
    assert receipt["status"] == "unknown"
    assert interpreter.provider.calls == []


def test_execute_waits_briefly_and_returns_the_finished_result_in_one_call(interpreter, monkeypatch):
    """#8223 Dev Box tool latency: a bare ``pending`` for a 170 ms execution cost the caller a whole extra
    model turn plus a status read (~8 s). Within the wait budget the finished result is returned by execute."""
    monkeypatch.setattr(gateway_interpreter_module, "EXECUTE_WAIT_SECONDS", 6.0)
    monkeypatch.setattr(gateway_interpreter_module, "_sleep", lambda seconds: None)
    polls = {"n": 0}
    original = interpreter.provider.invoke_code_interpreter

    def running_then_done(**kwargs):
        if kwargs["name"] == "getTask":
            polls["n"] += 1
            if polls["n"] < 3:
                interpreter.provider.calls.append(("getTask", kwargs))
                return {"stream": iter([{"result": {"structuredContent": {"taskStatus": "running"}}}])}
        return original(**kwargs)

    interpreter.provider.invoke_code_interpreter = running_then_done
    start = interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args()))
    args = execute_args(start["session_id"])
    outcome = interpreter.execute(args, CLAIMS, lambda: (CLAIMS, args))
    assert outcome["status"] == "confirmed"
    assert outcome["result"] == {"status": "completed", "stdout": "42\n", "stderr": "", "exitCode": 0}
    assert [name for name, _ in interpreter.provider.calls] == ["start", "startCommandExecution", "getTask", "getTask", "getTask"]
    # A later poll is answered from the recorded outcome without another provider read.
    assert interpreter.result({"execution_id": EXECUTION_ID, "request_id": EXECUTE_ID, "session_id": start["session_id"],
                               "provider_id": outcome["provider_id"]}, CLAIMS) == outcome
    assert [name for name, _ in interpreter.provider.calls].count("getTask") == 3


def test_execute_answers_pending_once_the_wait_budget_is_spent(interpreter, monkeypatch):
    monkeypatch.setattr(gateway_interpreter_module, "EXECUTE_WAIT_SECONDS", 1.0)
    monkeypatch.setattr(gateway_interpreter_module, "_sleep", lambda seconds: None)
    clock = {"now": 100.0}

    def monotonic():
        clock["now"] += 0.4
        return clock["now"]

    monkeypatch.setattr(gateway_interpreter_module, "_monotonic", monotonic)
    interpreter.provider.outcome = {"taskStatus": "running"}
    start = interpreter.start(start_args(), CLAIMS, lambda: (CLAIMS, start_args()))
    args = execute_args(start["session_id"])
    submitted = interpreter.execute(args, CLAIMS, lambda: (CLAIMS, args))
    assert submitted["status"] == "pending" and submitted["provider_id"] == "private-execution"
    polls = [name for name, _ in interpreter.provider.calls].count("getTask")
    assert 1 <= polls <= 3, "bounded by the budget, not by the provider staying busy"
    interpreter.provider.outcome = {"taskStatus": "completed", "stdout": "42\n", "stderr": "", "exitCode": 0}
    result = interpreter.result({"execution_id": EXECUTION_ID, "request_id": EXECUTE_ID,
                                 "session_id": start["session_id"], "provider_id": submitted["provider_id"]}, CLAIMS)
    assert result["status"] == "confirmed"
