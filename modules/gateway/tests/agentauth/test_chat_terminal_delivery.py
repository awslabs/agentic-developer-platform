"""Terminal delivery intent is committed with the protected outcome, not sent early."""

import json

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.bootstrap import envelope_digest
from tests.agentauth import test_chat_history_write as history
from tests.agentauth import test_chat_turn_finalization as finalization
from tests.agentauth import test_chat_turn_result as results
from tests.agentauth.test_chat_delivery import GENERATION, OWNER, ROUTING
from tests.agentauth.test_chat_delivery import transport as transport_fixture

client = finalization.client
runtime = finalization.runtime
store = finalization.store
sts = finalization.sts
capability = finalization.capability
retained_input_table = finalization.retained_input_table
ready = finalization.ready
transport = transport_fixture
KEY = {"pk": {"S": "CHAT-DELIVERY#run-write"}, "sk": {"S": "TERMINAL"}}


@pytest.fixture(autouse=True)
def owner_transport(transport):
    return transport


@pytest.fixture(autouse=True)
def registered_owner(monkeypatch):
    prepare = history.prepare
    monkeypatch.setattr(history, "prepare", lambda runtime, **changes: prepare(runtime, **{**ROUTING, **changes}))


def outbox(runtime):
    protected = runtime[1].store
    return protected.client.get_item(TableName=protected.table, Key=KEY, ConsistentRead=True).get("Item")


def write_protected(runtime, item):
    protected = runtime[1].store
    protected.client.put_item(TableName=protected.table, Item=item)


async def save_reply(client, ready, content="Saved owner-only reply 😀"):
    response = await history.append(client, ready, content=content)
    assert response.status_code == 200, response.text
    message_id = response.json()["message_id"]
    assert (await results.commit(client, ready, outcome="completed", message_id=message_id)).status_code == 200
    return message_id


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled", "interrupted"])
async def test_terminal_delivery_is_atomic_owner_bound_and_repeatable(client, runtime, ready, outcome):
    if outcome == "completed":
        await save_reply(client, ready)
    elif outcome == "failed":
        assert (await results.commit(client, ready)).status_code == 200
    elif outcome == "cancelled":
        results.cancel(runtime)
    assert outbox(runtime) is None
    response = await finalization.request(client, runtime)
    assert response.status_code == 200, response.text
    item = outbox(runtime)
    assert item is not None
    document = json.loads(item["document"]["S"])
    assert document["version"] == 1 and item["status"] == {"S": "queued"}
    assert document["terminal"] == response.json() == results.turn(runtime)["terminal_result"]
    assert document["delivery"] == {
        "run_id": "run-write",
        "session_id": "session-a",
        "task_id": "task-a",
        "thread_id": "thread-a",
        "tenant_id": "tenant",
        "user_id": "human",
        "team_id": "team",
        "owner_principal": OWNER,
        "session_generation": GENERATION,
    }
    assert document["delivery_id"] == "chat-terminal-" + envelope_digest(response.json())
    assert document["terminal"]["automatic_replay_permitted"] is False
    assert document["content"]
    if outcome == "completed":
        assert document["content"] == "Saved owner-only reply 😀"
    assert finalization.execution(runtime)["chat_terminal_delivery_digest"] == {"S": envelope_digest(document)}
    assert (await finalization.request(client, runtime)).json() == response.json()
    assert outbox(runtime) == item


async def test_no_delivery_before_confirmed_removal(client, runtime, capability, sts, monkeypatch):
    finalization.supervisor(sts, monkeypatch)
    await save_reply(client, capability)
    assert (await finalization.request(client, runtime)).status_code == 404
    assert outbox(runtime) is None
    assert "chat_terminal" not in finalization.execution(runtime)


async def test_cancelled_saved_reply_does_not_leak_as_success(client, runtime, ready):
    await save_reply(client, ready, "Reply superseded by owner cancellation")
    results.cancel(runtime)
    response = await finalization.request(client, runtime)
    assert response.status_code == 200
    document = json.loads(outbox(runtime)["document"]["S"])
    assert document["terminal"]["outcome"] == "cancelled" and document["terminal"]["message_id"] is None
    assert "superseded" not in document["content"]


