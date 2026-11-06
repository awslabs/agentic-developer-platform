"""L5 tool contract and authenticated MCP integration checks."""

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import boto3
import httpx
import pytest
from fastapi import FastAPI, HTTPException

from src.internal import tools_l5
from src.shared.models.organization import Organization, User

CONTRACT = Path(__file__).resolve().parents[4] / "docs/l5-services/contracts/v1/mcp-tools.json"


def test_tool_catalogue_matches_packaged_security_contract(monkeypatch):
    descriptors = json.loads(CONTRACT.read_text())
    monkeypatch.setattr(tools_l5, "files", lambda package: CONTRACT.parents[4] / "docs/l5-services")
    tools_l5.l5_tool_catalogue.cache_clear()
    try:
        listing = tools_l5.l5_tool_catalogue()
        assert {tool["name"] for tool in listing["tools"]} == {descriptor["name"].replace("___", ".", 1) for descriptor in descriptors}
        assert len(listing["tools"]) == len(descriptors)
        for tool, descriptor in zip(listing["tools"], descriptors):
            assert tool["inputSchema"] == descriptor["inputSchema"]
            assert tool["outputSchema"] == descriptor["outputSchema"]
            assert tool["annotations"] == descriptor["annotations"]
            assert tool["http_operation"] == descriptor["x-http-operation"]
        assert {"l5.start_binding_operation", "l5.reconcile_binding_operation", "l5.retry_publication", "l5.list_triage"} <= {
            tool["name"] for tool in listing["tools"]
        }
    finally:
        tools_l5.l5_tool_catalogue.cache_clear()


def test_missing_or_malformed_packaged_contract_is_not_advertised(monkeypatch, tmp_path):
    monkeypatch.setattr(tools_l5, "files", lambda package: tmp_path)
    tools_l5.l5_tool_catalogue.cache_clear()
    with pytest.raises(HTTPException) as error:
        tools_l5.l5_tool_catalogue()
    assert error.value.status_code == 503
    tools_l5.l5_tool_catalogue.cache_clear()
    contract = tmp_path / "contracts/v1/mcp-tools.json"
    contract.parent.mkdir(parents=True)
    contract.write_text('[{"name":"l5___forged","inputSchema":{}}]')
    with pytest.raises(HTTPException) as error:
        tools_l5.l5_tool_catalogue()
    assert error.value.status_code == 503
    tools_l5.l5_tool_catalogue.cache_clear()


def test_all_security_tools_map_to_shipped_http_operations(monkeypatch):
    monkeypatch.setattr(tools_l5, "files", lambda package: CONTRACT.parents[4] / "docs/l5-services")
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    descriptors = json.loads(CONTRACT.read_text())
    try:
        for descriptor in descriptors:
            schema = descriptor["inputSchema"]
            arguments = {
                name: ({"action": "check"} if name == "input" else 1 if schema["properties"][name].get("type") == "integer" else "fixture-value")
                for name in schema["required"]
            }
            method, path, query, headers, body = tools_l5.l5_http_request(descriptor["name"], arguments)
            assert method in {"GET", "POST", "PUT"}
            assert path.startswith("/v1/products/") and "{" not in path
            assert not query
            assert body == arguments.get("input")
            if "idempotency_key" in arguments:
                assert headers["Idempotency-Key"] == arguments["idempotency_key"]
            if "assessment_ref" in arguments:
                assert "runs/fixture-value" in path
        assert tools_l5.l5_http_request(
            "l5___download_artifact", {"product_id": "product", "project_id": "project", "service_id": "security", "artifact_id": "artifact"}
        )[1].endswith("/artifacts/artifact")
        method, path, query, headers, body = tools_l5.l5_http_request(
            "l5___save_configuration",
            {
                "product_id": "product",
                "project_id": "project",
                "service_id": "security",
                "input": {},
                "idempotency_key": "fixture-request-key",
                "create_only": True,
            },
        )
        assert (method, headers, body) == ("PUT", {"Idempotency-Key": "fixture-request-key", "If-None-Match": "*"}, {})
        with pytest.raises(HTTPException) as error:
            tools_l5.l5_http_request(
                "l5___get_run",
                {
                    "product_id": "product",
                    "project_id": "project",
                    "service_id": "security",
                    "run_id": "forged",
                },
            )
        assert error.value.status_code == 422
    finally:
        tools_l5.l5_tool_catalogue.cache_clear()
        tools_l5.l5_operations.cache_clear()


