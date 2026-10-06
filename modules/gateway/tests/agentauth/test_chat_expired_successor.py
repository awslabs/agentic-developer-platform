"""Registered expired follow-ups deliver an outcome before advancing the owner queue."""

import hashlib
import json
from datetime import UTC, datetime

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth import chat_data_routes
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.chat_admission import admit
from src.agentauth.chat_cancellation import CancelTurn, ChatCancellation
from src.agentauth.workload import VerifiedPod
from tests.agentauth import test_chat_pending_handoff as handoff
from tests.agentauth import test_chat_supervisor_resume as recovery

client = handoff.client
runtime = handoff.runtime
store = handoff.store
sts = handoff.sts
capability = handoff.capability
retained_input_table = handoff.retained_input_table
ready = handoff.ready
registered_owner = handoff.registered_owner
transport = handoff.transport
consumer = handoff.consumer
delivered = handoff.delivered
ingest = handoff.ingest
buffer = handoff.buffer
queue = handoff.queue
completion = handoff.completion
fixtures = completion.fixtures


def execution(runtime, run_id="run-expired"):
    return runtime[1].store._read("TENANT#tenant", f"EXEC#{run_id}")


def outbox(runtime, run_id="run-expired"):
    return runtime[1].store._read(f"CHAT-DELIVERY#{run_id}", "TERMINAL")


async def resume(client, runtime, envelope):
    return await recovery.resume(client, runtime, run_id=envelope["message_id"], envelope_digest=handoff.envelope_digest(envelope))


@pytest.fixture
async def promoted(client, runtime, transport, delivered, buffer, queue, sts, monkeypatch):
    recovery.supervisor(sts, monkeypatch)
    monkeypatch.setenv("SESSION_TTL_SECONDS", "60")
    buffer("run-expired")
    monkeypatch.setenv("SESSION_TTL_SECONDS", "3600")
    buffer("run-healthy")
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] + 60)
    response = await completion.complete(client, runtime)
    assert response.status_code == 200, response.text
    assert handoff.thread(transport)["processing_task_id"] == "task-run-expired"
    return json.loads(queue.send_message.call_args.kwargs["MessageBody"])


async def test_expired_registered_successor_delivers_interruption_then_advances_without_a_sandbox(
    client, runtime, promoted, transport, consumer, queue
):
    runtime[2].delete_item(Key={"PK": "chat-input#run-expired", "SK": "input"})
    before = runtime[2].scan()["Items"]
    authority_key = f"AUTHORITY#chat-event:{handoff.envelope_digest(promoted)}"
    authority = runtime[1].store._read("TENANT#tenant", authority_key)
    assert (await resume(client, runtime, promoted)).status_code == 409
    terminal = json.loads(execution(runtime)["chat_queued_terminal"]["S"])
    assert terminal == {
        "phase": "queued",
        "run_id": "run-expired",
        "session_id": "session-a",
        "attempt": 1,
        "credential_epoch": 1,
        "outcome": "interrupted",
        "message_id": None,
        "terminal": True,
        "retryable": True,
        "automatic_replay_permitted": False,
        "accounting_status": "not_used",
        "cleanup_required": True,
        "finalized_at": runtime[-1] + 60,
    }
    assert execution(runtime)["status"] == {"S": "completed"}
    assert "abort_command_id" not in execution(runtime)
    assert "completion_receipt" not in outbox(runtime)
    assert handoff.thread(transport)["processing_task_id"] == "task-run-expired"
    payload = completion.publication.payload(transport)
    assert payload["status"] == "interrupted" and payload["retryable"] is True
    consumer._process_response(payload)
    consumer._process_response(payload)
    completed = await resume(client, runtime, promoted)
    assert completed.status_code == 200, completed.text
    assert completed.json()["completion"]["terminal"] == terminal
    assert completed.json()["completion"]["creation_fenced"] is True
    assert completed.json()["completion"]["input_acknowledgement_ready"] is True
    assert runtime[2].scan()["Items"] == before
    assert runtime[1].store._read("TENANT#tenant", authority_key) == authority
    for sort_key in ("CREATION", "LAUNCH", "TEARDOWN", "PRE-ADMISSION-TEARDOWN"):
        assert runtime[1].store._read("CHAT-LAUNCH#run-expired", sort_key) is None
    successor = json.loads(queue.send_message.call_args.kwargs["MessageBody"])
    assert successor["message_id"] == "run-healthy"
    assert handoff.thread(transport)["processing_task_id"] == "task-run-healthy"
    assert handoff.thread(transport)["pending_turns"] == {}
    assert queue.send_message.call_count == 2
    run_hash = hashlib.sha256(b"run-healthy").hexdigest()
    pod = VerifiedPod(
        "healthy-pod",
        f"chat-turn-{run_hash[:12]}-abcde",
        "adp-test",
        "adp-chat-sandbox",
        "10.0.0.43",
        image_digest=runtime[5].image_digest,
        run_hash=run_hash,
    )
    launch = admit(runtime[1], run_id="run-healthy", digest=handoff.envelope_digest(successor), pod=pod, team_id="team", now=runtime[-1] + 60)
    assert launch.lease_generation == 2 and launch.sandbox_uid == pod.uid
    header = fixtures.finalization.header(runtime)
    assert (await resume(client, runtime, promoted)).json() == completed.json()
    assert (await completion.complete(client, runtime)).status_code == 200
    assert fixtures.finalization.header(runtime) == header
    assert handoff.thread(transport)["processing_task_id"] == "task-run-healthy"
    assert queue.send_message.call_count == 2


