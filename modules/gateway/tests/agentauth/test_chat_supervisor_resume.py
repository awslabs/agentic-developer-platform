"""Trusted recovery lookup through HTTP and transactional storage fixtures."""

import json
import os

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.bootstrap import envelope_digest
from tests.agentauth import test_chat_turn_completion as completion
from tests.agentauth.test_chat_sandbox_exit import supervisor
from tests.agentauth.test_work_producer import proof

fixtures = completion.fixtures
client = completion.client
runtime = completion.runtime
store = completion.store
sts = completion.sts
capability = completion.capability
retained_input_table = completion.retained_input_table
ready = completion.ready
registered_owner = completion.registered_owner
transport = completion.transport
consumer = completion.consumer
RESUME = "/internal/v1/agent/chat/data/resume"


async def resume(client, runtime, **changes):
    body = {field: value for field, value in fixtures.finalization.body(runtime).items() if field in {"run_id", "envelope_digest"}}
    body.update(changes)
    return await client.post(RESUME, json=body, headers={"X-Adp-Producer-Proof": proof(envelope_digest(body))})


@pytest.mark.parametrize("phase", ["admitted", "terminal", "delivered", "completed"])
async def test_recovers_original_binding_without_replaying_or_minting_authority(client, runtime, ready, transport, consumer, phase):
    if phase != "admitted":
        assert (await fixtures.finalization.request(client, runtime)).status_code == 200
    if phase in {"delivered", "completed"}:
        consumer._process_response(completion.publication.payload(transport))
    if phase == "completed":
        assert (await completion.complete(client, runtime)).status_code == 200
        row = completion.publication.session(transport)
        row["threads"]["thread-a"]["processing_task_id"] = "next-task"
        transport.sessions.put_item(Item=row)
    before = fixtures.finalization.execution(runtime)
    owner = completion.publication.session(transport)
    header = fixtures.finalization.header(runtime)
    result = await resume(client, runtime)
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    assert result.json() == {
        "state": "admitted",
        "run_id": "run-write",
        "session_id": "session-a",
        "task_id": "task-a",
        "session_generation": fixtures.GENERATION,
        "pod_name": "chat-a",
        "sandbox_uid": "chat-pod",
        "attempt": 1,
        "lease_generation": 1,
        "image_digest": runtime[5].image_digest,
    }
    assert (await resume(client, runtime)).json() == result.json()
    assert fixtures.finalization.execution(runtime) == before
    assert completion.publication.session(transport) == owner
    assert fixtures.finalization.header(runtime) == header
    if phase != "admitted":
        assert (await completion.complete(client, runtime)).status_code == (409 if phase == "terminal" else 200)


@pytest.mark.parametrize("state", ["pending", "cancelled", "partially-bound"])
async def test_only_unbound_uncancelled_dispatch_is_eligible_for_a_fresh_sandbox(client, runtime, sts, monkeypatch, state):
    fixtures.history.prepare(runtime, message_id="run-write")
    supervisor(sts, monkeypatch)
    if state != "pending":
        metadata = fixtures.finalization.execution(runtime)
        if state == "cancelled":
            metadata["abort_command_id"] = {"S": "cancel-before-admission"}
        else:
            metadata["workload_binding"] = {"S": "partially-bound-pod"}
        fixtures.write_protected(runtime, metadata)
    before = fixtures.finalization.execution(runtime)
    response = await resume(client, runtime)
    assert response.status_code == (200 if state == "pending" else 409), response.text
    if state == "pending":
        assert response.json() == {
            "state": "unstarted",
            "run_id": "run-write",
            "session_id": "session-a",
            "task_id": "task-a",
            "session_generation": fixtures.GENERATION,
        }
    assert fixtures.finalization.execution(runtime) == before
    assert runtime[1].store._read("CHAT-LAUNCH#run-write", "LAUNCH") is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("current_attempt", {"N": "2"}),
        ("current_credential_epoch", {"N": "2"}),
        ("workload_binding", {"S": "other-pod"}),
        ("repo", {"S": "chat/other-session"}),
        ("status", {"S": "pending"}),
    ],
)
async def test_refuses_changed_execution_binding(client, runtime, ready, field, value):
    item = fixtures.finalization.execution(runtime)
    item[field] = value
    fixtures.write_protected(runtime, item)
    assert (await resume(client, runtime)).status_code == 404
    assert fixtures.finalization.execution(runtime) == item


@pytest.mark.parametrize("change", [{"run_id": "foreign-run"}, {"envelope_digest": "f" * 64}])
async def test_refuses_forged_root(client, runtime, ready, change):
    assert (await resume(client, runtime, **change)).status_code == 404


@pytest.mark.parametrize("binding_field,value", [("tenant_id", "foreign-tenant"), ("personas", ["reviewer"])])
async def test_refuses_other_tenant_or_persona_supervision(client, runtime, ready, monkeypatch, binding_field, value):
    bindings = json.loads(os.environ["ADP_MODEL_ROOT_BINDINGS"])
    bindings[0][binding_field] = value
    monkeypatch.setenv("ADP_MODEL_ROOT_BINDINGS", json.dumps(bindings))
    assert (await resume(client, runtime)).status_code == 404


async def test_sandbox_and_worker_cannot_obtain_recovery_binding(client, runtime, ready, sts):
    body = {field: value for field, value in fixtures.finalization.body(runtime).items() if field in {"run_id", "envelope_digest"}}
    assert (await client.post(RESUME, json=body, headers={"Authorization": f"Bearer {ready}"})).status_code == 403
    sts["role"] = "worker"
    assert (await resume(client, runtime)).status_code == 403


@pytest.mark.parametrize("change", ["attempt", "launch", "storage"])
async def test_ambiguous_or_changed_snapshot_never_returns_recovery_authority(client, runtime, ready, monkeypatch, change):
    protected = runtime[1].store
    original = protected.client.transact_write_items

    def transact(**kwargs):
        if all("ConditionCheck" in item for item in kwargs["TransactItems"]):
            if change == "storage":
                raise EndpointConnectionError(endpoint_url="https://storage.example.test")
            if change == "attempt":
                item = fixtures.finalization.execution(runtime)
                item["current_attempt"] = {"N": "2"}
                fixtures.write_protected(runtime, item)
            else:
                protected.client.delete_item(TableName=protected.table, Key={"pk": {"S": "CHAT-LAUNCH#run-write"}, "sk": {"S": "LAUNCH"}})
        return original(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", transact)
    assert (await resume(client, runtime)).status_code == (503 if change == "storage" else 409)


async def test_cancellation_racing_fresh_dispatch_lookup_prevents_launch(client, runtime, sts, monkeypatch):
    fixtures.history.prepare(runtime, message_id="run-write")
    supervisor(sts, monkeypatch)
    protected = runtime[1].store
    original = protected.client.transact_write_items

    def transact(**kwargs):
        item = fixtures.finalization.execution(runtime)
        item["abort_command_id"] = {"S": "cancel-before-launch"}
        fixtures.write_protected(runtime, item)
        return original(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", transact)
    assert (await resume(client, runtime)).status_code == 409