def test_unsupported_server_contract_does_not_dispatch(monkeypatch, tmp_path):
    monkeypatch.setattr(tools_l5, "files", lambda package: tmp_path)
    contract = tmp_path / "contracts/v1"
    contract.mkdir(parents=True)
    (contract / "mcp-tools.json").write_bytes(CONTRACT.read_bytes())
    openapi = json.loads((CONTRACT.parent / "openapi.json").read_text())
    openapi["info"]["version"] = "2.0.0"
    (contract / "openapi.json").write_text(json.dumps(openapi))
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    try:
        with pytest.raises(HTTPException) as error:
            tools_l5.l5_http_request(
                "l5___get_configuration",
                {
                    "product_id": "product",
                    "project_id": "project",
                    "service_id": "security",
                },
            )
        assert error.value.status_code == 503
    finally:
        tools_l5.l5_tool_catalogue.cache_clear()
        tools_l5.l5_operations.cache_clear()


def test_unimplemented_http_operations_are_not_discovered(monkeypatch):
    monkeypatch.syspath_prepend(str(CONTRACT.parents[4] / "modules/L5-services/src"))
    monkeypatch.setattr(tools_l5, "files", lambda package: CONTRACT.parents[4] / "docs/l5-services")
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    try:
        listing = tools_l5.available_l5_catalogue()
        from l5_services.api.router import create_router

        implemented = {route.operation_id for route in create_router(lambda: None).routes}
        advertised = {tool["http_operation"] for tool in listing["tools"]}
        for tool in tools_l5.l5_tool_catalogue()["tools"]:
            if tool["http_operation"] not in implemented and tool["http_operation"] != "downloadArtifact":
                assert tool["http_operation"] not in advertised
        assert advertised
    finally:
        tools_l5.l5_tool_catalogue.cache_clear()
        tools_l5.l5_operations.cache_clear()


async def test_http_dispatch_reads_configuration_through_real_router(monkeypatch):
    monkeypatch.syspath_prepend(str(CONTRACT.parents[4] / "modules/L5-services/src"))
    monkeypatch.setattr(tools_l5, "files", lambda package: CONTRACT.parents[4] / "docs/l5-services")
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()

    class Backend:
        async def get(self, product_id, project_id, service_id):
            assert (product_id, project_id, service_id) == ("product-a", "project-a", "security")
            return {"configuration_id": "fixture-configuration", "hash": "opaque-revision"}

    try:
        response = await tools_l5.dispatch_l5(
            "l5___get_configuration",
            {
                "product_id": "product-a",
                "project_id": "project-a",
                "service_id": "security",
            },
            Backend(),
        )
        assert response["structuredContent"] == {
            "data": {"configuration_id": "fixture-configuration", "hash": "opaque-revision"},
            "http_status": 200,
            "etag": '"opaque-revision"',
        }
        assert json.loads(response["content"][0]["text"]) == response["structuredContent"]
    finally:
        tools_l5.l5_tool_catalogue.cache_clear()
        tools_l5.l5_operations.cache_clear()


async def test_tool_roundtrip(monkeypatch, valid_token_context):
    monkeypatch.syspath_prepend(str(CONTRACT.parents[4] / "modules/L5-services/src"))
    with monkeypatch.context() as isolated:
        isolated.setattr(boto3, "client", lambda *args, **kwargs: object())
        from src import l5_integration

    monkeypatch.setattr(tools_l5, "files", lambda package: CONTRACT.parents[4] / "docs/l5-services")
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()

    class Backend:
        async def get(self, product_id, project_id, service_id):
            assert (product_id, project_id, service_id) == ("product-one", "project-one", "security")
            return {"configuration_id": "config-one", "hash": "private-etag"}

    delegation_ids = []
    normalized_owner = valid_token_context.model_copy(update={"user_id": "owner-one"})

    async def normalized_actor(db, actor):
        # This dispatch-only test supplies the host identity seam. Canonical
        # workspace resolution is exercised independently by the real DB tests.
        return normalized_owner if actor.account_type == "human" else actor

    normalization = AsyncMock(side_effect=normalized_actor)
    monkeypatch.setattr(l5_integration, "l5_actor", normalization)

    def selected(service_id, *, request, db, actor, x_l5_delegation_id, response):
        assert service_id == "security"
        assert request.app is application
        if actor.account_type == "human":
            assert actor is normalized_owner
        delegation_ids.append(x_l5_delegation_id)
        return Backend()

    monkeypatch.setattr(l5_integration, "backend", selected)
    application = FastAPI()
    application.include_router(l5_integration.router)
    application.dependency_overrides[l5_integration.get_current_user] = lambda: valid_token_context
    application.dependency_overrides[l5_integration.get_db] = lambda: object()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
            listing = await client.post("/v1/l5/mcp/tools/list")
            assert listing.status_code == 200
            assert "l5___get_configuration" in {tool["name"] for tool in listing.json()["tools"]}
            response = await client.post(
                "/v1/l5/mcp/tools/call",
                json={
                    "tool": "l5___get_configuration",
                    "arguments": {
                        "product_id": "product-one",
                        "project_id": "project-one",
                        "service_id": "security",
                    },
                },
            )
            assert response.status_code == 200
            result = response.json()
            assert result["structuredContent"]["data"]["configuration_id"] == "config-one"
            assert result["structuredContent"]["etag"] == '"private-etag"'
            assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
            assert delegation_ids == [None]
            application.dependency_overrides[l5_integration.get_current_user] = lambda: valid_token_context.model_copy(
                update={"account_type": "service", "agent_registry_id": "registered-principal"}
            )

            class ProductGrants:
                async def scalars(self, statement):
                    return SimpleNamespace(all=lambda: ["verified-grant"])

            application.dependency_overrides[l5_integration.get_db] = lambda: ProductGrants()
            delegated = await client.post(
                "/v1/l5/mcp/tools/call",
                headers={"X-L5-Delegation-ID": "forged"},
                json={
                    "tool": "l5___get_configuration",
                    "arguments": {
                        "product_id": "product-one",
                        "project_id": "project-one",
                        "service_id": "security",
                    },
                },
            )
            assert delegated.status_code == 200
            assert delegation_ids == [None, "verified-grant"]
            assert normalization.await_count == 2
    finally:
        tools_l5.l5_tool_catalogue.cache_clear()
        tools_l5.l5_operations.cache_clear()


