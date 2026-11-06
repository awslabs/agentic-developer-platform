"""DevOps operations exercised through the authenticated L5 MCP HTTP transport."""

import asyncio
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import boto3
import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException

from src.internal import tools_l5

CONTRACT_ROOT = Path(__file__).resolve().parents[4] / "docs/l5-services"


async def test_mcp_configuration(monkeypatch):
    monkeypatch.syspath_prepend(str(CONTRACT_ROOT.parents[1] / "modules/L5-services/src"))
    with monkeypatch.context() as isolated:
        isolated.setattr(boto3, "client", lambda *args, **kwargs: object())
        from src import l5_integration

    monkeypatch.setattr(tools_l5, "files", lambda package: CONTRACT_ROOT)
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    scopes = {
        ("product-a", "project-one"): "grant-one",
        ("product-b", "project-two"): "grant-two",
    }
    readbacks = {scope: {"configuration_id": f"configuration-{index}", "hash": f"revision-{index}"} for index, scope in enumerate(scopes, start=1)}
    calls = []

    class Grants:
        async def scalars(self, statement):
            values = set(statement.compile().params.values())
            selected = [grant for (product, project), grant in scopes.items() if {"tenant-a", "registry-a", product, project} <= values]
            return SimpleNamespace(all=lambda: selected)

    class Backend:
        def __init__(self, delegation_id):
            self.delegation_id = delegation_id

        def check_scope(self, product_id, project_id, service_id):
            scope = (product_id, project_id)
            assert service_id == "devops"
            assert self.delegation_id == scopes[scope]
            calls.append(scope)
            return scope

        async def get(self, product_id, project_id, service_id):
            return readbacks[self.check_scope(product_id, project_id, service_id)]

        async def options(self, product_id, project_id, service_id):
            scope = self.check_scope(product_id, project_id, service_id)
            return {"project": scope[1]}

        async def list_bindings(self, product_id, project_id, service_id):
            scope = self.check_scope(product_id, project_id, service_id)
            return [
                SimpleNamespace(
                    binding_id="binding-" + scope[1],
                    scope={"product_id": scope[0], "project_id": scope[1], "service_id": service_id},
                    configuration_id=readbacks[scope]["configuration_id"],
                    revision=1,
                    generation=1,
                    state="ready",
                    ownership="adopted",
                    connection_ref={},
                    gaps=[],
                    readiness="ready",
                    native_resource_ref=None,
                    provider_identity=None,
                )
            ]

    def selected_backend(service_id, *, request, db, actor, x_l5_delegation_id, response):
        assert service_id == "devops"
        assert request.app is app
        assert actor.agent_registry_id == "registry-a"
        return Backend(x_l5_delegation_id)

    monkeypatch.setattr(l5_integration, "backend", selected_backend)
    app = FastAPI()
    app.include_router(l5_integration.router)
    app.dependency_overrides[l5_integration.get_current_user] = lambda: SimpleNamespace(
        account_type="service", org_id="tenant-a", agent_registry_id="registry-a"
    )
    app.dependency_overrides[l5_integration.get_db] = Grants
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            listed = await client.post("/v1/l5/mcp/tools/list")
            assert listed.status_code == 200
            tools = {tool["name"]: tool for tool in listed.json()["tools"]}
            for tool in ("l5___get_configuration", "l5___get_options", "l5___list_bindings"):
                assert {"product_id", "project_id", "service_id"} <= set(tools[tool]["inputSchema"]["required"])

            async def call(tool, product_id, project_id):
                return await client.post(
                    "/v1/l5/mcp/tools/call",
                    json={
                        "tool": tool,
                        "arguments": {"product_id": product_id, "project_id": project_id, "service_id": "devops"},
                    },
                )

            for scope in scopes:
                for tool in ("l5___get_configuration", "l5___get_options", "l5___list_bindings"):
                    response = await call(tool, *scope)
                    assert response.status_code == 200
                    result = response.json()
                    assert result["structuredContent"]["http_status"] == 200
                    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
                    data = result["structuredContent"]["data"]
                    if tool == "l5___get_configuration":
                        assert data == readbacks[scope]
                        assert result["structuredContent"]["etag"] == '"' + readbacks[scope]["hash"] + '"'
                    elif tool == "l5___get_options":
                        assert data["project"] == scope[1]
                    else:
                        assert data["items"][0]["scope"]["project_id"] == scope[1]
            assert calls == [scope for scope in scopes for _ in range(3)]
    finally:
        tools_l5.l5_tool_catalogue.cache_clear()
        tools_l5.l5_operations.cache_clear()


