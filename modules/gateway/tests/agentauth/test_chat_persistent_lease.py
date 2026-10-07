"""Persistent lease is exclusive, renewable and bounded by the original grant."""

import hashlib
from dataclasses import replace

import pytest

from src.agentauth import chat_data_routes
from src.agentauth.chat_session_mailbox import ChatSessionMailbox
from tests.agentauth.test_chat_data_routes import admit, exchange
from tests.agentauth.test_chat_data_routes import client as client_fixture
from tests.agentauth.test_chat_data_routes import runtime as runtime_fixture
from tests.agentauth.test_chat_data_routes import store as store_fixture
from tests.agentauth.test_chat_data_routes import sts as sts_fixture
from tests.agentauth.test_chat_lease_handoff import admit_next, next_turn
from tests.agentauth.test_chat_turn_mailbox import _ready_followup_claim
from tests.agentauth.test_chat_turn_mailbox import mailbox as mailbox_fixture
from tests.agentauth.test_chat_user_turn import retained_input_table as retained_input_table_fixture

client = client_fixture
runtime = runtime_fixture
store = store_fixture
sts = sts_fixture
mailbox = mailbox_fixture
retained_input_table = retained_input_table_fixture


@pytest.fixture
def persistent_workload(runtime, monkeypatch):
    workloads = runtime[1].workloads
    original_verify = workloads.verify
    original_bound = workloads.verify_bound
    session_hash = hashlib.sha256(b"session-a").hexdigest()
    monkeypatch.setattr(workloads, "verify", lambda *args, **kwargs: replace(original_verify(*args, **kwargs), session_hash=session_hash))
    monkeypatch.setattr(workloads, "verify_bound", lambda *args, **kwargs: replace(original_bound(*args, **kwargs), session_hash=session_hash))


async def test_bound_persistent_lease_renews_without_widening_root(client, runtime, persistent_workload, monkeypatch):
    table = runtime[2]
    now = runtime[-1]
    mailbox = ChatSessionMailbox(table)
    mailbox.select_mode(session_id="session-a", owner=("tenant", "team", "human"), mode="persistent", now=now)
    admitted = await admit(client, runtime)
    assert admitted.status_code == 200, admitted.text
    first = await exchange(client)
    assert first.status_code == 200, first.text
    assert first.json()["session_mode"] == "persistent"
    lease = table.get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]["chatLease"]
    assert lease["run_id"] == "run-a" and lease["sandbox_uid"] == "chat-pod"
    assert lease["expires_at"] == min(now + 90, runtime[0].launches.load("run-a").expires_at)
    assert mailbox.state(session_id="session-a", owner=("tenant", "team", "human"), now=now)["health"] == "active"
    monkeypatch.setattr(chat_data_routes, "clock", lambda: now + 20)
    refreshed = await exchange(client)
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["lease_generation"] == first.json()["lease_generation"]
    assert refreshed.json()["session_mode"] == "persistent"
    assert table.get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]["chatLease"]["expires_at"] == min(
        now + 110, runtime[0].launches.load("run-a").expires_at
    )
    assert mailbox.select_mode(session_id="session-a", owner=("tenant", "team", "human"), mode="ephemeral", now=now + 20) == "persistent"
    assert mailbox.state(session_id="session-a", owner=("tenant", "team", "human"), now=now + 20)["pending_mode"] == "ephemeral"


async def test_expired_lease_remains_recovering_after_mode_switch_until_cleanup(client, runtime, persistent_workload, monkeypatch):
    now = runtime[-1]
    mailbox = ChatSessionMailbox(runtime[2])
    owner = ("tenant", "team", "human")
    mailbox.select_mode(session_id="session-a", owner=owner, mode="persistent", now=now)
    assert (await admit(client, runtime)).status_code == 200
    assert mailbox.state(session_id="session-a", owner=owner, now=now + 90)["health"] == "recovering"
    mailbox.select_mode(session_id="session-a", owner=owner, mode="ephemeral", now=now + 90)
    assert mailbox.state(session_id="session-a", owner=owner, now=now + 90) == {
        "mode": "persistent",
        "sequence": 0,
        "health": "recovering",
        "pending_mode": "ephemeral",
    }
    monkeypatch.setattr(chat_data_routes, "clock", lambda: now + 90)
    assert (await exchange(client)).status_code == 404


