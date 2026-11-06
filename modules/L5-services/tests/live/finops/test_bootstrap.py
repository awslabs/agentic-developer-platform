"""Exercise empty-install setup and portable sessions without provider access."""

import json
import os
from copy import deepcopy
from pathlib import Path
from subprocess import run as run_process

import httpx
import pytest

from bootstrap import prepare, read_selection
from driver import FinOpsDriver
from fixtures import materialize_clients
from test_driver import case as driver_case


case = driver_case


@pytest.fixture
def first_install(case, monkeypatch):
    manifest, directory, client, run, result, config, binding, published, seen = case
    manifest["native_agent_ref"]["source_connection_id"] = "connection-example"
    connection = {**manifest["native_agent_ref"], "record_type": "connection", "record_id": "connection-example",
                  "source_connection_id": "connection-example"}
    settings = {"service_id": "finops", "enabled": True, "capabilities": ["cost_inquiry"],
                "connection_ref": connection, "region": "us-east-1",
                "overrides": [{"field": "connection_ref", "value": connection, "reason": "Selected"},
                              {"field": "region", "value": "us-east-1", "reason": "Selected"}],
                "limits": {"deadline_seconds": 3600, "max_provider_attempts": 1, "max_polls": 4,
                           "concurrency": 5, "max_cost": "300.00", "currency": "USD",
                           "policy_ref": {**connection, "record_type": "policy", "record_id": "budget-example"}},
                "finops": {
                    "agent_mode": "adopt", "native_agent_ref": manifest["native_agent_ref"]["record_id"],
                    "native_agent_display_name": manifest["native_agent_display_name"],
                    "billing_scope": manifest["billing_scope"], "data_role_ref": "arn:aws:iam::000000000000:role/data"}}
    manifest.pop("binding_id")
    manifest.pop("configuration_revision")
    manifest["input"].pop("binding_id")
    manifest["input"].pop("configuration_revision")
    manifest["bootstrap"] = {"configuration": settings, "trigger_id": "manual-example",
                             "receipts": {name: f"first-install-{name}-0001" for name in (
                                 "configure", "link", "trigger", "readback")}}
    state = {"configured": False, "operations": {}, "reads": {}, "uncertain": False, "deny": False}
    options = {"service_id": "finops", "capabilities": ["cost_inquiry"], "connections": [connection],
               "regions": ["us-east-1"], "unavailable_reasons": ["native_unqualified", "billing_scope_unverified"]}

    def respond(request):
        seen.append(request)
        path = request.url.path
        if path.endswith("/configuration/options"):
            return httpx.Response(403 if state["deny"] else 200, json=options)
        if path.endswith("/configuration") and request.method == "PUT":
            payload = json.loads(request.content)
            if not state["configured"]:
                assert request.headers["If-None-Match"] == "*" and payload == settings
                state["configured"] = True
                status = 201
            else:
                assert request.headers["If-Match"] == '"' + config["hash"] + '"'
                assert payload["triggers"][0]["binding_id"] == binding["binding_id"]
                config["revision"] += 1
                status = 200
            config["state"] = "blocked"
            config["settings"] = payload
            return httpx.Response(status, json=config)
        if path.endswith("/configuration"):
            return httpx.Response(200 if state["configured"] else 404, json=config)
        if path.endswith("/binding-operations") and request.method == "POST":
            payload = json.loads(request.content)
            assert payload == {"action": "link", "configuration_revision": config["revision"],
                               "native_resource_ref": manifest["native_agent_ref"]}
            operation = "setup-" + str(len(state["operations"]) + 1)
            state["operations"][operation] = payload
            state["reads"][operation] = 0
            if state["uncertain"]:
                raise httpx.ReadTimeout("acknowledgement lost")
            return httpx.Response(202, json={"operation_id": operation, "state": "accepted"})
        if "/binding-operations/" in path:
            operation = path.rsplit("/", 1)[1]
            state["reads"][operation] += 1
            ready = state["reads"][operation] > 1
            if ready:
                config["state"] = "ready"
            return httpx.Response(200, json={"operation_id": operation, "state": "succeeded" if ready else "waiting",
                                             "binding_id": binding["binding_id"]})
        if path.endswith("/bindings/binding-example"):
            return httpx.Response(200, json=binding)
        if path.endswith("/runs") and request.method == "POST":
            assert json.loads(request.content)["configuration_revision"] == config["revision"]
            return httpx.Response(202, json=run)
        return httpx.Response(404)

    constructor = httpx.Client
    monkeypatch.setattr("driver.httpx.Client", lambda **kwargs: constructor(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr("bootstrap.time.sleep", lambda _: None)
    client.close()
    return manifest, directory, state, seen, options


def test_first_install_access_preflight_does_not_require_configuration_or_binding(first_install):
    manifest, directory, state, seen, _ = first_install
    driver = FinOpsDriver(manifest, directory, mode="preflight")
    try:
        assert driver.installation_preflight() == {"installation_prerequisites": "available", "native_qualification": "not_run"}
        assert not state["configured"] and not state["operations"]
        assert all(request.method == "GET" and request.url.path.endswith("/configuration/options") for request in seen)
        assert not list(directory.glob("*.jsonl"))
    finally:
        driver.close()


def test_post_resume_setup_observes_original_operations_before_one_native_submission(first_install):
    manifest, directory, state, seen, _ = first_install
    selected = prepare(manifest, directory)
    assert selected["binding_id"] == "binding-example" and selected["configuration_revision"] == 2
    assert state["reads"] == {"setup-1": 2, "setup-2": 2}
    assert not any(request.url.path.endswith("/runs") for request in seen)
    assert read_selection(manifest, directory) == selected
    assert prepare(manifest, directory) == selected
    assert len(state["operations"]) == 2
    driver = FinOpsDriver(selected, directory)
    try:
        assert driver.submit_once() == "run-example"
        with pytest.raises(ValueError, match="already used"):
            driver.submit_once()
    finally:
        driver.close()
    assert len([request for request in seen if request.url.path.endswith("/runs")]) == 1
    altered = deepcopy(manifest)
    altered["input"]["question"] = "Different inquiry"
    with pytest.raises(ValueError, match="authorized inquiry"):
        read_selection(altered, directory)


def test_uncertain_setup_is_not_resubmitted(first_install):
    manifest, directory, state, seen, _ = first_install
    state["uncertain"] = True
    with pytest.raises(httpx.ReadTimeout):
        prepare(manifest, directory)
    with pytest.raises(FileExistsError):
        prepare(manifest, directory)
    assert len(state["operations"]) == 1
    assert not any(request.url.path.endswith("/runs") for request in seen)
    entries = [json.loads(line) for line in next(directory.glob("*-setup.jsonl")).read_text().splitlines()]
    assert entries[-1]["state"] == "prepared" and entries[-1]["operation"] == "link"


@pytest.mark.parametrize("denial", ("session", "connection", "capability"))
def test_setup_denies_unavailable_authority_before_mutation(first_install, denial):
    manifest, directory, state, seen, options = first_install
    if denial == "session":
        state["deny"] = True
    else:
        options["connections" if denial == "connection" else "capabilities"] = []
    with pytest.raises(ValueError):
        prepare(manifest, directory)
    assert all(request.method == "GET" for request in seen)
    assert not list(directory.glob("*.jsonl"))


@pytest.mark.parametrize("mismatch", ("scope", "native"))
def test_setup_cannot_turn_wrong_binding_readback_into_native_admission(first_install, case, mismatch):
    manifest, directory, state, seen, _ = first_install
    binding = case[6]
    if mismatch == "scope":
        binding["scope"] = {**binding["scope"], "tenant_id": "foreign-tenant"}
    else:
        binding["native_resource_ref"] = {**binding["native_resource_ref"], "record_id": "foreign-agent"}
    with pytest.raises(ValueError, match="Product configuration or native binding"):
        prepare(manifest, directory)
    assert not list(directory.glob("*-selection.json"))
    assert not any(request.url.path.endswith("/runs") for request in seen)


def test_setup_rejects_reused_receipt_before_any_request(first_install):
    manifest, directory, state, seen, _ = first_install
    manifest["bootstrap"]["receipts"]["link"] = manifest["receipt"]
    with pytest.raises(ValueError, match="distinct durable receipts"):
        prepare(manifest, directory)
    assert not seen and not list(directory.glob("*.jsonl"))


@pytest.mark.parametrize("field", ("binding_id", "configuration_revision"))
def test_bootstrap_requires_unresolved_identifiers_to_be_omitted(first_install, field):
    manifest, directory, state, seen, _ = first_install
    manifest["input"][field] = None
    with pytest.raises(ValueError, match="explicit Product setup selection"):
        prepare(manifest, directory)
    assert not seen and not list(directory.glob("*.jsonl"))


def test_portable_fixture_uses_real_cli_registry_without_replacing_system_home(case, monkeypatch):
    manifest, directory, client, *rest = case
    stable_id = "d" + "a" * 16
    manifest["clients"] = {
        "api_tokens": {key: "selected-product-session" for key in ("api_token_env", "readback_token_env", "mcp_token_env")},
        "cli_registry": {"schema_version": 1, "default": "example", "deployments": {
            "example": {"id": stable_id, "gateway_url": manifest["gateway_url"] + "/api"}}},
        "cli_config": {"gateway_url": manifest["gateway_url"] + "/api"},
        "cli_tokens": {"access_token": "selected-cli-session", "refresh_token": "selected-refresh"},
        "browser_storage_state": {"cookies": [], "origins": []},
    }
    for key in ("cli_home", "cli_executable", "cli_sha256", "browser_storage_state", "api_token_env",
                "readback_token_env", "mcp_token_env"):
        manifest.pop(key)
    manifest["release_sha"] = "a" * 40
    selected = materialize_clients(manifest, directory)
    monkeypatch.setenv("HOME", str(directory / "runner-home"))
    monkeypatch.setenv("BG_CONFIG_DIR", "/unrelated/credentials")
    monkeypatch.setenv("ADP_HOME", "/unrelated/registry")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-inherit")
    def external(command, **kwargs):
        response = run_process(command, **kwargs)
        assert response.returncode == 0, response.stderr.decode()
        return response

    monkeypatch.setattr("driver.subprocess.run", external)
    driver = FinOpsDriver(selected, directory, client=client, mode="preflight")
    try:
        driver.check_cli_target()
        environment = driver.client_environment()
        assert environment["HOME"] == os.environ["HOME"]
        assert environment["BG_CONFIG_DIR"] == str(Path(selected["cli_home"]) / ".adp/deployments" / stable_id)
        assert "UNRELATED_SECRET" not in environment
        assert driver.request("GET", driver.path + "/configuration/options")["service_id"] == "finops"
        assert rest[-1][-1].headers["Authorization"] == "Bearer selected-product-session"
        assert not Path(os.environ["HOME"]).exists()
        assert materialize_clients(manifest, directory) == selected
        tokens = Path(selected["cli_config_dir"]) / "tokens.json"
        assert tokens.stat().st_mode & 0o077 == 0
        tokens.write_text('{"access_token":"refreshed-session"}')
        materialize_clients(manifest, directory)
        assert "refreshed-session" in tokens.read_text()
        manifest["clients"]["cli_registry"]["deployments"]["example"]["gateway_url"] = "https://foreign.example.org/api"
        with pytest.raises(ValueError, match="selected gateway"):
            materialize_clients(manifest, directory)
    finally:
        driver.close()
