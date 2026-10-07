"""Storage-emulator checks for authenticated, retryable buffered chat inputs."""

import copy
import hashlib
import importlib
import json
from pathlib import Path
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws


@pytest.fixture
def pending(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "gateway/lambdas/ingest"))
    return importlib.import_module("pending_chat")


@pytest.fixture
def envelope():
    return {
        "message_id": "message-next",
        "task_id": "task-next",
        "session_id": "session-owner",
        "thread_id": "thread-owner",
        "tenant_id": "tenant-owner",
        "org_id": "tenant-owner",
        "user_id": "user-owner",
        "team_id": "team-owner",
        "channel": "webchat",
        "owner_principal": '["tenant-owner","tenant-owner","team-owner","user-owner","webchat"]',
        "session_generation": 1000,
        "agent_type": "intent-refinement",
        "mode": "chat",
        "message": "Retain this entire follow-up",
        "attachments": ["art_owned"],
        "platform_data": {"tenant_id": "tenant-owner", "role": "user"},
        "enqueued_at": 1100,
        "arrived_at": "2026-10-06T00:00:00Z",
        "connection_id": "connection-owner",
    }


@pytest.fixture
def table(envelope):
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="pending-sessions",
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.put_item(
            Item={
                "session_id": envelope["session_id"],
                "owner_principal": envelope["owner_principal"],
                "created_at": 1000,
                "expires_at": 9999,
                "channel": "webchat",
                "threads": {"thread-owner": {"processing_task_id": "task-active", "messages": []}},
            }
        )
        yield table


def thread(table):
    return table.get_item(Key={"session_id": "session-owner"}, ConsistentRead=True)["Item"][
        "threads"
    ]["thread-owner"]


def registered(envelope):
    return json.dumps(
        {**envelope, "persona": envelope["agent_type"]}, sort_keys=True, separators=(",", ":")
    )


def buffer(pending, table, envelope, register=None):
    return pending.buffer_pending_turn(
        table,
        envelope,
        processing_task="task-active",
        now=1100.25,
        register=register or registered,
    )


def recovery_binding(envelope):
    return {
        **{field: envelope[field] for field in ("session_id", "thread_id", "owner_principal", "session_generation")},
        "processing_task": "task-active",
        "pending_id": hashlib.sha256(envelope["message_id"].encode()).hexdigest(),
    }


def retain_unregistered(pending, table, envelope):
    with pytest.raises(TimeoutError):
        buffer(pending, table, envelope, Mock(side_effect=TimeoutError("not committed")))


def test_recovery_registers_original_request_without_rebuffering(pending, table, envelope):
    retain_unregistered(pending, table, envelope)
    before = thread(table)
    register = Mock(side_effect=registered)
    pending.recover_pending_turn(table, recovery_binding(envelope), now=1200, register=register)
    register.assert_called_once_with(envelope)
    after = thread(table)
    assert after["messages"] == before["messages"]
    assert after["processing_task_id"] == before["processing_task_id"]
    assert next(iter(after["pending_turns"].values()))["status"] == "registered"
    pending.recover_pending_turn(table, recovery_binding(envelope), now=1201, register=register)
    assert register.call_count == 1


@pytest.mark.parametrize("changes", [
    {"session_id": "another-session"}, {"thread_id": "another-thread"}, {"owner_principal": "another-owner"},
    {"session_generation": 1001}, {"session_generation": True}, {"processing_task": "another-task"},
    {"pending_id": "0" * 64}, {"message": "injected"}, {"pending_id": "invalid"},
])
def test_recovery_rejects_substituted_binding(pending, table, envelope, changes):
    retain_unregistered(pending, table, envelope)
    before = thread(table)
    register = Mock()
    with pytest.raises(pending.PendingChatConflict):
        pending.recover_pending_turn(table, {**recovery_binding(envelope), **changes}, now=1200, register=register)
    register.assert_not_called()
    assert thread(table) == before


def test_recovery_does_not_recreate_disappeared_pending_input(pending, table, envelope, monkeypatch):
    retain_unregistered(pending, table, envelope)
    load = pending._load
    calls = 0

    def disappear(*args):
        nonlocal calls
        calls += 1
        result = load(*args)
        if calls == 1:
            row = table.get_item(Key={"session_id": envelope["session_id"]})["Item"]
            row["threads"][envelope["thread_id"]]["pending_turns"] = {}
            row["threads"][envelope["thread_id"]]["messages"] = []
            table.put_item(Item=row)
        return result

    monkeypatch.setattr(pending, "_load", disappear)
    register = Mock()
    with pytest.raises(pending.PendingChatConflict, match="disappeared"):
        pending.recover_pending_turn(table, recovery_binding(envelope), now=1200, register=register)
    register.assert_not_called()
    assert thread(table)["pending_turns"] == {}
    assert thread(table)["messages"] == []


