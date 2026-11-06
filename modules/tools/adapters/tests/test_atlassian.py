"""Controlled provider fixtures: public read names and independent returned IDs."""

import asyncio
import hashlib
import time

import pytest

from tool_adapters.atlassian import READ_TOOLS, WRITE_TOOLS, discover_reads, discover_writes, read_tool, write_tool
from tool_adapters.atlassian_observe import canonical

SCHEMA = {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}
PINS = {alias: hashlib.sha256(canonical(SCHEMA)).hexdigest() for alias in READ_TOOLS}
FIXTURES = {
    "search_jira": "JIRA-SEARCH-17",
    "get_jira_issue": "JIRA-ISSUE-42",
    "search_confluence": "CONF-SEARCH-31",
    "get_confluence_page": "CONF-PAGE-9",
}


class FixtureTransport:
    def __init__(self, *, deadline):
        assert deadline > time.monotonic()
        self.calls = []

    def send(self, method, params, token):
        assert token == "fixture-transient-token"
        self.calls.append((method, params))
        if method == "initialize":
            return {"result": {"protocolVersion": "2025-03-26"}}
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            if not params:
                return {
                    "result": {
                        "tools": [
                            {"name": READ_TOOLS[alias][0], "inputSchema": SCHEMA}
                            for alias in ("search_jira", "get_jira_issue")
                        ],
                        "nextCursor": "page-2",
                    }
                }
            assert params == {"cursor": "page-2"}
            return {
                "result": {
                    "tools": [
                        {"name": READ_TOOLS[alias][0], "inputSchema": SCHEMA}
                        for alias in ("search_confluence", "get_confluence_page")
                    ]
                    + [{"name": "createJiraIssue", "inputSchema": SCHEMA}]
                }
            }
        assert method == "tools/call"
        assert params["arguments"] == {"id": "fixture-id"}
        alias = next(alias for alias, (upstream, _) in READ_TOOLS.items() if upstream == params["name"])
        return {"result": {"structuredContent": {"id": FIXTURES[alias]}}}


def test_named_reads():
    deadline = time.monotonic() + 20
    tools = discover_reads("fixture-transient-token", PINS, deadline=deadline, transport_factory=FixtureTransport)
    assert {tool["name"] for tool in tools} == set(FIXTURES)
    assert all(tool["inputSchema"] == SCHEMA for tool in tools)
    for alias, expected in FIXTURES.items():
        result = read_tool(
            "fixture-transient-token",
            alias,
            {"id": "fixture-id"},
            PINS,
            deadline=deadline,
            transport_factory=FixtureTransport,
        )
        assert result["structuredContent"]["id"] == expected


def test_missing_or_changed_schema_does_not_publish_partial_catalogue():
    deadline = time.monotonic() + 20
    with pytest.raises(ValueError, match="unverified provider schema"):
        discover_reads("fixture-transient-token", {}, deadline=deadline, transport_factory=FixtureTransport)
    with pytest.raises(ValueError, match="unverified provider schema"):
        discover_reads(
            "fixture-transient-token",
            {**PINS, "get_jira_issue": "0" * 64},
            deadline=deadline,
            transport_factory=FixtureTransport,
        )
    with pytest.raises(ValueError, match="unsupported read operation"):
        read_tool(
            "fixture-transient-token",
            "create_jira_issue",
            {},
            PINS,
            deadline=deadline,
            transport_factory=FixtureTransport,
        )


def test_pinned_jira_writes_single_send_and_reject_unsafe_arguments():
    schema = {
        "type": "object",
        "properties": {"cloudId": {"type": "string"}, "fields": {"type": "object"}},
        "required": ["cloudId"],
    }
    pins = {alias: hashlib.sha256(canonical(schema)).hexdigest() for alias in WRITE_TOOLS}

    class WriteTransport(FixtureTransport):
        effects = []

        def send(self, method, params, token):
            if method == "tools/list":
                return {
                    "result": {
                        "tools": [{"name": upstream, "inputSchema": schema} for upstream, _ in WRITE_TOOLS.values()]
                    }
                }
            if method == "tools/call":
                self.effects.append(params)
                return {"result": {"structuredContent": {"id": "JIRA-ISSUE-42"}}}
            return super().send(method, params, token)

    deadline = time.monotonic() + 20
    assert {
        tool["name"]
        for tool in discover_writes(
            "fixture-transient-token", pins, deadline=deadline, transport_factory=WriteTransport
        )
    } == set(WRITE_TOOLS)
    for alias, (upstream, _) in WRITE_TOOLS.items():
        arguments = {"cloudId": "site-one"}
        result = write_tool(
            "fixture-transient-token", alias, arguments, pins, deadline=deadline, transport_factory=WriteTransport
        )
        assert result["structuredContent"]["id"] == "JIRA-ISSUE-42"
        assert WriteTransport.effects[-1] == {"name": upstream, "arguments": arguments}
    assert len(WriteTransport.effects) == 4
    with pytest.raises(ValueError, match="unverified provider schema"):
        discover_writes(
            "fixture-transient-token",
            {**pins, "create_jira_issue": "0" * 64},
            deadline=deadline,
            transport_factory=WriteTransport,
        )
    for alias, arguments in (
        ("comment_jira_issue", {"cloudId": "site-one", "fields": {"commentId": "existing"}}),
        ("transition_jira_issue", {"cloudId": "site-one", "fields": {"sprint": "different"}}),
    ):
        with pytest.raises(ValueError):
            write_tool(
                "fixture-transient-token", alias, arguments, pins, deadline=deadline, transport_factory=WriteTransport
            )
    assert len(WriteTransport.effects) == 4


