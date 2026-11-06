"""Offline first-party driver checks; never contacts a paid provider."""

import json
import os
import re
import runpy
import shutil
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from subprocess import run as run_process
from types import SimpleNamespace

import httpx
import pytest

from driver import FinOpsDriver
from driver import ROOT
from l5_services.api.configuration import view as view_configuration
from l5_services.api.bindings import view_binding
from l5_services.models.run import view_run


@pytest.fixture
def case(tmp_path, monkeypatch):
    for name, value in (("EXAMPLE_SUBMIT", "submit-private"), ("EXAMPLE_READ", "read-private"),
                        ("EXAMPLE_MCP", "mcp-private")):
        monkeypatch.setenv(name, value)
    executable = tmp_path / "adp"
    executable.write_text("#!/bin/sh\nexit 1\n")
    executable.chmod(0o700)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    storage = tmp_path / "ui-state.json"
    storage.write_text("{}")
    storage.chmod(0o600)
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    native = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
              "record_type": "finops_agent", "record_id": "agent-example", "record_revision": "1"}
    billing = {"view_ref": "view-example", "account_ids": ["000000000000"]}
    query = {"start": "2026-01-01", "end": "2026-02-01", "metric": "UnblendedCost",
             "granularity": "MONTHLY", "group_by": ["SERVICE"], "currency": "USD"}
    request = {"service_id": "finops", "capability": "cost_inquiry", "configuration_revision": 1,
               "binding_id": "binding-example", "question": "What was spent?", "query": query}
    manifest = {"authorized": True, "release_sha": "a" * 40,
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(), "scope": scope, "gateway_url": "https://gateway.example.org",
                "frontend_url": "https://frontend.example.org", "binding_id": "binding-example",
                "configuration_revision": 1, "native_agent_ref": native,
                "native_agent_display_name": "Example Agent", "billing_scope": billing,
                "input": request, "receipt": "product-inquiry-0001", "cleanup_receipt": "owned-cleanup-0001",
                "api_token_env": "EXAMPLE_SUBMIT", "readback_token_env": "EXAMPLE_READ",
                "mcp_token_env": "EXAMPLE_MCP", "max_requests": 25, "deadline_seconds": 120,
                "cli_executable": str(executable), "cli_sha256": sha256(executable.read_bytes()).hexdigest(),
                "cli_deployment": "example", "cli_home": str(home), "browser_storage_state": str(storage)}
    answer_text = "Original example native answer"
    answer = {"artifact_id": "l5/finops/work/run-example/native/answers/original.txt", "version": "version-1",
              "sha256": sha256(answer_text.encode()).hexdigest()}
    page = {"artifact_id": "l5/finops/work/run-example/billing/ce-collection-example/" + "b" * 64 + ".json",
            "version": "version-2", "sha256": "b" * 64}
    config = view_configuration(SimpleNamespace(**scope, configuration_id="config-example",
                                relationship_revision="1", owner_id="actor-example", handoff_state="linked"),
                                SimpleNamespace(revision=1, state="ready", inherited_fields=[], gaps=[], hash="a" * 64,
                                settings={"enabled": True, "visibility": "only_me", "finops": {"billing_scope": billing}}))
    run = view_run(SimpleNamespace(service_id="finops", run_id="run-example", scope=config["scope"], input=request,
                   admission={"native_agent_ref": native, "binding_generation": 1}, state="succeeded",
                   provider_state="completed", revision=1, deadline_at=datetime.now(UTC) + timedelta(hours=1),
                   origin="manual", native_job_ref=None, monitoring=None, retry_of=None,
                   provider_readback_ref={"status": "answered", "answer_artifact": answer}))
    result = {"result_id": "result-example", "revision": 1, "kind": "finops_cost_inquiry",
              "run_id": run["run_id"], "scope": scope, "query": query,
              "billing_scope": billing, "native": {"status": "answered", "answer_artifact": answer},
              "oracle": {"collection_id": "ce-collection-example", "pages": [page]},
              "attribution": "account", "comparison": {"state": "unavailable"}}
    binding = view_binding(SimpleNamespace(service_id="finops", scope=config["scope"], binding_id="binding-example",
                           configuration_id="config-example", state="ready", native_resource_ref=native,
                           revision=1, generation=1, ownership="customer_linked", connection_ref={},
                           gaps=[], readiness=[], provider_identity=None))
    binding["native_agent_display_name"] = "Example Agent"
    binding["billing_scope"] = billing
    published = {"result_id": "result-example", "revision": 1, "consumers": [
        {"destination": destination, "state": "published", "receipt_id": f"receipt-{destination}",
         "projection_verified_on_ack": True} for destination in ("product-context", "engineering-insights")]}
    seen = []

    def respond(request):
        seen.append(request)
        path = request.url.path
        if path.endswith("/configuration/options"):
            return httpx.Response(200, json={"service_id": "finops", "capabilities": ["cost_inquiry"],
                "connections": [{"record_id": "connection-example"}], "regions": ["us-east-1"],
                "unavailable_reasons": ["native_unqualified", "billing_scope_unverified", "region_selection_required"]})
        if path.endswith("/configuration"):
            return httpx.Response(200, json=config)
        if path.endswith("/bindings/binding-example"):
            return httpx.Response(200, json=binding)
        if path.endswith("/runs") and request.method == "POST":
            return httpx.Response(202, json=run)
        if path.endswith("/runs/run-example"):
            return httpx.Response(200, json=run)
        if path.endswith("/results"):
            return httpx.Response(200, json={"items": [result]})
        if path.endswith("/results/result-example"):
            return httpx.Response(200, json=result)
        if path.endswith("/results/result-example/native-answer"):
            return httpx.Response(200, json={"result_id": result["result_id"], "run_id": run["run_id"],
                                             "revision": result["revision"], "artifact": answer, "text": answer_text})
        if path.endswith("/results/result-example/publication-status"):
            return httpx.Response(200, json=published)
        if path.endswith("/api/v1/l5/mcp/tools/call"):
            return httpx.Response(200, json={"structuredContent": {"http_status": 200, "data": result}})
        return httpx.Response(404)

    def cli_target(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "deployment": "example", "gateway_url": "https://gateway.example.org/api"}).encode())

    monkeypatch.setattr("driver.subprocess.run", cli_target)
    client = httpx.Client(transport=httpx.MockTransport(respond))
    return manifest, tmp_path, client, run, result, config, binding, published, seen


