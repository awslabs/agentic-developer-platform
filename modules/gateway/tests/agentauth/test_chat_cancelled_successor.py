"""Owner cancellation of buffered inputs and ordered terminal-only handoff."""

import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from botocore.exceptions import EndpointConnectionError
from fastapi import FastAPI
from httpx import AsyncClient

from src.agentauth import chat_cancellation
from src.agentauth.chat_admission import admit
from src.agentauth.workload import VerifiedPod
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
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


@pytest.fixture
async def owner(runtime, transport, db_session_factory, sts, monkeypatch):
    recovery.supervisor(sts, monkeypatch)
    app = FastAPI()
    app.include_router(chat_cancellation.router)
    user = SimpleNamespace(user_id="human", org_id="tenant", account_type="human", auth_source="jwt")
    service = chat_cancellation.ChatCancellation(runtime[1].store, runtime[2], transport.sessions)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[chat_cancellation.cancellation_service] = lambda: service

    async def database():
        async with db_session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway.example.test") as http:
        yield SimpleNamespace(client=http, user=user, service=service)


async def cancel(owner, run_id="run-cancelled"):
    return await owner.client.post("/v1/chat/turns/cancel", json={"session_id": "session-a", "task_id": f"task-{run_id}"})


def execution(runtime, run_id="run-cancelled"):
    return runtime[1].store._read("TENANT#tenant", f"EXEC#{run_id}")


async def resume(client, runtime, envelope):
    return await recovery.resume(client, runtime, run_id=envelope["message_id"], envelope_digest=handoff.envelope_digest(envelope))


@pytest.mark.parametrize("state", ["buffered", "registered-response-lost", "terminal-before-promotion", "handoff-reply-lost"])
async def test_owner_cancels_buffered_successor_then_delivers_outcome_before_advancing(
    client, runtime, buffer, queue, transport, consumer, owner, state
):
    buffer("run-cancelled", register_failure="after" if state == "registered-response-lost" else None)
    buffer("run-healthy")
    before = completion.publication.session(transport)
    result = await cancel(owner)
    assert result.status_code == 202, result.text
    marker = execution(runtime)
    assert marker["status"] == {"S": "pending"} and "abort_command_id" in marker
    assert (await cancel(owner)).status_code == 202
    assert execution(runtime) == marker
    assert completion.publication.session(transport) == before
    if state == "terminal-before-promotion":
        retained = next(iter(handoff.thread(transport)["pending_turns"].values()))
        assert (await resume(client, runtime, json.loads(retained["envelope_json"]))).status_code == 404
        assert execution(runtime)["status"] == {"S": "cancelled"}
        assert (await cancel(owner)).status_code == 202
    if state == "handoff-reply-lost":
        queue.send_message.side_effect = EndpointConnectionError(endpoint_url="https://queue.example.test")
        assert (await completion.complete(client, runtime)).status_code == 503
        assert handoff.thread(transport)["processing_task_id"] == "task-run-cancelled"
        queue.send_message.side_effect = None
    assert (await completion.complete(client, runtime)).status_code == 200
    envelope = json.loads(queue.send_message.call_args.kwargs["MessageBody"])
    assert envelope["message_id"] == "run-cancelled"
    assert (await resume(client, runtime, envelope)).status_code == 409
    terminal = json.loads(execution(runtime)["chat_queued_terminal"]["S"])
    assert terminal["outcome"] == "cancelled" and terminal["retryable"] is False and terminal["accounting_status"] == "not_used"
    assert handoff.thread(transport)["processing_task_id"] == "task-run-cancelled"
    payload = completion.publication.payload(transport)
    assert payload["task_id"] == "task-run-cancelled" and payload["status"] == "cancelled"
    consumer._process_response(payload)
    consumer._process_response(payload)
    completed = await resume(client, runtime, envelope)
    assert completed.status_code == 200, completed.text
    assert completed.json()["completion"]["input_acknowledgement_ready"] is True
    assert completed.json()["completion"]["creation_fenced"] is True
    assert handoff.thread(transport)["processing_task_id"] == "task-run-healthy"
    successor = json.loads(queue.send_message.call_args.kwargs["MessageBody"])
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
    launch = admit(runtime[1], run_id="run-healthy", digest=handoff.envelope_digest(successor), pod=pod, team_id="team", now=runtime[-1])
    assert launch.lease_generation == 2
    header = completion.fixtures.finalization.header(runtime)
    assert (await resume(client, runtime, envelope)).json() == completed.json()
    assert (await completion.complete(client, runtime)).status_code == 200
    assert completion.fixtures.finalization.header(runtime) == header
    assert handoff.thread(transport)["processing_task_id"] == "task-run-healthy"
    assert queue.send_message.call_count == (3 if state == "handoff-reply-lost" else 2)
    for sort_key in ("CREATION", "LAUNCH", "TEARDOWN"):
        assert runtime[1].store._read("CHAT-LAUNCH#run-cancelled", sort_key) is None


