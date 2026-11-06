import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_recovery import CALL, Gateway, Provider

from tools_facade.server import create_app


@pytest.fixture
def shim_client(tmp_path, monkeypatch):
    cli = Path(__file__).resolve().parents[3] / "gateway/cli"
    monkeypatch.syspath_prepend(str(cli))
    spec = importlib.util.spec_from_file_location("adp_mcp", cli / "adp-mcp.py")
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)
    monkeypatch.setattr(shim, "uuid4", lambda: shim.UUID(CALL["request_id"]))
    monkeypatch.setenv("BG_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ADP_DEPLOYMENT_ID", "test-deployment")
    monkeypatch.delenv("SIGV4_PROXY_TARGET", raising=False)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"gateway_url": "https://gateway.example.invalid/api"}))
    identity = {"sub": "person-one", "iss": "https://login.example.invalid", "org_id": "tenant-one", "iat": 1}

    def token():
        payload = shim.base64.urlsafe_b64encode(json.dumps(identity).encode()).decode().rstrip("=")
        return f"test.{payload}.signature"

    monkeypatch.setattr(shim.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=token()))

    class TokenGateway(Gateway):
        async def recover(self, tool, headers):
            from adp_common import _jwt_claims

            claims = _jwt_claims(headers["authorization"].removeprefix("Bearer "))
            self.owner = claims["sub"]
            self.tenant = claims["org_id"]
            return await super().recover(tool, {"authorization": "Bearer original"})

    gateway, provider = TokenGateway(), Provider()
    client = TestClient(create_app(gateway=gateway, agentcore=provider))
    transport = SimpleNamespace(drop=True, requests=[], saved=[])

    def open_request(request, timeout):
        transport.requests.append(request)
        l5_discovery = request.full_url.endswith("/v1/l5/mcp/tools/list")
        if l5_discovery:
            assert request.get_method() == "POST" and json.loads(request.data) == {}
        elif request.get_method() == "POST":
            transport.saved.append(shim.load_operation(CALL["request_id"]))
        response = client.request(
            request.get_method(), request.full_url, content=request.data, headers=dict(request.header_items()),
        )
        if l5_discovery:
            assert response.status_code == 404, response.text
            raise shim.HTTPError(request.full_url, response.status_code, "Not Found", response.headers, None)
        assert response.status_code == 200, response.text
        if transport.drop:
            transport.drop = False
            raise OSError("response lost after provider completed")
        return io.BytesIO(response.content)

    monkeypatch.setattr(shim, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    return shim, identity, provider, transport, config


def call(shim, retry=False):
    params = {"name": CALL["tool"], "arguments": CALL["arguments"]}
    if retry:
        params = {"name": "adp___operation_retry", "arguments": {"request_id": CALL["request_id"]}}
    result = shim.handle({"method": "tools/call", "params": params})
    return result, json.loads(result["content"][0]["text"])


def test_stateful_call_resolves_owned_start_operation_without_forwarding_reference(shim_client, monkeypatch):
    shim, identity, _, _, _ = shim_client
    sent = []
    start_id = "00000000-0000-4000-8000-000000000041"
    execution_id = "00000000-0000-4000-8000-000000000042"
    request_ids = iter((start_id, "00000000-0000-4000-8000-000000000043"))
    monkeypatch.setattr(shim, "uuid4", lambda: shim.UUID(next(request_ids)))

    def gateway_request(path, body=None, *, context=None):
        sent.append(body)
        return {"status": "confirmed", "result_status": "success", "request_id": body["request_id"],
                "execution_id": execution_id, "result": {"session_id": "session-owned"}}

    monkeypatch.setattr(shim, "gateway_request", gateway_request)
    start = shim.handle({"method": "tools/call", "params": {"name": "code_interpreter___start",
        "arguments": {}}})
    assert start["isError"] is False
    assert json.loads(start["content"][0]["text"])["operation_ref"] == start_id
    with pytest.raises(shim.ToolError, match="Session handle"):
        shim.handle({"method": "tools/call", "params": {"name": "code_interpreter___execute",
            "arguments": {"execution_ref": start_id, "session_id": "foreign-session", "code": "print(42)",
                          "language": "python"}}})
    with pytest.raises(shim.ToolError, match="not a completed start"):
        shim.handle({"method": "tools/call", "params": {"name": "browser___step",
            "arguments": {"execution_ref": start_id, "session_id": "session-owned",
                          "view_id": "view-one", "action": "click"}}})
    assert len(sent) == 1
    execute = shim.handle({"method": "tools/call", "params": {"name": "code_interpreter___execute",
        "arguments": {"execution_ref": start_id, "session_id": "session-owned", "code": "print(42)",
                      "language": "python"}}})
    assert execute["isError"] is False
    assert sent[1]["execution_id"] == execution_id
    assert sent[1]["arguments"] == {"session_id": "session-owned", "code": "print(42)", "language": "python"}
    identity["sub"] = "person-two"
    with pytest.raises(shim.ToolError, match="different login"):
        shim.handle({"method": "tools/call", "params": {"name": "code_interpreter___execute",
            "arguments": {"execution_ref": start_id, "session_id": "session-owned", "code": "print(42)",
                          "language": "python"}}})
    assert len(sent) == 2


@pytest.mark.parametrize("params", [
    {"name": CALL["tool"], "arguments": CALL["arguments"], "request_id": CALL["request_id"]},
    {"name": CALL["tool"], "arguments": CALL["arguments"], "execution_id": CALL["request_id"]},
    {"name": CALL["tool"], "arguments": {"query": "example", "adp_assertion": "spoofed"}},
    {"name": CALL["tool"], "arguments": {"query": "example", "tenant_id": "other"}},
    {"name": CALL["tool"], "arguments": {"query": "example", "request_id": CALL["request_id"]}},
    {"name": CALL["tool"], "arguments": {"query": "example", "execution_id": CALL["request_id"]}},
    {"name": "adp___operation_retry", "arguments": {"request_id": CALL["request_id"],
        "execution_id": CALL["request_id"]}},
    {"name": "code_interpreter___execute", "arguments": {"execution_ref": CALL["request_id"],
        "session_id": "foreign-session", "code": "print(42)", "language": "python"}},
])
def test_injected_authority_and_unknown_references_never_dispatch(shim_client, params):
    shim, _, provider, transport, _ = shim_client
    with pytest.raises((shim.ToolError, ValueError)):
        shim.handle({"method": "tools/call", "params": params})
    assert provider.calls == 0 and transport.requests == []


@pytest.mark.parametrize("params", [
    {"name": CALL["tool"], "arguments": []},
    {"name": CALL["tool"], "arguments": {}},
    {"name": CALL["tool"], "arguments": {"query": 42}},
    {"name": CALL["tool"], "arguments": {"query": "example", "extra": "unexpected"}},
    {"name": "browser___step", "arguments": {"session_id": "handle", "view_id": "view", "action": "click"}},
    {"name": "browser___step", "arguments": {"execution_ref": CALL["request_id"],
        "session_id": "handle", "view_id": "view", "action": "click", "seconds": True}},
])
def test_malformed_client_arguments_never_dispatch(shim_client, params):
    shim, _, provider, transport, _ = shim_client
    with pytest.raises((shim.ToolError, ValueError)):
        shim.handle({"method": "tools/call", "params": params})
    assert provider.calls == 0 and transport.requests == []


def test_known_422_is_rejected_without_retrying(shim_client, monkeypatch):
    shim, _, provider, transport, _ = shim_client

    def reject(*args, **kwargs):
        raise shim.GatewayHTTPError(422)

    monkeypatch.setattr(shim, "gateway_request", reject)
    first = shim.handle({"method": "tools/call", "params": {"name": CALL["tool"], "arguments": CALL["arguments"]}})
    receipt = json.loads(first["content"][0]["text"])
    assert receipt["status"] == "rejected" and receipt["operation_ref"] == CALL["request_id"]
    retried = shim.handle({"method": "tools/call", "params": {"name": "adp___operation_retry",
        "arguments": {"request_id": CALL["request_id"]}}})
    assert json.loads(retried["content"][0]["text"])["status"] == "rejected"
    assert provider.calls == 0 and transport.requests == []


def test_local_tools_advertise_versioned_operation_reference(shim_client, monkeypatch):
    shim, _, _, _, _ = shim_client

    def gateway_request(path, body=None, *, context=None):
        if path == "/v1/l5/mcp/tools/list":
            assert body == {}
            return {"tools": []}
        assert path == "/agent/tools" and body is None
        return {"tools": [
            {"name": "websearch___search", "description": "Search", "inputSchema": {"type": "object",
              "properties": {"query": {"type": "string"}}, "required": ["query"],
              "additionalProperties": False, "x-adp-schema-version": "1.0"}}]}

    monkeypatch.setattr(shim, "gateway_request", gateway_request)
    listing = shim.handle({"method": "tools/list"})["tools"]
    assert listing[0]["inputSchema"]["required"] == ["query"]
    for tool in listing:
        schema = tool["inputSchema"]
        assert schema["x-adp-schema-version"] == "1.0" and schema["additionalProperties"] is False
        if tool["name"] == "adp___operations_list":
            assert schema.get("required", []) == []
        elif tool["name"].startswith("adp___"):
            assert schema["required"] == ["operation_ref"]
            assert "request_id" not in schema["properties"]


@pytest.mark.parametrize("legacy_schema", [False, True])
def test_discovery_omits_provider_recovery_but_keeps_public_tools_and_local_helpers(
    shim_client, monkeypatch, legacy_schema,
):
    shim, _, provider, transport, _ = shim_client
    public_operations = {
        "websearch": ("search",),
        "code_interpreter": ("start", "execute", "result", "file", "close"),
        "browser": ("start", "step", "inspect", "close"),
    }
    public_names = {f"{family}___{operation}" for family, operations in public_operations.items()
                    for operation in operations}
    properties = ({name: {"type": "string"} for name in ("execution_id", "request_id", "adp_assertion")}
                  if legacy_schema else {})
    provider_schema = {"type": "object", "properties": properties, "required": list(properties)}
    catalogue = [
        {"name": f"{family}___{operation}", "description": operation, "inputSchema": provider_schema}
        for family, operations in public_operations.items()
        for operation in (*operations, "operation_get", "artifact_read", "execution_close")
    ]
    assert len(catalogue) == 19
    # Matching action names on other connectors must keep their own schemas.
    connector_tools = [{"name": name, "description": "Connector operation", "inputSchema": {
        "type": "object", "properties": {"handle": {"type": "string", "minLength": 1}},
        "required": ["handle"], "additionalProperties": False,
    }} for name in ("github___operation_get", "knowledge___artifact_read", "vendor___execution_close")]

    async def list_tools(self, headers):
        return {**await self.recover(CALL["tool"], headers), "tools": catalogue + connector_tools}

    monkeypatch.setattr(Gateway, "list_tools", list_tools, raising=False)
    transport.drop = False
    listing = shim.handle({"method": "tools/list"})["tools"]
    local_names = {"adp___operations_list", "adp___operation_retry", "adp___operation_status",
                   "adp___artifact_read", "adp___execution_close"}
    assert {tool["name"] for tool in listing} == public_names | local_names | {
        tool["name"] for tool in connector_tools
    }
    assert len(listing) == 18
    for tool in listing:
        if tool["name"] in public_names:
            assert not {"execution_id", "request_id", "adp_assertion"} & set(tool["inputSchema"]["properties"])
        if tool["name"] in {"browser___start", "code_interpreter___start"}:
            assert tool["inputSchema"]["properties"]["ttl_seconds"] == {
                "type": "integer", "minimum": 1, "maximum": 900}
    for tool in connector_tools:
        assert tool in listing
    assert provider.calls == 0
    assert [(request.get_method(), request.full_url) for request in transport.requests] == [
        ("POST", "https://gateway.example.invalid/api/v1/l5/mcp/tools/list"),
        ("GET", "https://gateway.example.invalid/api/agent/tools"),
    ]
    assert transport.saved == [] and shim.recent_operations() == []


def test_advertised_operation_reference_recovers_without_manual_id_translation(shim_client):
    shim, _, provider, transport, _ = shim_client
    first, receipt = call(shim)
    assert first["isError"] and receipt["status"] == "unknown"
    reference = receipt["operation_ref"]
    listed = shim.handle({"method": "tools/call", "params": {"name": "adp___operations_list", "arguments": {}}})
    assert json.loads(listed["content"][0]["text"])["operations"][0]["operation_ref"] == reference
    recovered = shim.handle({"method": "tools/call", "params": {"name": "adp___operation_retry",
        "arguments": {"operation_ref": reference, "tool": CALL["tool"], "arguments": CALL["arguments"]}}})
    assert json.loads(recovered["content"][0]["text"])["status"] == "confirmed"
    assert provider.calls == 1 and len(transport.requests) == 2


@pytest.mark.parametrize("changed", ["sub", "org_id", "iss", "gateway", "deployment"])
@pytest.mark.parametrize("retry", [True, False])
def test_lost_response_cannot_replay_under_another_binding(shim_client, monkeypatch, changed, retry):
    shim, identity, provider, transport, config = shim_client
    first, receipt = call(shim)
    assert first["isError"] and receipt["status"] == "unknown"
    assert provider.calls == 1
    if changed == "gateway":
        config.write_text(json.dumps({"gateway_url": "https://other.example.invalid/api"}))
    elif changed == "deployment":
        monkeypatch.setenv("ADP_DEPLOYMENT_ID", "other-deployment")
    else:
        identity[changed] += "-other"
    result, receipt = call(shim, retry=retry)
    assert result["isError"] and receipt["status"] == "rejected"
    assert receipt["operation_ref"] == CALL["request_id"]
    assert provider.calls == 1
    assert len(transport.requests) == 1
    assert shim.load_operation(CALL["request_id"])["binding"] == transport.saved[0]["binding"]
    assert shim.load_operation(CALL["request_id"])["status"] == "unknown"


def test_same_owner_refresh_recovers_original_after_lost_response(shim_client):
    shim, identity, provider, transport, _ = shim_client
    assert call(shim)[1]["status"] == "unknown"
    identity["iat"] = 2
    result, receipt = call(shim, retry=True)
    assert not result["isError"] and receipt["status"] == "confirmed"
    assert provider.calls == 1
    assert len(transport.requests) == 2
    assert transport.requests[0].get_header("Authorization") != transport.requests[1].get_header("Authorization")
    assert transport.saved[0]["binding"] == transport.saved[1]["binding"]
    assert "signature" not in json.dumps(transport.saved)
    result, repeated = call(shim)
    assert not result["isError"] and repeated == receipt
    assert transport.requests[-1].get_method() == "GET"
    assert provider.calls == 1


def test_reconnect_uses_durable_original_id_and_payload(shim_client, monkeypatch):
    shim, identity, provider, transport, _ = shim_client
    first, receipt = call(shim)
    assert first["isError"] and receipt["status"] == "unknown"
    assert provider.calls == 1
    cli = Path(__file__).resolve().parents[3] / "gateway/cli/adp-mcp.py"
    spec = importlib.util.spec_from_file_location("adp_mcp_reconnected", cli)
    reconnected = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reconnected)
    monkeypatch.setattr(reconnected, "build_opener", shim.build_opener)
    identity["iat"] = 2
    listed = reconnected.handle({"method": "tools/call", "params": {"name": "adp___operations_list", "arguments": {}}})
    operations = json.loads(listed["content"][0]["text"])["operations"]
    assert operations == [{"operation_ref": CALL["request_id"], "request_id": CALL["request_id"], "tool": CALL["tool"],
                           "execution_id": None, "status": "unknown"}]
    record = reconnected.load_operation(CALL["request_id"])
    assert record["arguments"] == CALL["arguments"] and record["client_arguments"] == CALL["arguments"]
    retry = reconnected.handle({"method": "tools/call", "params": {"name": "adp___operation_retry",
        "arguments": {"request_id": CALL["request_id"], "tool": CALL["tool"], "arguments": CALL["arguments"]}}})
    outcome = json.loads(retry["content"][0]["text"])
    assert outcome["status"] == "confirmed" and outcome["operation_ref"] == CALL["request_id"]
    assert provider.calls == 1 and len(transport.requests) == 2
    assert json.loads(transport.requests[0].data) == json.loads(transport.requests[1].data)