def test_driver_uses_product_binding_and_independent_readback(case, monkeypatch):
    manifest, directory, client, run, result, config, binding, published, seen = case
    driver = FinOpsDriver(manifest, directory, client=client)
    try:
        assert driver.submit_once() == "run-example"
        with pytest.raises(ValueError, match="already used"):
            driver.submit_once()
        assert driver.read_result("run-example") == result
        assert driver.native_answer(result)["text"] == "Original example native answer"
        assert driver.publication(result) == published
        assert driver.mcp_result(result) == result
        commands = []

        def external(command, **kwargs):
            commands.append((command, kwargs))
            if command[0] == "node":
                reply = {"result_id": result["result_id"], "revision": result["revision"],
                         "native_artifact": result["native"]["answer_artifact"],
                         "billing_pages": result["oracle"]["pages"], "answer_text": "Original example native answer"}
            else:
                reply = {"status": "ok", "detail": result}
            return SimpleNamespace(returncode=0, stdout=json.dumps(reply).encode())

        monkeypatch.setattr("driver.subprocess.run", external)
        assert driver.cli_result(result) == result
        assert driver.ui_result(result)["native_artifact"] == result["native"]["answer_artifact"]
        assert driver.cleanup(run["run_id"]) == {"status": "retained", "run_id": "run-example"}
        assert len([request for request in seen if request.method == "POST" and request.url.path.endswith("/runs")]) == 1
        assert seen[2].headers["Idempotency-Key"] == manifest["receipt"]
        assert seen[2].headers["Authorization"] == "Bearer submit-private"
        assert any(request.headers["Authorization"] == "Bearer read-private" for request in seen)
        assert any(request.headers["Authorization"] == "Bearer mcp-private" for request in seen)
        assert commands[0][0][:5] == [manifest["cli_executable"], "--deployment", "example", "service", "result"]
        assert commands[0][1]["env"]["HOME"] == os.environ["HOME"]
        for _, invocation in commands:
            for token_env in ("EXAMPLE_SUBMIT", "EXAMPLE_READ", "EXAMPLE_MCP"):
                assert token_env not in invocation["env"]
        assert "submit-private" not in driver.journal_path.read_text()
    finally:
        driver.close()


def test_denies_foreign_binding_before_paid_effect_and_uncertain_ack(case):
    manifest, directory, client, run, result, config, binding, published, seen = case
    binding["native_resource_ref"] = {**binding["native_resource_ref"], "record_id": "foreign"}
    driver = FinOpsDriver(manifest, directory, client=client)
    with pytest.raises(ValueError, match="binding differs"):
        driver.submit_once()
    assert not [request for request in seen if request.method == "POST"]
    driver.close()
    binding["native_resource_ref"] = manifest["native_agent_ref"]
    other = directory / "other"
    other.mkdir(mode=0o700)
    client.close()
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)
                          if request.method == "POST" else httpx.Response(200, json=(config if request.url.path.endswith(
                              "/configuration") else binding))))
    driver = FinOpsDriver(manifest, other, client=client)
    with pytest.raises(ValueError, match="denied"):
        driver.submit_once()
    assert '"uncertain"' in driver.journal_path.read_text()
    with pytest.raises(ValueError, match="already used"):
        driver.submit_once()
    driver.close()


def test_readback_rejects_foreign_native_and_unpublished_result(case):
    manifest, directory, client, run, result, config, binding, published, seen = case
    driver = FinOpsDriver(manifest, directory, client=client)
    try:
        assert driver.submit_once() == "run-example"
        run["provider_readback_ref"]["answer_artifact"] = {**run["provider_readback_ref"]["answer_artifact"],
                                                              "version": "other"}
        with pytest.raises(ValueError, match="native or independent billing"):
            driver.read_result("run-example")
        run["provider_readback_ref"]["answer_artifact"] = result["native"]["answer_artifact"]
        published["consumers"][0]["state"] = "pending"
        with pytest.raises(ValueError, match="not published"):
            driver.publication(result)
        result["scope"] = {**manifest["scope"], "product_id": "foreign"}
        with pytest.raises(ValueError, match="independent client"):
            driver.mcp_result({**result, "scope": manifest["scope"]})
    finally:
        driver.close()


