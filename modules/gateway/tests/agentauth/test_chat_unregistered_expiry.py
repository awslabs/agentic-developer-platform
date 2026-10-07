"""Expired retained inputs reconcile without acquiring executable authority."""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.bootstrap import BootstrapRefusedError, envelope_digest
from src.agentauth.chat_admission import admit
from src.agentauth.external_roots import provision_root
from src.agentauth.model_policy import canonical_json
from src.agentauth.workload import VerifiedPod
from tests.agentauth import test_chat_expired_successor as expiry
from tests.agentauth import test_chat_pending_registration as registration

handoff = registration.handoff
completion = handoff.completion
client = registration.client
runtime = registration.runtime
store = registration.store
sts = registration.sts
capability = registration.capability
retained_input_table = registration.retained_input_table
ready = registration.ready
registered_owner = registration.registered_owner
transport = registration.transport
consumer = registration.consumer
delivered = registration.delivered
ingest = registration.ingest
buffer = registration.buffer
queue = registration.queue
retry = registration.retry


@pytest.fixture
def expired(buffer, transport, retry):
    return registration.normalized(registration.delay_request(transport, buffer("run-expired", register_failure="before"), 7300))


def write(runtime, item):
    completion.fixtures.write_protected(runtime, item)


def is_expiry_transaction(kwargs):
    return any(item.get("Put", {}).get("Item", {}).get("chat_unregistered_expiry") == {"BOOL": True} for item in kwargs["TransactItems"])


def delayed_registration(protected, envelope, monkeypatch):
    check = protected._authority_check
    created = datetime.fromisoformat(envelope["arrived_at"])

    def original_check(grant):
        condition = check(grant)
        condition["ConditionCheck"]["ExpressionAttributeValues"][":now"] = {"S": created.strftime("%Y-%m-%dT%H:%M:%SZ")}
        return condition

    with monkeypatch.context() as delayed:
        delayed.setattr(protected, "_authority_check", original_check)
        provision_root(protected, envelope, source="chat", human_id="human", now=created)


def assert_no_execution_authority(runtime, envelope):
    protected = runtime[1].store
    run_id = envelope["message_id"]
    assert protected._read("TENANT#tenant", f"GRANT#{run_id}#1") is None
    assert runtime[2].get_item(Key={"PK": f"chat-input#{run_id}", "SK": "input"}).get("Item") is None
    for sort_key in ("CREATION", "LAUNCH", "TEARDOWN", "PRE-ADMISSION-TEARDOWN"):
        assert protected._read(f"CHAT-LAUNCH#{run_id}", sort_key) is None


async def test_expired_unregistered_input_delivers_once_before_healthy_successor(
    client, runtime, transport, consumer, expired, buffer, queue, retry, sts, monkeypatch
):
    buffer("run-healthy")
    before = runtime[2].scan()["Items"]
    result = await completion.complete(client, runtime)
    assert result.status_code == 200, result.text
    assert json.loads(queue.send_message.call_args.kwargs["MessageBody"]) == expired
    assert handoff.thread(transport)["processing_task_id"] == "task-run-expired"
    retry.invoke.assert_not_called()
    assert_no_execution_authority(runtime, expired)
    assert runtime[1].store._read("TENANT#tenant", f"AUTHORITY#chat-event:{envelope_digest(expired)}") is None
    terminal = json.loads(expiry.execution(runtime)["chat_queued_terminal"]["S"])
    assert terminal["outcome"] == "interrupted" and terminal["retryable"] is True
    assert terminal["accounting_status"] == "not_used" and terminal["automatic_replay_permitted"] is False
    assert expiry.execution(runtime)["chat_user_turn"]["M"]["expires_at"] == {
        "N": str(int((datetime.fromisoformat(expired["arrived_at"]) + timedelta(hours=2)).timestamp()))
    }
    expiry.recovery.supervisor(sts, monkeypatch)
    assert (await expiry.resume(client, runtime, expired)).status_code == 409
    assert "completion_receipt" not in expiry.outbox(runtime)
    assert handoff.thread(transport)["processing_task_id"] == "task-run-expired"
    payload = completion.publication.payload(transport)
    consumer._process_response(payload)
    consumer._process_response(payload)
    completed = await expiry.resume(client, runtime, expired)
    assert completed.status_code == 200, completed.text
    assert completed.json()["completion"]["terminal"] == terminal
    assert completed.json()["completion"]["input_acknowledgement_ready"] is True
    assert [item for item in runtime[2].scan()["Items"] if not item["SK"].startswith("output")] == [
        item for item in before if not item["SK"].startswith("output")
    ]
    events = completion.fixtures.replay(runtime)["events"]
    assert len(events) == 2 and events[-1]["payload"]["task_id"] == "task-run-expired"
    assert events[-1]["payload"]["status"] == "interrupted"
    for item in before:
        if item["SK"] != "output-state":
            assert runtime[2].get_item(Key={"PK": item["PK"], "SK": item["SK"]})["Item"] == item
    successor = json.loads(queue.send_message.call_args.kwargs["MessageBody"])
    assert successor["message_id"] == "run-healthy"
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
    admit(runtime[1], run_id="run-healthy", digest=envelope_digest(successor), pod=pod, team_id="team", now=runtime[-1])
    header = completion.fixtures.finalization.header(runtime)
    assert (await expiry.resume(client, runtime, expired)).json() == completed.json()
    assert (await completion.complete(client, runtime)).status_code == 200
    consumer._process_response(payload)
    assert completion.fixtures.finalization.header(runtime) == header
    assert handoff.thread(transport)["processing_task_id"] == "task-run-healthy"
    assert queue.send_message.call_count == 2
    assert_no_execution_authority(runtime, expired)


