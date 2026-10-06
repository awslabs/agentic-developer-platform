"""Owner-delivered cleanup releases only its lock and durably hands off successors."""

import hashlib
import json

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.chat_cancellation import CancelTurn, ChatCancellation
from tests.agentauth import test_chat_pending_handoff as handoff
from tests.agentauth import test_chat_pre_admission_terminal as outcomes
from tests.agentauth import test_chat_sandbox_creation as creation

client = outcomes.client
runtime = outcomes.runtime
store = outcomes.store
sts = outcomes.sts
retained_input_table = outcomes.retained_input_table
registered_owner = outcomes.registered_owner
transport = outcomes.transport
partial = outcomes.partial
consumer = outcomes.consumer
fixtures = outcomes.fixtures
cleanup = outcomes.cleanup
recovery = outcomes.recovery
ingest = handoff.ingest
queue = handoff.queue
buffer = handoff.buffer
unbound = creation.unbound


@pytest.fixture
async def cleaned(client, runtime, partial):
    assert (await recovery.resume(client, runtime)).status_code == 200
    partial[1]["absent"] = True
    assert (await cleanup.removal(client, runtime, partial)).json()["removed"] is True
    return {"run_id": "run-write", "envelope_digest": cleanup.envelope_digest(partial[2]), "pod_name": partial[0].name, "pod_uid": partial[0].uid}


@pytest.fixture
async def delivered(cleaned, transport, consumer):
    consumer._process_response(recovery.completion.publication.payload(transport))
    return cleaned


async def complete(client, body):
    return await client.post(
        "/internal/v1/agent/chat/data/complete", json=body, headers={"X-Adp-Producer-Proof": cleanup.proof(cleanup.envelope_digest(body))}
    )


async def test_completion_requires_owner_delivery_then_replays_without_releasing_a_successor_lock(client, runtime, cleaned, transport, consumer):
    assert (await complete(client, cleaned)).status_code == 409
    assert "completion_receipt" not in fixtures.outbox(runtime)
    assert handoff.thread(transport)["processing_task_id"] == "task-a"
    consumer._process_response(recovery.completion.publication.payload(transport))
    before = runtime[2].scan()["Items"]
    response = await complete(client, cleaned)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result == {
        "phase": "pre_admission",
        "run_id": "run-write",
        "session_id": "session-a",
        "attempt": 1,
        "credential_epoch": 1,
        "sandbox_uid": cleaned["pod_uid"],
        "terminal": outcomes.terminal(runtime),
        "delivery_id": json.loads(fixtures.outbox(runtime)["document"]["S"])["delivery_id"],
        "task_id": "task-a",
        "session_generation": fixtures.GENERATION,
        "processing_lock_released": True,
        "input_acknowledgement_ready": True,
        "completed_at": runtime[-1],
    }
    assert handoff.thread(transport)["processing_task_id"] == ""
    assert runtime[2].scan()["Items"] == before
    assert runtime[1].store._read("CHAT-LAUNCH#run-write", "LAUNCH") is None
    row = recovery.completion.publication.session(transport)
    row["threads"]["thread-a"]["processing_task_id"] = "later-task"
    transport.sessions.put_item(Item=row)
    assert (await complete(client, cleaned)).json() == result
    assert handoff.thread(transport)["processing_task_id"] == "later-task"


async def test_cleaned_completion_promotes_the_oldest_registered_successor_without_inventing_a_lease(
    client, runtime, delivered, transport, buffer, queue
):
    first = buffer()
    buffer("run-later")
    expected = next(iter(handoff.thread(transport)["pending_turns"].values()))["envelope_json"]
    response = await complete(client, delivered)
    assert response.status_code == 200, response.text
    assert response.json()["input_acknowledgement_ready"] is True
    assert "lease_generation" not in response.json()
    assert handoff.thread(transport)["processing_task_id"] == first["task_id"]
    assert len(handoff.thread(transport)["pending_turns"]) == 1
    assert queue.send_message.call_args.kwargs["MessageBody"] == expected
    assert queue.send_message.call_args.kwargs["MessageDeduplicationId"] == first["task_id"]
    assert runtime[1].store._read("TENANT#tenant", "EXEC#run-next")["status"] == {"S": "pending"}
    before = recovery.completion.publication.session(transport)
    assert (await complete(client, delivered)).json() == response.json()
    assert recovery.completion.publication.session(transport) == before
    assert queue.send_message.call_count == 1