async def test_live_input_is_not_finalized_before_its_protected_expiry(client, runtime, promoted, transport, monkeypatch):
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] + 59)
    result = await resume(client, runtime, promoted)
    assert result.status_code == 200 and result.json()["state"] == "unstarted"
    assert outbox(runtime) is None
    assert execution(runtime)["status"] == {"S": "pending"}
    assert handoff.thread(transport)["processing_task_id"] == "task-run-expired"


async def test_consecutive_expired_inputs_deliver_in_order_before_healthy_successor(
    client, runtime, delivered, transport, consumer, buffer, queue, sts, monkeypatch
):
    recovery.supervisor(sts, monkeypatch)
    monkeypatch.setenv("SESSION_TTL_SECONDS", "60")
    buffer("run-expired-first")
    buffer("run-expired-second")
    monkeypatch.setenv("SESSION_TTL_SECONDS", "3600")
    buffer("run-healthy")
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] + 60)
    assert (await completion.complete(client, runtime)).status_code == 200
    for run_id, successor in (("run-expired-first", "run-expired-second"), ("run-expired-second", "run-healthy")):
        envelope = json.loads(queue.send_message.call_args.kwargs["MessageBody"])
        assert envelope["message_id"] == run_id
        assert (await resume(client, runtime, envelope)).status_code == 409
        assert handoff.thread(transport)["processing_task_id"] == f"task-{run_id}"
        payload = completion.publication.payload(transport)
        assert payload["task_id"] == f"task-{run_id}" and payload["status"] == "interrupted"
        consumer._process_response(payload)
        result = await resume(client, runtime, envelope)
        assert result.status_code == 200, result.text
        assert result.json()["completion"]["input_acknowledgement_ready"] is True
        assert handoff.thread(transport)["processing_task_id"] == f"task-{successor}"
        assert (await resume(client, runtime, envelope)).json() == result.json()
        assert handoff.thread(transport)["processing_task_id"] == f"task-{successor}"
    assert queue.send_message.call_count == 3
    assert handoff.thread(transport)["pending_turns"] == {}


async def test_expiry_finalization_denies_late_reservation_and_binding(client, runtime, promoted, transport, consumer):
    assert (await resume(client, runtime, promoted)).status_code == 409
    consumer._process_response(completion.publication.payload(transport))
    assert (await resume(client, runtime, promoted)).status_code == 200
    digest = handoff.envelope_digest(promoted)
    body = {"run_id": "run-expired", "envelope_digest": digest, "image_digest": runtime[5].image_digest}
    result = await client.post(
        "/internal/v1/agent/chat/data/reserve", json=body, headers={"X-Adp-Producer-Proof": recovery.proof(handoff.envelope_digest(body))}
    )
    assert result.status_code == 409
    run_hash = hashlib.sha256(b"run-expired").hexdigest()
    pod = VerifiedPod(
        "late-pod",
        f"chat-turn-{run_hash[:12]}-abcde",
        "adp-test",
        "adp-chat-sandbox",
        "10.0.0.44",
        image_digest=runtime[5].image_digest,
        run_hash=run_hash,
    )
    with pytest.raises(BootstrapRefusedError):
        runtime[1].store.bind(invocation_id="run-expired", digest=digest, pod=pod, now=datetime.fromtimestamp(runtime[-1] + 60, UTC))


@pytest.mark.parametrize("field,value", [("expires_at", {"N": "1"}), ("input_digest", {"S": "f" * 64})])
async def test_substituted_expiry_metadata_cannot_finalize_or_advance(client, runtime, promoted, transport, field, value):
    item = execution(runtime)
    item["chat_user_turn"]["M"][field] = value
    fixtures.write_protected(runtime, item)
    assert (await resume(client, runtime, promoted)).status_code == 404
    assert outbox(runtime) is None
    assert handoff.thread(transport)["processing_task_id"] == "task-run-expired"


