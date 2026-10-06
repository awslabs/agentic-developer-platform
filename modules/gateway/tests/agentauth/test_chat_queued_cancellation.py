import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.bootstrap import BootstrapRefusedError, envelope_digest
from src.agentauth.chat_cancellation import CancelTurn
from src.agentauth.chat_delivery import delivery_lookup_key
from src.agentauth.execution import ExecutionStatus
from src.agentauth.workload import VerifiedPod
from tests.agentauth import test_chat_cancellation as fixtures
from tests.agentauth.test_chat_delivery import ROUTING
from tests.agentauth.test_chat_user_turn import accept, prepare

client = fixtures.client
model = fixtures.model
owner = fixtures.owner
protected_root = fixtures.protected_root
retained_input_table = fixtures.retained_input_table
runtime = fixtures.runtime
store = fixtures.store
sts = fixtures.sts
transport = fixtures.transport
PATH = fixtures.PATH


@pytest.fixture
def queued(owner, model, transport, monkeypatch):
    envelope = prepare(
        model.runtime,
        **{
            **ROUTING,
            "message_id": "run-queued",
            "task_id": "task-queued",
            "session_id": "session-queued",
            "source_ref": {"repo": "chat/session-queued"},
        },
    )
    session = {**transport.row, "session_id": "session-queued", "threads": {"thread-a": {"processing_task_id": "task-queued"}}}
    transport.sessions.put_item(Item=session)
    run_hash = hashlib.sha256(b"run-queued").hexdigest()
    pod = VerifiedPod(
        "queued-pod-uid",
        f"chat-turn-{run_hash[:12]}-abcde",
        "adp-gateway-agents",
        "adp-chat-sandbox",
        "127.0.0.1",
        image_digest=model.runtime[5].image_digest,
        run_hash=run_hash,
    )
    original = model.runtime[1].workloads.verify_bound
    monkeypatch.setattr(
        model.runtime[1].workloads, "verify_bound", lambda **fields: pod if fields == {"name": pod.name, "uid": pod.uid} else original(**fields)
    )
    return SimpleNamespace(envelope=envelope, pod=pod, body={"session_id": "session-queued", "task_id": "task-queued"}, session=session)


async def admit(queued, model):
    return await accept(model.client, model.runtime, queued.envelope, pod_name=queued.pod.name, pod_uid=queued.pod.uid)


def execution(model):
    return model.runtime[1].store.authority.load_execution(invocation_id="run-queued", tenant_id="tenant")


def marker(model):
    return model.runtime[1].store.authority.abort_intent(invocation_id="run-queued", tenant_id="tenant")


def bind(queued, model):
    return model.runtime[1].store.bind(
        invocation_id="run-queued", digest=envelope_digest(queued.envelope), pod=queued.pod, now=datetime.fromtimestamp(model.runtime[-1], UTC)
    )


def request_intent(queued, owner):
    body = CancelTurn(**queued.body)
    owner.service.cancel(body, owner.service.resolve(body, "tenant", "human"))


async def test_queued_owner_cancels_before_context_or_lease_exists(queued, owner, model, transport):
    assert "Item" not in model.runtime[2].get_item(Key={"PK": "session#session-queued", "SK": "header"})
    response = await owner.client.post(PATH, json=queued.body)
    assert response.status_code == 202 and response.json() == {"status": "cancellation_requested", **queued.body}
    first = marker(model)
    assert first is not None
    assert (await owner.client.post(PATH, json=queued.body)).status_code == 202
    assert marker(model) == first
    assert execution(model).status == ExecutionStatus.PENDING and execution(model).workload_binding is None
    assert (await admit(queued, model)).status_code == 404
    assert model.runtime[1].store._read(f"POD#{queued.pod.uid}", "BINDING") is None
    assert model.runtime[1].store._read("CHAT-LAUNCH#run-queued", "LAUNCH") is None
    assert model.runtime[2].get_item(Key={"PK": "chat-input#run-queued", "SK": "input"}).get("Item")
    assert transport.sessions.get_item(Key={"session_id": "session-queued"})["Item"]["threads"]["thread-a"]["processing_task_id"] == "task-queued"
    transport.client.send_message.assert_not_called()
    model.provider.assert_not_awaited()


async def test_uncancelled_queued_turn_still_admits(queued, model):
    response = await admit(queued, model)
    assert response.status_code == 200, response.text
    assert execution(model).status == ExecutionStatus.ACTIVE and marker(model) is None
    assert model.runtime[1].store._read("CHAT-LAUNCH#run-queued", "LAUNCH") is not None