def test_approved_jira_writes_use_independent_controlled_readback():
    schema = {
        "type": "object",
        "properties": {
            "cloudId": {"type": "string"},
            "issueKey": {"type": "string"},
            "fields": {"type": "object"},
            "comment": {"type": "string"},
            "status": {"type": "string"},
        },
        "required": ["cloudId"],
    }
    pins = {alias: hashlib.sha256(canonical(schema)).hexdigest() for alias in WRITE_TOOLS}
    provider = {"issues": {}, "effects": []}

    class ProviderTransport(FixtureTransport):
        def send(self, method, params, token):
            if method == "tools/list":
                return {
                    "result": {
                        "tools": [{"name": upstream, "inputSchema": schema} for upstream, _ in WRITE_TOOLS.values()]
                    }
                }
            if method == "tools/call":
                upstream, arguments = params["name"], params["arguments"]
                provider["effects"].append(upstream)
                if upstream == "createJiraIssue":
                    provider["issues"]["OWNED-1"] = {
                        "summary": arguments["fields"]["summary"],
                        "comments": [],
                        "status": "To Do",
                    }
                elif upstream == "editJiraIssue":
                    provider["issues"][arguments["issueKey"]]["summary"] = arguments["fields"]["summary"]
                elif upstream == "addOrEditJiraIssueComment":
                    provider["issues"][arguments["issueKey"]]["comments"].append(arguments["comment"])
                elif upstream == "transitionJiraIssue":
                    provider["issues"][arguments["issueKey"]]["status"] = arguments["status"]
                return {"result": {"structuredContent": {"issueKey": "OWNED-1"}}}
            return super().send(method, params, token)

    operations = (
        ("create_jira_issue", {"cloudId": "site-one", "fields": {"summary": "initial"}}),
        ("update_jira_issue", {"cloudId": "site-one", "issueKey": "OWNED-1", "fields": {"summary": "revised"}}),
        ("comment_jira_issue", {"cloudId": "site-one", "issueKey": "OWNED-1", "comment": "note"}),
        ("transition_jira_issue", {"cloudId": "site-one", "issueKey": "OWNED-1", "status": "Done"}),
    )
    for alias, arguments in operations:
        receipt = write_tool(
            "fixture-transient-token",
            alias,
            arguments,
            pins,
            deadline=time.monotonic() + 20,
            transport_factory=ProviderTransport,
        )
        assert receipt["structuredContent"]["issueKey"] == "OWNED-1"
    assert provider["issues"]["OWNED-1"] == {
        "summary": "revised",
        "comments": ["note"],
        "status": "Done",
    }
    assert provider["effects"] == [method for method, _ in WRITE_TOOLS.values()]


def test_write_configuration_defaults_off_and_rejects_unpinned_schemas(release):
    from pydantic import ValidationError

    from tool_adapters.config import AdapterConfiguration

    config = {
        "schema_version": "1.0",
        "mode": "observation",
        "release": release,
        "observation": {
            "core_authorize_url": "https://core.example.test/internal/v1/tool-adapters/authorize",
            "core_role_arn": "arn:aws:iam::123456789012:role/core",
            "assertion_key_arn": "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111",
            "edge_provenance_sha256": "a" * 64,
            "identity_workload": "adp-tools-fixture-atlassian-mcp-oauth-user",
            "identity_provider": "adp-tools-fixture-atlassian-mcp-user",
            "config_revision": "b" * 64,
            "subject_schema_sha256": "c" * 64,
            "sites_schema_sha256": "d" * 64,
            "gateway_role_arn": "arn:aws:iam::123456789012:role/gateway",
        },
    }
    assert AdapterConfiguration.model_validate(config).observation.write_enabled is False
    with pytest.raises(ValidationError, match="unverified write configuration"):
        AdapterConfiguration.model_validate({**config, "observation": {**config["observation"], "write_enabled": True}})
    with pytest.raises(ValidationError, match="unverified write configuration"):
        AdapterConfiguration.model_validate(
            {
                **config,
                "observation": {**config["observation"], "write_schemas": {alias: SCHEMA for alias in WRITE_TOOLS}},
            }
        )


def test_write_transport_never_retries_provider_rate_limit(monkeypatch):
    from tool_adapters.atlassian import WriteMCPTransport
    from tool_adapters.atlassian_observe import MCPTransport, ProviderResponseError

    calls = []

    def unavailable(self, method, params, token):
        calls.append(method)
        raise ProviderResponseError("rate_limited", retry_after=0)

    monkeypatch.setattr(MCPTransport, "send", unavailable)
    with pytest.raises(ProviderResponseError, match="rate_limited"):
        WriteMCPTransport(deadline=time.monotonic() + 20).send("tools/call", {}, "fixture-token")
    assert calls == ["tools/call"]


