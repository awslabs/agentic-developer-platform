"""Durable lifecycle evidence, with HTTP authentication and storage emulation."""

import json
import os
from copy import deepcopy

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.bootstrap import envelope_digest
from tests.agentauth.test_chat_data_routes import admit, document
from tests.agentauth.test_chat_sandbox_exit import EXIT, supervisor
from tests.agentauth.test_work_producer import proof

pytest_plugins = ("tests.agentauth.test_chat_data_routes",)
TEARDOWN = "/internal/v1/agent/chat/data/teardown"


async def observe(client, runtime, path=EXIT, **changes):
    body = {**document(runtime), **changes}
    return await client.post(path, json=body, headers={"X-Adp-Producer-Proof": proof(envelope_digest(body))})


def receipt(runtime):
    return runtime[1].store._read("CHAT-LAUNCH#run-a", "TEARDOWN")


@pytest.fixture
async def admitted(client, runtime, sts, monkeypatch):
    supervisor(sts, monkeypatch)
    assert (await admit(client, runtime)).status_code == 200
    state = {"exited": True, "removed": False, "observations": []}

    def exited(**kwargs):
        state["observations"].append(("exit", kwargs))
        return state["exited"]

    def removed(**kwargs):
        state["observations"].append(("removed", kwargs))
        return state["removed"]

    monkeypatch.setattr(runtime[1].workloads, "has_exited", exited)
    monkeypatch.setattr(runtime[1].workloads, "is_absent", removed)
    return state


async def test_exit_then_removal_is_durable_idempotent_and_nonterminal(client, runtime, admitted):
    store = runtime[1].store
    before = store._read("TENANT#tenant", "EXEC#run-a")
    header = runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]
    admitted["exited"] = False
    assert (await observe(client, runtime)).json()["terminated"] is False
    assert receipt(runtime) is None
    admitted["exited"] = True
    assert (await observe(client, runtime)).json()["terminated"] is True
    exited = receipt(runtime)
    assert exited["binding"]["M"]["pod_uid"] == {"S": "chat-pod"}
    assert "exited_at" in exited and "removed_at" not in exited
    assert (await observe(client, runtime, TEARDOWN)).json()["removed"] is False
    assert receipt(runtime) == exited
    admitted["removed"] = True
    response = await observe(client, runtime, TEARDOWN)
    assert response.json() == {"run_id": "run-a", "pod_uid": "chat-pod", "removed": True}
    assert response.headers["cache-control"] == "no-store"
    removed = receipt(runtime)
    assert "removed_at" in removed and removed["exited_at"] == exited["exited_at"]
    observations = deepcopy(admitted["observations"])
    admitted.update(exited=False, removed=False)
    assert (await observe(client, runtime)).json()["terminated"] is True
    assert (await observe(client, runtime, TEARDOWN)).json()["removed"] is True
    assert receipt(runtime) == removed
    assert admitted["observations"] == observations
    assert store._read("TENANT#tenant", "EXEC#run-a") == before
    assert runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"] == header


async def test_missing_pod_without_saved_exit_never_proves_teardown(client, runtime, admitted):
    admitted.update(exited=False, removed=True)
    assert (await observe(client, runtime, TEARDOWN)).status_code == 404
    assert not admitted["observations"]
    assert receipt(runtime) is None


@pytest.mark.parametrize("path", [EXIT, TEARDOWN])
@pytest.mark.parametrize("change", [{"pod_uid": "foreign"}, {"pod_name": "foreign"}, {"envelope_digest": "f" * 64}, {"run_id": "foreign"}])
async def test_foreign_binding_cannot_observe_or_replay(client, runtime, admitted, path, change):
    assert (await observe(client, runtime)).status_code == 200
    before = receipt(runtime)
    admitted["observations"].clear()
    assert (await observe(client, runtime, path, **change)).status_code == 404
    assert not admitted["observations"]
    assert receipt(runtime) == before


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer sandbox-capability"}, {"X-Adp-Producer-Proof": "forged"}])
async def test_teardown_requires_signed_supervisor_identity(client, runtime, admitted, headers):
    response = await client.post(TEARDOWN, json=document(runtime), headers=headers)
    assert response.status_code == 403
    assert not admitted["observations"]


async def test_worker_role_cannot_record_teardown(client, runtime, admitted, sts):
    sts["role"] = "worker"
    assert (await observe(client, runtime, TEARDOWN)).status_code == 403
    assert not admitted["observations"]


async def test_expired_or_cancelled_model_authority_does_not_prevent_cleanup(client, runtime, admitted, monkeypatch):
    store = runtime[1].store

    def denied_grant(**kwargs):
        raise AssertionError("cleanup must not acquire model authority")

    monkeypatch.setattr(store, "live_grant", denied_grant)
    store.client.update_item(
        TableName=store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-a"}},
        UpdateExpression="SET abort_command_id = :abort",
        ExpressionAttributeValues={":abort": {"S": "owner-stop"}},
    )
    assert (await observe(client, runtime)).json()["terminated"] is True
    admitted["removed"] = True
    assert (await observe(client, runtime, TEARDOWN)).json()["removed"] is True
    assert store._read("TENANT#tenant", "EXEC#run-a")["status"] == {"S": "active"}


