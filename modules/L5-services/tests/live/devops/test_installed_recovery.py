"""Offline installed recovery uses Product responses and native provider boundaries."""

import json
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

import installed_recovery
from installed_recovery import InstalledRecoveryWork
from provider_recovery import read_recovery
from recovery import FAULTS
from recovery_selector import HTTPRecoveryTarget, authorize_recovery


pytest_plugins = ("test_selector", "test_recovery_selector")


@pytest.fixture
def installed_case(recovery_selection, monkeypatch):
    manifest, path, directory, prior = recovery_selection
    recovery = manifest["recovery"]
    scope = recovery["scope"]
    driver = manifest["journey_driver"]
    driver["scope"] = {**scope, "binding_id": recovery["binding_id"]}
    driver["scope"].pop("tenant_id")
    for body in driver["inputs"].values():
        body["binding_id"] = recovery["binding_id"]
    body = {**deepcopy(driver["inputs"]["product"]), "devops_input": {
        "kind": "investigation", "question": "What changed?", "resource_refs": [], "telemetry_refs": []}}
    selected = {"read_token_env": "INSTALLED_RECOVERY_READER", "submit_token_env": "INSTALLED_RECOVERY_ACTOR",
                "binding_revision": 2, "agent_space_id": "space-example",
                "inputs": {fault: deepcopy(body) for fault in FAULTS}}
    recovery["installed_work"] = selected
    recovery["action_channels"].pop("submit")
    recovery["readback_channels"].pop("work")
    recovery["readback_channels"].pop("spaces")
    monkeypatch.setenv(selected["read_token_env"], "reader-credential")
    monkeypatch.setenv(selected["submit_token_env"], "action-credential")
    now = datetime.now(UTC)
    manifest["authorization"]["expires_at"] = (now + timedelta(hours=1)).isoformat()
    native = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
              "source_connection_id": driver["provider_readback"]["source_connection_id"],
              "record_type": "agent_space", "record_id": "space-example", "record_revision": "1"}
    binding_scope = {**scope, "owner_id": "owner-example", "visibility": "product_team", "relationship_revision": "1"}
    binding = {"binding_id": recovery["binding_id"], "state": "ready", "revision": 2, "generation": 3,
               "scope": binding_scope, "native_resource_ref": native,
               "connection_ref": {**native, "record_type": "connection", "record_id": native["source_connection_id"]},
               "provider_identity": {key: driver["provider_readback"][key] for key in ("account_id", "role_arn", "region")}}
    admission = {"scope": binding_scope, "binding_id": binding["binding_id"], "binding_generation": 3,
                 "native_space_ref": native, "connection_ref": binding["connection_ref"],
                 "provider_identity": binding["provider_identity"]}
    run = {"run_id": "run-example", "input": body, "admission": admission, "scope": binding_scope,
           "origin": "adp_submitted", "state": "succeeded", "provider_state": "completed",
           "native_job_ref": {**native, "record_type": "native_job", "record_id": "execution-example",
                              "record_revision": "task-example"}}
    description = {"run_id": run["run_id"], "scope": binding_scope, "binding_id": binding["binding_id"],
                   "binding_generation": 3, "configuration_revision": body["configuration_revision"],
                   "attempt_id": "attempt-example", "idempotency_token": "a" * 64,
                   **{key: body["devops_input"][key] for key in ("question", "resource_refs", "telemetry_refs")}}
    task = {"agentSpaceId": "space-example", "taskId": "task-example", "executionId": "execution-example",
            "taskType": "INVESTIGATION", "status": "COMPLETED", "description": json.dumps(description),
            "createdAt": (now - timedelta(seconds=5)).isoformat(), "updatedAt": now.isoformat()}
    requests = []
    states = {"binding": binding, "run": run, "submit_timeout": False, "status": 200}

    def handle(request):
        requests.append(request)
        if request.method == "POST":
            assert request.url.path.endswith("/runs")
            assert json.loads(request.content) == body
            assert request.headers["Authorization"] == "Bearer action-credential"
            assert request.headers["Idempotency-Key"] in recovery["receipts"].values()
            if states["submit_timeout"]:
                states["submit_timeout"] = False
                raise httpx.ReadTimeout("lost acknowledgement", request=request)
            return httpx.Response(202, json={"run_id": run["run_id"]})
        assert request.headers["Authorization"] == "Bearer reader-credential"
        return httpx.Response(states["status"], json=states["binding"] if "/bindings/" in request.url.path else states["run"])

    target = SimpleNamespace(client=httpx.Client(transport=httpx.MockTransport(handle)), httpx=httpx, calls=0, max_requests=60,
                             deadline=time.monotonic() + 60, _journal=Mock())
    provider = Mock(return_value={"task": task, "matching_task_ids": ["task-example"], "requests": 4})
    monkeypatch.setattr(installed_recovery, "read_provider_recovery", provider)
    worker = InstalledRecoveryWork(target, manifest)
    yield SimpleNamespace(manifest=manifest, directory=directory, target=target, worker=worker, requests=requests,
                          provider=provider, states=states, task=task, scope=scope,
                          fault=FAULTS[0], receipt=recovery["receipts"][FAULTS[0]])
    target.client.close()