async def test_consecutive_cancelled_successors_preserve_owner_outcomes_and_order(client, runtime, buffer, queue, transport, consumer, owner):
    for run_id in ("run-first", "run-second", "run-healthy"):
        buffer(run_id)
    for run_id in ("run-first", "run-second"):
        assert (await cancel(owner, run_id)).status_code == 202
    assert (await completion.complete(client, runtime)).status_code == 200
    for run_id, next_run in (("run-first", "run-second"), ("run-second", "run-healthy")):
        envelope = json.loads(queue.send_message.call_args.kwargs["MessageBody"])
        assert envelope["message_id"] == run_id
        assert (await resume(client, runtime, envelope)).status_code == 409
        payload = completion.publication.payload(transport)
        assert payload["task_id"] == f"task-{run_id}" and payload["status"] == "cancelled"
        assert handoff.thread(transport)["processing_task_id"] == f"task-{run_id}"
        consumer._process_response(payload)
        result = await resume(client, runtime, envelope)
        assert result.status_code == 200
        assert (await resume(client, runtime, envelope)).json() == result.json()
        assert handoff.thread(transport)["processing_task_id"] == f"task-{next_run}"
    assert queue.send_message.call_count == 3
    assert handoff.thread(transport)["pending_turns"] == {}


@pytest.mark.parametrize("change", ["owner", "generation", "expiry", "channel", "missing", "request", "envelope", "marker", "scheduled", "task"])
async def test_buffered_cancellation_refuses_changed_owner_or_retained_input(runtime, buffer, transport, owner, change):
    buffer("run-cancelled")
    row = completion.publication.session(transport)
    thread = row["threads"]["thread-a"]
    selected = hashlib.sha256(b"run-cancelled").hexdigest()
    if change in {"owner", "generation", "expiry", "channel"}:
        field, value = {
            "owner": ("owner_principal", "foreign"),
            "generation": ("created_at", 1),
            "expiry": ("expires_at", 1),
            "channel": ("channel", "foreign"),
        }[change]
        row[field] = value
    elif change == "missing":
        thread["pending_turns"] = {}
    elif change == "request":
        request = json.loads(thread["pending_turns"][selected]["request_json"])
        request["message"] = "substituted"
        thread["pending_turns"][selected]["request_json"] = json.dumps(request)
    elif change == "envelope":
        thread["pending_turns"][selected]["envelope_json"] = "{}"
    elif change == "marker":
        thread["messages"].append(thread["messages"][-1])
    elif change == "scheduled":
        thread["scheduled_turns"] = {selected: {"task_id": "task-run-cancelled"}}
    else:
        thread["processing_task_id"] = ""
    transport.sessions.put_item(Item=row)
    assert (await cancel(owner)).status_code == 404
    assert "abort_command_id" not in execution(runtime)
    assert completion.publication.session(transport) == row


@pytest.mark.parametrize("field,value", [("user_id", "other"), ("org_id", "other"), ("account_type", "service"), ("auth_source", "iam")])
async def test_buffered_cancellation_requires_its_authenticated_human_owner(runtime, buffer, owner, field, value):
    buffer("run-cancelled")
    setattr(owner.user, field, value)
    assert (await cancel(owner)).status_code == 404
    assert "abort_command_id" not in execution(runtime)


