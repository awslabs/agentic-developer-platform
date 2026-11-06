"""Service-owned dispatch retains incomplete qualification without inventing proof."""

import importlib
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from unittest.mock import Mock
import test_context_selector as context_fixtures
import test_portability_selector as portability_fixtures
import test_recovery_selector as recovery_fixtures
import test_selector as selector_fixtures

live_context = context_fixtures.live_context
live_acceptance = context_fixtures.live_acceptance
portable_selection = portability_fixtures.portable_selection
recovery_selection = recovery_fixtures.recovery_selection
selector_case = selector_fixtures.selector_case


@pytest.fixture
def driver_case(live_context, live_acceptance, portable_selection, recovery_selection, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    driver = importlib.import_module("devops_qualification_driver")
    manifest, path, directory, _ = recovery_selection
    manifest["authorization"]["expires_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    recovery = manifest["recovery"]
    manifest["journey_driver"]["scope"] = {**{key: recovery["scope"][key] for key in ("product_id", "project_id")},
                                           "binding_id": recovery["binding_id"]}
    for body in manifest["journey_driver"]["inputs"].values():
        body["binding_id"] = recovery["binding_id"]
    path.write_text(json.dumps(manifest))
    for key in ("ADP_CORRELATION", "ADP_SOURCE_REVISION", "ADP_DEFINITION_REVISION", "ADP_CONTRACT_DIGEST",
                "SOURCE_SHA", "DEFINITION_SHA",
                "TARGET_ENVIRONMENT", "CORRELATION_ID", "CONTRACT_DIGEST"):
        monkeypatch.delenv(key, raising=False)
    environment = {"L5_SERVICE": "devops", "L5_ENVIRONMENT": "test", "GITHUB_REPOSITORY": "example/platform",
                   "GITHUB_REPOSITORY_ID": "42", "GITHUB_RUN_ID": "40700000001", "GITHUB_RUN_ATTEMPT": "1",
                   "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
                   "GITHUB_WORKFLOW_REF": "example/platform/.github/workflows/l5-services-release.yml@refs/heads/main",
                   "GITHUB_WORKFLOW_SHA": "d" * 40, "REVIEWED_SOURCE_SHA": manifest["release"]["source_sha"],
                   "L5_CHECKOUT_REVISION": manifest["release"]["source_sha"]}
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    return driver, manifest, path, directory


def test_shared_release_preflight_has_no_actions_or_evidence_writes(driver_case, monkeypatch):
    driver, manifest, path, directory = driver_case
    source = manifest["release"]["source_sha"]
    before = set(directory.rglob("*"))
    environment = dict(os.environ)
    actions = {name: Mock(side_effect=AssertionError("preflight must not run a selector")) for name in driver.SELECTORS}
    monkeypatch.setattr(driver, "SELECTORS", actions)
    spec = importlib.util.spec_from_file_location("devops_release_entry", Path(driver.__file__).with_name("qualify-devops-release.py"))
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    assert entry.main(["preflight", source, str(path), str(directory)]) == 0
    assert set(directory.rglob("*")) == before and dict(os.environ) == environment
    assert all(action.call_count == 0 for action in actions.values())
    monkeypatch.setenv("SOURCE_SHA", "f" * 40)
    with pytest.raises(ValueError, match="identity differs"):
        entry.main(["preflight", source, str(path), str(directory)])


def test_shared_release_run_preserves_driver_exit_and_observation_selection(driver_case, monkeypatch):
    driver, manifest, path, directory = driver_case
    spec = importlib.util.spec_from_file_location("devops_release_entry", Path(driver.__file__).with_name("qualify-devops-release.py"))
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    run = Mock(return_value=1)
    monkeypatch.setattr(entry, "run_all", run)
    assert entry.main(["run", manifest["release"]["source_sha"], str(path), str(directory),
                       "--observations", "/private/observations.json"]) == 1
    run.assert_called_once_with(manifest["release"]["source_sha"], str(path), str(directory),
                                observations="/private/observations.json")


def test_preflight_accepts_planned_setup_without_waiting_for_consumers(driver_case, monkeypatch):
    driver, manifest, path, directory = driver_case
    manifest["journey_driver"]["scope"]["binding_id"] = "pending-setup"
    for body in manifest["journey_driver"]["inputs"].values():
        body["binding_id"] = "pending-setup"
    manifest["recovery"]["binding_id"] = "pending-setup"
    manifest["ingress_policy"]["launch_path"] = (
        f"/api/v1/products/{manifest['journey_driver']['scope']['product_id']}/projects/"
        f"{manifest['journey_driver']['scope']['project_id']}/services/devops/bindings/pending-setup/browser-launch")
    manifest["installed_setup"] = {"tenant_id": "example-tenant", "request": {"action": "create", "configuration_revision": 1},
                                   "receipt": "example-first-setup-00001", "max_requests": 30, "deadline_seconds": 300}
    path.write_text(json.dumps(manifest))
    prepared = Mock(side_effect=AssertionError("paused consumers must not be awaited during preflight"))
    monkeypatch.setattr(driver, "prepare_binding", prepared)
    assert driver.preflight(manifest["release"]["source_sha"], path, directory)[1] == manifest
    prepared.assert_not_called()
    assert list(directory.iterdir()) == []


def test_failed_setup_retains_receipt_and_never_starts_qualification(driver_case, monkeypatch):
    driver, manifest, path, directory = driver_case
    manifest["installed_setup"] = {"tenant_id": "example-tenant", "request": {"action": "check", "configuration_revision": 1,
                                   "binding_id": manifest["journey_driver"]["scope"]["binding_id"]},
                                   "receipt": "example-first-setup-00001", "max_requests": 30, "deadline_seconds": 300, "if_match": '"1"'}
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(driver, "prepare_binding", Mock(side_effect=TimeoutError("private response")))
    selectors = {name: Mock(side_effect=AssertionError("setup must finish first")) for name in driver.SELECTORS}
    monkeypatch.setattr(driver, "SELECTORS", selectors)
    assert driver.run_all(manifest["release"]["source_sha"], path, directory) == 1
    assert all(not selector.called for selector in selectors.values())
    attempt = directory / manifest["authorization"]["run_id"]
    report = json.loads((attempt / "driver.json").read_text())
    assert report["attempted_selectors"] == [] and report["failures"] == [{"selector": "setup", "error_type": "TimeoutError"}]
    assert json.loads((attempt / "manifest.json").read_text())["installed_setup"] == manifest["installed_setup"]


@pytest.fixture
def transported_clients(driver_case, monkeypatch):
    import hashlib
    from devops_private_fixture import token_names
    from qualification import ROOT

    driver, manifest, path, directory = driver_case
    manifest["journey_driver"]["cli_sha256"] = hashlib.sha256((ROOT / "modules/gateway/cli/adp").read_bytes()).hexdigest()
    environment = {name: os.environ[name] for name in token_names(manifest)}
    manifest["private_clients"] = {
        "version": 1, "environment": environment,
        "cli_config": {"gateway_url": manifest["journey_driver"]["gateway_url"] + "/api", "client_id": "example-client"},
        "cli_tokens": {"access_token": "example-maintained-cli-session", "refresh_token": "example-refresh"},
        "browser_state": {"cookies": [], "origins": [{"origin": manifest["journey_driver"]["gateway_url"], "localStorage": []}]},
        "recovery_browser_states": {user["id"]: {"cookies": [], "origins": []} for user in manifest["recovery"]["users"]},
    }
    for name in environment:
        monkeypatch.delenv(name)
    path.write_text(json.dumps(manifest))
    return driver, manifest, path, directory


def test_preflight_materializes_only_local_product_sessions_and_preserves_refresh(transported_clients):
    driver, manifest, path, directory = transported_clients
    from journey_driver import FirstPartyDriver

    _, hydrated = driver.preflight(manifest["release"]["source_sha"], path, directory)
    assert "private_clients" not in hydrated
    assert list(directory.iterdir()) == []
    client = hydrated["journey_driver"]
    home = Path(client["cli_home"])
    assert Path(client["cli_executable"]).is_relative_to(home)
    assert json.loads(Path(client["browser_storage_state"]).read_text()) == manifest["private_clients"]["browser_state"]
    assert len(set(hydrated["recovery"]["browser"]["storage_states"].values())) == 2
    assert all(Path(value).is_file() for value in hydrated["recovery"]["browser"]["storage_states"].values())
    tokens = next(home.glob(".adp/deployments/*/tokens.json"))
    assert json.loads(tokens.read_text()) == manifest["private_clients"]["cli_tokens"]
    assert tokens.stat().st_mode & 0o077 == 0
    assert all(os.environ[name] == value for name, value in manifest["private_clients"]["environment"].items())
    target = FirstPartyDriver(hydrated, directory, deadline=time.monotonic() + 30, max_requests=1)
    try:
        target.check_cli_target()
        assert target.calls == 0
    finally:
        target.close()
    tokens.write_text(json.dumps({"access_token": "example-refreshed-session"}))
    assert driver.preflight(manifest["release"]["source_sha"], path, directory)[1] == hydrated
    assert json.loads(tokens.read_text())["access_token"] == "example-refreshed-session"
    manifest["private_clients"]["cli_tokens"]["access_token"] = "another-session"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="another fixture"):
        driver.preflight(manifest["release"]["source_sha"], path, directory)


@pytest.mark.parametrize("boundary", ["foreign_gateway", "foreign_browser", "process_environment", "unreferenced", "cli_pin", "symlink"])
def test_private_client_transport_rejects_scope_and_environment_changes(transported_clients, boundary, tmp_path):
    driver, manifest, path, directory = transported_clients
    bundle = manifest["private_clients"]
    if boundary == "foreign_gateway":
        bundle["cli_config"]["gateway_url"] = "https://foreign.example.org/api"
    elif boundary == "foreign_browser":
        bundle["browser_state"]["origins"][0]["origin"] = "https://foreign.example.org"
    elif boundary == "process_environment":
        manifest["journey_driver"]["api_token_env"] = "PATH"
        bundle["environment"]["PATH"] = "/untrusted"
    elif boundary == "unreferenced":
        bundle["environment"]["DEVOPS_UNREFERENCED_TOKEN"] = "not-selected"
    elif boundary == "cli_pin":
        manifest["journey_driver"]["cli_sha256"] = "0" * 64
    elif boundary == "symlink":
        destination = tmp_path / "foreign-state"
        destination.mkdir(mode=0o700)
        (path.parent / "private-clients").symlink_to(destination, target_is_directory=True)
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        driver.preflight(manifest["release"]["source_sha"], path, directory)
    assert not (path.parent / "private-clients/home").exists()


@pytest.mark.parametrize("failure", [None, "journeys", "recovery"])
def test_driver_attempts_all_five_and_emits_native_unresolved_records(driver_case, monkeypatch, failure):
    driver, manifest, path, directory = driver_case
    called = []
    run_id = manifest["authorization"]["run_id"]

    def selector(name):
        def run(source_sha, selected_manifest, evidence):
            assert source_sha == manifest["release"]["source_sha"]
            assert json.loads(Path(selected_manifest).read_text()) == manifest
            assert Path(evidence) == directory / run_id
            called.append(name)
            if name == failure:
                raise RuntimeError("private failure details must not appear in the driver receipt")
            return run_id
        return run

    monkeypatch.setattr(driver, "SELECTORS", {name: selector(name) for name in driver.SELECTORS})
    assert driver.run_all(manifest["release"]["source_sha"], path, directory) == 1
    assert called == ["journeys", "recovery", "context", "portability", "acceptance"]
    attempt = directory / run_id
    report = json.loads((attempt / "l5-devops-qualification/qualification.json").read_text())
    delivery = json.loads((attempt / "delivery-evidence.json").read_text())
    assert report["requirements_revision"] == "R3-secure-native-ui-ingress"
    assert report["source_revision"] == manifest["release"]["source_sha"]
    assert report["definition_revision"] == "d" * 40 and report["run_id"] == "40700000001"
    assert len(report["epic_criteria"]) == 10 and len(report["story_criteria"]) == len(report["scenarios"]) == 63
    assert {record["id"] for record in report["live_checks"]} == set(driver.qualification_devops.LIVE_CHECKS)
    assert report["unresolved"] and delivery["unresolved"]
    assert delivery["qualification_artifact"] == "l5-devops-qualification/qualification.json"
    receipt = json.loads((attempt / "driver.json").read_text())
    assert receipt["failures"] == ([{"selector": failure, "error_type": "RuntimeError"}] if failure else [])
    assert "private failure details" not in (attempt / "driver.json").read_text()
    assert all(item.stat().st_mode & 0o077 == 0 for item in attempt.rglob("*") if item.is_file())
    with pytest.raises(FileExistsError):
        driver.run_all(manifest["release"]["source_sha"], path, directory)
    assert len(called) == 5


def test_driver_emits_native_contract_for_complete_synthetic_observations(driver_case, monkeypatch):
    driver, manifest, path, directory = driver_case
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "deployment"))
    fixtures = importlib.import_module("test_devops_qualification_rollup")
    manifest["release"] = fixtures.release()
    manifest["authorization"].update(source_sha=fixtures.SOURCE_SHA, run_id=fixtures.RUN_ID)
    path.write_text(json.dumps(manifest))
    monkeypatch.setenv("REVIEWED_SOURCE_SHA", fixtures.SOURCE_SHA)
    monkeypatch.setenv("L5_CHECKOUT_REVISION", fixtures.SOURCE_SHA)
    contract = driver.qualification_devops.accepted_contract()
    summaries = fixtures.summaries(*contract)
    observations = path.parent / "independent-observations.json"
    observations.write_text(json.dumps(fixtures.independent_observations(driver.qualification_devops, contract)))
    observations.chmod(0o600)

    def selector(name):
        def run(source_sha, selected_manifest, evidence):
            (Path(evidence) / f"{name}.json").write_text(json.dumps(summaries[name]))
            return fixtures.RUN_ID
        return run

    monkeypatch.setattr(driver, "SELECTORS", {name: selector(name) for name in driver.SELECTORS})
    assert driver.run_all(fixtures.SOURCE_SHA, path, directory, observations=observations) == 0
    report = json.loads((directory / fixtures.RUN_ID / "l5-devops-qualification/qualification.json").read_text())
    assert report["unresolved"] == []
    assert report["source_revision"] == fixtures.SOURCE_SHA and report["definition_revision"] == "d" * 40
    assert all(record["evidence_verified"] for record in report["story_criteria"])
    monkeypatch.setattr(driver, "SELECTORS", {name: Mock(side_effect=AssertionError("emission must not run native work"))
                                             for name in driver.SELECTORS})
    spec = importlib.util.spec_from_file_location("devops_release_emitter", Path(driver.__file__).with_name("emit-devops-qualification.py"))
    emitter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emitter)
    assert emitter.main([fixtures.SOURCE_SHA, str(path), str(directory), "--observations", str(observations)]) == 0
    for selector in driver.SELECTORS.values():
        selector.assert_not_called()
    manifest["target_ref"] = "foreign-target"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="selected attempt"):
        emitter.main([fixtures.SOURCE_SHA, str(path), str(directory), "--observations", str(observations)])
    manifest["target_ref"] = json.loads((directory / fixtures.RUN_ID / "manifest.json").read_text())["target_ref"]
    path.write_text(json.dumps(manifest))
    receipt_path = directory / fixtures.RUN_ID / "driver.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["release_identity"]["definition_revision"] = "f" * 40
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="selected attempt"):
        emitter.main([fixtures.SOURCE_SHA, str(path), str(directory), "--observations", str(observations)])