async def test_persistent_lease_refuses_replaced_pod(client, runtime, persistent_workload):
    now = runtime[-1]
    table = runtime[2]
    ChatSessionMailbox(table).select_mode(session_id="session-a", owner=("tenant", "team", "human"), mode="persistent", now=now)
    assert (await admit(client, runtime)).status_code == 200
    table.update_item(
        Key={"PK": "session#session-a", "SK": "header"},
        UpdateExpression="SET chatLease.sandbox_uid = :uid",
        ExpressionAttributeValues={":uid": "replaced-pod"},
    )
    assert (await exchange(client)).status_code == 404


async def test_second_pod_cannot_claim_live_persistent_session(client, runtime, persistent_workload):
    now = runtime[-1]
    table = runtime[2]
    ChatSessionMailbox(table).select_mode(session_id="session-a", owner=("tenant", "team", "human"), mode="persistent", now=now)
    assert (await admit(client, runtime)).status_code == 200
    candidate = next_turn(runtime)
    assert (await admit_next(client, runtime, candidate)).status_code == 404
    assert runtime[0].launches.store._read("CHAT-LAUNCH#run-b", "LAUNCH") is None
    assert table.get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]["chatLease"]["sandbox_uid"] == "chat-pod"


async def test_sandbox_reads_server_mode_and_health_only_for_its_bound_session(client, runtime, persistent_workload):
    now = runtime[-1]
    table = runtime[2]
    ChatSessionMailbox(table).select_mode(session_id="session-a", owner=("tenant", "team", "human"), mode="persistent", now=now)
    assert (await admit(client, runtime)).status_code == 200
    token = (await exchange(client)).json()["capability"]
    headers = {"Authorization": f"Bearer {token}"}
    path = "/v1/chat/data/session/state"
    body = {"run_id": "run-a", "session_id": "session-a"}
    response = await client.post(path, json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"mode": "persistent", "sequence": 0, "health": "active"}
    assert response.headers["cache-control"] == "no-store"
    assert (await client.post(path, json={**body, "session_id": "other-session"}, headers=headers)).status_code == 404
    assert (await client.post(path, json={**body, "ownerUserId": "another"}, headers=headers)).status_code == 422
    table.update_item(
        Key={"PK": "session#session-a", "SK": "header"}, UpdateExpression="SET ownerUserId = :owner", ExpressionAttributeValues={":owner": "another"}
    )
    assert (await client.post(path, json=body, headers=headers)).status_code == 404


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_session_mailbox_route_requires_current_pod_lease_and_scope(mailbox):
    client, runtime, token = await _ready_followup_claim(mailbox)
    body = {"run_id": "run-user", "session_id": "session-a", "after": 1}
    response = await client.post("/v1/chat/data/session/next", json=body, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "turn": {"sequence": 2, "turn_id": "run-followup", "message": "Follow up"},
        "run_id": "run-user",
        "session_id": "session-a",
        "lease_generation": 1,
    }
    assert (
        await client.post("/v1/chat/data/session/next", json={**body, "session_id": "session-b"}, headers={"Authorization": f"Bearer {token}"})
    ).status_code == 404
    runtime[2].update_item(
        Key={"PK": "session#session-a", "SK": "header"},
        UpdateExpression="SET chatLease.generation = :generation",
        ExpressionAttributeValues={":generation": 2},
    )
    assert (await client.post("/v1/chat/data/session/next", json=body, headers={"Authorization": f"Bearer {token}"})).status_code == 404
