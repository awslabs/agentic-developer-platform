"""Supervisor completion across protected and owner stores, using storage emulators."""

from copy import deepcopy

import pytest
from botocore.exceptions import EndpointConnectionError

from tests.agentauth import test_chat_terminal_publication as publication

fixtures = publication.fixtures
client = publication.client
runtime = publication.runtime
store = publication.store
sts = publication.sts
capability = publication.capability
retained_input_table = publication.retained_input_table
ready = publication.ready
registered_owner = publication.registered_owner
transport = publication.transport
consumer = publication.consumer
COMPLETE = "/internal/v1/agent/chat/data/complete"


async def complete(client, runtime, **changes):
    return await fixtures.finalization.request(client, runtime, COMPLETE, **changes)


def receipt(runtime):
    return fixtures.outbox(runtime).get("completion_receipt")


@pytest.fixture
async def delivered(client, runtime, ready, transport, consumer):
    response = await fixtures.finalization.request(client, runtime)
    assert response.status_code == 200, response.text
    consumer._process_response(publication.payload(transport))
    return response.json()


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled", "interrupted"])
async def test_delivery_completion_atomically_unlocks_only_its_thread(client, runtime, ready, transport, consumer, outcome):
    if outcome == "completed":
        await fixtures.save_reply(client, ready)
    elif outcome == "failed":
        assert (await fixtures.results.commit(client, ready)).status_code == 200
    elif outcome == "cancelled":
        fixtures.results.cancel(runtime)
    row = publication.session(transport)
    row["threads"]["other-thread"] = {"processing_task_id": "unrelated-task"}
    transport.sessions.put_item(Item=row)
    terminal = await fixtures.finalization.request(client, runtime)
    assert terminal.status_code == 200
    consumer._process_response(publication.payload(transport))
    before = publication.session(transport)
    assert before["threads"]["thread-a"]["processing_task_id"] == "task-a"
    result = await complete(client, runtime)
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    assert result.json() == {
        "run_id": "run-write",
        "session_id": "session-a",
        "attempt": 1,
        "lease_generation": 1,
        "sandbox_uid": "chat-pod",
        "delivery_id": publication.payload(transport)["delivery_id"],
        "task_id": "task-a",
        "session_generation": fixtures.GENERATION,
        "processing_lock_released": True,
        "input_acknowledgement_ready": True,
        "completed_at": runtime[-1],
    }
    after = publication.session(transport)
    assert after["threads"]["thread-a"]["processing_task_id"] == ""
    assert after["threads"]["other-thread"] == before["threads"]["other-thread"]
    assert after["messages"] == before["messages"]
    assert after["completed_terminal_deliveries"][result.json()["delivery_id"]] == {
        **before["threads"]["thread-a"]["terminal_delivery"],
        "thread_id": "thread-a",
        "status": "completed",
    }
    assert receipt(runtime) is not None
    assert (await complete(client, runtime)).json() == result.json()
    assert publication.session(transport) == after
    assert (await fixtures.finalization.request(client, runtime)).json() == terminal.json()
    consumer.sqs.send_message.assert_not_called()
    assert transport.client.send_message.call_count == consumer.ws_router._client.post_to_connection.call_count == 1


@pytest.mark.parametrize("phase", ["unfinalized", "queued", "persisted"])
async def test_missing_terminal_or_confirmed_delivery_never_unlocks(client, runtime, ready, transport, consumer, monkeypatch, phase):
    assert (await fixtures.finalization.request(client, runtime, "/internal/v1/agent/chat/data/teardown")).status_code == 200
    if phase != "unfinalized":
        assert (await fixtures.finalization.request(client, runtime)).status_code == 200
    if phase == "persisted":
        monkeypatch.setattr(consumer.ws_router, "route", lambda *args: False)
        with pytest.raises(RuntimeError):
            consumer._process_response(publication.payload(transport))
    before = publication.session(transport)
    result = await complete(client, runtime)
    assert result.status_code == 409, result.text
    assert publication.session(transport) == before
    assert not (fixtures.outbox(runtime) or {}).get("completion_receipt")


