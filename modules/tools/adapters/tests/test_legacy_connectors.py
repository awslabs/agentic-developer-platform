"""Independent baseline oracle for the four existing connector operations."""

import hashlib
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tool_adapters.legacy_catalogue import LEGACY_SCOPES, legacy_tools
from tool_adapters.server import create_app

BASELINE = {
    "github": {"get_user": ((), "read:user")},
    "jira": {"get_issue": (("cloudId", "issueIdOrKey"), "read:jira-work")},
    "slack": {
        "get_user": (("user",), "users:read"),
        "post_message": (("channel", "text", "run_marker"), "chat:write"),
    },
}


@pytest.mark.parametrize("connector", ["github", "jira", "slack"])
def test_legacy_schema_and_grant_inventory_matches_existing_gateway_fixture(connector):
    tools = legacy_tools(connector)
    assert set(LEGACY_SCOPES[connector]) == set(BASELINE[connector])
    assert {tool["name"] for tool in tools} == set(BASELINE[connector])
    for tool in tools:
        properties, scope = BASELINE[connector][tool["name"]]
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert tuple(schema["properties"]) == properties
        if properties:
            assert schema["required"] == list(properties)
        else:
            assert "required" not in schema
        assert LEGACY_SCOPES[connector][tool["name"]] == scope
    before = hashlib.sha256(json.dumps(tools, sort_keys=True).encode()).hexdigest()
    tools[0]["inputSchema"]["properties"]["forged"] = {"type": "string"}
    assert hashlib.sha256(json.dumps(legacy_tools(connector), sort_keys=True).encode()).hexdigest() == before


def test_unapproved_connector_is_not_advertised_or_called(configuration):
    with pytest.raises(ValueError, match="unsupported legacy connector"):
        legacy_tools("atlassian")
    with TestClient(create_app(configuration, source_revision="a" * 40)) as client:
        for connector in BASELINE:
            response = client.post(
                f"/internal/v1/tools/{connector}/mcp",
                json={"jsonrpc": "2.0", "id": "fixture", "method": "tools/call", "params": {}},
            )
            assert response.status_code == 403
            assert response.json() == {"error": "adapter_authority_required"}