@pytest.mark.parametrize(
    "path,account_type,grants,status",
    [
        ("/v1/l5/mcp/tools/call", "human", [], 200),
        ("/v1/l5/mcp/tools/call", "service", ["verified-grant"], 200),
        ("/v1/l5/mcp/tools/call", "service", [], 404),
        ("/v1/l5/mcp/tools/call", "service", ["grant-one", "grant-two"], 404),
        ("/internal/v1/tools/l5-call", "human", [], 200),
    ],
)
async def test_security_tool_routes_construct_real_backend(monkeypatch, db_session, valid_token_context, path, account_type, grants, status):
    monkeypatch.syspath_prepend(str(CONTRACT.parents[4] / "modules/L5-services/src"))
    with monkeypatch.context() as isolated:
        isolated.setattr(boto3, "client", lambda *args, **kwargs: object())
        from src import l5_integration
        from src.internal import tools_authorize

    monkeypatch.setattr(tools_l5, "files", lambda package: CONTRACT.parents[4] / "docs/l5-services")
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    monkeypatch.delenv("L5_WORK_QUEUE_URL", raising=False)
    monkeypatch.setenv("ADP_TOOLS_FACADE_ROLE_ARN", "arn:aws:iam::123456789012:role/tools-facade")
    monkeypatch.setattr(
        tools_authorize,
        "verified_caller_identity",
        lambda request, settings: "arn:aws:sts::123456789012:assumed-role/tools-facade/pod",
    )
    actor = valid_token_context.model_copy(
        update={"account_type": account_type, "agent_registry_id": "registered-principal" if account_type == "service" else ""}
    )
    actor._authenticated_subject_id = actor.user_id
    member = User(
        id="owner-one",
        cognito_sub=actor.user_id,
        org_id=actor.org_id,
        team_id=actor.team_id,
        email="l5-tool-owner@example.test",
        user_kind="human",
    )
    db_session.add_all([Organization(id=actor.org_id, name="Tool test workspace"), member])
    await db_session.commit()
    monkeypatch.setattr(tools_authorize, "validate_cognito_jwt", AsyncMock(return_value=actor))
    monkeypatch.setattr(tools_authorize, "membership", AsyncMock(return_value=(member, None, None)))
    application = FastAPI()
    application.include_router(l5_integration.router)
    application.include_router(tools_authorize.router, prefix="/internal/v1")
    application.state.l5_devops_verification_reader = object()
    application.dependency_overrides[l5_integration.get_current_user] = lambda: actor
    application.dependency_overrides[tools_authorize.require_agent_transport] = lambda: None
    session = db_session
    calls = []
    try:
        with monkeypatch.context() as database_mocks:
            application.dependency_overrides[l5_integration.get_db] = lambda: session
            database_mocks.setattr(session, "scalars", AsyncMock(return_value=SimpleNamespace(all=lambda: grants)))

            async def get(selected, product_id, project_id, service_id):
                assert selected.db is session
                if account_type == "human":
                    assert selected.actor is not actor
                    assert selected.actor.user_id == member.id != actor.user_id
                    assert selected.actor._authenticated_subject_id == actor.user_id
                else:
                    assert selected.actor is actor
                assert selected.store.tenant_id == actor.org_id
                assert selected.verification_reader is None
                assert selected.delegation_id == (grants[0] if account_type == "service" else None)
                calls.append((product_id, project_id, service_id))
                return {"configuration_id": "config-one", "hash": "private-etag"}

            monkeypatch.setattr(l5_integration.GatewayConfigurationBackend, "get", get)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
                response = await client.post(
                    path,
                    headers={"x-adp-user-authorization": "Bearer verified-login", "X-L5-Delegation-ID": "forged"},
                    json={
                        "tool": "l5___get_configuration",
                        "arguments": {"product_id": "product-one", "project_id": "project-one", "service_id": "security"},
                    },
                )
            assert response.status_code == status, response.text
            if status == 200:
                assert response.json()["structuredContent"] == {
                    "data": {"configuration_id": "config-one", "hash": "private-etag"},
                    "http_status": 200,
                    "etag": '"private-etag"',
                }
                assert calls == [("product-one", "project-one", "security")]
            else:
                assert calls == []
    finally:
        tools_l5.l5_tool_catalogue.cache_clear()
        tools_l5.l5_operations.cache_clear()


