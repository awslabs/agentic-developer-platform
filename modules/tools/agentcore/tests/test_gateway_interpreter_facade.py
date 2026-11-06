"""Owner-scoped façade and Gateway interpreter integration with a synthetic provider."""

import base64
import hashlib
import json
import shlex
from types import SimpleNamespace
from uuid import UUID

import boto3
import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from moto import mock_aws

from agentcore_tools.gateway_interpreter import GatewayInterpreter
from tools_facade import server
from tools_facade.server import AgentCoreClient, Decision, create_app

START = "code_interpreter___start"
EXECUTE = "code_interpreter___execute"
RESULT = "code_interpreter___result"
FILE = "code_interpreter___file"
CLOSE = "code_interpreter___close"
HEADERS = {"Authorization": "Bearer original"}
START_ID = "00000000-0000-4000-8000-000000000011"
EXECUTE_ID = "00000000-0000-4000-8000-000000000012"
FILE_ID = "00000000-0000-4000-8000-000000000013"


class Authority:
    def __init__(self):
        self.grants = {START, EXECUTE, RESULT, FILE, CLOSE}
        self.listed = set(self.grants)
        self.owner = "user-one"
        self.calls = []

    def decision(self, tool):
        return {"schema_version": "1.0", "allowed": True, "receipt_id": "receipt-" + tool,
                "principal": {"user_id": self.owner, "root_human_id": self.owner,
                              "tenant_id": "tenant-one", "team_id": "team-one"}}

    async def list_tools(self, headers):
        assert headers["authorization"] == "Bearer original"
        return {**self.decision(START), "tools": [{"name": tool, "description": tool,
                "inputSchema": {"type": "object"}} for tool in sorted(self.listed)]}

    async def authorize(self, tool, arguments, headers):
        self.calls.append((tool, dict(arguments)))
        if tool not in self.grants:
            raise HTTPException(403, "grant revoked")
        return self.decision(tool)

    async def recover(self, tool, headers):
        return self.decision(tool)

    async def close(self, tool, headers):
        return self.decision(tool)


class Provider(AgentCoreClient):
    def __init__(self, client):
        self.store = GatewayInterpreter(client, self)
        self.calls = []
        self.lose_execute = False
        self.lose_file = False
        self.lose_close = False
        self.fail_stop = False
        self.stopped = False
        self.commands = []

    def start_code_interpreter_session(self, **kwargs):
        self.calls.append("start")
        return {"sessionId": "private-session"}

    def invoke_code_interpreter(self, **kwargs):
        self.calls.append(kwargs["name"])
        if kwargs["name"] == "startCommandExecution":
            self.commands.append(shlex.split(kwargs["arguments"]["command"])[2])
        response = {"startCommandExecution": {"taskId": "private-task"},
                    "getTask": {"taskStatus": "completed", "stdout": "42\n", "stderr": "", "exitCode": 0},
                    "executeCommand": {"stdout": json.dumps({"encoding": "base64",
                        "data": base64.b64encode(b"42\n").decode()})}}[kwargs["name"]]
        return {"stream": iter([{"result": {"structuredContent": response}}])}

    def stop_code_interpreter_session(self, **kwargs):
        self.calls.append("stop")
        if self.fail_stop:
            raise TimeoutError("provider stop response lost")
        self.stopped = True
        return {}

    def get_code_interpreter_session(self, **kwargs):
        return {"status": "TERMINATED" if self.stopped else "READY"}

    async def invoke(self, tool, arguments, decision):
        claims = {"principal": decision.principal.model_dump(exclude={"root_human_id"}),
                  "receipt_id": decision.receipt_id}
        if getattr(decision, "_native_interpreter", None) is not None:
            claims.update(native_interpreter=decision._native_interpreter, run_id=decision.run_id,
                          workload_id=decision.workload_id, run_expires_at=decision.run_expires_at)
            if decision.execution_deadline_at is not None:
                claims["execution_deadline_at"] = decision.execution_deadline_at
        operation = tool.split("___", 1)[1]
        def revalidate():
            return claims, arguments

        if operation in {"start", "execute", "file", "close", "execution_close"}:
            result = getattr(self.store, operation)(arguments, claims, revalidate)
        else:
            result = getattr(self.store, operation)(arguments, claims)
        if operation == "execute" and self.lose_execute:
            self.lose_execute = False
            raise RuntimeError("reply lost after the provider accepted code")
        if operation == "file" and self.lose_file:
            self.lose_file = False
            raise RuntimeError("reply lost after file artifact was saved")
        if operation == "close" and self.lose_close:
            self.lose_close = False
            raise RuntimeError("reply lost after provider stop")
        return {"jsonrpc": "2.0", "result": {"result": result, "receipt_id": result.get("receipt_id"),
                "user_id": decision.principal.user_id, "tenant_id": decision.principal.tenant_id}}