@pytest.mark.parametrize(
    "connector,operation,arguments,scopes,account,site",
    [
        ("github", "get_user", {}, ["read:user"], "17", None),
        (
            "jira",
            "get_issue",
            {"cloudId": "11111111-1111-4111-8111-111111111111", "issueIdOrKey": "EX-1"},
            ["read:jira-work", "read:me"],
            "jira-account",
            "11111111-1111-4111-8111-111111111111",
        ),
        ("slack", "get_user", {"user": "U12345678"}, ["users:read"], "T12345678:U12345678", None),
        (
            "slack",
            "post_message",
            {
                "channel": "C12345678",
                "text": "Test message [run:11111111-1111-4111-8111-111111111111]",
                "run_marker": "11111111-1111-4111-8111-111111111111",
            },
            ["users:read", "chat:write"],
            "T12345678:U12345678",
            None,
        ),
    ],
)
@pytest.mark.parametrize("transport_id", ["rpc-1", "agentcore-target-2", 2])
def test_private_legacy_route_requires_core_admission_before_provider(
    release, monkeypatch, connector, operation, arguments, scopes, account, site, transport_id
):
    import asyncio
    import base64
    import time

    from adp_tools.adapter_contracts import Admission

    from tool_adapters import legacy_route
    from tool_adapters.atlassian_observe import canonical
    from tool_adapters.config import AdapterConfiguration

    gateway_role = "arn:aws:iam::123456789012:role/fixture-gateway"
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
                "identity_workload": "fixture-atlassian-mcp-oauth-user",
                "identity_provider": "fixture-atlassian-mcp-user",
                "legacy_identity_workload": "fixture-oauth-user",
                "config_revision": "a" * 64,
                "subject_schema_sha256": "b" * 64,
                "sites_schema_sha256": "c" * 64,
                "gateway_role_arn": gateway_role,
            },
        }
    )
    headers = {
        "x-adp-edge-provenance": "fixture-edge",
        "x-caller-identity": gateway_role.replace(":iam:", ":sts:").replace(":role/", ":assumed-role/")
        + "/gateway-session",
    }
    connection = {"connection_id": "connection-1", "revision": "revision-1"}
    now = int(time.time() * 1000)
    binding = {
        "operation_id": "rpc-1",
        "receipt_id": "receipt-1",
        "payload_sha256": hashlib.sha256(canonical(arguments)).hexdigest(),
        "release": release,
    }
    claims = {
        "rpc_id": "rpc-1",
        "binding": binding,
        "tool": f"{connector}___{operation}",
        "arguments_sha256": binding["payload_sha256"],
        "expires_at": now // 1000 + 18,
        "principal": {"tenant_id": "tenant-1", "user_id": "owner-1"},
        "connection": connection,
        f"{connector}_connection": connection,
        "connection_id": connection["connection_id"],
        "connection_scopes": scopes,
        "site_id": site,
        "run_marker": arguments.get("run_marker"),
    }
    assertion = base64.urlsafe_b64encode(json.dumps({"claims": claims}).encode()).decode().rstrip("=")
    admission = Admission.model_validate(
        {
            "schema_version": "1.0",
            "admission_id": "admission-1",
            "binding": binding,
            "purpose": "tool_call",
            "connector": connector,
            "operation": operation,
            "identity": {
                "workload": "fixture-oauth-user",
                "provider": f"fixture-{'atlassian' if connector == 'jira' else connector}-user",
                "canonical_user_id": "owner-1",
            },
            "context": {
                "tenant_id": "tenant-1",
                "canonical_user_id": "owner-1",
                "connection_id": "connection-1",
                "binding_revision": "revision-1",
                "site_id": site,
                "scopes": scopes,
                "legacy": {
                    "provider_account_id": account,
                    "observed_scopes": scopes,
                    "site_scopes": ["read:jira-work"] if connector == "jira" else [],
                },
            },
            "issued_at_ms": now - 100,
            "expires_at_ms": now + 5000,
            "deadline_at_ms": now + 9000,
        }
    )
    events = []

    async def authorize(*args, **kwargs):
        events.append("authorize")
        assert kwargs == {"connector": connector}
        assert args[0] == assertion
        assert args[1].operation_id == claims["rpc_id"]
        return admission

    async def dispatch(*args):
        events.append("dispatch")

    async def identity(*args):
        events.append("identity")
        return "fixture-token"

    async def provider(token, name, verb, passed, protected):
        events.append("provider")
        assert (token, name, verb, passed) == ("fixture-token", connector, operation, arguments)
        assert protected["account_id"] == account
        assert protected["scopes"] == scopes
        return {"id": "fixture-result"}

    monkeypatch.setattr(legacy_route, "authorize_read", authorize)
    monkeypatch.setattr(legacy_route, "consume_dispatch", dispatch)
    monkeypatch.setattr(legacy_route, "retrieve_access_token", identity)
    monkeypatch.setattr(legacy_route, "execute_legacy", provider)
    packet = {
        "jsonrpc": "2.0",
        "id": transport_id,
        "method": "tools/call",
        "params": {"name": operation, "arguments": {**arguments, "adp_assertion": assertion}},
    }
    monkeypatch.setenv("ADP_TOOL_ADAPTER_LEGACY_ENABLED", "true")
    with TestClient(create_app(config, source_revision="a" * 40)) as client:
        response = client.post(f"/internal/v1/tools/{connector}/mcp", json=packet, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["id"] == transport_id
        assert json.loads(response.json()["result"]["content"][0]["text"]) == {"id": "fixture-result"}
        assert events == ["authorize", "dispatch", "identity", "provider"]
        events.clear()
        published = []

        async def publish(settings, name, tools):
            assert settings == config and name == connector
            published.append(tools)

        monkeypatch.setattr(legacy_route, "publish_legacy_catalogue", publish)
        listing = client.post(
            f"/internal/v1/tools/{connector}/mcp",
            json={"jsonrpc": "2.0", "id": "listing", "method": "tools/list", "params": {}},
            headers=headers,
        )
        assert listing.status_code == 200
        assert len(published) == 1
        assert {tool["name"] for tool in published[0]} == {tool["name"] for tool in listing.json()["result"]["tools"]}
        assert events == []

        async def reject_publication(*args):
            raise HTTPException(503, detail={"error": "catalogue_incomplete"})

        monkeypatch.setattr(legacy_route, "publish_legacy_catalogue", reject_publication)
        unavailable = client.post(
            f"/internal/v1/tools/{connector}/mcp",
            json={"jsonrpc": "2.0", "id": "listing", "method": "tools/list", "params": {}},
            headers=headers,
        )
        assert unavailable.status_code == 503 and "result" not in unavailable.json()
        assert events == []
        monkeypatch.setattr(legacy_route, "publish_legacy_catalogue", publish)
        if connector == "github":
            app_context = admission.context.model_copy(
                update={
                    "scopes": (),
                    "legacy": admission.context.legacy.model_copy(
                        update={
                            "app_id": "51",
                            "resources_sha256": "a" * 64,
                        }
                    ),
                }
            )

            async def app_admission(*args, **kwargs):
                events.append("authorize")
                return admission.model_copy(update={"context": app_context})

            monkeypatch.setattr(legacy_route, "authorize_read", app_admission)
            refused = client.post("/internal/v1/tools/github/mcp", json=packet, headers=headers)
            assert refused.status_code == 403
            assert refused.json() == {"detail": {"error": "github_app_grant_unverified"}}
            assert events == ["authorize"]
            events.clear()
            monkeypatch.setattr(legacy_route, "authorize_read", authorize)

        async def malformed_provider(*args):
            raise ValueError("invalid provider response")

        monkeypatch.setattr(legacy_route, "execute_legacy", malformed_provider)
        broken = client.post(f"/internal/v1/tools/{connector}/mcp", json=packet, headers=headers)
        assert broken.status_code == 502
        assert broken.json() == {"detail": {"error": "provider_unavailable"}}
        assert events == ["authorize", "dispatch", "identity"]
        events.clear()
        monkeypatch.setattr(legacy_route, "execute_legacy", provider)
        for changed in (
            *({**packet, "id": invalid_id} for invalid_id in (None, True, "", "x" * 257, [], {})),
            {
                **packet,
                "params": {**packet["params"], "arguments": {**packet["params"]["arguments"], "forged": "value"}},
            },
        ):
            assert client.post(f"/internal/v1/tools/{connector}/mcp", json=changed, headers=headers).status_code in (
                400,
                403,
            )
            assert events == []
        monkeypatch.setattr(
            legacy_route,
            "authorize_read",
            lambda *args, **kwargs: asyncio.sleep(
                0,
                result=admission.model_copy(
                    update={"context": admission.context.model_copy(update={"canonical_user_id": "foreign"})}
                ),
            ),
        )
        assert client.post(f"/internal/v1/tools/{connector}/mcp", json=packet, headers=headers).status_code == 403
        assert events == []
    monkeypatch.delenv("ADP_TOOL_ADAPTER_LEGACY_ENABLED")
    with TestClient(create_app(config, source_revision="a" * 40)) as client:
        assert client.post(f"/internal/v1/tools/{connector}/mcp", json=packet, headers=headers).status_code == 403


@pytest.mark.parametrize("connector", ("github", "jira", "slack"))
def test_legacy_catalogue_publication_requires_core_success(monkeypatch, release, connector):
    import asyncio

    import httpx

    from tool_adapters import legacy_route
    from tool_adapters.config import AdapterConfiguration

    config = AdapterConfiguration.model_validate(
        {
            "schema_version": "1.0",
            "mode": "observation",
            "release": release,
            "observation": {
                "core_authorize_url": "https://core.example.test/internal/v1/tool-adapters/authorize",
                "core_role_arn": "arn:aws:iam::123456789012:role/fixture-core",
                "assertion_key_arn": "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111",
                "edge_provenance_sha256": "a" * 64,
                "identity_workload": "fixture-atlassian-mcp-oauth-user",
                "identity_provider": "fixture-atlassian-mcp-user",
                "legacy_identity_workload": "fixture-oauth-user",
                "config_revision": "a" * 64,
                "subject_schema_sha256": "b" * 64,
                "sites_schema_sha256": "c" * 64,
            },
        }
    )
    approved = legacy_tools(connector)
    actual = []
    accepted = True
    original_client = legacy_route.httpx.AsyncClient

    def respond(request):
        actual.append(json.loads(request.content))
        assert request.url.path == "/internal/v1/tool-adapters/catalogue/legacy"
        return httpx.Response(200 if accepted else 503, json={"status": "ready"} if accepted else {})

    monkeypatch.setattr(legacy_route, "_sign_dispatch", lambda url, payload: {"content-type": "application/json"})
    monkeypatch.setattr(
        legacy_route.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    asyncio.run(legacy_route.publish_legacy_catalogue(config, connector, approved))
    assert actual == [{"schema_version": "1.0", "release": release, "connector": connector, "tools": approved}]
    accepted = False
    with pytest.raises(HTTPException) as error:
        asyncio.run(legacy_route.publish_legacy_catalogue(config, connector, approved))
    assert error.value.status_code == 503