def test_cli_discovers_and_calls_security_tools_without_run_id_authority(monkeypatch):
    cli = CONTRACT.parents[4] / "modules/gateway/cli"
    monkeypatch.syspath_prepend(str(cli))
    spec = importlib.util.spec_from_file_location("l5_mcp_client_fixture", cli / "adp-mcp.py")
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    monkeypatch.setattr(client.native_handoff, "ready", lambda: {"result": {"tools": []}})
    monkeypatch.setattr(client.native_handoff, "descriptors", lambda tools: [])
    tools = json.loads(CONTRACT.read_text())
    requests = []

    def gateway(path, body=None):
        requests.append((path, body))
        if path == "/v1/l5/mcp/tools/list":
            return {"tools": [{key: value for key, value in tool.items() if key != "x-http-operation"} for tool in tools]}
        if path == "/agent/tools":
            raise client.GatewayHTTPError(403)
        if path == "/v1/l5/mcp/tools/call":
            return {
                "structuredContent": {"data": {"items": []}, "http_status": 200},
                "content": [{"type": "text", "text": '{"data":{"items":[]},"http_status":200}'}],
            }
        raise AssertionError(path)

    monkeypatch.setattr(client, "gateway_request", gateway)
    listing = client.handle({"method": "tools/list"})
    assert {tool["name"] for tool in tools} <= {tool["name"] for tool in listing["tools"]}
    assert next(tool for tool in listing["tools"] if tool["name"] == "l5___get_configuration")["outputSchema"]
    result = client.handle(
        {
            "method": "tools/call",
            "params": {
                "name": "l5___get_configuration",
                "arguments": {
                    "product_id": "product",
                    "project_id": "project",
                    "service_id": "security",
                },
            },
        }
    )
    assert result["structuredContent"]["http_status"] == 200
    assert requests[-1][0] == "/v1/l5/mcp/tools/call"
    with pytest.raises(client.ToolError, match="authority arguments"):
        client.handle({"method": "tools/call", "params": {"name": "l5___get_run", "arguments": {"run_id": "forged"}}})
    assert requests[-1][0] == "/v1/l5/mcp/tools/call"


@pytest.mark.parametrize("mode,proxy", [("crew-proxy", True), ("sigv4-proxy", True), ("native-test", False)])
@pytest.mark.parametrize("path", ["/v1/l5/mcp/tools/list", "/v1/l5/mcp/tools/call"])
def test_cli_l5_routes_are_unavailable_on_restricted_transports(monkeypatch, mode, proxy, path):
    cli = CONTRACT.parents[4] / "modules/gateway/cli"
    monkeypatch.syspath_prepend(str(cli))
    spec = importlib.util.spec_from_file_location("l5_mcp_restricted_transport", cli / "adp-mcp.py")
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)

    def unexpected_dispatch(*args, **kwargs):
        pytest.fail("Unavailable L5 route reached network or native session dispatch")

    monkeypatch.setattr(client, "build_opener", unexpected_dispatch)
    monkeypatch.setattr(client.NativeTransport, "configured", unexpected_dispatch)
    context = {"url": "http://127.0.0.1:1", "headers": {}, "proxy": proxy, "binding": {"mode": mode}}
    with pytest.raises(client.GatewayHTTPError) as error:
        client.gateway_request(path, {}, context=context)
    assert error.value.status == 404


