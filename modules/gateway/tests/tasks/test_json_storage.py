"""Finite JSON numbers survive physical storage without narrowing the API domain."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from src.tasks.json_storage import JSON_ENCODING_FIELD, decode_record_json, encode_json_updates, encode_record_json
from src.tasks.records import META_SORT_KEY, TaskState, payload_digest, task_authority_partition, task_partition, task_run_grant_sort_key
from src.tasks.store import _serialize_authority
from tests.tasks import test_store as storage_tests


@pytest.fixture
def client():
    yield from storage_tests.client.__wrapped__()


@pytest.fixture
def store(client):
    return storage_tests.store.__wrapped__(client)


@pytest.mark.parametrize("number", [1e200, -1e200, 1e-200, -1e-200, 5e-324, 1.7976931348623157e308])
def test_extreme_input_accepts_resolves_and_replays_without_digest_drift(store, client, number):
    request = storage_tests._request(request_payload={"instructions": "inspect", "inputs": {"number": number}})
    first = store.accept(request)
    row = store.read_task(first.task_id)
    assert row["input_payload"] == request.request_payload
    assert payload_digest(row["input_payload"]) == first.request_digest
    assert store.resolve_work(request.dispatch_id, expected_kind="dispatch")["task_id"] == first.task_id
    assert store.accept(request).task_id == first.task_id
    if hasattr(store, "bind_runtime_attempt"):
        store.bind_runtime_attempt(
            task_id=request.task_id, invocation_id=request.invocation_id, generation=1, runtime_attempt_id=str(uuid.uuid4()), expected_version=1
        )
    raw = client.get_item(TableName=store.table_name, Key={"event_id": {"S": task_partition(first.task_id)}, "arrived_at": {"S": META_SORT_KEY}})[
        "Item"
    ]
    assert set(raw["input_payload"]) == {"S"}
    assert raw[JSON_ENCODING_FIELD] == {"L": [{"S": "input_payload"}]}
    grant = store._get_authority(task_authority_partition(request.tenant), task_run_grant_sort_key(invocation_id=request.invocation_id, generation=1))
    operand = _serialize_authority({":grant_input": grant["input"]})[":grant_input"]
    raw_grant = client.get_item(
        TableName=store.authority_table_name,
        Key={
            "pk": {"S": task_authority_partition(request.tenant)},
            "sk": {"S": task_run_grant_sort_key(invocation_id=request.invocation_id, generation=1)},
        },
    )["Item"]
    assert operand == raw_grant["input"]


def test_command_event_and_terminal_result_preserve_json(store):
    request = storage_tests._request()
    store.accept(request)
    command_id = str(uuid.uuid4())
    payload = {"text": "more: 1e-200"}
    store.insert_command(
        task_id=request.task_id,
        command_id=command_id,
        kind="input",
        payload=payload,
        author="svc-principal-1",
        authority_expires_at=storage_tests.NOW + timedelta(minutes=10),
        expected_version=1,
    )
    assert store.read_commands(task_id=request.task_id)[0]["payload"] == payload
    store.append_event(task_id=request.task_id, expected_sequence=2, kind="progress.updated", data={"number": 1e200})
    assert store.read_events(task_id=request.task_id, after_sequence=2)[0]["data"] == {"number": 1e200}
    store.transition(task_id=request.task_id, expected_version=2, target_state=TaskState.QUEUED)
    store.transition(task_id=request.task_id, expected_version=3, target_state=TaskState.RUNNING)
    result = {"usage": {"total_usd": 1e-200}}
    # The reviewer adds the attempt-bound transition API; exercise it when
    # checking the patch against that newer repository snapshot as well.
    attempt_arguments = {}
    if hasattr(store, "bind_runtime_attempt"):
        attempt_id = str(uuid.uuid4())
        store.bind_runtime_attempt(
            task_id=request.task_id, invocation_id=request.invocation_id, generation=1, runtime_attempt_id=attempt_id, expected_version=4
        )
        attempt_arguments = {"invocation_id": request.invocation_id, "generation": 1, "runtime_attempt_id": attempt_id}
        store.commit_turn(
            task_id=request.task_id,
            invocation_id=request.invocation_id,
            generation=1,
            runtime_attempt_id=attempt_id,
            turn_number=1,
            turn_id=str(uuid.uuid4()),
            command_ids=[command_id],
            expected_version=store.read_task(request.task_id)["version"],
        )
    store.transition(
        task_id=request.task_id,
        expected_version=store.read_task(request.task_id)["version"],
        target_state=TaskState.COMPLETED,
        attributes={"result": result},
        **attempt_arguments,
    )
    assert store.read_task(request.task_id)["result"] == result
    snapshot = store.read_task(request.task_id)
    encoded = encode_json_updates(snapshot, {"result": {"usage": {"total_usd": 0.5}}})
    assert encoded["result"] == {"usage": {"total_usd": 0.5}}
    assert encoded[JSON_ENCODING_FIELD] == []


def test_user_map_cannot_spoof_physical_encoding_and_normal_records_stay_native(store, client):
    user = {JSON_ENCODING_FIELD: ["number"], "number": "1e+200", "record_type": "TASK", "input_payload": "not JSON"}
    request = storage_tests._request(request_payload={"instructions": "inspect", "inputs": user})
    accepted = store.accept(request)
    restored = store.read_task(accepted.task_id)["input_payload"]
    assert restored["inputs"] == user
    raw = client.get_item(TableName=store.table_name, Key={"event_id": {"S": task_partition(accepted.task_id)}, "arrived_at": {"S": META_SORT_KEY}})[
        "Item"
    ]
    assert JSON_ENCODING_FIELD not in raw
    assert "M" in raw["input_payload"]


def test_closed_metadata_rejects_unknown_column_and_noncanonical_encoded_json():
    with pytest.raises(ValueError):
        decode_record_json({"record_type": "TASK", JSON_ENCODING_FIELD: ["scope"], "scope": "{}"})
    with pytest.raises(ValueError):
        decode_record_json({"record_type": "TASK", JSON_ENCODING_FIELD: ["input_payload"], "input_payload": '{"x": NaN}'})
    normal = {"record_type": "TASK", "input_payload": {"number": 1.5}}
    assert encode_record_json(normal) == normal


def test_stored_model_operation_content_restores_through_repository_boundary(store, client):
    from src.tasks.records import base_item, task_ops_partition
    from src.tasks.store import _serialize

    task_id = storage_tests._request().task_id
    row = base_item(
        partition=task_ops_partition(task_id),
        sort_key="MODEL#fixture",
        record_type="TASK_OPS",
        scope={"tenant": "tenant-a", "canonical_principal": "svc-principal-1"},
    ) | {
        "content": [{"type": "text", "text": "answer"}],
        "usage": {"estimated_usd": 1e-200},
        "receipt": {"metadata": {"finite": 1e200}},
    }
    client.put_item(TableName=store.table_name, Item=_serialize(row))
    restored = store._get(task_ops_partition(task_id), "MODEL#fixture")
    assert restored["content"] == row["content"]
    assert restored["usage"] == row["usage"]
    assert restored["receipt"] == row["receipt"]