@pytest.mark.parametrize(
    "change", ["owner", "generation", "task", "session-expiry", "message", "marker-time", "scheduled", "arrival", "request-owner"]
)
async def test_changed_retained_owner_or_input_cannot_create_terminal_registration(client, runtime, transport, expired, queue, retry, change):
    row = completion.publication.session(transport)
    thread = row["threads"]["thread-a"]
    selected = hashlib.sha256(b"run-expired").hexdigest()
    marker = next(message for message in thread["messages"] if message.get("pending_id") == selected)
    if change == "owner":
        row["owner_principal"] = "another-owner"
    elif change == "generation":
        row["created_at"] += 1
    elif change == "task":
        thread["processing_task_id"] = "another-task"
    elif change == "session-expiry":
        row["expires_at"] = 1
    elif change == "message":
        marker["content"] = "substitute"
    elif change == "marker-time":
        marker["timestamp"] = runtime[-1]
    elif change == "scheduled":
        thread["scheduled_turns"] = {selected: {"task_id": "already-scheduled"}}
    else:
        record = thread["pending_turns"][selected]
        request = json.loads(record["request_json"])
        request["arrived_at" if change == "arrival" else "owner_principal"] = "invalid"
        record["request_json"] = canonical_json(request).decode()
    transport.sessions.put_item(Item=row)
    result = await completion.complete(client, runtime)
    assert result.status_code in {404, 409}, result.text
    assert runtime[1].store._read("INVOCATION#run-expired", "DISPATCH") is None
    assert expiry.execution(runtime) is None and expiry.outbox(runtime) is None
    queue.send_message.assert_not_called()
    retry.invoke.assert_not_called()