@pytest.mark.parametrize("race", ["owner", "generation", "expiry", "pending", "processing", "dispatch", "creation", "binding"])
async def test_buffered_cancellation_fences_concurrent_scope_or_authority_changes(runtime, buffer, owner, transport, monkeypatch, race):
    buffer("run-cancelled")
    protected = runtime[1].store
    transact = protected.client.transact_write_items
    raced = []

    def transaction(**kwargs):
        cancelling = any("abort_command_id =" in item.get("Update", {}).get("UpdateExpression", "") for item in kwargs["TransactItems"])
        if cancelling and not raced:
            raced.append(True)
            if race in {"dispatch", "creation", "binding"}:
                if race == "creation":
                    item = {"pk": {"S": "CHAT-LAUNCH#run-cancelled"}, "sk": {"S": "CREATION"}}
                elif race == "dispatch":
                    item = protected._read("INVOCATION#run-cancelled", "DISPATCH")
                    item["envelope_digest"] = {"S": "f" * 64}
                else:
                    item = execution(runtime)
                    item["workload_binding"] = {"S": "late-pod"}
                completion.fixtures.write_protected(runtime, item)
            else:
                row = completion.publication.session(transport)
                if race == "owner":
                    row["owner_principal"] = "foreign"
                elif race == "generation":
                    row["created_at"] += 1
                elif race == "expiry":
                    row["expires_at"] = 1
                elif race == "pending":
                    row["threads"]["thread-a"]["pending_turns"] = {}
                else:
                    row["threads"]["thread-a"]["processing_task_id"] = "later-task"
                transport.sessions.put_item(Item=row)
        return transact(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", transaction)
    assert (await cancel(owner)).status_code == 409
    assert raced and "abort_command_id" not in execution(runtime)


async def test_cancellation_racing_handoff_retries_as_terminal_only_work(client, runtime, buffer, owner, queue, transport, monkeypatch):
    buffer("run-cancelled")
    protected = runtime[1].store
    transact = protected.client.transact_write_items
    raced = []

    def transaction(**kwargs):
        if not raced and any("completion_pending =" in item.get("Update", {}).get("UpdateExpression", "") for item in kwargs["TransactItems"]):
            raced.append(True)
            body = chat_cancellation.CancelTurn(session_id="session-a", task_id="task-run-cancelled")
            owner.service.cancel(body, owner.service.resolve(body, "tenant", "human"))
        return transact(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", transaction)
    assert (await completion.complete(client, runtime)).status_code == 409
    assert handoff.thread(transport)["processing_task_id"] == "task-a"
    queue.send_message.assert_not_called()
    assert (await completion.complete(client, runtime)).status_code == 200
    envelope = json.loads(queue.send_message.call_args.kwargs["MessageBody"])
    assert (await resume(client, runtime, envelope)).status_code == 409
    assert json.loads(execution(runtime)["chat_queued_terminal"]["S"])["outcome"] == "cancelled"


@pytest.mark.parametrize("loss", ["before", "after"])
async def test_lost_buffered_cancellation_transaction_retries_without_changing_the_lock(runtime, buffer, owner, transport, monkeypatch, loss):
    buffer("run-cancelled")
    protected = runtime[1].store
    transact = protected.client.transact_write_items
    before = completion.publication.session(transport)

    def transaction(**kwargs):
        if loss == "after":
            transact(**kwargs)
        raise EndpointConnectionError(endpoint_url="https://storage.example.test")

    monkeypatch.setattr(protected.client, "transact_write_items", transaction)
    assert (await cancel(owner)).status_code == 503
    previous = execution(runtime)
    assert ("abort_command_id" in previous) == (loss == "after")
    monkeypatch.setattr(protected.client, "transact_write_items", transact)
    assert (await cancel(owner)).status_code == 202
    if loss == "after":
        assert execution(runtime) == previous
    assert completion.publication.session(transport) == before


@pytest.mark.parametrize("change", ["command", "attempt", "digest", "time"])
async def test_forged_cancellation_cannot_advance_buffered_work(client, runtime, buffer, owner, queue, transport, change):
    buffer("run-cancelled")
    assert (await cancel(owner)).status_code == 202
    item = execution(runtime)
    field, value = {
        "command": ("abort_command_id", {"S": "foreign"}),
        "attempt": ("abort_requested_attempt", {"N": "2"}),
        "digest": ("abort_body_digest", {"S": "f" * 64}),
        "time": ("abort_requested_at", {"S": "invalid"}),
    }[change]
    item[field] = value
    completion.fixtures.write_protected(runtime, item)
    assert (await completion.complete(client, runtime)).status_code == 409
    assert handoff.thread(transport)["processing_task_id"] == "task-a"
    queue.send_message.assert_not_called()
