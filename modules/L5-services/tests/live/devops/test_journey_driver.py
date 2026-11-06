"""Synthetic first-party client fixtures; never contact Product or AWS."""

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from journey_driver import FirstPartyDriver, drive_journeys, validate_driver, verify_driven_journeys
from selector import qualify_journeys
from test_selector import FakeReader, NOW, offline_drive, offline_policy, selector_case


class StubClient:
    def __init__(self, manifest, directory, *, deadline, max_requests):
        self.manifest = manifest
        self.directory = directory
        self.receipts = []
        self.closed = False

    def journal(self, entry, status, receipt, run_id=None):
        self.receipts.append((entry, status, receipt, run_id))

    def close(self):
        self.closed = True

    def check_cli_target(self):
        return None

    def run_product(self):
        return {"run_id": "run-product-example"}

    def run_cli(self):
        return {"run_id": "run-cli-example"}

    def run_mcp(self):
        return {"run_id": "run-mcp-example"}

    def run_ui(self):
        return {"run_id": "run-ui-example", "receipt": "ui-generated-receipt-example", "native_handoff": True}

    def read_native(self, run_id):
        return run_id.replace("run-", "native-")


def test_drives_four_first_party_operations_before_accepting_readbacks(selector_case):
    manifest, path, directory, release_reader = selector_case
    drivers = []
    def runner(config, destination, *, deadline):
        def factory(*args, **kwargs):
            instance = StubClient(*args, **kwargs)
            drivers.append(instance)
            return instance
        return drive_journeys(config, destination, deadline=deadline, driver_factory=factory)
    result = qualify_journeys(manifest["release"]["source_sha"], path, directory,
        policy_reader=offline_policy, journey_runner=runner,
        reader_factory=lambda config, **bounds: FakeReader(config, release_reader, **bounds), now=NOW)
    saved = json.loads((directory / f"devops-{result}-journeys.json").read_text())
    assert len(saved["driven_journeys"]) == 4
    assert [entry for entry, status, _, _ in drivers[0].receipts if status == "verified"] == [
        "product", "cli", "mcp", "ui"]
    assert drivers[0].receipts[-2][2] == "ui-generated-receipt-example"
    assert drivers[0].closed
    with pytest.raises(FileExistsError):
        runner(manifest, directory, deadline=time.monotonic() + 20)


@pytest.mark.parametrize("change", [
    lambda manifest: manifest.pop("journey_driver"),
    lambda manifest: manifest["journey_driver"]["scope"].update(project_id="foreign"),
    lambda manifest: manifest["journey_driver"].update(cli_sha256="0" * 64),
    lambda manifest: manifest["journey_driver"]["inputs"]["ui"].update(binding_id="foreign"),
    lambda manifest: manifest["journey_driver"]["receipts"].update(cli=manifest["journey_driver"]["receipts"]["mcp"]),
    lambda manifest: manifest["journey_driver"].update(mcp_token_env="DEVOPS_PRODUCT_TOKEN"),
])
def test_refuses_unpinned_clients_wrong_scope_or_duplicate_receipts_before_effect(selector_case, change):
    manifest, path, directory, release_reader = selector_case
    change(manifest)
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        qualify_journeys(manifest["release"]["source_sha"], path, directory,
            policy_reader=offline_policy, journey_runner=lambda *args, **kwargs: pytest.fail("action started"), now=NOW)
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("change", [
    lambda items: items[0].pop("run_id"),
    lambda items: items[0].update(native_id="older-native"),
    lambda items: items[0].update(scope={"product_id": "foreign"}),
    lambda items: items[0]["usage"].update(requests=99),
    lambda items: items[0]["usage"].update(spend_usd="NaN"),
    lambda items: items[0].pop("usage"),
    lambda items: items[0]["usage"].pop("spend_usd"),
    lambda items: items[0].update(status="partial"),
])
def test_refuses_unlinked_native_partial_foreign_and_unbounded_results(selector_case, change):
    manifest, path, directory, release_reader = selector_case
    class WrongReadback(FakeReader):
        def read_journeys(self, target):
            items = super().read_journeys(target)
            change(items)
            return items
    with pytest.raises(ValueError):
        qualify_journeys(manifest["release"]["source_sha"], path, directory,
            policy_reader=offline_policy, journey_runner=offline_drive,
            reader_factory=lambda config, **bounds: WrongReadback(config, release_reader, **bounds), now=NOW)
    assert not (directory / f"devops-{manifest['authorization']['run_id']}-journeys.json").exists()


