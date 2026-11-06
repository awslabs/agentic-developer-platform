"""Authenticated MCP review tools use the existing scoped L5 API."""

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import boto3
import httpx
import pytest
from fastapi import FastAPI, HTTPException

from src.internal import tools_l5

CONTRACT_ROOT = Path(__file__).resolve().parents[4] / "docs/l5-services"
SCOPE = {"product_id": "product-example", "project_id": "project-example", "service_id": "well_architected"}
VIEW_SCOPE = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example", "relationship_revision": "1"}
SOURCE = {
    "tenant_id": "tenant-example",
    "project_id": "project-example",
    "source_connection_id": "source-example",
    "record_type": "document",
    "record_id": "design-example",
    "record_revision": "1",
}
CONFIGURATION = {
    "configuration_id": "configuration-example",
    "scope": VIEW_SCOPE,
    "revision": 1,
    "hash": "hash-example",
    "state": "ready",
    "settings": {"service_id": "well_architected", "source_refs": [SOURCE], "capabilities": ["architecture_review"]},
}
RUN_INPUT = {
    "service_id": "well_architected",
    "capability": "architecture_review",
    "configuration_revision": 1,
    "binding_id": "binding-example",
    "sources": [SOURCE],
}
RESULT = {
    "service_id": "well_architected",
    "scope": VIEW_SCOPE,
    "result_id": "result-example",
    "run_id": "run-example",
    "revision": 2,
    "coverage": "partial",
    "limitations": ["source_only"],
}


class Run(dict):
    @property
    def revision(self):
        return self["revision"]