def test_approved_jira_writes_private_route_bind_dispatch_and_receipt(release, monkeypatch):
    import base64
    import json
    from unittest.mock import AsyncMock

    from adp_tools.adapter_contracts import Admission
    from fastapi.testclient import TestClient

    from tool_adapters import atlassian_route
    from tool_adapters.config import AdapterConfiguration
    from tool_adapters.server import create_app

    gateway_role = "arn:aws:iam::123456789012:role/fixture-gateway"
    schema = {"type": "object", "properties": {"cloudId": {"type": "string"}}, "required": ["cloudId"]}
    digest = hashlib.sha256(canonical(schema)).hexdigest()
    config = AdapterConfiguration.model_validate(
        {
            "schema_version": "1.0",
            "mode": "observation",
            "release": release,
            "observation": {
                "core_authorize_url": "https://core.example.test/internal/v1/tool-adapters/authorize",
                "core_role_arn": "arn:aws:iam::123456789012:role/fixture-core",
                "assertion_key_arn": "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111",
                "edge_provenance_sha256": hashlib.sha256(b"fixture-edge").hexdigest(),
                "identity_workload": "adp-tools-fixture-atlassian-mcp-oauth-user",
                "identity_provider": "adp-tools-fixture-atlassian-mcp-user",
                "config_revision": "a" * 64,
                "subject_schema_sha256": "b" * 64,
                "sites_schema_sha256": "c" * 64,
                "gateway_role_arn": gateway_role,
                "read_schemas": {alias: schema for alias in READ_TOOLS},
                "read_schemas_sha256": {alias: digest for alias in READ_TOOLS},
                "write_enabled": True,
                "write_schemas": {alias: schema for alias in WRITE_TOOLS},
                "write_schemas_sha256": {alias: digest for alias in WRITE_TOOLS},
            },
        }
    )
    now = int(time.time() * 1000)
    receipt_id = "receipt-one"
    connection = {"id": "connection-one", "revision": "revision-one", "site_id": "site-one"}
    arguments = {"cloudId": "site-one"}
    payload_digest = hashlib.sha256(canonical(arguments)).hexdigest()
    binding = {
        "operation_id": "relay-one",
        "receipt_id": receipt_id,
        "payload_sha256": payload_digest,
        "release": release,
    }
    admission = Admission.model_validate(
        {
            "schema_version": "1.0",
            "admission_id": "admission-one",
            "binding": binding,
            "purpose": "tool_call",
            "connector": "atlassian",
            "operation": "create_jira_issue",
            "identity": {
                "workload": config.observation.identity_workload,
                "provider": config.observation.identity_provider,
                "canonical_user_id": "owner-one",
            },
            "context": {
                "tenant_id": "tenant-one",
                "canonical_user_id": "owner-one",
                "connection_id": connection["id"],
                "binding_revision": connection["revision"],
                "site_id": "site-one",
                "scopes": ["write:jira:agent-interface"],
            },
            "issued_at_ms": now,
            "expires_at_ms": now + 5000,
            "deadline_at_ms": now + 16000,
        }
    )
    authorization = AsyncMock(return_value=admission)
    dispatch = AsyncMock()
    identity = AsyncMock(return_value="fixture-transient-token")
    effects = []
    monkeypatch.setattr(atlassian_route, "publish_reads", AsyncMock(return_value=[]))
    monkeypatch.setattr(atlassian_route, "authorize_read", authorization)
    monkeypatch.setattr(atlassian_route, "consume_dispatch", dispatch)
    monkeypatch.setattr(atlassian_route, "retrieve_access_token", identity)

    def mutate(token, alias, received, pins, *, deadline):
        assert dispatch.await_count == len(effects) + 1
        assert (token, received, pins) == (
            "fixture-transient-token",
            arguments,
            config.observation.write_schemas_sha256,
        )
        assert deadline > time.monotonic()
        effects.append(alias)
        return {"structuredContent": {"id": f"JIRA-{len(effects)}"}}

    monkeypatch.setattr(atlassian_route, "write_tool", mutate)
    headers = {"x-adp-edge-provenance": "fixture-edge", "x-caller-identity": gateway_role}
    with TestClient(create_app(config, source_revision="a" * 40)) as client:
        listing = client.post(
            "/internal/v1/tools/atlassian/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert {tool["name"] for tool in listing.json()["result"]["tools"]} == set(READ_TOOLS) | set(WRITE_TOOLS)
        for alias in WRITE_TOOLS:
            authorization.return_value = admission.model_copy(update={"operation": alias})
            operation_id = f"OP#tenant-one#owner-one#atlassian___{alias}#request-one"
            approval = {
                "tool": f"atlassian___{alias}",
                "request_id": "request-one",
                "payload_sha256": payload_digest,
                "policy_revision": 1,
                "consent_source": "explicit",
            }
            recorded = {
                "tool": f"atlassian___{alias}",
                "operation_id": operation_id,
                "request_id": "request-one",
                "receipt_id": receipt_id,
                "execution_id": "execution-one",
                "payload_sha256": payload_digest,
                "release": release,
                "connection": connection,
                "dispatch_intent": "dispatched",
                "approval": approval,
            }
            claims = {
                "binding": binding,
                "rpc_id": "relay-one",
                "tool": f"atlassian___{alias}",
                "principal": {"tenant_id": "tenant-one", "user_id": "owner-one"},
                "connection": connection,
                "arguments_sha256": payload_digest,
                "atlassian_approval": approval,
                "atlassian_operation": recorded,
                "expires_at": (now // 1000) + 18,
            }

            def call(overrides=None):
                assertion = base64.urlsafe_b64encode(json.dumps({"claims": {**claims, **(overrides or {})}}).encode())
                return client.post(
                    "/internal/v1/tools/atlassian/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": "relay-one",
                        "method": "tools/call",
                        "params": {
                            "name": alias,
                            "arguments": {**arguments, "adp_assertion": assertion.decode().rstrip("=")},
                        },
                    },
                )

            for invalid in (
                {"atlassian_operation": None},
                {"atlassian_operation": {**recorded, "release": {**release, "backend_generation": 2}}},
                {"atlassian_operation": {**recorded, "payload_sha256": "b" * 64}},
                {"atlassian_approval": {**approval, "policy_revision": 2}},
            ):
                assert call(invalid).status_code == 400
            assert len(effects) == dispatch.await_count
            result = call()
            assert result.status_code == 200
            assert result.json()["result"]["structuredContent"]["id"] == f"JIRA-{len(effects)}"
    assert len(effects) == dispatch.await_count == len(WRITE_TOOLS)


@pytest.mark.parametrize("assumed_role", [False, True])
@pytest.mark.parametrize("protocol_version", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"])
@pytest.mark.parametrize("transport_id", ["relay-7", "agentcore-target-8", 8])
def test_authenticated_private_route_uses_pinned_catalogue_and_core_receipt(
    release, monkeypatch, assumed_role, protocol_version, transport_id
):
    import base64
    import json
    from unittest.mock import AsyncMock

    from adp_tools.adapter_contracts import Admission
    from fastapi.testclient import TestClient

    from tool_adapters import atlassian_route
    from tool_adapters.config import AdapterConfiguration
    from tool_adapters.server import create_app

    gateway_role = "arn:aws:iam::123456789012:role/fixture-gateway"
    read_schema = {
        "type": "object",
        "properties": {"cloudId": {"type": "string"}, "id": {"type": "string"}},
        "required": ["cloudId", "id"],
    }
    digests = {alias: hashlib.sha256(canonical(read_schema)).hexdigest() for alias in READ_TOOLS}
    config = AdapterConfiguration.model_validate(
        {
            "schema_version": "1.0",
            "mode": "observation",
            "release": release,
            "observation": {
                "core_authorize_url": "https://core.example.test/internal/v1/tool-adapters/authorize",
                "core_role_arn": "arn:aws:iam::123456789012:role/fixture-core",
                "assertion_key_arn": "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111",
                "edge_provenance_sha256": hashlib.sha256(b"fixture-edge").hexdigest(),
                "identity_workload": "adp-tools-fixture-atlassian-mcp-oauth-user",
                "identity_provider": "adp-tools-fixture-atlassian-mcp-user",
                "config_revision": "a" * 64,
                "subject_schema_sha256": "b" * 64,
                "sites_schema_sha256": "c" * 64,
                "gateway_role_arn": gateway_role,
                "read_schemas": {alias: read_schema for alias in READ_TOOLS},
                "read_schemas_sha256": digests,
            },
        }
    )
    headers = {"x-adp-edge-provenance": "fixture-edge", "x-caller-identity": gateway_role}
    if assumed_role:
        headers["x-caller-identity"] = "arn:aws:sts::123456789012:assumed-role/fixture-gateway/gateway-session-live"
    now = int(time.time() * 1000)
    binding = {"operation_id": "relay-7", "receipt_id": "receipt-1", "payload_sha256": "", "release": release}
    arguments = {"cloudId": "site-one", "id": "fixture-id"}
    binding["payload_sha256"] = hashlib.sha256(canonical(arguments)).hexdigest()
    claims = {
        "binding": binding,
        "rpc_id": "relay-7",
        "tool": "atlassian___get_jira_issue",
        "principal": {"tenant_id": "tenant-one", "user_id": "owner-one"},
        "connection": {"id": "connection-one", "revision": "revision-one", "site_id": "site-one"},
        "arguments_sha256": binding["payload_sha256"],
        "expires_at": (now // 1000) + 18,
    }
    assertion = base64.urlsafe_b64encode(json.dumps({"claims": claims}).encode()).decode().rstrip("=")
    admission = Admission.model_validate(
        {
            "schema_version": "1.0",
            "admission_id": "admission-1",
            "binding": binding,
            "purpose": "tool_call",
            "connector": "atlassian",
            "operation": "get_jira_issue",
            "identity": {
                "workload": config.observation.identity_workload,
                "provider": config.observation.identity_provider,
                "canonical_user_id": "owner-one",
            },
            "context": {
                "tenant_id": "tenant-one",
                "canonical_user_id": "owner-one",
                "connection_id": "connection-one",
                "binding_revision": "revision-one",
                "site_id": "site-one",
                "scopes": ["read:jira:agent-interface"],
            },
            "issued_at_ms": now,
            "expires_at_ms": now + 5000,
            "deadline_at_ms": now + 16000,
        }
    )
    import httpx

    production_client = atlassian_route.httpx.AsyncClient
    published = []

    def respond(request):
        assert request.url.path == "/internal/v1/tool-adapters/catalogue"
        assert json.loads(request.content)["release"] == release
        tools = json.loads(request.content)["tools"]
        assert {tool["name"] for tool in tools} == set(READ_TOOLS)
        published.append(tools)
        return httpx.Response(200, json={"status": "ready"})

    monkeypatch.setattr(atlassian_route, "_sign_dispatch", lambda url, payload: {"content-type": "application/json"})
    monkeypatch.setattr(
        atlassian_route.httpx,
        "AsyncClient",
        lambda **kwargs: production_client(transport=httpx.MockTransport(respond)),
    )
    assert {tool["name"] for tool in asyncio.run(atlassian_route.publish_reads(config))} == set(READ_TOOLS)
    assert len(published) == 1
    monkeypatch.setattr(atlassian_route.httpx, "AsyncClient", production_client)
    publication = AsyncMock(return_value=[])
    monkeypatch.setattr(atlassian_route, "publish_reads", publication)
    authorization = AsyncMock(return_value=admission)
    dispatch = AsyncMock()
    identity = AsyncMock(return_value="fixture-transient-token")
    reads = []
    monkeypatch.setattr(atlassian_route, "authorize_read", authorization)
    monkeypatch.setattr(atlassian_route, "consume_dispatch", dispatch)
    monkeypatch.setattr(atlassian_route, "retrieve_access_token", identity)

    def read(token, alias, received, pins, *, deadline, reviewed_schemas):
        assert token == "fixture-transient-token" and pins == digests and deadline > time.monotonic()
        assert reviewed_schemas == config.observation.read_schemas
        reads.append((alias, received))
        return {"structuredContent": {"id": "JIRA-ISSUE-42"}}

    monkeypatch.setattr(atlassian_route, "read_tool", read)
    with TestClient(create_app(config, source_revision="a" * 40)) as client:
        path = "/internal/v1/tools/atlassian/mcp"
        initialize = {
            "jsonrpc": "2.0",
            "id": "gateway-initialize",
            "method": "initialize",
            "params": {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "gateway", "version": "1"},
            },
        }
        assert client.post(path, json=initialize).status_code == 403
        initialized = client.post(path, headers=headers, json=initialize)
        assert initialized.status_code == 200
        assert initialized.json() == {
            "jsonrpc": "2.0",
            "id": "gateway-initialize",
            "result": {
                "protocolVersion": "2025-06-18" if protocol_version == "2025-11-25" else protocol_version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "adp-private-atlassian", "version": "1.0"},
            },
        }
        notification = client.post(
            path, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        )
        assert notification.status_code == 202 and not notification.content
        ping = client.post(path, headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "ping"})
        assert ping.status_code == 200 and ping.json() == {"jsonrpc": "2.0", "id": 2, "result": {}}
        for invalid_params in (None, [], {}, {"protocolVersion": 20250618}, {"protocolVersion": "invalid"}):
            assert client.post(path, headers=headers, json={**initialize, "params": invalid_params}).status_code == 400
        publication.assert_not_awaited()
        authorization.assert_not_awaited()
        identity.assert_not_awaited()
        assert (
            client.post(
                "/internal/v1/tools/atlassian/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            ).status_code
            == 403
        )
        listing = client.post(
            "/internal/v1/tools/atlassian/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {},
            },
        )
        assert listing.status_code == 200
        assert {tool["name"] for tool in listing.json()["result"]["tools"]} == set(READ_TOOLS)
        response = client.post(
            "/internal/v1/tools/atlassian/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": transport_id,
                "method": "tools/call",
                "params": {"name": "get_jira_issue", "arguments": {**arguments, "adp_assertion": assertion}},
            },
        )
        packet = json.loads(response.request.content)
        assert response.status_code == 200, response.text
        assert response.json()["id"] == transport_id
        assert authorization.await_args.args[0] == assertion
        assert authorization.await_args.args[1].operation_id == claims["rpc_id"]
        for invalid_id in (None, True, "", "x" * 257, [], {}):
            refused = client.post(path, headers=headers, json={**packet, "id": invalid_id})
            assert refused.status_code == 400
        for invalid_claims in (
            {**claims, "rpc_id": True},
            {**claims, "rpc_id": ""},
            {**claims, "rpc_id": "x" * 257},
            {**claims, "binding": {**binding, "operation_id": str(transport_id) + "-forged"}},
            {**claims, "arguments_sha256": "0" * 64},
        ):
            invalid_assertion = (
                base64.urlsafe_b64encode(json.dumps({"claims": invalid_claims}).encode()).decode().rstrip("=")
            )
            changed = {
                **packet,
                "params": {**packet["params"], "arguments": {**arguments, "adp_assertion": invalid_assertion}},
            }
            assert client.post(path, headers=headers, json=changed).status_code == 400
        authorization.assert_awaited_once()
        drifted_contexts = (
            ("tenant_id", "tenant-other"),
            ("canonical_user_id", "owner-other"),
            ("connection_id", "connection-other"),
            ("binding_revision", "revision-other"),
            ("site_id", "site-other"),
        )
        for field, value in drifted_contexts:
            authorization.return_value = admission.model_copy(
                update={"context": admission.context.model_copy(update={field: value})}
            )
            refused = client.post("/internal/v1/tools/atlassian/mcp", headers=headers, json=packet)
            assert refused.status_code == 403
            assert refused.json()["detail"]["error"] == "connection_changed"
        authorization.return_value = admission.model_copy(
            update={"identity": admission.identity.model_copy(update={"canonical_user_id": "owner-other"})}
        )
        assert client.post("/internal/v1/tools/atlassian/mcp", headers=headers, json=packet).status_code == 403
        authorization.return_value = admission.model_copy(update={"operation": "search_jira"})
        assert client.post("/internal/v1/tools/atlassian/mcp", headers=headers, json=packet).status_code == 403
        authorization.return_value = admission
        from tool_adapters.atlassian_observe import ProviderResponseError

        errors = (
            (ProviderResponseError("provider_unauthorized"), 401),
            (ProviderResponseError("provider_forbidden"), 403),
            (ProviderResponseError("not_found"), 404),
            (ProviderResponseError("rate_limited"), 429),
            (ProviderResponseError("provider_response_too_large"), 503),
            (ValueError("provider deadline exceeded"), 503),
            (ValueError("catalogue_incomplete"), 503),
            (ValueError("unverified provider schema"), 503),
            (TimeoutError(), 503),
        )
        for error, status in errors:

            def failed_read(*args, **kwargs):
                raise error

            monkeypatch.setattr(atlassian_route, "read_tool", failed_read)
            failure = client.post("/internal/v1/tools/atlassian/mcp", headers=headers, json=packet)
            assert failure.status_code == status
            expected = (
                "provider_contract_unverified"
                if str(error) == "unverified provider schema"
                else "provider_unavailable"
                if isinstance(error, TimeoutError)
                else str(error)
            )
            assert failure.json()["detail"]["error"] == expected
            assert "fixture-transient-token" not in failure.text
        from fastapi import HTTPException

        identity.side_effect = HTTPException(503, detail={"error": "identity_unavailable"})
        denied = client.post("/internal/v1/tools/atlassian/mcp", headers=headers, json=packet)
        assert denied.status_code == 503 and denied.json()["detail"]["error"] == "identity_unavailable"
        assert reads == [("get_jira_issue", arguments)]
    assert response.status_code == 200
    assert response.json()["result"]["structuredContent"]["id"] == "JIRA-ISSUE-42"
    assert reads == [("get_jira_issue", arguments)]
    publication.assert_awaited_once_with(config)
    assert authorization.await_count == 2 + len(drifted_contexts) + 2 + len(errors)
    assert dispatch.await_count == identity.await_count == 2 + len(errors)
    dispatch.assert_awaited_with(admission, config)
    identity.assert_awaited_with(admission, config)