def test_aggregate_spend_rejected_even_when_each_run_is_within_limit(selector_case):
    manifest, path, directory, reader = selector_case
    items = FakeReader(manifest, reader, deadline=1, max_requests=10).read_journeys(manifest["target_ref"])
    for item in items:
        item["usage"]["spend_usd"] = "0.10"
    with pytest.raises(ValueError, match="shared capability budget"):
        verify_driven_journeys(manifest, items, offline_drive(manifest, directory, deadline=1))


def test_uncertain_submission_keeps_private_journal_and_cannot_replay(selector_case):
    manifest, path, directory, reader = selector_case
    class Uncertain(StubClient):
        def run_cli(self):
            raise TimeoutError("response lost")
        def journal(self, entry, status, receipt, run_id=None):
            super().journal(entry, status, receipt, run_id)
            with (directory / f"devops-{manifest['authorization']['run_id']}-journey-actions.jsonl").open("a") as sink:
                sink.write(json.dumps({"entry_point": entry, "status": status, "receipt": receipt}) + "\n")
    with pytest.raises(TimeoutError, match="lost"):
        drive_journeys(manifest, directory, deadline=time.monotonic() + 20, driver_factory=Uncertain)
    journal = directory / f"devops-{manifest['authorization']['run_id']}-journey-actions.jsonl"
    assert journal.stat().st_mode & 0o077 == 0
    assert '"uncertain"' in journal.read_text()
    with pytest.raises(FileExistsError):
        drive_journeys(manifest, directory, deadline=time.monotonic() + 20, driver_factory=Uncertain)


def test_real_first_party_transports_use_scoped_api_mcp_and_installed_cli(selector_case, monkeypatch):
    from test_installed_results import installed_result_response

    manifest, path, directory, reader = selector_case
    driver = FirstPartyDriver(manifest, directory, deadline=time.monotonic() + 20, max_requests=12)
    seen = []
    latest_run = {}
    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/mcp/tools/call"):
            return httpx.Response(200, json={"structuredContent": {"http_status": 202,
                "data": {"run_id": "run-mcp-example"}}})
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "run-product-example"})
        if latest_run:
            result_response = installed_result_response(request, latest_run)
            if result_response is not None:
                return result_response
        latest_run.update({"run_id": "run-product-example", "state": "succeeded",
            "scope": manifest["journey_driver"]["scope"], "origin": "adp_submitted",
            "input": manifest["journey_driver"]["inputs"]["product"], "provider_state": "completed",
            "admission": {"provider_identity": {key: manifest["journey_driver"]["provider_readback"][key]
                                              for key in ("account_id", "role_arn", "region")},
                          "connection_ref": {"source_connection_id": "example-source"},
                          "native_space_ref": {"source_connection_id": "example-source",
                                               "project_id": "example", "record_type": "agent_space",
                                               "record_id": "space-example"}},
            "native_job_ref": {"record_type": "native_job", "record_id": "native-product-example",
                               "project_id": manifest["journey_driver"]["scope"]["project_id"],
                               "source_connection_id": "example-source", "record_revision": "task-example"}})
        return httpx.Response(200, json=latest_run)
    monkeypatch.setattr("journey_driver.read_provider_task", lambda *args, **kwargs: {
        "agentSpaceId": "space-example", "taskId": "task-example", "executionId": "native-product-example",
        "status": "COMPLETED", "taskType": "INVESTIGATION"})
    driver.client.close()
    driver.client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    try:
        assert driver.run_product()["run_id"] == "run-product-example"
        assert driver.run_mcp()["run_id"] == "run-mcp-example"
        assert driver.read_native("run-product-example") == "native-product-example"
        assert seen[0].url.path.endswith("/services/devops/runs")
        assert seen[0].headers["Idempotency-Key"] == manifest["journey_driver"]["receipts"]["product"]
        assert seen[1].url.path == "/api/v1/l5/mcp/tools/call"
        assert json.loads(seen[1].content)["tool"] == "l5___submit_run"
        assert seen[2].url.path.endswith("/services/devops/runs/run-product-example")
        invoked = []
        def fake_run(command, **kwargs):
            invoked.append((command, kwargs))
            if command[0] == "node":
                return SimpleNamespace(returncode=0, stdout=b'{"run_id":"run-ui-example","receipt":"ui-generated-receipt-example"}')
            if command[2:4] == ["resolve", "--deployment"]:
                return SimpleNamespace(returncode=0, stdout=b'{"deployment":"example","gateway_url":"https://gateway.example.org/api"}')
            return SimpleNamespace(returncode=0, stdout=b'{"status":"ok","detail":{"run_id":"run-cli-example"}}')
        monkeypatch.setattr("journey_driver.subprocess.run", fake_run)
        driver.check_cli_target()
        assert invoked[0][0][1] == str(directory.parent / "adp_deployments.py")
        assert invoked[0][0][2:5] == ["resolve", "--deployment", "example"]
        assert driver.run_cli()["run_id"] == "run-cli-example"
        assert invoked[1][0][:5] == [manifest["journey_driver"]["cli_executable"], "--deployment",
                                      "example", "service", "run"]
        assert invoked[1][1]["env"]["HOME"] == manifest["journey_driver"]["cli_home"]
        assert driver.run_ui()["run_id"] == "run-ui-example"
        assert invoked[2][0][0] == "node"
        assert json.loads(invoked[2][1]["input"])["scope"] == manifest["journey_driver"]["scope"]
    finally:
        driver.close()


