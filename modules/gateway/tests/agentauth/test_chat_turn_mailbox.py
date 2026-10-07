"""Scoped one-turn mailbox reads only a protected, accepted owner message."""

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.agentauth import chat_data_routes, chat_model
from src.agentauth.bootstrap import envelope_digest
from src.agentauth.chat_session_mailbox import AcceptedTurn, ChatSessionMailbox
from src.agentauth.external_roots import provision_root
from src.agentauth.workload import VerifiedPod, WorkloadRefusedError
from tests.agentauth.test_chat_data_routes import exchange
from tests.agentauth.test_chat_sandbox_exit import supervisor
from tests.agentauth.test_chat_user_turn import accept, prepare
from tests.agentauth.test_chat_user_turn import client as client_fixture
from tests.agentauth.test_chat_user_turn import retained_input_table as retained_input_table_fixture
from tests.agentauth.test_chat_user_turn import runtime as runtime_fixture
from tests.agentauth.test_chat_user_turn import store as store_fixture
from tests.agentauth.test_chat_user_turn import sts as sts_fixture
from tests.agentauth.test_work_producer import proof

client = client_fixture
runtime = runtime_fixture
store = store_fixture
sts = sts_fixture
retained_input_table = retained_input_table_fixture
PATH = "/v1/chat/turn/next"