async def test_mcp_operations(monkeypatch):
    monkeypatch.syspath_prepend(str(CONTRACT_ROOT.parents[1] / "modules/L5-services/src"))
    with monkeypatch.context() as isolated:
        isolated.setattr(boto3, "client", lambda *args, **kwargs: object())
        from src import l5_integration

    monkeypatch.setattr(tools_l5, "files", lambda package: CONTRACT_ROOT)
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    scope = ("product-b", "project-two", "devops")
    attempts = []

    class Run(dict):
        revision = 1

    run = Run(
        {
            "run_id": "run-one",
            "scope": {"product_id": scope[0], "project_id": scope[1], "service_id": scope[2]},
            "revision": 1,
            "state": "succeeded",
            "origin": "native",
            "native_job_ref": {"record_id": "native-one"},
        }
    )
    result = {"result_id": "result-one", "run_id": "run-one", "revision": "1", "origin": "native"}
    publication = {
        "publication_id": "publication-one",
        "result_id": "result-one",
        "state": "retry_wait",
        "contribution_hash": "a" * 64,
        "invalidation_watermark": 0,
    }
    artifact = {"artifact_id": "artifact-one", "version": "1", "sha256": "b" * 64, "media_type": "application/json", "bytes": 2}

    class Grants:
        async def scalars(self, statement):
            values = set(statement.compile().params.values())
            return SimpleNamespace(all=lambda: ["scope-grant"] if {"tenant-a", "registry-a", *scope[:2]} <= values else [])

    class Store:
        def clock(self):
            from datetime import UTC, datetime

            return datetime(2026, 1, 1, tzinfo=UTC)

        async def get(self, result_id):
            assert result_id == result["result_id"]
            return result

        async def artifact(self, artifact_id):
            assert artifact_id == artifact["artifact_id"]
            return artifact, b"{}"

        async def publication(self, publication_id):
            assert publication_id == publication["publication_id"]
            return SimpleNamespace(revision=1), dict(publication)

        async def retry_publication(self, publication_id, request, idempotency_key):
            await self.publication(publication_id)
            publication["state"] = "pending"
            return dict(publication)

    class Backend:
        def __init__(self, delegation_id):
            assert delegation_id == "scope-grant"

        async def submit(self, product_id, project_id, service_id, run_input, idempotency_key):
            assert (product_id, project_id, service_id) == scope
            assert run_input["configuration_revision"] == 1
            assert run_input["service_id"] == "devops"
            assert idempotency_key == "bounded-run-key-0001"
            if not attempts:
                attempts.append("native-one")
            return run

        async def get_run(self, product_id, project_id, service_id, run_id):
            assert (product_id, project_id, service_id, run_id) == (*scope, "run-one")
            return run

        async def result_store(self, product_id, project_id, service_id):
            assert (product_id, project_id, service_id) == scope
            return Store()

    def selected_backend(service_id, *, request, db, actor, x_l5_delegation_id, response):
        assert service_id == scope[2] and request.app is app
        assert actor.agent_registry_id == "registry-a"
        return Backend(x_l5_delegation_id)

    monkeypatch.setattr(l5_integration, "backend", selected_backend)
    app = FastAPI()
    app.include_router(l5_integration.router)
    app.dependency_overrides[l5_integration.get_current_user] = lambda: SimpleNamespace(
        account_type="service", org_id="tenant-a", agent_registry_id="registry-a"
    )
    app.dependency_overrides[l5_integration.get_db] = Grants
    arguments = {"product_id": scope[0], "project_id": scope[1], "service_id": scope[2]}
    source = {
        "tenant_id": "tenant-a",
        "project_id": scope[1],
        "source_connection_id": "connection-one",
        "record_type": "native_resource",
        "record_id": "resource-one",
        "record_revision": "1",
    }
    run_input = {
        "service_id": "devops",
        "configuration_revision": 1,
        "binding_id": "binding-one",
        "capability": "investigation",
        "devops_input": {"kind": "investigation", "resource_refs": [source], "telemetry_refs": [], "question": "What changed?"},
    }
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            listing = await client.post("/v1/l5/mcp/tools/list")
            assert listing.status_code == 200
            names = {tool["name"] for tool in listing.json()["tools"]}
            assert {
                "l5___submit_run",
                "l5___get_run",
                "l5___get_result",
                "l5___get_artifact_access",
                "l5___get_publication",
                "l5___retry_publication",
            } <= names
            assert "l5___launch_native_browser" not in names

            async def call(tool, **extra):
                response = await client.post("/v1/l5/mcp/tools/call", json={"tool": tool, "arguments": {**arguments, **extra}})
                assert response.status_code == 200, response.text
                payload = response.json()["structuredContent"]
                assert payload == json.loads(response.json()["content"][0]["text"])
                return payload

            for _ in range(2):
                submitted = await call("l5___submit_run", input=run_input, idempotency_key="bounded-run-key-0001")
                assert submitted["data"]["run_id"] == "run-one"
                assert submitted["data"]["native_job_ref"]["record_id"] == "native-one"
                assert submitted["etag"] == '"1"'
            assert attempts == ["native-one"]
            read = await call("l5___get_run", assessment_ref="run-one")
            assert read["data"] == run
            found = await call("l5___get_result", result_id="result-one")
            assert found["data"]["run_id"] == read["data"]["run_id"]
            access = await call("l5___get_artifact_access", artifact_id="artifact-one")
            assert access["data"]["artifact"]["artifact_id"] == "artifact-one"
            assert access["data"]["download_path"].startswith("/v1/products/product-b/projects/project-two/")
            pending = await call("l5___get_publication", publication_id="publication-one")
            assert pending["data"]["result_id"] == found["data"]["result_id"]
            retried = await call(
                "l5___retry_publication",
                publication_id="publication-one",
                input={"reason": "recover Context acknowledgement"},
                idempotency_key="publication-key-0001",
            )
            assert retried["data"]["state"] == "pending"
            assert (await call("l5___get_publication", publication_id="publication-one"))["data"]["state"] == "pending"
            assert attempts == ["native-one"]
    finally:
        tools_l5.l5_tool_catalogue.cache_clear()
        tools_l5.l5_operations.cache_clear()