async def test_retry_and_duplicate_delivery_do_not_touch_a_subsequent_turn(client, runtime, delivered, transport, consumer):
    response = await complete(client, runtime)
    assert response.status_code == 200
    old = publication.payload(transport)
    row = publication.session(transport)
    row["threads"]["thread-a"] = {"processing_task_id": "next-task", "terminal_delivery": {"task_id": "previous-newer-task"}}
    transport.sessions.put_item(Item=row)
    context = fixtures.finalization.header(runtime)
    context["chatLease"] = {"run_id": "next-run", "sandbox_uid": "next-pod", "generation": 2, "expires_at": runtime[-1] + 300}
    runtime[2].put_item(Item=context)
    assert (await complete(client, runtime)).json() == response.json()
    consumer._process_response(old)
    assert publication.session(transport) == row
    assert fixtures.finalization.header(runtime) == context
    assert consumer.ws_router._client.post_to_connection.call_count == 1
    with pytest.raises(ValueError, match="completed receipt"):
        consumer._process_response({**old, "text": "substituted result"})


@pytest.mark.parametrize("change", ["owner", "session-generation", "task", "delivery", "lease"])
@pytest.mark.parametrize("racing", [False, True])
async def test_changed_owner_task_receipt_or_lease_cannot_release_lock(client, runtime, delivered, transport, monkeypatch, change, racing):
    protected = runtime[1].store
    original = protected.client.transact_write_items

    def substitute():
        if change == "lease":
            header = fixtures.finalization.header(runtime)
            header["chatLease"]["generation"] += 1
            runtime[2].put_item(Item=header)
            return
        row = publication.session(transport)
        if change == "owner":
            row["owner_principal"] = "another-owner"
        elif change == "session-generation":
            row["created_at"] += 1
        elif change == "task":
            row["threads"]["thread-a"]["processing_task_id"] = "next-task"
        else:
            row["threads"]["thread-a"]["terminal_delivery"]["digest"] = "f" * 64
        transport.sessions.put_item(Item=row)

    def commit(**kwargs):
        if any("completion_receipt =" in item.get("Update", {}).get("UpdateExpression", "") for item in kwargs["TransactItems"]):
            substitute()
        return original(**kwargs)

    if racing:
        monkeypatch.setattr(protected.client, "transact_write_items", commit)
    else:
        substitute()
    response = await complete(client, runtime)
    assert response.status_code in {404, 409}, response.text
    assert receipt(runtime) is None
    assert publication.session(transport)["threads"]["thread-a"]["processing_task_id"] == ("next-task" if change == "task" else "task-a")
    assert "completed_terminal_deliveries" not in publication.session(transport)


