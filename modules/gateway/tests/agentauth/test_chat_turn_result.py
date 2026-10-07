import asyncio
from datetime import UTC, datetime
from threading import Barrier, Lock

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.execution import ExecutionStatus
from tests.agentauth import test_chat_history_write as fixtures

client = fixtures.client
runtime = fixtures.runtime
store = fixtures.store
sts = fixtures.sts
capability = fixtures.capability
retained_input_table = fixtures.retained_input_table
PATH = "/v1/chat/data/turn/result"
TURN = {"PK": "session#session-a", "SK": "turn#run-write"}


async def commit(client, capability, **changes):
    return await client.post(
        PATH,
        json={"run_id": "run-write", "session_id": "session-a", "outcome": "failed", **changes},
        headers={"Authorization": f"Bearer {capability}"},
    )


def turn(runtime):
    return runtime[2].get_item(Key=TURN, ConsistentRead=True)["Item"]


def cancel(runtime):
    runtime[1].store.authority.record_abort_intent(
        invocation_id="run-write",
        tenant_id="tenant",
        attempt=1,
        command_id="owner-cancel",
        body_digest="a" * 64,
        now=datetime.fromtimestamp(runtime[-1], UTC),
    )


async def test_success_requires_saved_assistant_and_records_nonterminal_result(client, runtime, capability):
    response = await fixtures.append(client, capability)
    assert response.status_code == 200
    message_id = response.json()["message_id"]
    header = runtime[2].get_item(Key=fixtures.HEADER)["Item"]
    first = await commit(client, capability, outcome="completed", message_id=message_id)
    assert first.status_code == 200, first.text
    receipt = first.json()
    assert receipt == {
        "run_id": "run-write",
        "session_id": "session-a",
        "attempt": 1,
        "lease_generation": 1,
        "sandbox_uid": runtime[5].sandbox_uid,
        "outcome": "completed",
        "message_id": message_id,
        "terminal": False,
    }
    assert first.headers["cache-control"] == "no-store"
    before = turn(runtime)
    assert before["status"] == "accepted" and before["result_candidate"] == receipt
    assert before["resultRecordedAt"] == runtime[-1]
    assert (await commit(client, capability, outcome="completed", message_id=message_id)).json() == receipt
    assert turn(runtime) == before
    assert runtime[2].get_item(Key=fixtures.HEADER)["Item"] == header
    execution = runtime[1].store.authority.load_execution(invocation_id="run-write", tenant_id="tenant")
    assert execution.status == ExecutionStatus.ACTIVE
    assert (await commit(client, capability)).status_code == 409
    assert turn(runtime) == before


async def test_failure_can_be_recorded_without_reply_and_cannot_be_overwritten(client, runtime, capability):
    first = await commit(client, capability)
    assert first.status_code == 200 and first.json()["outcome"] == "failed" and first.json()["message_id"] is None
    assert first.json()["terminal"] is False and turn(runtime)["status"] == "accepted"
    assert (await commit(client, capability)).json() == first.json()
    message_id = (await fixtures.append(client, capability)).json()["message_id"]
    assert (await commit(client, capability, outcome="completed", message_id=message_id)).status_code == 409
    assert turn(runtime)["result_candidate"] == first.json()


@pytest.mark.parametrize(
    "changes",
    [
        {"outcome": "cancelled"},
        {"outcome": "completed"},
        {"message_id": "unexpected"},
        {"outcome": "interrupted"},
        {"attempt": 2},
        {"lease_generation": 2},
        {"user_id": "other"},
        {"terminal": True},
        {"error": "private exception"},
    ],
)
async def test_result_cannot_supply_terminal_state_or_trusted_claims(client, runtime, capability, changes):
    assert (await commit(client, capability, **changes)).status_code == 422
    assert "result_candidate" not in turn(runtime)


@pytest.mark.parametrize("change", ["missing", "user", "foreign_run", "foreign_owner", "wrong_turn", "wrong_lease"])
async def test_success_rejects_foreign_or_unrelated_message(client, runtime, capability, change):
    message_id = (await fixtures.append(client, capability)).json()["message_id"]
    key = {"PK": TURN["PK"], "SK": f"msg#{message_id}"}
    if change == "missing":
        message_id = "missing-message"
    elif change == "user":
        message_id = fixtures.USER_REF
    else:
        field, value = {
            "foreign_run": ("runId", "other"),
            "foreign_owner": ("ownerUserId", "other"),
            "wrong_turn": ("userTurnId", "user_other"),
            "wrong_lease": ("leaseGeneration", 2),
        }[change]
        runtime[2].update_item(Key=key, UpdateExpression=f"SET {field} = :value", ExpressionAttributeValues={":value": value})
    assert (await commit(client, capability, outcome="completed", message_id=message_id)).status_code == 404
    assert "result_candidate" not in turn(runtime)


