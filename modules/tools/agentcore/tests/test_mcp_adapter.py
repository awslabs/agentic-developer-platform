"""Gateway tools accept only the facade's signed, principal-bound invocation."""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import boto3
import httpx
import pytest
from fastapi import HTTPException
from agentcore_tools import handler, mcp_adapter
from moto import mock_aws
from tools_facade import server as facade
from tools_interceptor import handler as interceptor_module

ROLE = "arn:aws:iam::123456789012:role/example-tools-facade"
KEY = "arn:aws:kms:us-east-1:123456789012:key/example"
SIGNATURE = b"synthetic-signature"
EXECUTION_ID = "00000000-0000-4000-8000-000000000001"
REQUEST_ID = "00000000-0000-4000-8000-000000000002"


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def token(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def gateway_event(*, principal=None, query="example", root="user-one", tool="websearch___search", arguments=None):
    args = {"query": query, "execution_id": EXECUTION_ID, "request_id": REQUEST_ID} if arguments is None else arguments
    claims = {
        "caller_role_arn": ROLE,
        "principal": principal or {"user_id": "user-one", "tenant_id": "tenant-one", "team_id": "team-one"},
        "root_human_id": root,
        "receipt_id": "receipt-one", "method": "tools/call", "tool": tool,
        "arguments_sha256": hashlib.sha256(encoded(args)).hexdigest(),
        "issued_at": int(time.time()) - 1, "expires_at": int(time.time()) + 30,
    }
    if tool in {"browser___start", "code-interpreter___start"}:
        claims["execution_deadline_at"] = int(time.time()) + 300
    signature = hmac.digest(SIGNATURE, encoded(claims), "sha256")
    return {**args, "adp_assertion": token(encoded({"claims": claims, "signature": token(signature)}))}


def test_cleanup_assertion_uses_separate_key_and_only_admits_provider_close(monkeypatch):
    worker_role = "arn:aws:iam::123456789012:role/adp-dev-role-tools-reconciler"
    worker_key = "arn:aws:kms:us-east-1:123456789012:key/cleanup-only"
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ADP_TOOLS_FACADE_ROLE_ARN", ROLE)
    monkeypatch.setenv("ADP_TOOLS_ASSERTION_KEY_ARN", KEY)
    monkeypatch.setenv("ADP_TOOLS_RECONCILER_ROLE_ARN", worker_role)
    monkeypatch.setenv("ADP_TOOLS_RECONCILER_KEY_ARN", worker_key)
    verified_keys = []

    class KMS:
        def verify(self, **kwargs):
            verified_keys.append(kwargs["KeyId"])
            return {"SignatureValid": kwargs["KeyId"] == worker_key and hmac.compare_digest(
                kwargs["Signature"], hmac.digest(SIGNATURE, kwargs["Message"], "sha256"))}

    monkeypatch.setattr(mcp_adapter.boto3, "client", lambda service, **kwargs: KMS())

    def call(tool, role=worker_role):
        event = gateway_event(tool=tool, arguments={"execution_id": EXECUTION_ID})
        envelope = json.loads(base64.urlsafe_b64decode(event["adp_assertion"] + "=="))
        claims = envelope["claims"]
        claims.update({"caller_role_arn": role, "cleanup_reconcile": True,
                       "run_id": "original-run", "workload_id": "original-worker", "run_expires_at": None})
        event["adp_assertion"] = token(encoded({"claims": claims,
            "signature": token(hmac.digest(SIGNATURE, encoded(claims), "sha256"))}))
        context = SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": tool}))
        return event, context

    event, context = call("browser___execution_close")
    claims, arguments = mcp_adapter.verified_call(event, context, "browser___execution_close")
    assert arguments == {"execution_id": EXECUTION_ID} and claims["run_id"] == "original-run"
    assert verified_keys == [worker_key]
    for tool, role in (("browser___start", worker_role),
                       ("browser___execution_close", ROLE), ("websearch___search", worker_role)):
        event, context = call(tool, role)
        with pytest.raises(HTTPException) as denied:
            mcp_adapter.verified_call(event, context, tool)
        assert denied.value.status_code == 403


@pytest.fixture
def gateway(monkeypatch):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(TableName="gateway-search", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"},
                       {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"},
                                  {"AttributeName": "arrived_at", "AttributeType": "S"}])
        monkeypatch.setenv("ADP_TOOLS_TABLE", "gateway-search")
        yield from gateway_fixture(monkeypatch, client)


def gateway_fixture(monkeypatch, client):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ADP_TOOLS_FACADE_ROLE_ARN", ROLE)
    monkeypatch.setenv("ADP_TOOLS_ASSERTION_KEY_ARN", KEY)
    monkeypatch.setenv("ADP_WEBSEARCH_ENABLED", "true")
    calls = []

    class KMS:
        def verify(self, **kwargs):
            assert kwargs["KeyId"] == KEY
            assert kwargs["SigningAlgorithm"] == "RSASSA_PSS_SHA_256"
            return {"SignatureValid": hmac.compare_digest(
                kwargs["Signature"], hmac.digest(SIGNATURE, kwargs["Message"], "sha256"),
            )}

    monkeypatch.setattr(mcp_adapter.boto3, "client", lambda service, **kwargs: KMS() if service == "kms" else client)
    def search(args, *, before_search):
        before_search()
        calls.append(args)
        return {"status": "completed", "results": []}

    monkeypatch.setattr(handler.websearch, "search", search)
    context = SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": "websearch___search"}))
    yield context, calls


def test_signed_gateway_search_retains_user_tenant_and_receipt(gateway):
    context, calls = gateway
    result = handler.lambda_handler(gateway_event(), context)
    assert result["result"]["status"] == "confirmed"
    assert result["result"]["result"] == {"status": "completed", "results": []}
    assert result["receipt_id"] == "receipt-one"
    assert result["tenant_id"] == "tenant-one" and result["user_id"] == "user-one"
    assert calls == [{"query": "example"}]


@pytest.mark.parametrize("alter", [
    lambda event: event.update(query="changed"),
    lambda event: event.update(adp_assertion="forged"),
    lambda event: event.pop("adp_assertion"),
    lambda event: event.update(query=""),
])
def test_tampered_or_invalid_gateway_search_does_not_call_provider(gateway, alter):
    context, calls = gateway
    event = gateway_event()
    alter(event)
    assert handler.lambda_handler(event, context)["isError"] is True
    assert calls == []


def test_foreign_principal_and_tool_identity_are_refused(gateway):
    context, calls = gateway
    foreign = gateway_event(principal={"user_id": "user-two", "tenant_id": "tenant-two", "team_id": "team-two"})
    assert handler.lambda_handler(foreign, context)["isError"] is True
    wrong_tool = gateway_event(tool="browser___start")
    assert handler.lambda_handler(wrong_tool, context)["isError"] is True
    assert calls == []

def test_expired_interpreter_grant_is_refused_before_provider_or_kms(gateway, monkeypatch):
    context, provider_calls = gateway
    context.client_context.custom["bedrockAgentCoreToolName"] = "code-interpreter___file"
    event = gateway_event(tool="code-interpreter___file", arguments={
        "execution_id": EXECUTION_ID, "request_id": REQUEST_ID,
        "session_id": "a" * 64, "path": "/tmp/output.txt"})
    envelope = json.loads(base64.urlsafe_b64decode(event["adp_assertion"] + "=="))
    claims = envelope["claims"]
    claims.update({"run_id": "run-one", "workload_id": "workload-one",
                   "run_expires_at": int(time.time()) - 1})
    signature = hmac.digest(SIGNATURE, encoded(claims), "sha256")
    event["adp_assertion"] = token(encoded({"claims": claims, "signature": token(signature)}))

    def unexpected(*args, **kwargs):
        raise AssertionError("expired grant reached KMS or provider")

    monkeypatch.setattr(mcp_adapter.boto3, "client", unexpected)
    with pytest.raises(HTTPException) as denied:
        mcp_adapter.verified_stateful(event, context)
    assert denied.value.status_code == 403 and provider_calls == []


SESSION_ID = "a" * 64


@pytest.mark.parametrize("tool,arguments", [
    ("code-interpreter___start", {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID}),
    ("code-interpreter___execute", {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID,
                                     "session_id": SESSION_ID, "language": "python", "code": "print(42)"}),
    ("code-interpreter___result", {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID,
                                    "session_id": SESSION_ID, "provider_id": "provider-run"}),
    ("code-interpreter___file", {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID,
                                  "session_id": SESSION_ID, "path": "/tmp/out.txt"}),
    ("code-interpreter___close", {"execution_id": EXECUTION_ID, "session_id": SESSION_ID}),
    ("code-interpreter___operation_get", {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID}),
    ("code-interpreter___artifact_read", {"execution_id": EXECUTION_ID, "artifact_id": "artifact-1", "offset": 0}),
    ("code-interpreter___execution_close", {"execution_id": EXECUTION_ID}),
    ("browser___start", {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID,
                          "url": "https://example.com"}),
    ("browser___step", {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID,
                         "session_id": SESSION_ID, "view_id": "view-one", "action": "screenshot"}),
    ("browser___inspect", {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID,
                            "session_id": SESSION_ID, "view_id": "view-one"}),
    ("browser___close", {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID, "session_id": SESSION_ID}),
    ("browser___operation_get", {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID}),
    ("browser___artifact_read", {"execution_id": EXECUTION_ID, "artifact_id": "artifact-1"}),
    ("browser___execution_close", {"execution_id": EXECUTION_ID}),
])
def test_stateful_gateway_events_reach_only_owned_dispatch(gateway, monkeypatch, tool, arguments):
    context, search_calls = gateway
    context.client_context.custom["bedrockAgentCoreToolName"] = tool
    monkeypatch.setenv("ADP_CODE_INTERPRETER_ENABLED", "true")
    monkeypatch.setenv("ADP_TASK_BROWSER_HTTP_ENABLED", "true")
    dispatches = []

    def dispatch(target, operation, payload, claims, revalidate, assertion=None):
        assert revalidate() == (claims, payload, target, operation)
        assert (assertion is not None) == (target == "browser")
        dispatches.append((target, operation, payload, claims["principal"]))
        return {"accepted": True}

    monkeypatch.setattr(handler, "dispatch_stateful", dispatch)
    result = handler.lambda_handler(gateway_event(tool=tool, arguments=arguments), context)
    assert result == {"result": {"accepted": True}, "receipt_id": "receipt-one",
                      "tenant_id": "tenant-one", "user_id": "user-one"}
    assert dispatches == [(tool.split("___")[0], tool.split("___")[1], arguments,
                           {"user_id": "user-one", "tenant_id": "tenant-one", "team_id": "team-one"})]
    assert search_calls == []


@pytest.mark.parametrize("tool,arguments", [
    ("code-interpreter___execute", {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID,
                                     "session_id": SESSION_ID, "code": "print(42)", "language": "javascript"}),
    ("code-interpreter___file", {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID,
                                  "session_id": SESSION_ID, "path": "/etc/passwd"}),
    ("browser___start", {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID,
                          "url": "http://127.0.0.1"}),
    ("browser___step", {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID,
                         "session_id": SESSION_ID, "view_id": "view-one", "action": "navigate"}),
    ("browser___start", {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID,
                          "url": "https://example.com", "attempt": {"task_id": "forged"}}),
    ("browser___close", {"execution_id": EXECUTION_ID, "session_id": "foreign"}),
    ("code-interpreter___start", {"execution_id": "not-uuid", "request_id": REQUEST_ID}),
])
def test_invalid_stateful_gateway_events_do_not_dispatch(gateway, monkeypatch, tool, arguments):
    context, search_calls = gateway
    context.client_context.custom["bedrockAgentCoreToolName"] = tool
    monkeypatch.setenv("ADP_CODE_INTERPRETER_ENABLED", "true")
    monkeypatch.setenv("ADP_TASK_BROWSER_HTTP_ENABLED", "true")
    monkeypatch.setattr(handler, "dispatch_stateful", lambda *args, **kwargs: pytest.fail("Dispatched invalid event"))
    result = handler.lambda_handler(gateway_event(tool=tool, arguments=arguments), context)
    assert result == {"isError": True, "content": [{"type": "text", "text": "tool_refused"}]}
    assert search_calls == []

def test_facade_interceptor_lambda_search_and_receipt(monkeypatch):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(TableName="gateway-search", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"},
                       {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"},
                                  {"AttributeName": "arrived_at", "AttributeType": "S"}])
        monkeypatch.setenv("ADP_TOOLS_TABLE", "gateway-search")
        facade_search_fixture(monkeypatch, client)


def facade_search_fixture(monkeypatch, client):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ADP_TOOLS_FACADE_ROLE_ARN", ROLE)
    monkeypatch.setenv("ADP_TOOLS_ASSERTION_KEY_ARN", KEY)
    monkeypatch.setenv("ADP_TOOLS_AGENTCORE_URL", "https://example.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp")
    monkeypatch.setenv("ADP_WEBSEARCH_ENABLED", "true")
    monkeypatch.setenv("ADP_WEBSEARCH_GATEWAY_URL", "https://search.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp")
    monkeypatch.setenv("ADP_WEBSEARCH_REGION", "us-east-1")
    monkeypatch.setenv("ADP_WEBSEARCH_TARGET", "web-search")
    monkeypatch.setenv("ADP_WEBSEARCH_CONNECTOR_VERSION", "1.2.0")
    provider_calls = []

    class KMS:
        def sign(self, **kwargs):
            assert kwargs["KeyId"] == KEY
            return {"Signature": hmac.digest(SIGNATURE, kwargs["Message"], "sha256")}

        def verify(self, **kwargs):
            assert kwargs["KeyId"] == KEY
            return {"SignatureValid": hmac.compare_digest(
                kwargs["Signature"], hmac.digest(SIGNATURE, kwargs["Message"], "sha256"),
            )}

    monkeypatch.setattr(mcp_adapter.boto3, "client", lambda service, **kwargs: KMS() if service == "kms" else client)
    monkeypatch.setattr(facade, "signed_headers", lambda *args: {"Content-Type": "application/json"})

    def connector(endpoint, region, method, params):
        assert endpoint.endswith("/mcp") and region == "us-east-1"
        if method == "tools/list":
            return {"tools": [{"name": "web-search___WebSearch", "inputSchema": {"properties": {
                "query": {"type": "string"}, "maxResults": {"type": "integer"},
                "filters": {"type": "object", "properties": {"domainFilter": {}, "publishedDateFilter": {}}},
            }}}]}
        provider_calls.append(params)
        return {"content": [{"type": "text", "text": json.dumps({"results": [{
            "url": "https://example.com", "text": "example document", "title": "Example",
        }, {"url": "https://other.example.com", "text": "second document"}]})}]}

    original_search = handler.websearch.search
    monkeypatch.setattr(handler.websearch, "search", lambda args, *, before_search: original_search(
        args, call=connector, before_search=before_search,
    ))

    class Gateway:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, endpoint, content, headers):
            packet = json.loads(content)
            event = {"interceptorInputVersion": "1.0", "mcp": {"gatewayRequest": {
                "body": packet, "headers": headers,
            }}}
            transformed = interceptor_module.handler(event, None)["mcp"]["transformedGatewayRequest"]["body"]
            assert packet["params"]["arguments"] == {"query": "example", "execution_id": EXECUTION_ID,
                "request_id": REQUEST_ID}
            context = SimpleNamespace(client_context=SimpleNamespace(custom={
                "bedrockAgentCoreToolName": transformed["params"]["name"],
            }))
            result = handler.lambda_handler(transformed["params"]["arguments"], context)
            response = {"jsonrpc": "2.0", "id": packet["id"],
                        "result": {"content": [{"type": "text", "text": json.dumps(result)}]}}
            event["mcp"]["gatewayResponse"] = {"statusCode": 200, "body": response}
            received = interceptor_module.handler(event, None)["mcp"]["transformedGatewayResponse"]["body"]
            return httpx.Response(200, json=received, request=httpx.Request("POST", endpoint))

    monkeypatch.setattr(facade.httpx, "AsyncClient", lambda **kwargs: Gateway())
    decision = facade.Decision.model_validate({
        "schema_version": "1.0", "allowed": True, "receipt_id": "receipt-one",
        "principal": {"user_id": "user-one", "root_human_id": "user-one",
                      "tenant_id": "tenant-one", "team_id": "team-one"},
    })
    response = asyncio.run(facade.AgentCoreClient().invoke("websearch___search", {
        "query": "example", "execution_id": EXECUTION_ID, "request_id": REQUEST_ID}, decision))
    assert response["result"]["receipt_id"] == "receipt-one"
    assert response["result"]["tenant_id"] == "tenant-one"
    assert response["result"]["user_id"] == "user-one"
    search = response["result"]["result"]["result"]
    assert search["results"][0]["url"] == "https://example.com"
    assert search["citations"] == ["https://example.com", "https://other.example.com"]
    assert search["results_truncated"] is False and len(search["results"]) == 2
    assert search["query_count"] == 1 and search["estimated_search_usd"] == 0.007
    assert search["pricing_source"] == "https://aws.amazon.com/bedrock/agentcore/pricing/"
    assert provider_calls[0]["params"]["arguments"] == {"query": "example"}
    assert response["result"]["_meta"]["adp_receipt_id"] == "receipt-one"
    assert len(provider_calls) == 1


