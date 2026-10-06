"""Cleaned unadmitted turns reach their owner without claiming queue completion."""

import json

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.chat_cancellation import CancelTurn, ChatCancellation
from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from tests.agentauth import test_chat_pre_admission_cleanup as cleanup

client = cleanup.client
runtime = cleanup.runtime
store = cleanup.store
sts = cleanup.sts
retained_input_table = cleanup.retained_input_table
registered_owner = cleanup.registered_owner
transport = cleanup.transport
partial = cleanup.partial
consumer = cleanup.queued.consumer
fixtures = cleanup.fixtures
recovery = cleanup.recovery


def terminal(runtime):
    return json.loads(cleanup.queued.execution(runtime)["chat_pre_admission_terminal"]["S"])


async def test_prior_committed_removal_recovers_owner_outcome_without_new_pod_observation(client, runtime, partial, transport):
    assert (await recovery.resume(client, runtime)).status_code == 200
    saved = cleanup.receipt(runtime)
    saved["removed_at"] = {"N": str(runtime[-1])}
    fixtures.write_protected(runtime, saved)
    partial[1]["observations"].clear()
    assert fixtures.outbox(runtime) is None
    assert (await recovery.resume(client, runtime)).json()["removed"] is True
    assert terminal(runtime)["outcome"] == "interrupted"
    assert cleanup.receipt(runtime) == saved
    assert not partial[1]["observations"]
    assert transport.client.send_message.call_count == 1


@pytest.mark.parametrize("cancelled", [False, True])
async def test_cleaned_unadmitted_turn_has_atomic_owner_outcome_without_completion(client, runtime, partial, transport, consumer, cancelled):
    if cancelled:
        service = ChatCancellation(runtime[1].store, runtime[2], transport.sessions)
        body = CancelTurn(session_id="session-a", task_id="task-a")
        service.cancel(body, service.resolve(body, "tenant", "human"))
    before = runtime[2].scan()["Items"]
    assert (await recovery.resume(client, runtime)).status_code == 200
    assert (await cleanup.removal(client, runtime, partial)).json()["removed"] is False
    assert fixtures.outbox(runtime) is None
    partial[1]["absent"] = True
    assert (await cleanup.removal(client, runtime, partial)).json()["removed"] is True
    result = terminal(runtime)
    assert result == {
        "phase": "pre_admission",
        "run_id": "run-write",
        "session_id": "session-a",
        "attempt": 1,
        "credential_epoch": 1,
        "sandbox_uid": partial[0].uid,
        "outcome": "cancelled" if cancelled else "interrupted",
        "message_id": None,
        "terminal": True,
        "retryable": not cancelled,
        "automatic_replay_permitted": False,
        "accounting_status": "not_used",
        "cleanup_required": False,
        "finalized_at": runtime[-1],
    }
    execution = cleanup.queued.execution(runtime)
    assert execution["status"] == {"S": "cancelled" if cancelled else "completed"}
    assert "chat_terminal" not in execution and "chat_queued_terminal" not in execution
    assert runtime[1].store._read("CHAT-LAUNCH#run-write", "LAUNCH") is None
    assert runtime[2].scan()["Items"] == before
    payload = recovery.completion.publication.payload(transport)
    assert payload["status"] == result["outcome"] and payload["retryable"] == result["retryable"]
    consumer._process_response(payload)
    consumer._process_response(payload)
    assert consumer.ws_router._client.post_to_connection.call_count == 1
    assert consumer.ws_router._client.post_to_connection.call_args.kwargs["ConnectionId"] == "owner-connection"
    assert recovery.completion.publication.session(transport)["threads"]["thread-a"]["processing_task_id"] == "task-a"
    assert (await recovery.resume(client, runtime)).json()["removed"] is True
    assert (await cleanup.removal(client, runtime, partial)).json()["removed"] is True
    assert terminal(runtime) == result and transport.client.send_message.call_count == 1
    assert "completion_receipt" not in fixtures.outbox(runtime)
    assert recovery.completion.publication.session(transport)["threads"]["thread-a"]["processing_task_id"] == "task-a"
    consumer.sqs.send_message.assert_not_called()
    if cancelled:
        service.cancel(body, service.resolve(body, "tenant", "human"))
        assert cleanup.queued.execution(runtime) == execution
        assert terminal(runtime) == result
        with pytest.raises(ChatAuthorizationRefusedError):
            service.resolve(body, "tenant", "other-owner")