def test_shared_dispatch_selects_real_devops_entry_points(driver_case):
    driver, *_ = driver_case
    scripts = Path(driver.__file__).parent
    spec = importlib.util.spec_from_file_location("devops_shared_dispatch", scripts / "qualification-dispatch.py")
    dispatch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatch)
    assert dispatch.check_driver("devops") == ("qualify-devops-release.py", "emit-devops-qualification.py")


@pytest.mark.parametrize("mutation", ["source", "scenario", "readback", "service", "definition"])
def test_driver_preflight_rejects_wrong_scope_before_any_selector(driver_case, monkeypatch, mutation):
    driver, manifest, path, directory = driver_case
    if mutation == "source":
        manifest["release"]["source_sha"] = "f" * 40
    elif mutation == "scenario":
        manifest["authorization"]["scenarios"].remove("acceptance")
    elif mutation == "readback":
        manifest["context"]["readback_channels"].pop("result")
    elif mutation == "service":
        monkeypatch.setenv("L5_SERVICE", "security")
    else:
        monkeypatch.setenv("DEFINITION_SHA", "f" * 40)
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(driver, "SELECTORS", {name: lambda *args: pytest.fail("preflight must precede effects")
                                            for name in driver.SELECTORS})
    with pytest.raises(ValueError):
        driver.run_all(manifest["release"]["source_sha"], path, directory)
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("failed_journeys", [False, True])
def test_installed_journey_handoff_uses_only_this_attempts_run_ids(driver_case, monkeypatch, failed_journeys):
    driver, manifest, path, directory = driver_case
    run_id = manifest["authorization"]["run_id"]
    entries = ("product", "ui", "cli", "mcp")
    runs = {entry: f"current-{entry}" for entry in entries}
    manifest["installed_journeys"] = {"token_env": "DEVOPS_INSTALLED_READ_TOKEN", "tenant_id": "tenant-example",
        "started_at": (datetime.now(UTC) - timedelta(seconds=30)).isoformat(),
        "runs": {entry: f"old-{entry}" for entry in entries}}
    manifest["readback_channels"].pop("journeys")
    monkeypatch.setenv("DEVOPS_INSTALLED_READ_TOKEN", "independent-installed-read-token")
    path.write_text(json.dumps(manifest))
    called = []

    def selector(name):
        def run(source_sha, selected_manifest, evidence):
            selected = json.loads(Path(selected_manifest).read_text())["installed_journeys"]
            called.append(name)
            if name == "journeys":
                assert "runs" not in selected
                if failed_journeys:
                    raise ValueError("journey did not complete")
                driver.qualification_devops.shared_rollup().write_private(
                    Path(evidence) / f"devops-{run_id}-journeys.json", {
                        "authorization_run_id": run_id,
                        "driven_journeys": {entry: {"run_id": value} for entry, value in runs.items()}})
            elif failed_journeys:
                assert "runs" not in selected
            else:
                assert selected["runs"] == runs
                assert Path(selected_manifest).name == "manifest-observed.json"
            return run_id
        return run

    monkeypatch.setattr(driver, "SELECTORS", {name: selector(name) for name in driver.SELECTORS})
    assert driver.run_all(manifest["release"]["source_sha"], path, directory) == 1
    assert called == ["journeys", "recovery", "context", "portability", "acceptance"]
    attempt = directory / run_id
    assert (attempt / "manifest-observed.json").exists() is not failed_journeys
    receipt = json.loads((attempt / "driver.json").read_text())
    assert receipt["failures"] == ([{"selector": "journeys", "error_type": "ValueError"}] if failed_journeys else [])
    report = json.loads((attempt / "l5-devops-qualification/qualification.json").read_text())
    assert report["unresolved"]