@pytest.fixture
async def mailbox(client, runtime, monkeypatch, request):
    persistent = getattr(request, "param", "ephemeral") == "persistent"
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
        session_hash=hashlib.sha256(b"session-a").hexdigest() if persistent else None,
    )
    state = {"pod": sandbox, "exited": False, "removed": False}
    observation_scope = authority.workloads.observation_scope

    class Workloads:
        def verify(self, proof):
            if proof != "sandbox-token":
                raise WorkloadRefusedError("sandbox workload refused")
            return state["pod"]

        def verify_bound(self, *, name, uid):
            if name != sandbox.name or uid != sandbox.uid:
                raise WorkloadRefusedError("sandbox binding refused")
            return state["pod"]

        def has_exited(self, **kwargs):
            return state["exited"]

        def is_absent(self, **kwargs):
            return state["removed"]

        def approved_sandbox_image(self, image):
            return image == sandbox.image_digest

    Workloads.observation_scope = observation_scope
    monkeypatch.setattr(authority, "workloads", Workloads())
    client.headers["X-Adp-Workload-Token"] = "sandbox-token"
    if persistent:
        ChatSessionMailbox(runtime[2]).select_mode(session_id="session-a", owner=("tenant", "team", "human"), mode="persistent", now=runtime[-1])
    envelope = prepare(runtime)
    if persistent:
        ChatSessionMailbox(runtime[2]).accept(
            session_id="session-a",
            owner=("tenant", "team", "human"),
            turn=AcceptedTurn(turn_id="run-user", message=envelope["message"]),
            now=runtime[-1],
        )
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


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_initial_persistent_turn_exposes_its_bound_mailbox_cursor(mailbox):
    client, runtime, token, _, envelope = mailbox
    response = await next_turn(client, token)
    assert response.status_code == 200, response.text
    assert response.json()["session_sequence"] == 1
    assert response.json()["turn"]["message"]["content"] == envelope["message"]
    assert runtime[2].get_item(Key={"PK": "session#session-a", "SK": "mailbox-id#run-user"})["Item"]["sequence"] == 1


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
@pytest.mark.parametrize("change", ["missing", "other_owner", "other_message", "other_lease"])
async def test_initial_cursor_refuses_substituted_mailbox_or_lease(mailbox, change):
    client, runtime, token, _, _ = mailbox
    table = runtime[2]
    if change == "missing":
        table.delete_item(Key={"PK": "session#session-a", "SK": "mailbox-id#run-user"})
    elif change == "other_owner":
        table.update_item(
            Key={"PK": "session#session-a", "SK": "mailbox-id#run-user"},
            UpdateExpression="SET ownerUserId = :owner",
            ExpressionAttributeValues={":owner": "another"},
        )
    elif change == "other_message":
        table.update_item(
            Key={"PK": "session#session-a", "SK": "mailbox#00000001"},
            UpdateExpression="SET message = :message",
            ExpressionAttributeValues={":message": "replaced"},
        )
    else:
        table.update_item(
            Key={"PK": "session#session-a", "SK": "header"},
            UpdateExpression="SET chatLease.generation = :generation",
            ExpressionAttributeValues={":generation": 2},
        )
    assert (await next_turn(client, token)).status_code == 404


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
@pytest.mark.parametrize("outcome", ["failed", "completed"])
async def test_initial_result_commits_mailbox_status_once_under_bound_lease(mailbox, outcome):
    client, runtime, token, _, _ = mailbox
    payload = {"run_id": "run-user", "session_id": "session-a", "outcome": outcome}
    if outcome == "completed":
        reference = "user_" + hashlib.sha256(b"run-user").hexdigest()
        written = await client.post(
            "/v1/chat/data/history/append",
            json={
                "run_id": "run-user",
                "session_id": "session-a",
                "idempotency_key": "initial-reply",
                "expected_version": 1,
                "user_turn_id": reference,
                "content": "The answer",
                "tokens": 3,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert written.status_code == 200, written.text
        payload["message_id"] = written.json()["message_id"]
    response = await client.post("/v1/chat/data/turn/result", json=payload, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    key = {"PK": "session#session-a", "SK": "mailbox#00000001"}
    entry = runtime[2].get_item(Key=key, ConsistentRead=True)["Item"]
    assert entry["status"] == "result_recorded" and entry["resultRecordedAt"] == runtime[-1]
    assert (await client.post("/v1/chat/data/turn/result", json=payload, headers={"Authorization": f"Bearer {token}"})).json() == response.json()
    assert runtime[2].get_item(Key=key, ConsistentRead=True)["Item"] == entry


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
@pytest.mark.parametrize("change", ["foreign_owner", "foreign_turn", "replaced_lease"])
async def test_initial_result_cannot_commit_foreign_mailbox_or_replaced_lease(mailbox, change):
    client, runtime, token, _, _ = mailbox
    table = runtime[2]
    key = {"PK": "session#session-a", "SK": "mailbox#00000001"}
    if change == "foreign_owner":
        table.update_item(Key=key, UpdateExpression="SET ownerUserId = :other", ExpressionAttributeValues={":other": "another"})
    elif change == "foreign_turn":
        table.update_item(Key=key, UpdateExpression="SET turnId = :other", ExpressionAttributeValues={":other": "run-other"})
    else:
        table.update_item(
            Key={"PK": "session#session-a", "SK": "header"}, UpdateExpression="SET chatLease.generation = :new", ExpressionAttributeValues={":new": 2}
        )
    response = await client.post(
        "/v1/chat/data/turn/result",
        json={"run_id": "run-user", "session_id": "session-a", "outcome": "failed"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404
    assert "result_candidate" not in table.get_item(Key={"PK": "session#session-a", "SK": "turn#run-user"})["Item"]


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_mailbox_replacement_during_result_commit_cannot_record_candidate(mailbox, monkeypatch):
    client, runtime, token, _, _ = mailbox
    store = runtime[1].store
    original = store.client.transact_write_items

    def replace(**request):
        runtime[2].update_item(
            Key={"PK": "session#session-a", "SK": "mailbox#00000001"},
            UpdateExpression="SET #status = :changed",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":changed": "interrupted"},
        )
        return original(**request)

    monkeypatch.setattr(store.client, "transact_write_items", replace)
    response = await client.post(
        "/v1/chat/data/turn/result",
        json={"run_id": "run-user", "session_id": "session-a", "outcome": "failed"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 409, response.text
    assert "result_candidate" not in runtime[2].get_item(Key={"PK": "session#session-a", "SK": "turn#run-user"})["Item"]


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_mailbox_poll_stays_closed_after_unfinalized_initial_result(mailbox, monkeypatch):
    client, runtime, token, _, _ = mailbox
    store = ChatSessionMailbox(runtime[2])
    prepare(runtime, message_id="run-followup", message="Follow up", team_id="team")
    store.accept(
        session_id="session-a", owner=("tenant", "team", "human"), turn=AcceptedTurn(turn_id="run-followup", message="Follow up"), now=runtime[-1]
    )
    path = "/v1/chat/data/session/next"
    headers = {"Authorization": f"Bearer {token}"}
    body = {"run_id": "run-user", "session_id": "session-a", "after": 1}
    assert (await client.post(path, json=body, headers=headers)).status_code == 404
    assert (await client.post(path, json={**body, "after": 3}, headers=headers)).status_code == 404
    initial = await client.post(
        "/v1/chat/data/turn/result", json={"run_id": "run-user", "session_id": "session-a", "outcome": "failed"}, headers=headers
    )
    assert initial.status_code == 200, initial.text
    ticks = iter([0, 21])
    monkeypatch.setattr(chat_data_routes, "time", SimpleNamespace(monotonic=lambda: next(ticks, 21)))
    response = await client.post(path, json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["turn"] is None
    assert store.table.get_item(Key={"PK": "session#session-a", "SK": "mailbox#00000001"})["Item"]["status"] == "result_recorded"
    assert (await client.post(path, json=body, headers={**headers, "X-Adp-Workload-Token": "other-pod"})).status_code == 404


async def finalize_initial(client, runtime, sts, monkeypatch, state):
    supervisor(sts, monkeypatch)
    dispatch = runtime[1].store._read("INVOCATION#run-user", "DISPATCH")
    execution = runtime[1].store._read("TENANT#tenant", "EXEC#run-user")
    document = {
        "run_id": "run-user",
        "envelope_digest": dispatch["envelope_digest"]["S"],
        "pod_name": execution["pod_name"]["S"],
        "pod_uid": "chat-pod",
    }
    headers = {"X-Adp-Producer-Proof": proof(envelope_digest(document))}
    state["exited"] = True
    exit_result = await client.post("/internal/v1/agent/chat/data/exit", json=document, headers=headers)
    assert exit_result.status_code == 200, exit_result.text
    assert exit_result.json()["terminated"] is True
    state["removed"] = True
    teardown = await client.post("/internal/v1/agent/chat/data/teardown", json=document, headers=headers)
    assert teardown.status_code == 200, teardown.text
    assert teardown.json()["removed"] is True
    return document, headers


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
@pytest.mark.parametrize("outcome", ["completed", "failed", "interrupted", "cancelled"])
async def test_initial_terminal_finalizes_mailbox_atomically_after_teardown(mailbox, sts, monkeypatch, outcome):
    client, runtime, token, state, _ = mailbox
    headers = {"Authorization": f"Bearer {token}"}
    if outcome == "completed":
        response = await client.post(
            "/v1/chat/data/history/append",
            json={
                "run_id": "run-user",
                "session_id": "session-a",
                "idempotency_key": "assistant-a",
                "expected_version": 1,
                "user_turn_id": "user_" + hashlib.sha256(b"run-user").hexdigest(),
                "content": "Response",
                "tokens": 1,
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        message_id = response.json()["message_id"]
    else:
        message_id = None
    if outcome in {"completed", "failed", "cancelled"}:
        result = await client.post(
            "/v1/chat/data/turn/result",
            json={
                "run_id": "run-user",
                "session_id": "session-a",
                "outcome": "failed" if outcome == "cancelled" else outcome,
                **({"message_id": message_id} if message_id else {}),
            },
            headers=headers,
        )
        assert result.status_code == 200, result.text
    if outcome == "cancelled":
        runtime[1].store.client.update_item(
            TableName=runtime[1].store.table,
            Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-user"}},
            UpdateExpression="SET abort_command_id = :command, abort_requested_attempt = :attempt",
            ExpressionAttributeValues={":command": {"S": "cancel-a"}, ":attempt": {"N": "1"}},
        )
    document, supervisor_headers = await finalize_initial(client, runtime, sts, monkeypatch, state)
    before = runtime[2].get_item(Key={"PK": "session#session-a", "SK": "mailbox#00000001"})["Item"]
    assert before["status"] == ("queued" if outcome == "interrupted" else "result_recorded")
    finalized = await client.post("/internal/v1/agent/chat/data/finalize", json=document, headers=supervisor_headers)
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["outcome"] == outcome
    entry = runtime[2].get_item(Key={"PK": "session#session-a", "SK": "mailbox#00000001"})["Item"]
    assert entry["status"] == outcome and entry["finalizedAt"] == runtime[-1]
    assert (await client.post("/internal/v1/agent/chat/data/finalize", json=document, headers=supervisor_headers)).json() == finalized.json()
    assert runtime[2].get_item(Key={"PK": "session#session-a", "SK": "mailbox#00000001"})["Item"] == entry
    assert (
        await client.post(
            "/v1/chat/data/session/next",
            json={
                "run_id": "run-user",
                "session_id": "session-a",
                "after": 1,
            },
            headers=headers,
        )
    ).status_code == 404


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
@pytest.mark.parametrize("change", ["missing_receipt", "foreign_receipt", "wrong_digest", "wrong_outcome", "wrong_time", "oversized_message"])
async def test_duplicate_terminal_rejects_changed_mailbox(mailbox, sts, monkeypatch, change):
    client, runtime, token, state, _ = mailbox
    candidate = await client.post(
        "/v1/chat/data/turn/result",
        json={
            "run_id": "run-user",
            "session_id": "session-a",
            "outcome": "failed",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert candidate.status_code == 200, candidate.text
    document, headers = await finalize_initial(client, runtime, sts, monkeypatch, state)
    first = await client.post("/internal/v1/agent/chat/data/finalize", json=document, headers=headers)
    assert first.status_code == 200, first.text
    table = runtime[2]
    receipt_key = {"PK": "session#session-a", "SK": "mailbox-id#run-user"}
    entry_key = {"PK": "session#session-a", "SK": "mailbox#00000001"}
    if change == "missing_receipt":
        table.delete_item(Key=receipt_key)
    elif change == "foreign_receipt":
        table.update_item(Key=receipt_key, UpdateExpression="SET ownerUserId = :other", ExpressionAttributeValues={":other": "another"})
    elif change == "wrong_digest":
        table.update_item(Key=receipt_key, UpdateExpression="SET digest = :other", ExpressionAttributeValues={":other": "0" * 64})
    elif change == "wrong_outcome":
        table.update_item(
            Key=entry_key,
            UpdateExpression="SET #status = :other",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":other": "completed"},
        )
    elif change == "wrong_time":
        table.update_item(Key=entry_key, UpdateExpression="SET finalizedAt = :other", ExpressionAttributeValues={":other": runtime[-1] + 1})
    else:
        table.update_item(Key=entry_key, UpdateExpression="SET message = :other", ExpressionAttributeValues={":other": "x" * 65_537})
    duplicate = await client.post("/internal/v1/agent/chat/data/finalize", json=document, headers=headers)
    assert duplicate.status_code == 404, duplicate.text
    assert "mailbox" not in duplicate.text
    assert runtime[1].store._read("TENANT#tenant", "EXEC#run-user")["chat_terminal"] is not None


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_duplicate_terminal_racing_mailbox_change_refuses_receipt(mailbox, sts, monkeypatch):
    client, runtime, token, state, _ = mailbox
    candidate = await client.post(
        "/v1/chat/data/turn/result",
        json={
            "run_id": "run-user",
            "session_id": "session-a",
            "outcome": "failed",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert candidate.status_code == 200, candidate.text
    document, headers = await finalize_initial(client, runtime, sts, monkeypatch, state)
    first = await client.post("/internal/v1/agent/chat/data/finalize", json=document, headers=headers)
    assert first.status_code == 200, first.text
    original = runtime[1].store.client.transact_write_items

    def mutate(**kwargs):
        if any(item.get("ConditionCheck", {}).get("Key", {}).get("SK", {}).get("S") == "mailbox-id#run-user" for item in kwargs["TransactItems"]):
            runtime[2].update_item(
                Key={"PK": "session#session-a", "SK": "mailbox#00000001"},
                UpdateExpression="SET #status = :changed",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":changed": "interrupted"},
            )
        return original(**kwargs)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", mutate)
    duplicate = await client.post("/internal/v1/agent/chat/data/finalize", json=document, headers=headers)
    assert duplicate.status_code == 409, duplicate.text
    assert "mailbox" not in duplicate.text


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_finalized_initial_terminal_retries_after_session_lease_replacement(mailbox, sts, monkeypatch):
    client, runtime, token, state, _ = mailbox
    candidate = await client.post(
        "/v1/chat/data/turn/result",
        json={
            "run_id": "run-user",
            "session_id": "session-a",
            "outcome": "failed",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert candidate.status_code == 200, candidate.text
    document, headers = await finalize_initial(client, runtime, sts, monkeypatch, state)
    first = await client.post("/internal/v1/agent/chat/data/finalize", json=document, headers=headers)
    assert first.status_code == 200, first.text
    runtime[2].update_item(
        Key={"PK": "session#session-a", "SK": "header"},
        UpdateExpression="SET chatLease.run_id = :run, chatLease.generation = :generation, chatLease.expires_at = :expiry",
        ExpressionAttributeValues={":run": "run-followup", ":generation": 2, ":expiry": runtime[-1] + 90},
    )
    again = await client.post("/internal/v1/agent/chat/data/finalize", json=document, headers=headers)
    assert again.status_code == 200, again.text
    assert again.json() == first.json()


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
@pytest.mark.parametrize("change", ["foreign_owner", "replaced_lease", "premature_completion"])
async def test_initial_terminal_refuses_replaced_mailbox_or_lease(mailbox, sts, monkeypatch, change):
    client, runtime, token, state, _ = mailbox
    result = await client.post(
        "/v1/chat/data/turn/result",
        json={
            "run_id": "run-user",
            "session_id": "session-a",
            "outcome": "failed",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert result.status_code == 200, result.text
    table = runtime[2]
    if change == "foreign_owner":
        table.update_item(
            Key={"PK": "session#session-a", "SK": "mailbox#00000001"},
            UpdateExpression="SET ownerUserId = :other",
            ExpressionAttributeValues={":other": "another"},
        )
    elif change == "replaced_lease":
        table.update_item(
            Key={"PK": "session#session-a", "SK": "header"},
            UpdateExpression="SET chatLease.generation = :next",
            ExpressionAttributeValues={":next": 2},
        )
    else:
        table.update_item(
            Key={"PK": "session#session-a", "SK": "mailbox#00000001"},
            UpdateExpression="SET #status = :completed",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":completed": "completed"},
        )
    document, headers = await finalize_initial(client, runtime, sts, monkeypatch, state)
    response = await client.post("/internal/v1/agent/chat/data/finalize", json=document, headers=headers)
    assert response.status_code == 404
    assert "chat_terminal" not in runtime[1].store._read("TENANT#tenant", "EXEC#run-user")
    assert "terminal_result" not in table.get_item(Key={"PK": "session#session-a", "SK": "turn#run-user"})["Item"]


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_mailbox_race_during_initial_terminal_cannot_commit_outcome(mailbox, sts, monkeypatch):
    client, runtime, token, state, _ = mailbox
    result = await client.post(
        "/v1/chat/data/turn/result",
        json={
            "run_id": "run-user",
            "session_id": "session-a",
            "outcome": "failed",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert result.status_code == 200, result.text
    document, headers = await finalize_initial(client, runtime, sts, monkeypatch, state)
    original = runtime[1].store.client.transact_write_items

    def replace(**kwargs):
        if any("finalizedAt" in item.get("Update", {}).get("UpdateExpression", "") for item in kwargs["TransactItems"]):
            runtime[2].update_item(
                Key={"PK": "session#session-a", "SK": "mailbox#00000001"},
                UpdateExpression="SET #status = :interrupted",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":interrupted": "interrupted"},
            )
        return original(**kwargs)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", replace)
    response = await client.post("/internal/v1/agent/chat/data/finalize", json=document, headers=headers)
    assert response.status_code == 409, response.text
    assert "chat_terminal" not in runtime[1].store._read("TENANT#tenant", "EXEC#run-user")
    assert "terminal_result" not in runtime[2].get_item(Key={"PK": "session#session-a", "SK": "turn#run-user"})["Item"]


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
@pytest.mark.parametrize("change", ["revoked_root", "changed_mailbox", "changed_input"])
async def test_followup_rechecks_root_and_mailbox_before_reply(mailbox, monkeypatch, change):
    client, runtime, token, _, _ = mailbox
    table = runtime[2]
    prepare(runtime, message_id="run-followup", message="Follow up", team_id="team")
    ChatSessionMailbox(table).accept(
        session_id="session-a", owner=("tenant", "team", "human"), turn=AcceptedTurn(turn_id="run-followup", message="Follow up"), now=runtime[-1]
    )
    table.update_item(
        Key={"PK": "session#session-a", "SK": "mailbox#00000001"},
        UpdateExpression="SET #status = :failed",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":failed": "failed"},
    )
    original = runtime[0].verify
    checks = []

    def verify(*args, **kwargs):
        checks.append(True)
        result = original(*args, **kwargs)
        if len(checks) == 2:
            if change == "revoked_root":
                runtime[1].store.client.update_item(
                    TableName=runtime[1].store.table,
                    Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-followup"}},
                    UpdateExpression="SET #status = :revoked",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={":revoked": {"S": "revoked"}},
                )
            elif change == "changed_mailbox":
                table.update_item(
                    Key={"PK": "session#session-a", "SK": "mailbox#00000002"},
                    UpdateExpression="SET message = :changed",
                    ExpressionAttributeValues={":changed": "Substituted"},
                )
            else:
                table.update_item(
                    Key={"PK": "chat-input#run-followup", "SK": "input"},
                    UpdateExpression="SET ownerUserId = :changed",
                    ExpressionAttributeValues={":changed": "another"},
                )
        return result

    monkeypatch.setattr(runtime[0], "verify", verify)
    response = await client.post(
        "/v1/chat/data/session/next", json={"run_id": "run-user", "session_id": "session-a", "after": 1}, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == (503 if change == "changed_input" else 404), response.text
    assert len(checks) == 2
    assert "Follow up" not in response.text


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
@pytest.mark.parametrize(
    "change,expected",
    [
        ("missing_root", 404),
        ("other_session", 404),
        ("foreign_root", 404),
        ("foreign_input", 503),
        ("expired_input", 503),
        ("substituted_input", 503),
        ("valid", 200),
    ],
)
async def test_followup_root_guard_with_synthetic_committed_cursor(mailbox, change, expected):
    client, runtime, token, _, _ = mailbox
    if change == "foreign_root":
        envelope = {
            "message_id": "run-followup",
            "tenant_id": "tenant",
            "persona": "developer",
            "session_id": "session-a",
            "source_ref": {"repo": "chat/session-a"},
            "arrived_at": datetime.fromtimestamp(runtime[-1], UTC).isoformat(),
            "message": "Follow up",
            "team_id": "team",
        }
        provision_root(runtime[1].store, envelope, source="chat", human_id="another", now=datetime.fromtimestamp(runtime[-1], UTC))
    elif change != "missing_root":
        changes = {"session_id": "other-session", "source_ref": {"repo": "chat/other-session"}} if change == "other_session" else {}
        prepare(runtime, message_id="run-followup", message="Follow up", team_id="team", **changes)
    ChatSessionMailbox(runtime[2]).accept(
        session_id="session-a", owner=("tenant", "team", "human"), turn=AcceptedTurn(turn_id="run-followup", message="Follow up"), now=runtime[-1]
    )
    result = await client.post(
        "/v1/chat/data/turn/result",
        json={"run_id": "run-user", "session_id": "session-a", "outcome": "failed"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert result.status_code == 200, result.text
    runtime[2].update_item(
        Key={"PK": "session#session-a", "SK": "mailbox#00000001"},
        UpdateExpression="SET #status = :failed",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":failed": "failed"},
    )
    if change in {"foreign_input", "expired_input", "substituted_input"}:
        key = {"PK": "chat-input#run-followup", "SK": "input"}
        if change == "foreign_input":
            runtime[2].update_item(Key=key, UpdateExpression="SET ownerUserId = :other", ExpressionAttributeValues={":other": "another"})
        elif change == "expired_input":
            runtime[2].update_item(
                Key=key,
                UpdateExpression="SET #ttl = :expired",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={":expired": runtime[-1] - 1},
            )
        else:
            row = runtime[2].get_item(Key=key)["Item"]
            row["payload"]["input"]["message"] = "Substituted text"
            runtime[2].put_item(Item=row)
    response = await client.post(
        "/v1/chat/data/session/next", json={"run_id": "run-user", "session_id": "session-a", "after": 1}, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == expected, response.text
    if change == "valid":
        assert response.json()["turn"] == {"sequence": 2, "turn_id": "run-followup", "message": "Follow up"}
    else:
        assert "Follow up" not in response.text


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
@pytest.mark.parametrize("change", ["missing_receipt", "foreign_receipt", "wrong_digest", "wrong_sequence", "changed_text", "swapped_turn"])
async def test_followup_rejects_receipt_and_sequence_substitution(mailbox, change):
    client, runtime, token, _, _ = mailbox
    table = runtime[2]
    prepare(runtime, message_id="run-followup", message="Follow up", team_id="team")
    store = ChatSessionMailbox(table)
    store.accept(
        session_id="session-a", owner=("tenant", "team", "human"), turn=AcceptedTurn(turn_id="run-followup", message="Follow up"), now=runtime[-1]
    )
    table.update_item(
        Key={"PK": "session#session-a", "SK": "mailbox#00000001"},
        UpdateExpression="SET #status = :failed",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":failed": "failed"},
    )
    entry_key = {"PK": "session#session-a", "SK": "mailbox#00000002"}
    receipt_key = {"PK": "session#session-a", "SK": "mailbox-id#run-followup"}
    if change == "missing_receipt":
        table.delete_item(Key=receipt_key)
    elif change == "foreign_receipt":
        table.update_item(Key=receipt_key, UpdateExpression="SET ownerUserId = :other", ExpressionAttributeValues={":other": "another"})
    elif change == "wrong_digest":
        table.update_item(Key=receipt_key, UpdateExpression="SET digest = :other", ExpressionAttributeValues={":other": "0" * 64})
    elif change == "wrong_sequence":
        table.update_item(
            Key=receipt_key,
            UpdateExpression="SET #sequence = :other",
            ExpressionAttributeNames={"#sequence": "sequence"},
            ExpressionAttributeValues={":other": 3},
        )
    elif change == "changed_text":
        table.update_item(Key=entry_key, UpdateExpression="SET message = :other", ExpressionAttributeValues={":other": "Changed"})
    else:
        prepare(runtime, message_id="run-swap", message="Swapped", team_id="team")
        store.accept(
            session_id="session-a", owner=("tenant", "team", "human"), turn=AcceptedTurn(turn_id="run-swap", message="Swapped"), now=runtime[-1]
        )
        table.update_item(
            Key=entry_key,
            UpdateExpression="SET turnId = :other, message = :message",
            ExpressionAttributeValues={":other": "run-swap", ":message": "Swapped"},
        )
    response = await client.post(
        "/v1/chat/data/session/next", json={"run_id": "run-user", "session_id": "session-a", "after": 1}, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 404, response.text
    assert "Follow up" not in response.text and "Swapped" not in response.text


async def _ready_followup_claim(mailbox):
    client, runtime, token, _, _ = mailbox
    prepare(runtime, message_id="run-followup", message="Follow up", team_id="team")
    ChatSessionMailbox(runtime[2]).accept(
        session_id="session-a", owner=("tenant", "team", "human"), turn=AcceptedTurn(turn_id="run-followup", message="Follow up"), now=runtime[-1]
    )
    recorded = await client.post(
        "/v1/chat/data/turn/result",
        json={"run_id": "run-user", "session_id": "session-a", "outcome": "failed"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert recorded.status_code == 200, recorded.text
    runtime[2].update_item(
        Key={"PK": "session#session-a", "SK": "mailbox#00000001"},
        UpdateExpression="SET #status = :failed",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":failed": "failed"},
    )
    return client, runtime, token


async def _read_followup_claim(client, token):
    return await client.post(
        "/v1/chat/data/session/next", json={"run_id": "run-user", "session_id": "session-a", "after": 1}, headers={"Authorization": f"Bearer {token}"}
    )


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_followup_claim_retries_without_rebinding_pod(mailbox):
    client, runtime, token = await _ready_followup_claim(mailbox)
    store = runtime[1].store
    bound = store._read("POD#chat-pod", "BINDING")
    for _ in range(2):
        response = await _read_followup_claim(client, token)
        assert response.status_code == 200, response.text
        assert response.json()["turn"] == {"sequence": 2, "turn_id": "run-followup", "message": "Follow up"}
    claim = store._read("CHAT-SESSION-TURN#run-followup", "ADMISSION")
    assert claim["session_run_id"] == {"S": "run-user"}
    assert claim["sandbox_uid"] == {"S": "chat-pod"}
    assert claim["sequence"] == {"N": "2"}
    assert store._read("POD#chat-pod", "BINDING") == bound
    assert store._read("TENANT#tenant", "EXEC#run-followup")["status"] == {"S": "pending"}


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
@pytest.mark.parametrize("change", ["foreign_pod", "expired_lease", "aborted_root", "foreign_claim"])
async def test_followup_claim_denies_replaced_authority(mailbox, change):
    client, runtime, token = await _ready_followup_claim(mailbox)
    store = runtime[1].store
    if change == "foreign_pod":
        store.client.update_item(
            TableName=store.table,
            Key={"pk": {"S": "POD#chat-pod"}, "sk": {"S": "BINDING"}},
            UpdateExpression="SET invocation_id = :foreign",
            ExpressionAttributeValues={":foreign": {"S": "another"}},
        )
    elif change == "expired_lease":
        runtime[2].update_item(
            Key={"PK": "session#session-a", "SK": "header"},
            UpdateExpression="SET chatLease.expires_at = :expired",
            ExpressionAttributeValues={":expired": runtime[-1] - 1},
        )
    elif change == "aborted_root":
        store.client.update_item(
            TableName=store.table,
            Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-followup"}},
            UpdateExpression="SET abort_command_id = :abort",
            ExpressionAttributeValues={":abort": {"S": "abort"}},
        )
    else:
        store.client.put_item(
            TableName=store.table,
            Item={"pk": {"S": "CHAT-SESSION-TURN#run-followup"}, "sk": {"S": "ADMISSION"}, "session_id": {"S": "other-session"}},
        )
    response = await _read_followup_claim(client, token)
    assert response.status_code == 404, response.text
    assert "Follow up" not in response.text
    if change != "foreign_claim":
        assert store._read("CHAT-SESSION-TURN#run-followup", "ADMISSION") is None


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
@pytest.mark.parametrize("change", ["receipt", "root", "input", "pod", "abort_race", "lease_race"])
async def test_followup_claim_denies_authority_race(mailbox, monkeypatch, change):
    client, runtime, token = await _ready_followup_claim(mailbox)
    store, table = runtime[1].store, runtime[2]
    original = store.client.transact_write_items

    def race(**kwargs):
        if change == "receipt":
            table.update_item(
                Key={"PK": "session#session-a", "SK": "mailbox-id#run-followup"},
                UpdateExpression="SET #sequence = :changed",
                ExpressionAttributeNames={"#sequence": "sequence"},
                ExpressionAttributeValues={":changed": 3},
            )
        elif change == "root":
            store.client.update_item(
                TableName=store.table,
                Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-followup"}},
                UpdateExpression="SET #status = :changed",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":changed": {"S": "revoked"}},
            )
        elif change == "abort_race":
            store.client.update_item(
                TableName=store.table,
                Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-followup"}},
                UpdateExpression="SET abort_command_id = :abort",
                ExpressionAttributeValues={":abort": {"S": "abort"}},
            )
        elif change == "lease_race":
            table.update_item(
                Key={"PK": "session#session-a", "SK": "header"},
                UpdateExpression="SET chatLease.generation = :changed",
                ExpressionAttributeValues={":changed": 2},
            )
        elif change == "pod":
            store.client.update_item(
                TableName=store.table,
                Key={"pk": {"S": "POD#chat-pod"}, "sk": {"S": "BINDING"}},
                UpdateExpression="SET attempt = :changed",
                ExpressionAttributeValues={":changed": {"N": "9"}},
            )
        else:
            table.update_item(
                Key={"PK": "chat-input#run-followup", "SK": "input"},
                UpdateExpression="SET ownerUserId = :changed",
                ExpressionAttributeValues={":changed": "another"},
            )
        return original(**kwargs)

    monkeypatch.setattr(store.client, "transact_write_items", race)
    response = await _read_followup_claim(client, token)
    assert response.status_code == 404, response.text
    assert "Follow up" not in response.text
    assert store._read("CHAT-SESSION-TURN#run-followup", "ADMISSION") is None