@pytest.mark.parametrize("lost_reply", [False, True])
async def test_ambiguous_transaction_is_atomic_and_retryable(client, runtime, delivered, transport, monkeypatch, lost_reply):
    protected = runtime[1].store
    original = protected.client.transact_write_items
    before = publication.session(transport)

    def commit(**kwargs):
        if any("completion_receipt =" in item.get("Update", {}).get("UpdateExpression", "") for item in kwargs["TransactItems"]):
            if lost_reply:
                original(**kwargs)
            raise EndpointConnectionError(endpoint_url="https://storage.example.test")
        return original(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", commit)
    assert (await complete(client, runtime)).status_code == 503
    if lost_reply:
        assert receipt(runtime) is not None
        assert publication.session(transport)["threads"]["thread-a"]["processing_task_id"] == ""
    else:
        assert receipt(runtime) is None
        assert publication.session(transport) == before
    monkeypatch.setattr(protected.client, "transact_write_items", original)
    result = await complete(client, runtime)
    assert result.status_code == 200, result.text
    assert (await complete(client, runtime)).json() == result.json()
    assert publication.session(transport)["messages"] == before["messages"]


async def test_concurrent_other_thread_completion_is_not_lost(client, runtime, delivered, transport, monkeypatch):
    protected = runtime[1].store
    original = protected.client.transact_write_items
    other = {"chat-terminal-" + "b" * 64: {"task_id": "other-task", "thread_id": "other-thread", "status": "completed", "digest": "c" * 64}}

    def commit(**kwargs):
        if any("completion_receipt =" in item.get("Update", {}).get("UpdateExpression", "") for item in kwargs["TransactItems"]):
            transport.sessions.put_item(Item={**publication.session(transport), "completed_terminal_deliveries": other})
        return original(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", commit)
    assert (await complete(client, runtime)).status_code == 409
    assert receipt(runtime) is None
    assert publication.session(transport)["threads"]["thread-a"]["processing_task_id"] == "task-a"
    monkeypatch.setattr(protected.client, "transact_write_items", original)
    assert (await complete(client, runtime)).status_code == 200
    archive = publication.session(transport)["completed_terminal_deliveries"]
    assert len(archive) == 2 and all(archive[key] == value for key, value in other.items())


@pytest.mark.parametrize("change", ["missing", "pending", "empty"])
async def test_sent_owner_receipt_cannot_replace_missing_queue_acceptance(client, runtime, delivered, transport, change):
    item = fixtures.outbox(runtime)
    if change == "missing":
        del item["queue_message_id"]
    elif change == "empty":
        item["queue_message_id"] = {"S": ""}
    else:
        item["status"] = {"S": "pending"}
    fixtures.write_protected(runtime, item)
    before = publication.session(transport)
    assert (await complete(client, runtime)).status_code == 409
    assert publication.session(transport) == before and receipt(runtime) is None


@pytest.mark.parametrize("changes", [{"pod_uid": "other-pod"}, {"envelope_digest": "f" * 64}, {"pod_name": "other-pod"}])
async def test_substituted_supervisor_scope_cannot_complete(client, runtime, delivered, transport, changes):
    before = publication.session(transport)
    assert (await complete(client, runtime, **changes)).status_code == 404
    assert publication.session(transport) == before and receipt(runtime) is None


async def test_sandbox_capability_and_worker_role_cannot_complete(client, runtime, ready, delivered, transport, sts):
    before = publication.session(transport)
    response = await client.post(COMPLETE, json=fixtures.finalization.body(runtime), headers={"Authorization": f"Bearer {ready}"})
    assert response.status_code == 403
    sts["role"] = "worker"
    assert (await complete(client, runtime)).status_code == 403
    assert publication.session(transport) == before and receipt(runtime) is None


@pytest.mark.parametrize(
    "field,value", [("task_id", {"S": "another-task"}), ("lease_generation", {"N": "2"}), ("input_acknowledgement_ready", {"N": "1"})]
)
async def test_corrupt_completion_receipt_is_not_acknowledgement_authority(client, runtime, delivered, transport, field, value):
    assert (await complete(client, runtime)).status_code == 200
    item = deepcopy(fixtures.outbox(runtime))
    item["completion_receipt"]["M"][field] = value
    fixtures.write_protected(runtime, item)
    before = publication.session(transport)
    assert (await complete(client, runtime)).status_code == 503
    assert publication.session(transport) == before


@pytest.mark.parametrize("racing", [False, True])
@pytest.mark.parametrize("pending_kind", ["message", "registering", "registered"])
async def test_buffered_input_cannot_be_abandoned_by_completion(client, runtime, delivered, transport, monkeypatch, racing, pending_kind):
    protected = runtime[1].store
    original = protected.client.transact_write_items

    def append_pending():
        row = publication.session(transport)
        if pending_kind == "message":
            row["threads"]["thread-a"]["messages"].append({"role": "user", "content": "Follow up after this turn"})
        else:
            row["threads"]["thread-a"]["pending_turns"] = {"pending-id": {"status": pending_kind}}
        transport.sessions.put_item(Item=row)

    def commit(**kwargs):
        if any("completion_receipt =" in item.get("Update", {}).get("UpdateExpression", "") for item in kwargs["TransactItems"]):
            append_pending()
        return original(**kwargs)

    if racing:
        monkeypatch.setattr(protected.client, "transact_write_items", commit)
    else:
        append_pending()
    assert (await complete(client, runtime)).status_code == 409
    assert receipt(runtime) is None
    thread = publication.session(transport)["threads"]["thread-a"]
    assert thread["processing_task_id"] == "task-a"
    if pending_kind == "message":
        assert thread["messages"][-1]["role"] == "user"
    else:
        assert thread["pending_turns"]["pending-id"]["status"] == pending_kind


async def test_empty_pending_journal_allows_completion(client, runtime, delivered, transport):
    row = publication.session(transport)
    row["threads"]["thread-a"]["pending_turns"] = {}
    transport.sessions.put_item(Item=row)
    assert (await complete(client, runtime)).status_code == 200
