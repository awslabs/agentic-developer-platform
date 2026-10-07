"""Queued cancellation reaches its owner without inventing teardown evidence."""

import hashlib
import json
from datetime import UTC, datetime

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.bootstrap import BootstrapRefusedError, envelope_digest
from src.agentauth.chat_cancellation import CancelTurn, ChatCancellation
from src.agentauth.workload import VerifiedPod
from tests.agentauth import test_chat_supervisor_resume as recovery

fixtures = recovery.fixtures
client = recovery.client
runtime = recovery.runtime
store = recovery.store
sts = recovery.sts
retained_input_table = recovery.retained_input_table
registered_owner = recovery.registered_owner
transport = recovery.transport
consumer = recovery.consumer


@pytest.fixture
def queued(runtime, transport, retained_input_table, registered_owner, sts, monkeypatch):
    recovery.supervisor(sts, monkeypatch)
    envelope = fixtures.history.prepare(runtime, message_id="run-write")
    service = ChatCancellation(runtime[1].store, runtime[2], transport.sessions)
    body = CancelTurn(session_id="session-a", task_id="task-a")
    service.cancel(body, service.resolve(body, "tenant", "human"))
    return service, body, envelope


def execution(runtime):
    return fixtures.finalization.execution(runtime)


def terminal(runtime):
    return json.loads(execution(runtime)["chat_queued_terminal"]["S"])


async def test_queued_cancel_is_atomic_visible_and_retryable_without_claiming_cleanup(client, runtime, queued, transport, consumer):
    before = runtime[2].scan()["Items"]
    assert (await recovery.resume(client, runtime)).status_code == 409
    result = terminal(runtime)
    assert result == {
        "phase": "queued",
        "run_id": "run-write",
        "session_id": "session-a",
        "attempt": 1,
        "credential_epoch": 1,
        "outcome": "cancelled",
        "message_id": None,
        "terminal": True,
        "retryable": False,
        "automatic_replay_permitted": False,
        "accounting_status": "not_used",
        "cleanup_required": True,
        "finalized_at": runtime[-1],
    }
    assert execution(runtime)["status"] == {"S": "cancelled"}
    assert "chat_terminal" not in execution(runtime)
    assert runtime[1].store._read("CHAT-LAUNCH#run-write", "LAUNCH") is None
    assert runtime[1].store._read("CHAT-LAUNCH#run-write", "TEARDOWN") is None
    assert [item for item in runtime[2].scan()["Items"] if not item["SK"].startswith("output")] == before
    assert fixtures.replay(runtime)["events"][0]["payload"]["status"] == "cancelled"
    payload = recovery.completion.publication.payload(transport)
    assert payload["status"] == "cancelled" and payload["text"] == "This turn was cancelled."
    consumer._process_response(payload)
    consumer._process_response(payload)
    assert consumer.ws_router._client.post_to_connection.call_count == 1
    assert consumer.ws_router._client.post_to_connection.call_args.kwargs["ConnectionId"] == "owner-connection"
    row = recovery.completion.publication.session(transport)
    assert row["threads"]["thread-a"]["processing_task_id"] == "task-a"
    assert row["threads"]["thread-a"]["terminal_delivery"]["status"] == "sent"
    service, body, envelope = queued
    service.cancel(body, service.resolve(body, "tenant", "human"))
    completed = await recovery.resume(client, runtime)
    assert completed.status_code == 200, completed.text
    assert completed.json()["completion"]["creation_fenced"] is True
    assert terminal(runtime) == result
    assert transport.client.send_message.call_count == 1
    assert "completion_receipt" in fixtures.outbox(runtime)
    assert (await recovery.completion.complete(client, runtime)).status_code == 404
    consumer.sqs.send_message.assert_not_called()
    assert terminal(runtime) == result
    pod = VerifiedPod(
        "chat-pod",
        "chat-a",
        "adp-gateway-agents",
        "adp-chat-sandbox",
        "127.0.0.1",
        image_digest=runtime[5].image_digest,
        run_hash=hashlib.sha256(b"run-write").hexdigest(),
    )
    with pytest.raises(BootstrapRefusedError):
        runtime[1].store.bind(invocation_id="run-write", digest=envelope_digest(envelope), pod=pod, now=datetime.fromtimestamp(runtime[-1], UTC))