def test_confirmed_receipt_cannot_be_read_under_different_login(shim_client):
    shim, identity, provider, transport, _ = shim_client
    transport.drop = False
    assert call(shim)[1]["status"] == "confirmed"
    identity["sub"] = "another-owner"
    refused, receipt = call(shim, retry=True)
    assert refused["isError"] and receipt["status"] == "rejected"
    assert provider.calls == 1 and len(transport.requests) == 1


@pytest.mark.parametrize("changed", ["query", "tool"])
def test_changed_retry_payload_conflicts_without_dispatch(shim_client, changed):
    shim, _, provider, transport, _ = shim_client
    assert call(shim)[1]["status"] == "unknown"
    retry_arguments = {"request_id": CALL["request_id"], "tool": CALL["tool"],
                       "arguments": dict(CALL["arguments"])}
    if changed == "tool":
        retry_arguments["tool"] = "browser___start"
    else:
        retry_arguments["arguments"]["query"] = "different request"
    rejected = shim.handle({"method": "tools/call", "params": {"name": "adp___operation_retry",
        "arguments": retry_arguments}})
    assert json.loads(rejected["content"][0]["text"])["status"] == "rejected"
    assert shim.load_operation(CALL["request_id"])["status"] == "unknown"
    assert provider.calls == 1 and len(transport.requests) == 1