@pytest.mark.parametrize("path", [EXIT, TEARDOWN])
async def test_lost_write_response_recovers_without_reobserving_deleted_pod(client, runtime, admitted, monkeypatch, path):
    if path == TEARDOWN:
        assert (await observe(client, runtime)).status_code == 200
        admitted["removed"] = True
    store = runtime[1].store
    original = store.client.transact_write_items

    def lost_response(**kwargs):
        original(**kwargs)
        raise EndpointConnectionError(endpoint_url="https://storage.test")

    monkeypatch.setattr(store.client, "transact_write_items", lost_response)
    assert (await observe(client, runtime, path)).status_code == 503
    stored = receipt(runtime)
    assert stored is not None
    monkeypatch.setattr(store.client, "transact_write_items", original)
    admitted.update(exited=False, removed=False)
    admitted["observations"].clear()
    response = await observe(client, runtime, path)
    assert response.status_code == 200, response.text
    assert not admitted["observations"]
    assert receipt(runtime) == stored


@pytest.mark.parametrize("path", [EXIT, TEARDOWN])
@pytest.mark.parametrize(
    "field,value", [("current_attempt", {"N": "2"}), ("current_credential_epoch", {"N": "2"}), ("workload_binding", {"S": "foreign"})]
)
async def test_changed_execution_fences_evidence_commit(client, runtime, admitted, monkeypatch, path, field, value):
    if path == TEARDOWN:
        assert (await observe(client, runtime)).status_code == 200
        admitted["removed"] = True
    before = receipt(runtime)
    store = runtime[1].store
    original = store.client.transact_write_items

    def race(**kwargs):
        store.client.update_item(
            TableName=store.table,
            Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-a"}},
            UpdateExpression="SET #field = :value",
            ExpressionAttributeNames={"#field": field},
            ExpressionAttributeValues={":value": value},
        )
        return original(**kwargs)

    monkeypatch.setattr(store.client, "transact_write_items", race)
    assert (await observe(client, runtime, path)).status_code == 404
    assert receipt(runtime) == before
    monkeypatch.setattr(store.client, "transact_write_items", original)
    admitted["observations"].clear()
    assert (await observe(client, runtime, path)).status_code == 404
    assert not admitted["observations"]


async def test_stale_exit_retry_cannot_erase_concurrent_removal(client, runtime, admitted, monkeypatch):
    assert (await observe(client, runtime)).status_code == 200
    store = runtime[1].store
    original = store.client.transact_write_items

    def removal_wins(**kwargs):
        store.client.update_item(
            TableName=store.table,
            Key={"pk": {"S": "CHAT-LAUNCH#run-a"}, "sk": {"S": "TEARDOWN"}},
            UpdateExpression="SET removed_at = :now",
            ExpressionAttributeValues={":now": {"N": str(runtime[-1])}},
        )
        return original(**kwargs)

    monkeypatch.setattr(store.client, "transact_write_items", removal_wins)
    assert (await observe(client, runtime)).status_code == 404
    assert "removed_at" in receipt(runtime)


async def test_tampered_receipt_cannot_be_replayed(client, runtime, admitted):
    assert (await observe(client, runtime)).status_code == 200
    store = runtime[1].store
    stored = receipt(runtime)
    stored["binding"]["M"]["pod_uid"] = {"S": "foreign"}
    store.client.put_item(TableName=store.table, Item=stored)
    admitted["observations"].clear()
    assert (await observe(client, runtime, TEARDOWN)).status_code == 404
    assert not admitted["observations"]


@pytest.mark.parametrize("path", [EXIT, TEARDOWN])
async def test_observation_scope_change_cannot_reuse_exit_evidence(client, runtime, admitted, monkeypatch, path):
    assert (await observe(client, runtime)).status_code == 200
    before = receipt(runtime)
    monkeypatch.setattr(runtime[1].workloads, "_namespace", "foreign-namespace")
    admitted["observations"].clear()
    assert (await observe(client, runtime, path)).status_code == 404
    assert not admitted["observations"]
    assert receipt(runtime) == before


@pytest.mark.parametrize("source", ["launch", "dispatch"])
async def test_registered_assignment_change_fences_observation_commit(client, runtime, admitted, monkeypatch, source):
    store = runtime[1].store
    original = store.client.transact_write_items

    def race(**kwargs):
        key, field = (
            ({"pk": {"S": "CHAT-LAUNCH#run-a"}, "sk": {"S": "LAUNCH"}}, "document")
            if source == "launch"
            else ({"pk": {"S": "INVOCATION#run-a"}, "sk": {"S": "DISPATCH"}}, "envelope_digest")
        )
        store.client.update_item(
            TableName=store.table,
            Key=key,
            UpdateExpression="SET #field = :value",
            ExpressionAttributeNames={"#field": field},
            ExpressionAttributeValues={":value": {"S": "changed"}},
        )
        return original(**kwargs)

    monkeypatch.setattr(store.client, "transact_write_items", race)
    assert (await observe(client, runtime)).status_code == 404
    assert receipt(runtime) is None


async def test_teardown_retains_disabled_by_default_boundary(client, runtime, admitted, monkeypatch):
    monkeypatch.delenv("ADP_CHAT_DATA_ENABLED")
    assert (await observe(client, runtime, TEARDOWN)).status_code == 503
    assert not admitted["observations"]


@pytest.mark.parametrize("field,value", [("tenant_id", "foreign"), ("personas", ["foreign"])])
async def test_supervisor_binding_does_not_broaden_tenant_or_persona(client, runtime, admitted, monkeypatch, field, value):
    bindings = json.loads(os.environ["ADP_MODEL_ROOT_BINDINGS"])
    bindings[0][field] = value
    monkeypatch.setenv("ADP_MODEL_ROOT_BINDINGS", json.dumps(bindings))
    assert (await observe(client, runtime)).status_code == 404
    assert not admitted["observations"]
    assert receipt(runtime) is None