@pytest.mark.asyncio
async def test_review_tools_share_authenticated_scope_receipts_and_results(monkeypatch):
    monkeypatch.syspath_prepend(str(CONTRACT_ROOT.parents[1] / "modules/L5-services/src"))
    with monkeypatch.context() as isolated:
        isolated.setattr(boto3, "client", lambda *args, **kwargs: object())
        from src import l5_integration

    monkeypatch.setattr(tools_l5, "files", lambda package: CONTRACT_ROOT)
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    calls = []

    class Grants:
        async def scalars(self, statement):
            values = set(statement.compile().params.values())
            allowed = {"tenant-example", "registry-example", SCOPE["product_id"], SCOPE["project_id"]} <= values
            return SimpleNamespace(all=lambda: ["grant-example"] if allowed else [])

    class Results:
        wa_reader_ready = True
        service_id = "well_architected"
        runs = {
            "run-example": SimpleNamespace(
                tenant_id="tenant-example", product_id=SCOPE["product_id"], project_id=SCOPE["project_id"], service_id="well_architected"
            )
        }

        async def get(self, result_id):
            if result_id != "result-example":
                raise HTTPException(404, detail={"code": "not_found"})
            return RESULT

        async def well_architected_publication_status(self, result_id):
            if result_id != "result-example":
                raise HTTPException(404, detail={"code": "not_found"})
            return {
                "result_id": result_id,
                "revision": 2,
                "consumers": [
                    {
                        "destination": "product-context",
                        "state": "unavailable",
                        "projection_verified_on_ack": False,
                        "last_error": "consumer_schema_unadmitted",
                    }
                ],
            }

        async def list(self, cursor=None):
            return [RESULT]

    class Backend:
        async def options(self, product_id, project_id, service_id):
            calls.append(("options", product_id, project_id, service_id))
            return {"capabilities": ["architecture_review"], "unavailable_reasons": []}

        async def get(self, product_id, project_id, service_id):
            calls.append(("get", product_id, project_id, service_id))
            return CONFIGURATION

        async def save(self, product_id, project_id, service_id, choices, key, if_none_match, if_match):
            calls.append(("save", choices, key, if_none_match, if_match))
            return CONFIGURATION

        async def check(self, product_id, project_id, service_id):
            calls.append(("check", product_id, project_id, service_id))
            return {"ready": False, "gaps": ["binding_not_ready"]}

        async def submit(self, product_id, project_id, service_id, run_input, key):
            calls.append(("submit", run_input, key))
            return Run(run_id="run-example", scope=VIEW_SCOPE, revision=1, state="admitted", provider_state="not_started")

        async def get_run(self, product_id, project_id, service_id, run_id):
            calls.append(("get_run", run_id))
            return Run(run_id=run_id, scope=VIEW_SCOPE, revision=1, state="admitted", provider_state="not_started")

        async def result_store(self, product_id, project_id, service_id):
            calls.append(("result_store", product_id, project_id, service_id))
            return Results()

    def selected_backend(service_id, *, request, db, actor, x_l5_delegation_id, response):
        assert service_id == "well_architected" and x_l5_delegation_id == "grant-example"
        assert actor.account_type == "service" and actor.org_id == "tenant-example"
        return Backend()

    monkeypatch.setattr(l5_integration, "backend", selected_backend)
    app = FastAPI()
    app.include_router(l5_integration.router)
    app.dependency_overrides[l5_integration.get_current_user] = lambda: SimpleNamespace(
        account_type="service", org_id="tenant-example", agent_registry_id="registry-example"
    )
    app.dependency_overrides[l5_integration.get_db] = Grants

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            listed = await client.post("/v1/l5/mcp/tools/list")
            names = {tool["name"]: tool for tool in listed.json()["tools"]}
            assert listed.status_code == 200
            for name in (
                "get_options",
                "get_configuration",
                "save_configuration",
                "check_configuration",
                "submit_run",
                "get_run",
                "get_result",
                "get_result_publication_status",
            ):
                schema = names["l5___" + name]["inputSchema"]
                assert schema["additionalProperties"] is False
                assert "service_id" in schema["required"]

            async def invoke(name, **extra):
                return await client.post("/v1/l5/mcp/tools/call", json={"tool": "l5___" + name, "arguments": {**SCOPE, **extra}})

            options = await invoke("get_options")
            assert options.status_code == 200 and options.json()["structuredContent"]["data"]["capabilities"] == ["architecture_review"]
            config = await invoke("get_configuration")
            assert config.json()["structuredContent"]["etag"] == '"hash-example"'
            assert config.json()["structuredContent"]["data"] == CONFIGURATION
            saved = await invoke(
                "save_configuration", input={"service_id": "well_architected"}, if_match='"hash-example"', idempotency_key="review-save-12345678"
            )
            assert saved.status_code == 200 and saved.json()["structuredContent"]["data"] == CONFIGURATION
            checked = await invoke("check_configuration")
            assert checked.status_code == 200 and checked.json()["structuredContent"]["data"]["gaps"] == ["binding_not_ready"]
            submitted = await invoke("submit_run", input=RUN_INPUT, idempotency_key="review-run-12345678")
            assert submitted.status_code == 200
            assert submitted.json()["structuredContent"]["http_status"] == 202
            assert submitted.json()["structuredContent"]["data"]["state"] == "admitted"
            run = await invoke("get_run", assessment_ref="run-example")
            assert run.json()["structuredContent"]["data"]["run_id"] == "run-example"
            result = await invoke("get_result", result_id="result-example")
            assert result.json()["structuredContent"]["data"] == RESULT
            assert result.json()["structuredContent"]["etag"] == '"2"'
            publication = await invoke("get_result_publication_status", result_id="result-example")
            assert publication.status_code == 200
            payload = publication.json()["structuredContent"]
            assert payload["http_status"] == 200 and payload["data"]["revision"] == 2
            assert payload["data"]["consumers"][0]["state"] == "unavailable"
            assert payload["data"]["consumers"][0]["projection_verified_on_ack"] is False
            missing = await invoke("get_result_publication_status", result_id="unknown-result")
            assert missing.status_code == 200 and missing.json()["isError"] is True
            assert missing.json()["structuredContent"]["error"]["code"] == "not_found"
            assert missing.json()["structuredContent"]["http_status"] == 404
            assert ("save", {"service_id": "well_architected"}, "review-save-12345678", None, '"hash-example"') in calls
            assert ("submit", RUN_INPUT, "review-run-12345678") in calls

            denied = await client.post(
                "/v1/l5/mcp/tools/call", json={"tool": "l5___get_configuration", "arguments": {**SCOPE, "project_id": "foreign-project"}}
            )

            denied_receipt = await invoke("get_result_publication_status", result_id="result-example", project_id="foreign-project")
            assert denied_receipt.status_code == 404 and denied_receipt.json() == {"detail": {"code": "not_found"}}
            assert denied.status_code == 404 and denied.json() == {"detail": {"code": "not_found"}}
            assert len(calls) == 9
    finally:
        tools_l5.l5_tool_catalogue.cache_clear()
        tools_l5.l5_operations.cache_clear()