async def test_unresolved_model_accounting_is_preserved_without_error_details(client, runtime, ready):
    finalization.model_operation(runtime, provider_error="private provider failure")
    response = await finalization.request(client, runtime)
    assert response.status_code == 200
    document = json.loads(outbox(runtime)["document"]["S"])
    assert document["terminal"]["outcome"] == "interrupted"
    assert document["terminal"]["accounting_status"] == "unresolved"
    assert document["terminal"]["retryable"] is False
    assert "private provider" not in document["content"]
    assert "not replayed automatically" in document["content"]


async def test_delivery_uses_scrubbed_saved_content(client, runtime, ready):
    await save_reply(client, ready, "Saved reply with key: " + "AKIA" + "A" * 16)
    assert (await finalization.request(client, runtime)).status_code == 200
    document = json.loads(outbox(runtime)["document"]["S"])
    assert "AKIA" not in document["content"]
    saved = runtime[2].get_item(Key={"PK": "session#session-a", "SK": "msg#" + document["terminal"]["message_id"]})["Item"]
    assert document["content"] == saved["content"]


@pytest.mark.parametrize("field,value", [("ownerUserId", "other-user"), ("runId", "other-run"), ("leaseGeneration", 2)])
async def test_foreign_saved_reply_cannot_create_delivery(client, runtime, ready, field, value):
    message_id = await save_reply(client, ready)
    runtime[2].update_item(
        Key={"PK": "session#session-a", "SK": "msg#" + message_id},
        UpdateExpression="SET #field = :value",
        ExpressionAttributeNames={"#field": field},
        ExpressionAttributeValues={":value": value},
    )
    assert (await finalization.request(client, runtime)).status_code == 404
    assert outbox(runtime) is None and "chat_terminal" not in finalization.execution(runtime)


@pytest.mark.parametrize("field", ["tenant_id", "user_id", "session_id", "task_id", "thread_id", "owner_principal", "session_generation"])
async def test_replaced_registered_recipient_cannot_create_delivery(client, runtime, ready, field):
    execution = finalization.execution(runtime)
    delivery = json.loads(execution["chat_delivery"]["S"])
    delivery[field] = GENERATION + 1 if field == "session_generation" else "replacement"
    execution["chat_delivery"] = {"S": json.dumps(delivery)}
    write_protected(runtime, execution)
    assert (await finalization.request(client, runtime)).status_code == 503
    assert outbox(runtime) is None and "chat_terminal" not in finalization.execution(runtime)


async def test_removing_registered_routing_cannot_downgrade_to_legacy(client, runtime, ready):
    execution = finalization.execution(runtime)
    del execution["chat_delivery"]
    write_protected(runtime, execution)
    assert (await finalization.request(client, runtime)).status_code == 503
    assert outbox(runtime) is None


async def test_existing_outbox_refuses_partial_terminal_commit(client, runtime, ready):
    conflicting = {**KEY, "document": {"S": "foreign delivery"}, "status": {"S": "pending"}}
    write_protected(runtime, conflicting)
    assert (await finalization.request(client, runtime)).status_code == 409
    assert "terminal_result" not in results.turn(runtime)
    assert "chat_terminal" not in finalization.execution(runtime)
    assert outbox(runtime) == conflicting


