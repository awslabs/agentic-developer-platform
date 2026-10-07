"""Persistent terminal delivery uses the owner transport without destroying the pod."""

from types import SimpleNamespace

import pytest

from src.agentauth import chat_data_routes
from src.agentauth.bootstrap import envelope_digest
from src.agentauth.chat_session_cleanup import publish_session_cleanup, request_session_end
from src.agentauth.chat_session_mailbox import AcceptedTurn, ChatSessionMailbox
from tests.agentauth import test_chat_terminal_publication as publication
from tests.agentauth import test_chat_turn_mailbox as fixtures
from tests.agentauth.test_chat_delivery import ROUTING
from tests.agentauth.test_chat_sandbox_exit import supervisor
from tests.agentauth.test_work_producer import proof

client = fixtures.client
runtime = fixtures.runtime
store = fixtures.store
sts = fixtures.sts
retained_input_table = fixtures.retained_input_table
mailbox = fixtures.mailbox
transport = publication.transport
consumer = publication.consumer


@pytest.fixture(autouse=True)
def registered_owner(monkeypatch, transport):
    prepare = fixtures.prepare
    monkeypatch.setattr(fixtures, "prepare", lambda runtime, **changes: prepare(runtime, **{**ROUTING, **changes}))


async def commit(client, runtime, state, **changes):
    body = {
        "run_id": "run-user",
        "envelope_digest": runtime[1].store._read("INVOCATION#run-user", "DISPATCH")["envelope_digest"]["S"],
        "pod_name": state["pod"].name,
        "pod_uid": state["pod"].uid,
        **changes,
    }
    return await client.post("/internal/v1/agent/chat/data/session/commit", json=body, headers={"X-Adp-Producer-Proof": proof(envelope_digest(body))})


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_persistent_result_reaches_owner_before_followup_can_advance(mailbox, sts, monkeypatch, transport, consumer):
    client, runtime, token, state, _ = mailbox
    supervisor(sts, monkeypatch)
    assert (await commit(client, runtime, state)).status_code == 409
    assert not transport.client.send_message.called
    turn = await fixtures.next_turn(client, token)
    reply = await client.post(
        "/v1/chat/data/history/append",
        json={
            "run_id": "run-user",
            "session_id": "session-a",
            "idempotency_key": "reply",
            "expected_version": 1,
            "content": "First owner reply",
            "tokens": 4,
            "user_turn_id": turn.json()["turn"]["ref"],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert reply.status_code == 200, reply.text
    result = await client.post(
        "/v1/chat/data/turn/result",
        json={
            "run_id": "run-user",
            "session_id": "session-a",
            "outcome": "completed",
            "message_id": reply.json()["message_id"],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert result.status_code == 200, result.text
    awaiting_delivery = await commit(client, runtime, state)
    assert awaiting_delivery.status_code == 409, awaiting_delivery.text
    assert transport.client.send_message.call_count == 1
    fixtures.prepare(runtime, message_id="run-followup", message="Follow up", task_id="task-b")
    ChatSessionMailbox(runtime[2]).accept(
        session_id="session-a", owner=("tenant", "team", "human"), turn=AcceptedTurn(turn_id="run-followup", message="Follow up"), now=runtime[-1]
    )
    next_body = {
        "run_id": "run-followup",
        "envelope_digest": runtime[1].store._read("INVOCATION#run-followup", "DISPATCH")["envelope_digest"]["S"],
        "image_digest": state["pod"].image_digest,
    }
    reserved = await client.post(
        "/internal/v1/agent/chat/data/reserve", json=next_body, headers={"X-Adp-Producer-Proof": proof(envelope_digest(next_body))}
    )
    assert reserved.status_code == 200, reserved.text
    assert reserved.json()["state"] == "session_pending"
    for _duplicate in range(2):
        repeated = await client.post(
            "/internal/v1/agent/chat/data/reserve", json=next_body, headers={"X-Adp-Producer-Proof": proof(envelope_digest(next_body))}
        )
        assert repeated.status_code == 200 and repeated.json() == reserved.json()
    assert runtime[1].store._read("CHAT-LAUNCH#run-followup", "LAUNCH") is None
    ticks = iter([0, 21])
    monkeypatch.setattr(chat_data_routes, "time", SimpleNamespace(monotonic=lambda: next(ticks, 21)))
    waiting = await fixtures._read_followup_claim(client, token)
    assert waiting.status_code == 200 and waiting.json()["turn"] is None
    payload = publication.payload(transport)
    consumer._process_response(payload)
    consumer._process_response(payload)
    delivered = await commit(client, runtime, state)
    assert delivered.status_code == 200, delivered.text
    assert delivered.json()["terminal"]["outcome"] == "completed"
    assert delivered.json()["completion"]["processing_lock_released"] is True
    assert (await commit(client, runtime, state)).json() == delivered.json()
    assert consumer.ws_router._client.post_to_connection.call_count == 1
    assert transport.client.send_message.call_count == 1
    assert not state["exited"] and not state["removed"]
    assert runtime[1].store._read("CHAT-LAUNCH#run-user", "TEARDOWN") is None
    following = await fixtures._read_followup_claim(client, token)
    assert following.status_code == 200 and following.json()["turn"]["turn_id"] == "run-followup"
    admitted = await client.post(
        "/v1/chat/data/session/admit",
        json={"run_id": "run-user", "session_id": "session-a", "after": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert admitted.status_code == 200, admitted.text
    next_body.pop("image_digest")
    resumed = await client.post(
        "/internal/v1/agent/chat/data/resume", json=next_body, headers={"X-Adp-Producer-Proof": proof(envelope_digest(next_body))}
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"] == "admitted"
    assert resumed.json()["session_run_id"] == "run-user"
    assert resumed.json()["sandbox_uid"] == state["pod"].uid
    assert resumed.json()["pod_name"] == state["pod"].name
    for _duplicate in range(2):
        repeated = await client.post(
            "/internal/v1/agent/chat/data/resume", json=next_body, headers={"X-Adp-Producer-Proof": proof(envelope_digest(next_body))}
        )
        assert repeated.status_code == 200 and repeated.json() == resumed.json()
    replayed = await commit(client, runtime, state)
    assert replayed.status_code == 200 and replayed.json() == delivered.json()
    assert consumer.ws_router._client.post_to_connection.call_count == 1
    assert transport.client.send_message.call_count == 1
    assert runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]["chatLease"]["generation"] == 2
    cleanup = runtime[2].get_item(Key={"PK": "chat-notifications", "SK": "cleanup#run-followup"}, ConsistentRead=True).get("Item")
    assert cleanup is not None
    assert cleanup["sandboxUid"] == state["pod"].uid and cleanup["leaseGeneration"] == 2
    assert runtime[2].get_item(Key={"PK": "chat-notifications", "SK": "cleanup#run-user"}, ConsistentRead=True).get("Item") is None
    now = runtime[-1]
    request_session_end(runtime[2], session_id="session-a", owner=("tenant", "team", "human"), now=now, reason="user")
    assert publish_session_cleanup(runtime[1], runtime[0], cleanup, now=now, transport=lambda: (transport.client, transport.url)) == "notified"
    notice = publication.payload(transport)
    assert notice["message_id"] == "run-followup" and notice["task_id"] == "task-b"
    assert runtime[1].store._read("TENANT#tenant", "EXEC#run-followup")["chat_session_lost"]["M"] == {
        "run_id": {"S": "run-followup"},
        "sandbox_uid": {"S": state["pod"].uid},
        "lease_generation": {"N": "2"},
    }
    assert not runtime[1].current(runtime[0].launches.load("run-followup"), now)


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_persistent_commit_requires_supervisor_and_original_pod(mailbox, sts, monkeypatch):
    client, runtime, _, state, _ = mailbox
    assert (await commit(client, runtime, state)).status_code == 403
    supervisor(sts, monkeypatch)
    assert (await commit(client, runtime, state, pod_uid="foreign-pod")).status_code == 404


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
@pytest.mark.parametrize("proof_mode", ["missing", "malformed", "changed-body", "invalid-signature"])
async def test_persistent_commit_rejects_invalid_proof_before_observing_pod(mailbox, sts, monkeypatch, proof_mode):
    client, runtime, _, state, _ = mailbox
    supervisor(sts, monkeypatch)
    body = {
        "run_id": "run-user",
        "envelope_digest": runtime[1].store._read("INVOCATION#run-user", "DISPATCH")["envelope_digest"]["S"],
        "pod_name": state["pod"].name,
        "pod_uid": state["pod"].uid,
    }
    headers = {"X-Adp-Producer-Proof": proof(envelope_digest(body))}
    if proof_mode == "missing":
        headers = {}
    elif proof_mode == "malformed":
        headers = {"X-Adp-Producer-Proof": "not-a-signed-proof"}
    elif proof_mode == "changed-body":
        body["pod_uid"] = "foreign-pod"
    else:
        sts["status"] = 403
    observed = []
    monkeypatch.setattr(runtime[1].workloads, "has_exited", lambda **kwargs: observed.append(kwargs) or False)
    response = await client.post("/internal/v1/agent/chat/data/session/commit", json=body, headers=headers)
    assert response.status_code == 403, response.text
    assert not observed