@pytest.mark.parametrize("denial", ["foreign", "stale", "expired", "forbidden"])
async def test_mcp_denial(monkeypatch, denial):
    monkeypatch.syspath_prepend(str(CONTRACT_ROOT.parents[1] / "modules/L5-services/src"))
    with monkeypatch.context() as isolated:
        isolated.setattr(boto3, "client", lambda *args, **kwargs: object())
        from src import l5_integration

    monkeypatch.setattr(tools_l5, "files", lambda package: CONTRACT_ROOT)
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    calls = []
    protected = "https://signin.example.test/federation?token=private"
    actor = SimpleNamespace(account_type="service", org_id="tenant-a", agent_registry_id="registry-a")

    class Grants:
        async def scalars(self, statement):
            values = set(statement.compile().params.values())
            granted = {"tenant-a", "registry-a", "product-a", "project-a"} <= values
            return SimpleNamespace(all=lambda: ["grant-a"] if granted else [])

    class Backend:
        async def get(self, product_id, project_id, service_id):
            calls.append((product_id, project_id, service_id))
            raise HTTPException(403, detail={"code": "private_grant_id", "launch_url": protected, "retryable": True})

        async def save(self, product_id, project_id, service_id, settings, key, if_none_match, if_match):
            calls.append((product_id, project_id, service_id, key, if_match))
            raise HTTPException(412, detail={"code": "precondition_failed", "launch_url": protected})

        async def submit(self, product_id, project_id, service_id, run_input, key):
            calls.append((product_id, project_id, service_id, key))
            raise HTTPException(410, detail={"code": "source_expired", "launch_url": protected})

    def selected_backend(service_id, *, request, db, actor, x_l5_delegation_id, response):
        assert service_id == "devops" and x_l5_delegation_id == "grant-a"
        assert request.app is app
        return Backend()

    monkeypatch.setattr(l5_integration, "backend", selected_backend)
    app = FastAPI()
    app.include_router(l5_integration.router)
    app.dependency_overrides[l5_integration.get_current_user] = lambda: actor
    app.dependency_overrides[l5_integration.get_db] = Grants
    args = {"product_id": "product-a", "project_id": "project-a", "service_id": "devops"}
    source = {
        "tenant_id": "tenant-a",
        "project_id": "project-a",
        "source_connection_id": "connection-a",
        "record_type": "native_resource",
        "record_id": "resource-a",
        "record_revision": "1",
    }
    run_input = {
        "service_id": "devops",
        "configuration_revision": 1,
        "binding_id": "binding-a",
        "capability": "investigation",
        "devops_input": {"kind": "investigation", "resource_refs": [source], "telemetry_refs": [], "question": "What changed?"},
    }
    requests = {
        "foreign": ("l5___get_configuration", {**args, "project_id": "project-other"}, 404, None),
        "stale": (
            "l5___save_configuration",
            {**args, "input": {}, "idempotency_key": "stale-etag-key-001", "if_match": '"old"'},
            412,
            "precondition_failed",
        ),
        "expired": ("l5___submit_run", {**args, "input": run_input, "idempotency_key": "expired-source-key-001"}, 410, "source_expired"),
        "forbidden": ("l5___get_configuration", args, 403, "forbidden"),
    }
    tool, arguments, status, code = requests[denial]
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            listing = await client.post("/v1/l5/mcp/tools/list")
            assert listing.status_code == 200
            entries = {entry["name"]: entry for entry in listing.json()["tools"]}
            assert "DevOps run retry is unavailable" in entries["l5___retry_run"]["description"]
            assert "no bearer session URL" in entries["l5___get_artifact_access"]["description"]
            assert "l5___launch_native_browser" not in entries
            response = await client.post("/v1/l5/mcp/tools/call", json={"tool": tool, "arguments": arguments})
            if denial == "foreign":
                assert response.status_code == status
                assert not calls
            else:
                assert response.status_code == 200
                result = response.json()
                assert result["isError"] is True
                payload = result["structuredContent"]
                assert json.loads(result["content"][0]["text"]) == payload
                assert payload["http_status"] == status
                assert payload["error"]["code"] == code
                assert payload["error"]["retryable"] is False
                assert payload["error"]["message"] in {"Request failed", "Resource not found"}
                assert UUID(payload["error"]["request_id"]).version == 4
                assert len(calls) == 1
            assert protected not in response.text
            assert "private_grant_id" not in response.text
            assert "token=private" not in response.text
    finally:
        tools_l5.l5_tool_catalogue.cache_clear()
        tools_l5.l5_operations.cache_clear()