@pytest.mark.parametrize(
    ("code", "retry_after", "elapsed", "attempts"),
    [
        ("rate_limited", 2, 0, 2),
        ("provider_unavailable", None, 0, 2),
        ("provider_unavailable", 2, 4, 1),
        ("rate_limited", 6, 0, 1),
        ("rate_limited", None, 0, 1),
        ("provider_unauthorized", None, 0, 1),
        ("provider_forbidden", None, 0, 1),
        ("not_found", None, 0, 1),
        ("provider_response_too_large", None, 0, 1),
    ],
)
def test_read_retry_budget_and_protocol_errors(monkeypatch, code, retry_after, elapsed, attempts):
    from types import SimpleNamespace

    from tool_adapters import atlassian
    from tool_adapters.atlassian_observe import MCPTransport, ProviderResponseError

    clock = [100.0]
    sleeps = []
    calls = []

    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    def reply(transport, method, params, token):
        calls.append((method, transport.deadline, clock[0]))
        if len(calls) == 1:
            clock[0] += elapsed
            raise ProviderResponseError(code, retry_after)
        raise ProviderResponseError(code)

    monkeypatch.setattr(atlassian, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(MCPTransport, "send", reply)
    transport = atlassian.ReadMCPTransport(deadline=120)
    with pytest.raises(ProviderResponseError, match=code):
        transport.send("tools/call", {"name": "searchJiraIssuesUsingJql"}, "fixture-transient-token")
    assert len(calls) == attempts
    assert all(deadline == 105 for _, deadline, _ in calls)
    assert sleeps == ([retry_after] if retry_after and attempts == 2 else [])
    assert transport.deadline == 120
    calls.clear()
    with pytest.raises(ProviderResponseError, match=code):
        transport.send("tools/list", {}, "fixture-transient-token")
    assert len(calls) == 1


def test_read_connect_timeout_and_response_bounds(monkeypatch):
    from types import SimpleNamespace

    from tool_adapters import atlassian_observe
    from tool_adapters.atlassian import ReadMCPTransport
    from tool_adapters.atlassian_observe import MCPTransport, ProviderResponseError

    issued = []

    class Socket:
        def settimeout(self, value):
            issued.append(value)

    class Response:
        status = 200
        content = b""

        def getheader(self, name, default=None):
            if name == "Content-Type":
                return "application/json"
            if name == "Retry-After":
                return "2"
            return default

        def read(self, limit):
            issued.append(limit)
            return self.content[:limit]

    class Connection:
        def __init__(self, host, timeout):
            issued.append(timeout)
            self.sock = Socket()

        def request(self, method, path, body, headers):
            assert method == "POST" and path == "/v2/mcp?tools=all"
            assert headers["Authorization"] == "Bearer fixture-transient-token"

        def getresponse(self):
            return response

        def close(self):
            pass

    response = Response()
    monkeypatch.setattr(
        atlassian_observe.socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, ("8.8.8.8", 443))]
    )
    monkeypatch.setattr(atlassian_observe.socket, "create_connection", lambda *args, **kwargs: Socket())
    monkeypatch.setattr(
        atlassian_observe.ssl,
        "create_default_context",
        lambda: SimpleNamespace(wrap_socket=lambda *args, **kwargs: Socket()),
    )
    monkeypatch.setattr(atlassian_observe.http.client, "HTTPSConnection", Connection)
    for status, code in [
        (401, "provider_unauthorized"),
        (403, "provider_forbidden"),
        (404, "not_found"),
        (429, "rate_limited"),
        (503, "provider_unavailable"),
    ]:
        response.status = status
        with pytest.raises(ProviderResponseError, match=code) as failure:
            MCPTransport(deadline=time.monotonic() + 20).send("tools/call", {}, "fixture-transient-token")
        assert failure.value.retry_after == 2
    response.status = 200
    response.content = b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}' + b" " * 70_000
    with pytest.raises(ProviderResponseError, match="catalogue_incomplete"):
        MCPTransport(deadline=time.monotonic() + 20).send("tools/list", {}, "fixture-transient-token")
    assert ReadMCPTransport(deadline=time.monotonic() + 20).send("tools/list", {}, "fixture-transient-token")[
        "result"
    ] == {"tools": []}
    assert 2 in issued
    assert 65537 in issued
    assert 4 * 1024 * 1024 + 128 * 1024 + 1 in issued
    response.content = b"x" * (4 * 1024 * 1024 + 1)
    with pytest.raises(ProviderResponseError, match="provider_response_too_large"):
        ReadMCPTransport(deadline=time.monotonic() + 20).send("tools/call", {}, "fixture-transient-token")