def test_five_helpers_recover_original_lost_reply_and_keep_artifact_and_close(shim_client):
    import base64
    import hashlib

    shim, _, provider, transport, _ = shim_client
    first, missing = call(shim)
    reference = missing["operation_ref"]
    assert first["isError"] and missing["status"] == "unknown" and provider.calls == 1
    listed = shim.handle({"method": "tools/call", "params": {
        "name": "adp___operations_list", "arguments": {}}})
    operations = json.loads(listed["content"][0]["text"])["operations"]
    assert len(operations) == 1 and operations[0]["operation_ref"] == reference
    before = len(transport.requests)
    pending = shim.handle({"method": "tools/call", "params": {
        "name": "adp___operation_status", "arguments": {"operation_ref": reference}}})
    assert json.loads(pending["content"][0]["text"])["status"] == "unknown"
    assert len(transport.requests) == before

    retried = shim.handle({"method": "tools/call", "params": {
        "name": "adp___operation_retry", "arguments": {"operation_ref": reference,
            "tool": CALL["tool"], "arguments": CALL["arguments"]}}})
    assert not retried["isError"] and provider.calls == 1
    assert json.loads(transport.requests[0].data) == json.loads(transport.requests[1].data)
    status = shim.handle({"method": "tools/call", "params": {
        "name": "adp___operation_status", "arguments": {"operation_ref": reference}}})
    receipt = json.loads(status["content"][0]["text"])
    assert receipt["status"] == "confirmed" and receipt["operation_ref"] == reference
    data = bytearray()
    total = None
    while total is None or len(data) < total:
        artifact = shim.handle({"method": "tools/call", "params": {
            "name": "adp___artifact_read", "arguments": {"operation_ref": reference,
                "offset": len(data), "length": 8192}}})
        packet = json.loads(artifact["content"][0]["text"])
        assert packet["operation_ref"] == reference and packet["offset"] == len(data)
        total = packet["total_length"]
        data.extend(base64.b64decode(packet["data_base64"], validate=True))
    assert len(data) == total and hashlib.sha256(data).hexdigest() == packet["sha256"]
    closed = shim.handle({"method": "tools/call", "params": {
        "name": "adp___execution_close", "arguments": {"operation_ref": reference}}})
    closed_receipt = json.loads(closed["content"][0]["text"])
    assert not closed["isError"] and closed_receipt["cleanup_status"] == "confirmed"
    assert provider.calls == 1 and shim.load_operation(reference)["request_id"] == reference
    after = shim.handle({"method": "tools/call", "params": {
        "name": "adp___operation_status", "arguments": {"operation_ref": reference}}})
    assert json.loads(after["content"][0]["text"])["result"] == receipt["result"]