def test_installed_resolver_pins_cli_gateway_before_any_submission(selector_case):
    manifest, _, directory, _ = selector_case
    resolver = Path(manifest["journey_driver"]["cli_executable"]).with_name("adp_deployments.py")
    source = Path(__file__).resolve().parents[4] / "gateway/cli/adp_deployments.py"
    resolver.write_bytes(source.read_bytes())
    driver = FirstPartyDriver(manifest, directory, deadline=time.monotonic() + 20, max_requests=12)
    try:
        registered = subprocess.run([sys.executable, str(resolver), "add", "example", "--url",
                                     "https://gateway.example.org"], env=driver.cli_environment(),
                                    capture_output=True, timeout=10)
        assert registered.returncode == 0
        driver.check_cli_target()
        manifest["journey_driver"]["gateway_url"] = "https://other.example.org"
        with pytest.raises(ValueError, match="different gateway"):
            driver.check_cli_target()
    finally:
        driver.close()


def test_wrong_installed_cli_deployment_stops_before_all_native_actions(selector_case):
    manifest, path, directory, reader = selector_case
    class WrongGateway(StubClient):
        def check_cli_target(self):
            raise ValueError("installed CLI is connected to a different gateway")
        def run_product(self):
            pytest.fail("side effect before installed CLI target check")
    with pytest.raises(ValueError, match="different gateway"):
        drive_journeys(manifest, directory, deadline=time.monotonic() + 20,
                       driver_factory=WrongGateway)
    assert not any(directory.glob("*journeys.json"))


def test_native_polling_rejects_foreign_and_partial_runs(selector_case):
    manifest, path, directory, reader = selector_case
    driver = FirstPartyDriver(manifest, directory, deadline=time.monotonic() + 20, max_requests=3)
    driver.client.close()
    for value in ({"state": "succeeded", "scope": {"product_id": "foreign", "project_id": "example"},
                   "native_job_ref": {"job_id": "native-example"}},
                  {"state": "partial", "scope": manifest["journey_driver"]["scope"]}):
        driver.calls = 0
        driver.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200,
            json={"run_id": "run-example", **value})), follow_redirects=False)
        with pytest.raises(ValueError):
            driver.read_native("run-example")
        driver.client.close()


@pytest.mark.parametrize("wrong", [
    {"origin": "native"},
    {"input": {"capability": "release_readiness"}},
    {"provider_state": "unknown"},
    {"native_job_ref": {"record_type": "native_job", "record_id": "other", "project_id": "foreign"}},
    {"native_job_ref": {"record_type": "unverified", "record_id": "other", "project_id": "example"}},
])
def test_polled_run_rejects_unrelated_or_unverified_native_result(selector_case, monkeypatch, wrong):
    manifest, _, directory, _ = selector_case
    driver = FirstPartyDriver(manifest, directory, deadline=time.monotonic() + 20, max_requests=3)
    scope = manifest["journey_driver"]["scope"]
    record = {"run_id": "run-example", "state": "succeeded", "scope": scope,
              "origin": "adp_submitted", "input": manifest["journey_driver"]["inputs"]["product"],
              "provider_state": "completed", "native_job_ref": {"record_type": "native_job",
              "record_id": "native-example", "project_id": scope["project_id"]}}
    monkeypatch.setattr("journey_driver.read_provider_task", lambda *args, **kwargs:
                        pytest.fail("unverified Gateway record reached the provider"))
    driver.client.close()
    driver.client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={**record, **wrong})), follow_redirects=False)
    try:
        with pytest.raises(ValueError):
            driver.read_native("run-example")
    finally:
        driver.close()


def test_browser_submission_without_native_handoff_is_not_accepted(selector_case):
    manifest, path, directory, reader = selector_case
    class EmptyBrowser(StubClient):
        def run_ui(self):
            return {"run_id": "run-ui-example", "receipt": "ui-generated-receipt-example"}
    with pytest.raises(ValueError, match="native UI"):
        drive_journeys(manifest, directory, deadline=time.monotonic() + 20,
                       driver_factory=EmptyBrowser)
    assert not any(directory.glob("*journeys.json"))