def test_expired_or_wrong_cli_target_stops_before_product_submission(case, monkeypatch):
    manifest, directory, client, run, result, config, binding, published, seen = case
    manifest["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="bounded authorization"):
        FinOpsDriver(manifest, directory, client=client)
    assert not seen
    manifest["expires_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    monkeypatch.setattr("driver.subprocess.run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps({"deployment": "foreign", "gateway_url": "https://other.example.org/api"}).encode()))
    driver = FinOpsDriver(manifest, directory, client=client)
    with pytest.raises(ValueError, match="different Product gateway"):
        driver.submit_once()
    assert not seen
    driver.close()


def test_same_actor_tokens_allow_independent_surface_reads(case, monkeypatch):
    manifest, directory, client, run, result, config, binding, published, seen = case
    for field in ("api_token_env", "readback_token_env", "mcp_token_env"):
        monkeypatch.setenv(manifest[field], "same-authorized-actor")
    source_bound_observations(manifest, run, result)
    driver = FinOpsDriver(manifest, directory, client=client)
    commands = []

    def external(command, **kwargs):
        if len(command) > 2 and command[2].endswith("adp_deployments.py"):
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "deployment": "example", "gateway_url": "https://gateway.example.org/api"}).encode())
        commands.append(command)
        assert not any(manifest[field] in kwargs["env"] for field in
                       ("api_token_env", "readback_token_env", "mcp_token_env"))
        body = ({"result_id": result["result_id"], "revision": result["revision"],
                 "native_artifact": result["native"]["answer_artifact"], "billing_pages": result["oracle"]["pages"],
                 "answer_text": "Original example native answer"}
                if command[0] == "node" else {"status": "ok", "detail": result})
        return SimpleNamespace(returncode=0, stdout=json.dumps(body).encode())

    monkeypatch.setattr("driver.subprocess.run", external)
    try:
        assert driver.submit_once() == run["run_id"]
        observed = driver.read_result(run["run_id"])
        answer = driver.native_answer(observed)
        clients = {"cli": driver.cli_result(observed), "mcp": driver.mcp_result(observed),
                   "ui": driver.ui_result(observed, answer)}
        from evaluator import evaluate
        assert evaluate(manifest, run, observed, answer, clients, driver.publication(observed))["status"] == "schema_verified"
        assert len(commands) == 2
        assert all(request.headers["Authorization"] == "Bearer same-authorized-actor" for request in seen)
        assert any(request.url.path == "/api/v1/l5/mcp/tools/call" for request in seen)
    finally:
        driver.close()


def test_unscoped_billing_fails_before_paid_effect_and_owned_cleanup_never_deletes(case):
    manifest, directory, client, run, result, config, binding, published, seen = case
    manifest["billing_scope"] = {"view_ref": "", "account_ids": []}
    with pytest.raises(ValueError, match="billing view"):
        FinOpsDriver(manifest, directory, client=client)
    assert not seen
    manifest["billing_scope"] = config["settings"]["finops"]["billing_scope"]
    run["state"] = "outcome_unknown"
    run["provider_state"] = "unknown"
    driver = FinOpsDriver(manifest, directory, client=client)
    try:
        with pytest.raises(ValueError, match="not owned"):
            driver.cleanup("run-example")
        assert driver.submit_once() == "run-example"
        assert driver.cleanup("run-example") == {"status": "uncertain", "run_id": "run-example"}
        assert len([request for request in seen if request.method == "POST"]) == 1
    finally:
        driver.close()


def source_bound_observations(manifest, run, result):
    snapshot = {"artifact_id": "scope-snapshot-example", "version": "version-3", "sha256": "c" * 64}
    scope_digest = "d" * 64
    run["admission"]["binding_generation"] = 1
    run["admission"]["billing_scope"] = {**manifest["billing_scope"], "scope_digest": scope_digest,
                                         "snapshot_artifact": snapshot}
    run["native_job_ref"] = {**manifest["scope"], "agent_id": manifest["native_agent_ref"]["record_id"],
                             "conversation_id": "conversation-example", "turn_id": "turn-example",
                             "question_hash": sha256(manifest["input"]["question"].encode()).hexdigest()}
    result["binding_generation"] = 1
    result["scope_digest"] = scope_digest
    result["billing_scope"] = {**manifest["billing_scope"], "snapshot_artifact": snapshot}
    result["attribution"] = "product_verified"
    result["native"].update({"conversation_id": "conversation-example", "turn_id": "turn-example"})
    result["oracle"].update({"query_hash": "c" * 64, "measure": {"coverage": "complete"}})


