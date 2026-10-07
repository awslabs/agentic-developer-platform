"""Real ingest retention, protected registration and completion with transport fixtures."""

import hashlib
import importlib.util
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth import chat_pending_handoff
from src.agentauth.bootstrap import envelope_digest
from src.agentauth.chat_admission import admit
from src.agentauth.external_roots import provision_root
from src.agentauth.model_policy import canonical_json
from src.agentauth.workload import VerifiedPod
from tests.agentauth import test_chat_turn_completion as completion

client = completion.client
runtime = completion.runtime
store = completion.store
sts = completion.sts
capability = completion.capability
retained_input_table = completion.retained_input_table
ready = completion.ready
registered_owner = completion.registered_owner
transport = completion.transport
consumer = completion.consumer
delivered = completion.delivered


@pytest.fixture
def ingest():
    path = Path(__file__).resolve().parents[4] / "modules/agent-factory/gateway/lambdas/ingest/pending_chat.py"
    spec = importlib.util.spec_from_file_location("pending_ingest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def queue(monkeypatch):
    client = Mock()
    client.send_message.return_value = {"MessageId": "next-turn-accepted"}
    monkeypatch.setattr(chat_pending_handoff, "input_transport", lambda: (client, "https://sqs.us-east-1.amazonaws.com/123456789012/chat.fifo"))
    return client


@pytest.fixture
def buffer(runtime, transport, delivered, ingest):
    def retain(run_id="run-next", *, register_failure=None, server_mode=None, **changes):
        request = {
            **completion.fixtures.ROUTING,
            "message_id": run_id,
            "task_id": f"task-{run_id}",
            "session_id": "session-a",
            "tenant_id": "tenant",
            "agent_type": "developer",
            "mode": "chat",
            "message": f"Follow-up {run_id}",
            "attachments": [],
            "arrived_at": datetime.fromtimestamp(runtime[-1], UTC).isoformat(),
            "enqueued_at": runtime[-1],
            "platform_data": {"tenant_id": "tenant", "org_id": "tenant", "team_id": "team"},
            **changes,
        }

        def register(envelope):
            if register_failure == "before":
                raise TimeoutError("root not committed")
            final = {
                **envelope,
                "persona": envelope["agent_type"],
                "source_ref": {"repo": "chat/session-a"},
                "correlation": {"correlation_id": run_id, "root_human_id": "human", "is_human_rooted": True},
            }
            if server_mode is not None:
                final["session_mode"] = server_mode
            provision_root(runtime[1].store, final, source="chat", human_id="human", now=datetime.fromtimestamp(runtime[-1], UTC))
            if register_failure == "after":
                raise TimeoutError("root response lost")
            return canonical_json(final).decode()

        try:
            ingest.buffer_pending_turn(transport.sessions, request, processing_task="task-a", now=int(time.time()), register=register)
        except TimeoutError:
            if register_failure is None:
                raise
        return request

    return retain


@pytest.mark.parametrize("lost_registration_response", [False, True])
async def test_handoff_retains_protected_ephemeral_mode(client, runtime, transport, buffer, queue, lost_registration_response):
    buffer(server_mode="ephemeral", register_failure="after" if lost_registration_response else None)
    result = await completion.complete(client, runtime)
    assert result.status_code == 200, result.text
    published = json.loads(queue.send_message.call_args.kwargs["MessageBody"])
    assert published["session_mode"] == "ephemeral"
    assert runtime[1].store._read("INVOCATION#run-next", "DISPATCH")["envelope_digest"] == {"S": envelope_digest(published)}


def thread(transport):
    return completion.publication.session(transport)["threads"]["thread-a"]


async def test_completion_promotes_oldest_registered_input_and_is_idempotent(client, runtime, transport, buffer, queue):
    first = buffer()
    buffer("run-later")
    expected = next(iter(thread(transport)["pending_turns"].values()))["envelope_json"]
    result = await completion.complete(client, runtime)
    assert result.status_code == 200, result.text
    assert result.json()["input_acknowledgement_ready"] is True
    assert thread(transport)["processing_task_id"] == first["task_id"]
    assert len(thread(transport)["pending_turns"]) == 1
    assert [entry["content"] for entry in thread(transport)["messages"] if entry["role"] == "user"] == ["Follow-up run-later"]
    assert queue.send_message.call_args.kwargs == {
        "QueueUrl": "https://sqs.us-east-1.amazonaws.com/123456789012/chat.fifo",
        "MessageGroupId": "session-a",
        "MessageDeduplicationId": first["task_id"],
        "MessageBody": expected,
    }
    before = completion.publication.session(transport)
    assert (await completion.complete(client, runtime)).json() == result.json()
    assert completion.publication.session(transport) == before
    assert queue.send_message.call_count == 1
    assert runtime[1].store._read("TENANT#tenant", "EXEC#run-next")["status"] == {"S": "pending"}


@pytest.mark.parametrize("failure", ["queue", "missing-receipt", "receipt-write", "receipt-response", "transfer-response"])
async def test_handoff_loss_retries_original_outbox_without_unlocking(client, runtime, transport, buffer, queue, monkeypatch, failure):
    buffer()
    protected = runtime[1].store
    update, transact = protected.client.update_item, protected.client.transact_write_items
    if failure == "queue":
        queue.send_message.side_effect = EndpointConnectionError(endpoint_url="https://queue.example.test")
    elif failure == "missing-receipt":
        queue.send_message.return_value = {}
    elif failure == "transfer-response":

        def lost_transfer(**kwargs):
            result = transact(**kwargs)
            if any("completion_pending =" in item.get("Update", {}).get("UpdateExpression", "") for item in kwargs["TransactItems"]):
                raise EndpointConnectionError(endpoint_url="https://storage.example.test")
            return result

        monkeypatch.setattr(protected.client, "transact_write_items", lost_transfer)
    else:

        def lost_receipt(**kwargs):
            if "input_queue_message_id" in kwargs.get("UpdateExpression", ""):
                if failure == "receipt-response":
                    update(**kwargs)
                raise EndpointConnectionError(endpoint_url="https://storage.example.test")
            return update(**kwargs)

        monkeypatch.setattr(protected.client, "update_item", lost_receipt)
    assert (await completion.complete(client, runtime)).status_code == 503
    assert thread(transport)["processing_task_id"] == "task-run-next"
    assert thread(transport)["pending_turns"] == {}
    outbox = completion.fixtures.outbox(runtime)
    assert "completion_pending" in outbox
    assert ("completion_receipt" in outbox) is (failure == "receipt-response")
    queue.send_message.side_effect = None
    queue.send_message.return_value = {"MessageId": "next-turn-accepted"}
    monkeypatch.setattr(protected.client, "update_item", update)
    monkeypatch.setattr(protected.client, "transact_write_items", transact)
    result = await completion.complete(client, runtime)
    assert result.status_code == 200, result.text
    assert thread(transport)["processing_task_id"] == "task-run-next"
    calls = queue.send_message.call_args_list
    assert len(calls) == (1 if failure in {"receipt-response", "transfer-response"} else 2)
    assert all(call.kwargs["MessageBody"] == calls[0].kwargs["MessageBody"] for call in calls)


async def test_lost_registration_response_recovers_only_existing_protected_root(client, runtime, transport, buffer, queue):
    buffer(register_failure="after")
    assert next(iter(thread(transport)["pending_turns"].values()))["status"] == "registering"
    result = await completion.complete(client, runtime)
    assert result.status_code == 200, result.text
    assert json.loads(queue.send_message.call_args.kwargs["MessageBody"])["message_id"] == "run-next"


async def test_missing_root_remains_held_without_configured_ingest_recovery(client, runtime, transport, buffer, queue, monkeypatch):
    monkeypatch.delenv("BG_INTAKE_INGEST_FUNCTION", raising=False)
    buffer(register_failure="before")
    assert (await completion.complete(client, runtime)).status_code == 503
    assert thread(transport)["processing_task_id"] == "task-a"
    assert completion.receipt(runtime) is None
    queue.send_message.assert_not_called()
    buffer()
    assert (await completion.complete(client, runtime)).status_code == 200


@pytest.mark.parametrize("field,value", [("message", "substituted"), ("user_id", "other-user"), ("session_generation", 123), ("agent_type", "admin")])
async def test_forged_pending_bytes_never_dispatch(client, runtime, transport, buffer, queue, field, value):
    buffer()
    row = completion.publication.session(transport)
    record = next(iter(row["threads"]["thread-a"]["pending_turns"].values()))
    request = json.loads(record["request_json"])
    request[field] = value
    record["request_json"] = canonical_json(request).decode()
    transport.sessions.put_item(Item=row)
    assert (await completion.complete(client, runtime)).status_code == 404
    queue.send_message.assert_not_called()
    assert thread(transport)["processing_task_id"] == "task-a"


async def test_arrival_racing_transfer_preserves_both_inputs(client, runtime, transport, buffer, queue, monkeypatch):
    buffer()
    protected = runtime[1].store
    transact = protected.client.transact_write_items
    raced = False

    def concurrent(**kwargs):
        nonlocal raced
        if not raced and any("completion_pending =" in item.get("Update", {}).get("UpdateExpression", "") for item in kwargs["TransactItems"]):
            raced = True
            buffer("run-concurrent")
        return transact(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", concurrent)
    assert (await completion.complete(client, runtime)).status_code == 409
    assert len(thread(transport)["pending_turns"]) == 2
    queue.send_message.assert_not_called()
    assert (await completion.complete(client, runtime)).status_code == 200
    assert len(thread(transport)["pending_turns"]) == 1


async def test_cancel_racing_transfer_cannot_promote_cancelled_execution(client, runtime, transport, buffer, queue, monkeypatch):
    buffer()
    protected = runtime[1].store
    transact = protected.client.transact_write_items

    def cancel(**kwargs):
        if any("completion_pending =" in item.get("Update", {}).get("UpdateExpression", "") for item in kwargs["TransactItems"]):
            protected.client.update_item(
                TableName=protected.table,
                Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-next"}},
                UpdateExpression="SET abort_command_id = :abort",
                ExpressionAttributeValues={":abort": {"S": "cancel-next"}},
            )
        return transact(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", cancel)
    assert (await completion.complete(client, runtime)).status_code == 409
    assert thread(transport)["processing_task_id"] == "task-a"
    queue.send_message.assert_not_called()


async def test_duplicate_after_promotion_does_not_register_or_buffer_again(client, runtime, transport, buffer, queue, ingest):
    request = buffer()
    assert (await completion.complete(client, runtime)).status_code == 200
    register = Mock()
    before = completion.publication.session(transport)
    assert (
        ingest.buffer_pending_turn(
            transport.sessions,
            {**request, "task_id": "retry-task"},
            processing_task="task-run-next",
            now=int(time.time()),
            register=register,
        )
        is None
    )
    register.assert_not_called()
    assert completion.publication.session(transport) == before
    with pytest.raises(ingest.PendingChatConflict, match="input changed"):
        ingest.buffer_pending_turn(
            transport.sessions,
            {**request, "message": "replacement"},
            processing_task="task-run-next",
            now=int(time.time()),
            register=register,
        )


@pytest.mark.parametrize(
    "destination",
    ["", "__ADP_CHAT_INPUT_QUEUE_URL__", "https://queue.example.test/tasks.fifo", "https://sqs.us-east-1.amazonaws.com/123456789012/tasks"],
)
def test_input_transport_refuses_unconfigured_or_non_fifo_destinations(monkeypatch, destination):
    factory = Mock()
    monkeypatch.setattr(chat_pending_handoff.boto3, "client", factory)
    monkeypatch.setenv("ADP_CHAT_INPUT_QUEUE_URL", destination)
    chat_pending_handoff.input_transport.cache_clear()
    try:
        with pytest.raises(chat_pending_handoff.ChatAuthorizationUnavailableError):
            chat_pending_handoff.input_transport()
        factory.assert_not_called()
    finally:
        chat_pending_handoff.input_transport.cache_clear()


async def test_promoted_input_acquires_next_lease_without_replaying_predecessor(client, runtime, transport, buffer, queue):
    buffer()
    completed = await completion.complete(client, runtime)
    assert completed.status_code == 200, completed.text
    envelope = json.loads(queue.send_message.call_args.kwargs["MessageBody"])
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
    launch = admit(runtime[1], run_id=envelope["message_id"], digest=envelope_digest(envelope), pod=pod, team_id="team", now=runtime[-1])
    assert launch.lease_generation == 2 and launch.sandbox_uid == "fresh-chat-pod"
    assert (await completion.complete(client, runtime)).json() == completed.json()
    assert queue.send_message.call_count == 1
    header = completion.fixtures.finalization.header(runtime)
    assert header["chatLease"]["run_id"] == "run-next"
    assert thread(transport)["processing_task_id"] == "task-run-next"


async def test_missing_transport_does_not_transfer_lock(client, runtime, transport, buffer, monkeypatch):
    buffer()
    monkeypatch.delenv("ADP_CHAT_INPUT_QUEUE_URL", raising=False)
    chat_pending_handoff.input_transport.cache_clear()
    try:
        assert (await completion.complete(client, runtime)).status_code == 503
        assert thread(transport)["processing_task_id"] == "task-a"
        assert "completion_pending" not in completion.fixtures.outbox(runtime)
    finally:
        chat_pending_handoff.input_transport.cache_clear()