@pytest.mark.parametrize("race", ["cancellation", "creation", "launch", "binding", "expiry", "dispatch"])
async def test_expiry_transaction_fences_competing_authority_and_cancellation(client, runtime, promoted, transport, consumer, monkeypatch, race):
    protected = runtime[1].store
    transact = protected.client.transact_write_items
    raced = []

    def transaction(**kwargs):
        ending = any("chat_queued_terminal =" in item.get("Update", {}).get("UpdateExpression", "") for item in kwargs["TransactItems"])
        if ending and not raced:
            raced.append(True)
            if race == "cancellation":
                service = ChatCancellation(protected, runtime[2], transport.sessions)
                body = CancelTurn(session_id="session-a", task_id="task-run-expired")
                service.cancel(body, service.resolve(body, "tenant", "human"))
            else:
                if race in {"creation", "launch"}:
                    item = {"pk": {"S": "CHAT-LAUNCH#run-expired"}, "sk": {"S": race.upper()}}
                elif race == "dispatch":
                    item = protected._read("INVOCATION#run-expired", "DISPATCH")
                    item["envelope_digest"] = {"S": "f" * 64}
                else:
                    item = execution(runtime)
                    if race == "binding":
                        item["workload_binding"] = {"S": "late-pod"}
                    else:
                        item["chat_user_turn"]["M"]["expires_at"] = {"N": str(runtime[-1] + 600)}
                fixtures.write_protected(runtime, item)
        return transact(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", transaction)
    assert (await resume(client, runtime, promoted)).status_code == 409
    assert raced and outbox(runtime) is None
    assert "chat_queued_terminal" not in execution(runtime)
    assert handoff.thread(transport)["processing_task_id"] == "task-run-expired"
    if race == "cancellation":
        assert (await resume(client, runtime, promoted)).status_code == 409
        assert json.loads(execution(runtime)["chat_queued_terminal"]["S"])["outcome"] == "cancelled"
        consumer._process_response(completion.publication.payload(transport))
        assert (await resume(client, runtime, promoted)).status_code == 200


@pytest.mark.parametrize("loss", ["terminal-before", "terminal-after", "owner-queue", "completion-before", "completion-after", "successor-queue"])
async def test_worker_loss_retries_the_same_outcome_and_successor_handoff(client, runtime, promoted, transport, consumer, queue, monkeypatch, loss):
    if loss.startswith("completion") or loss == "successor-queue":
        assert (await resume(client, runtime, promoted)).status_code == 409
        consumer._process_response(completion.publication.payload(transport))
    protected = runtime[1].store
    transact = protected.client.transact_write_items

    def fail():
        raise EndpointConnectionError(endpoint_url="https://storage.example.test")

    def transaction(**kwargs):
        expression = "chat_queued_terminal =" if loss.startswith("terminal") else "completion_pending ="
        matches = any(expression in item.get("Update", {}).get("UpdateExpression", "") for item in kwargs["TransactItems"])
        if matches and loss.endswith("before"):
            fail()
        result = transact(**kwargs)
        if matches and loss.endswith("after"):
            fail()
        return result

    monkeypatch.setattr(protected.client, "transact_write_items", transaction)
    if loss == "owner-queue":
        transport.client.send_message.side_effect = lambda **kwargs: fail()
    if loss == "successor-queue":
        queue.send_message.side_effect = lambda **kwargs: fail()
    assert (await resume(client, runtime, promoted)).status_code == 503
    assert "completion_receipt" not in (outbox(runtime) or {})
    previous = execution(runtime).get("chat_queued_terminal")
    monkeypatch.setattr(protected.client, "transact_write_items", transact)
    transport.client.send_message.side_effect = None
    queue.send_message.side_effect = None
    if loss.startswith("terminal") or loss == "owner-queue":
        assert (await resume(client, runtime, promoted)).status_code == 409
        consumer._process_response(completion.publication.payload(transport))
    result = await resume(client, runtime, promoted)
    assert result.status_code == 200, result.text
    assert result.json()["completion"]["input_acknowledgement_ready"] is True
    if previous is not None:
        assert execution(runtime)["chat_queued_terminal"] == previous
    assert handoff.thread(transport)["processing_task_id"] == "task-run-healthy"
    assert json.loads(queue.send_message.call_args.kwargs["MessageBody"])["message_id"] == "run-healthy"
    assert (await resume(client, runtime, promoted)).json() == result.json()