def test_recovery_cannot_write_after_processing_task_changes(pending, table, envelope):
    retain_unregistered(pending, table, envelope)

    def race(request):
        row = table.get_item(Key={"session_id": envelope["session_id"]})["Item"]
        row["threads"][envelope["thread_id"]]["processing_task_id"] = "next-task"
        table.put_item(Item=row)
        return registered(request)

    with pytest.raises(pending.PendingChatConflict, match="owner or task changed"):
        pending.recover_pending_turn(table, recovery_binding(envelope), now=1200, register=race)
    assert thread(table)["processing_task_id"] == "next-task"
    assert next(iter(thread(table)["pending_turns"].values()))["status"] == "registering"


def test_retains_exact_registered_envelope_and_buffer_marker(pending, table, envelope):
    result = buffer(pending, table, envelope)
    stored = thread(table)
    assert stored["processing_task_id"] == "task-active"
    pending_id = hashlib.sha256(envelope["message_id"].encode()).hexdigest()
    record = stored["pending_turns"][pending_id]
    assert record == {
        "status": "registered",
        "request_json": pending._canonical(envelope),
        "envelope_json": result,
    }
    assert json.loads(result)["attachments"] == ["art_owned"]
    assert stored["messages"][0]["pending_id"] == pending_id
    assert stored["messages"][0]["role"] == "user"


def test_duplicate_preserves_original_task_and_registration(pending, table, envelope):
    register = Mock(side_effect=registered)
    first = buffer(pending, table, envelope, register)
    retry = {**envelope, "task_id": "retry-task", "enqueued_at": 1200, "arrived_at": "later"}
    assert buffer(pending, table, retry, register) == first
    assert register.call_count == 1
    assert len(thread(table)["messages"]) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("message", "substitution"),
        ("attachments", ["art_other"]),
        ("agent_type", "developer"),
        ("user_id", "another-user"),
        ("tenant_id", "another-tenant"),
        ("team_id", "another-team"),
    ],
)
def test_duplicate_cannot_replace_authenticated_input(pending, table, envelope, field, value):
    buffer(pending, table, envelope)
    before = thread(table)
    register = Mock()
    with pytest.raises(pending.PendingChatConflict, match="input changed"):
        buffer(pending, table, {**envelope, field: value}, register)
    register.assert_not_called()
    assert thread(table) == before


def test_lost_registration_response_retries_retained_bytes(pending, table, envelope):
    observed = []

    def lost(request):
        observed.append(copy.deepcopy(request))
        raise TimeoutError("synthetic lost response")

    with pytest.raises(TimeoutError):
        buffer(pending, table, envelope, lost)
    assert next(iter(thread(table)["pending_turns"].values()))["status"] == "registering"
    retry = {**envelope, "task_id": "different-task", "arrived_at": "later"}
    register = Mock(side_effect=registered)
    buffer(pending, table, retry, register)
    assert register.call_args.args == (observed[0],)
    assert len(thread(table)["messages"]) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_principal", "different-owner"),
        ("created_at", 2000),
        ("channel", "slack"),
        ("expires_at", 1000),
    ],
)
def test_refuses_changed_session_before_registration(pending, table, envelope, field, value):
    row = table.get_item(Key={"session_id": envelope["session_id"]})["Item"]
    row[field] = value
    table.put_item(Item=row)
    register = Mock()
    with pytest.raises(pending.PendingChatConflict, match="owner or task changed"):
        buffer(pending, table, envelope, register)
    register.assert_not_called()
    assert "pending_turns" not in thread(table)


def test_completion_racing_initial_write_does_not_buffer_or_register(
    pending, table, envelope, monkeypatch
):
    update = table.update_item

    def complete_first(**kwargs):
        update(
            Key={"session_id": envelope["session_id"]},
            UpdateExpression="SET threads.#thread.processing_task_id = :task",
            ExpressionAttributeNames={"#thread": envelope["thread_id"]},
            ExpressionAttributeValues={":task": "new-task"},
        )
        return update(**kwargs)

    monkeypatch.setattr(table, "update_item", complete_first)
    register = Mock()
    with pytest.raises(pending.PendingChatConflict, match="owner or task changed"):
        buffer(pending, table, envelope, register)
    register.assert_not_called()
    assert thread(table)["processing_task_id"] == "new-task"
    assert thread(table)["messages"] == []