def test_installed_authorization_replaces_submit_work_and_spaces_channels(installed_case):
    case = installed_case
    assert authorize_recovery(case.manifest, case.manifest["release"]["source_sha"], str(case.directory)) == case.directory
    assert "submit" not in case.manifest["recovery"]["action_channels"]
    assert "work" not in case.manifest["recovery"]["readback_channels"]
    assert "spaces" not in case.manifest["recovery"]["readback_channels"]


def test_installed_submit_and_independent_native_readback(installed_case):
    case = installed_case
    case.worker.submit(case.scope, case.fault, case.receipt)
    result = case.worker.read_work(case.scope, case.receipt)
    assert result["native_job_id"] == "execution-example"
    assert result["native_submit_count"] == 1
    assert result["submit_count_coverage"] == "retained_native_tasks_not_api_attempts"
    assert result["readback"]["run"] == case.states["run"]
    assert case.target.calls == 9
    case.provider.assert_called_once()


def test_lost_ack_replays_same_idempotency_key_before_readback(installed_case):
    case = installed_case
    case.states["submit_timeout"] = True
    with pytest.raises(TimeoutError):
        case.worker.submit(case.scope, case.fault, case.receipt)
    result = case.worker.read_work(case.scope, case.receipt)
    assert result["native_submit_count"] == 1
    posts = [request for request in case.requests if request.method == "POST"]
    assert len(posts) == 2
    assert posts[0].headers["Idempotency-Key"] == posts[1].headers["Idempotency-Key"] == case.receipt
    assert posts[0].content == posts[1].content
    case.target._journal.assert_any_call("submit", "uncertain", case.receipt)


@pytest.mark.parametrize("fault", ["foreign_scope", "stale_binding", "wrong_role", "denied", "expired", "request_bound"])
def test_invalid_authority_has_zero_provider_effects(installed_case, fault):
    case = installed_case
    scope = deepcopy(case.scope)
    if fault == "foreign_scope":
        scope["product_id"] = "another-product"
    elif fault == "stale_binding":
        case.states["binding"]["revision"] += 1
    elif fault == "wrong_role":
        case.states["binding"]["provider_identity"] = {"role_arn": "wrong"}
    elif fault == "denied":
        case.states["status"] = 403
    elif fault == "expired":
        case.target.deadline = time.monotonic() - 1
    elif fault == "request_bound":
        case.target.calls = case.target.max_requests
    with pytest.raises(ValueError):
        case.worker.submit(scope, case.fault, case.receipt)
    assert not [request for request in case.requests if request.method == "POST"]
    case.provider.assert_not_called()