@pytest.mark.parametrize("fault", ["cursor_loop", "page_limit", "tool_limit", "definition_bytes", "catalogue_bytes"])
def test_catalogue_page_and_byte_limits_fail_explicitly(fault):
    class CatalogueTransport(FixtureTransport):
        def send(self, method, params, token):
            if method != "tools/list":
                return super().send(method, params, token)
            index = int(params["cursor"]) if params else 0
            tools = (
                [{"name": upstream, "inputSchema": SCHEMA} for upstream, _ in READ_TOOLS.values()] if index == 0 else []
            )
            if fault == "cursor_loop":
                cursor = "1" if index == 0 else "1"
            elif fault == "page_limit":
                cursor = str(index + 1)
            elif fault == "tool_limit":
                tools.extend({"name": f"extra-{number}", "inputSchema": SCHEMA} for number in range(1021))
                cursor = None
            elif fault == "definition_bytes":
                tools[0]["description"] = "z" * (256 * 1024)
                cursor = None
            else:
                tools.extend(
                    {"name": f"extra-{number}", "description": "z" * 250_000, "inputSchema": SCHEMA}
                    for number in range(17)
                )
                cursor = None
            return {"result": {"tools": tools, "nextCursor": cursor}}

    with pytest.raises(ValueError, match="catalogue_incomplete"):
        discover_reads(
            "fixture-transient-token", PINS, deadline=time.monotonic() + 20, transport_factory=CatalogueTransport
        )


