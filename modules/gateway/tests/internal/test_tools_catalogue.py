"""Public OpenAPI inputs retain parameters alongside request-body fields."""

import pytest

from src.internal.tools_catalogue import openapi_tool_schema, public_tool_schema


def test_path_parameters_operation_overrides_and_body_share_public_schema():
    path = {
        "parameters": [
            {"in": "path", "name": "site", "required": True, "schema": {"type": "string", "pattern": "^[a-z]+$"}},
            {"in": "query", "name": "limit", "required": True, "schema": {"type": "integer", "maximum": 100}},
            {"in": "query", "name": "verbose", "schema": {"type": "boolean"}},
            {"in": "header", "name": "adp_assertion", "required": True, "schema": {"type": "string"}},
        ]
    }
    action = {
        "parameters": [
            {"in": "query", "name": "limit", "schema": {"type": "integer", "minimum": 1, "maximum": 10}},
            {
                "in": "query",
                "name": "tags",
                "required": True,
                "description": "Selected tags",
                "schema": {"type": "array", "items": {"type": "string"}},
            },
            {"in": "query", "name": "verbose", "required": True, "schema": {"type": "boolean"}},
            {"in": "query", "name": "request_id", "required": True, "schema": {"type": "string"}},
        ],
        "requestBody": {
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"text": {"type": "string", "minLength": 1}, "execution_id": {"type": "string"}},
                        "required": ["text", "execution_id"],
                    }
                }
            }
        },
    }
    schema = public_tool_schema(openapi_tool_schema(path, action))
    assert schema == {
        "type": "object",
        "properties": {
            "text": {"type": "string", "minLength": 1},
            "site": {"type": "string", "pattern": "^[a-z]+$"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            "verbose": {"type": "boolean"},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Selected tags"},
        },
        "required": ["text", "site", "verbose", "tags"],
    }
    assert path["parameters"][1]["required"] is True
    assert "description" not in action["parameters"][1]["schema"]
    assert action["requestBody"]["content"]["application/json"]["schema"]["required"] == ["text", "execution_id"]


def test_tool_catalogue_adds_first_party_platform_tools_and_distinguishes_unconfigured_from_unavailable(monkeypatch):
    """#8113: first-party read-only tools are catalogue entries governed like every other tool."""
    from fastapi import HTTPException

    from src.internal import tools_catalogue
    from src.internal.tools_platform import PLATFORM_TOOL_NAMES

    monkeypatch.delenv("ADP_TOOLS_GATEWAY_ROLE_ARN", raising=False)
    only_first_party = tools_catalogue.tool_catalogue()
    assert [tool["name"] for tool in only_first_party["tools"]] == sorted(PLATFORM_TOOL_NAMES)
    assert all(tool["first_party"] is True and tool["read_only"] is True and tool["credential_kinds"] == [] for tool in only_first_party["tools"])
    assert all(tool["inputSchema"]["type"] == "object" and tool["inputSchema"]["additionalProperties"] is False for tool in only_first_party["tools"])
    assert only_first_party["connectors"] == [{"name": "platform", "credential_kinds": [], "dynamic": False, "first_party": True}]

    monkeypatch.setenv("ADP_TOOLS_GATEWAY_ROLE_ARN", "arn:aws:iam::123456789012:role/tools")
    deployed = {
        "tools": [{"name": "browser.start", "connector": "browser"}],
        "connectors": [{"name": "browser", "credential_kinds": [], "dynamic": False}],
    }
    monkeypatch.setattr(tools_catalogue, "gateway_catalogue", lambda: deployed)
    merged = tools_catalogue.tool_catalogue()
    assert [tool["name"] for tool in merged["tools"]] == ["browser.start", *sorted(PLATFORM_TOOL_NAMES)]
    assert [connector["name"] for connector in merged["connectors"]] == ["browser", "platform"]

    def unavailable():
        raise HTTPException(503, "tool catalogue unavailable")

    monkeypatch.setattr(tools_catalogue, "gateway_catalogue", unavailable)
    with pytest.raises(HTTPException) as error:
        tools_catalogue.tool_catalogue()
    assert error.value.status_code == 503
    monkeypatch.setattr(
        tools_catalogue, "gateway_catalogue", lambda: {"tools": [{"name": "platform.other", "connector": "platform"}], "connectors": []}
    )
    with pytest.raises(HTTPException):
        tools_catalogue.tool_catalogue()


@pytest.mark.parametrize(
    "configuration",
    [
        {"mcpServer": {"endpoint": "https://private.example.invalid/mcp/"}},
        {"mcpServer": {"endpoint": "https://private.example.invalid/mcp/", "tools": [{"name": "search"}]}},
    ],
)
@pytest.mark.parametrize("with_core", [False, True])
def test_unverified_dynamic_target_is_explicit_without_disabling_core_tools(monkeypatch, configuration, with_core):
    """Control-plane READY/inline caller-shaped metadata is not MCP discovery."""
    from types import SimpleNamespace

    from src.internal import tools_catalogue

    role = "arn:aws:iam::123456789012:role/fixture-tools"
    monkeypatch.setenv("ADP_TOOLS_GATEWAY_ROLE_ARN", role)
    listing = {
        "list_gateways": [{"name": "adp-tools", "gatewayId": "fixture-gateway"}],
        "list_gateway_targets": [{"targetId": "fixture-target", "status": "READY"}],
    }
    targets = {"fixture-target": {"name": "knowledge", "targetConfiguration": {"mcp": configuration}}}
    if with_core:
        listing["list_gateway_targets"].append({"targetId": "fixture-core", "status": "READY"})
        targets["fixture-core"] = {
            "name": "websearch",
            "targetConfiguration": {
                "mcp": {
                    "lambda": {
                        "toolSchema": {
                            "inlinePayload": [
                                {
                                    "name": "search",
                                    "description": "Search fixture",
                                    "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
                                },
                            ]
                        }
                    }
                }
            },
        }
    control = SimpleNamespace(
        get_paginator=lambda action: SimpleNamespace(paginate=lambda **kwargs: [{"items": listing[action]}]),
        get_gateway=lambda **kwargs: {"roleArn": role},
        get_gateway_target=lambda **kwargs: targets[kwargs["targetId"]],
    )
    monkeypatch.setattr(tools_catalogue.boto3, "client", lambda name: control)
    result = tools_catalogue.gateway_catalogue()
    assert [tool["name"] for tool in result["tools"]] == (["websearch.search"] if with_core else [])
    knowledge = next(connector for connector in result["connectors"] if connector["name"] == "knowledge")
    assert knowledge["discovery_status"] == "unavailable"
    assert knowledge["discovery_reason"] == "dynamic_catalogue_provenance_unavailable"
    if with_core:
        assert result["tools"][0]["inputSchema"]["properties"] == {"query": {"type": "string"}}


def test_gateway_catalogue_is_served_from_a_short_process_cache(monkeypatch):
    """#8223 Dev Box tool latency: every tool authorization rebuilt the catalogue from the AgentCore
    control plane (~1 s on dev). Within the TTL the second read costs no control-plane call and
    returns an equal, independent copy; after the TTL the control plane is read again; a failed
    read is never cached."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from src.internal import tools_catalogue

    role = "arn:aws:iam::123456789012:role/fixture-tools"
    monkeypatch.setenv("ADP_TOOLS_GATEWAY_ROLE_ARN", role)
    calls = []
    listing = {
        "list_gateways": [{"name": "adp-tools", "gatewayId": "fixture-gateway"}],
        "list_gateway_targets": [{"targetId": "fixture-core", "status": "READY"}],
    }
    target = {
        "name": "websearch",
        "targetConfiguration": {
            "mcp": {"lambda": {"toolSchema": {"inlinePayload": [{"name": "search", "description": "d", "inputSchema": {"type": "object"}}]}}}
        },
    }

    def paginator(action):
        calls.append(action)
        return SimpleNamespace(paginate=lambda **kwargs: [{"items": listing[action]}])

    control = SimpleNamespace(
        get_paginator=paginator,
        get_gateway=lambda **kwargs: {"roleArn": role},
        get_gateway_target=lambda **kwargs: target,
    )
    monkeypatch.setattr(tools_catalogue.boto3, "client", lambda name: control)
    clock = {"now": 1000.0}
    monkeypatch.setattr(tools_catalogue.time, "monotonic", lambda: clock["now"])

    first = tools_catalogue.gateway_catalogue()
    assert [tool["name"] for tool in first["tools"]] == ["websearch.search"]
    assert calls == ["list_gateways", "list_gateway_targets"]
    second = tools_catalogue.gateway_catalogue()
    assert second == first and second is not first and calls == ["list_gateways", "list_gateway_targets"]
    second["tools"].clear()
    assert tools_catalogue.gateway_catalogue()["tools"], "a caller's mutation never reaches the cache"

    clock["now"] += tools_catalogue.CATALOGUE_TTL_SECONDS
    tools_catalogue.gateway_catalogue()
    assert calls == ["list_gateways", "list_gateway_targets"] * 2

    def broken(action):
        raise tools_catalogue.ClientError({"Error": {"Code": "Throttling", "Message": "slow down"}}, action)

    tools_catalogue.invalidate_catalogue_cache()
    monkeypatch.setattr(control, "get_paginator", broken)
    with pytest.raises(HTTPException):
        tools_catalogue.gateway_catalogue()
    monkeypatch.setattr(control, "get_paginator", paginator)
    assert tools_catalogue.gateway_catalogue()["tools"], "the failure was not cached"