async def test_storage_outage_before_commit_leaves_no_partial_delivery(client, runtime, ready, monkeypatch):
    protected = runtime[1].store
    transact = protected.client.transact_write_items

    def lose_request(**kwargs):
        if any(item.get("Put", {}).get("Item", {}).get("pk") == KEY["pk"] for item in kwargs["TransactItems"]):
            raise EndpointConnectionError(endpoint_url="https://storage.example.test")
        return transact(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", lose_request)
    assert (await finalization.request(client, runtime)).status_code == 503
    assert outbox(runtime) is None
    assert "terminal_result" not in results.turn(runtime)
    assert "chat_terminal" not in finalization.execution(runtime)
    monkeypatch.setattr(protected.client, "transact_write_items", transact)
    assert (await finalization.request(client, runtime)).status_code == 200
    assert outbox(runtime) is not None


async def test_lost_commit_response_recovers_exact_delivery_without_rebuilding_content(client, runtime, ready, monkeypatch):
    message_id = await save_reply(client, ready)
    protected = runtime[1].store
    transact = protected.client.transact_write_items

    def lose_response(**kwargs):
        response = transact(**kwargs)
        if any(item.get("Put", {}).get("Item", {}).get("pk") == KEY["pk"] for item in kwargs["TransactItems"]):
            raise EndpointConnectionError(endpoint_url="https://storage.example.test")
        return response

    monkeypatch.setattr(protected.client, "transact_write_items", lose_response)
    assert (await finalization.request(client, runtime)).status_code == 503
    saved = outbox(runtime)
    assert saved is not None
    monkeypatch.setattr(protected.client, "transact_write_items", transact)
    runtime[2].update_item(
        Key={"PK": "session#session-a", "SK": "msg#" + message_id},
        UpdateExpression="SET content = :content",
        ExpressionAttributeValues={":content": "Later content must not replace delivery"},
    )
    assert (await finalization.request(client, runtime)).status_code == 200
    assert outbox(runtime)["document"] == saved["document"]
    assert saved["status"] == {"S": "pending"} and outbox(runtime)["status"] == {"S": "queued"}


@pytest.mark.parametrize("change", ["missing", "malformed", "content", "delivery", "terminal", "digest"])
async def test_damaged_delivery_is_not_recreated_on_terminal_retry(client, runtime, ready, change):
    assert (await finalization.request(client, runtime)).status_code == 200
    item = outbox(runtime)
    assert item is not None
    protected = runtime[1].store
    if change == "missing":
        protected.client.delete_item(TableName=protected.table, Key=KEY)
    elif change == "digest":
        execution = finalization.execution(runtime)
        execution["chat_terminal_delivery_digest"] = {"S": "0" * 64}
        write_protected(runtime, execution)
    else:
        document = json.loads(item["document"]["S"])
        if change == "content":
            document["content"] = "replacement content"
        elif change == "delivery":
            document["delivery"]["user_id"] = "other-user"
        elif change == "terminal":
            document["terminal"]["outcome"] = "completed"
        item["document"] = {"S": "{" if change == "malformed" else json.dumps(document)}
        write_protected(runtime, item)
    before = outbox(runtime)
    assert (await finalization.request(client, runtime)).status_code == 503
    assert outbox(runtime) == before


async def test_reply_change_during_commit_cannot_queue_changed_content(client, runtime, ready, monkeypatch):
    message_id = await save_reply(client, ready)
    protected = runtime[1].store
    transact = protected.client.transact_write_items

    def change_reply(**kwargs):
        if any(item.get("Put", {}).get("Item", {}).get("pk") == KEY["pk"] for item in kwargs["TransactItems"]):
            runtime[2].update_item(
                Key={"PK": "session#session-a", "SK": "msg#" + message_id},
                UpdateExpression="SET content = :content",
                ExpressionAttributeValues={":content": "Changed during commit"},
            )
        return transact(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", change_reply)
    assert (await finalization.request(client, runtime)).status_code == 409
    assert outbox(runtime) is None
    assert "chat_terminal" not in finalization.execution(runtime)


@pytest.mark.parametrize("changes", [{"content": "forged reply"}, {"task_id": "other"}, {"delivery": ROUTING}, {"outcome": "completed"}])
async def test_supervisor_request_cannot_choose_delivery_fields(client, runtime, ready, changes):
    assert (await finalization.request(client, runtime, **changes)).status_code == 422
    assert outbox(runtime) is None