@pytest.mark.parametrize(
    "race", ["registration", "grant", "creation", "launch", "routing", "outbox", "owner", "generation", "task", "retained", "marker", "scheduled"]
)
async def test_expiry_transaction_fences_late_registration_and_owner_changes(client, runtime, transport, expired, queue, monkeypatch, race):
    protected = runtime[1].store
    transact = protected.client.transact_write_items
    raced = []

    def transaction(**kwargs):
        if is_expiry_transaction(kwargs) and not raced:
            raced.append(True)
            if race == "registration":
                delayed_registration(protected, expired, monkeypatch)
            elif race in {"grant", "creation", "launch", "routing", "outbox"}:
                if race == "grant":
                    item = {"pk": {"S": "TENANT#tenant"}, "sk": {"S": "GRANT#run-expired#1"}}
                elif race in {"creation", "launch"}:
                    item = {"pk": {"S": "CHAT-LAUNCH#run-expired"}, "sk": {"S": race.upper()}}
                else:
                    prefix = "CHAT-TURN#" if race == "routing" else "CHAT-DELIVERY#"
                    item = deepcopy(
                        next(
                            entry["Put"]["Item"]
                            for entry in kwargs["TransactItems"]
                            if entry.get("Put", {}).get("Item", {}).get("pk", {}).get("S", "").startswith(prefix)
                        )
                    )
                write(runtime, item)
            else:
                row = completion.publication.session(transport)
                thread = row["threads"]["thread-a"]
                if race == "owner":
                    row["owner_principal"] = "another-owner"
                elif race == "generation":
                    row["created_at"] += 1
                elif race == "task":
                    thread["processing_task_id"] = "another-task"
                elif race == "retained":
                    thread["pending_turns"] = {}
                elif race == "marker":
                    thread["messages"] = []
                else:
                    thread["scheduled_turns"] = {"another": {"task_id": "another-task"}}
                transport.sessions.put_item(Item=row)
        return transact(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", transaction)
    result = await completion.complete(client, runtime)
    assert result.status_code == 409, result.text
    assert raced and completion.receipt(runtime) is None
    assert "chat_unregistered_expiry" not in (expiry.execution(runtime) or {})
    queue.send_message.assert_not_called()
    if race == "registration":
        assert (await completion.complete(client, runtime)).status_code == 200
        assert handoff.thread(transport)["processing_task_id"] == "task-run-expired"


@pytest.mark.parametrize("loss", ["before-commit", "after-commit", "queue"])
async def test_lost_worker_retries_atomic_outcome_and_handoff(client, runtime, transport, expired, queue, monkeypatch, loss):
    protected = runtime[1].store
    transact = protected.client.transact_write_items

    def fail():
        raise EndpointConnectionError(endpoint_url="https://storage.example.test")

    def transaction(**kwargs):
        matches = is_expiry_transaction(kwargs)
        if matches and loss == "before-commit":
            fail()
        result = transact(**kwargs)
        if matches and loss == "after-commit":
            fail()
        return result

    monkeypatch.setattr(protected.client, "transact_write_items", transaction)
    if loss == "queue":
        queue.send_message.side_effect = lambda **kwargs: fail()
    assert (await completion.complete(client, runtime)).status_code == 503
    recorded = deepcopy(expiry.execution(runtime))
    assert completion.receipt(runtime) is None
    assert handoff.thread(transport)["processing_task_id"] == ("task-a" if loss == "before-commit" else "task-run-expired")
    monkeypatch.setattr(protected.client, "transact_write_items", transact)
    queue.send_message.side_effect = None
    assert (await completion.complete(client, runtime)).status_code == 200
    if recorded is not None:
        assert expiry.execution(runtime) == recorded
    assert json.loads(queue.send_message.call_args.kwargs["MessageBody"]) == expired
    assert_no_execution_authority(runtime, expired)


async def test_terminal_registration_fences_a_delayed_live_registration(client, runtime, expired, queue, monkeypatch):
    assert (await completion.complete(client, runtime)).status_code == 200
    before = deepcopy(expiry.execution(runtime))
    for clock in (datetime.now(UTC), datetime.fromisoformat(expired["arrived_at"])):
        with pytest.raises(BootstrapRefusedError):
            provision_root(runtime[1].store, expired, source="chat", human_id="human", now=clock)
    with pytest.raises(BootstrapRefusedError, match="dispatch conflict"):
        delayed_registration(runtime[1].store, expired, monkeypatch)
    renewed = {**expired, "arrived_at": datetime.now(UTC).isoformat()}
    with pytest.raises(BootstrapRefusedError, match="dispatch conflict"):
        provision_root(runtime[1].store, renewed, source="chat", human_id="human", now=datetime.now(UTC))
    assert expiry.execution(runtime) == before
    assert_no_execution_authority(runtime, expired)


async def test_partial_root_authority_is_not_renewed_or_used(client, runtime, expired, queue, monkeypatch):
    protected = runtime[1].store
    transact = protected.client.transact_write_items

    def failure(**kwargs):
        raise EndpointConnectionError(endpoint_url="https://storage.example.test")

    monkeypatch.setattr(protected.client, "transact_write_items", failure)
    with pytest.raises(BootstrapRefusedError):
        provision_root(protected, expired, source="chat", human_id="human", now=datetime.fromisoformat(expired["arrived_at"]))
    key = f"AUTHORITY#chat-event:{envelope_digest(expired)}"
    authority = protected._read("TENANT#tenant", key)
    assert authority is not None and expiry.execution(runtime) is None
    monkeypatch.setattr(protected.client, "transact_write_items", transact)
    assert (await completion.complete(client, runtime)).status_code == 200
    assert protected._read("TENANT#tenant", key) == authority
    assert_no_execution_authority(runtime, expired)


async def test_consecutive_unregistered_expiries_retain_each_owner_outcome(
    client, runtime, transport, expired, buffer, queue, consumer, sts, monkeypatch
):
    following = registration.normalized(registration.delay_request(transport, buffer("run-expired-next", register_failure="before"), 7400))
    buffer("run-healthy")
    assert (await completion.complete(client, runtime)).status_code == 200
    expiry.recovery.supervisor(sts, monkeypatch)
    for envelope in (expired, following):
        assert json.loads(queue.send_message.call_args.kwargs["MessageBody"]) == envelope
        assert (await expiry.resume(client, runtime, envelope)).status_code == 409
        payload = completion.publication.payload(transport)
        assert payload["task_id"] == envelope["task_id"]
        consumer._process_response(payload)
        assert (await expiry.resume(client, runtime, envelope)).status_code == 200
        assert_no_execution_authority(runtime, envelope)
    assert handoff.thread(transport)["processing_task_id"] == "task-run-healthy"
    assert queue.send_message.call_count == 3


async def test_expired_input_cannot_advance_before_predecessor_delivery(client, runtime, transport, expired, queue):
    row = completion.publication.session(transport)
    row["threads"]["thread-a"].pop("terminal_delivery")
    transport.sessions.put_item(Item=row)
    assert (await completion.complete(client, runtime)).status_code == 409
    assert expiry.execution(runtime) is None and expiry.outbox(runtime) is None
    assert handoff.thread(transport)["processing_task_id"] == "task-a"
    queue.send_message.assert_not_called()