def test_installed_stdio_bridge_advertises_and_forwards_review_result(monkeypatch):
    cli_root = CONTRACT_ROOT.parents[1] / "modules/gateway/cli"
    monkeypatch.syspath_prepend(str(cli_root))
    spec = importlib.util.spec_from_file_location("well_architected_mcp_bridge", cli_root / "adp-mcp.py")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    monkeypatch.setattr(bridge.native_handoff, "ready", lambda: {"result": {"tools": []}})
    monkeypatch.setattr(bridge.native_handoff, "descriptors", lambda tools: [])
    calls = []
    payload = {"http_status": 200, "etag": '"2"', "data": RESULT}

    def gateway_request(path, body=None):
        calls.append((path, body))
        if path == "/v1/l5/mcp/tools/list":
            return {
                "tools": [
                    {
                        "name": "l5___get_result",
                        "inputSchema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "product_id": {"type": "string"},
                                "project_id": {"type": "string"},
                                "service_id": {"type": "string"},
                                "result_id": {"type": "string"},
                            },
                            "required": ["product_id", "project_id", "service_id", "result_id"],
                        },
                    }
                ]
            }
        if path == "/agent/tools":
            return {"tools": []}
        if path == "/v1/l5/mcp/tools/call":
            return {"structuredContent": payload, "content": [{"type": "text", "text": json.dumps(payload)}]}
        raise AssertionError(path)

    monkeypatch.setattr(bridge, "gateway_request", gateway_request)
    listing = bridge.handle({"method": "tools/list"})
    assert [tool["name"] for tool in listing["tools"] if tool["name"].startswith("l5___")] == ["l5___get_result"]
    response = bridge.handle({"method": "tools/call", "params": {"name": "l5___get_result", "arguments": {**SCOPE, "result_id": "result-example"}}})
    assert response["structuredContent"] == payload
    assert calls == [
        ("/v1/l5/mcp/tools/list", {}),
        ("/agent/tools", None),
        ("/v1/l5/mcp/tools/call", {"tool": "l5___get_result", "arguments": {**SCOPE, "result_id": "result-example"}}),
    ]