@pytest.mark.parametrize("tool,arguments", [
    ("code-interpreter___start", {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID}),
    ("browser___start", {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID, "url": "https://example.com"}),
])
def test_unwired_stateful_provider_never_claims_success(gateway, monkeypatch, tool, arguments):
    context, search_calls = gateway
    context.client_context.custom["bedrockAgentCoreToolName"] = tool
    monkeypatch.setenv("ADP_CODE_INTERPRETER_ENABLED", "true")
    monkeypatch.setenv("ADP_TASK_BROWSER_HTTP_ENABLED", "true")
    assert handler.lambda_handler(gateway_event(tool=tool, arguments=arguments), context) == {
        "isError": True, "content": [{"type": "text", "text": "tool_refused"}],
    }
    assert search_calls == []


@pytest.mark.parametrize("tool,arguments", [
    ("browser___start", {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID, "url": "https://example.com"}),
    ("code-interpreter___start", {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID}),
])
def test_malformed_or_untrusted_stateful_gateway_event_is_refused(gateway, monkeypatch, tool, arguments):
    context, search_calls = gateway
    context.client_context.custom["bedrockAgentCoreToolName"] = tool
    monkeypatch.setenv("ADP_CODE_INTERPRETER_ENABLED", "true")
    monkeypatch.setenv("ADP_TASK_BROWSER_HTTP_ENABLED", "true")
    monkeypatch.setattr(handler, "dispatch_stateful", lambda *args, **kwargs: pytest.fail("Untrusted event dispatched"))
    event = gateway_event(tool=tool, arguments=arguments)
    for malformed in ({**event, "adp_assertion": "broken"},
                      {key: value for key, value in event.items() if key != "adp_assertion"},
                      {**event, "request_id": "different"}):
        assert handler.lambda_handler(malformed, context)["isError"] is True
    context.client_context.custom["bedrockAgentCoreToolName"] = "websearch___search"
    assert handler.lambda_handler(event, context)["isError"] is True
    assert search_calls == []