@pytest.mark.parametrize("stage", ["binding", "lease"])
async def test_cancellation_racing_admission_is_fenced_in_transaction(queued, owner, model, monkeypatch, stage):
    store = model.runtime[1].store
    original = store.client.transact_write_items
    raced = []

    def transact(**request):
        binding = any("workload_binding = :pod" in item.get("Update", {}).get("UpdateExpression", "") for item in request["TransactItems"])
        lease = any(item.get("Put", {}).get("Item", {}).get("pk") == {"S": "CHAT-LAUNCH#run-queued"} for item in request["TransactItems"])
        if not raced and ((stage == "binding" and binding) or (stage == "lease" and lease)):
            raced.append(True)
            request_intent(queued, owner)
        return original(**request)

    monkeypatch.setattr(store.client, "transact_write_items", transact)
    response = await admit(queued, model)
    assert response.status_code == 404, response.text
    assert raced and marker(model) is not None
    assert store._read("CHAT-LAUNCH#run-queued", "LAUNCH") is None
    assert "Item" not in model.runtime[2].get_item(Key={"PK": "session#session-queued", "SK": "header"})
    assert execution(model).status == (ExecutionStatus.PENDING if stage == "binding" else ExecutionStatus.ACTIVE)
    assert (await admit(queued, model)).status_code == 404


async def test_binding_wins_cancel_write_then_owner_retry_cancels_before_lease(queued, owner, model, monkeypatch):
    store = model.runtime[1].store
    original = store.client.update_item
    raced = []

    def update(**request):
        if "abort_command_id" in request.get("UpdateExpression", "") and not raced:
            raced.append(True)
            bind(queued, model)
        return original(**request)

    monkeypatch.setattr(store.client, "update_item", update)
    assert (await owner.client.post(PATH, json=queued.body)).status_code == 409
    assert marker(model) is None and execution(model).status == ExecutionStatus.ACTIVE
    assert (await owner.client.post(PATH, json=queued.body)).status_code == 202
    assert marker(model) is not None and (await admit(queued, model)).status_code == 404


async def test_lost_pending_cancel_write_response_retries_without_overwrite(queued, owner, model, monkeypatch):
    store = model.runtime[1].store
    original = store.client.update_item

    def update(**request):
        result = original(**request)
        if "abort_command_id" in request.get("UpdateExpression", ""):
            raise EndpointConnectionError(endpoint_url="https://private-storage.example.test")
        return result

    monkeypatch.setattr(store.client, "update_item", update)
    response = await owner.client.post(PATH, json=queued.body)
    assert response.status_code == 503 and "private-storage" not in response.text
    first = marker(model)
    assert first is not None
    monkeypatch.setattr(store.client, "update_item", original)
    assert (await owner.client.post(PATH, json=queued.body)).status_code == 202
    assert marker(model) == first


@pytest.mark.parametrize("change", ["owner", "task", "generation", "expired", "pointer", "metadata", "no_index", "attempt", "foreign_context"])
async def test_queued_cancel_rejects_changed_authority(queued, owner, model, transport, change):
    store = model.runtime[1].store
    if change in {"owner", "task", "generation", "expired"}:
        row = queued.session
        if change == "owner":
            row["owner_principal"] = "foreign"
        elif change == "task":
            row["threads"]["thread-a"]["processing_task_id"] = "other"
        elif change == "generation":
            row["created_at"] += 1
        else:
            row["expires_at"] = 1
        transport.sessions.put_item(Item=row)
    elif change in {"pointer", "no_index"}:
        key = delivery_lookup_key("tenant", "session-queued", "task-queued")
        store.client.delete_item(TableName=store.table, Key=key)
        if change == "pointer":
            store.client.put_item(TableName=store.table, Item={**key, "run_id": {"S": "run-user"}})
    elif change == "foreign_context":
        model.runtime[2].put_item(Item={"PK": "session#session-queued", "SK": "header", "ownerUserId": "other"})
    else:
        field, value = ("chat_delivery", {"S": "{}"}) if change == "metadata" else ("current_attempt", {"N": "2"})
        store.client.update_item(
            TableName=store.table,
            Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-queued"}},
            UpdateExpression=f"SET {field} = :value",
            ExpressionAttributeValues={":value": value},
        )
    assert (await owner.client.post(PATH, json=queued.body)).status_code == 404
    assert marker(model) is None
    assert store.authority.abort_intent(invocation_id="run-user", tenant_id="tenant") is None


async def test_registration_index_is_atomic_immutable_and_retryable(queued, model):
    store = model.runtime[1].store
    key = delivery_lookup_key("tenant", "session-queued", "task-queued")
    assert store.client.get_item(TableName=store.table, Key=key)["Item"]["run_id"] == {"S": "run-queued"}
    assert prepare(model.runtime, **queued.envelope) == queued.envelope
    with pytest.raises(BootstrapRefusedError):
        prepare(model.runtime, **{**queued.envelope, "message_id": "run-replacement"})
    assert store._read("TENANT#tenant", "EXEC#run-replacement") is None
    assert store._read("INVOCATION#run-replacement", "DISPATCH") is None
    assert "Item" not in model.runtime[2].get_item(Key={"PK": "chat-input#run-replacement", "SK": "input"})


async def test_admitted_legacy_turn_without_index_still_cancels(owner, model):
    store = model.runtime[1].store
    store.client.delete_item(TableName=store.table, Key=delivery_lookup_key("tenant", "session-a", "task-a"))
    assert (await owner.client.post(PATH, json=fixtures.BODY)).status_code == 202
