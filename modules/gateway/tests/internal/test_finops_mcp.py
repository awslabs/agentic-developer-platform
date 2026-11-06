"""FinOps tools reuse scoped L5 operation registration and delegation."""

import asyncio
import base64
import importlib.util
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from src import l5_integration
from src.internal import tools_l5
from tests.cli.conftest import write_adp_session

CONTRACT_ROOT = Path(__file__).resolve().parents[4] / "docs/l5-services"
SCOPE = {"product_id": "product-example", "project_id": "project-example", "service_id": "finops"}


@pytest.fixture
def finops_catalogue(monkeypatch):
    monkeypatch.setattr(tools_l5, "files", lambda package: CONTRACT_ROOT)
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    yield
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()


async def test_client_parity(finops_catalogue, monkeypatch, tmp_path):
    await _exercise_client_journey(finops_catalogue, monkeypatch, tmp_path)


async def test_client_denial(finops_catalogue, monkeypatch, tmp_path):
    await _exercise_client_journey(finops_catalogue, monkeypatch, tmp_path, denial=True)


async def test_client_outcomes(finops_catalogue, monkeypatch, tmp_path):
    await _exercise_client_journey(finops_catalogue, monkeypatch, tmp_path, outcomes=True)


async def _exercise_client_journey(finops_catalogue, monkeypatch, tmp_path, *, denial=False, outcomes=False):
    scope = {
        "tenant_id": "tenant-example",
        "product_id": "product-example",
        "project_id": "project-example",
        "relationship_revision": "1",
        "owner_id": "owner-example",
        "visibility": "only_me",
    }
    query = {
        "start": "2026-01-01",
        "end": "2026-02-01",
        "metric": "UnblendedCost",
        "granularity": "MONTHLY",
        "group_by": ["SERVICE"],
        "currency": "USD",
    }
    inquiry = {
        "service_id": "finops",
        "capability": "cost_inquiry",
        "configuration_revision": 1,
        "binding_id": "binding-example",
        "question": "What did January cost?",
        "query": query,
    }
    calls = []
    settings_calls = []
    setup_calls = []
    choices = {"service_id": "finops", "finops": {"agent_mode": "create"}}
    binding_request = {"action": "create", "configuration_revision": 1}

    class Grants:
        async def scalars(self, statement):
            values = set(statement.compile().params.values())
            granted = {"tenant-example", "registry-example", "product-example", "project-example"} <= values
            return SimpleNamespace(all=lambda: ["grant-example"] if granted else [])

    class Run(dict):
        revision = 1

        @property
        def run_id(self):
            return self["run_id"]

    run = Run(run_id="run-example", scope=scope, revision=1, state="admitted")
    next_run = Run(run_id="run-next", scope=scope, revision=1, state="admitted")
    artifact = {"artifact_id": "evidence-example", "version": "1", "sha256": "a" * 64}
    native_artifact = {"artifact_id": "native-example", "version": "1", "sha256": "d" * 64}
    attribution_artifact = {"artifact_id": "attribution-example", "version": "1", "sha256": "e" * 64}
    native_measure = {
        "amount": None,
        "currency": None,
        "estimated": None,
        "source_refreshed_at": None,
        "freshness_reason": "native_unavailable",
        "coverage": "unavailable",
    }
    billing_measure = {
        "amount": "-12.3400",
        "currency": "USD",
        "estimated": False,
        "source_refreshed_at": None,
        "freshness_reason": None,
        "coverage": "complete",
    }
    result = {
        "result_id": "result-example",
        "revision": 1,
        "scope": scope,
        "kind": "finops_cost_inquiry",
        "run_id": "run-example",
        "query": query,
        "scope_digest": "b" * 64,
        "binding_generation": 1,
        "attribution": "unknown",
        "attribution_evidence": [],
        "native": {
            "status": "outcome_unknown",
            "conversation_id": None,
            "turn_id": None,
            "answer_artifact": None,
            "observed_at": None,
            "measure": native_measure,
        },
        "oracle": {
            "collection_id": "collection-example",
            "query_hash": "c" * 64,
            "observed_at": "2026-02-02T00:00:00Z",
            "measure": billing_measure,
            "pages": [artifact],
        },
        "comparison": {"state": "incomparable", "delta": None, "display_quantum": None, "reason": "native_unavailable"},
        "billing_scope": {"account_ids": ["123456789012"], "view_ref": "view-example", "snapshot_artifact": artifact},
    }
    refused = {**result, "result_id": "result-refused", "native": {**result["native"], "status": "refused"}}
    partial = {
        **result,
        "result_id": "result-partial",
        "attribution": "partial",
        "attribution_evidence": [attribution_artifact],
        "native": {
            **result["native"],
            "status": "answered",
            "answer_artifact": native_artifact,
            "measure": {**native_measure, "amount": "-10.0000", "currency": "USD", "coverage": "partial"},
        },
        "comparison": {"state": "mismatch", "delta": "-2.3400", "display_quantum": "0.01", "reason": "partial_billing"},
    }
    results = {item["result_id"]: item for item in (result, refused, partial)}

    class ResultStore:
        async def get(self, result_id):
            if result_id not in results:
                raise HTTPException(404, detail={"code": "not_found"})
            return results[result_id]

        async def list(self, cursor=None):
            items = list(results.values())
            return items[1:] if cursor == "result-example" else items

    class Backend:
        def __init__(self, delegation_id):
            self.delegation_id = delegation_id

        def authorize(self, product_id, project_id, service_id):
            if (product_id, project_id, service_id, self.delegation_id) != ("product-example", "project-example", "finops", "grant-example"):
                raise HTTPException(404, detail={"code": "not_found"})

        async def get(self, product_id, project_id, service_id):
            self.authorize(product_id, project_id, service_id)
            return {"scope": scope, "hash": "config-example", "revision": 1, "state": "ready", "settings": choices, "inherited_fields": []}

        async def save(self, product_id, project_id, service_id, settings, key, if_none_match, if_match):
            self.authorize(product_id, project_id, service_id)
            if key == "config-key-example-0001" and settings != choices:
                raise HTTPException(409, detail={"code": "idempotency_conflict"})
            if if_match != '"config-example"' or settings != choices:
                raise HTTPException(412, detail={"code": "precondition_failed"})
            if (settings, key) not in settings_calls:
                settings_calls.append((settings, key))
            return await self.get(product_id, project_id, service_id)

        async def start_binding(self, product_id, project_id, service_id, request, key, if_match):
            self.authorize(product_id, project_id, service_id)
            if request != binding_request:
                raise HTTPException(422, detail={"code": "validation_error"})
            if (request, key) not in setup_calls:
                setup_calls.append((request, key))
            return SimpleNamespace(
                operation_id="operation-example",
                revision=1,
                action="create",
                state="blocked",
                gaps=["native_unqualified"],
                binding_id=None,
                readback_ref=None,
                cleanup_receipt=None,
            )

        async def submit(self, product_id, project_id, service_id, run_input, key):
            self.authorize(product_id, project_id, service_id)
            if (run_input, key) not in calls:
                calls.append((run_input, key))
            return run

        async def get_run(self, product_id, project_id, service_id, run_id):
            self.authorize(product_id, project_id, service_id)
            if run_id not in {"run-example", "run-next"}:
                raise HTTPException(404, detail={"code": "not_found"})
            return run if run_id == "run-example" else next_run

        async def list_runs(self, product_id, project_id, service_id, cursor=None):
            self.authorize(product_id, project_id, service_id)
            return [next_run] if cursor == "run-example" else [run, next_run]

        async def result_store(self, product_id, project_id, service_id):
            self.authorize(product_id, project_id, service_id)
            return ResultStore()

    host_backend = l5_integration.backend
    monkeypatch.setattr(
        l5_integration,
        "backend",
        lambda service_id, *, request, db, actor, x_l5_delegation_id, response: Backend(
            x_l5_delegation_id if actor.org_id == "tenant-example" else "foreign-grant"
        ),
    )
    app = FastAPI()
    app.include_router(l5_integration.router)
    app.include_router(l5_integration.router, prefix="/api")

    cli_dir = Path(__file__).resolve().parents[2] / "cli"
    installed_tokens = {}

    @app.middleware("http")
    async def require_installed_session(request: Request, call_next):
        if request.url.path.startswith("/api/v1/") or request.url.path == "/api/me/cli-capabilities":
            if request.headers.get("authorization") not in installed_tokens:
                return JSONResponse({"detail": {"code": "authentication_required"}}, status_code=401)
        return await call_next(request)

    @app.get("/api/me/cli-capabilities")
    def cli_capabilities(request: Request):
        return {
            "schema_version": "2026-09-21",
            "gateway": {"state": "yes", "release": "example"},
            "tenant": {"org_id": installed_tokens[request.headers["authorization"]]},
            "operations": [{"id": "service.mutate", "supported": "yes", "enabled": "yes", "permitted": "yes", "ready": "yes"}],
        }

    def host_fixture(request: Request, x_l5_delegation_id: str | None = Header(None, alias="X-L5-Delegation-ID")):
        tenant = installed_tokens.get(request.headers.get("authorization"), "tenant-example")
        return Backend((x_l5_delegation_id or "grant-example") if tenant == "tenant-example" else "foreign-grant")

    def actor_fixture(request: Request):
        return SimpleNamespace(
            account_type="service",
            org_id=installed_tokens.get(request.headers.get("authorization"), "tenant-example"),
            agent_registry_id="registry-example",
        )

    app.dependency_overrides[host_backend] = host_fixture
    app.dependency_overrides[l5_integration.get_current_user] = actor_fixture
    app.dependency_overrides[l5_integration.get_db] = Grants

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        listing = await client.post("/v1/l5/mcp/tools/list")
        assert listing.status_code == 200
        tools = {item["name"]: item for item in listing.json()["tools"]}
        assert {"l5___get_configuration", "l5___submit_run", "l5___get_run", "l5___get_result"} <= tools.keys()
        assert "assessment_ref" in tools["l5___get_run"]["inputSchema"]["properties"]
        assert "run_id" not in tools["l5___get_run"]["inputSchema"]["properties"]

        async def call(tool, **arguments):
            return await client.post("/v1/l5/mcp/tools/call", json={"tool": tool, "arguments": {**SCOPE, **arguments}})

        read = await call("l5___get_configuration")
        assert read.status_code == 200
        assert read.json()["structuredContent"]["etag"] == '"config-example"'
        assert json.loads(read.json()["content"][0]["text"]) == read.json()["structuredContent"]

        saved = await call("l5___save_configuration", input=choices, idempotency_key="config-key-example-0001", if_match='"config-example"')
        assert saved.json()["structuredContent"]["etag"] == read.json()["structuredContent"]["etag"]
        setup = await call("l5___start_binding_operation", input=binding_request, idempotency_key="setup-key-example-0001")
        assert setup.json()["structuredContent"]["http_status"] == 202
        assert setup.json()["structuredContent"]["data"]["gaps"] == ["native_unqualified"]
        assert settings_calls == [(choices, "config-key-example-0001")]
        assert setup_calls == [(binding_request, "setup-key-example-0001")]

        accepted = await call("l5___submit_run", input=inquiry, idempotency_key="inquiry-key-example-0001")
        assert accepted.status_code == 200
        assert accepted.json()["structuredContent"]["http_status"] == 202
        assert accepted.json()["structuredContent"]["data"]["state"] == "admitted"
        assert calls == [(inquiry, "inquiry-key-example-0001")]
        status = await call("l5___get_run", assessment_ref="run-example")
        assert status.json()["structuredContent"]["etag"] == '"1"'
        answer = await call("l5___get_result", result_id="result-example")
        assert answer.json()["structuredContent"]["data"]["oracle"]["measure"]["amount"] == "-12.3400"
        assert answer.json()["structuredContent"]["data"]["native"]["measure"]["amount"] is None
        assert "coverage" not in answer.json()["structuredContent"]["data"]
        for result_id in ("result-refused", "result-partial"):
            observed = await call("l5___get_result", result_id=result_id)
            assert observed.status_code == 200
            structured = observed.json()["structuredContent"]
            assert observed.json().get("isError") is not True
            assert structured["data"] == results[result_id]
            assert json.loads(observed.json()["content"][0]["text"]) == structured
            assert structured["data"]["native"]["measure"]["amount"] == (None if result_id == "result-refused" else "-10.0000")
            assert structured["data"]["oracle"]["measure"]["amount"] == "-12.3400"
            assert structured["data"]["comparison"]["delta"] == (None if result_id == "result-refused" else "-2.3400")

        base = "/v1/products/product-example/projects/project-example/services/finops"
        direct_read = await client.get(base + "/configuration", headers={"X-L5-Delegation-ID": "grant-example"})
        direct_result = await client.get(base + "/results/result-example", headers={"X-L5-Delegation-ID": "grant-example"})
        assert direct_read.json() == read.json()["structuredContent"]["data"]
        assert direct_read.headers["etag"] == read.json()["structuredContent"]["etag"]
        assert direct_result.json() == answer.json()["structuredContent"]["data"]
        assert direct_result.headers["etag"] == answer.json()["structuredContent"]["etag"]

        spec = importlib.util.spec_from_file_location("finops_service_cli", Path(__file__).resolve().parents[2] / "cli/adp-service.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        monkeypatch.setattr(cli.common, "ensure_can_mutate", lambda *args, **kwargs: None)

        with TestClient(app) as transport:

            class LocalClient:
                last_etag = None

                def request(self, method, path, body=None, *, extra_headers=None):
                    response = transport.request(
                        method,
                        path,
                        json=body,
                        headers={
                            "X-L5-Delegation-ID": "grant-example",
                            **(extra_headers or {}),
                        },
                    )
                    assert response.status_code < 400, response.text
                    self.last_etag = response.headers.get("etag")
                    return response.json()

            local = LocalClient()
            scope_args = ["--product", "product-example", "--project", "project-example", "--service", "finops"]

            def command(*parts):
                args = cli.parser().parse_args([*parts, *scope_args, "--json"])
                return cli.execute(args, local)

            assert command("config", "show")["detail"] == direct_read.json()
            assert local.last_etag == direct_read.headers["etag"]
            with tempfile.TemporaryDirectory() as directory:
                config_file = Path(directory) / "configuration.json"
                config_file.write_text(json.dumps(choices))
                cli_saved = command(
                    "config", "update", "--input", str(config_file), "--if-match", '"config-example"', "--idempotency-key", "config-key-example-0001"
                )
                binding_file = Path(directory) / "binding.json"
                binding_file.write_text(json.dumps(binding_request))
                cli_setup = command("binding", "create", "--input", str(binding_file), "--idempotency-key", "setup-key-example-0001")
                input_file = Path(directory) / "inquiry.json"
                input_file.write_text(json.dumps(inquiry))
                cli_accepted = command("run", "--input", str(input_file), "--idempotency-key", "inquiry-key-example-0001")
            assert cli_saved["detail"] == saved.json()["structuredContent"]["data"]
            assert cli_saved["etag"] == saved.json()["structuredContent"]["etag"]
            assert cli_setup["detail"] == setup.json()["structuredContent"]["data"]
            assert cli_setup["etag"] == setup.json()["structuredContent"]["etag"]
            assert cli_accepted["detail"] == accepted.json()["structuredContent"]["data"]
            assert cli_accepted["etag"] == accepted.json()["structuredContent"]["etag"]
            assert command("status", "--run-id", "run-example")["detail"] == status.json()["structuredContent"]["data"]
            assert command("result", "show", "--result-id", "result-example")["detail"] == direct_result.json()
            assert local.last_etag == direct_result.headers["etag"]

        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical", access_log=False))
        worker = threading.Thread(target=server.run, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and worker.is_alive() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert server.started
            gateway_url = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
            home = tmp_path / "home"
            home.mkdir()
            binary = tmp_path / "bin"
            environment = {
                key: value for key, value in os.environ.items() if not key.startswith(("ADP_", "HERMES_", "BG_", "XDG_", "CODEX_", "AWS_"))
            }
            environment.update(
                {"HOME": str(home), "AWS_CONFIG_FILE": "/dev/null", "AWS_SHARED_CREDENTIALS_FILE": "/dev/null", "AWS_EC2_METADATA_DISABLED": "true"}
            )
            installed = await asyncio.to_thread(
                subprocess.run,
                ["sh", str(cli_dir / "install.sh"), "--gateway-url", gateway_url, "--prefix", str(binary), "--no-path-edit"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=90,
            )
            assert installed.returncode == 0, installed.stderr
            assert (binary / "adp-service.py").read_bytes() == (cli_dir / "adp-service.py").read_bytes()

            def seed_session(directory, username, tenant):
                write_adp_session(directory, username=username)
                token_file = directory / ".bedrock-gateway" / "tokens.json"
                tokens = json.loads(token_file.read_text())
                segments = tokens["access_token"].split(".")
                claims = json.loads(base64.urlsafe_b64decode(segments[1] + "=" * (-len(segments[1]) % 4)))
                claims["org_id"] = tenant
                segments[1] = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
                token = tokens["access_token"] = ".".join(segments)
                token_file.write_text(json.dumps(tokens))
                (directory / ".bedrock-gateway" / "config.json").write_text(json.dumps({"gateway_url": gateway_url + "/api"}))
                installed_tokens["Bearer " + token] = tenant
                return token

            owned_token = seed_session(home, "example-alice", "tenant-example")

            async def installed_command(*parts, command_scope=None, client_environment=None, exit_code=0):
                completed = await asyncio.to_thread(
                    subprocess.run,
                    [str(binary / "adp"), "service", *parts, *(command_scope or scope_args), "--json"],
                    env=client_environment or environment,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert completed.returncode == exit_code, completed.stderr + completed.stdout
                return json.loads(completed.stdout)

            async with httpx.AsyncClient(base_url=gateway_url) as served:
                unauthenticated = await served.get("/api/v1/products/product-example/projects/project-example/services/finops/configuration")
                assert unauthenticated.status_code == 401
                assert "settings" not in unauthenticated.json()
            assert (await installed_command("config", "show"))["detail"] == direct_read.json()
            with tempfile.TemporaryDirectory(dir=tmp_path) as directory:
                config_file = Path(directory) / "configuration.json"
                config_file.write_text(json.dumps(choices))
                binding_file = Path(directory) / "binding.json"
                binding_file.write_text(json.dumps(binding_request))
                input_file = Path(directory) / "inquiry.json"
                input_file.write_text(json.dumps(inquiry))
                installed_saved = await installed_command(
                    "config", "update", "--input", str(config_file), "--if-match", '"config-example"', "--idempotency-key", "config-key-example-0001"
                )
                installed_setup = await installed_command(
                    "binding", "create", "--input", str(binding_file), "--idempotency-key", "setup-key-example-0001"
                )
                installed_accepted = await installed_command("run", "--input", str(input_file), "--idempotency-key", "inquiry-key-example-0001")
            for installed_response, mcp_response in ((installed_saved, saved), (installed_setup, setup), (installed_accepted, accepted)):
                assert installed_response["detail"] == mcp_response.json()["structuredContent"]["data"]
                assert installed_response.get("etag") == mcp_response.json()["structuredContent"].get("etag")
            assert installed_accepted["detail"]["state"] == "admitted"
            assert (await installed_command("status", "--run-id", "run-example"))["detail"] == status.json()["structuredContent"]["data"]
            assert (await installed_command("result", "show", "--result-id", "result-example"))["detail"] == direct_result.json()
            if outcomes:
                for result_id, native_state, amount in (
                    ("result-example", "outcome_unknown", None),
                    ("result-refused", "refused", None),
                    ("result-partial", "answered", "-10.0000"),
                ):
                    installed = await installed_command("result", "show", "--result-id", result_id)
                    observed = await call("l5___get_result", result_id=result_id)
                    assert installed["detail"] == observed.json()["structuredContent"]["data"] == results[result_id]
                    assert installed["detail"]["native"]["status"] == native_state
                    assert installed["detail"]["native"]["measure"]["amount"] == amount
                    assert installed["detail"]["oracle"]["measure"]["amount"] == "-12.3400"
                    assert installed["detail"]["query"] == query
                    assert installed["detail"]["comparison"]["delta"] == ("-2.3400" if result_id == "result-partial" else None)
                assert partial["native"]["answer_artifact"] != partial["oracle"]["pages"][0]
                assert partial["attribution_evidence"][0] != partial["native"]["answer_artifact"]
                assert {item["result_id"] for item in (await installed_command("results"))["detail"]["items"]} == set(results)
                run["state"] = "outcome_unknown"
                pending = await installed_command("wait", "--run-id", "run-example", "--wait-seconds", "1", "--poll-seconds", "1", exit_code=7)
                assert pending["status"] == "pending" and pending["detail"]["state"] == "outcome_unknown"
                assert "do not submit again" in pending["next_action"]
                assert (await installed_command("status", "--run-id", "run-example"))["detail"]["state"] == "outcome_unknown"
                run["state"] = "partial"
                terminal = await installed_command("wait", "--run-id", "run-example", "--wait-seconds", "1", exit_code=6)
                assert terminal["status"] == "failed" and terminal["error"]["code"] == "run_partial"
                assert terminal["detail"]["state"] == "partial"
                assert (await installed_command("result", "show", "--result-id", "result-partial"))["detail"] == partial
                assert calls == [(inquiry, "inquiry-key-example-0001")]
                assert settings_calls == [(choices, "config-key-example-0001")]
                assert setup_calls == [(binding_request, "setup-key-example-0001")]
            if denial:
                foreign_home = tmp_path / "foreign-home"
                foreign_home.mkdir()
                foreign_token = seed_session(foreign_home, "example-bob", "foreign-tenant")
                foreign_environment = {**environment, "HOME": str(foreign_home)}
                foreign_project = ["--product", "product-example", "--project", "foreign-project", "--service", "finops"]
                owned_path = "/api/v1/products/product-example/projects/project-example/services/finops"
                foreign_path = owned_path.replace("project-example", "foreign-project")
                async with httpx.AsyncClient(base_url=gateway_url, headers={"Authorization": "Bearer " + owned_token}) as served:
                    result_page = await served.get(owned_path + "/results?limit=1")
                    run_page = await served.get(owned_path + "/runs?limit=1")
                    assert result_page.status_code == run_page.status_code == 200
                    result_cursor = result_page.json()["next_cursor"]
                    run_cursor = run_page.json()["next_cursor"]
                    assert result_cursor == "result-example" and run_cursor == "run-example"
                    denied_paths = [
                        "/configuration",
                        "/runs/run-example",
                        "/results/result-example",
                        f"/results?cursor={result_cursor}",
                        f"/runs?cursor={run_cursor}",
                        "/artifacts/evidence-example/content",
                    ]
                    for path in denied_paths:
                        denied = await served.get(foreign_path + path)
                        assert denied.status_code == 404
                        assert denied.json() == {"detail": {"code": "not_found"}}
                        assert "tenant-example" not in denied.text
                    for method, path, body, key in (
                        ("PUT", "/configuration", choices, "config-key-example-0001"),
                        ("POST", "/runs", inquiry, "inquiry-key-example-0001"),
                    ):
                        denied = await served.request(
                            method, foreign_path + path, json=body, headers={"Idempotency-Key": key, "If-Match": '"config-example"'}
                        )
                        assert denied.status_code == 404
                        assert "config-example" not in denied.text
                async with httpx.AsyncClient(base_url=gateway_url, headers={"Authorization": "Bearer " + foreign_token}) as served:
                    for path in denied_paths:
                        denied = await served.get(owned_path + path)
                        assert denied.status_code == 404
                        assert denied.json() == {"detail": {"code": "not_found"}}
                    denied = await served.post(owned_path + "/runs", json=inquiry, headers={"Idempotency-Key": "inquiry-key-example-0001"})
                    assert denied.status_code == 404
                input_file = tmp_path / "denied-inquiry.json"
                input_file.write_text(json.dumps(inquiry))
                for command_parts in (
                    ("config", "show"),
                    ("status", "--run-id", "run-example"),
                    ("result", "show", "--result-id", "result-example"),
                    ("results", "--cursor", result_cursor),
                    ("runs", "--cursor", run_cursor),
                ):
                    denied = await installed_command(*command_parts, command_scope=foreign_project, exit_code=5)
                    assert denied["error"]["code"] == "not_found"
                    assert "result-example" not in json.dumps(denied)
                denied = await installed_command(
                    "run", "--input", str(input_file), "--idempotency-key", "inquiry-key-example-0001", command_scope=foreign_project, exit_code=5
                )
                assert denied["error"]["code"] == "not_found"
                denied = await installed_command(
                    "result", "show", "--result-id", "result-example", client_environment=foreign_environment, exit_code=5
                )
                assert denied["error"]["code"] == "not_found"
                for tool, arguments in (
                    ("l5___get_result", {"result_id": "result-example"}),
                    ("l5___submit_run", {"input": inquiry, "idempotency_key": "inquiry-key-example-0001"}),
                ):
                    denied_mcp = await client.post(
                        "/v1/l5/mcp/tools/call",
                        json={"tool": tool, "arguments": {**SCOPE, **arguments}},
                        headers={"Authorization": "Bearer " + foreign_token},
                    )
                    assert denied_mcp.status_code == 404
                    assert denied_mcp.json() == {"detail": {"code": "not_found"}}
                assert calls == [(inquiry, "inquiry-key-example-0001")]
                assert settings_calls == [(choices, "config-key-example-0001")]
        finally:
            server.should_exit = True
            worker.join(timeout=5)
            assert not worker.is_alive()
        assert calls == [(inquiry, "inquiry-key-example-0001")]
        assert settings_calls == [(choices, "config-key-example-0001")]
        assert setup_calls == [(binding_request, "setup-key-example-0001")]
        assert (await call("l5___get_configuration", project_id="other-project")).status_code == 404
        assert (await call("l5___get_run", run_id="run-example")).status_code == 422
        for tool, arguments, http_status, code in (
            (
                "l5___save_configuration",
                {"input": choices, "idempotency_key": "config-key-example-0002", "if_match": '"stale"'},
                412,
                "precondition_failed",
            ),
            ("l5___save_configuration", {"input": choices, "idempotency_key": "config-key-example-0002"}, 428, "precondition_required"),
            (
                "l5___save_configuration",
                {
                    "input": {**choices, "finops": {"agent_mode": "adopt", "native_agent_ref": "agent-example"}},
                    "idempotency_key": "config-key-example-0001",
                    "if_match": '"config-example"',
                },
                409,
                "idempotency_conflict",
            ),
            (
                "l5___submit_run",
                {"input": {**inquiry, "query": {**query, "metric": "Forecast"}}, "idempotency_key": "inquiry-key-example-0002"},
                422,
                "validation_error",
            ),
            ("l5___get_result", {"result_id": "foreign-result"}, 404, "not_found"),
        ):
            failure = await call(tool, **arguments)
            assert failure.status_code == 200
            payload = failure.json()
            assert payload["isError"] is True
            assert payload["structuredContent"]["http_status"] == http_status
            assert payload["structuredContent"]["error"]["code"] == code
            assert "data" not in payload["structuredContent"]
            assert "private-provider-secret" not in json.dumps(payload)
        for tool, arguments in (
            ("l5___retry_run", {"assessment_ref": "run-example", "input": {"reason": "retry"}, "idempotency_key": "retry-key-example-0001"}),
            ("l5___list_findings", {"result_id": "result-example"}),
            ("l5___get_artifact_access", {"artifact_id": "artifact-example"}),
        ):
            assert (await call(tool, **arguments)).status_code == 404
        assert len(calls) == 1