async def test_successor_acquires_a_fresh_lease_and_predecessor_replay_preserves_it(client, runtime, delivered, transport, buffer, queue):
    buffer()
    response = await complete(client, delivered)
    assert response.status_code == 200, response.text
    envelope = json.loads(queue.send_message.call_args.kwargs["MessageBody"])
    run_hash = hashlib.sha256(envelope["message_id"].encode()).hexdigest()
    pod = cleanup.VerifiedPod(
        uid="fresh-chat-pod",
        name=f"chat-turn-{run_hash[:12]}-abcde",
        namespace="adp-test",
        service_account="adp-chat-sandbox",
        ip="10.0.0.43",
        image_digest=runtime[5].image_digest,
        run_hash=run_hash,
    )
    launch = cleanup.admit(
        runtime[1], run_id=envelope["message_id"], digest=cleanup.envelope_digest(envelope), pod=pod, team_id="team", now=runtime[-1]
    )
    assert launch.lease_generation == 1 and launch.sandbox_uid == pod.uid
    before = runtime[2].scan()["Items"]
    assert (await complete(client, delivered)).json() == response.json()
    assert runtime[2].scan()["Items"] == before
    assert handoff.thread(transport)["processing_task_id"] == "task-run-next"
    assert queue.send_message.call_count == 1


async def test_cancelled_cleaned_turn_completes_only_after_owner_delivery(client, runtime, partial, transport, consumer):
    service = ChatCancellation(runtime[1].store, runtime[2], transport.sessions)
    body = CancelTurn(session_id="session-a", task_id="task-a")
    service.cancel(body, service.resolve(body, "tenant", "human"))
    assert (await recovery.resume(client, runtime)).status_code == 200
    partial[1]["absent"] = True
    assert (await cleanup.removal(client, runtime, partial)).json()["removed"] is True
    request = {"run_id": "run-write", "envelope_digest": cleanup.envelope_digest(partial[2]), "pod_name": partial[0].name, "pod_uid": partial[0].uid}
    assert (await complete(client, request)).status_code == 409
    consumer._process_response(recovery.completion.publication.payload(transport))
    response = await complete(client, request)
    assert response.status_code == 200, response.text
    assert response.json()["terminal"]["outcome"] == "cancelled"
    assert response.json()["terminal"]["retryable"] is False
    assert response.json()["input_acknowledgement_ready"] is True
    assert handoff.thread(transport)["processing_task_id"] == ""
    assert (await complete(client, request)).json() == response.json()


async def test_reserved_original_pod_requires_positive_removal_then_completes_without_recreation(client, runtime, unbound, transport, consumer):
    assert (await creation.reserve(client, unbound)).status_code == 200
    saved = creation.creation(runtime)
    request = {"run_id": "run-write", "envelope_digest": cleanup.envelope_digest(unbound[2]), "pod_name": unbound[0].name, "pod_uid": unbound[0].uid}
    assert (await complete(client, request)).status_code == 404
    assert fixtures.outbox(runtime) is None
    assert handoff.thread(transport)["processing_task_id"] == "task-a"
    unbound[1]["exited"] = True
    assert (await recovery.resume(client, runtime)).status_code == 200
    assert (await complete(client, request)).status_code == 409
    unbound[1]["absent"] = True
    assert (await cleanup.removal(client, runtime, unbound)).json()["removed"] is True
    consumer._process_response(recovery.completion.publication.payload(transport))
    response = await complete(client, request)
    assert response.status_code == 200, response.text
    assert response.json()["input_acknowledgement_ready"] is True
    assert (await complete(client, request)).json() == response.json()
    assert creation.creation(runtime) == saved
    assert (await creation.reserve(client, unbound)).status_code == 409
    assert runtime[1].store._read("CHAT-LAUNCH#run-write", "LAUNCH") is None