@pytest.mark.parametrize("status", [403, 503])
def test_cli_l5_discovery_does_not_hide_server_failures(monkeypatch, status):
    cli = CONTRACT.parents[4] / "modules/gateway/cli"
    monkeypatch.syspath_prepend(str(cli))
    spec = importlib.util.spec_from_file_location("l5_mcp_failed_discovery", cli / "adp-mcp.py")
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)

    def rejected(request, timeout):
        assert request.full_url == "https://gateway.example.org/api/v1/l5/mcp/tools/list"
        assert request.get_method() == "POST"
        raise client.HTTPError(request.full_url, status, "unavailable", {}, None)

    context = {"url": "https://gateway.example.org/api", "headers": {}, "proxy": False, "binding": {"mode": "bearer"}}
    monkeypatch.setattr(client, "gateway_context", lambda: context)
    monkeypatch.setattr(client.NativeTransport, "configured", lambda: None)
    monkeypatch.setattr(client, "build_opener", lambda *args: SimpleNamespace(open=rejected))
    with pytest.raises(client.GatewayHTTPError) as error:
        client.handle({"method": "tools/list"})
    assert error.value.status == status


def test_tool_replay_and_availability(monkeypatch, tmp_path):
    cli = CONTRACT.parents[4] / "modules/gateway/cli"
    monkeypatch.syspath_prepend(str(cli))
    spec = importlib.util.spec_from_file_location("l5_mcp_replay", cli / "adp-mcp.py")
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    monkeypatch.setenv("BG_CONFIG_DIR", str(tmp_path))
    binding = {"value": {"mode": "bearer", "tenant": "tenant-one"}}
    monkeypatch.setattr(client, "gateway_context", lambda: {"binding": binding["value"]})
    calls = []
    lost = {"first": True}
    run = {
        "run_id": "run-one",
        "state": "running",
        "monitoring": {
            "policy": {"poll_interval_seconds": 600},
            "progress_coverage": "unavailable",
        },
    }

    def gateway(path, body=None, *, context=None):
        assert path == "/v1/l5/mcp/tools/call" and context["binding"] == binding["value"]
        calls.append(body)
        if body["tool"] == "l5___submit_run" and lost["first"]:
            lost["first"] = False
            raise client.ToolError("lost response")
        data = run
        if body["tool"] == "l5___get_run":
            assert body["arguments"]["assessment_ref"] == run["run_id"]
        payload = {"http_status": 200 if body["tool"] == "l5___get_run" else 202, "data": data}
        return {"structuredContent": payload, "content": [{"type": "text", "text": json.dumps(payload)}]}

    monkeypatch.setattr(client, "gateway_request", gateway)
    arguments = {
        "product_id": "product-one",
        "project_id": "project-one",
        "service_id": "security",
        "input": {"configuration_revision": 1},
        "idempotency_key": "one_original_run_key_0001",
    }
    with pytest.raises(client.ToolError, match="idempotency_key"):
        client.handle(
            {
                "method": "tools/call",
                "params": {
                    "name": "l5___submit_run",
                    "arguments": {key: value for key, value in arguments.items() if key != "idempotency_key"},
                },
            }
        )
    assert calls == []
    first = client.handle({"method": "tools/call", "params": {"name": "l5___submit_run", "arguments": arguments}})
    assert first["isError"] is True
    operation_ref = json.loads(first["content"][0]["text"])["operation_ref"]
    record = client.load_operation(operation_ref)
    assert record["arguments"] == arguments and record["status"] == "unknown"
    assert len(calls) == 1
    status = client.handle(
        {
            "method": "tools/call",
            "params": {
                "name": "adp___operation_status",
                "arguments": {"operation_ref": operation_ref},
            },
        }
    )
    assert status["isError"] is True and len(calls) == 1
    binding["value"] = {"mode": "bearer", "tenant": "tenant-two"}
    denied = client.handle(
        {
            "method": "tools/call",
            "params": {
                "name": "adp___operation_retry",
                "arguments": {"operation_ref": operation_ref},
            },
        }
    )
    assert denied["isError"] is True and len(calls) == 1
    binding["value"] = {"mode": "bearer", "tenant": "tenant-one"}
    rejected = client.handle(
        {
            "method": "tools/call",
            "params": {
                "name": "adp___operation_retry",
                "arguments": {
                    "operation_ref": operation_ref,
                    "tool": "l5___submit_run",
                    "arguments": {**arguments, "idempotency_key": "forged_key_000001"},
                },
            },
        }
    )
    assert rejected["isError"] is True and len(calls) == 1
    replay = client.handle(
        {
            "method": "tools/call",
            "params": {
                "name": "adp___operation_retry",
                "arguments": {"operation_ref": operation_ref},
            },
        }
    )
    assert replay["structuredContent"]["data"] == run
    assert [call["arguments"]["idempotency_key"] for call in calls] == [arguments["idempotency_key"]] * 2
    read = client.handle(
        {
            "method": "tools/call",
            "params": {
                "name": "adp___operation_status",
                "arguments": {"operation_ref": operation_ref},
            },
        }
    )
    assert read["structuredContent"]["data"]["state"] == "running"
    assert "progress_percent" not in str(read)
    assert [call["tool"] for call in calls] == ["l5___submit_run", "l5___submit_run", "l5___get_run"]
    assert client.load_operation(operation_ref)["assessment_ref"] == "run-one"
    with monkeypatch.context() as isolated:

        def mismatched(path, body=None, *, context=None):
            payload = {"http_status": 200, "data": {"run_id": "different-run"}}
            return {"structuredContent": payload, "content": [{"type": "text", "text": json.dumps(payload)}]}

        isolated.setattr(client, "gateway_request", mismatched)
        mismatch = client.handle(
            {
                "method": "tools/call",
                "params": {
                    "name": "adp___operation_status",
                    "arguments": {"operation_ref": operation_ref},
                },
            }
        )
        assert mismatch["isError"] is True
    assert client.load_operation(operation_ref)["assessment_ref"] == "run-one"