def test_discovery_aggregate_deadline_with_virtual_clock(monkeypatch):
    from types import SimpleNamespace

    from tool_adapters import atlassian

    clock = [100.0]
    limits = []

    class SlowTransport(FixtureTransport):
        def __init__(self, *, deadline):
            limits.append(deadline)
            self.calls = []

        def send(self, method, params, token):
            if method == "tools/list":
                clock[0] += 21
            return super().send(method, params, token)

    monkeypatch.setattr(atlassian, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    with pytest.raises(ValueError, match="provider deadline exceeded"):
        discover_reads("fixture-transient-token", PINS, deadline=1000, transport_factory=SlowTransport)
    assert limits == [120]


def test_rate_limit_retry_succeeds_once_inside_deadline(monkeypatch):
    from types import SimpleNamespace

    from tool_adapters import atlassian
    from tool_adapters.atlassian_observe import MCPTransport, ProviderResponseError

    clock = [200.0]
    calls = []
    delays = []

    def reply(transport, method, params, token):
        calls.append((method, transport.deadline))
        if len(calls) == 1:
            raise ProviderResponseError("rate_limited", retry_after=2)
        return {"result": {"structuredContent": {"id": "JIRA-ISSUE-42"}}}

    def advance(delay):
        delays.append(delay)
        clock[0] += delay

    monkeypatch.setattr(atlassian, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=advance))
    monkeypatch.setattr(MCPTransport, "send", reply)
    transport = atlassian.ReadMCPTransport(deadline=220)
    result = transport.send("tools/call", {"name": "getJiraIssue"}, "fixture-transient-token")
    assert result["result"]["structuredContent"]["id"] == "JIRA-ISSUE-42"
    assert calls == [("tools/call", 205), ("tools/call", 205)]
    assert delays == [2]
    assert transport.deadline == 220