@pytest.mark.parametrize("turn_field", ("turn_id", "turn_locator"))
async def test_prose_collection_keeps_durable_identity_through_qualification(case, turn_field):
    from unittest.mock import AsyncMock
    from l5_services.adapters.finops.inquiry import FinOpsInquiryAdapter
    from l5_services.finops_reconciliation import normalize_cost_result, unavailable_native_cost
    from evaluator import evaluate

    manifest, directory, client, run, result, config, binding, published, seen = case
    source_bound_observations(manifest, run, result)
    native_ref = run["native_job_ref"]
    if turn_field == "turn_locator":
        native_ref.pop("turn_id")
        native_ref["turn_locator"] = "ordinal:1:" + "a" * 64
    admission = run["admission"]
    admission["scope"] = run["scope"]
    admission["billing_scope"]["scope_generation"] = admission["binding_generation"]
    selected = SimpleNamespace(**manifest["scope"], service_id="finops", run_id=run["run_id"],
                               input=run["input"], admission=admission, native_job_ref=native_ref,
                               binding_generation=admission["binding_generation"])
    original = {**run["provider_readback_ref"], "kind": "finops_native_answer",
                "native_job_ref": native_ref, "observed_at": "2026-02-02T00:00:00Z"}
    executor = SimpleNamespace(**{method: AsyncMock() for method in
        ("submit", "read_submit", "observe", "read_answer", "read_native_cost", "stop")})
    executor.read_answer.return_value = original
    executor.read_native_cost.return_value = None
    adapter = FinOpsInquiryAdapter(executor, SimpleNamespace(collect_billing_page=AsyncMock(), read_billing_page=AsyncMock()))
    collected = await adapter.collect(selected, native_ref, None, {})
    selected.provider_readback_ref = run["provider_readback_ref"] = collected.result
    assert await adapter.read_native_cost(selected, None, {}) is None
    query = selected.input["query"]
    native = unavailable_native_cost(selected, "c" * 64)
    billing = {"query": query, "scope_digest": admission["billing_scope"]["scope_digest"],
               "collection_id": result["oracle"]["collection_id"], "query_hash": "c" * 64,
               "pages": result["oracle"]["pages"], "coverage": "complete", "estimated": False,
               "amount": "10.125", "currency": "USD", "series": [{
                   **{field: query[field] for field in ("metric", "currency", "granularity", "group_by")},
                   "period": {"start": query["start"], "end": query["end"]}, "group": ["Example"], "amount": "10.125"}]}
    result.update(normalize_cost_result(selected, native, billing))
    assert result["native"]["conversation_id"] == native_ref["conversation_id"]
    assert result["native"][turn_field] == native_ref[turn_field]
    assert result["native"]["measure"]["coverage"] == "unavailable"
    assert result["native"]["measure"]["amount"] is None
    assert result["comparison"]["state"] == "unavailable"
    text = "Original example native answer"
    answer = {"text": text, "artifact": original["answer_artifact"], "run_id": run["run_id"],
              "result_id": result["result_id"], "revision": result["revision"]}
    clients = {"cli": deepcopy(result), "mcp": deepcopy(result), "ui": {
        "result_id": result["result_id"], "revision": result["revision"], "native_artifact": original["answer_artifact"],
        "billing_pages": result["oracle"]["pages"], "answer_text": text}}
    with pytest.raises(ValueError, match="native Product result"):
        evaluate(manifest, run, result, answer, clients, published)
    result["attribution"] = "product_verified"
    clients.update(cli=deepcopy(result), mcp=deepcopy(result))
    assert evaluate(manifest, run, result, answer, clients, published)["status"] == "schema_verified"
    executor.submit.assert_not_awaited()


@pytest.mark.parametrize("returncode", (0, 1))
def test_release_browser_smoke_uses_isolated_driver_environment_without_provider_work(case, monkeypatch, returncode):
    import driver as live

    manifest, directory, client, *_, seen = case
    monkeypatch.setenv("HOME", str(directory / "runner-home"))
    browser_cache = directory / "shared-browsers"
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browser_cache))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "unrelated-provider-secret")
    monkeypatch.setenv("ADP_DEPLOYMENT_OTHER_TOKEN", "unrelated-deployment-secret")
    manifest_path = directory / "authorized.json"
    manifest_path.write_text(json.dumps(manifest))
    manifest_path.chmod(0o600)
    evidence = directory / "evidence"
    evidence.mkdir(mode=0o700)
    monkeypatch.setattr(live.httpx, "Client", lambda **kwargs: client)
    calls = []

    def external(command, **kwargs):
        calls.append(command)
        assert command == ["node", str(Path(__file__).with_name("browser.mjs")), "--check-dependencies"]
        assert kwargs["cwd"] == ROOT / "modules/gateway/frontend"
        environment = kwargs["env"]
        assert environment["HOME"] == os.environ["HOME"] != manifest["cli_home"]
        assert environment["ADP_HOME"] == str(Path(manifest["cli_home"]) / ".adp")
        assert environment["BG_CONFIG_DIR"] == str(Path(manifest["cli_home"]) / ".bedrock-gateway")
        assert environment["ADP_LEGACY_CONFIG_DIR"] == environment["BG_CONFIG_DIR"]
        assert environment["PLAYWRIGHT_BROWSERS_PATH"] == str(browser_cache)
        assert not any(key in environment for key in ("EXAMPLE_SUBMIT", "EXAMPLE_READ", "EXAMPLE_MCP",
                                                      "AWS_ACCESS_KEY_ID", "ADP_DEPLOYMENT_OTHER_TOKEN"))
        assert 0 < kwargs["timeout"] <= 45
        return SimpleNamespace(returncode=returncode, stdout=b'{"browser":"chromium","ready":true}', stderr=b"private detail")

    monkeypatch.setattr(live.subprocess, "run", external)
    monkeypatch.setattr(sys, "argv", ["qualify-finops-release", "check-browser", manifest["release_sha"],
                                     str(manifest_path), str(evidence)])
    script = ROOT / "modules/L5-services/scripts/qualify-finops-release.py"
    if returncode:
        with pytest.raises(ValueError, match="FinOps browser dependencies are unavailable"):
            runpy.run_path(str(script), run_name="__main__")
    else:
        runpy.run_path(str(script), run_name="__main__")
    assert len(calls) == 1 and not seen and not list(evidence.iterdir())


