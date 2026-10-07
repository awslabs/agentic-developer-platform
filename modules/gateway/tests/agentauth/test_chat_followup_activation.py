"""Fresh protected turns advance on one pod without reusing the preceding grant."""

from types import SimpleNamespace

import pytest
from starlette.concurrency import run_in_threadpool

from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.agentauth.chat_history_store import ChatHistoryStore
from src.agentauth.chat_model_journal import ChatModelJournal
from src.agentauth.chat_session_mailbox import AcceptedTurn, ChatSessionMailbox
from src.agentauth.chat_turn_finalization import ChatTurnFinalizer
from tests.agentauth import test_chat_turn_mailbox as fixtures

client = fixtures.client
runtime = fixtures.runtime
store = fixtures.store
sts = fixtures.sts
retained_input_table = fixtures.retained_input_table
mailbox = fixtures.mailbox


async def ready(mailbox):
    client, runtime, token, state, _ = mailbox
    result = await client.post(
        "/v1/chat/data/turn/result",
        json={"run_id": "run-user", "session_id": "session-a", "outcome": "failed"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert result.status_code == 200, result.text
    authority = runtime[1]
    writer = ChatTurnFinalizer(authority, ChatHistoryStore(runtime[2], runtime[0]))
    body = SimpleNamespace(
        run_id="run-user",
        pod_name=state["pod"].name,
        pod_uid=state["pod"].uid,
        envelope_digest=authority.store._read("INVOCATION#run-user", "DISPATCH")["envelope_digest"]["S"],
    )
    terminal = await run_in_threadpool(writer.finalize, body, now=runtime[-1], persistent=True)
    assert await run_in_threadpool(writer.finalize, body, now=runtime[-1], persistent=True) == terminal
    assert not state["exited"] and not state["removed"]
    fixtures.prepare(runtime, message_id="run-followup", message="Follow up", team_id="team")
    ChatSessionMailbox(runtime[2]).accept(
        session_id="session-a",
        owner=("tenant", "team", "human"),
        turn=AcceptedTurn(turn_id="run-followup", message="Follow up"),
        now=runtime[-1],
    )
    return client, runtime, token


async def activate(client, token, **changes):
    return await client.post(
        "/v1/chat/data/session/admit",
        json={"run_id": "run-user", "session_id": "session-a", "after": 1, **changes},
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_fresh_followup_executes_on_original_pod_with_new_grant_and_fence(mailbox):
    client, runtime, token = await ready(mailbox)
    store = runtime[1].store
    binding = store._read("POD#chat-pod", "BINDING")
    response = await activate(client, token)
    assert response.status_code == 200, response.text
    following = response.json()
    assert following["run_id"] == "run-followup" and following["lease_generation"] == 2
    assert following["session_mode"] == "persistent"
    assert store._read("POD#chat-pod", "BINDING") == binding
    first_launch = runtime[0].launches.load("run-user")
    next_launch = runtime[0].launches.load("run-followup")
    assert next_launch.grant_id != first_launch.grant_id
    assert next_launch.session_run_id == "run-user" and next_launch.sandbox_uid == first_launch.sandbox_uid
    assert store._read("TENANT#tenant", "EXEC#run-user")["status"] == {"S": "completed"}
    assert store._read("TENANT#tenant", "EXEC#run-followup")["status"] == {"S": "active"}
    turn = await client.post(
        "/v1/chat/turn/next",
        json={"run_id": "run-followup", "session_id": "session-a"},
        headers={"Authorization": f"Bearer {following['capability']}"},
    )
    assert turn.status_code == 200, turn.text
    assert turn.json()["turn"]["message"]["content"] == "Follow up"
    assert turn.json()["session_sequence"] == 2
    refreshed = await fixtures.exchange(client, **{"X-Adp-Workload-Token": "sandbox-token"})
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["run_id"] == "run-followup"
    result_body = {"run_id": "run-followup", "session_id": "session-a", "outcome": "failed"}
    assert (await client.post("/v1/chat/data/turn/result", json=result_body, headers={"Authorization": f"Bearer {token}"})).status_code == 404
    result = await client.post("/v1/chat/data/turn/result", json=result_body, headers={"Authorization": f"Bearer {following['capability']}"})
    assert result.status_code == 200, result.text
    writer = ChatTurnFinalizer(runtime[1], ChatHistoryStore(runtime[2], runtime[0]))
    body = SimpleNamespace(
        run_id="run-followup",
        pod_name=mailbox[3]["pod"].name,
        pod_uid="chat-pod",
        envelope_digest=store._read("INVOCATION#run-followup", "DISPATCH")["envelope_digest"]["S"],
    )
    await run_in_threadpool(writer.finalize, body, now=runtime[-1], persistent=True)
    fixtures.prepare(runtime, message_id="run-third", message="Third turn", team_id="team")
    ChatSessionMailbox(runtime[2]).accept(
        session_id="session-a", owner=("tenant", "team", "human"), turn=AcceptedTurn(turn_id="run-third", message="Third turn"), now=runtime[-1]
    )
    third = await client.post(
        "/v1/chat/data/session/admit",
        json={"run_id": "run-followup", "session_id": "session-a", "after": 2},
        headers={"Authorization": f"Bearer {following['capability']}"},
    )
    assert third.status_code == 200, third.text
    assert third.json()["lease_generation"] == 3 and third.json()["run_id"] == "run-third"
    assert runtime[0].launches.load("run-third").session_run_id == "run-user"
    assert store._read("POD#chat-pod", "BINDING") == binding
    cleanup = runtime[2].get_item(Key={"PK": "chat-notifications", "SK": "cleanup#run-third"}, ConsistentRead=True).get("Item")
    assert cleanup is not None
    assert cleanup["sandboxUid"] == "chat-pod" and cleanup["leaseGeneration"] == 3
    assert (cleanup["tenantId"], cleanup["teamId"], cleanup["ownerUserId"], cleanup["sessionId"]) == ("tenant", "team", "human", "session-a")
    assert cleanup["ttl"] >= runtime[-1]
    for previous_run in ("run-user", "run-followup"):
        assert runtime[2].get_item(Key={"PK": "chat-notifications", "SK": f"cleanup#{previous_run}"}, ConsistentRead=True).get("Item") is None


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
@pytest.mark.parametrize("change", [{"session_id": "session-b"}, {"run_id": "run-other"}, {"after": 0}, {"after": 2}])
async def test_followup_activation_cannot_change_scope_or_skip_sequence(mailbox, change):
    client, runtime, token = await ready(mailbox)
    assert (await activate(client, token, **change)).status_code == 404
    assert runtime[1].store._read("CHAT-LAUNCH#run-followup", "LAUNCH") is None


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_sealed_turn_cannot_read_or_write_data_while_waiting_for_followup(mailbox):
    client, runtime, token = await ready(mailbox)
    headers = {"Authorization": f"Bearer {token}"}
    for path, body in (
        ("history/read", {"run_id": "run-user", "session_id": "session-a", "limit": 10}),
        (
            "history/append",
            {
                "run_id": "run-user",
                "session_id": "session-a",
                "idempotency_key": "late",
                "expected_version": 1,
                "content": "late output",
                "tokens": 3,
                "user_turn_id": "user_old",
            },
        ),
    ):
        response = await client.post(f"/v1/chat/data/{path}", json=body, headers=headers)
        assert response.status_code == 404, response.text
    assert (await fixtures._read_followup_claim(client, token)).status_code == 200
    with pytest.raises(ChatAuthorizationRefusedError):
        await run_in_threadpool(
            ChatModelJournal(runtime[1]).claim,
            runtime[0].launches.load("run-user"),
            operation_id="late-model",
            request_digest="a" * 64,
            model_id="approved-model",
            now=runtime[-1],
        )


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_result_seal_blocks_model_dispatch_before_supervisor_finalization(mailbox):
    client, runtime, token, _, _ = mailbox
    launch = runtime[0].launches.load("run-user")
    journal = ChatModelJournal(runtime[1])
    operation = await run_in_threadpool(
        journal.claim, launch, operation_id="prepared", request_digest="a" * 64, model_id="approved-model", now=runtime[-1]
    )
    result = await client.post(
        "/v1/chat/data/turn/result",
        json={"run_id": "run-user", "session_id": "session-a", "outcome": "failed"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert result.status_code == 200, result.text
    turn = runtime[2].get_item(Key={"PK": "session#session-a", "SK": "turn#run-user"})["Item"]
    assert turn["status"] == "accepted" and "terminal_result" not in turn
    with pytest.raises(ChatAuthorizationRefusedError):
        await run_in_threadpool(journal.claim, launch, operation_id="late", request_digest="b" * 64, model_id="approved-model", now=runtime[-1])
    assert await run_in_threadpool(journal.transition, launch, operation, now=runtime[-1], authorize=True, status="running") is None


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
@pytest.mark.parametrize("change", ["another_user", "another_tenant", "revoked_followup", "replaced_lease", "foreign_pod"])
async def test_followup_activation_rechecks_owner_grant_and_pod(mailbox, change):
    client, runtime, token = await ready(mailbox)
    if change in {"another_user", "another_tenant", "replaced_lease"}:
        field, value = {
            "another_user": ("ownerUserId", "human-a2"),
            "another_tenant": ("tenantId", "tenant-b"),
            "replaced_lease": ("chatLease.generation", 2),
        }[change]
        runtime[2].update_item(
            Key={"PK": "session#session-a", "SK": "header"}, UpdateExpression=f"SET {field} = :value", ExpressionAttributeValues={":value": value}
        )
    elif change == "revoked_followup":
        runtime[1].store.client.update_item(
            TableName=runtime[1].store.table,
            Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-followup"}},
            UpdateExpression="SET #status = :revoked",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":revoked": {"S": "revoked"}},
        )
    else:
        client.headers["X-Adp-Workload-Token"] = "other-pod"
    assert (await activate(client, token)).status_code == 404
    assert runtime[1].store._read("CHAT-LAUNCH#run-followup", "LAUNCH") is None


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
@pytest.mark.parametrize("change", ["lease", "input", "pod_binding", "abort", "cleanup_owner", "cleanup_missing", "cleanup_collision"])
async def test_followup_activation_transaction_fences_racing_mutations(mailbox, monkeypatch, change):
    client, runtime, token = await ready(mailbox)
    store, table = runtime[1].store, runtime[2]
    transact = store.client.transact_write_items

    def race(**kwargs):
        activating = any(item.get("Put", {}).get("Item", {}).get("pk") == {"S": "CHAT-LAUNCH#run-followup"} for item in kwargs["TransactItems"])
        if activating:
            if change == "lease":
                table.update_item(
                    Key={"PK": "session#session-a", "SK": "header"},
                    UpdateExpression="SET chatLease.generation = :next",
                    ExpressionAttributeValues={":next": 9},
                )
            elif change == "input":
                table.delete_item(Key={"PK": "chat-input#run-followup", "SK": "input"})
            elif change == "pod_binding":
                store.client.update_item(
                    TableName=store.table,
                    Key={"pk": {"S": "POD#chat-pod"}, "sk": {"S": "BINDING"}},
                    UpdateExpression="SET invocation_id = :other",
                    ExpressionAttributeValues={":other": {"S": "run-other"}},
                )
            elif change == "abort":
                store.client.update_item(
                    TableName=store.table,
                    Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-followup"}},
                    UpdateExpression="SET abort_command_id = :abort",
                    ExpressionAttributeValues={":abort": {"S": "cancel"}},
                )
            elif change == "cleanup_owner":
                table.update_item(
                    Key={"PK": "chat-notifications", "SK": "cleanup#run-user"},
                    UpdateExpression="SET ownerUserId = :other",
                    ExpressionAttributeValues={":other": "intruder"},
                )
            elif change == "cleanup_missing":
                table.delete_item(Key={"PK": "chat-notifications", "SK": "cleanup#run-user"})
            else:
                table.put_item(Item={"PK": "chat-notifications", "SK": "cleanup#run-followup", "ownerUserId": "intruder"})
        return transact(**kwargs)

    monkeypatch.setattr(store.client, "transact_write_items", race)
    response = await activate(client, token)
    assert response.status_code == 409, response.text
    assert store._read("CHAT-LAUNCH#run-followup", "LAUNCH") is None
    assert table.get_item(Key={"PK": "session#session-a", "SK": "turn#run-followup"}).get("Item") is None
    assert store._read("TENANT#tenant", "EXEC#run-followup")["status"] == {"S": "pending"}
    cleanup = table.get_item(Key={"PK": "chat-notifications", "SK": "cleanup#run-followup"}, ConsistentRead=True).get("Item")
    expected_cleanup = (
        {"PK": "chat-notifications", "SK": "cleanup#run-followup", "ownerUserId": "intruder"} if change == "cleanup_collision" else None
    )
    assert cleanup == expected_cleanup
    if change != "cleanup_missing":
        assert table.get_item(Key={"PK": "chat-notifications", "SK": "cleanup#run-user"}, ConsistentRead=True).get("Item") is not None


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
@pytest.mark.parametrize("change", ["another_user", "another_tenant", "revoked_root", "replaced_lease", "foreign_pod"])
async def test_followup_result_cannot_outlive_its_owner_root_or_lease(mailbox, change):
    client, runtime, token = await ready(mailbox)
    admitted = await activate(client, token)
    assert admitted.status_code == 200, admitted.text
    if change in {"another_user", "another_tenant", "replaced_lease"}:
        field, value = {
            "another_user": ("ownerUserId", "human-a2"),
            "another_tenant": ("tenantId", "tenant-b"),
            "replaced_lease": ("chatLease.generation", 3),
        }[change]
        runtime[2].update_item(
            Key={"PK": "session#session-a", "SK": "header"}, UpdateExpression=f"SET {field} = :value", ExpressionAttributeValues={":value": value}
        )
    elif change == "revoked_root":
        runtime[1].store.client.update_item(
            TableName=runtime[1].store.table,
            Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-followup"}},
            UpdateExpression="SET #status = :revoked",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":revoked": {"S": "revoked"}},
        )
    else:
        client.headers["X-Adp-Workload-Token"] = "foreign-pod"
    response = await client.post(
        "/v1/chat/data/turn/result",
        json={"run_id": "run-followup", "session_id": "session-a", "outcome": "failed"},
        headers={"Authorization": f"Bearer {admitted.json()['capability']}"},
    )
    assert response.status_code == 404, response.text
    turn = runtime[2].get_item(Key={"PK": "session#session-a", "SK": "turn#run-followup"})["Item"]
    assert "result_candidate" not in turn