def test_recovery_helpers_refuse_foreign_binding_before_artifact_or_close(shim_client):
    shim, identity, provider, transport, _ = shim_client
    call(shim)
    reference = CALL["request_id"]
    identity["sub"] = "another-owner"
    listed = shim.handle({"method": "tools/call", "params": {
        "name": "adp___operations_list", "arguments": {}}})
    assert json.loads(listed["content"][0]["text"])["operations"] == []
    for tool in ("adp___operation_retry", "adp___operation_status",
                 "adp___artifact_read", "adp___execution_close"):
        refused = shim.handle({"method": "tools/call", "params": {
            "name": tool, "arguments": {"operation_ref": reference}}})
        assert refused["isError"]
        assert json.loads(refused["content"][0]["text"])["status"] == "rejected"
    assert provider.calls == 1 and len(transport.requests) == 1


@pytest.mark.parametrize("tool,arguments", [
    ("browser___start", {"url": "https://example.org", "ttl_seconds": 300}),
    ("code_interpreter___start", {"ttl_seconds": 300}),
])
def test_start_lifetime_survives_lost_reply_and_original_id_retry(shim_client, monkeypatch, tool, arguments):
    shim, _, _, _, _ = shim_client
    original_id = CALL["request_id"]
    monkeypatch.setattr(shim, "uuid4", lambda: shim.UUID(original_id))
    posted = []

    def gateway_request(path, body=None, *, context=None):
        assert path == ("/agent/tools/call" if not posted else "/agent/tools/operations/recover")
        posted.append(body)
        if len(posted) == 1:
            raise shim.ToolError("response lost after gateway accepted original operation")
        return {"status": "confirmed", "result_status": "success", "execution_id": CALL["request_id"],
                "request_id": original_id, "result": {"session_id": "original-session"}}

    monkeypatch.setattr(shim, "gateway_request", gateway_request)
    first = shim.handle({"method": "tools/call", "params": {"name": tool, "arguments": arguments}})
    assert first["isError"] and len(posted) == 1
    saved = shim.load_operation(original_id)
    assert saved["client_arguments"] == arguments and saved["ttl_seconds"] == 300
    assert "ttl_seconds" not in saved["arguments"]
    changed = shim.handle({"method": "tools/call", "params": {"name": "adp___operation_retry",
        "arguments": {"operation_ref": original_id, "tool": tool,
                      "arguments": {**arguments, "ttl_seconds": 301}}}})
    assert changed["isError"] and len(posted) == 1
    assert json.loads(changed["content"][0]["text"])["status"] == "rejected"
    repeated = shim.handle({"method": "tools/call", "params": {"name": tool, "arguments": arguments}})
    assert not repeated["isError"] and posted[0] == posted[1]
    assert posted[0]["ttl_seconds"] == 300 and posted[0]["request_id"] == original_id
    assert posted[0]["arguments"] == {key: value for key, value in arguments.items() if key != "ttl_seconds"}
    assert shim.load_operation(original_id)["session_id"] == "original-session"


@pytest.mark.parametrize("lifetime", [0, 901, True, "300", 1.5])
def test_cli_rejects_unsupported_start_lifetime_before_saving(shim_client, lifetime):
    shim, _, provider, transport, _ = shim_client
    with pytest.raises(shim.ToolError):
        shim.handle({"method": "tools/call", "params": {"name": "browser___start",
            "arguments": {"url": "https://example.org", "ttl_seconds": lifetime}}})
    assert provider.calls == 0 and transport.requests == []
    assert shim.recent_operations() == []
