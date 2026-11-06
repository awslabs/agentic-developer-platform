"""Native lifecycle requests and readbacks use the pinned installed SDK model."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace

import boto3
import pytest
from botocore.stub import Stubber

from l5_services.adapters.devops.investigations import InvestigationAdapter, _description

NOW = datetime(2026, 10, 29, tzinfo=UTC)


@pytest.fixture
def lifecycle():
    scope = {"tenant_id": "tenant-one", "product_id": "product-one", "project_id": "project-one"}
    reference = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                 "source_connection_id": "connection-one"}
    identity = {"account_id": "123456789012", "region": "us-east-1",
                "role_arn": "arn:aws:iam::123456789012:role/example"}
    native = {**reference, "record_type": "native_job", "record_id": "execution-one", "record_revision": "task-one"}
    run = SimpleNamespace(**scope, run_id="run-one", service_id="devops", configuration_revision=1,
                          binding_id="binding-one", binding_generation=1, native_job_ref=native,
                          input={"capability": "investigation", "devops_input": {
                              "question": "What changed?", "resource_refs": [], "telemetry_refs": []}},
                          admission={"scope": scope, "provider_identity": identity,
                                     "connection_ref": reference,
                                     "native_space_ref": {**reference, "record_type": "agent_space", "record_id": "space-one"}},
                          manifest={"scope": scope, "run_id": "run-one", "configuration_revision": 1,
                                    "binding_generation": 1, "resource_refs": [], "telemetry_refs": []})
    manifest_hash = sha256(json.dumps(run.manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    attempt = SimpleNamespace(run_id=run.run_id, attempt_id="attempt-one", input_hash=manifest_hash,
                              idempotency_token=sha256(f"{run.tenant_id}:{run.run_id}:{manifest_hash}".encode()).hexdigest())
    task = {"agentSpaceId": "space-one", "taskId": "task-one", "executionId": "execution-one",
            "title": "What changed?", "description": _description(run, run.manifest, attempt),
            "taskType": "INVESTIGATION", "priority": "HIGH", "status": "COMPLETED",
            "createdAt": NOW, "updatedAt": NOW, "version": 1}
    client = boto3.client("devops-agent", region_name="us-east-1",
                          aws_access_key_id="testing", aws_secret_access_key="testing")
    provider = SimpleNamespace(**identity, client=lambda name: client)
    with Stubber(client) as stub:
        yield InvestigationAdapter(clock=lambda: NOW), run, attempt, task, provider, stub
        stub.assert_no_pending_responses()


def task_read(stub, task):
    stub.add_response("get_backlog_task", {"task": task}, {"agentSpaceId": "space-one", "taskId": "task-one"})


@pytest.mark.parametrize("status,expected", [
    ("IN_PROGRESS", "running"), ("PENDING_TRIAGE", "running"), ("WAITING", "running"),
    ("COMPLETED", "completed"), ("FAILED", "failed"), ("TIMED_OUT", "failed"),
    ("CANCELED", "stopped"), ("SKIPPED", "unknown"), ("NEW_STATUS", "unknown"),
])
async def test_observe_uses_native_task_state(lifecycle, status, expected):
    adapter, run, attempt, task, provider, stub = lifecycle
    task_read(stub, {**task, "status": status})
    outcome = await adapter.observe(run, run.native_job_ref, attempt, provider)
    assert outcome.classification == outcome.observation["outcome"] == expected
    assert outcome.observation["native_job_ref"] == run.native_job_ref


@pytest.mark.parametrize("operation", ["observe", "collect", "cancel"])
@pytest.mark.parametrize("field,value", [("tenant_id", "foreign"), ("project_id", "foreign"),
                                         ("source_connection_id", "foreign"), ("record_id", "foreign"),
                                         ("record_revision", "foreign")])
async def test_foreign_reference_makes_no_provider_calls(lifecycle, operation, field, value):
    adapter, run, attempt, _, provider, _ = lifecycle
    with pytest.raises(ValueError, match="foreign native investigation"):
        await getattr(adapter, operation)(run, {**run.native_job_ref, field: value}, attempt, provider)


@pytest.mark.parametrize("operation", ["observe", "collect", "cancel"])
@pytest.mark.parametrize("field", ["account_id", "role_arn", "region"])
async def test_wrong_provider_session_makes_no_calls(lifecycle, operation, field):
    adapter, run, attempt, _, provider, _ = lifecycle
    setattr(provider, field, "foreign")
    with pytest.raises(ValueError, match="pinned provider scope"):
        await getattr(adapter, operation)(run, run.native_job_ref, attempt, provider)


@pytest.mark.parametrize("field,value", [("agentSpaceId", "foreign"), ("executionId", "foreign"),
                                         ("taskId", "foreign"), ("description", "{}"),
                                         ("taskType", "MITIGATION")])
async def test_changed_task_cannot_be_cancelled(lifecycle, field, value):
    adapter, run, attempt, task, provider, stub = lifecycle
    task_read(stub, {**task, field: value})
    with pytest.raises(ValueError, match="differs from pinned run"):
        await adapter.cancel(run, run.native_job_ref, attempt, provider)


async def test_cancel_is_idempotent_and_requires_readback(lifecycle):
    adapter, run, attempt, task, provider, stub = lifecycle
    task_read(stub, {**task, "status": "IN_PROGRESS"})
    token = sha256(f"{run.tenant_id}:{run.run_id}:task-one:cancel".encode()).hexdigest()
    stopped = {**task, "status": "CANCELED"}
    stub.add_response("update_backlog_task", {"task": stopped}, {
        "agentSpaceId": "space-one", "taskId": "task-one", "taskStatus": "CANCELED", "clientToken": token})
    task_read(stub, stopped)
    assert (await adapter.cancel(run, run.native_job_ref, attempt, provider)).classification == "stopped"
    task_read(stub, stopped)
    assert (await adapter.cancel(run, run.native_job_ref, attempt, provider)).classification == "stopped"


async def test_cancel_unknown_state_never_writes(lifecycle):
    adapter, run, attempt, task, provider, stub = lifecycle
    task_read(stub, {**task, "status": "NEW_STATUS"})
    assert (await adapter.cancel(run, run.native_job_ref, attempt, provider)).classification == "unknown"


def execution_read(stub, task, *, execution_id="execution-one"):
    execution = {"agentSpaceId": task["agentSpaceId"], "executionId": execution_id,
                 "agentSubTask": "investigation", "createdAt": NOW, "updatedAt": NOW,
                 "executionStatus": "STOPPED"}
    stub.add_response("list_executions", {"executions": [execution]}, {
        "agentSpaceId": "space-one", "taskId": "task-one", "limit": 100})


async def test_collection_requires_the_accepted_execution_not_a_replacement(lifecycle):
    adapter, run, attempt, task, provider, stub = lifecycle
    task_read(stub, task)
    execution_read(stub, task, execution_id="replacement-execution")
    with pytest.raises(ValueError, match="accepted native investigation execution unavailable"):
        await adapter.collect(run, run.native_job_ref, attempt, provider)


@pytest.mark.parametrize("foreign", [False, True])
async def test_journal_pages_reject_foreign_records_and_repeated_cursors(lifecycle, foreign):
    adapter, run, attempt, task, provider, stub = lifecycle
    task_read(stub, task)
    execution_read(stub, task)
    record = {"agentSpaceId": "space-one", "executionId": "foreign" if foreign else "execution-one",
              "recordId": "record-one", "recordType": "summary", "createdAt": NOW,
              "content": {"summary": "Timeout changed"}}
    expected = {"agentSpaceId": "space-one", "executionId": "execution-one", "limit": 100}
    stub.add_response("list_journal_records", {"records": [record], **({} if foreign else {"nextToken": "repeat"})}, expected)
    if not foreign:
        stub.add_response("list_journal_records", {"records": [], "nextToken": "repeat"}, {**expected, "nextToken": "repeat"})
    with pytest.raises(ValueError, match="foreign or duplicate" if foreign else "repeated a page cursor"):
        await adapter.collect(run, run.native_job_ref, attempt, provider)


@pytest.mark.parametrize("mutation", [None, "taskId", "agentSpaceArn", "version"])
async def test_collection_preserves_native_recommendations_not_security_findings(lifecycle, mutation):
    adapter, run, attempt, task, provider, stub = lifecycle
    task_read(stub, task)
    execution_read(stub, task)
    stub.add_response("list_journal_records", {"records": []}, {
        "agentSpaceId": "space-one", "executionId": "execution-one", "limit": 100})
    recommendation = {"agentSpaceArn": "arn:aws:aidevops:us-east-1:123456789012:agentspace/space-one",
                      "recommendationId": "recommendation-one", "taskId": "task-one", "version": 1,
                      "title": "Restore timeout", "content": {"summary": "Restore previous timeout"},
                      "status": "OPEN", "priority": "HIGH", "createdAt": NOW, "updatedAt": NOW}
    stub.add_response("list_recommendations", {"recommendations": [recommendation]}, {
        "agentSpaceId": "space-one", "taskId": "task-one", "limit": 100})
    detail = deepcopy(recommendation)
    if mutation:
        detail[mutation] = 2 if mutation == "version" else "foreign"
    stub.add_response("get_recommendation", {"recommendation": detail}, {
        "agentSpaceId": "space-one", "recommendationId": "recommendation-one", "recommendationVersion": 1})
    if mutation:
        with pytest.raises(ValueError, match="recommendation readback mismatch"):
            await adapter.collect(run, run.native_job_ref, attempt, provider)
    else:
        result = (await adapter.collect(run, run.native_job_ref, attempt, provider)).result
        assert result["operational_recommendations"][0]["content"] == recommendation["content"]
        assert result["costs"] == [{"kind": "unknown", "currency": "USD"}]
        assert result["usage_coverage"]["scope"]["executionId"] == run.native_job_ref["record_id"]
        assert "findings" not in result
