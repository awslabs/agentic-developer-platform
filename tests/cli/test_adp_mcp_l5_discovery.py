"""Overlapping facade and direct L5 discovery must fit one MCP catalogue."""

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def bridge(monkeypatch):
    cli = ROOT / "modules/gateway/cli"
    monkeypatch.syspath_prepend(str(cli))
    spec = importlib.util.spec_from_file_location("adp_mcp_l5_discovery", cli / "adp-mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.native_handoff, "ready", lambda: {"result": {"tools": []}})
    return module


def test_overlapping_l5_catalogues_are_advertised_once(bridge, monkeypatch):
    # This size fits the advertised limit once, but fails if both transport
    # catalogues are appended. Keep the direct schema used by L5 execution.
    direct = {"name": "l5___get_run", "description": "Read an assessment",
              "inputSchema": {"type": "object", "description": "x" * 270_000,
                              "properties": {"assessment_ref": {"type": "string"}}}}
    facade = {**direct, "description": "Facade view of the same tool"}
    search = {"name": "websearch___search", "description": "Search",
              "inputSchema": {"type": "object"}}

    def request(path, body=None):
        return {"tools": [direct] if path == "/v1/l5/mcp/tools/list" else [search, facade]}

    monkeypatch.setattr(bridge, "gateway_request", request)
    listing = bridge.handle({"method": "tools/list"})["tools"]
    names = [tool["name"] for tool in listing]
    assert names.count("l5___get_run") == 1
    assert next(tool for tool in listing if tool["name"] == "l5___get_run") == direct
    assert search in listing
    assert all(tool in listing for tool in bridge.LOCAL_TOOLS)
    assert len(json.dumps(listing).encode()) < bridge.MAX_EAGER_MCP_SCHEMA_BYTES + 384 * 1024


def test_facade_catalogue_survives_absent_direct_l5_endpoint(bridge, monkeypatch):
    tool = {"name": "l5___get_run", "description": "Read an assessment",
            "inputSchema": {"type": "object"}}

    def request(path, body=None):
        if path == "/v1/l5/mcp/tools/list":
            raise bridge.GatewayHTTPError(404)
        return {"tools": [tool]}

    monkeypatch.setattr(bridge, "gateway_request", request)
    assert bridge.handle({"method": "tools/list"})["tools"] == [tool] + bridge.LOCAL_TOOLS
