"""Scoped one-turn mailbox reads only a protected, accepted owner message."""

import hashlib

import pytest

from src.agentauth import chat_model
from src.agentauth.workload import VerifiedPod, WorkloadRefusedError
from tests.agentauth.test_chat_data_routes import exchange
from tests.agentauth.test_chat_user_turn import accept, prepare
from tests.agentauth.test_chat_user_turn import client as client_fixture
from tests.agentauth.test_chat_user_turn import retained_input_table as retained_input_table_fixture
from tests.agentauth.test_chat_user_turn import runtime as runtime_fixture
from tests.agentauth.test_chat_user_turn import store as store_fixture
from tests.agentauth.test_chat_user_turn import sts as sts_fixture

client = client_fixture
runtime = runtime_fixture
store = store_fixture
sts = sts_fixture
retained_input_table = retained_input_table_fixture
PATH = "/v1/chat/turn/next"


@pytest.fixture
async def mailbox(client, runtime, monkeypatch):
    authority = runtime[1]
    run_hash = hashlib.sha256(b"run-user").hexdigest()
    pod_name = f"chat-turn-{run_hash[:12]}-abcde"
    sandbox = VerifiedPod(
        "chat-pod",
        pod_name,
        "adp-gateway-agents",
        "adp-chat-sandbox",
        "127.0.0.1",
        image_digest=runtime[5].image_digest,
        run_hash=run_hash,
    )
    state = {"pod": sandbox}

    class Workloads:
        def verify(self, proof):
            if proof != "sandbox-token":
                raise WorkloadRefusedError("sandbox workload refused")
            return state["pod"]

        def verify_bound(self, *, name, uid):
            if name != sandbox.name or uid != sandbox.uid:
                raise WorkloadRefusedError("sandbox binding refused")
            return state["pod"]

    monkeypatch.setattr(authority, "workloads", Workloads())
    client.headers["X-Adp-Workload-Token"] = "sandbox-token"
    envelope = prepare(runtime)
    admitted = await accept(client, runtime, envelope, pod_name=pod_name)
    assert admitted.status_code == 200, admitted.text
    exchanged = await exchange(client, **{"X-Adp-Workload-Token": "sandbox-token"})
    assert exchanged.status_code == 200, exchanged.text
    return client, runtime, exchanged.json()["capability"], state, envelope


async def next_turn(client, token, *, workload="sandbox-token", **changes):
    return await client.post(
        PATH,
        json={"run_id": "run-user", "session_id": "session-a", **changes},
        headers={"Authorization": f"Bearer {token}", "X-Adp-Workload-Token": workload},
    )


async def test_bound_sandbox_reads_only_its_accepted_turn(mailbox):
    client, _, token, _, envelope = mailbox
    response = await next_turn(client, token)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["run_id"] == "run-user"
    assert response.json()["session_id"] == "session-a"
    assert response.json()["lease_generation"] == 1
    assert response.json()["turn"]["message"]["content"] == envelope["message"]
    assert response.json()["turn"]["message"]["parts"] == [{"type": "file", "artifactId": "art_0123456789ab"}]
    assert token not in response.text


@pytest.mark.parametrize("forgery", [{"run_id": "run-a"}, {"session_id": "session-other"}, {"user_id": "other"}])
async def test_turn_scope_cannot_be_forged(mailbox, forgery):
    client, _, token, _, _ = mailbox
    response = await next_turn(client, token, **forgery)
    assert response.status_code == (422 if "user_id" in forgery else 404)


async def test_wrong_workload_or_changed_lease_cannot_read_turn(mailbox):
    client, runtime, token, state, _ = mailbox
    assert (await next_turn(client, token, workload="other-token")).status_code == 404
    original = state["pod"]
    state["pod"] = VerifiedPod(
        "other-pod", original.name, original.namespace, original.service_account, original.ip, image_digest=original.image_digest
    )
    assert (await next_turn(client, token)).status_code == 404
    state["pod"] = original
    runtime[2].update_item(
        Key={"PK": "session#session-a", "SK": "header"},
        UpdateExpression="SET chatLease.generation = :generation",
        ExpressionAttributeValues={":generation": 2},
    )
    assert (await next_turn(client, token)).status_code == 404


