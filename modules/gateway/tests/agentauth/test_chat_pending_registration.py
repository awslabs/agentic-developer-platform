"""Automatic recovery composes trusted ingest, protected storage and completion."""

import hashlib
import io
import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.external_roots import provision_root
from src.agentauth.model_policy import canonical_json
from src.orchestration import intake_wiring
from tests.agentauth import test_chat_pending_handoff as handoff

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


def normalized(request):
    return {
        **request,
        "persona": request["agent_type"],
        "source_ref": {"repo": "chat/session-a"},
        "correlation": {"correlation_id": request["message_id"], "root_human_id": "human", "is_human_rooted": True},
    }


@pytest.fixture
def retry(monkeypatch, runtime, transport, ingest):
    def register(request):
        envelope = normalized(request)
        provision_root(runtime[1].store, envelope, source="chat", human_id="human", now=datetime.now(UTC))
        return canonical_json(envelope).decode()

    def invoke(**kwargs):
        event = json.loads(kwargs["Payload"])
        assert kwargs["FunctionName"] == "trusted-ingest"
        assert kwargs["InvocationType"] == "RequestResponse"
        assert set(event) == {"source", "binding"}
        assert event["source"] == "chat-pending-recovery"
        ingest.recover_pending_turn(transport.sessions, event["binding"], now=time.time(), register=register)
        return {"StatusCode": 200, "Payload": io.BytesIO(b'{"statusCode":200}')}

    client = Mock()
    client.invoke.side_effect = invoke
    monkeypatch.setenv(intake_wiring.INTAKE_FUNCTION_ENV, "trusted-ingest")
    monkeypatch.setattr(intake_wiring, "_get_lambda_client", lambda: client)
    monkeypatch.setattr(intake_wiring, "_get_sessions_table", lambda: transport.sessions)
    return client


async def test_uncommitted_registration_recovers_without_owner_retry(client, runtime, transport, buffer, queue, retry):
    request = buffer(register_failure="before")
    assert runtime[1].store._read("INVOCATION#run-next", "DISPATCH") is None
    assert (await handoff.completion.complete(client, runtime)).status_code == 409
    assert handoff.thread(transport)["processing_task_id"] == "task-a"
    queue.send_message.assert_not_called()
    assert handoff.completion.receipt(runtime) is None
    assert (await handoff.completion.complete(client, runtime)).status_code == 200
    assert retry.invoke.call_count == 1
    assert json.loads(queue.send_message.call_args.kwargs["MessageBody"]) == normalized(request)
    assert handoff.thread(transport)["processing_task_id"] == request["task_id"]


@pytest.mark.parametrize("loss", ["before-invoke", "after-invoke", "successful-lie", "function-error", "malformed-response"])
async def test_retry_transport_cannot_release_or_create_authority(client, runtime, transport, buffer, queue, retry, loss):
    buffer(register_failure="before")
    invoke = retry.invoke.side_effect

    def failure(**kwargs):
        if loss == "after-invoke":
            invoke(**kwargs)
        if loss in {"before-invoke", "after-invoke"}:
            raise EndpointConnectionError(endpoint_url="https://lambda.example.test")
        if loss == "successful-lie":
            return {"StatusCode": 200, "Payload": io.BytesIO(b'{"statusCode":200}')}
        if loss == "function-error":
            return {"StatusCode": 200, "FunctionError": "Unhandled", "Payload": io.BytesIO(b"{}")}
        return {"StatusCode": 200, "Payload": io.BytesIO(b"not-json")}

    retry.invoke.side_effect = failure
    result = await handoff.completion.complete(client, runtime)
    assert result.status_code == (409 if loss == "successful-lie" else 503)
    assert handoff.thread(transport)["processing_task_id"] == "task-a"
    assert handoff.completion.receipt(runtime) is None
    queue.send_message.assert_not_called()
    if loss != "after-invoke":
        assert runtime[1].store._read("INVOCATION#run-next", "DISPATCH") is None
    retry.invoke.side_effect = invoke
    if loss != "after-invoke":
        assert (await handoff.completion.complete(client, runtime)).status_code == 409
    assert (await handoff.completion.complete(client, runtime)).status_code == 200


async def test_lost_registration_status_write_recovers_committed_root(client, runtime, transport, buffer, queue, retry, monkeypatch):
    buffer(register_failure="before")
    update = transport.sessions.update_item
    monkeypatch.setattr(transport.sessions, "update_item", Mock(side_effect=EndpointConnectionError(endpoint_url="https://storage.example.test")))
    assert (await handoff.completion.complete(client, runtime)).status_code == 503
    monkeypatch.setattr(transport.sessions, "update_item", update)
    assert next(iter(handoff.thread(transport)["pending_turns"].values()))["status"] == "registering"
    assert (await handoff.completion.complete(client, runtime)).status_code == 200
    assert retry.invoke.call_count == 1


def delay_request(transport, request, seconds):
    delayed = {**request, "arrived_at": (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()}
    row = handoff.completion.publication.session(transport)
    thread = row["threads"]["thread-a"]
    selected = hashlib.sha256(request["message_id"].encode()).hexdigest()
    thread["pending_turns"][selected]["request_json"] = canonical_json(delayed).decode()
    for message in thread["messages"]:
        if message.get("pending_id") == selected:
            message["timestamp"] = int(time.time()) - seconds
    transport.sessions.put_item(Item=row)
    return delayed


async def test_delayed_retained_registration_preserves_original_expiry(client, runtime, transport, buffer, queue, retry):
    request = delay_request(transport, buffer(register_failure="before"), 1800)
    assert (await handoff.completion.complete(client, runtime)).status_code == 409
    assert (await handoff.completion.complete(client, runtime)).status_code == 200
    envelope = normalized(request)
    from src.agentauth.bootstrap import envelope_digest

    root = runtime[1].store._read("TENANT#tenant", f"AUTHORITY#chat-event:{envelope_digest(envelope)}")
    assert root["expires_at"] == {"S": (datetime.fromisoformat(request["arrived_at"]) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")}


@pytest.mark.parametrize(
    "change", ["no-record", "message", "owner", "generation", "expired-session", "unlocked", "scheduled", "marker", "expired-root"]
)
def test_stale_root_requires_exact_live_retention(runtime, transport, buffer, retry, change):
    request = delay_request(transport, buffer(register_failure="before"), 7300 if change == "expired-root" else 1800)
    envelope = normalized(request)
    row = handoff.completion.publication.session(transport)
    thread = row["threads"]["thread-a"]
    selected = hashlib.sha256(request["message_id"].encode()).hexdigest()
    if change == "no-record":
        thread["pending_turns"] = {}
    elif change == "message":
        envelope["message"] = "substitute"
    elif change == "owner":
        row["owner_principal"] = "other-owner"
    elif change == "generation":
        row["created_at"] += 1
    elif change == "expired-session":
        row["expires_at"] = 1
    elif change == "unlocked":
        thread["processing_task_id"] = ""
    elif change == "scheduled":
        thread["scheduled_turns"] = {selected: {"task_id": request["task_id"]}}
    elif change == "marker":
        thread["messages"] = []
    transport.sessions.put_item(Item=row)
    with pytest.raises(BootstrapRefusedError, match="stale root"):
        provision_root(runtime[1].store, envelope, source="chat", human_id="human", now=datetime.now(UTC))
    assert runtime[1].store._read("INVOCATION#run-next", "DISPATCH") is None