@pytest.mark.parametrize("fault", ["missing_final", "rpc_error", "provider_unauthorized", "rate_limited", "timeout"])
def test_partial_provider_catalogue_never_becomes_an_empty_read(fault):
    from tool_adapters.atlassian_observe import ProviderResponseError

    calls = []

    class FaultTransport(FixtureTransport):
        def send(self, method, params, token):
            calls.append(method)
            if method == "tools/list":
                if not params:
                    return {
                        "result": {
                            "tools": [{"name": READ_TOOLS["search_jira"][0], "inputSchema": SCHEMA}],
                            "nextCursor": "last-page",
                        }
                    }
                if fault == "missing_final":
                    return {"result": {"tools": []}}
                if fault == "rpc_error":
                    return {"error": {"code": -32603}}
                if fault == "timeout":
                    raise TimeoutError("upstream timed out")
                raise ProviderResponseError(fault)
            return super().send(method, params, token)

    failure = (
        ValueError
        if fault in {"missing_final", "rpc_error"}
        else TimeoutError
        if fault == "timeout"
        else ProviderResponseError
    )
    with pytest.raises(failure):
        read_tool(
            "fixture-transient-token",
            "search_jira",
            {"id": "fixture-id"},
            PINS,
            deadline=time.monotonic() + 20,
            transport_factory=FaultTransport,
        )
    assert calls == ["initialize", "notifications/initialized", "tools/list", "tools/list"]
    assert "tools/call" not in calls


@pytest.mark.parametrize(
    "result",
    [
        {"structuredContent": {"issues": []}},
        {"content": []},
        {},
        {"isError": True, "content": []},
        {"isError": "true", "content": []},
    ],
)
def test_empty_provider_result_requires_an_explicit_success_shape(result):
    from tool_adapters.atlassian_observe import ProviderResponseError

    class EmptyTransport(FixtureTransport):
        def send(self, method, params, token):
            if method == "tools/call":
                return {"result": result}
            return super().send(method, params, token)

    if result in ({}, {"isError": True, "content": []}, {"isError": "true", "content": []}):
        with pytest.raises(ProviderResponseError, match="provider_unavailable"):
            read_tool(
                "fixture-transient-token",
                "search_jira",
                {"id": "fixture-id"},
                PINS,
                deadline=time.monotonic() + 20,
                transport_factory=EmptyTransport,
            )
    else:
        assert (
            read_tool(
                "fixture-transient-token",
                "search_jira",
                {"id": "fixture-id"},
                PINS,
                deadline=time.monotonic() + 20,
                transport_factory=EmptyTransport,
            )
            == result
        )