@pytest.mark.parametrize("route", ["/tools/websearch", "/tools/code-interpreter", "/tools/browser"])
def test_lambda_rejects_direct_rest_even_with_legacy_worker_role(monkeypatch, route):
    monkeypatch.setenv("ADP_TOOLS_WORKER_ROLES", ROLE)
    monkeypatch.setattr(handler, "dispatch_stateful", lambda *args, **kwargs: pytest.fail("REST entered gateway"))
    event = {"resource": route, "httpMethod": "POST", "body": "{}"}
    assert handler.lambda_handler(event, None)["statusCode"] == 404


def test_signed_interpreter_start_execute_and_result_use_original_provider_task(gateway, monkeypatch):
    from agentcore_tools.gateway_interpreter import GatewayInterpreter

    class Provider:
        def __init__(self):
            self.calls = []

        def start_code_interpreter_session(self, **kwargs):
            self.calls.append("start")
            return {"sessionId": "private-session"}

        def invoke_code_interpreter(self, **kwargs):
            self.calls.append(kwargs["name"])
            result = {"startCommandExecution": {"taskId": "private-task"},
                      "getTask": {"taskStatus": "completed", "stdout": "42\n", "stderr": "", "exitCode": 0}}
            return {"stream": iter([{"result": {"structuredContent": result[kwargs["name"]]}}])}

    provider = Provider()
    monkeypatch.setattr(GatewayInterpreter, "_provider", lambda self: provider)
    monkeypatch.setenv("ADP_CODE_INTERPRETER_ENABLED", "true")
    monkeypatch.setenv("ADP_CODE_INTERPRETER_ID", "CodeInterpreterExample-0123456789")
    context, search_calls = gateway
    context.client_context.custom["bedrockAgentCoreToolName"] = "code-interpreter___start"
    start_args = {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID}
    start = handler.lambda_handler(gateway_event(tool="code-interpreter___start", arguments=start_args), context)
    assert start["result"]["status"] == "confirmed"
    handle = start["result"]["session_id"]
    execute_id = "00000000-0000-4000-8000-000000000003"
    execute_args = {"execution_id": EXECUTION_ID, "request_id": execute_id,
                    "session_id": handle, "language": "python", "code": "print(42)"}
    context.client_context.custom["bedrockAgentCoreToolName"] = "code-interpreter___execute"
    event = gateway_event(tool="code-interpreter___execute", arguments=execute_args)
    submitted = handler.lambda_handler(event, context)
    # #8223: a task that finishes within the execute wait budget is answered in the execute call itself.
    assert submitted["result"]["status"] == "confirmed" and submitted["result"]["provider_id"] == "private-task"
    assert submitted["result"]["result"]["stdout"] == "42\n"
    assert handler.lambda_handler(event, context)["result"] == submitted["result"]
    monkeypatch.setenv("ADP_CODE_INTERPRETER_ENABLED", "false")
    context.client_context.custom["bedrockAgentCoreToolName"] = "code-interpreter___result"
    result_args = {"execution_id": EXECUTION_ID, "request_id": execute_id,
                   "session_id": handle, "provider_id": "private-task"}
    result = handler.lambda_handler(gateway_event(tool="code-interpreter___result", arguments=result_args), context)
    assert result["result"]["result"]["stdout"] == "42\n"
    assert result["receipt_id"] == "receipt-one" and result["tenant_id"] == "tenant-one"
    assert provider.calls == ["start", "startCommandExecution", "getTask"]
    assert search_calls == []


