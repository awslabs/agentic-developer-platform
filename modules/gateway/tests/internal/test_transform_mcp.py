"""Transform MCP reads retain typed L5 envelopes and scoped artifact identity."""

import base64
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import HTTPException

from src.internal import tools_l5

CONTRACT_ROOT = Path(__file__).resolve().parents[4] / "docs/l5-services"
SCOPE = {"product_id": "product-example", "project_id": "project-example", "service_id": "transform"}
PATCH = b"--- original\n+++ candidate\n"


@pytest.fixture
def transform_contract(monkeypatch):
    monkeypatch.syspath_prepend(str(CONTRACT_ROOT.parents[1] / "modules/L5-services/src"))
    monkeypatch.setattr(tools_l5, "files", lambda package: CONTRACT_ROOT)
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    yield
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()


class Store:
    transform_reader_ready = True

    def __init__(self):
        self.contents = PATCH
        self.calls = []
        self.reference = {
            "artifact_id": "artifact-example",
            "version": "1",
            "sha256": hashlib.sha256(PATCH).hexdigest(),
            "media_type": "text/x-diff",
            "bytes": len(PATCH),
            "tenant_id": "tenant-example",
            "project_id": "project-example",
            "expires_at": "2026-11-30T00:00:00Z",
        }
        self.result = {
            "service_id": "transform",
            "result_id": "result-example",
            "run_id": "run-example",
            "revision": 2,
            "scope": {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example", "relationship_revision": "1"},
            "original_patch": {"sha256": self.reference["sha256"]},
            "retained_artifacts": {"original_patch": self.reference},
            "verification_status": "partial",
            "tests_before": [{"status": "passed"}],
            "tests_after": [{"status": "failed"}],
            "limitations": ["candidate test failed"],
            "cost_status": "unknown",
        }

    def clock(self):
        return datetime(2026, 10, 28, tzinfo=UTC)

    async def artifact(self, artifact_id):
        self.calls.append(("artifact", artifact_id))
        if artifact_id != self.reference["artifact_id"]:
            raise HTTPException(404, detail={"code": "not_found"})
        return self.reference, self.contents

    async def get(self, result_id):
        self.calls.append(("result", result_id))
        if result_id != self.result["result_id"]:
            raise HTTPException(404, detail={"code": "not_found"})
        return self.result


class Backend:
    transform_result_reader_ready = True

    def __init__(self):
        self.store = Store()
        self.calls = []

    async def result_store(self, product_id, project_id, service_id):
        self.calls.append((product_id, project_id, service_id))
        if (product_id, project_id, service_id) != ("product-example", "project-example", "transform"):
            raise HTTPException(404, detail={"code": "not_found"})
        return self.store


@pytest.mark.asyncio
async def test_transform_mcp_result_and_original_diff_share_scoped_routes(transform_contract):
    backend = Backend()
    result = await tools_l5.dispatch_l5("l5___get_result", {**SCOPE, "result_id": "result-example"}, backend)
    assert result["structuredContent"]["http_status"] == 200
    assert result["structuredContent"]["etag"] == '"2"'
    assert result["structuredContent"]["data"]["tests_after"] == [{"status": "failed"}]
    assert result["structuredContent"]["data"]["cost_status"] == "unknown"
    diff = await tools_l5.dispatch_l5("l5___download_artifact", {**SCOPE, "artifact_id": "artifact-example"}, backend)
    assert diff["structuredContent"]["http_status"] == 200
    assert base64.b64decode(diff["structuredContent"]["data"]["content_base64"]) == PATCH
    assert diff["structuredContent"]["data"]["sha256"] == hashlib.sha256(PATCH).hexdigest()
    assert backend.store.calls == [("result", "result-example"), ("artifact", "artifact-example"), ("artifact", "artifact-example")]
    assert backend.calls == [("product-example", "project-example", "transform")] * 3


@pytest.mark.asyncio
async def test_transform_mcp_refuses_tampered_and_oversized_patch_before_evidence_leaks(transform_contract):
    backend = Backend()
    backend.store.contents = b"untrusted provider bytes"
    response = await tools_l5.dispatch_l5("l5___download_artifact", {**SCOPE, "artifact_id": "artifact-example"}, backend)
    assert response["isError"] and response["structuredContent"]["http_status"] == 503
    assert "content_base64" not in response["structuredContent"]
    backend.store.reference = {**backend.store.reference, "bytes": 256 * 1024 + 1}
    backend.store.calls.clear()
    response = await tools_l5.dispatch_l5("l5___download_artifact", {**SCOPE, "artifact_id": "artifact-example"}, backend)
    assert response["isError"] and response["structuredContent"]["http_status"] == 503
    assert backend.store.calls == [("artifact", "artifact-example")]
    response = await tools_l5.dispatch_l5("l5___get_result", {**SCOPE, "result_id": "missing-result"}, backend)
    assert response["isError"] and response["structuredContent"]["http_status"] == 404
    assert "content_base64" not in response["structuredContent"]


def test_transform_mcp_uses_existing_typed_tool_operations_and_rejects_unrelated_actions(transform_contract):
    catalogue = {tool["name"]: tool for tool in tools_l5.l5_tool_catalogue()["tools"]}
    for name in ("get_configuration", "get_configuration_version", "get_options", "submit_run", "get_run", "get_result", "download_artifact"):
        assert "l5." + name in catalogue
    assert "TransformResult" in catalogue["l5.get_result"]["outputSchema"]["$defs"]
    assert "content_base64" in catalogue["l5.download_artifact"]["outputSchema"]["$defs"]["ArtifactRef"]["properties"]
    method, path, _query, _headers, _body = tools_l5.l5_http_request(
        "l5___get_result",
        {**SCOPE, "result_id": "result-example"},
    )
    assert (method, path) == ("GET", "/v1/products/product-example/projects/project-example/services/transform/results/result-example")
    for name in ("list_findings", "get_publication", "retry_run"):
        with pytest.raises(HTTPException) as error:
            tools_l5.l5_http_request("l5___" + name, SCOPE)
        assert error.value.status_code == 404


def test_installed_stdio_mcp_preserves_transform_schema_and_result_envelope(transform_contract, monkeypatch):
    import importlib.util
    import json

    cli_root = CONTRACT_ROOT.parents[1] / "modules/gateway/cli"
    monkeypatch.syspath_prepend(str(cli_root))
    spec = importlib.util.spec_from_file_location("transform_mcp_cli", cli_root / "adp-mcp.py")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    monkeypatch.setattr(bridge.native_handoff, "ready", lambda: {"result": {"tools": []}})
    monkeypatch.setattr(bridge.native_handoff, "descriptors", lambda tools: [])
    backend = Backend()
    payload = {"data": backend.store.result, "http_status": 200, "etag": '"2"'}
    calls = []

    def gateway_request(path, body=None):
        calls.append((path, body))
        if path == "/v1/l5/mcp/tools/list":
            return {
                "tools": [
                    {
                        "name": "l5___get_result",
                        "inputSchema": next(tool["inputSchema"] for tool in tools_l5.l5_tool_catalogue()["tools"] if tool["name"] == "l5.get_result"),
                    },
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
    assert [item[0] for item in calls] == ["/v1/l5/mcp/tools/list", "/agent/tools", "/v1/l5/mcp/tools/call"]
    assert calls[-1][1] == {"tool": "l5___get_result", "arguments": {**SCOPE, "result_id": "result-example"}}


@pytest.mark.asyncio
async def test_pinned_result_matches_installed_cli_mcp_and_scoped_api(transform_contract):
    import asyncio
    import importlib.util
    import json

    import httpx
    from fastapi import FastAPI
    from l5_services.api.router import create_router

    fixture = json.loads((Path(__file__).resolve().parents[2] / "frontend/src/__tests__/fixtures/transform-client-parity.json").read_text())

    class RunView(dict):
        def __getattr__(self, name):
            return self[name]

    class ParityBackend(Backend):
        async def get_run(self, product_id, project_id, service_id, run_id):
            assert (product_id, project_id, service_id, run_id) == ("product-example", "project-example", "transform", "run-one")
            return RunView(fixture["run"])

    backend = ParityBackend()
    backend.store.result = fixture["result"]
    app = FastAPI()
    app.include_router(create_router(lambda: backend))
    root = "/v1/products/product-example/projects/project-example/services/transform"

    async def read(method, path):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http_client:
            return await http_client.request(method, path)

    direct_run = await read("GET", root + "/runs/run-one")
    direct_result = await read("GET", root + "/results/result-example")
    assert (direct_run.json(), direct_run.headers["etag"]) == (fixture["run"], '"2"')
    assert (direct_result.json(), direct_result.headers["etag"]) == (fixture["result"], '"3"')

    cli_file = Path(__file__).resolve().parents[2] / "cli/adp-service.py"
    spec = importlib.util.spec_from_file_location("transform_parity_cli", cli_file)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    class CliClient:
        def request(self, method, path, body=None, **options):
            assert method == "GET" and body is None and not options.get("extra_headers")
            response = asyncio.run(read(method, path))
            assert response.status_code == 200
            return response.json()

    arguments = ["--product", "product-example", "--project", "project-example", "--service", "transform", "--json"]
    cli_run = await asyncio.to_thread(cli.execute, cli.parser().parse_args(["status", "--run-id", "run-one", *arguments]), CliClient())
    cli_result_args = cli.parser().parse_args(["result", "show", "--result-id", "result-example", *arguments])
    cli_result = await asyncio.to_thread(cli.execute, cli_result_args, CliClient())
    mcp_run = await tools_l5.dispatch_l5("l5___get_run", {**SCOPE, "assessment_ref": "run-one"}, backend)
    mcp_result = await tools_l5.dispatch_l5("l5___get_result", {**SCOPE, "result_id": "result-example"}, backend)
    assert cli_run["detail"] == mcp_run["structuredContent"]["data"] == fixture["run"]
    assert cli_result["detail"] == mcp_result["structuredContent"]["data"] == fixture["result"]
    assert (mcp_run["structuredContent"]["http_status"], mcp_run["structuredContent"]["etag"]) == (200, '"2"')
    assert (mcp_result["structuredContent"]["http_status"], mcp_result["structuredContent"]["etag"]) == (200, '"3"')
    assert cli_result["detail"]["tests_after"][0]["status"] == "failed"
    assert cli_run["detail"]["provider_state"] == "unknown"


@pytest.mark.asyncio
async def test_inspection_and_denied_handoff_leave_independent_scm_state_unchanged(transform_contract, monkeypatch):
    from src.agentauth.github_provider import GitHubProvider

    scm_state = {"branch_head": "existing-head", "pull_requests": []}
    writer_calls = []

    async def unexpected_write(*args, **kwargs):
        writer_calls.append((args, kwargs))
        raise AssertionError("Transform inspection cannot call the assigned-Task SCM writer")

    for method in ("publish_commit", "upsert_pull_request", "merge_pull_request"):
        monkeypatch.setattr(GitHubProvider, method, unexpected_write)
    backend = Backend()
    before = {"branch_head": scm_state["branch_head"], "pull_requests": list(scm_state["pull_requests"])}
    result = await tools_l5.dispatch_l5("l5___get_result", {**SCOPE, "result_id": "result-example"}, backend)
    patch = await tools_l5.dispatch_l5("l5___download_artifact", {**SCOPE, "artifact_id": "artifact-example"}, backend)
    assert result["structuredContent"]["data"]["result_id"] == "result-example"
    assert base64.b64decode(patch["structuredContent"]["data"]["content_base64"]) == PATCH
    for _ in range(2):
        with pytest.raises(HTTPException) as denied:
            await tools_l5.dispatch_l5(
                "l5___publish_transform_result",
                {
                    **SCOPE,
                    "result_id": "result-example",
                    "idempotency_key": "publish-retry-key-001",
                },
                backend,
            )
        assert denied.value.status_code == 503
    assert not writer_calls and scm_state == before
    assert backend.store.calls == [("result", "result-example"), ("artifact", "artifact-example"), ("artifact", "artifact-example")]


@pytest.mark.asyncio
async def test_transform_scm_handoff_is_not_registered_and_cannot_dispatch(transform_contract):
    catalogue = {tool["name"] for tool in tools_l5.available_l5_catalogue()["tools"]}
    assert "l5.publish_transform_result" not in catalogue
    backend = Backend()
    inspected = await tools_l5.dispatch_l5("l5___get_result", {**SCOPE, "result_id": "result-example"}, backend)
    assert inspected["structuredContent"]["data"]["result_id"] == "result-example"
    for attempt in range(2):
        with pytest.raises(HTTPException) as error:
            await tools_l5.dispatch_l5(
                "l5___publish_transform_result",
                {
                    **SCOPE,
                    "result_id": "result-example",
                    "idempotency_key": "publish-retry-key-001",
                },
                backend,
            )
        assert error.value.status_code == 503 and "unavailable" in str(error.value.detail)
    assert backend.store.calls == [("result", "result-example")]
    assert backend.calls == [("product-example", "project-example", "transform")]