@pytest.mark.parametrize("loss", ["transaction-before", "transaction-after", "queue", "queue-receipt", "outbox-receipt"])
async def test_loss_retries_keep_one_immutable_outcome_and_owner_delivery(client, runtime, partial, transport, monkeypatch, loss):
    assert (await recovery.resume(client, runtime)).status_code == 200
    partial[1]["absent"] = True
    protected = runtime[1].store
    transact = protected.client.transact_write_items
    update = protected.client.update_item

    def fail():
        raise EndpointConnectionError(endpoint_url="https://service.example.test")

    def lost_transaction(**kwargs):
        if loss == "transaction-before":
            fail()
        response = transact(**kwargs)
        if loss == "transaction-after":
            fail()
        return response

    def lost_outbox(**kwargs):
        if loss == "outbox-receipt" and kwargs.get("Key", {}).get("sk") == {"S": "TERMINAL"}:
            fail()
        return update(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", lost_transaction)
    monkeypatch.setattr(protected.client, "update_item", lost_outbox)
    if loss == "queue":
        transport.client.send_message.side_effect = lambda **kwargs: fail()
    elif loss == "queue-receipt":
        transport.client.send_message.return_value = {}
    assert (await cleanup.removal(client, runtime, partial)).status_code == 503
    previous = cleanup.queued.execution(runtime).get("chat_pre_admission_terminal")
    if loss == "transaction-before":
        assert previous is None and fixtures.outbox(runtime) is None
        assert "removed_at" not in cleanup.receipt(runtime)
    else:
        assert previous is not None and fixtures.outbox(runtime) is not None
        assert "removed_at" in cleanup.receipt(runtime)
    monkeypatch.setattr(protected.client, "transact_write_items", transact)
    monkeypatch.setattr(protected.client, "update_item", update)
    transport.client.send_message.side_effect = None
    transport.client.send_message.return_value = {"MessageId": "accepted"}
    partial[1]["observations"].clear()
    retry = await cleanup.removal(client, runtime, partial) if previous is None else await recovery.resume(client, runtime)
    assert retry.status_code == 200 and retry.json()["removed"] is True
    if previous is not None:
        assert cleanup.queued.execution(runtime)["chat_pre_admission_terminal"] == previous
        assert not partial[1]["observations"]
    calls = transport.client.send_message.call_args_list
    assert calls and all(call.kwargs == calls[0].kwargs for call in calls)
    assert "completion_receipt" not in fixtures.outbox(runtime)
    assert recovery.completion.publication.session(transport)["threads"]["thread-a"]["processing_task_id"] == "task-a"


@pytest.mark.parametrize("race", ["attempt", "epoch", "binding", "dispatch", "pod", "launch", "removal", "outbox", "cancel"])
async def test_terminal_transaction_fences_concurrent_scope_and_admission_changes(client, runtime, partial, transport, monkeypatch, race):
    assert (await recovery.resume(client, runtime)).status_code == 200
    partial[1]["absent"] = True
    protected = runtime[1].store
    transact = protected.client.transact_write_items
    raced = []

    def concurrent(**kwargs):
        if not raced:
            raced.append(True)
            if race == "cancel":
                service = ChatCancellation(protected, runtime[2], transport.sessions)
                body = CancelTurn(session_id="session-a", task_id="task-a")
                service.cancel(body, service.resolve(body, "tenant", "human"))
            else:
                if race in {"attempt", "epoch", "binding"}:
                    item = cleanup.queued.execution(runtime)
                    field, value = {
                        "attempt": ("current_attempt", {"N": "2"}),
                        "epoch": ("current_credential_epoch", {"N": "2"}),
                        "binding": ("workload_binding", {"S": "foreign-pod-uid"}),
                    }[race]
                    item[field] = value
                elif race == "dispatch":
                    item = protected._read("INVOCATION#run-write", "DISPATCH")
                    item["envelope_digest"] = {"S": "f" * 64}
                elif race == "pod":
                    item = protected._read(f"POD#{partial[0].uid}", "BINDING")
                    item["invocation_id"] = {"S": "foreign-run"}
                elif race == "removal":
                    item = cleanup.receipt(runtime)
                    item["removed_at"] = {"N": str(runtime[-1])}
                else:
                    item = {
                        "pk": {"S": f"CHAT-{'LAUNCH' if race == 'launch' else 'DELIVERY'}#run-write"},
                        "sk": {"S": "LAUNCH" if race == "launch" else "TERMINAL"},
                    }
                fixtures.write_protected(runtime, item)
        return transact(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", concurrent)
    assert (await cleanup.removal(client, runtime, partial)).status_code == 409
    assert raced and "chat_pre_admission_terminal" not in cleanup.queued.execution(runtime)
    if race != "removal":
        assert "removed_at" not in cleanup.receipt(runtime)
    if race != "outbox":
        assert fixtures.outbox(runtime) is None
    transport.client.send_message.assert_not_called()
    if race == "cancel":
        assert (await cleanup.removal(client, runtime, partial)).status_code == 200
        assert terminal(runtime)["outcome"] == "cancelled"


@pytest.mark.parametrize("change", ["outcome", "retryable", "cleanup_required", "sandbox_uid", "epoch", "outbox", "removal"])
async def test_changed_terminal_or_evidence_cannot_be_republished(client, runtime, partial, transport, change):
    assert (await recovery.resume(client, runtime)).status_code == 200
    partial[1]["absent"] = True
    assert (await cleanup.removal(client, runtime, partial)).status_code == 200
    if change == "outbox":
        item = fixtures.outbox(runtime)
        document = json.loads(item["document"]["S"])
        document["delivery"]["owner_principal"] = "other-owner"
        item["document"] = {"S": json.dumps(document)}
    elif change == "removal":
        item = cleanup.receipt(runtime)
        del item["removed_at"]
    else:
        item = cleanup.queued.execution(runtime)
        result = terminal(runtime)
        field, value = {
            "outcome": ("outcome", "completed"),
            "retryable": ("retryable", False),
            "cleanup_required": ("cleanup_required", True),
            "sandbox_uid": ("sandbox_uid", "foreign-pod"),
            "epoch": ("credential_epoch", 2),
        }[change]
        result[field] = value
        item["chat_pre_admission_terminal"] = {"S": json.dumps(result)}
    fixtures.write_protected(runtime, item)
    response = await recovery.resume(client, runtime)
    assert response.status_code in {404, 503}
    assert transport.client.send_message.call_count == 1


@pytest.mark.parametrize("field,value", [("owner_principal", "foreign-owner"), ("created_at", 1), ("expires_at", 1), ("threads", {})])
async def test_terminal_never_publishes_to_replaced_or_expired_owner_session(client, runtime, partial, transport, field, value):
    assert (await recovery.resume(client, runtime)).status_code == 200
    partial[1]["absent"] = True
    transport.sessions.put_item(Item={**transport.row, field: value})
    assert (await cleanup.removal(client, runtime, partial)).status_code == 404
    assert "removed_at" in cleanup.receipt(runtime)
    assert terminal(runtime)["outcome"] == "interrupted"
    assert fixtures.outbox(runtime)["status"] == {"S": "pending"}
    transport.client.send_message.assert_not_called()
