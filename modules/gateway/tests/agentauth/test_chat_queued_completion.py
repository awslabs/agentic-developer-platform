"""Unreserved cancellations complete only after owner delivery and creation fencing."""

import hashlib
import json

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth import chat_data_routes
from src.agentauth.chat_admission import admit
from src.agentauth.workload import VerifiedPod
from tests.agentauth import test_chat_pending_handoff as handoff
from tests.agentauth import test_chat_queued_terminal as outcomes

client = outcomes.client
runtime = outcomes.runtime
store = outcomes.store
sts = outcomes.sts
retained_input_table = outcomes.retained_input_table
registered_owner = outcomes.registered_owner
transport = outcomes.transport
consumer = outcomes.consumer
queued = outcomes.queued
fixtures = outcomes.fixtures
recovery = outcomes.recovery
ingest = handoff.ingest
buffer = handoff.buffer
queue = handoff.queue


@pytest.fixture
async def delivered(client, runtime, queued, transport, consumer):
    assert (await recovery.resume(client, runtime)).status_code == 409
    consumer._process_response(recovery.completion.publication.payload(transport))
    return outcomes.terminal(runtime)


async def test_unreserved_completion_requires_owner_delivery_and_replays_without_releasing_later_lock(client, runtime, queued, transport, consumer):
    before = runtime[2].scan()["Items"]
    assert (await recovery.resume(client, runtime)).status_code == 409
    assert "completion_receipt" not in fixtures.outbox(runtime)
    assert handoff.thread(transport)["processing_task_id"] == "task-a"
    terminal = outcomes.terminal(runtime)
    consumer._process_response(recovery.completion.publication.payload(transport))
    response = await recovery.resume(client, runtime)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result == {
        "run_id": "run-write",
        "session_id": "session-a",
        "task_id": "task-a",
        "session_generation": fixtures.GENERATION,
        "state": "queued_completed",
        "completion": {
            "phase": "queued",
            "run_id": "run-write",
            "session_id": "session-a",
            "attempt": 1,
            "credential_epoch": 1,
            "terminal": terminal,
            "creation_fenced": True,
            "delivery_id": json.loads(fixtures.outbox(runtime)["document"]["S"])["delivery_id"],
            "task_id": "task-a",
            "session_generation": fixtures.GENERATION,
            "processing_lock_released": True,
            "input_acknowledgement_ready": True,
            "completed_at": runtime[-1],
        },
    }
    assert handoff.thread(transport)["processing_task_id"] == ""
    assert runtime[2].scan()["Items"] == before
    for sort_key in ("CREATION", "LAUNCH", "TEARDOWN", "PRE-ADMISSION-TEARDOWN"):
        assert runtime[1].store._read("CHAT-LAUNCH#run-write", sort_key) is None
    row = recovery.completion.publication.session(transport)
    row["threads"]["thread-a"]["processing_task_id"] = "later-task"
    transport.sessions.put_item(Item=row)
    assert (await recovery.resume(client, runtime)).json() == result
    assert handoff.thread(transport)["processing_task_id"] == "later-task"
    assert outcomes.terminal(runtime) == terminal
    assert transport.client.send_message.call_count == 1


async def test_queued_completion_promotes_exact_successor_and_preserves_its_fresh_lease(client, runtime, delivered, transport, buffer, queue):
    first = buffer()
    buffer("run-later")
    expected = next(iter(handoff.thread(transport)["pending_turns"].values()))["envelope_json"]
    response = await recovery.resume(client, runtime)
    assert response.status_code == 200, response.text
    assert response.json()["completion"]["input_acknowledgement_ready"] is True
    assert handoff.thread(transport)["processing_task_id"] == first["task_id"]
    assert len(handoff.thread(transport)["pending_turns"]) == 1
    assert queue.send_message.call_args.kwargs["MessageBody"] == expected
    envelope = json.loads(expected)
    run_hash = hashlib.sha256(envelope["message_id"].encode()).hexdigest()
    pod = VerifiedPod(
        uid="fresh-chat-pod",
        name=f"chat-turn-{run_hash[:12]}-abcde",
        namespace="adp-test",
        service_account="adp-chat-sandbox",
        ip="10.0.0.43",
        image_digest=runtime[5].image_digest,
        run_hash=run_hash,
    )
    launch = admit(runtime[1], run_id=envelope["message_id"], digest=outcomes.envelope_digest(envelope), pod=pod, team_id="team", now=runtime[-1])
    assert launch.lease_generation == 1 and launch.sandbox_uid == pod.uid
    before = runtime[2].scan()["Items"]
    assert (await recovery.resume(client, runtime)).json() == response.json()
    assert runtime[2].scan()["Items"] == before
    assert handoff.thread(transport)["processing_task_id"] == first["task_id"]
    assert queue.send_message.call_count == 1


