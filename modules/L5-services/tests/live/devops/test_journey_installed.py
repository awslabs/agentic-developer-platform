"""Installed first-party journey and independent native readback boundary."""

import json
import time
from types import SimpleNamespace

import httpx
import pytest

from journey_driver import FirstPartyDriver, drive_journeys
from test_selector import selector_case as selector_case
from test_installed_results import installed_result_response


@pytest.mark.parametrize("provider_failure", [None, "mcp"])
@pytest.mark.parametrize("capability,task_type", [
    ("investigation", "INVESTIGATION"),
    ("release_readiness", "RELEASE_READINESS_REVIEW"),
    ("release_verification", "RELEASE_TESTING"),
])
def test_four_scoped_clients_require_provider_proof_before_next_action(selector_case, monkeypatch, provider_failure,
                                                                        capability, task_type):
    manifest, _, directory, _ = selector_case
    for selected_input in manifest["journey_driver"]["inputs"].values():
        selected_input["capability"] = capability
        selected_input["devops_input"]["kind"] = capability
    selected = manifest["journey_driver"]["provider_readback"]
    scope = manifest["journey_driver"]["scope"]
    identity = {field: selected[field] for field in ("account_id", "role_arn", "region")}
    submissions = []
    provider_reads = []
    run_reads = []
    drivers = []
    latest_run = {}

    def transport(request):
        if request.method == "POST":
            if request.url.path.endswith("/mcp/tools/call"):
                submissions.append("mcp")
                assert json.loads(request.content)["arguments"]["input"] == manifest["journey_driver"]["inputs"]["mcp"]
                return httpx.Response(200, json={"structuredContent": {"http_status": 202,
                    "data": {"run_id": "run-mcp"}}})
            submissions.append("product")
            assert json.loads(request.content) == manifest["journey_driver"]["inputs"]["product"]
            return httpx.Response(202, json={"run_id": "run-product"})
        if latest_run:
            result_response = installed_result_response(request, latest_run)
            if result_response is not None:
                return result_response
        entry = request.url.path.rsplit("/", 1)[-1].removeprefix("run-")
        run_reads.append(entry)
        latest_run.update({
            "run_id": f"run-{entry}", "scope": scope, "state": "succeeded",
            "origin": "adp_submitted", "provider_state": "completed",
            "input": manifest["journey_driver"]["inputs"][entry],
            "admission": {"provider_identity": identity,
                "connection_ref": {"source_connection_id": "example-source"},
                "native_space_ref": {"record_type": "agent_space", "record_id": "space-example",
                                     "source_connection_id": "example-source", "project_id": scope["project_id"]}},
            "native_job_ref": {"record_type": "native_job", "record_id": f"native-{entry}",
                               "record_revision": f"task-{entry}", "project_id": scope["project_id"],
                               "source_connection_id": "example-source"}})
        return httpx.Response(200, json=latest_run)

    def fake_process(command, **kwargs):
        if command[0] == "node":
            submissions.append("ui")
            assert json.loads(kwargs["input"])["scope"] == scope
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "run_id": "run-ui", "receipt": "ui-generated-receipt-example", "native_handoff": True}).encode())
        if command[2:4] == ["resolve", "--deployment"]:
            return SimpleNamespace(returncode=0, stdout=b'{"deployment":"example","gateway_url":"https://gateway.example.org/api"}')
        submissions.append("cli")
        assert command[:3] == [manifest["journey_driver"]["cli_executable"], "--deployment", "example"]
        return SimpleNamespace(returncode=0, stdout=b'{"status":"ok","detail":{"run_id":"run-cli"}}')

    def provider_read(selected_connection, space_id, task_id, *, deadline):
        assert selected_connection == selected and space_id == "space-example"
        assert deadline > time.monotonic()
        entry = task_id.removeprefix("task-")
        provider_reads.append(entry)
        return {"agentSpaceId": space_id, "taskId": task_id, "executionId": f"native-{entry}",
                "status": "FAILED" if entry == provider_failure else "COMPLETED",
                "taskType": task_type}

    def driver_factory(fixture, destination, *, deadline, max_requests):
        driver = FirstPartyDriver(fixture, destination, deadline=deadline, max_requests=max_requests)
        driver.client.close()
        driver.client = httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=False)
        drivers.append(driver)
        return driver

    monkeypatch.setattr("journey_driver.subprocess.run", fake_process)
    monkeypatch.setattr("journey_driver.read_provider_task", provider_read)
    if provider_failure:
        with pytest.raises(ValueError, match="native provider task"):
            drive_journeys(manifest, directory, deadline=time.monotonic() + 20, driver_factory=driver_factory)
        assert submissions == ["product", "cli", "mcp"]
        assert run_reads == provider_reads == ["product", "cli", "mcp"]
        assert '"status": "uncertain"' in (directory / f"devops-{manifest['authorization']['run_id']}-journey-actions.jsonl").read_text()
    else:
        result = drive_journeys(manifest, directory, deadline=time.monotonic() + 20,
                                driver_factory=driver_factory)
        assert submissions == ["product", "cli", "mcp", "ui"]
        assert run_reads == provider_reads == submissions
        assert {entry: {key: value for key, value in observed.items() if key != "result_readback"}
                for entry, observed in result.items()} == {
                    entry: {"run_id": f"run-{entry}", "native_id": f"native-{entry}"} for entry in submissions}
        if capability == "investigation":
            assert all(result[entry]["result_readback"]["result_id"] == f"result-run-{entry}" for entry in submissions)
    assert drivers[0].client.is_closed