@pytest.mark.parametrize("change", ["missing", "other_owner", "other_run", "not_accepted"])
async def test_missing_or_substituted_receipt_refuses_turn(mailbox, change):
    client, runtime, token, _, _ = mailbox
    table = runtime[2]
    key = {"PK": "session#session-a", "SK": "turn#run-user"}
    if change == "missing":
        table.delete_item(Key=key)
    else:
        field, value = {
            "other_owner": ("ownerUserId", "other"),
            "other_run": ("runId", "run-a"),
            "not_accepted": ("status", "interrupted"),
        }[change]
        table.update_item(
            Key=key, UpdateExpression="SET #field = :value", ExpressionAttributeNames={"#field": field}, ExpressionAttributeValues={":value": value}
        )
    assert (await next_turn(client, token)).status_code == 404


async def test_missing_message_is_unavailable_not_an_empty_turn(mailbox):
    client, runtime, token, _, _ = mailbox
    reference = runtime[2].get_item(Key={"PK": "session#session-a", "SK": "turn#run-user"})["Item"]["ref"]
    runtime[2].delete_item(Key={"PK": "session#session-a", "SK": f"msg#{reference}"})
    response = await next_turn(client, token)
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "chat_authority_unavailable"


async def test_same_owner_message_substitution_cannot_replace_trusted_input(mailbox):
    client, runtime, token, _, _ = mailbox
    receipt = runtime[2].get_item(Key={"PK": "session#session-a", "SK": "turn#run-user"})["Item"]
    runtime[2].update_item(
        Key={"PK": "session#session-a", "SK": f"msg#{receipt['ref']}"},
        UpdateExpression="SET content = :content",
        ExpressionAttributeValues={":content": "Substituted message"},
    )
    response = await next_turn(client, token)
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "chat_authority_unavailable"


async def test_protected_input_tampering_cannot_be_served(mailbox):
    client, runtime, token, _, _ = mailbox
    store = runtime[1].store
    protected = store._read("TENANT#tenant", "EXEC#run-user")
    protected["chat_user_turn"]["M"]["message_digest"] = {"S": "0" * 64}
    store.client.put_item(TableName=store.table, Item=protected)
    response = await next_turn(client, token)
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "chat_authority_unavailable"


async def test_message_replacement_between_verification_and_history_read_refuses_turn(mailbox, monkeypatch):
    client, runtime, token, _, _ = mailbox
    original = chat_model.ChatHistoryStore.get_messages

    def replaced(store, *args, **kwargs):
        reference = runtime[2].get_item(Key={"PK": "session#session-a", "SK": "turn#run-user"})["Item"]["ref"]
        runtime[2].update_item(
            Key={"PK": "session#session-a", "SK": f"msg#{reference}"},
            UpdateExpression="SET content = :content",
            ExpressionAttributeValues={":content": "Substituted after verification"},
        )
        return original(store, *args, **kwargs)

    monkeypatch.setattr(chat_model.ChatHistoryStore, "get_messages", replaced)
    response = await next_turn(client, token)
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "chat_authority_unavailable"


async def test_lease_replacement_during_message_read_blocks_delivery(mailbox, monkeypatch):
    client, runtime, token, _, _ = mailbox
    original = chat_model.ChatHistoryStore.get_messages

    def replaced(store, *args, **kwargs):
        result = original(store, *args, **kwargs)
        runtime[2].update_item(
            Key={"PK": "session#session-a", "SK": "header"},
            UpdateExpression="SET chatLease.generation = :generation",
            ExpressionAttributeValues={":generation": 2},
        )
        return result

    monkeypatch.setattr(chat_model.ChatHistoryStore, "get_messages", replaced)
    assert (await next_turn(client, token)).status_code == 404
