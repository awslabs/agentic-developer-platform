"""Installed Product/native boundaries, without a substitute journey server."""

import json
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import test_selector as selector_fixtures

from installed_journeys import InstalledJourneyReader
from journey_driver import TASK_TYPES
from selector import authorize, qualify_journeys
from test_installed_results import installed_result_response
from test_selector import offline_policy

selector_case = selector_fixtures.selector_case


@pytest.fixture
def installed_journeys(selector_case, monkeypatch):
    manifest, path, directory, prior = selector_case
    now = datetime.now(UTC)
    manifest["authorization"]["expires_at"] = (now + timedelta(hours=1)).isoformat()
    manifest["installed_journeys"] = {
        "tenant_id": "tenant-example", "token_env": "DEVOPS_INSTALLED_READ_TOKEN",
        "started_at": (now - timedelta(seconds=40)).isoformat(),
        "runs": {entry: f"run-{entry}" for entry in ("product", "ui", "cli", "mcp")}}
    manifest["readback_channels"].pop("journeys")
    monkeypatch.delenv("DEVOPS_JOURNEYS_TOKEN")
    monkeypatch.setenv("DEVOPS_INSTALLED_READ_TOKEN", "private-installed-read-token")
    for limit in manifest["limits"].values():
        limit["max_requests"] = 50
    path.write_text(json.dumps(manifest))
    calls, provider_calls = [], []
    runs, tasks = {}, {}
    scope = manifest["journey_driver"]["scope"]
    selected = manifest["journey_driver"]["provider_readback"]
    for entry in ("product", "ui", "cli", "mcp"):
        run_scope = {"tenant_id": "tenant-example", **{key: scope[key] for key in ("product_id", "project_id")}}
        runs[entry] = {
            "run_id": f"run-{entry}", "scope": run_scope, "origin": "adp_submitted", "state": "succeeded",
            "provider_state": "completed", "input": manifest["journey_driver"]["inputs"][entry],
            "admission": {"scope": run_scope, "provider_identity": {
                key: selected[key] for key in ("account_id", "role_arn", "region")},
                "connection_ref": {"source_connection_id": selected["source_connection_id"]},
                "native_space_ref": {"record_type": "agent_space", "record_id": "space-example",
                    "project_id": scope["project_id"], "source_connection_id": selected["source_connection_id"]}},
            "native_job_ref": {"record_type": "native_job", "record_id": f"native-{entry}",
                "record_revision": f"task-{entry}", "project_id": scope["project_id"],
                "source_connection_id": selected["source_connection_id"]}}
        tasks[entry] = {"agentSpaceId": "space-example", "taskId": f"task-{entry}",
            "executionId": f"native-{entry}", "taskType": "INVESTIGATION", "status": "COMPLETED",
            "createdAt": (now - timedelta(seconds=30)).isoformat(),
            "updatedAt": (now - timedelta(seconds=20)).isoformat()}

    def transport(request):
        calls.append(request)
        if request.url.host != "gateway.example.org":
            if request.url.host == "release.example.org":
                observation = prior.read_release(manifest["target_ref"])
            elif request.url.host == "capabilities.example.org":
                observation = [{**item, "qualification_run_id": manifest["authorization"]["run_id"],
                                "usage": {"requests": 1, "elapsed_seconds": 1, "spend_usd": "0.01"}}
                               for item in prior.read_capabilities(manifest["target_ref"])]
            else:
                pytest.fail("custom journey readback must never be called")
            return httpx.Response(200, json={"target_ref": manifest["target_ref"], "observation": observation})
        if request.method == "POST":
            if request.url.path.endswith("/mcp/tools/call"):
                return httpx.Response(200, json={"structuredContent": {"http_status": 202,
                                                                     "data": {"run_id": "run-mcp"}}})
            return httpx.Response(202, json={"run_id": "run-product"})
        if "/runs/" in request.url.path:
            entry = request.url.path.rsplit("/", 1)[1].removeprefix("run-")
            return httpx.Response(200, json=runs[entry])
        if request.url.path.endswith("/results"):
            from test_installed_results import result_fixture
            return httpx.Response(200, json={"items": [result_fixture(run)[0] for run in runs.values()]})
        for run in runs.values():
            response = installed_result_response(request, run)
            if response is not None:
                return response
        pytest.fail("unexpected installed route")

    def provider(connection, space_id, task_id, *, deadline):
        provider_calls.append(task_id)
        assert connection == selected and space_id == "space-example" and deadline > time.monotonic()
        return deepcopy(tasks[task_id.removeprefix("task-")])

    def process(command, **kwargs):
        if command[0] == "node":
            response = {"run_id": "run-ui", "receipt": "ui-receipt-example-0001", "native_handoff": True}
        elif command[2:4] == ["resolve", "--deployment"]:
            response = {"deployment": "example", "gateway_url": "https://gateway.example.org/api"}
        else:
            response = {"status": "ok", "detail": {"run_id": "run-cli"}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(response).encode())

    original_client = httpx.Client
    monkeypatch.setattr("httpx.Client", lambda **kwargs: original_client(transport=httpx.MockTransport(transport), **kwargs))
    monkeypatch.setattr("journey_driver.read_provider_task", provider)
    monkeypatch.setattr("journey_driver.subprocess.run", process)
    return manifest, path, directory, calls, provider_calls, runs, tasks