def test_signed_interpreter_file_artifact_and_confirmed_close(gateway, monkeypatch):
    from agentcore_tools.gateway_interpreter import GatewayInterpreter

    class Provider:
        def __init__(self):
            self.calls = []
            self.stopped = False

        def start_code_interpreter_session(self, **kwargs):
            self.calls.append("start")
            return {"sessionId": "private-session"}

        def invoke_code_interpreter(self, **kwargs):
            self.calls.append(kwargs["name"])
            assert "O_NOFOLLOW" in kwargs["arguments"]["command"]
            payload = {"encoding": "base64", "data": base64.b64encode(b"42\n").decode()}
            return {"stream": iter([{"result": {"structuredContent": {"stdout": json.dumps(payload)}}}])}

        def stop_code_interpreter_session(self, **kwargs):
            assert kwargs["sessionId"] == "private-session"
            self.calls.append("stop")
            self.stopped = True
            return {}

        def get_code_interpreter_session(self, **kwargs):
            return {"status": "TERMINATED" if self.stopped else "READY"}

    provider = Provider()
    monkeypatch.setattr(GatewayInterpreter, "_provider", lambda self: provider)
    monkeypatch.setenv("ADP_CODE_INTERPRETER_ENABLED", "true")
    monkeypatch.setenv("ADP_CODE_INTERPRETER_ID", "CodeInterpreterExample-0123456789")
    context, search_calls = gateway
    context.client_context.custom["bedrockAgentCoreToolName"] = "code-interpreter___start"
    start_args = {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID}
    started = handler.lambda_handler(gateway_event(tool="code-interpreter___start", arguments=start_args), context)
    handle = started["result"]["session_id"]
    file_id = "00000000-0000-4000-8000-000000000006"
    file_args = {"execution_id": EXECUTION_ID, "request_id": file_id,
                 "session_id": handle, "path": "/tmp/sample.txt"}
    context.client_context.custom["bedrockAgentCoreToolName"] = "code-interpreter___file"
    event = gateway_event(tool="code-interpreter___file", arguments=file_args)
    found = handler.lambda_handler(event, context)
    assert found["result"]["status"] == "confirmed"
    assert found["result"]["artifact"]["content_sha256"] == hashlib.sha256(b"42\n").hexdigest()
    assert handler.lambda_handler(event, context)["result"] == found["result"]
    context.client_context.custom["bedrockAgentCoreToolName"] = "code-interpreter___artifact_read"
    read_args = {"execution_id": EXECUTION_ID, "artifact_id": file_id, "offset": 0}
    artifact = handler.lambda_handler(gateway_event(tool="code-interpreter___artifact_read", arguments=read_args), context)
    assert base64.b64decode(artifact["result"]["data_base64"]) == b"42\n"
    assert artifact["result"]["sha256"] == found["result"]["artifact"]["content_sha256"]
    context.client_context.custom["bedrockAgentCoreToolName"] = "code-interpreter___execution_close"
    close_args = {"execution_id": EXECUTION_ID}
    closed = handler.lambda_handler(gateway_event(tool="code-interpreter___execution_close", arguments=close_args), context)
    assert closed["result"]["termination_confirmed"] is True
    assert handler.lambda_handler(gateway_event(tool="code-interpreter___execution_close", arguments=close_args), context) == closed
    assert provider.calls == ["start", "executeCommand", "stop"] and search_calls == []
    context.client_context.custom["bedrockAgentCoreToolName"] = "code-interpreter___operation_get"
    get_args = {"execution_id": EXECUTION_ID, "request_id": file_id}
    assert handler.lambda_handler(gateway_event(tool="code-interpreter___operation_get", arguments=get_args), context)["result"] == found["result"]
    context.client_context.custom["bedrockAgentCoreToolName"] = "code-interpreter___file"
    denied = gateway_event(tool="code-interpreter___file", arguments={**file_args, "path": "/etc/passwd"})
    assert handler.lambda_handler(denied, context)["isError"] is True
    assert provider.calls == ["start", "executeCommand", "stop"]