async def test_tool_authority(monkeypatch):
    monkeypatch.syspath_prepend(str(CONTRACT.parents[4] / "modules/L5-services/src"))
    with monkeypatch.context() as isolated:
        isolated.setattr(boto3, "client", lambda *args, **kwargs: object())
        from src import l5_integration

    monkeypatch.setattr(tools_l5, "files", lambda package: CONTRACT.parents[4] / "docs/l5-services")
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    principal = {"actor": SimpleNamespace(account_type="service", org_id="tenant-a", agent_registry_id="registry-a")}
    allowed = {("tenant-a", "registry-a", "product-a", "project-a"): ["grant-a"], ("tenant-b", "registry-b", "product-b", "project-b"): ["grant-b"]}
    queries = []
    selected = []
    faults = {"status": None, "code": "private_leak", "revoked_source": False}

    class ProductGrants:
        async def scalars(self, statement):
            values = {value for value in statement.compile().params.values() if isinstance(value, str)}
            queries.append(values)
            for scope, grants in allowed.items():
                if set(scope) <= values:
                    return SimpleNamespace(all=lambda: grants)
            return SimpleNamespace(all=lambda: [])

    class Backend:
        def __init__(self, grant, actor):
            self.grant = grant
            self.actor = actor

        async def get(self, product_id, project_id, service_id):
            assert service_id == "security"
            assert (self.actor.org_id, self.actor.agent_registry_id, product_id, project_id) in allowed
            assert self.grant == allowed[(self.actor.org_id, self.actor.agent_registry_id, product_id, project_id)][0]
            if faults["status"] is not None:
                raise HTTPException(
                    faults["status"],
                    detail={
                        "code": faults["code"],
                        "message": "private-source.py",
                        "count": 17,
                        "retryable": faults["status"] == 503,
                    },
                )
            return {"configuration_id": "private-" + self.actor.org_id, "hash": "opaque"}

        async def result_store(self, product_id, project_id, service_id):
            assert faults["revoked_source"]
            raise HTTPException(
                404, detail={"code": "private_leak", "message": "private-source.py", "count": 17, "source_ref": "confidential-repository"}
            )

    def select_backend(service_id, *, request, db, actor, x_l5_delegation_id, response):
        assert service_id == "security"
        assert request.app is app
        selected.append(x_l5_delegation_id)
        return Backend(x_l5_delegation_id, actor)

    monkeypatch.setattr(l5_integration, "backend", select_backend)
    app = FastAPI()
    app.include_router(l5_integration.router)
    app.dependency_overrides[l5_integration.get_current_user] = lambda: principal["actor"]
    app.dependency_overrides[l5_integration.get_db] = ProductGrants
    args = {"product_id": "product-a", "project_id": "project-a", "service_id": "security"}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

            async def call(arguments, headers=None, tool="l5___get_configuration"):
                return await client.post("/v1/l5/mcp/tools/call", headers=headers, json={"tool": tool, "arguments": arguments})

            def assert_safe_error(response, status, code):
                assert response.status_code == 200
                result = response.json()
                assert result["isError"] is True
                payload = result["structuredContent"]
                assert json.loads(result["content"][0]["text"]) == payload
                assert set(payload) == {"http_status", "error"} and payload["http_status"] == status
                error = payload["error"]
                assert set(error) == {"code", "message", "retryable", "request_id"}
                assert error["code"] == code and UUID(error["request_id"]).version == 4
                assert not any(
                    value in response.text for value in ("private-", "confidential-repository", "count", "source_ref", "grant-a", "grant-b")
                )
                return error

            permitted = await call(args, {"X-L5-Delegation-ID": "grant-b"})
            assert permitted.status_code == 200
            assert permitted.json()["structuredContent"]["data"]["configuration_id"] == "private-tenant-a"
            assert selected == ["grant-a"]
            faults["revoked_source"] = True
            revoked_source = await call({**args, "artifact_id": "findings-private"}, tool="l5___get_artifact_access")
            assert_safe_error(revoked_source, 404, "not_found")
            faults["revoked_source"] = False
            faults["status"] = 403
            assert_safe_error(await call(args), 403, "forbidden")
            faults["status"] = 503
            assert assert_safe_error(await call(args), 503, "service_unavailable")["retryable"] is True
            faults["code"] = "source_capture_preparing"
            pending = assert_safe_error(await call(args), 503, "source_capture_preparing")
            assert pending["message"] == "Repository source preparation is in progress; check the original request receipt."
            faults["status"] = 500
            assert_safe_error(await call(args), 500, "service_unavailable")
            faults["code"] = "private_leak"
            faults["status"] = None
            transport_request = httpx.ASGITransport.handle_async_request

            async def non_json_failure(transport, request):
                if request.url.host == "l5.internal":
                    return httpx.Response(503, text="private-source.py from upstream")
                return await transport_request(transport, request)

            with monkeypatch.context() as isolated:
                isolated.setattr(httpx.ASGITransport, "handle_async_request", non_json_failure)
                assert_safe_error(await call(args), 503, "service_unavailable")
            initial_queries = len(queries)
            for forged in ({**args, "tenant_id": "tenant-b"}, {**args, "delegation_id": "grant-b"}, {**args, "run_id": "opaque-forged"}):
                invalid = await call(forged)
                assert invalid.status_code == 422
                assert len(queries) == initial_queries
            for wrong in ({**args, "product_id": "product-b"}, {**args, "project_id": "project-b"}):
                denied = await call(wrong)
                assert denied.status_code == 404
                assert "private-" not in denied.text and "grant-" not in denied.text
            principal["actor"] = SimpleNamespace(account_type="service", org_id="tenant-b", agent_registry_id="registry-b")
            denied = await call(args)
            assert denied.status_code == 404
            assert "private-" not in denied.text and "grant-" not in denied.text
            permitted_b = await call({"product_id": "product-b", "project_id": "project-b", "service_id": "security"})
            assert permitted_b.status_code == 200
            assert permitted_b.json()["structuredContent"]["data"]["configuration_id"] == "private-tenant-b"
            principal["actor"] = SimpleNamespace(account_type="service", org_id="tenant-a", agent_registry_id="registry-a")
            allowed[("tenant-a", "registry-a", "product-a", "project-a")] = []
            revoked = await call(args)
            assert revoked.status_code == 404
            assert selected == ["grant-a"] * 7 + ["grant-b"]
    finally:
        tools_l5.l5_tool_catalogue.cache_clear()
        tools_l5.l5_operations.cache_clear()


