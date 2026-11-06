"""A slow gateway result must not cause a second provider submission."""

import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def bridge(monkeypatch):
    cli = Path(__file__).resolve().parents[2] / "cli"
    monkeypatch.syspath_prepend(str(cli))
    spec = importlib.util.spec_from_file_location("adp_mcp_timeouts", cli / "adp-mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.NativeTransport, "configured", lambda: None)
    return module


@pytest.mark.parametrize("elapsed", [35, 125])
def test_wait_for_connector_refresh_and_call_without_resubmission(bridge, monkeypatch, elapsed):
    context = {"url": "https://gateway.example/api", "headers": {}, "proxy": False, "binding": {"mode": "bearer"}}
    body = {"tool": "atlassian___search_jira", "arguments": {"jql": "key = TEST-1"}, "request_id": "00000000-0000-4000-8000-000000000001"}
    receipt = {"status": "confirmed", "result_status": "success", "request_id": body["request_id"]}
    requests = []

    def open_request(request, *, timeout):
        requests.append(request)
        assert 0 < timeout <= 120
        # Model a gateway that has refreshed metadata and dispatched once, but
        # whose response arrives after the former 30-second client deadline.
        if elapsed > timeout:
            raise TimeoutError("gateway response deadline")
        return io.BytesIO(json.dumps(receipt).encode())

    monkeypatch.setattr(bridge, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    if elapsed == 35:
        assert bridge.gateway_request("/agent/tools/call", body, context=context) == receipt
    else:
        with pytest.raises(bridge.ToolError, match="Gateway tools unavailable"):
            bridge.gateway_request("/agent/tools/call", body, context=context)
    assert len(requests) == 1
    assert json.loads(requests[0].data) == body


def test_native_transport_keeps_its_original_deadline_and_unknown_result(bridge, monkeypatch):
    context = {"url": "https://gateway.example/api", "headers": {}, "proxy": False, "binding": {"mode": "native"}}
    completed = []
    attempt = SimpleNamespace(body=b"{}", headers={}, maximum=1024, complete=lambda *args: completed.append(args))
    native = SimpleNamespace(context=lambda: context, begin=lambda *args: attempt)
    monkeypatch.setattr(bridge.NativeTransport, "configured", lambda: native)
    waits = []

    def open_request(request, *, timeout):
        waits.append(timeout)
        raise TimeoutError("native response deadline")

    monkeypatch.setattr(bridge, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    with pytest.raises(bridge.ToolError, match="Original native operation is unresolved"):
        bridge.gateway_request("/agent/tools/call", {}, context=context)
    assert waits == [30] and completed == []


@pytest.mark.parametrize("path", ["/v1/l5/mcp/tools/call", "/agent/tools", "/agent/tools/operations/recover"])
def test_other_gateway_requests_keep_their_existing_deadline(bridge, monkeypatch, path):
    context = {"url": "https://gateway.example/api", "headers": {}, "proxy": False, "binding": {"mode": "bearer"}}
    waits = []

    def open_request(request, *, timeout):
        waits.append(timeout)
        raise TimeoutError("gateway response deadline")

    monkeypatch.setattr(bridge, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    with pytest.raises(bridge.ToolError, match="Gateway tools unavailable"):
        bridge.gateway_request(path, {}, context=context)
    assert waits == [30]
