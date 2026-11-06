"""Native task proof comes from a selected broker role, never a claimed HTTP result."""

import json
from datetime import UTC, datetime
import time
from types import SimpleNamespace

import httpx
import pytest

from journey_driver import FirstPartyDriver, read_provider_task
from provider_task import read_task
import test_selector as selector_fixtures

selector_case = selector_fixtures.selector_case


def test_native_task_reader_retains_only_aware_provider_timestamps():
    created = datetime(2026, 10, 28, tzinfo=UTC)

    def factory(service, **options):
        if service == "sts":
            return SimpleNamespace(get_caller_identity=lambda: {
                "Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/example-readonly/session"})
        return SimpleNamespace(get_backlog_task=lambda **kwargs: {"task": {
            "agentSpaceId": kwargs["agentSpaceId"], "taskId": kwargs["taskId"], "executionId": "native-example",
            "taskType": "INVESTIGATION", "status": "COMPLETED", "createdAt": created,
            "updatedAt": created.replace(tzinfo=None), "description": "not part of this observation"}})

    result = read_task("123456789012", ROLE, "us-east-1", "space-example", "task-example", client_factory=factory)
    assert result["createdAt"] == "2026-10-28T00:00:00Z"
    assert "updatedAt" not in result and "description" not in result


ROLE = "arn:aws:iam::123456789012:role/example-readonly"


def test_selected_provider_identity_precedes_one_bounded_native_read():
    calls = []

    def factory(service, **options):
        calls.append((service, options))
        if service == "sts":
            return SimpleNamespace(get_caller_identity=lambda: {
                "Account": "123456789012",
                "Arn": "arn:aws:sts::123456789012:assumed-role/example-readonly/session"})
        return SimpleNamespace(get_backlog_task=lambda **kwargs: {
            "task": {"agentSpaceId": kwargs["agentSpaceId"], "taskId": kwargs["taskId"],
                     "executionId": "native-example", "status": "COMPLETED", "taskType": "INVESTIGATION",
                     "description": "private provider evidence", "spend_usd": "0.00",
                     "usage": {"requests": 0, "elapsed_seconds": 0}}})

    task = read_task("123456789012", ROLE, "us-east-1", "space-example", "task-example",
                     client_factory=factory)
    assert task == {"agentSpaceId": "space-example", "taskId": "task-example",
                    "executionId": "native-example", "status": "COMPLETED", "taskType": "INVESTIGATION"}
    assert [service for service, _ in calls] == ["sts", "devops-agent"]
    assert calls[1][1]["region_name"] == "us-east-1"
    assert calls[1][1]["config"].retries["total_max_attempts"] == 1


@pytest.mark.parametrize("identity", [
    {"Account": "000000000000", "Arn": "arn:aws:sts::000000000000:assumed-role/example-readonly/session"},
    {"Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/other/session"},
    {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:role/example-readonly"},
])
def test_unselected_identity_never_reads_native_task(identity):
    calls = []

    def factory(service, **options):
        calls.append(service)
        return SimpleNamespace(get_caller_identity=lambda: identity)

    with pytest.raises(ValueError, match="identity"):
        read_task("123456789012", ROLE, "us-east-1", "space-example", "task-example",
                  client_factory=factory)
    assert calls == ["sts"]


def test_provider_read_uses_broker_not_ambient_aws(selector_case, monkeypatch):
    manifest, _, _, _ = selector_case
    selected = manifest["journey_driver"]["provider_readback"]
    calls = []

    def run(command, **options):
        calls.append((command, options))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"taskId": "task-example"}).encode())

    monkeypatch.setattr("journey_driver.subprocess.run", run)
    assert read_provider_task(selected, "space-example", "task-example",
                              deadline=time.monotonic() + 10) == {"taskId": "task-example"}
    assert calls[0][0][:7] == ["adp-cred", "assume", "--service", "aws", "--label", "example", "--exec"]
    assert calls[0][0][-5:] == ["123456789012", ROLE, "us-east-1", "space-example", "task-example"]
    assert calls[0][1]["capture_output"] is True


@pytest.mark.parametrize("change", [
    {"agentSpaceId": "other"}, {"taskId": "other"}, {"executionId": "other"},
    {"status": "IN_PROGRESS"}, {"status": None}, {"taskType": "MITIGATION"},
])
def test_gateway_success_requires_independent_exact_provider_readback(selector_case, monkeypatch, change):
    manifest, _, directory, _ = selector_case
    selected = manifest["journey_driver"]["provider_readback"]
    identity = {key: selected[key] for key in ("account_id", "role_arn", "region")}
    scope = manifest["journey_driver"]["scope"]
    observed = {"run_id": "run-example", "state": "succeeded", "origin": "adp_submitted",
                "provider_state": "completed", "scope": scope,
                "input": manifest["journey_driver"]["inputs"]["product"],
                "admission": {"provider_identity": identity,
                              "connection_ref": {"source_connection_id": "example-source"},
                              "native_space_ref": {"record_type": "agent_space", "record_id": "space-example",
                                                   "project_id": scope["project_id"],
                                                   "source_connection_id": "example-source"}},
                "native_job_ref": {"record_type": "native_job", "record_id": "native-example",
                                   "record_revision": "task-example", "project_id": scope["project_id"],
                                   "source_connection_id": "example-source"}}
    calls = []

    def provider(*args, **kwargs):
        calls.append((args, kwargs))
        return {**{"agentSpaceId": "space-example", "taskId": "task-example",
                   "executionId": "native-example", "status": "COMPLETED",
                   "taskType": "INVESTIGATION"}, **change}

    driver = FirstPartyDriver(manifest, directory, deadline=time.monotonic() + 10, max_requests=5)
    driver.client.close()
    driver.client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=observed)), follow_redirects=False)
    monkeypatch.setattr("journey_driver.read_provider_task", provider)
    try:
        with pytest.raises(ValueError, match="native provider task"):
            driver.read_native("run-example")
        assert calls[0][0][:3] == (selected, "space-example", "task-example")
    finally:
        driver.close()