@pytest.mark.parametrize("cache", ("", "relative-cache", "0"))
def test_browser_smoke_requires_absolute_cache_before_any_process(case, monkeypatch, cache):
    manifest, directory, client, *_, seen = case
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", cache)
    driver = FinOpsDriver(manifest, directory, client=client, mode="preflight")
    monkeypatch.setattr("driver.subprocess.run", lambda *args, **kwargs: pytest.fail("invalid cache started a process"))
    try:
        with pytest.raises(ValueError, match="absolute shared cache path"):
            driver.check_browser_dependencies()
        assert not seen and not driver.journal_path.exists()
    finally:
        driver.close()


def test_browser_smoke_discovers_shared_installation_with_distinct_cli_home(case, monkeypatch):
    import yaml

    frontend = ROOT / "modules/gateway/frontend"
    if shutil.which("node") is None or not (frontend / "node_modules/@playwright/test/package.json").is_file():
        pytest.skip("Browser discovery requires Node and the locked frontend dependencies")
    manifest, directory, client, *_, seen = case
    monkeypatch.setenv("HOME", str(directory / "runner-home"))
    monkeypatch.setattr("driver.subprocess.run", run_process)
    steps = yaml.safe_load((ROOT / ".github/workflows/l5-services-release.yml").read_text())["jobs"]["qualify-selected"]["steps"]
    cache_step = next(step for step in steps if "PLAYWRIGHT_BROWSERS_PATH=" in step.get("run", ""))
    github_env = directory / "github-env"
    run_process(["bash", "-eu", "-c", cache_step["run"]], check=True,
                   env={"RUNNER_TEMP": str(directory), "GITHUB_ENV": str(github_env)})
    key, value = github_env.read_text().strip().split("=", 1)
    monkeypatch.setenv(key, value)
    driver = FinOpsDriver(manifest, directory, client=client, mode="preflight")
    try:
        environment = driver.client_environment()
        assert environment["HOME"] == os.environ["HOME"] != manifest["cli_home"]
        command = ["node", str(Path(__file__).with_name("browser.mjs")), "--check-dependencies"]
        missing = run_process(command, cwd=frontend, env=environment, capture_output=True, text=True, timeout=20)
        assert missing.returncode != 0
        executable = Path(re.search(r"Executable doesn't exist at ([^\r\n]+)", missing.stderr)[1].strip())
        assert executable.is_relative_to(Path(value))
        executable.parent.mkdir(parents=True)
        marker = directory / "browser-launched"
        executable.write_text(f'#!/bin/sh\nprintf "%s" "$HOME" > "{marker}"\nexit 23\n')
        executable.chmod(0o700)
        with pytest.raises(ValueError, match="FinOps browser dependencies are unavailable"):
            driver.check_browser_dependencies()
        assert marker.read_text() == os.environ["HOME"]
        marker.unlink()
        isolated = {key: value for key, value in environment.items() if key != "PLAYWRIGHT_BROWSERS_PATH"}
        missing = run_process(command, cwd=frontend, env=isolated, capture_output=True, text=True, timeout=20)
        assert missing.returncode != 0 and "Executable doesn't exist" in missing.stderr
        assert os.environ["HOME"] in missing.stderr and not marker.exists()
        assert not seen and not driver.journal_path.exists()
    finally:
        driver.close()