@pytest.mark.parametrize("change,expected", [("cancel", 404), ("lease", 404), ("attempt", 404), ("message", 409), ("input", 503)])
async def test_concurrent_revocation_or_record_change_refuses_result(client, runtime, capability, monkeypatch, change, expected):
    message_id = (await fixtures.append(client, capability)).json()["message_id"]
    store = runtime[1].store
    original = store.client.transact_write_items

    def transact(**request):
        if change == "cancel":
            cancel(runtime)
        elif change == "lease":
            runtime[2].update_item(Key=fixtures.HEADER, UpdateExpression="SET chatLease.generation = :next", ExpressionAttributeValues={":next": 2})
        elif change == "attempt":
            store.client.update_item(
                TableName=store.table,
                Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-write"}},
                UpdateExpression="SET current_attempt = :next",
                ExpressionAttributeValues={":next": {"N": "2"}},
            )
        elif change == "message":
            runtime[2].update_item(
                Key={"PK": TURN["PK"], "SK": f"msg#{message_id}"},
                UpdateExpression="SET content = :changed",
                ExpressionAttributeValues={":changed": "changed"},
            )
        else:
            runtime[2].update_item(Key=TURN, UpdateExpression="SET inputDigest = :changed", ExpressionAttributeValues={":changed": "changed"})
        return original(**request)

    monkeypatch.setattr(store.client, "transact_write_items", transact)
    response = await commit(client, capability, outcome="completed", message_id=message_id)
    assert response.status_code == expected, response.text
    assert "result_candidate" not in turn(runtime)


@pytest.mark.parametrize("outcome", ["completed", "failed"])
async def test_lost_commit_response_recovers_same_result_without_terminal_transition(client, runtime, capability, monkeypatch, outcome):
    fields = {"outcome": outcome}
    if outcome == "completed":
        fields["message_id"] = (await fixtures.append(client, capability)).json()["message_id"]
    store = runtime[1].store
    original = store.client.transact_write_items

    def transact(**request):
        original(**request)
        raise EndpointConnectionError(endpoint_url="https://private-storage.example.test")

    monkeypatch.setattr(store.client, "transact_write_items", transact)
    response = await commit(client, capability, **fields)
    assert response.status_code == 503 and "private-storage" not in response.text
    first = turn(runtime)
    assert first["result_candidate"]["outcome"] == outcome
    monkeypatch.setattr(store.client, "transact_write_items", original)
    assert (await commit(client, capability, **fields)).json() == first["result_candidate"]
    assert turn(runtime) == first


@pytest.mark.parametrize("same_result", [True, False])
async def test_concurrent_outcomes_commit_once(client, runtime, capability, monkeypatch, same_result):
    """Race requests before commit while serializing Moto's snapshot/rollback."""
    message_id = (await fixtures.append(client, capability)).json()["message_id"]
    original = runtime[1].store.client.transact_write_items
    barrier = Barrier(2)
    lock = Lock()

    def transact(**request):
        barrier.wait(timeout=10)
        with lock:
            return original(**request)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", transact)
    success = {"outcome": "completed", "message_id": message_id}
    responses = await asyncio.gather(commit(client, capability, **success), commit(client, capability, **(success if same_result else {})))
    assert sorted(response.status_code for response in responses) == ([200, 200] if same_result else [200, 409])
    recorded = turn(runtime)["result_candidate"]
    assert all(response.json() == recorded for response in responses if response.status_code == 200)
    assert turn(runtime)["status"] == "accepted"


async def test_cancelled_turn_cannot_commit_or_replay_result(client, runtime, capability):
    assert (await commit(client, capability)).status_code == 200
    before = turn(runtime)
    cancel(runtime)
    assert (await commit(client, capability)).status_code == 404
    assert turn(runtime) == before


@pytest.mark.parametrize("changes", [{"session_id": "other"}, {"run_id": "other"}])
async def test_result_is_bound_to_own_run_and_session(client, runtime, capability, changes):
    assert (await commit(client, capability, **changes)).status_code == 404
    assert "result_candidate" not in turn(runtime)


async def test_result_requires_workload_bound_capability(client, runtime, capability):
    response = await client.post(PATH, json={"run_id": "run-write", "session_id": "session-a", "outcome": "failed"})
    assert response.status_code == 401
    response = await client.post(
        PATH,
        json={"run_id": "run-write", "session_id": "session-a", "outcome": "failed"},
        headers={"Authorization": f"Bearer {capability}", "X-Adp-Workload-Token": "foreign"},
    )
    assert response.status_code == 404
    assert "result_candidate" not in turn(runtime)


async def test_valid_history_capability_does_not_imply_result_permission(client, runtime, capability):
    launches = runtime[0].launches
    launch = launches.load("run-write")
    reduced = launch.model_copy(update={"operations": launch.operations - {"turn.result"}})
    runtime[1].store.client.put_item(TableName=runtime[1].store.table, Item=launches.item(reduced))
    token = (await fixtures.exchange(client)).json()["capability"]
    assert (await commit(client, token)).status_code == 404
    assert (await fixtures.append(client, token)).status_code == 200
    assert "result_candidate" not in turn(runtime)