def test_jira_uncertain_readback_is_core_authorized_on_original_backend_only(release, monkeypatch):
    import base64
    import json
    from unittest.mock import AsyncMock

    from adp_tools.adapter_contracts import Admission
    from fastapi.testclient import TestClient

    from tool_adapters import atlassian_route
    from tool_adapters.config import AdapterConfiguration
    from tool_adapters.server import create_app

    schema = {
        "type": "object",
        "properties": {"cloudId": {"type": "string"}, "issueIdOrKey": {"type": "string"}},
        "required": ["cloudId", "issueIdOrKey"],
    }
    digest = hashlib.sha256(canonical(schema)).hexdigest()
    role = "arn:aws:iam::123456789012:role/fixture-reconciler"
    config = AdapterConfiguration.model_validate(
        {
            "schema_version": "1.0",
            "mode": "observation",
            "release": release,
            "observation": {
                "core_authorize_url": "https://core.example.test/internal/v1/tool-adapters/authorize",
                "core_role_arn": "arn:aws:iam::123456789012:role/fixture-core",
                "assertion_key_arn": "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111",
                "edge_provenance_sha256": hashlib.sha256(b"fixture-edge").hexdigest(),
                "identity_workload": "adp-tools-fixture-atlassian-mcp-oauth-user",
                "identity_provider": "adp-tools-fixture-atlassian-mcp-user",
                "config_revision": "a" * 64,
                "subject_schema_sha256": "b" * 64,
                "sites_schema_sha256": "c" * 64,
                "gateway_role_arn": "arn:aws:iam::123456789012:role/fixture-gateway",
                "reconciler_role_arn": role,
                "read_schemas": {alias: schema for alias in READ_TOOLS},
                "read_schemas_sha256": {alias: digest for alias in READ_TOOLS},
                "write_enabled": True,
                "write_schemas": {alias: schema for alias in WRITE_TOOLS},
                "write_schemas_sha256": {alias: digest for alias in WRITE_TOOLS},
            },
        }
    )
    arguments = {"cloudId": "site-one", "issueIdOrKey": "ISSUE-42"}
    connection = {"id": "connection-one", "revision": "revision-one", "site_id": "site-one"}
    original = {
        "tool": "atlassian___update_jira_issue",
        "operation_id": "OP#tenant#owner#update#request-one",
        "receipt_id": "receipt-one",
        "payload_sha256": "c" * 64,
        "connection": connection,
        "release": release,
        "request_id": "request-one",
    }
    binding = {
        "operation_id": original["operation_id"],
        "receipt_id": original["receipt_id"],
        "payload_sha256": original["payload_sha256"],
        "release": release,
    }
    claims = {
        "principal": {"tenant_id": "tenant", "user_id": "owner"},
        "connection": connection,
        "atlassian_operation": original,
        "read_arguments_sha256": hashlib.sha256(canonical(arguments)).hexdigest(),
    }
    assertion = base64.urlsafe_b64encode(json.dumps({"claims": claims}).encode()).decode().rstrip("=")
    now = int(time.time() * 1000)
    admission = Admission.model_validate(
        {
            "schema_version": "1.0",
            "admission_id": "reconcile-one",
            "binding": binding,
            "purpose": "reconcile",
            "connector": "atlassian",
            "operation": "get_jira_issue",
            "identity": {
                "workload": config.observation.identity_workload,
                "provider": config.observation.identity_provider,
                "canonical_user_id": "owner",
            },
            "context": {
                "tenant_id": "tenant",
                "canonical_user_id": "owner",
                "connection_id": connection["id"],
                "binding_revision": connection["revision"],
                "site_id": "site-one",
                "scopes": ["read:jira:agent-interface"],
            },
            "issued_at_ms": now,
            "expires_at_ms": now + 5000,
            "deadline_at_ms": now + 16000,
        }
    )
    authorize = AsyncMock(return_value=admission)
    dispatch = AsyncMock()
    reads = []
    monkeypatch.setattr(atlassian_route, "authorize_read", authorize)
    monkeypatch.setattr(atlassian_route, "consume_dispatch", dispatch)
    monkeypatch.setattr(atlassian_route, "retrieve_access_token", AsyncMock(return_value="fixture-token"))

    def read(token, alias, received, pins, *, deadline, reviewed_schemas):
        assert token == "fixture-token" and alias == "get_jira_issue"
        assert received == arguments and pins == config.observation.read_schemas_sha256
        assert reviewed_schemas == config.observation.read_schemas
        reads.append(received)
        return {"structuredContent": {"id": "ISSUE-42"}}

    monkeypatch.setattr(atlassian_route, "read_tool", read)
    headers = {
        "x-adp-edge-provenance": "fixture-edge",
        "x-caller-identity": role.replace(":iam:", ":sts:").replace(":role/", ":assumed-role/") + "/reconciler-session",
    }
    body = {
        "schema_version": "1.0",
        "purpose": "reconcile",
        "connector": "atlassian",
        "operation": "get_jira_issue",
        "binding": binding,
        "read_arguments": arguments,
        "signed_assertion": assertion,
        "deadline_at_ms": now + 16000,
    }
    with TestClient(create_app(config, source_revision=release["source_revision"])) as client:
        path = "/internal/v1/tools/atlassian/reconcile"
        for invalid in (
            {"binding": {**binding, "release": {**release, "backend_generation": 2}}},
            {"read_arguments": {**arguments, "issueIdOrKey": "ISSUE-OTHER"}},
        ):
            assert client.post(path, json={**body, **invalid}, headers=headers).status_code == 403
        foreign = client.post(
            path, json=body, headers={**headers, "x-caller-identity": "arn:aws:iam::123456789012:role/foreign"}
        )
        assert foreign.status_code == 403
        assert reads == []
        response = client.post(path, json=body, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json() == {
            "status": "unknown",
            "operation_id": binding["operation_id"],
            "backend_generation": 1,
            "readback_performed": True,
        }
    assert reads == [arguments] and authorize.await_count == 1 and dispatch.await_count == 0


@pytest.mark.parametrize("transport_id", [None, "agentcore-write-target", 42])
def test_write_denials(release, monkeypatch, transport_id):
    from atlassian_write_scenarios import DENIALS, WriteDenialRoute

    with WriteDenialRoute(release, monkeypatch) as scenario:
        for alias in WRITE_TOOLS:
            approved = scenario.call(alias, transport_id=transport_id)
            assert approved.status_code == 200, (alias, approved.text)
            before = scenario.provider_readback()
            assert approved.json()["result"]["structuredContent"]["id"] in before["issues"]
            dispatched = scenario.dispatch.await_count
            for change in DENIALS:
                denied = scenario.call(alias, change, transport_id=transport_id)
                assert denied.status_code in {400, 403}, (alias, change, denied.text)
                assert scenario.provider_readback() == before, (alias, change)
                assert scenario.dispatch.await_count == dispatched, (alias, change)
        assert scenario.provider_readback()["effects"] == len(WRITE_TOOLS)
        assert scenario.identity.await_count == len(WRITE_TOOLS)


def test_relay_id_change_cannot_avoid_original_write_dispatch(release, monkeypatch):
    from atlassian_write_scenarios import WriteDenialRoute
    from fastapi import HTTPException

    consumed = set()

    async def dispatch(admission, configuration):
        assert admission.binding.release == configuration.release
        identifier = admission.binding.operation_id
        assert identifier == "relay-create_jira_issue"
        if identifier in consumed:
            raise HTTPException(503, detail={"error": "authority_unavailable"})
        consumed.add(identifier)

    with WriteDenialRoute(release, monkeypatch) as scenario:
        scenario.dispatch.side_effect = dispatch
        first = scenario.call("create_jira_issue", transport_id="target-one")
        assert first.status_code == 200 and first.json()["id"] == "target-one"
        before = scenario.provider_readback()
        replay = scenario.call("create_jira_issue", transport_id="target-two")
        assert replay.status_code == 503
        assert scenario.provider_readback() == before
        assert before["effects"] == scenario.identity.await_count == 1
        assert scenario.dispatch.await_count == 2


def test_uncertain_write_recovery(release, monkeypatch):
    from atlassian_recovery_scenarios import UncertainWriteRoute

    scenario = UncertainWriteRoute(release, monkeypatch)
    try:
        scenario.restart()
        lost = scenario.write()
        assert lost.status_code == 503
        assert scenario.effect_count == 1
        assert scenario.provider["FIX-42"]["summary"] == "updated"
        assert scenario.provider["FOREIGN-1"]["summary"] == "unrelated"
        scenario.restart()
        assert scenario.write().status_code == 503
        assert scenario.effect_count == 1 and scenario.dispatch.await_count == 2
        scenario.restart(generation=2)
        assert scenario.write().status_code == 403
        assert scenario.readback().status_code == 403
        assert scenario.effect_count == 1
        scenario.restart()
        scenario.active_generation = 2
        assert scenario.write().status_code == 403
        assert scenario.readback().status_code == 403
        scenario.active_generation = 1
        assert scenario.write().status_code == 503
        reconciled = scenario.readback()
        assert reconciled.status_code == 200, reconciled.text
        assert reconciled.json() == {
            "status": "unknown",
            "operation_id": scenario.operation_id,
            "backend_generation": 1,
            "readback_performed": True,
        }
        assert scenario.effect_count == 1 and scenario.read_count == 1
    finally:
        scenario.close()