@pytest.mark.parametrize("fault", ["foreign_run", "changed_input", "stale_generation", "duplicate_native", "wrong_execution",
                                   "wrong_description", "unbounded_requests", "stale_task", "future_task"])
def test_readback_rejects_foreign_stale_and_duplicate_work(installed_case, fault):
    case = installed_case
    case.worker.submit(case.scope, case.fault, case.receipt)
    case.states["run"] = deepcopy(case.states["run"])
    if fault == "foreign_run":
        case.states["run"]["run_id"] = "another-run"
    elif fault == "changed_input":
        case.states["run"]["input"]["devops_input"]["question"] = "different question"
    elif fault == "stale_generation":
        case.states["run"]["admission"]["binding_generation"] = 4
    elif fault == "duplicate_native":
        case.provider.return_value["matching_task_ids"].append("second-task")
    elif fault == "wrong_execution":
        case.task["executionId"] = "another-execution"
    elif fault == "wrong_description":
        case.task["description"] = json.dumps({"run_id": "another-run"})
    elif fault == "unbounded_requests":
        case.provider.return_value["requests"] = 1000
    elif fault == "stale_task":
        case.task["createdAt"] = "2000-01-01T00:00:00Z"
    elif fault == "future_task":
        case.task["updatedAt"] = "2999-01-01T00:00:00Z"
    with pytest.raises(ValueError):
        case.worker.read_work(case.scope, case.receipt)


def test_target_routes_installed_work_without_custom_channel(installed_case):
    case = installed_case
    target = HTTPRecoveryTarget(case.manifest, case.directory, deadline=case.target.deadline, max_requests=60)
    target.client.close()
    target.client = case.target.client
    try:
        target.submit(case.scope, case.fault, case.receipt)
        assert target.read_work(case.scope, case.receipt)["native_job_id"] == "execution-example"
        entries = [json.loads(line) for line in (case.directory / f"devops-{target.run_id}-recovery.jsonl").read_text().splitlines()]
        assert [entry["phase"] for entry in entries] == ["prepared", "acknowledged"]
    finally:
        target.close()


@pytest.mark.parametrize("route_reserve", [4, 19])
def test_space_inventory_reserves_cleanup_reads_and_preserves_other_spaces(installed_case, route_reserve):
    case = installed_case
    case.target.route_cleanup_reserve = route_reserve
    spaces = {identifier: {"space": {"agentSpaceId": identifier, "description": "retained"}, "tags": {}}
              for identifier in ("space-example", "another-space")}
    case.provider.return_value = {"spaces": spaces, "requests": 4}
    assert case.worker.read_spaces(case.scope) == spaces
    assert case.target.cleanup_reserve == route_reserve + 5
    case.target.cleanup_phase = True
    case.target.cleanup_grace = 30
    case.target.deadline = time.monotonic() - 1
    case.target.calls = case.target.max_requests - 5
    assert case.worker.read_spaces(case.scope) == spaces
    assert case.target.calls == case.target.max_requests
    with pytest.raises(ValueError, match="cannot submit during cleanup"):
        case.worker.submit(case.scope, case.fault, case.receipt)


@pytest.mark.parametrize("fault", ["absent", "foreign", "invalid_count", "stale_binding"])
def test_space_inventory_fails_closed(installed_case, fault):
    case = installed_case
    case.provider.return_value = {"spaces": {"space-example": {
        "space": {"agentSpaceId": "space-example"}, "tags": {}}}, "requests": 3}
    if fault == "absent":
        case.provider.return_value["spaces"] = {}
    elif fault == "foreign":
        case.provider.return_value["spaces"]["space-example"]["space"]["agentSpaceId"] = "another-space"
    elif fault == "invalid_count":
        case.provider.return_value["requests"] = True
    else:
        case.states["binding"]["revision"] += 1
    with pytest.raises(ValueError):
        case.worker.read_spaces(case.scope)
    if fault == "stale_binding":
        case.provider.assert_not_called()