async def test_tool_unsupported_version_is_not_advertised_or_dispatched(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(CONTRACT.parents[4] / "modules/L5-services/src"))
    with monkeypatch.context() as isolated:
        isolated.setattr(boto3, "client", lambda *args, **kwargs: object())
        from src import l5_integration

    versioned = tmp_path / "contracts/v1"
    versioned.mkdir(parents=True)
    (versioned / "mcp-tools.json").write_bytes(CONTRACT.read_bytes())
    openapi = json.loads((CONTRACT.parent / "openapi.json").read_text())
    openapi["info"]["version"] = "2.0.0"
    (versioned / "openapi.json").write_text(json.dumps(openapi))
    active_contract = {"root": tmp_path}
    monkeypatch.setattr(tools_l5, "files", lambda package: active_contract["root"])

    class NoGrantReads:
        async def scalars(self, statement):
            pytest.fail("Unavailable operations must not look up Product grants")

    def no_backend(*args, **kwargs):
        pytest.fail("Unavailable operations must not instantiate a backend")

    monkeypatch.setattr(l5_integration, "backend", no_backend)
    app = FastAPI()
    app.include_router(l5_integration.router)
    app.dependency_overrides[l5_integration.get_current_user] = lambda: SimpleNamespace(
        account_type="service",
        org_id="tenant-one",
        agent_registry_id="registry-one",
    )
    app.dependency_overrides[l5_integration.get_db] = NoGrantReads
    arguments = {"product_id": "product-one", "project_id": "project-one", "service_id": "security"}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for contract_root, unavailable in (
                (tmp_path, "l5___get_configuration"),
                (CONTRACT.parents[4] / "docs/l5-services", "l5___get_devops_prevention"),
            ):
                active_contract["root"] = contract_root
                tools_l5.l5_tool_catalogue.cache_clear()
                tools_l5.l5_operations.cache_clear()
                listing = await client.post("/v1/l5/mcp/tools/list")
                assert listing.status_code == 200
                assert unavailable not in {tool["name"] for tool in listing.json()["tools"]}
                if contract_root == tmp_path:
                    assert listing.json()["tools"] == []
                response = await client.post(
                    "/v1/l5/mcp/tools/call",
                    json={
                        "tool": unavailable,
                        "arguments": {**arguments, **({"binding_id": "opaque"} if unavailable == "l5___get_devops_prevention" else {})},
                    },
                )
                assert response.status_code == 503
                assert "L5 tool operation unavailable" in response.text
    finally:
        tools_l5.l5_tool_catalogue.cache_clear()
        tools_l5.l5_operations.cache_clear()