@pytest.mark.parametrize("loss", ["transaction-before", "transaction-after", "queue", "queue-receipt", "handoff-write"])
async def test_lost_completion_and_handoff_replies_never_authorize_early_ack(client, runtime, delivered, transport, buffer, queue, monkeypatch, loss):
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
    assert (await recovery.resume(client, runtime)).status_code == 503
    assert "completion_receipt" not in fixtures.outbox(runtime)
    assert handoff.thread(transport)["processing_task_id"] == ("task-a" if loss == "transaction-before" else "task-run-next")
    monkeypatch.setattr(protected.client, "transact_write_items", transact)
    monkeypatch.setattr(protected.client, "update_item", update)
    queue.send_message.side_effect = None
    queue.send_message.return_value = {"MessageId": "accepted"}
    response = await recovery.resume(client, runtime)
    assert response.status_code == 200, response.text
    assert response.json()["completion"]["input_acknowledgement_ready"] is True
    assert handoff.thread(transport)["processing_task_id"] == "task-run-next"
    calls = queue.send_message.call_args_list
    assert calls and all(call.kwargs == calls[0].kwargs for call in calls)


@pytest.mark.parametrize(
    "race", ["binding", "pod-name", "creation-marker", "cleanup", "dispatch", "creation", "launch", "exit", "removal", "owner", "task", "delivery"]
)
async def test_completion_fences_concurrent_creation_scope_and_owner_changes(client, runtime, delivered, transport, monkeypatch, race):
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
                if race in {"binding", "pod-name", "creation-marker", "cleanup"}:
                    item = outcomes.execution(runtime)
                    field = {
                        "binding": "workload_binding",
                        "pod-name": "pod_name",
                        "creation-marker": "chat_sandbox_creation",
                        "cleanup": "chat_pre_admission_cleanup",
                    }[race]
                    item[field] = {"S": "late-creation"}
                elif race == "dispatch":
                    item = protected._read("INVOCATION#run-write", "DISPATCH")
                    item["envelope_digest"] = {"S": "f" * 64}
                else:
                    sort_key = {"creation": "CREATION", "launch": "LAUNCH", "exit": "PRE-ADMISSION-TEARDOWN", "removal": "TEARDOWN"}[race]
                    item = {"pk": {"S": "CHAT-LAUNCH#run-write"}, "sk": {"S": sort_key}}
                fixtures.write_protected(runtime, item)
        return transact(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", transaction)
    assert (await recovery.resume(client, runtime)).status_code == 409
    assert raced and "completion_receipt" not in fixtures.outbox(runtime)
    assert handoff.thread(transport)["processing_task_id"] == ("next-task" if race == "task" else "task-a")


async def test_late_reservation_and_binding_remain_denied_after_queued_completion(client, runtime, delivered, queued):
    assert (await recovery.resume(client, runtime)).status_code == 200
    body = {"run_id": "run-write", "envelope_digest": outcomes.envelope_digest(queued[2]), "image_digest": runtime[5].image_digest}
    response = await client.post(
        "/internal/v1/agent/chat/data/reserve", json=body, headers={"X-Adp-Producer-Proof": recovery.proof(outcomes.envelope_digest(body))}
    )
    assert response.status_code == 409
    pod = VerifiedPod(
        "late-pod",
        "chat-a",
        "adp-gateway-agents",
        "adp-chat-sandbox",
        "127.0.0.1",
        image_digest=runtime[5].image_digest,
        run_hash=hashlib.sha256(b"run-write").hexdigest(),
    )
    with pytest.raises(outcomes.BootstrapRefusedError):
        runtime[1].store.bind(
            invocation_id="run-write",
            digest=outcomes.envelope_digest(queued[2]),
            pod=pod,
            now=outcomes.datetime.fromtimestamp(runtime[-1], outcomes.UTC),
        )


async def test_completion_cannot_predate_its_terminal_outcome(client, runtime, delivered, transport, monkeypatch):
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] - 1)
    assert (await recovery.resume(client, runtime)).status_code == 404
    assert "completion_receipt" not in fixtures.outbox(runtime)
    assert handoff.thread(transport)["processing_task_id"] == "task-a"


@pytest.mark.parametrize("field,value", [("creation_fenced", {"BOOL": False}), ("attempt", {"N": "2"}), ("sandbox_uid", {"S": "foreign-pod"})])
async def test_changed_completion_receipt_cannot_authorize_acknowledgement(client, runtime, delivered, field, value):
    assert (await recovery.resume(client, runtime)).status_code == 200
    item = fixtures.outbox(runtime)
    item["completion_receipt"]["M"][field] = value
    fixtures.write_protected(runtime, item)
    assert (await recovery.resume(client, runtime)).status_code == 503