@pytest.fixture(params=["text", "structured"])
def gateway(monkeypatch, request):
    from agentcore_tools import gateway_interpreter as gateway_interpreter_module

    # These tests drive the explicit pending → result protocol; the in-call wait has its own tests.
    monkeypatch.setattr(gateway_interpreter_module, "EXECUTE_WAIT_SECONDS", 0.0)
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(TableName="facade-executions", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "execution_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "execution_id", "AttributeType": "S"},
                                  {"AttributeName": "cleanup_partition", "AttributeType": "S"},
                                  {"AttributeName": "next_cleanup_at", "AttributeType": "N"}],
            GlobalSecondaryIndexes=[{"IndexName": "cleanup-due",
                "KeySchema": [{"AttributeName": "cleanup_partition", "KeyType": "HASH"},
                              {"AttributeName": "next_cleanup_at", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"}}])
        client.create_table(TableName="interpreter-ops", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"},
                       {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"},
                                  {"AttributeName": "arrived_at", "AttributeType": "S"}])
        monkeypatch.setenv("ADP_TOOLS_EXECUTION_TABLE", "facade-executions")
        monkeypatch.setenv("ADP_TOOLS_TABLE", "interpreter-ops")
        monkeypatch.setenv("ADP_CODE_INTERPRETER_ID", "CodeInterpreterExample-0123456789")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        authority, provider = Authority(), Provider(client)
        lambda_invoke = provider.invoke
        boto_client = boto3.client
        monkeypatch.setenv("ADP_TOOLS_AGENTCORE_URL",
                           "https://tools.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp")
        monkeypatch.setenv("ADP_TOOLS_FACADE_ROLE_ARN", "arn:aws:iam::123456789012:role/example-facade")
        monkeypatch.setenv("ADP_TOOLS_ASSERTION_KEY_ARN", "arn:aws:kms:us-east-1:123456789012:key/example")
        monkeypatch.setattr(server, "signed_headers", lambda *args: {})
        monkeypatch.setattr(boto3, "client", lambda service, **kwargs:
            SimpleNamespace(sign=lambda **values: {"Signature": b"synthetic"}) if service == "kms"
            else boto_client(service, **kwargs))

        class Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, content, headers):
                packet = json.loads(content)
                claims = json.loads(base64.urlsafe_b64decode(headers["x-adp-assertion"] + "=="))["claims"]
                decision = Decision.model_validate({"schema_version": "1.0", "allowed": True,
                    "receipt_id": claims["receipt_id"],
                    "principal": {**claims["principal"], "root_human_id": claims["root_human_id"]},
                    **{name: claims[name] for name in ("run_id", "workload_id", "run_expires_at", "execution_deadline_at")
                       if name in claims}})
                decision._native_interpreter = claims.get("native_interpreter")
                tool = packet["params"]["name"].replace("code-interpreter___", "code_interpreter___")
                payload = (await lambda_invoke(tool, packet["params"]["arguments"], decision))["result"]
                wrapped = {"content": [{"type": "text", "text": json.dumps(payload)}]}
                if request.param == "structured":
                    wrapped["structuredContent"] = payload
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": packet["id"], "result": wrapped},
                                      request=httpx.Request("POST", url))

        monkeypatch.setattr(server.httpx, "AsyncClient", Client)
        monkeypatch.setattr(provider, "invoke", AgentCoreClient.invoke.__get__(provider))
        yield authority, provider


def post(client, tool, arguments, request_id, execution_id=None):
    payload = {"tool": tool, "arguments": arguments, "request_id": request_id}
    if execution_id:
        payload["execution_id"] = execution_id
    return client.post("/agent/tools/call", json=payload, headers=HEADERS)


def path(receipt):
    return f'/agent/tools/executions/{receipt["execution_id"]}/operations/{receipt["request_id"]}'


def test_public_interpreter_lifecycle_returns_exact_file_bytes(gateway):
    authority, provider = gateway
    client = TestClient(create_app(gateway=authority, agentcore=provider))
    started = post(client, START, {}, START_ID)
    assert started.status_code == 200
    execution_id = started.json()["execution_id"]
    session_id = started.json()["result"]["result"]["result"]["session_id"]
    code = "from pathlib import Path; print(6*7); Path('/tmp/sample.txt').write_bytes(b'42\\n')"
    executed = post(client, EXECUTE, {"session_id": session_id, "language": "python", "code": code},
                    EXECUTE_ID, execution_id)
    assert executed.status_code == 202
    assert provider.commands == [code]
    submitted = provider.store.operation_get({"execution_id": execution_id, "request_id": EXECUTE_ID},
        {"principal": {"user_id": authority.owner, "tenant_id": "tenant-one", "team_id": "team-one"}})
    result = post(client, RESULT, {"session_id": session_id, "provider_id": submitted["provider_id"]},
                  EXECUTE_ID, execution_id)
    assert result.status_code == 200
    outcome = result.json()["result"]["result"]["result"]["result"]
    assert outcome["stdout"].strip() == "42" and outcome["exitCode"] == 0
    file = post(client, FILE, {"session_id": session_id, "path": "/tmp/sample.txt"}, FILE_ID, execution_id)
    assert file.status_code == 200
    artifact = file.json()["result"]["result"]["result"]["artifact"]
    assert artifact["content_sha256"] == hashlib.sha256(b"42\n").hexdigest()
    read = client.get(path(file.json()) + "/artifacts/result", params={"tool": FILE}, headers=HEADERS)
    assert read.status_code == 200
    assert read.json()["content_type"] == "application/octet-stream"
    assert base64.b64decode(read.json()["data_base64"]) == b"42\n"
    closed = post(client, CLOSE, {"session_id": session_id},
                  "00000000-0000-4000-8000-000000000014", execution_id)
    assert closed.status_code == 200 and closed.json()["status"] == "confirmed"
    assert closed.json()["result"]["result"]["result"]["termination_confirmed"] is True
    assert provider.get_code_interpreter_session()["status"] == "TERMINATED"


@pytest.mark.parametrize("lose_response", [False, True])
def test_explicit_execute_refusal_recovers_without_provider_task(gateway, monkeypatch, lose_response):
    authority, provider = gateway
    client = TestClient(create_app(gateway=authority, agentcore=provider))
    started = post(client, START, {}, START_ID).json()
    handle = started["result"]["result"]["result"]["session_id"]

    def refuse(**kwargs):
        provider.calls.append(kwargs["name"])
        return {"stream": iter([{"result": {"isError": True}}])}

    monkeypatch.setattr(provider, "invoke_code_interpreter", refuse)
    provider.lose_execute = lose_response
    arguments = {"session_id": handle, "language": "python", "code": "print(42)"}
    executed = post(client, EXECUTE, arguments, EXECUTE_ID, started["execution_id"])
    assert executed.status_code == (202 if lose_response else 200)
    restarted = TestClient(create_app(gateway=authority, agentcore=provider))
    recovered = restarted.get(path(executed.json()), params={"tool": EXECUTE}, headers=HEADERS).json()
    assert recovered["status"] == "confirmed" and recovered["result_status"] == "error"
    receipt = recovered["result"]["result"]["result"]
    assert receipt["status"] == "failed" and "provider_id" not in receipt
    assert receipt["result"] == {"status": "failed", "error": "provider_refused"}
    assert post(restarted, EXECUTE, arguments, EXECUTE_ID, started["execution_id"]).json() == recovered
    assert provider.calls == ["start", "startCommandExecution"]


def test_facade_interpreter_restart_recovery_file_and_provider_close(gateway):
    authority, provider = gateway
    client = TestClient(create_app(gateway=authority, agentcore=provider))
    started = post(client, START, {}, START_ID)
    assert started.status_code == 200
    original = started.json()
    execution_id = original["execution_id"]
    assert str(UUID(execution_id)) == execution_id
    handle = original["result"]["result"]["result"]["session_id"]
    provider.lose_execute = True
    execute = post(client, EXECUTE, {"session_id": handle, "language": "python", "code": "print(42)"},
                   EXECUTE_ID, execution_id)
    assert execute.status_code == 202 and execute.json()["status"] == "unknown"
    restarted = TestClient(create_app(gateway=authority, agentcore=provider))
    recovered = restarted.get(path(execute.json()), params={"tool": EXECUTE}, headers=HEADERS)
    assert recovered.status_code == 200 and recovered.json()["status"] == "confirmed"
    assert recovered.json()["result"]["result"]["result"]["result"]["stdout"] == "42\n"
    assert post(restarted, EXECUTE, {"session_id": handle, "language": "python", "code": "print(42)"},
                EXECUTE_ID, execution_id).json() == recovered.json()
    assert provider.calls.count("startCommandExecution") == provider.calls.count("getTask") == 1
    file = post(restarted, FILE, {"session_id": handle, "path": "/tmp/sample.txt"}, FILE_ID, execution_id)
    assert file.status_code == 200 and file.json()["status"] == "confirmed"
    evidence = file.json()["result"]["result"]["result"]["artifact"]
    assert evidence["content_sha256"] == hashlib.sha256(b"42\n").hexdigest()
    authority.grants.remove(FILE)
    artifact = restarted.get(path(file.json()) + "/artifacts/result", params={"tool": FILE}, headers=HEADERS)
    assert artifact.status_code == 200 and base64.b64decode(artifact.json()["data_base64"]) == b"42\n"
    assert artifact.json()["sha256"] == evidence["content_sha256"]
    closed = post(restarted, CLOSE, {"session_id": handle}, "00000000-0000-4000-8000-000000000014", execution_id)
    assert closed.status_code == 200 and closed.json()["status"] == "confirmed"
    assert provider.calls.count("stop") == 1
    root = boto3.client("dynamodb", region_name="us-east-1").get_item(TableName="facade-executions",
        Key={"execution_id": {"S": execution_id}}, ConsistentRead=True)["Item"]
    assert root["state"]["S"] == "closed" and root["cleanup_status"]["S"] == "confirmed"
    assert root["cleanup_evidence"]["S"] == "provider_terminated"
    assert provider.get_code_interpreter_session()["status"] == "TERMINATED"
    after = restarted.get(path(file.json()) + "/artifacts/result", params={"tool": FILE}, headers=HEADERS)
    assert after.status_code == 200 and base64.b64decode(after.json()["data_base64"]) == b"42\n"
    retry = restarted.post(f"/agent/tools/executions/{execution_id}/close", params={"tool": START}, headers=HEADERS)
    assert retry.status_code == 200 and retry.json()["cleanup_status"] == "confirmed"
    assert provider.calls.count("stop") == 1
    authority.owner = "user-two"
    assert restarted.get(path(file.json()), params={"tool": FILE}, headers=HEADERS).status_code == 404
    assert post(restarted, EXECUTE, {"session_id": handle, "language": "python", "code": "print(42)"},
                "00000000-0000-4000-8000-000000000015", execution_id).status_code == 404
    assert provider.calls.count("startCommandExecution") == 1


def test_confirmed_ordinary_interpreter_close_repairs_root_after_restart(gateway):
    authority, provider = gateway
    client = TestClient(create_app(gateway=authority, agentcore=provider))
    started = post(client, START, {}, START_ID).json()
    identifier = started["execution_id"]
    handle = started["result"]["result"]["result"]["session_id"]
    closed = post(client, CLOSE, {"session_id": handle}, FILE_ID, identifier).json()
    db = boto3.client("dynamodb", region_name="us-east-1")
    db.update_item(TableName="facade-executions", Key={"execution_id": {"S": identifier}},
        UpdateExpression="SET #state = :closing, cleanup_status = :unknown REMOVE cleanup_evidence",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={":closing": {"S": "closing"}, ":unknown": {"S": "unknown"}})
    restarted = TestClient(create_app(gateway=authority, agentcore=provider))
    assert post(restarted, CLOSE, {"session_id": handle}, FILE_ID, identifier).json() == closed
    root = db.get_item(TableName="facade-executions", Key={"execution_id": {"S": identifier}},
                       ConsistentRead=True)["Item"]
    assert root["state"]["S"] == "closed" and root["cleanup_evidence"]["S"] == "provider_terminated"
    assert provider.calls == ["start", "stop"]


@pytest.mark.parametrize("read_failure", [False, True])
def test_legacy_facade_and_interpreter_terminal_flags_require_new_proof(gateway, monkeypatch, read_failure):
    authority, provider = gateway
    client = TestClient(create_app(gateway=authority, agentcore=provider))
    started = post(client, START, {}, START_ID).json()
    identifier = started["execution_id"]
    db = boto3.client("dynamodb", region_name="us-east-1")
    key = {"execution_id": {"S": identifier}}
    db.update_item(TableName="facade-executions", Key=key,
        UpdateExpression="SET #state = :closed, cleanup_status = :confirmed REMOVE cleanup_partition, next_cleanup_at",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={":closed": {"S": "closed"}, ":confirmed": {"S": "confirmed"}})
    before = db.get_item(TableName="facade-executions", Key=key, ConsistentRead=True)["Item"]
    claims = {"principal": {"tenant_id": "tenant-one", "user_id": "user-one", "team_id": "team-one"}}
    db.update_item(TableName="interpreter-ops", Key=provider.store._key(claims, identifier, "CODE_SESSION"),
        UpdateExpression="SET cleanup_state = :closed REMOVE cleanup_partition, next_cleanup_at",
        ExpressionAttributeValues={":closed": {"S": "closed"}})
    path = f"/agent/tools/executions/{identifier}/close"
    authority.owner = "foreign"
    assert client.post(path, params={"tool": START}, headers=HEADERS).status_code == 404
    assert db.get_item(TableName="facade-executions", Key=key, ConsistentRead=True)["Item"] == before
    authority.owner = "user-one"
    original_get = provider.get_code_interpreter_session
    # A Stop acknowledgement must not make a still-READY Get terminal.
    monkeypatch.setattr(provider, "get_code_interpreter_session", lambda **kwargs: {"status": "READY"})
    if read_failure:
        def fail(**kwargs):
            raise TimeoutError("provider read unavailable")
        monkeypatch.setattr(provider, "get_code_interpreter_session", fail)
    pending = client.post(path, params={"tool": START}, headers=HEADERS)
    assert pending.status_code == 202 and pending.json()["cleanup_status"] == "unknown"
    root = db.get_item(TableName="facade-executions", Key=key, ConsistentRead=True)["Item"]
    assert root["state"]["S"] == "closing" and root["cleanup_partition"]["S"] == "due"
    for field in ("generation", "deadline_at", "accepted_operations", "unresolved_operations", "frozen_grants"):
        assert root[field] == before[field]
    assert "cleanup_evidence" not in root
    assert provider.calls == (["start"] if read_failure else ["start", "stop"])
    monkeypatch.setattr(provider, "get_code_interpreter_session", original_get)
    provider.stopped = True
    proved = client.post(path, params={"tool": START}, headers=HEADERS)
    assert proved.status_code == 200 and proved.json()["cleanup_status"] == "confirmed"
    assert db.get_item(TableName="facade-executions", Key=key)["Item"]["cleanup_evidence"]["S"] == "provider_terminated"
    monkeypatch.setattr(provider, "get_code_interpreter_session", lambda **kwargs: pytest.fail("proved root reread provider"))
    assert client.post(path, params={"tool": START}, headers=HEADERS).status_code == 200


def test_frozen_interpreter_grant_and_live_revocation_refuse_new_work(gateway):
    authority, provider = gateway
    authority.listed.remove(EXECUTE)
    client = TestClient(create_app(gateway=authority, agentcore=provider))
    start = post(client, START, {}, START_ID).json()
    handle = start["result"]["result"]["result"]["session_id"]
    forbidden = post(client, EXECUTE, {"session_id": handle, "language": "python", "code": "print(42)"},
                     EXECUTE_ID, start["execution_id"])
    assert forbidden.status_code == 404 and provider.calls == ["start"]
    authority.listed.add(EXECUTE)
    second = post(client, START, {}, "00000000-0000-4000-8000-000000000016").json()
    authority.grants.remove(EXECUTE)
    denied = post(client, EXECUTE, {"session_id": second["result"]["result"]["result"]["session_id"],
                                 "language": "python", "code": "print(42)"},
                  "00000000-0000-4000-8000-000000000017", second["execution_id"])
    assert denied.status_code == 403 and provider.calls == ["start", "start"]


def test_lost_file_reply_and_uncertain_stop_recover_without_repeating_file(gateway):
    authority, provider = gateway
    client = TestClient(create_app(gateway=authority, agentcore=provider))
    started = post(client, START, {}, START_ID).json()
    handle = started["result"]["result"]["result"]["session_id"]
    provider.lose_file = True
    file = post(client, FILE, {"session_id": handle, "path": "/tmp/sample.txt"},
                FILE_ID, started["execution_id"])
    assert file.status_code == 202 and file.json()["status"] == "unknown"
    restarted = TestClient(create_app(gateway=authority, agentcore=provider))
    recovered = restarted.get(path(file.json()), params={"tool": FILE}, headers=HEADERS)
    assert recovered.status_code == 200 and recovered.json()["status"] == "confirmed"
    assert provider.calls.count("executeCommand") == 1
    authority.grants.remove(FILE)
    provider.fail_stop = True
    close_path = f'/agent/tools/executions/{started["execution_id"]}/close'
    pending = restarted.post(close_path, params={"tool": START}, headers=HEADERS)
    assert pending.status_code == 202 and pending.json()["cleanup_status"] == "unknown"
    assert provider.calls.count("stop") == 1
    provider.fail_stop = False
    confirmed = restarted.post(close_path, params={"tool": START}, headers=HEADERS)
    assert confirmed.status_code == 200 and confirmed.json()["cleanup_status"] == "confirmed"
    assert provider.calls.count("stop") == 2
    artifact = restarted.get(path(file.json()) + "/artifacts/result", params={"tool": FILE}, headers=HEADERS)
    assert artifact.status_code == 200
    assert hashlib.sha256(base64.b64decode(artifact.json()["data_base64"])).hexdigest() == artifact.json()["sha256"]
    assert provider.calls.count("executeCommand") == 1


def test_interpreter_listing_principal_mismatch_refuses_before_claim(gateway):
    authority, provider = gateway

    async def wrong_listing(headers):
        result = await Authority.list_tools(authority, headers)
        result["principal"]["user_id"] = "foreign"
        return result

    authority.list_tools = wrong_listing
    denied = post(TestClient(create_app(gateway=authority, agentcore=provider)), START, {}, START_ID)
    assert denied.status_code == 403 and provider.calls == []


def test_mismatched_provider_principal_cannot_confirm_receipt(gateway):
    authority, provider = gateway
    original = provider.invoke

    async def foreign_identity(tool, arguments, decision):
        result = await original(tool, arguments, decision)
        if tool == START:
            result["result"]["user_id"] = "foreign"
        return result

    provider.invoke = foreign_identity
    client = TestClient(create_app(gateway=authority, agentcore=provider))
    reply = post(client, START, {}, START_ID)
    assert reply.status_code == 202 and reply.json()["status"] == "unknown"
    assert provider.calls == ["start"]
    authority.owner = "user-two"
    assert client.get(path(reply.json()), params={"tool": START}, headers=HEADERS).status_code == 404
    assert provider.calls == ["start"]


def test_client_cannot_supply_different_signed_interpreter_identity(gateway):
    authority, provider = gateway
    client = TestClient(create_app(gateway=authority, agentcore=provider))
    reply = post(client, START, {"execution_id": "00000000-0000-4000-8000-000000000088"}, START_ID)
    assert reply.status_code == 422 and provider.calls == []
    assert authority.calls == [(START, {"execution_id": "00000000-0000-4000-8000-000000000088"})]


def test_nonzero_provider_exit_is_visible_as_failed_tool_result(gateway, monkeypatch):
    authority, provider = gateway
    client = TestClient(create_app(gateway=authority, agentcore=provider))
    started = post(client, START, {}, START_ID).json()
    handle = started["result"]["result"]["result"]["session_id"]
    submitted = post(client, EXECUTE, {"session_id": handle, "language": "python", "code": "raise SystemExit(2)"},
                     EXECUTE_ID, started["execution_id"])
    assert submitted.status_code == 202
    provider_id = provider.store.operation_get({"execution_id": started["execution_id"], "request_id": EXECUTE_ID},
        {"principal": {"user_id": authority.owner, "tenant_id": "tenant-one", "team_id": "team-one"}})["provider_id"]
    original = provider.invoke_code_interpreter

    def failed_execution(**kwargs):
        if kwargs["name"] == "getTask":
            provider.calls.append("getTask")
            return {"stream": iter([{"result": {"structuredContent": {
                "taskStatus": "completed", "stdout": "", "stderr": "failed", "exitCode": 2}}}])}
        return original(**kwargs)

    monkeypatch.setattr(provider, "invoke_code_interpreter", failed_execution)
    result = post(client, RESULT, {"session_id": handle, "provider_id": provider_id},
                  EXECUTE_ID, started["execution_id"])
    assert result.status_code == 200 and result.json()["result_status"] == "error"
    receipt = result.json()["result"]["result"]["result"]
    assert receipt["status"] == "failed"
    assert receipt["result"]["exitCode"] == 2 and receipt["result"]["stderr"] == "failed"
    count = provider.calls.count("getTask")
    assert client.get(path(submitted.json()), params={"tool": EXECUTE}, headers=HEADERS).status_code == 200
    assert provider.calls.count("getTask") == count


def test_result_poll_uses_original_execute_identity_without_rerunning_code(gateway):
    authority, provider = gateway
    client = TestClient(create_app(gateway=authority, agentcore=provider))
    start = post(client, START, {}, START_ID).json()
    execution_id = start["execution_id"]
    handle = start["result"]["result"]["result"]["session_id"]
    execute = post(client, EXECUTE, {"session_id": handle, "language": "python", "code": "print(42)"},
                   EXECUTE_ID, execution_id)
    assert execute.status_code == 202
    provider_id = provider.store.operation_get({"execution_id": execution_id, "request_id": EXECUTE_ID},
        {"principal": {"user_id": authority.owner, "tenant_id": "tenant-one", "team_id": "team-one"}})["provider_id"]
    restarted = TestClient(create_app(gateway=authority, agentcore=provider))
    missing = post(restarted, RESULT, {"session_id": handle, "provider_id": provider_id},
                   "00000000-0000-4000-8000-000000000018", execution_id)
    assert missing.status_code == 404
    result = post(restarted, RESULT, {"session_id": handle, "provider_id": provider_id},
                  EXECUTE_ID, execution_id)
    assert result.status_code == 200
    assert result.json()["result"]["result"]["result"]["result"]["stdout"] == "42\n"
    assert post(restarted, RESULT, {"session_id": handle, "provider_id": provider_id},
                EXECUTE_ID, execution_id).json() == result.json()
    assert provider.calls.count("startCommandExecution") == provider.calls.count("getTask") == 1


def test_lost_close_reply_recovers_owned_termination_without_repeating_stop(gateway):
    authority, provider = gateway
    client = TestClient(create_app(gateway=authority, agentcore=provider))
    start = post(client, START, {}, START_ID).json()
    execution_id = start["execution_id"]
    handle = start["result"]["result"]["result"]["session_id"]
    provider.lose_close = True
    close_id = "00000000-0000-4000-8000-000000000019"
    pending = post(client, CLOSE, {"session_id": handle}, close_id, execution_id)
    assert pending.status_code == 202 and pending.json()["status"] == "unknown"
    restarted = TestClient(create_app(gateway=authority, agentcore=provider))
    recovered = restarted.get(path(pending.json()), params={"tool": CLOSE}, headers=HEADERS)
    assert recovered.status_code == 200 and recovered.json()["status"] == "confirmed"
    assert post(restarted, CLOSE, {"session_id": handle}, close_id, execution_id).json() == recovered.json()
    assert provider.calls.count("stop") == 1
    authority.owner = "user-two"
    assert restarted.get(path(pending.json()), params={"tool": CLOSE}, headers=HEADERS).status_code == 404


def test_revoked_execution_grant_still_allows_owned_close(gateway):
    authority, provider = gateway
    client = TestClient(create_app(gateway=authority, agentcore=provider))
    start = post(client, START, {}, START_ID).json()
    handle = start["result"]["result"]["result"]["session_id"]
    authority.grants.difference_update({EXECUTE, CLOSE})
    refused = post(client, EXECUTE, {"session_id": handle, "language": "python", "code": "print(42)"},
                   EXECUTE_ID, start["execution_id"])
    assert refused.status_code == 403
    closed = post(client, CLOSE, {"session_id": handle}, FILE_ID, start["execution_id"])
    assert closed.status_code == 200 and closed.json()["status"] == "confirmed"
    assert provider.calls == ["start", "stop"]
    authority.owner = "foreign"
    assert post(client, CLOSE, {"session_id": handle},
                "00000000-0000-4000-8000-000000000020", start["execution_id"]).status_code == 404


def test_generic_close_accepts_owned_execute_reference(gateway):
    authority, provider = gateway
    client = TestClient(create_app(gateway=authority, agentcore=provider))
    started = post(client, START, {}, START_ID).json()
    handle = started["result"]["result"]["result"]["session_id"]
    executed = post(client, EXECUTE, {"session_id": handle, "language": "python",
        "code": "print(42)"}, EXECUTE_ID, started["execution_id"]).json()
    assert client.get(path(executed), params={"tool": EXECUTE}, headers=HEADERS).json()["status"] == "confirmed"
    close_path = f'/agent/tools/executions/{started["execution_id"]}/close'
    closed = client.post(close_path, params={"tool": EXECUTE}, headers=HEADERS)
    assert closed.status_code == 200 and closed.json()["cleanup_status"] == "confirmed"
    assert provider.calls.count("stop") == 1
    authority.owner = "foreign"
    assert client.post(close_path, params={"tool": EXECUTE}, headers=HEADERS).status_code == 404