@pytest.mark.parametrize("capability", ["investigation", "release_readiness", "release_verification"])
def test_real_selector_drives_clients_then_reads_installed_routes(installed_journeys, capability):
    manifest, path, directory, calls, provider_calls, runs, tasks = installed_journeys
    for entry in runs:
        runs[entry]["input"]["capability"] = capability
        runs[entry]["input"]["devops_input"]["kind"] = capability
        tasks[entry]["taskType"] = TASK_TYPES[capability]
    path.write_text(json.dumps(manifest))
    run_id = qualify_journeys(manifest["release"]["source_sha"], path, directory, policy_reader=offline_policy)
    result = json.loads((directory / f"devops-{run_id}-journeys.json").read_text())
    assert len(result["journeys"]) == 4 and len(provider_calls) == 8
    for observation in result["journeys"]:
        assert observation["readback"]["run"] == runs[observation["entry_point"]]
        assert observation["usage"]["cost_coverage"] == "unavailable"
        assert "spend_usd" not in observation["usage"]
        assert observation["usage"]["requests_scope"] == "qualification_readback"
        if capability == "investigation":
            assert observation["result_readback"]["evidence_ref"]["sha256"]
    assert result["native_usage"][capability]["total_spend_usd"] is None
    independent = [request for request in calls if request.headers.get("Authorization") == "Bearer private-installed-read-token"]
    assert len(independent) == (16 if capability == "investigation" else 4)
    assert all(request.method == "GET" and request.url.host == "gateway.example.org" for request in independent)


@pytest.mark.parametrize("mutation", ["tenant", "project", "binding", "admission", "provider_identity", "stale",
                                      "future", "missing_time", "duration", "native_id", "native_kind", "requests"])
def test_installed_readback_rejects_scope_time_and_budget_failures(installed_journeys, mutation):
    manifest, _, _, calls, provider_calls, runs, tasks = installed_journeys
    if mutation == "tenant":
        runs["product"]["scope"]["tenant_id"] = "foreign"
    elif mutation == "project":
        runs["product"]["scope"]["project_id"] = "foreign"
    elif mutation == "binding":
        runs["product"]["input"] = {**runs["product"]["input"], "binding_id": "foreign"}
    elif mutation == "admission":
        runs["product"]["admission"]["scope"] = {"tenant_id": "foreign"}
    elif mutation == "provider_identity":
        runs["product"]["admission"]["provider_identity"]["account_id"] = "000000000000"
    elif mutation == "stale":
        tasks["product"]["createdAt"] = manifest["release"]["deployed_at"]
    elif mutation == "future":
        tasks["product"]["updatedAt"] = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    elif mutation == "missing_time":
        del tasks["product"]["createdAt"]
    elif mutation == "duration":
        manifest["limits"]["investigation"]["deadline_seconds"] = 1
    elif mutation == "native_id":
        tasks["product"]["executionId"] = "foreign"
    elif mutation == "native_kind":
        tasks["product"]["taskType"] = "RELEASE_TESTING"
    else:
        manifest["limits"]["investigation"]["max_requests"] = 2
    reader = InstalledJourneyReader(manifest, deadline=time.monotonic() + 30, max_requests=50)
    try:
        with pytest.raises(ValueError):
            reader.read(manifest["installed_journeys"]["runs"])
        if mutation in {"tenant", "project", "binding", "admission", "provider_identity", "requests"}:
            assert provider_calls == []
            assert len(calls) == 1
    finally:
        reader.close()


@pytest.mark.parametrize("failure", ["shared", "missing", "downgrade", "duplicate"])
def test_installed_authorization_has_no_custom_journey_channel_fallback(installed_journeys, monkeypatch, failure):
    manifest, _, directory, calls, provider_calls, _, _ = installed_journeys
    if failure == "shared":
        monkeypatch.setenv("DEVOPS_INSTALLED_READ_TOKEN", "private-product-token")
    elif failure == "missing":
        monkeypatch.delenv("DEVOPS_INSTALLED_READ_TOKEN")
    elif failure == "downgrade":
        manifest["readback_channels"]["journeys"] = {"source": "journeys"}
    else:
        manifest["installed_journeys"]["runs"]["ui"] = "run-product"
    with pytest.raises(ValueError):
        authorize(manifest, manifest["release"]["source_sha"], directory)
    assert calls == [] and provider_calls == []


@pytest.mark.parametrize("failure", ["mutation", "deadline", "future_start"])
def test_installed_reader_cannot_mutate_or_read_outside_its_time_bound(installed_journeys, failure):
    manifest, _, _, calls, provider_calls, _, _ = installed_journeys
    if failure == "future_start":
        manifest["installed_journeys"]["started_at"] = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
    reader = InstalledJourneyReader(manifest, deadline=time.monotonic() + (-1 if failure == "deadline" else 30),
                                    max_requests=50)
    try:
        with pytest.raises(ValueError):
            if failure == "mutation":
                reader.run_product()
            else:
                reader.read(manifest["installed_journeys"]["runs"])
        assert calls == [] and provider_calls == []
    finally:
        reader.close()