async def shared_client_review_journey(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(CONTRACT_ROOT.parents[1] / "modules/L5-services/src"))
    from l5_services.api.router import create_router

    fixture_path = CONTRACT_ROOT.parents[1] / "modules/gateway/frontend/src/__tests__/fixtures/well-architected-client-parity.json"
    fixture = json.loads(fixture_path.read_text())
    monkeypatch.setattr(tools_l5, "files", lambda package: CONTRACT_ROOT)
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    scope = {"product_id": fixture["scope"]["product_id"], "project_id": fixture["scope"]["project_id"], "service_id": "well_architected"}
    root = f"/v1/products/{scope['product_id']}/projects/{scope['project_id']}/services/well_architected"
    request_keys = []

    class ResultStore:
        wa_reader_ready = True
        service_id = "well_architected"
        runs = {
            fixture["run"]["run_id"]: SimpleNamespace(
                tenant_id=fixture["scope"]["tenant_id"], product_id=scope["product_id"], project_id=scope["project_id"], service_id="well_architected"
            )
        }

        async def get(self, result_id):
            assert result_id == fixture["result"]["result_id"]
            return fixture["result"]

        async def list(self, cursor=None):
            return [fixture["result"]]

    class Backend:
        async def get(self, product_id, project_id, service_id):
            assert (product_id, project_id, service_id) == tuple(scope.values())
            return fixture["configuration"]

        async def save(self, product_id, project_id, service_id, choices, key, if_none_match, if_match):
            assert (product_id, project_id, service_id) == tuple(scope.values())
            assert if_none_match is None and if_match == '"hash-one"' and choices == {"service_id": "well_architected"}
            request_keys.append(("save", key))
            return fixture["configuration"]

        async def start_binding(self, product_id, project_id, service_id, body, key, if_match):
            assert (product_id, project_id, service_id) == tuple(scope.values())
            assert body == {"action": "create", "configuration_revision": 1} and if_match is None
            request_keys.append(("setup", key))
            return SimpleNamespace(**fixture["setup"], readback_ref=None, cleanup_receipt=None)

        async def get_binding_operation(self, product_id, project_id, service_id, operation_id):
            assert operation_id == fixture["setup"]["operation_id"]
            return SimpleNamespace(**fixture["setup"], readback_ref=None, cleanup_receipt=None)

        async def submit(self, product_id, project_id, service_id, run_input, key):
            assert (product_id, project_id, service_id) == tuple(scope.values())
            assert run_input == fixture["run_input"]
            request_keys.append(("run", key))
            return Run(fixture["run"])

        async def get_run(self, product_id, project_id, service_id, run_id):
            assert (product_id, project_id, service_id, run_id) == (*scope.values(), fixture["run"]["run_id"])
            return Run(fixture["run"])

        async def list_runs(self, product_id, project_id, service_id, cursor=None):
            return []

        async def result_store(self, product_id, project_id, service_id):
            return ResultStore()

    backend = Backend()
    app = FastAPI()
    app.include_router(create_router(lambda: backend))

    async def request(method, path, body=None, headers=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://review.test", trust_env=False) as client:
            return await client.request(method, path, json=body, headers=headers)

    cli_path = CONTRACT_ROOT.parents[1] / "modules/gateway/cli/adp-service.py"
    spec = importlib.util.spec_from_file_location("well_architected_shared_cli", cli_path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    class CliClient:
        def request(self, method, path, body=None, **options):
            response = asyncio.run(request(method, path, body, options.get("extra_headers")))
            assert response.status_code < 400
            return response.json()

    async def cli_call(*arguments):
        scope_args = ["--product", scope["product_id"], "--project", scope["project_id"], "--service", scope["service_id"], "--json"]
        parsed = cli.parser().parse_args([*arguments, *scope_args])
        return await asyncio.to_thread(cli.execute, parsed, CliClient())

    try:
        config = await request("GET", root + "/configuration")
        assert config.json() == fixture["configuration"] and config.headers["etag"] == '"hash-one"'
        assert (await cli_call("config", "show"))["detail"] == config.json()
        mcp_config = await tools_l5.dispatch_l5("l5___get_configuration", scope, backend)
        assert mcp_config["structuredContent"]["data"] == config.json()
        assert mcp_config["structuredContent"]["etag"] == config.headers["etag"]

        choices = tmp_path / "choices.json"
        choices.write_text(json.dumps({"service_id": "well_architected"}))
        saved = await cli_call("config", "update", "--input", str(choices), "--if-match", '"hash-one"', "--idempotency-key", fixture["save_key"])
        mcp_save = await tools_l5.dispatch_l5(
            "l5___save_configuration",
            {**scope, "input": {"service_id": "well_architected"}, "if_match": '"hash-one"', "idempotency_key": fixture["save_key"]},
            backend,
        )
        assert saved["detail"] == mcp_save["structuredContent"]["data"] == config.json()
        assert mcp_save["structuredContent"]["etag"] == config.headers["etag"]

        setup_input = {"action": "create", "configuration_revision": fixture["configuration"]["revision"]}
        setup_file = tmp_path / "setup.json"
        setup_file.write_text(json.dumps(setup_input))
        cli_setup = await cli_call("binding", "create", "--input", str(setup_file), "--idempotency-key", fixture["setup_key"])
        mcp_setup = await tools_l5.dispatch_l5(
            "l5___start_binding_operation", {**scope, "input": setup_input, "idempotency_key": fixture["setup_key"]}, backend
        )
        direct_setup = await request("GET", root + "/binding-operations/setup-one")
        assert cli_setup["detail"] == mcp_setup["structuredContent"]["data"] == direct_setup.json() == fixture["setup"]
        assert mcp_setup["structuredContent"]["http_status"] == 202
        assert mcp_setup["structuredContent"]["etag"] == '"1"'
        assert (await cli_call("binding", "status", "--operation-id", "setup-one"))["detail"] == direct_setup.json()

        run_input = tmp_path / "run.json"
        run_input.write_text(json.dumps(fixture["run_input"]))
        accepted = await cli_call("run", "--input", str(run_input), "--idempotency-key", fixture["run_key"])
        mcp_run = await tools_l5.dispatch_l5(
            "l5___submit_run", {**scope, "input": fixture["run_input"], "idempotency_key": fixture["run_key"]}, backend
        )
        assert accepted["status"] == "accepted"
        assert accepted["detail"] == mcp_run["structuredContent"]["data"] == fixture["run"]
        assert mcp_run["structuredContent"]["http_status"] == 202
        assert mcp_run["structuredContent"]["etag"] == '"2"'
        assert request_keys == [(kind, fixture[kind + "_key"]) for kind in ("save", "setup", "run") for _ in range(2)]
        direct_run = await request("GET", root + "/runs/run-one")
        cli_run = await cli_call("status", "--run-id", "run-one")
        mcp_read = await tools_l5.dispatch_l5("l5___get_run", {**scope, "assessment_ref": "run-one"}, backend)
        assert cli_run["detail"] == mcp_read["structuredContent"]["data"] == direct_run.json() == fixture["run"]
        assert mcp_read["structuredContent"]["etag"] == direct_run.headers["etag"] == '"2"'
        assert (await request("GET", root + "/runs")).json()["items"] == []
        assert (await request("GET", root + "/results")).json()["items"] == [fixture["result"]]
        direct_result = await request("GET", root + "/results/result-one")
        cli_result = await cli_call("result", "show", "--result-id", "result-one")
        mcp_result = await tools_l5.dispatch_l5("l5___get_result", {**scope, "result_id": "result-one"}, backend)
        assert cli_result["detail"] == mcp_result["structuredContent"]["data"] == direct_result.json() == fixture["result"]
        assert mcp_result["structuredContent"]["etag"] == direct_result.headers["etag"] == '"3"'
    finally:
        tools_l5.l5_tool_catalogue.cache_clear()
        tools_l5.l5_operations.cache_clear()


@pytest.mark.asyncio
async def test_review_clients_read_same_api_revisions_and_replay_receipt(monkeypatch, tmp_path):
    await shared_client_review_journey(monkeypatch, tmp_path)


@pytest.mark.asyncio
async def test_foreign_project_review_tools_refuse_read_and_mutation_before_backend(monkeypatch):
    fixture_path = CONTRACT_ROOT.parents[1] / "modules/gateway/frontend/src/__tests__/fixtures/well-architected-client-parity.json"
    fixture = json.loads(fixture_path.read_text())
    monkeypatch.syspath_prepend(str(CONTRACT_ROOT.parents[1] / "modules/L5-services/src"))
    with monkeypatch.context() as isolated:
        isolated.setattr(boto3, "client", lambda *args, **kwargs: object())
        from src import l5_integration

    monkeypatch.setattr(tools_l5, "files", lambda package: CONTRACT_ROOT)
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()

    class Grants:
        async def scalars(self, statement):
            assert fixture["foreign_project_id"] in statement.compile().params.values()
            return SimpleNamespace(all=lambda: [])

    def forbidden_backend(*args, **kwargs):
        raise AssertionError("Foreign-project requests must not reach the L5 backend")

    monkeypatch.setattr(l5_integration, "backend", forbidden_backend)
    app = FastAPI()
    app.include_router(l5_integration.router)
    app.dependency_overrides[l5_integration.get_current_user] = lambda: SimpleNamespace(
        account_type="service", org_id=fixture["scope"]["tenant_id"], agent_registry_id="registry-example"
    )
    app.dependency_overrides[l5_integration.get_db] = Grants
    scope = {"product_id": fixture["scope"]["product_id"], "project_id": fixture["foreign_project_id"], "service_id": "well_architected"}
    operations = [
        ("get_configuration", {}),
        ("save_configuration", {"input": {"service_id": "well_architected"}, "if_match": '"hash-one"', "idempotency_key": fixture["save_key"]}),
        ("submit_run", {"input": fixture["run_input"], "idempotency_key": fixture["run_key"]}),
        ("get_result", {"result_id": fixture["result"]["result_id"]}),
    ]
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for operation, fields in operations:
                denied = await client.post("/v1/l5/mcp/tools/call", json={"tool": "l5___" + operation, "arguments": {**scope, **fields}})
                assert denied.status_code == 404 and denied.json() == {"detail": {"code": "not_found"}}
                assert fixture["foreign_project_id"] not in denied.text
    finally:
        tools_l5.l5_tool_catalogue.cache_clear()
        tools_l5.l5_operations.cache_clear()


async def shared_client_state_journey(monkeypatch):
    monkeypatch.syspath_prepend(str(CONTRACT_ROOT.parents[1] / "modules/L5-services/src"))
    from l5_services.api.router import create_router

    fixture_path = CONTRACT_ROOT.parents[1] / "modules/gateway/frontend/src/__tests__/fixtures/well-architected-client-parity.json"
    fixture = json.loads(fixture_path.read_text())
    states = fixture["states"]
    run = Run({**fixture["run"], **states["run"]})
    result = {**fixture["result"], **states["result"]}
    scope = {"product_id": fixture["scope"]["product_id"], "project_id": fixture["scope"]["project_id"], "service_id": "well_architected"}
    root = f"/v1/products/{scope['product_id']}/projects/{scope['project_id']}/services/well_architected"
    reads = []
    monkeypatch.setattr(tools_l5, "files", lambda package: CONTRACT_ROOT)
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()

    class Results:
        wa_reader_ready = True
        service_id = "well_architected"
        runs = {
            run["run_id"]: SimpleNamespace(
                tenant_id=fixture["scope"]["tenant_id"], product_id=scope["product_id"], project_id=scope["project_id"], service_id="well_architected"
            )
        }

        async def get(self, result_id):
            reads.append("get_result")
            assert result_id == result["result_id"]
            return result

        async def well_architected_publication_status(self, result_id):
            reads.append("get_publication")
            assert result_id == result["result_id"]
            return states["publication"]

        async def compare(self, baseline_result_id, candidate_result_id):
            reads.append("compare_results")
            assert (baseline_result_id, candidate_result_id) == (
                states["comparison"]["baseline_result_id"],
                states["comparison"]["candidate_result_id"],
            )
            return states["comparison"]

    class Backend:
        async def get_run(self, product_id, project_id, service_id, run_id):
            reads.append("get_run")
            assert (product_id, project_id, service_id, run_id) == (*scope.values(), run["run_id"])
            return run

        async def result_store(self, product_id, project_id, service_id):
            assert (product_id, project_id, service_id) == tuple(scope.values())
            return Results()

    backend = Backend()
    app = FastAPI()
    app.include_router(create_router(lambda: backend))
    operations = (
        ("get_run", {"assessment_ref": run["run_id"]}, "GET", root + "/runs/" + run["run_id"], None, run),
        ("get_result", {"result_id": result["result_id"]}, "GET", root + "/results/" + result["result_id"], None, result),
        (
            "get_result_publication_status",
            {"result_id": result["result_id"]},
            "GET",
            root + "/results/" + result["result_id"] + "/publication-status",
            None,
            states["publication"],
        ),
        (
            "compare_results",
            {"input": {key: states["comparison"][key] for key in ("baseline_result_id", "candidate_result_id")}},
            "POST",
            root + "/comparisons",
            {key: states["comparison"][key] for key in ("baseline_result_id", "candidate_result_id")},
            states["comparison"],
        ),
    )
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://review.test", trust_env=False) as client:
            for operation, arguments, method, path, body, expected in operations:
                direct = await client.request(method, path, json=body)
                mcp = await tools_l5.dispatch_l5("l5___" + operation, {**scope, **arguments}, backend)
                assert direct.status_code == 200 and direct.json() == expected
                assert mcp["structuredContent"]["http_status"] == direct.status_code
                assert mcp["structuredContent"]["data"] == direct.json()
                assert not mcp.get("isError", False)
            run.update({"state": "cancelled", "provider_state": "running"})
            result["recommendations"] = [{"comparison": {"status": "supported"}}]
            direct_run = await client.get(root + "/runs/" + run["run_id"])
            mcp_run = await tools_l5.dispatch_l5("l5___get_run", {**scope, "assessment_ref": run["run_id"]}, backend)
            assert direct_run.json() == mcp_run["structuredContent"]["data"] == run
            assert mcp_run["structuredContent"]["data"]["provider_state"] == "running"
            assert mcp_run["structuredContent"]["data"]["monitoring"]["last_poll_error"] == {"code": "cancel_unsupported"}
            assert "AWS cancellation confirmed" not in json.dumps(mcp_run)
            direct_result = await client.get(root + "/results/" + result["result_id"])
            mcp_result = await tools_l5.dispatch_l5("l5___get_result", {**scope, "result_id": result["result_id"]}, backend)
            assert direct_result.json() == mcp_result["structuredContent"]["data"] == result
            assert result["review_mode"] == "source_only" and result["recommendations"][0]["comparison"]["status"] == "supported"
            assert "deployment_verified" not in json.dumps(mcp_result)
            assert "verification" not in mcp_result["structuredContent"]["data"]
        assert reads == (
            ["get_run"] * 2 + ["get_result"] * 2 + ["get_publication"] * 2 + ["compare_results"] * 2 + ["get_run"] * 2 + ["get_result"] * 2
        )
    finally:
        tools_l5.l5_tool_catalogue.cache_clear()
        tools_l5.l5_operations.cache_clear()


@pytest.mark.asyncio
async def test_state_fixture_mcp_matches_authoritative_scoped_api(monkeypatch):
    await shared_client_state_journey(monkeypatch)