DISPATCH_ARGUMENTS = {"product_id": "product-example", "project_id": "project-example", "service_id": "finops"}


@pytest.fixture
def compiled_dispatch_catalogue(monkeypatch):
    monkeypatch.syspath_prepend(str(CONTRACT.parents[4] / "modules/L5-services/src"))
    monkeypatch.setattr(tools_l5, "files", lambda _: Path(__file__).resolve().parents[4] / "docs/l5-services")
    for cache in (tools_l5.l5_tool_catalogue, tools_l5.l5_operations, tools_l5._l5_application):
        cache.cache_clear()
    yield
    for cache in (tools_l5.l5_tool_catalogue, tools_l5.l5_operations, tools_l5._l5_application):
        cache.cache_clear()


async def test_concurrent_calls_keep_their_admitted_backend(compiled_dispatch_catalogue, monkeypatch):
    from l5_services.api.router import create_router

    entered = 0
    both_entered = asyncio.Event()
    built = []

    def build(dependency):
        built.append(dependency)
        return create_router(dependency)

    monkeypatch.setattr("l5_services.api.router.create_router", build)

    class Backend:
        def __init__(self, tenant):
            self.tenant = tenant
            self.calls = []

        async def get(self, product_id, project_id, service_id):
            nonlocal entered
            self.calls.append((product_id, project_id, service_id))
            entered += 1
            if entered == 2:
                both_entered.set()
            await both_entered.wait()
            return {"hash": self.tenant, "tenant_id": self.tenant}

    first, second = Backend("tenant-first"), Backend("tenant-second")
    results = await asyncio.gather(
        tools_l5.dispatch_l5("l5___get_configuration", DISPATCH_ARGUMENTS, first),
        tools_l5.dispatch_l5("l5___get_configuration", DISPATCH_ARGUMENTS, second),
    )
    for backend, result in zip((first, second), results, strict=True):
        assert result["structuredContent"]["data"]["tenant_id"] == backend.tenant
        assert result["structuredContent"]["etag"] == f'"{backend.tenant}"'
        assert backend.calls == [("product-example", "project-example", "finops")]
    assert len(built) == 1


async def test_cancelled_or_failed_call_cannot_supply_the_next_backend(compiled_dispatch_catalogue):
    from l5_services.api.router import create_router

    started = asyncio.Event()

    class Cancelled:
        async def get(self, *args):
            started.set()
            await asyncio.Event().wait()

    pending = asyncio.create_task(tools_l5.dispatch_l5("l5___get_configuration", DISPATCH_ARGUMENTS, Cancelled()))
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    class Denied:
        async def get(self, *args):
            raise HTTPException(403, detail={"code": "forbidden", "private": "must-not-escape"})

    refused = await tools_l5.dispatch_l5("l5___get_configuration", DISPATCH_ARGUMENTS, Denied())
    assert refused["structuredContent"]["http_status"] == 403
    assert "must-not-escape" not in str(refused)

    class Allowed:
        async def get(self, *args):
            return {"hash": "next-tenant", "tenant_id": "next-tenant"}

    accepted = await tools_l5.dispatch_l5("l5___get_configuration", DISPATCH_ARGUMENTS, Allowed())
    assert accepted["structuredContent"]["data"]["tenant_id"] == "next-tenant"

    # Calling the shared router without a transport-bound backend must fail.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=tools_l5._l5_application(create_router)), base_url="http://test") as client:
        response = await client.get("/v1/products/product-example/projects/project-example/services/finops/configuration")
    assert response.status_code == 503