@pytest.mark.parametrize("loss", ["transaction-before", "transaction-after", "queue", "queue-receipt", "handoff-write"])
async def test_completion_loss_resumes_exact_pending_handoff_without_premature_ack(
    client, runtime, delivered, transport, buffer, queue, monkeypatch, loss
):
    buffer()
    protected = runtime[1].store
    transact, update = protected.client.transact_write_items, protected.client.update_item

    def fail():
        raise EndpointConnectionError(endpoint_url="https://storage.example.test")

    def transaction(**kwargs):
        completing = any("completion_pending =" in item.get("Update", {}).get("UpdateExpression", "") for item in kwargs["TransactItems"])
        if completing and loss == "transaction-before":
            fail()
        result = transact(**kwargs)
        if completing and loss == "transaction-after":
            fail()
        return result

    def receipt(**kwargs):
        if loss == "handoff-write" and "input_queue_message_id" in kwargs.get("UpdateExpression", ""):
            fail()
        return update(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", transaction)
    monkeypatch.setattr(protected.client, "update_item", receipt)
    if loss == "queue":
        queue.send_message.side_effect = lambda **kwargs: fail()
    elif loss == "queue-receipt":
        queue.send_message.return_value = {}
    assert (await complete(client, delivered)).status_code == 503
    assert "completion_receipt" not in fixtures.outbox(runtime)
    assert handoff.thread(transport)["processing_task_id"] == ("task-a" if loss == "transaction-before" else "task-run-next")
    monkeypatch.setattr(protected.client, "transact_write_items", transact)
    monkeypatch.setattr(protected.client, "update_item", update)
    queue.send_message.side_effect = None
    queue.send_message.return_value = {"MessageId": "accepted"}
    response = await complete(client, delivered)
    assert response.status_code == 200, response.text
    assert response.json()["input_acknowledgement_ready"] is True
    assert handoff.thread(transport)["processing_task_id"] == "task-run-next"
    calls = queue.send_message.call_args_list
    assert calls and all(call.kwargs == calls[0].kwargs for call in calls)


@pytest.mark.parametrize("race", ["execution", "dispatch", "pod", "teardown", "launch", "creation", "owner", "task", "delivery"])
async def test_completion_transaction_rejects_changed_scope_evidence_and_owner_lock(client, runtime, delivered, transport, monkeypatch, race):
    protected = runtime[1].store
    transact = protected.client.transact_write_items
    raced = []

    def transaction(**kwargs):
        completing = any("completion_receipt =" in item.get("Update", {}).get("UpdateExpression", "") for item in kwargs["TransactItems"])
        if completing and not raced:
            raced.append(True)
            if race in {"owner", "task", "delivery"}:
                row = recovery.completion.publication.session(transport)
                if race == "owner":
                    row["owner_principal"] = "foreign-owner"
                elif race == "task":
                    row["threads"]["thread-a"]["processing_task_id"] = "next-task"
                else:
                    row["threads"]["thread-a"]["terminal_delivery"]["digest"] = "f" * 64
                transport.sessions.put_item(Item=row)
            else:
                if race == "execution":
                    item = cleanup.queued.execution(runtime)
                    item["current_attempt"] = {"N": "2"}
                elif race == "dispatch":
                    item = protected._read("INVOCATION#run-write", "DISPATCH")
                    item["envelope_digest"] = {"S": "f" * 64}
                elif race == "pod":
                    item = protected._read(f"POD#{delivered['pod_uid']}", "BINDING")
                    item["invocation_id"] = {"S": "other-run"}
                elif race == "teardown":
                    item = cleanup.receipt(runtime)
                    item["removed_at"] = {"N": "1"}
                else:
                    item = {"pk": {"S": "CHAT-LAUNCH#run-write"}, "sk": {"S": race.upper()}}
                fixtures.write_protected(runtime, item)
        return transact(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", transaction)
    assert (await complete(client, delivered)).status_code == 409
    assert raced and "completion_receipt" not in fixtures.outbox(runtime)
    assert handoff.thread(transport)["processing_task_id"] == ("next-task" if race == "task" else "task-a")


@pytest.mark.parametrize("field,value", [("pod_uid", "foreign-pod"), ("pod_name", "foreign-pod"), ("envelope_digest", "f" * 64)])
async def test_substituted_cleanup_cannot_complete(client, runtime, delivered, transport, field, value):
    assert (await complete(client, {**delivered, field: value})).status_code == 404
    assert "completion_receipt" not in fixtures.outbox(runtime)
    assert handoff.thread(transport)["processing_task_id"] == "task-a"