def release_environment(monkeypatch, fixture, source):
    environment = {
        "L5_SERVICE": "finops", "L5_ENVIRONMENT": "test", "GITHUB_REPOSITORY": "example/adp",
        "GITHUB_REPOSITORY_ID": "101", "GITHUB_RUN_ID": "202", "GITHUB_RUN_ATTEMPT": "3",
        "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKFLOW_REF": "example/adp/.github/workflows/l5-services-release.yml@refs/heads/main",
        "GITHUB_WORKFLOW_SHA": "b" * 40, "GITHUB_SHA": "b" * 40,
        "ADP_DEFINITION_REVISION": "b" * 40, "ADP_SOURCE_REVISION": source,
        "REVIEWED_SOURCE_SHA": source, "L5_CHECKOUT_REVISION": source,
        "ADP_CORRELATION": "orch:00000000-0000-0000-0000-000000000001", "ADP_CONTRACT_DIGEST": "c" * 64,
        "L5_FIXTURE_DIGEST": sha256(fixture.read_bytes()).hexdigest(),
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("entrypoint", ("direct", "release", "shared-release"))
def test_private_entrypoint_drives_one_existing_product_run(case, monkeypatch, entrypoint):
    import sys
    import runpy
    import driver as live

    manifest, directory, client, run, result, config, binding, published, seen = case
    source_bound_observations(manifest, run, result)
    if entrypoint == "shared-release":
        manifest["source_sha"] = manifest.pop("release_sha")
    manifest_path = directory / "authorized.json"
    manifest_path.write_text(json.dumps(manifest))
    manifest_path.chmod(0o600)
    evidence = directory / "evidence"
    evidence.mkdir(mode=0o700)
    monkeypatch.setattr(live.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(client, "close", lambda: None)

    def external(command, **kwargs):
        if len(command) > 2 and command[2].endswith("adp_deployments.py"):
            body = {"deployment": "example", "gateway_url": "https://gateway.example.org/api"}
        elif command[0] == "node":
            body = {"result_id": result["result_id"], "revision": result["revision"],
                    "native_artifact": result["native"]["answer_artifact"],
                    "billing_pages": result["oracle"]["pages"], "answer_text": "Original example native answer"}
        else:
            body = {"status": "ok", "detail": result}
        return SimpleNamespace(returncode=0, stdout=json.dumps(body).encode())

    monkeypatch.setattr(live.subprocess, "run", external)
    monkeypatch.setattr(sys, "argv", ["finops-driver", "--manifest", str(manifest_path),
                                  "--evidence-dir", str(evidence)])
    scripts = Path(__file__).resolve().parents[3] / "scripts"
    arguments = [manifest.get("source_sha", manifest.get("release_sha")), str(manifest_path), str(evidence)]
    if entrypoint in {"release", "shared-release"}:
        monkeypatch.setattr(sys, "argv", ["qualify-finops-release", "preflight", *arguments])
        runpy.run_path(str(scripts / "qualify-finops-release.py"), run_name="__main__")
        assert not list(evidence.iterdir()) and not any(request.method == "POST" for request in seen)
        monkeypatch.setattr(sys, "argv", ["qualify-finops-release", "run", *arguments])
        runpy.run_path(str(scripts / "qualify-finops-release.py"), run_name="__main__")
        release_environment(monkeypatch, manifest_path, arguments[0])
        monkeypatch.setattr(sys, "argv", ["emit-finops-qualification", *arguments])
        runpy.run_path(str(scripts / "emit-finops-qualification.py"), run_name="__main__")
        assert (evidence / "qualification.json").is_file()
        observation_path = next(evidence.glob("*-qualification.json"))
        original_observation = observation_path.read_text()
        forged = json.loads(original_observation)
        forged["native"]["conversation_id"] = "fabricated-conversation"
        forged.pop("sha256")
        forged["sha256"] = sha256(json.dumps(forged, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        observation_path.write_text(json.dumps(forged))
        with pytest.raises(ValueError, match="fresh authoritative readbacks"):
            runpy.run_path(str(scripts / "emit-finops-qualification.py"), run_name="__main__")
        observation_path.write_text(original_observation)
        result["revision"] += 1
        with pytest.raises(ValueError):
            runpy.run_path(str(scripts / "emit-finops-qualification.py"), run_name="__main__")
        result["revision"] -= 1
    else:
        live.main()
    journal = next(evidence.glob("*.jsonl")).read_text()
    assert '"qualification_observed"' in journal
    observed = json.loads(next(evidence.glob("*-qualification.json")).read_text())
    assert observed["revision"] == result["revision"]
    assert observed["native"]["answer_artifact"] == result["native"]["answer_artifact"]
    assert observed["billing"]["pages"] == result["oracle"]["pages"]
    assert observed["validation"] == "schema_only"
    assert "Original example native answer" not in json.dumps(observed)
    assert all(token not in json.dumps(observed) for token in ("submit-private", "read-private", "mcp-private"))
    assert next(evidence.glob("*-qualification.json")).stat().st_mode & 0o077 == 0
    assert journal.count('"acknowledged"') == 1
    assert len([request for request in seen if request.method == "POST" and
                request.url.path.endswith("/runs")]) == 1
    with pytest.raises(FileExistsError):
        live.run_qualification(manifest, evidence)


@pytest.mark.parametrize("script,action", (("qualify-finops-release.py", ["preflight"]),
                                         ("emit-finops-qualification.py", [])))
def test_release_entrypoints_reject_wrong_revision_before_requests(case, monkeypatch, script, action):
    import runpy
    import sys

    manifest, directory, client, run, result, config, binding, published, seen = case
    path = directory / "fixture.json"
    path.write_text(json.dumps(manifest))
    path.chmod(0o600)
    monkeypatch.setattr(sys, "argv", [script, *action, "f" * 40, str(path), str(directory)])
    with pytest.raises(ValueError, match="selected release revision"):
        runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts" / script), run_name="__main__")
    assert seen == [] and not list(directory.glob("*.jsonl"))


def test_missing_versioned_product_agent_label_denies_submission(case):
    manifest, directory, client, run, result, config, binding, published, seen = case
    del binding["native_agent_display_name"]
    driver = FinOpsDriver(manifest, directory, client=client)
    with pytest.raises(ValueError, match="Product configuration or native binding"):
        driver.submit_once()
    assert not [request for request in seen if request.method == "POST"]
    driver.close()


def test_evaluator_requires_original_answer_independent_ce_and_client_readback(case):
    from evaluator import evaluate

    manifest, directory, client, run, result, config, binding, published, seen = case
    driver = FinOpsDriver(manifest, directory, client=client)
    try:
        driver.submit_once()
        observed = driver.read_result(run["run_id"])
        answer = driver.native_answer(observed)
        result["attribution"] = "product_verified"
        result["native"].update({"conversation_id": "conversation-example", "turn_id": "turn-example"})
        result["oracle"].update({"query_hash": "b" * 64, "measure": {"coverage": "complete"}})
        clients = {"cli": result, "mcp": result, "ui": {
            "result_id": result["result_id"], "revision": result["revision"],
            "native_artifact": result["native"]["answer_artifact"],
            "billing_pages": result["oracle"]["pages"], "answer_text": answer["text"]}}
        verified = evaluate(manifest, run, result, answer, clients, published)
        assert verified["status"] == "schema_verified"
        assert verified["native_artifact"] == result["native"]["answer_artifact"]
        assert answer["text"] not in json.dumps(verified)
        result["attribution"] = "account"
        with pytest.raises(ValueError, match="native Product"):
            evaluate(manifest, run, result, answer, clients, published)
        result["attribution"] = "product_verified"
        with pytest.raises(ValueError, match="source-bound"):
            evaluate(manifest, run, result, {**answer, "text": "fabricated"}, clients, published)
        with pytest.raises(ValueError, match="CE page"):
            evaluate(manifest, run, {**result, "oracle": {**result["oracle"], "pages": []}}, answer, clients, published)
        with pytest.raises(ValueError, match="client or publication"):
            evaluate(manifest, run, result, answer, {**clients, "mcp": {**result, "revision": 2}}, published)
    finally:
        driver.close()


@pytest.mark.parametrize(('source', 'path', 'replacement', 'failure'), [
    ('result', ('native',), 'fabricated', 'native Product'),
    ('result', ('billing_scope',), None, 'native Product'),
    ('result', ('native', 'turn_id'), None, 'native Product'),
    ('result', ('native', 'answer_artifact'), None, 'native Product'),
    ('result', ('native', 'status'), 'unavailable', 'native Product'),
    ('result', ('attribution',), 'account', 'native Product'),
    ('result', ('revision',), 2, 'source-bound'),
    ('result', ('scope', 'product_id'), 'foreign-product', 'native Product'),
    ('result', ('billing_scope', 'account_ids'), ['111111111111'], 'native Product'),
    ('run', ('admission', 'native_agent_ref', 'record_revision'), 'other', 'native Product'),
    ('run', ('provider_readback_ref', 'answer_artifact', 'version'), 'other', 'native Product'),
    ('answer', ('text',), '', 'source-bound'),
    ('answer', ('text',), 'fabricated native answer', 'source-bound'),
    ('answer', ('artifact', 'version'), 'other', 'source-bound'),
    ('answer', ('revision',), 2, 'source-bound'),
    ('result', ('oracle', 'pages'), [], 'CE page'),
    ('result', ('oracle', 'pages', 0, 'artifact_id'), 'foreign-ce-page', 'CE page'),
    ('result', ('oracle', 'pages', 0, 'sha256'), 'f' * 64, 'CE page'),
    ('result', ('oracle', 'pages', 0, 'version'), '', 'CE page'),
    ('result', ('oracle', 'query_hash'), 'not-a-sha256', 'CE page'),
    ('result', ('oracle', 'measure', 'coverage'), 'unavailable', 'CE page'),
    ('result', ('oracle', 'measure'), None, 'CE page'),
    ('result', ('oracle',), 'fabricated', 'CE page'),
    ('clients', ('cli', 'revision'), 2, 'client or publication'),
    ('clients', ('mcp', 'scope', 'product_id'), 'foreign-product', 'client or publication'),
    ('clients', ('ui', 'answer_text'), 'fabricated native answer', 'client or publication'),
    ('publication', ('revision',), 2, 'client or publication'),
    ('publication', ('consumers', 0, 'state'), 'pending', 'client or publication'),
    ('publication', ('consumers', 1, 'destination'), 'product-context', 'client or publication'),
    ('publication', ('consumers',), [None, None], 'client or publication'),
], ids=lambda value: str(value) if isinstance(value, tuple) else None)
def test_evaluator_denies_absent_fabricated_and_wrong_revision_sources(case, source, path, replacement, failure):
    from evaluator import evaluate

    manifest, directory, client, run, result, _, _, publication, requests = case
    driver = FinOpsDriver(manifest, directory, client=client)
    try:
        assert driver.submit_once() == run['run_id']
        result = driver.read_result(run['run_id'])
        answer = driver.native_answer(result)
        result['attribution'] = 'product_verified'
        result['native'].update({'conversation_id': 'conversation-example', 'turn_id': 'turn-example'})
        result['oracle'].update({'query_hash': 'b' * 64, 'measure': {'coverage': 'complete'}})
        clients = {'cli': deepcopy(result), 'mcp': deepcopy(result), 'ui': {
            'result_id': result['result_id'], 'revision': result['revision'],
            'native_artifact': deepcopy(result['native']['answer_artifact']),
            'billing_pages': deepcopy(result['oracle']['pages']), 'answer_text': answer['text']}}
        observations = deepcopy({'run': run, 'result': result, 'answer': answer,
                                 'clients': clients, 'publication': publication})
        selected = observations[source]
        for field in path[:-1]:
            selected = selected[field]
        selected[path[-1]] = replacement
        effects = len([request for request in requests if request.method == 'POST' and request.url.path.endswith('/runs')])
        with pytest.raises(ValueError, match=failure):
            evaluate(manifest, observations['run'], observations['result'], observations['answer'],
                     observations['clients'], observations['publication'])
        assert len([request for request in requests if request.method == 'POST' and request.url.path.endswith('/runs')]) == effects == 1
    finally:
        driver.close()


def prepared_emission(case, monkeypatch, *, altered=None):
    manifest, directory, client, run, result, _, _, publication, requests = case
    source_bound_observations(manifest, run, result)
    if altered:
        selected = {'run': run, 'result': result}[altered[0]]
        for field in altered[1][:-1]:
            selected = selected[field]
        selected[altered[1][-1]] = altered[2]
    driver = FinOpsDriver(manifest, directory, client=client)
    driver.submit_once()
    readback = driver.read_run(run['run_id'])
    result = driver.read_result(run['run_id'])
    answer = driver.native_answer(result)

    def external(command, **kwargs):
        if command[0] == 'node':
            observed = {'result_id': result['result_id'], 'revision': result['revision'],
                        'native_artifact': result['native']['answer_artifact'],
                        'billing_pages': result['oracle']['pages'], 'answer_text': answer['text']}
        else:
            observed = {'status': 'ok', 'detail': result}
        return SimpleNamespace(returncode=0, stdout=json.dumps(observed).encode())

    monkeypatch.setattr('driver.subprocess.run', external)
    clients = {'cli': driver.cli_result(result), 'mcp': driver.mcp_result(result),
               'ui': driver.ui_result(result, answer)}
    return driver, readback, result, answer, clients, driver.publication(result), requests


def test_emission_is_private_deterministically_bound_and_one_shot(case, monkeypatch):
    driver, run, result, answer, clients, publication, requests = prepared_emission(case, monkeypatch)
    try:
        assert driver.cleanup(run['run_id']) == {'status': 'retained', 'run_id': run['run_id']}
        path = driver.emit_observation(run, result, answer, clients, publication)
        content = path.read_bytes()
        recorded = json.loads(content)
        integrity = recorded.pop('sha256')
        assert integrity == sha256(json.dumps(recorded, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        assert recorded['native']['turn_id'] == run['native_job_ref']['turn_id']
        assert recorded['billing']['snapshot_artifact'] == run['admission']['billing_scope']['snapshot_artifact']
        assert recorded['billing']['pages'] == result['oracle']['pages']
        assert recorded['result_sha256'] == sha256(json.dumps(result, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        assert recorded['selected_release_sha'] == driver.manifest['release_sha']
        assert recorded['validation'] == 'schema_only'
        assert all(secret not in content.decode() for secret in (
            answer['text'], 'submit-private', 'read-private', 'mcp-private', 'secret_access_key', 'session_token'))
        assert path.stat().st_mode & 0o077 == 0
        with pytest.raises(ValueError, match='already exists'):
            driver.emit_observation(run, result, answer, clients, publication)
        assert path.read_bytes() == content
        assert len([request for request in requests if request.method == 'POST' and request.url.path.endswith('/runs')]) == 1
    finally:
        driver.close()


@pytest.mark.parametrize(('source', 'fields', 'replacement'), [
    ('run', ('native_job_ref', 'turn_id'), 'foreign-turn'),
    ('run', ('native_job_ref', 'product_id'), 'foreign-product'),
    ('run', ('native_job_ref', 'question_hash'), 'f' * 64),
    ('run', ('admission', 'billing_scope', 'account_ids'), ['111111111111']),
    ('run', ('admission', 'billing_scope', 'snapshot_artifact'), None),
    ('run', ('admission', 'binding_generation'), 2),
    ('result', ('scope_digest',), 'e' * 64),
    ('result', ('native', 'conversation_id'), 'foreign-conversation'),
    ('result', ('billing_scope', 'snapshot_artifact'), None),
])
def test_emitter_rejects_mismatched_durable_turn_and_billing(case, monkeypatch, source, fields, replacement):
    driver, run, result, answer, clients, publication, requests = prepared_emission(
        case, monkeypatch, altered=(source, fields, replacement))
    try:
        with pytest.raises(ValueError, match='native turn or independent billing scope'):
            driver.emit_observation(run, result, answer, clients, publication)
        assert not list(driver.directory.glob('*-qualification.json'))
        assert len([request for request in requests if request.method == 'POST' and request.url.path.endswith('/runs')]) == 1
    finally:
        driver.close()


def test_emitter_denies_manual_injection_and_lost_readback(case, monkeypatch):
    driver, run, result, answer, clients, publication, requests = prepared_emission(case, monkeypatch)
    try:
        with pytest.raises(ValueError, match='original Product run'):
            driver.emit_observation(run, result, answer, {**clients, 'ui': None}, publication)
        with pytest.raises(ValueError, match='original Product run'):
            driver.emit_observation(run, result, answer, clients, {**publication, 'revision': 2})
        original_request = driver.request

        def revoked(method, path, **kwargs):
            if path.endswith('/runs/' + run['run_id']):
                raise ValueError('current Product readback denied')
            return original_request(method, path, **kwargs)

        monkeypatch.setattr(driver, 'request', revoked)
        with pytest.raises(ValueError, match='readback denied'):
            driver.emit_observation(run, result, answer, clients, publication)
        assert not list(driver.directory.glob('*-qualification.json'))
        assert len([request for request in requests if request.method == 'POST' and request.url.path.endswith('/runs')]) == 1
    finally:
        driver.close()