def stdio_client(monkeypatch, app, producer):
    monkeypatch.syspath_prepend(str(CONTRACT_ROOT.parents[1] / "modules/gateway/cli"))
    cli_path = CONTRACT_ROOT.parents[1] / "modules/gateway/cli/adp-mcp.py"
    spec = importlib.util.spec_from_file_location("l5_mcp_devops_transport", cli_path)
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    monkeypatch.setattr(bridge.NativeTransport, "configured", lambda: None)
    monkeypatch.setattr(bridge.native_handoff, "ready", lambda: {"result": {"tools": []}})
    monkeypatch.setattr(bridge.native_handoff, "descriptors", lambda tools: [])

    def gateway_context():
        identity = producer["current"]
        return {
            "url": "https://gateway.example.test/api",
            "headers": {"Authorization": "Bearer " + identity},
            "binding": {
                "mode": "bearer",
                "gateway": "https://gateway.example.test/api",
                "identity": identity,
                "tenant": "tenant-a",
                "deployment_id": "offline",
            },
            "proxy": False,
        }

    monkeypatch.setattr(bridge, "gateway_context", gateway_context)

    async def dispatch(request):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.local") as client:
            return await client.post(
                request.full_url.removeprefix("https://gateway.example.test/api"), content=request.data, headers=dict(request.header_items())
            )

    def open_request(request, timeout):
        assert timeout == 30
        if request.full_url.endswith("/agent/tools"):
            raise bridge.HTTPError(request.full_url, 403, "not permitted", {}, None)
        response = asyncio.run(dispatch(request))
        if response.status_code >= 400:
            raise bridge.HTTPError(request.full_url, response.status_code, "denied", {}, None)
        return io.BytesIO(response.content)

    monkeypatch.setattr(bridge, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    return bridge


async def test_mcp_configuration_stdio_identities(monkeypatch):
    monkeypatch.syspath_prepend(str(CONTRACT_ROOT.parents[1] / "modules/L5-services/src"))
    monkeypatch.syspath_prepend(str(CONTRACT_ROOT.parents[1] / "modules/gateway/cli"))
    with monkeypatch.context() as isolated:
        isolated.setattr(boto3, "client", lambda *args, **kwargs: object())
        from src import l5_integration

    monkeypatch.setattr(tools_l5, "files", lambda package: CONTRACT_ROOT)
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    identities = {
        "producer-one": ("registry-one", "product-one", "project-one", "grant-one"),
        "producer-two": ("registry-two", "product-two", "project-two", "grant-two"),
    }
    producer = {"current": "producer-one"}
    selected = []

    def authenticate(authorization: str | None = Header(None)):
        identity = authorization.removeprefix("Bearer ") if authorization else ""
        if identity not in identities:
            raise HTTPException(401)
        return SimpleNamespace(account_type="service", org_id="tenant-a", agent_registry_id=identities[identity][0])

    class Grants:
        async def scalars(self, statement):
            values = set(statement.compile().params.values())
            matching = [grant for registry, product, project, grant in identities.values() if {"tenant-a", registry, product, project} <= values]
            return SimpleNamespace(all=lambda: matching)

    class Backend:
        def __init__(self, actor, delegation_id):
            self.actor = actor
            self.delegation_id = delegation_id

        async def get(self, product_id, project_id, service_id):
            assert service_id == "devops"
            identity = next((item for item in identities.values() if item[0] == self.actor.agent_registry_id), None)
            assert identity is not None
            assert (product_id, project_id, self.delegation_id) == identity[1:]
            selected.append((self.actor.agent_registry_id, product_id, project_id))
            return {"configuration_id": product_id + ":" + project_id, "hash": self.delegation_id}

    def selected_backend(service_id, *, request, db, actor, x_l5_delegation_id, response):
        assert service_id == "devops" and request.app is app
        return Backend(actor, x_l5_delegation_id)

    monkeypatch.setattr(l5_integration, "backend", selected_backend)
    app = FastAPI()
    app.include_router(l5_integration.router)
    app.dependency_overrides[l5_integration.get_current_user] = authenticate
    app.dependency_overrides[l5_integration.get_db] = Grants

    bridge = stdio_client(monkeypatch, app, producer)
    try:
        for identity in identities:
            producer["current"] = identity
            registry, product, project, grant = identities[identity]
            listing = await asyncio.to_thread(bridge.handle, {"method": "tools/list"})
            config_tool = next(tool for tool in listing["tools"] if tool["name"] == "l5___get_configuration")
            assert {"product_id", "project_id", "service_id"} <= set(config_tool["inputSchema"]["required"])
            result = await asyncio.to_thread(
                bridge.handle,
                {
                    "method": "tools/call",
                    "params": {"name": "l5___get_configuration", "arguments": {"product_id": product, "project_id": project, "service_id": "devops"}},
                },
            )
            assert result["structuredContent"]["data"]["configuration_id"] == product + ":" + project
            assert result["structuredContent"]["etag"] == '"' + grant + '"'
            if identity == "producer-two":
                with pytest.raises(bridge.GatewayHTTPError) as denied:
                    await asyncio.to_thread(
                        bridge.handle,
                        {
                            "method": "tools/call",
                            "params": {
                                "name": "l5___get_configuration",
                                "arguments": {"product_id": "product-one", "project_id": "project-one", "service_id": "devops"},
                            },
                        },
                    )
                assert denied.value.status == 404
        assert selected == [(item[0], item[1], item[2]) for item in identities.values()]
    finally:
        tools_l5.l5_tool_catalogue.cache_clear()
        tools_l5.l5_operations.cache_clear()


async def test_mcp_operations_stdio_identity(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(CONTRACT_ROOT.parents[1] / "modules/L5-services/src"))
    with monkeypatch.context() as isolated:
        isolated.setattr(boto3, "client", lambda *args, **kwargs: object())
        from src import l5_integration

    monkeypatch.setattr(tools_l5, "files", lambda package: CONTRACT_ROOT)
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    monkeypatch.setenv("BG_CONFIG_DIR", str(tmp_path))
    producer = {"current": "producer-one"}
    submissions = []
    scope = {"product_id": "product-one", "project_id": "project-one", "service_id": "devops"}
    source = {
        "tenant_id": "tenant-one",
        "project_id": "project-one",
        "source_connection_id": "connection-one",
        "record_type": "native_resource",
        "record_id": "resource-one",
        "record_revision": "1",
    }
    run_input = {
        "service_id": "devops",
        "configuration_revision": 1,
        "binding_id": "binding-one",
        "capability": "investigation",
        "devops_input": {"kind": "investigation", "resource_refs": [source], "telemetry_refs": [], "question": "What changed?"},
    }

    class Run(dict):
        revision = 1

    run = Run(
        {"run_id": "run-one", "scope": scope, "revision": 1, "state": "succeeded", "origin": "native", "native_job_ref": {"record_id": "native-one"}}
    )
    result = {"result_id": "result-one", "run_id": "run-one", "revision": "1", "origin": "native"}
    publication = {
        "publication_id": "publication-one",
        "result_id": "result-one",
        "state": "retry_wait",
        "contribution_hash": "a" * 64,
        "invalidation_watermark": 0,
    }

    def authenticate(authorization: str | None = Header(None)):
        identity = authorization.removeprefix("Bearer ") if authorization else ""
        if identity not in {"producer-one", "producer-two"}:
            raise HTTPException(401)
        return SimpleNamespace(account_type="service", org_id="tenant-one", agent_registry_id="registry-" + identity.removeprefix("producer-"))

    class Grants:
        async def scalars(self, statement):
            values = set(statement.compile().params.values())
            granted = {"tenant-one", "registry-one", "product-one", "project-one"} <= values
            return SimpleNamespace(all=lambda: ["grant-one"] if granted else [])

    class Store:
        async def get(self, result_id):
            assert result_id == result["result_id"]
            return result

        async def publication(self, publication_id):
            assert publication_id == publication["publication_id"]
            return SimpleNamespace(revision=1), dict(publication)

        async def retry_publication(self, publication_id, request, idempotency_key):
            await self.publication(publication_id)
            publication["state"] = "pending"
            return dict(publication)

    class Backend:
        async def submit(self, product_id, project_id, service_id, value, key):
            assert (product_id, project_id, service_id) == tuple(scope.values())
            assert value == run_input and key == "bounded-run-key-0001"
            if not submissions:
                submissions.append("native-one")
            return run

        async def get_run(self, product_id, project_id, service_id, run_id):
            assert (product_id, project_id, service_id, run_id) == (*scope.values(), "run-one")
            return run

        async def result_store(self, product_id, project_id, service_id):
            assert (product_id, project_id, service_id) == tuple(scope.values())
            return Store()

    def selected_backend(service_id, *, request, db, actor, x_l5_delegation_id, response):
        assert request.app is app and service_id == "devops"
        assert actor.agent_registry_id == "registry-one" and x_l5_delegation_id == "grant-one"
        return Backend()

    monkeypatch.setattr(l5_integration, "backend", selected_backend)
    app = FastAPI()
    app.include_router(l5_integration.router)
    app.dependency_overrides[l5_integration.get_current_user] = authenticate
    app.dependency_overrides[l5_integration.get_db] = Grants
    bridge = stdio_client(monkeypatch, app, producer)

    async def call(tool, **arguments):
        return await asyncio.to_thread(bridge.handle, {"method": "tools/call", "params": {"name": tool, "arguments": arguments}})

    try:
        listing = await asyncio.to_thread(bridge.handle, {"method": "tools/list"})
        assert {"l5___submit_run", "l5___get_run", "l5___get_result", "l5___get_publication", "l5___retry_publication"} <= {
            tool["name"] for tool in listing["tools"]
        }
        submitted = await call("l5___submit_run", **scope, input=run_input, idempotency_key="bounded-run-key-0001")
        assert submitted["structuredContent"]["data"]["run_id"] == "run-one"
        assert submitted["structuredContent"]["data"]["native_job_ref"]["record_id"] == "native-one"
        assert submissions == ["native-one"]
        original = next(path.stem for path in (tmp_path / "tool-operations").glob("*.json"))
        producer["current"] = "producer-two"
        denied = await call("adp___operation_status", operation_ref=original)
        assert denied["isError"] is True
        assert "Original login" in denied["content"][0]["text"]
        with pytest.raises(bridge.GatewayHTTPError) as foreign:
            await call("l5___get_result", **scope, result_id="result-one")
        assert foreign.value.status == 404
        assert submissions == ["native-one"]
        producer["current"] = "producer-one"
        read = await call("adp___operation_status", operation_ref=original)
        assert read["structuredContent"]["data"]["run_id"] == "run-one"
        replay = await call("l5___submit_run", **scope, input=run_input, idempotency_key="bounded-run-key-0001")
        assert replay["structuredContent"]["data"]["run_id"] == "run-one"
        assert submissions == ["native-one"]
        outcome = await call("l5___get_result", **scope, result_id="result-one")
        assert outcome["structuredContent"]["data"]["run_id"] == read["structuredContent"]["data"]["run_id"]
        status = await call("l5___get_publication", **scope, publication_id="publication-one")
        assert status["structuredContent"]["data"]["result_id"] == outcome["structuredContent"]["data"]["result_id"]
        retried = await call(
            "l5___retry_publication",
            **scope,
            publication_id="publication-one",
            input={"reason": "recover Context receipt"},
            idempotency_key="publication-key-0001",
        )
        assert retried["structuredContent"]["data"]["state"] == "pending"
        assert submissions == ["native-one"]
    finally:
        tools_l5.l5_tool_catalogue.cache_clear()
        tools_l5.l5_operations.cache_clear()


@pytest.mark.parametrize("denial", ["foreign", "stale", "expired", "forbidden"])
async def test_mcp_denial_stdio_identity(monkeypatch, tmp_path, denial):
    monkeypatch.syspath_prepend(str(CONTRACT_ROOT.parents[1] / "modules/L5-services/src"))
    with monkeypatch.context() as isolated:
        isolated.setattr(boto3, "client", lambda *args, **kwargs: object())
        from src import l5_integration

    monkeypatch.setattr(tools_l5, "files", lambda package: CONTRACT_ROOT)
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    monkeypatch.setenv("BG_CONFIG_DIR", str(tmp_path))
    producer = {"current": "producer-two" if denial == "foreign" else "producer-one"}
    protected = "https://signin.example.test/federation?token=private"
    calls = []
    scope = {"product_id": "product-one", "project_id": "project-one", "service_id": "devops"}

    def authenticate(authorization: str | None = Header(None)):
        identity = authorization.removeprefix("Bearer ") if authorization else ""
        if identity not in {"producer-one", "producer-two"}:
            raise HTTPException(401)
        return SimpleNamespace(account_type="service", org_id="tenant-one", agent_registry_id="registry-" + identity.removeprefix("producer-"))

    class Grants:
        async def scalars(self, statement):
            values = set(statement.compile().params.values())
            permitted = {"tenant-one", "registry-one", "product-one", "project-one"} <= values
            return SimpleNamespace(all=lambda: ["grant-one"] if permitted else [])

    class Backend:
        async def get(self, product_id, project_id, service_id):
            calls.append("get")
            raise HTTPException(403, detail={"code": "private_credential", "launch_url": protected, "retryable": True})

        async def save(self, product_id, project_id, service_id, settings, key, if_none_match, if_match):
            assert (product_id, project_id, service_id) == tuple(scope.values())
            assert if_match == '"old"' and key == "stale-etag-key-001"
            calls.append("save")
            raise HTTPException(412, detail={"code": "precondition_failed", "launch_url": protected})

        async def submit(self, product_id, project_id, service_id, run_input, key):
            assert (product_id, project_id, service_id) == tuple(scope.values())
            assert run_input["service_id"] == "devops" and key == "expired-source-key-001"
            calls.append("submit")
            raise HTTPException(410, detail={"code": "source_expired", "launch_url": protected})

    def selected_backend(service_id, *, request, db, actor, x_l5_delegation_id, response):
        assert service_id == "devops" and request.app is app
        assert actor.agent_registry_id == "registry-one" and x_l5_delegation_id == "grant-one"
        return Backend()

    monkeypatch.setattr(l5_integration, "backend", selected_backend)
    app = FastAPI()
    app.include_router(l5_integration.router)
    app.dependency_overrides[l5_integration.get_current_user] = authenticate
    app.dependency_overrides[l5_integration.get_db] = Grants
    bridge = stdio_client(monkeypatch, app, producer)
    source = {
        "tenant_id": "tenant-one",
        "project_id": "project-one",
        "source_connection_id": "connection-one",
        "record_type": "native_resource",
        "record_id": "resource-one",
        "record_revision": "1",
    }
    run_input = {
        "service_id": "devops",
        "configuration_revision": 1,
        "binding_id": "binding-one",
        "capability": "investigation",
        "devops_input": {"kind": "investigation", "resource_refs": [source], "telemetry_refs": [], "question": "What changed?"},
    }
    requests = {
        "foreign": ("l5___get_configuration", scope, None),
        "stale": (
            "l5___save_configuration",
            {**scope, "input": {}, "idempotency_key": "stale-etag-key-001", "if_match": '"old"'},
            (412, "precondition_failed"),
        ),
        "expired": ("l5___submit_run", {**scope, "input": run_input, "idempotency_key": "expired-source-key-001"}, (410, "source_expired")),
        "forbidden": ("l5___get_configuration", scope, (403, "forbidden")),
    }
    tool, arguments, expected = requests[denial]
    try:
        listing = await asyncio.to_thread(bridge.handle, {"method": "tools/list"})
        assert tool in {entry["name"] for entry in listing["tools"]}
        if denial == "foreign":
            with pytest.raises(bridge.GatewayHTTPError) as refused:
                await asyncio.to_thread(bridge.handle, {"method": "tools/call", "params": {"name": tool, "arguments": arguments}})
            assert refused.value.status == 404
            assert not calls
        else:
            result = await asyncio.to_thread(bridge.handle, {"method": "tools/call", "params": {"name": tool, "arguments": arguments}})
            assert result["isError"] is True
            payload = result["structuredContent"]
            assert (payload["http_status"], payload["error"]["code"]) == expected
            assert json.loads(result["content"][0]["text"]) == payload
            assert payload["error"]["retryable"] is False
            assert UUID(payload["error"]["request_id"]).version == 4
            assert calls == [{"stale": "save", "expired": "submit", "forbidden": "get"}[denial]]
            assert protected not in json.dumps(result) and "private_credential" not in json.dumps(result)
    finally:
        tools_l5.l5_tool_catalogue.cache_clear()
        tools_l5.l5_operations.cache_clear()