def test_browser_consumer_reverifies_signed_queue_payload(gateway):
    from agentcore_tools.browser_http import reauthorize_gateway

    arguments = {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID,
                 "url": "https://example.com"}
    event = gateway_event(tool="browser___start", arguments=arguments)
    envelope = {"operation": "start", "arguments": arguments, "assertion": event["adp_assertion"]}
    verified = reauthorize_gateway(envelope)
    assert verified["principal"]["user_id"] == "user-one"
    with pytest.raises(HTTPException) as changed:
        reauthorize_gateway({**envelope, "arguments": {**arguments, "url": "https://other.example"}})
    assert changed.value.status_code == 403
    with pytest.raises(HTTPException) as untrusted:
        reauthorize_gateway({**envelope, "assertion": "untrusted"})
    assert untrusted.value.status_code == 403


@pytest.mark.parametrize("tool", ["browser___start", "code-interpreter___start"])
def test_signed_start_deadline_cannot_be_extended_or_reused_after_expiry(gateway, tool):
    context, calls = gateway
    context.client_context.custom["bedrockAgentCoreToolName"] = tool
    arguments = {"execution_id": EXECUTION_ID, "request_id": REQUEST_ID}
    if tool == "browser___start":
        arguments["url"] = "https://example.com"
    event = gateway_event(tool=tool, arguments=arguments)
    claims, payload = mcp_adapter.verified_call(event, context, tool)
    assert claims["execution_deadline_at"] > int(time.time()) and payload == arguments
    envelope = json.loads(base64.urlsafe_b64decode(event["adp_assertion"] + "=="))
    for value, resign in ((claims["execution_deadline_at"] + 1, False),
                          (int(time.time()) - 1, True),
                          (claims["issued_at"] + 901, True), ("300", True)):
        forged_claims = {**claims, "execution_deadline_at": value}
        signature = (hmac.digest(SIGNATURE, encoded(forged_claims), "sha256") if resign else
                     base64.urlsafe_b64decode(envelope["signature"] + "=="))
        forged = {**event, "adp_assertion": token(encoded({"claims": forged_claims,
                "signature": token(signature)}))}
        with pytest.raises(HTTPException) as refused:
            mcp_adapter.verified_call(forged, context, tool)
        assert refused.value.status_code == 403
    assert calls == []
