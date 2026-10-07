"""Persistent result retries preserve one durable outcome under the current fence."""

import asyncio
import hashlib
from threading import Barrier, Lock

import pytest
from botocore.exceptions import EndpointConnectionError

from tests.agentauth import test_chat_turn_mailbox as fixtures

client = fixtures.client
runtime = fixtures.runtime
store = fixtures.store
sts = fixtures.sts
retained_input_table = fixtures.retained_input_table
mailbox = fixtures.mailbox
TURN = {"PK": "session#session-a", "SK": "turn#run-user"}
HEADER = {"PK": "session#session-a", "SK": "header"}
ENTRY = {"PK": "session#session-a", "SK": "mailbox#00000001"}


async def candidate(mailbox):
    client, runtime, token, _, _ = mailbox
    response = await client.post(
        "/v1/chat/data/history/append",
        json={
            "run_id": "run-user",
            "session_id": "session-a",
            "idempotency_key": "reply",
            "expected_version": 1,
            "user_turn_id": "user_" + hashlib.sha256(b"run-user").hexdigest(),
            "content": "One durable answer",
            "tokens": 4,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    return {"run_id": "run-user", "session_id": "session-a", "outcome": "completed", "message_id": response.json()["message_id"]}


async def result(mailbox, payload):
    client, _, token, _, _ = mailbox
    return await client.post("/v1/chat/data/turn/result", json=payload, headers={"Authorization": f"Bearer {token}"})


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
@pytest.mark.parametrize("same_result", [True, False])
async def test_concurrent_persistent_results_commit_one_candidate_and_mailbox_transition(mailbox, monkeypatch, same_result):
    payload = await candidate(mailbox)
    runtime = mailbox[1]
    table, store = runtime[2], runtime[1].store
    original = store.client.transact_write_items
    barrier, lock = Barrier(2), Lock()

    def transact(**request):
        barrier.wait(timeout=10)
        with lock:
            return original(**request)

    monkeypatch.setattr(store.client, "transact_write_items", transact)
    alternative = payload if same_result else {"run_id": "run-user", "session_id": "session-a", "outcome": "failed"}
    responses = await asyncio.gather(result(mailbox, payload), result(mailbox, alternative))
    assert sorted(response.status_code for response in responses) == ([200, 200] if same_result else [200, 409])
    recorded = table.get_item(Key=TURN, ConsistentRead=True)["Item"]["result_candidate"]
    assert all(response.json() == recorded for response in responses if response.status_code == 200)
    assert table.get_item(Key=ENTRY)["Item"]["status"] == "result_recorded"
    assert store._read("TENANT#tenant", "EXEC#run-user")["chat_turn_sealed"] == {"BOOL": True}
    header = table.get_item(Key=HEADER)["Item"]
    assert header["sessionTurnSequence"] == 1 and header["historyNextOrdinal"] == 3


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
@pytest.mark.parametrize("outcome", ["completed", "failed"])
async def test_lost_persistent_result_response_replays_without_another_write(mailbox, monkeypatch, outcome):
    payload = await candidate(mailbox) if outcome == "completed" else {"run_id": "run-user", "session_id": "session-a", "outcome": "failed"}
    runtime = mailbox[1]
    table, store = runtime[2], runtime[1].store
    original = store.client.transact_write_items

    def transact(**request):
        original(**request)
        raise EndpointConnectionError(endpoint_url="https://storage.example.test")

    monkeypatch.setattr(store.client, "transact_write_items", transact)
    assert (await result(mailbox, payload)).status_code == 503
    before = table.scan(ConsistentRead=True)["Items"]
    execution = store._read("TENANT#tenant", "EXEC#run-user")
    monkeypatch.setattr(store.client, "transact_write_items", original)
    recorded = table.get_item(Key=TURN)["Item"]["result_candidate"]
    for _retry in range(3):
        response = await result(mailbox, payload)
        assert response.status_code == 200 and response.json() == recorded
    assert table.scan(ConsistentRead=True)["Items"] == before
    assert store._read("TENANT#tenant", "EXEC#run-user") == execution
    table.update_item(Key=HEADER, UpdateExpression="SET chatLease.generation = :next", ExpressionAttributeValues={":next": 2})
    assert (await result(mailbox, payload)).status_code == 404
    assert table.get_item(Key=TURN)["Item"]["result_candidate"] == recorded


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_lease_replacement_during_result_commit_leaves_no_partial_outcome(mailbox, monkeypatch):
    payload = await candidate(mailbox)
    runtime = mailbox[1]
    table, store = runtime[2], runtime[1].store
    original = store.client.transact_write_items

    def transact(**request):
        table.update_item(Key=HEADER, UpdateExpression="SET chatLease.generation = :next", ExpressionAttributeValues={":next": 2})
        return original(**request)

    monkeypatch.setattr(store.client, "transact_write_items", transact)
    assert (await result(mailbox, payload)).status_code == 404
    assert "result_candidate" not in table.get_item(Key=TURN)["Item"]
    assert table.get_item(Key=ENTRY)["Item"]["status"] == "queued"
    assert "chat_turn_sealed" not in store._read("TENANT#tenant", "EXEC#run-user")