@pytest.mark.parametrize("loss", ["transaction-before", "transaction-after", "queue", "queue-receipt", "receipt-before", "receipt-after"])
async def test_queued_cancellation_loss_retries_same_terminal_delivery(client, runtime, queued, transport, monkeypatch, loss):
    protected = runtime[1].store
    transact, update = protected.client.transact_write_items, protected.client.update_item

    def unavailable():
        raise EndpointConnectionError(endpoint_url="https://storage.example.test")

    def lost_transaction(**kwargs):
        if any("chat_queued_terminal =" in item.get("Update", {}).get("UpdateExpression", "") for item in kwargs["TransactItems"]):
            if loss == "transaction-after":
                transact(**kwargs)
            unavailable()
        return transact(**kwargs)

    def lost_receipt(**kwargs):
        if kwargs.get("Key") == fixtures.KEY:
            if loss == "receipt-after":
                update(**kwargs)
            unavailable()
        return update(**kwargs)

    if loss.startswith("transaction"):
        monkeypatch.setattr(protected.client, "transact_write_items", lost_transaction)
    elif loss.startswith("receipt"):
        monkeypatch.setattr(protected.client, "update_item", lost_receipt)
    elif loss == "queue":
        transport.client.send_message.side_effect = EndpointConnectionError(endpoint_url="https://queue.example.test")
    else:
        transport.client.send_message.return_value = {}
    assert (await recovery.resume(client, runtime)).status_code == 503
    if loss == "transaction-before":
        assert "chat_queued_terminal" not in execution(runtime)
        assert fixtures.outbox(runtime) is None
        transport.client.send_message.assert_not_called()
    else:
        assert terminal(runtime)["cleanup_required"] is True
        assert fixtures.outbox(runtime) is not None
    previous = execution(runtime).get("chat_queued_terminal")
    monkeypatch.setattr(protected.client, "transact_write_items", transact)
    monkeypatch.setattr(protected.client, "update_item", update)
    transport.client.send_message.side_effect = None
    transport.client.send_message.return_value = {"MessageId": "accepted"}
    assert (await recovery.resume(client, runtime)).status_code == 409
    if previous is not None:
        assert execution(runtime)["chat_queued_terminal"] == previous
    calls = transport.client.send_message.call_args_list
    assert all(call.kwargs == calls[0].kwargs for call in calls)
    assert recovery.completion.publication.session(transport)["threads"]["thread-a"]["processing_task_id"] == "task-a"
    assert "completion_receipt" not in fixtures.outbox(runtime)


@pytest.mark.parametrize(
    "field,value",
    [
        ("abort_body_digest", {"S": "f" * 64}),
        ("abort_requested_attempt", {"N": "2"}),
        ("abort_requested_at", {"S": "not-a-date"}),
        ("pod_name", {"S": "ambiguous-pod"}),
        ("workload_binding", {"S": "partially-bound-pod"}),
    ],
)
async def test_incomplete_or_partially_bound_cancellation_is_not_finalized(client, runtime, queued, transport, field, value):
    item = execution(runtime)
    item[field] = value
    fixtures.write_protected(runtime, item)
    assert (await recovery.resume(client, runtime)).status_code == 409
    assert execution(runtime) == item
    assert fixtures.outbox(runtime) is None
    transport.client.send_message.assert_not_called()


@pytest.mark.parametrize("race", ["binding", "pod-name", "launch", "attempt"])
async def test_cancellation_finalization_transaction_fences_admission_races(client, runtime, queued, transport, monkeypatch, race):
    protected = runtime[1].store
    transact = protected.client.transact_write_items
    raced = []

    def concurrent(**kwargs):
        if not raced:
            raced.append(True)
            if race == "launch":
                protected.client.put_item(TableName=protected.table, Item={"pk": {"S": "CHAT-LAUNCH#run-write"}, "sk": {"S": "LAUNCH"}})
            else:
                item = execution(runtime)
                field, value = {
                    "binding": ("workload_binding", {"S": "late-pod"}),
                    "pod-name": ("pod_name", {"S": "late-pod"}),
                    "attempt": ("current_attempt", {"N": "2"}),
                }[race]
                item[field] = value
                fixtures.write_protected(runtime, item)
        return transact(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", concurrent)
    assert (await recovery.resume(client, runtime)).status_code == 409
    assert raced and "chat_queued_terminal" not in execution(runtime)
    assert fixtures.outbox(runtime) is None
    transport.client.send_message.assert_not_called()


@pytest.mark.parametrize("field,value", [("owner_principal", "other-owner"), ("created_at", 1), ("expires_at", 1), ("threads", {})])
async def test_queued_outcome_never_routes_to_replaced_session(client, runtime, queued, transport, field, value):
    transport.sessions.put_item(Item={**transport.row, field: value})
    assert (await recovery.resume(client, runtime)).status_code == 404
    assert terminal(runtime)["outcome"] == "cancelled"
    transport.client.send_message.assert_not_called()


async def test_replaced_execution_dispatch_cannot_finalize_cancellation(client, runtime, queued, transport):
    item = execution(runtime)
    item["envelope_digest"] = {"S": "f" * 64}
    fixtures.write_protected(runtime, item)
    assert (await recovery.resume(client, runtime)).status_code == 404
    assert fixtures.outbox(runtime) is None
    assert execution(runtime) == item
    transport.client.send_message.assert_not_called()


@pytest.mark.parametrize("field,value", [("attempt", 2), ("credential_epoch", 2), ("cleanup_required", False), ("outcome", "completed")])
async def test_changed_queued_terminal_is_not_republished(client, runtime, queued, transport, field, value):
    assert (await recovery.resume(client, runtime)).status_code == 409
    item = execution(runtime)
    changed = terminal(runtime)
    changed[field] = value
    item["chat_queued_terminal"] = {"S": json.dumps(changed)}
    fixtures.write_protected(runtime, item)
    assert (await recovery.resume(client, runtime)).status_code == 503
    assert transport.client.send_message.call_count == 1
