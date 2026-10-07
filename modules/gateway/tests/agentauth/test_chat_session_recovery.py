"""Persistent restart reconciliation fences authority before uncertain pod cleanup."""

import pytest

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.chat_session_mailbox import AcceptedTurn, ChatSessionMailbox
from tests.agentauth import test_chat_persistent_completion as completion
from tests.agentauth.test_chat_sandbox_exit import supervisor
from tests.agentauth.test_chat_terminal_delivery import replay
from tests.agentauth.test_work_producer import proof

client = completion.client
runtime = completion.runtime
store = completion.store
sts = completion.sts
retained_input_table = completion.retained_input_table
mailbox = completion.mailbox
transport = completion.transport
consumer = completion.consumer
registered_owner = completion.registered_owner


async def call(mailbox, operation, run_id="run-user", **extra):
    client, runtime, _, state, _ = mailbox
    body = {"run_id": run_id, "envelope_digest": runtime[1].store._read(f"INVOCATION#{run_id}", "DISPATCH")["envelope_digest"]["S"], **extra}
    if operation not in {"resume", "reserve"}:
        body.update(pod_name=state["pod"].name, pod_uid=state["pod"].uid)
    return await client.post(f"/internal/v1/agent/chat/data/{operation}", json=body, headers={"X-Adp-Producer-Proof": proof(envelope_digest(body))})


def expire(mailbox):
    runtime = mailbox[1]
    runtime[2].update_item(
        Key={"PK": "session#session-a", "SK": "header"},
        UpdateExpression="SET chatLease.expires_at = :expired",
        ExpressionAttributeValues={":expired": runtime[-1] - 1},
    )


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_live_restart_preserves_binding_and_expired_restart_fences_it_once(mailbox, sts, monkeypatch):
    client, runtime, token, state, _ = mailbox
    supervisor(sts, monkeypatch)
    healthy = await call(mailbox, "resume")
    assert healthy.status_code == 200 and "recovery_required" not in healthy.json()
    expire(mailbox)
    recovered = await call(mailbox, "resume")
    assert recovered.status_code == 200, recovered.text
    assert recovered.json() == {**healthy.json(), "recovery_required": True}
    header = runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]
    assert header["chatLease"]["expires_at"] == 1 and header["sessionState"] == "recovering"
    assert header["chatLease"]["sandbox_uid"] == state["pod"].uid
    assert (await call(mailbox, "resume")).json() == recovered.json()
    assert runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"] == header
    denied = await client.post(
        "/v1/chat/data/turn/result",
        json={"run_id": "run-user", "session_id": "session-a", "outcome": "failed"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert denied.status_code == 404


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_lost_pod_preserves_history_and_unblocks_only_after_owner_delivery(mailbox, sts, monkeypatch, transport, consumer):
    _, runtime, _, state, _ = mailbox
    supervisor(sts, monkeypatch)
    completion.fixtures.prepare(runtime, message_id="run-followup", message="Follow up", task_id="task-b")
    ChatSessionMailbox(runtime[2]).accept(
        session_id="session-a", owner=("tenant", "team", "human"), turn=AcceptedTurn(turn_id="run-followup", message="Follow up"), now=runtime[-1]
    )
    assert (await call(mailbox, "resume", "run-followup")).json()["state"] == "session_pending"
    expire(mailbox)
    resumed = await call(mailbox, "resume", "run-followup")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"] == "session_recovery"
    assert resumed.json()["recovery"]["run_id"] == "run-user"
    assert (await call(mailbox, "exit")).json()["terminated"] is False
    state["removed"] = True
    assert (await call(mailbox, "exit")).json()["terminated"] is True
    teardown = runtime[1].store._read("CHAT-LAUNCH#run-user", "TEARDOWN")
    assert teardown["exit_evidence"] == {"S": "lease_fenced_absence"}
    terminal = await call(mailbox, "finalize")
    assert terminal.status_code == 200, terminal.text
    assert terminal.json()["outcome"] == "interrupted"
    assert terminal.json()["automatic_replay_permitted"] is False
    event = replay(runtime)["events"][0]
    assert event["payload"]["status"] == "interrupted"
    assert event["payload"]["automatic_replay_permitted"] is False
    assert (await call(mailbox, "complete")).status_code == 409
    assert (await call(mailbox, "resume", "run-followup")).json()["state"] == "session_recovery"
    consumer._process_response(completion.publication.payload(transport))
    assert (await call(mailbox, "complete")).status_code == 200
    available = await call(mailbox, "resume", "run-followup")
    assert available.status_code == 200 and available.json()["state"] == "unstarted", available.text
    assert (await call(mailbox, "reserve", "run-followup", image_digest=state["pod"].image_digest)).status_code == 200
    history = runtime[2].get_item(Key={"PK": "session#session-a", "SK": "turn#run-user"})["Item"]
    assert history["status"] == "interrupted"
    assert runtime[2].get_item(Key={"PK": "session#session-a", "SK": f"msg#{history['ref']}"})["Item"]["role"] == "user"
    assert runtime[1].store._read("CHAT-LAUNCH#run-followup", "LAUNCH") is None


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_heartbeat_racing_recovery_cannot_be_overwritten(mailbox, sts, monkeypatch):
    _, runtime, _, _, _ = mailbox
    supervisor(sts, monkeypatch)
    expire(mailbox)
    original = runtime[1].store.client.transact_write_items

    def transact(**request):
        runtime[2].update_item(
            Key={"PK": "session#session-a", "SK": "header"},
            UpdateExpression="SET chatLease.expires_at = :renewed",
            ExpressionAttributeValues={":renewed": runtime[-1] + 90},
        )
        return original(**request)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", transact)
    assert (await call(mailbox, "resume")).status_code == 409
    assert "chat_session_lost" not in runtime[1].store._read("TENANT#tenant", "EXEC#run-user")
    assert runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]["chatLease"]["expires_at"] == runtime[-1] + 90


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_expired_completed_turn_keeps_its_committed_output(mailbox, sts, monkeypatch, transport, consumer):
    client, runtime, token, state, _ = mailbox
    supervisor(sts, monkeypatch)
    recorded = await client.post(
        "/v1/chat/data/turn/result",
        json={"run_id": "run-user", "session_id": "session-a", "outcome": "failed"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert recorded.status_code == 200
    assert (await completion.commit(client, runtime, state)).status_code == 409
    consumer._process_response(completion.publication.payload(transport))
    committed = await completion.commit(client, runtime, state)
    assert committed.status_code == 200
    expire(mailbox)
    assert (await call(mailbox, "resume")).json()["recovery_required"] is True
    state["removed"] = True
    assert (await call(mailbox, "exit")).json()["terminated"] is True
    replay = await call(mailbox, "finalize")
    assert replay.status_code == 200 and replay.json() == committed.json()["terminal"], replay.text
    assert (await call(mailbox, "complete")).status_code == 200
    assert transport.client.send_message.call_count == 1
    assert consumer.ws_router._client.post_to_connection.call_count == 1