@pytest.mark.parametrize("fault", [None, "duplicate", "absent", "foreign_readback", "cursor"])
def test_native_space_inventory_reads_all_pages_and_retains_configuration(fault):
    identifiers = ["space-example", "another-space"]
    if fault == "duplicate":
        identifiers[1] = identifiers[0]
    if fault == "absent":
        identifiers[0] = "missing-space"
    pages = [{"agentSpaces": [{"agentSpaceId": identifiers[0]}], "nextToken": "next"},
             {"agentSpaces": [{"agentSpaceId": identifiers[1]}]}]
    if fault == "cursor":
        pages[1]["nextToken"] = "next"

    def get_space(**parameters):
        assert parameters["includedData"] == "ALL_DATA"
        return {"agentSpace": {"agentSpaceId": "foreign" if fault == "foreign_readback" else parameters["agentSpaceId"],
                               "name": "retained", "updatedAt": "volatile", "description": "retained",
                               "createdAt": datetime(2026, 10, 1, tzinfo=UTC),
                               "preferences": {"locale": "en"}}, "tags": {"owner": "retained-owner"}}

    client = Mock(list_agent_spaces=Mock(side_effect=pages), get_agent_space=Mock(side_effect=get_space))
    identity = {"Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/example/session"}
    clients = {"sts": Mock(get_caller_identity=Mock(return_value=identity)), "devops-agent": client}
    selection = {"kind": "spaces", "space_id": "space-example", "max_requests": 20}
    def factory(service, **kwargs):
        return clients[service]
    if fault:
        with pytest.raises(ValueError):
            read_recovery("123456789012", "arn:aws:iam::123456789012:role/example", "us-east-1", selection,
                          client_factory=factory)
    else:
        observed = read_recovery("123456789012", "arn:aws:iam::123456789012:role/example", "us-east-1", selection,
                                 client_factory=factory)
        assert observed["requests"] == 5
        assert set(observed["spaces"]) == set(identifiers)
        assert "updatedAt" not in observed["spaces"]["space-example"]["space"]
        assert observed["spaces"]["another-space"]["tags"] == {"owner": "retained-owner"}
        assert observed["spaces"]["another-space"]["space"]["preferences"] == {"locale": "en"}


@pytest.mark.parametrize("fault", [None, "wrong_identity", "duplicate", "repeated_cursor", "request_bound"])
def test_native_inventory_is_bounded_and_counts_actual_tasks(fault):
    task = {"taskId": "task-example", "description": json.dumps({"run_id": "run-example"})}
    pages = [{"tasks": [task]}]
    if fault == "duplicate":
        pages[0]["tasks"].append({**task, "taskId": "second-task"})
    if fault == "repeated_cursor":
        pages = [{"tasks": [], "nextToken": "repeated"}] * 2
    client = Mock(get_agent_space=Mock(return_value={"agentSpace": {"agentSpaceId": "space-example"}}),
                  list_backlog_tasks=Mock(side_effect=pages), get_backlog_task=Mock(return_value={"task": task}))
    identity = {"Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/example/session"}
    if fault == "wrong_identity":
        identity["Arn"] = "arn:aws:sts::123456789012:assumed-role/other/session"
    clients = {"sts": Mock(get_caller_identity=Mock(return_value=identity)), "devops-agent": client}
    selection = {"kind": "work", "space_id": "space-example", "run_id": "run-example", "task_id": "task-example",
                 "max_requests": 3 if fault == "request_bound" else 20}
    factory = Mock(side_effect=lambda service, **kwargs: clients[service])
    if fault:
        with pytest.raises(ValueError):
            read_recovery("123456789012", "arn:aws:iam::123456789012:role/example", "us-east-1", selection,
                          client_factory=factory)
        if fault == "wrong_identity":
            client.get_agent_space.assert_not_called()
    else:
        result = read_recovery("123456789012", "arn:aws:iam::123456789012:role/example", "us-east-1", selection,
                               client_factory=factory)
        assert result == {"task": task, "matching_task_ids": ["task-example"], "requests": 4}
