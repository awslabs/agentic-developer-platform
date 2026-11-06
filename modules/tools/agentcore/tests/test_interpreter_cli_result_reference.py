"""Real MCP request construction against the facade/provider correlation contract."""

import importlib.util
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tools_facade.server import create_app
from test_gateway_interpreter_facade import HEADERS, gateway  # noqa: F401


def load_shim(monkeypatch):
    cli = Path(__file__).resolve().parents[3] / "gateway" / "cli"
    monkeypatch.syspath_prepend(str(cli))
    spec = importlib.util.spec_from_file_location("interpreter_result_shim", cli / "adp-mcp.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def shim(gateway, monkeypatch, tmp_path):  # noqa: F811 - imported parametrized pytest fixture
    authority, provider = gateway
    module = load_shim(monkeypatch)
    monkeypatch.setenv("BG_CONFIG_DIR", str(tmp_path / "private"))
    monkeypatch.setattr(module, "gateway_context", lambda: {"binding": "offline-owner"})
    client = TestClient(create_app(gateway=authority, agentcore=provider))
    sent = []

    def request(path, body=None, *, context=None):
        sent.append((path, body))
        response = client.request("POST" if body is not None else "GET", path, json=body, headers=HEADERS)
        if response.status_code >= 400:
            raise module.GatewayHTTPError(response.status_code)
        return response.json()

    monkeypatch.setattr(module, "gateway_request", request)
    return module, provider, sent


def call(shim, tool, arguments):
    response = shim.handle({"method": "tools/call", "params": {"name": tool, "arguments": arguments}})
    return response, json.loads(response["content"][0]["text"])


def executed(shim):
    _, start = call(shim, "code_interpreter___start", {"ttl_seconds": 300})
    session = start["result"]["result"]["result"]["session_id"]
    binding = {"execution_ref": start["operation_ref"], "session_id": session}
    _, execute = call(shim, "code_interpreter___execute", {
        **binding, "language": "python", "code": "print(42)"})
    _, execute = call(shim, "adp___operation_status", {"operation_ref": execute["operation_ref"]})
    assert execute["status"] == "confirmed"
    task = execute["result"]["result"]["result"]["provider_id"]
    return binding, execute, task


def test_cli_result_uses_original_execute_request_without_replaying_code(shim):
    module, provider, sent = shim
    binding, execute, task = executed(module)
    original_record = module.load_operation(execute["operation_ref"])
    response, result = call(module, "code_interpreter___result", {**binding, "provider_id": task})
    assert not response["isError"], {"result": result, "original_execute": execute["request_id"], "result_request": sent[-1][1]["request_id"]}
    assert result["status"] == "confirmed"
    assert sent[-1][1]["request_id"] == execute["request_id"]
    assert result["operation_ref"] != execute["operation_ref"]
    assert module.load_operation(execute["operation_ref"]) == original_record
    assert provider.commands == ["print(42)"]
    _, recovered = call(module, "adp___operation_status", {"operation_ref": result["operation_ref"]})
    assert recovered["status"] == "confirmed" and recovered["operation_ref"] == result["operation_ref"]
    assert execute["request_id"] in sent[-1][0]
    artifact, _ = call(module, "adp___artifact_read", {"operation_ref": result["operation_ref"]})
    assert not artifact.get("isError") and execute["request_id"] in sent[-1][0]
    assert provider.commands == ["print(42)"]


@pytest.mark.parametrize("mismatch", ("binding", "execution", "session", "task", "pending", "missing", "ambiguous"))
def test_result_refuses_unmatched_or_ambiguous_execute_before_network(shim, mismatch):
    module, provider, sent = shim
    binding, execute, task = executed(module)
    original = module.load_operation(execute["operation_ref"])
    if mismatch == "binding":
        original["binding"] = "other-owner"
    elif mismatch == "execution":
        original["execution_id"] = str(uuid4())
    elif mismatch == "session":
        original["arguments"]["session_id"] = "other-session"
    elif mismatch == "task":
        original["provider_id"] = "other-task"
    elif mismatch == "pending":
        original["status"] = "unknown"
    elif mismatch == "missing":
        del original["provider_id"]
    else:
        original["request_id"] = str(uuid4())
    module.store_operation(original)
    before = len(sent)
    with pytest.raises(module.ToolError, match="unique confirmed execute"):
        call(module, "code_interpreter___result", {**binding, "provider_id": task})
    assert len(sent) == before
    assert provider.commands == ["print(42)"]


def test_result_recovers_after_lost_reply_and_shim_restart(shim, monkeypatch):
    module, provider, sent = shim
    binding, execute, task = executed(module)
    original_record = module.load_operation(execute["operation_ref"])
    request = module.gateway_request

    def lose_reply(path, body=None, *, context=None):
        request(path, body, context=context)
        raise module.ToolError("offline dropped result reply")

    monkeypatch.setattr(module, "gateway_request", lose_reply)
    response, result = call(module, "code_interpreter___result", {**binding, "provider_id": task})
    assert response["isError"] and result["status"] == "unknown"
    result_ref = result["operation_ref"]
    assert result_ref != execute["operation_ref"]
    restarted = load_shim(monkeypatch)
    monkeypatch.setattr(restarted, "gateway_context", lambda: {"binding": "offline-owner"})
    monkeypatch.setattr(restarted, "gateway_request", request)
    before = len(sent)
    for tool in ("adp___operation_status", "adp___operation_retry", "adp___artifact_read"):
        response, recovered = call(restarted, tool, {"operation_ref": result_ref})
        assert not response.get("isError") and recovered["operation_ref"] == result_ref
        assert execute["request_id"] in sent[-1][0] and sent[-1][1] is None
    assert len(sent) == before + 3
    assert restarted.load_operation(execute["operation_ref"]) == original_record
    assert provider.commands == ["print(42)"]


@pytest.mark.parametrize("mutation", ("wire_id", "original_binding", "original_task"))
def test_result_recovery_revalidates_original_execute_without_redirect(shim, mutation):
    module, provider, sent = shim
    binding, execute, task = executed(module)
    _, result = call(module, "code_interpreter___result", {**binding, "provider_id": task})
    if mutation == "wire_id":
        record = module.load_operation(result["operation_ref"])
        record["provider_request_id"] = str(uuid4())
    else:
        record = module.load_operation(execute["operation_ref"])
        record["binding" if mutation == "original_binding" else "provider_id"] = "other"
    module.store_operation(record)
    before = len(sent)
    for tool in ("adp___operation_status", "adp___operation_retry", "adp___artifact_read"):
        response, recovered = call(module, tool, {"operation_ref": result["operation_ref"]})
        assert response["isError"] and recovered["status"] == "unknown"
    assert len(sent) == before
    assert provider.commands == ["print(42)"]