def test_concurrent_arrival_during_registration_is_preserved(pending, table, envelope):
    other = {
        **envelope,
        "message_id": "other-message",
        "task_id": "other-task",
        "message": "Second input",
    }

    def concurrent(request):
        buffer(pending, table, other)
        return registered(request)

    buffer(pending, table, envelope, concurrent)
    stored = thread(table)
    assert len(stored["pending_turns"]) == len(stored["messages"]) == 2
    assert {record["status"] for record in stored["pending_turns"].values()} == {"registered"}


def test_lost_final_storage_response_is_idempotent(pending, table, envelope, monkeypatch):
    update = table.update_item

    def lost(**kwargs):
        result = update(**kwargs)
        if (
            next(iter(kwargs["ExpressionAttributeValues"][":pending"].values()))["status"]
            == "registered"
        ):
            raise TimeoutError("synthetic lost storage response")
        return result

    monkeypatch.setattr(table, "update_item", lost)
    with pytest.raises(TimeoutError):
        buffer(pending, table, envelope)
    monkeypatch.setattr(table, "update_item", update)
    register = Mock()
    assert buffer(pending, table, envelope, register) == registered(envelope)
    register.assert_not_called()
    assert len(thread(table)["messages"]) == 1


def test_unavailable_storage_never_calls_registration(pending, table, envelope, monkeypatch):
    monkeypatch.setattr(
        table,
        "update_item",
        Mock(
            side_effect=ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException"}},
                "UpdateItem",
            )
        ),
    )
    register = Mock()
    with pytest.raises(ClientError):
        buffer(pending, table, envelope, register)
    register.assert_not_called()
    assert thread(table)["messages"] == []


@pytest.mark.parametrize(
    "field,value", [("task_id", "forged"), ("session_generation", 2000), ("persona", "developer")]
)
def test_mismatched_registration_never_becomes_ready(pending, table, envelope, field, value):
    def substituted(request):
        return pending._canonical({**json.loads(registered(request)), field: value})

    with pytest.raises(pending.PendingChatConflict, match="registration changed"):
        buffer(pending, table, envelope, substituted)
    assert next(iter(thread(table)["pending_turns"].values()))["status"] == "registering"
    assert thread(table)["processing_task_id"] == "task-active"


def test_capacity_refusal_does_not_drop_existing_inputs(pending, table, envelope):
    for index in range(32):
        buffer(pending, table, {**envelope, "message_id": f"message-{index}"})
    before = thread(table)
    register = Mock()
    with pytest.raises(pending.PendingChatConflict, match="capacity"):
        buffer(pending, table, envelope, register)
    register.assert_not_called()
    assert thread(table) == before


def test_concurrent_append_racing_initial_write_is_not_overwritten(
    pending, table, envelope, monkeypatch
):
    update = table.update_item
    raced = False

    def append_first(**kwargs):
        nonlocal raced
        if not raced:
            raced = True
            update(
                Key={"session_id": envelope["session_id"]},
                UpdateExpression="SET threads.#thread.messages = :messages",
                ExpressionAttributeNames={"#thread": envelope["thread_id"]},
                ExpressionAttributeValues={
                    ":messages": [{"role": "user", "content": "Concurrent input"}]
                },
            )
        return update(**kwargs)

    monkeypatch.setattr(table, "update_item", append_first)
    buffer(pending, table, envelope)
    assert [entry["content"] for entry in thread(table)["messages"]] == [
        "Concurrent input",
        envelope["message"],
    ]


def test_owner_change_during_registration_never_records_success(pending, table, envelope):
    def change_owner(request):
        table.update_item(
            Key={"session_id": envelope["session_id"]},
            UpdateExpression="SET owner_principal = :owner",
            ExpressionAttributeValues={":owner": "replacement-owner"},
        )
        return registered(request)

    with pytest.raises(pending.PendingChatConflict, match="owner or task changed"):
        buffer(pending, table, envelope, change_owner)
    assert next(iter(thread(table)["pending_turns"].values()))["status"] == "registering"


def test_lost_initial_write_response_reuses_retained_identity(
    pending, table, envelope, monkeypatch
):
    update = table.update_item

    def lost(**kwargs):
        update(**kwargs)
        raise TimeoutError("synthetic lost initial write response")

    monkeypatch.setattr(table, "update_item", lost)
    register = Mock(side_effect=registered)
    with pytest.raises(TimeoutError):
        buffer(pending, table, envelope, register)
    register.assert_not_called()
    monkeypatch.setattr(table, "update_item", update)
    buffer(pending, table, {**envelope, "task_id": "retry-task"}, register)
    assert register.call_args.args == (envelope,)
    assert len(thread(table)["messages"]) == 1
