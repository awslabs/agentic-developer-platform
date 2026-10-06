"""Gateway-only recovery against accepted user turns and fenced session leases."""

import pytest
from starlette.concurrency import run_in_threadpool

from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.agentauth.chat_turn_recovery import reconcile_lost_turn
from tests.agentauth.test_chat_history_write import append
from tests.agentauth.test_chat_history_write import capability as capability_fixture
from tests.agentauth.test_chat_history_write import client as client_fixture
from tests.agentauth.test_chat_history_write import retained_input_table as retained_input_table_fixture
from tests.agentauth.test_chat_history_write import runtime as runtime_fixture
from tests.agentauth.test_chat_history_write import store as store_fixture
from tests.agentauth.test_chat_history_write import sts as sts_fixture

capability = capability_fixture
client = client_fixture
retained_input_table = retained_input_table_fixture
runtime = runtime_fixture
store = store_fixture
sts = sts_fixture

HEADER = {"PK": "session#session-a", "SK": "header"}
RECEIPT = {"PK": HEADER["PK"], "SK": "turn#run-write"}


def turn(runtime):
    return runtime[2].get_item(Key=RECEIPT, ConsistentRead=True)["Item"]


async def test_worker_killed_before_reply_fences_lease_and_marks_safe_retry(client, runtime, capability):
    assert turn(runtime)["status"] == "accepted"
    result = await run_in_threadpool(reconcile_lost_turn, runtime[1], run_id="run-write", now=runtime[-1] + 1, worker_terminated=True)
    assert result == {"status": "interrupted", "retryable": True}
    assert turn(runtime)["status"] == "interrupted" and turn(runtime)["retryable"] is True
    assert turn(runtime)["automaticReplayPermitted"] is False
    header = runtime[2].get_item(Key=HEADER, ConsistentRead=True)["Item"]
    assert header["chatLease"]["expires_at"] <= runtime[-1]
    assert (await append(client, capability)).status_code == 404
    assert header["chatLease"]["expires_at"] == 1
    assert await run_in_threadpool(reconcile_lost_turn, runtime[1], run_id="run-write", now=runtime[-1] + 1) == result


async def test_lease_expiry_reconciles_without_a_worker_report(runtime, capability):
    expires_at = runtime[2].get_item(Key=HEADER, ConsistentRead=True)["Item"]["chatLease"]["expires_at"]
    before = await run_in_threadpool(reconcile_lost_turn, runtime[1], run_id="run-write", now=runtime[-1])
    assert before == {"status": "running", "retryable": False}
    assert await run_in_threadpool(reconcile_lost_turn, runtime[1], run_id="run-write", now=int(expires_at) + 1) == {
        "status": "interrupted",
        "retryable": True,
    }


async def test_committed_history_is_not_auto_replayed_after_worker_loss(client, runtime, capability):
    assert (await append(client, capability)).status_code == 200
    result = await run_in_threadpool(reconcile_lost_turn, runtime[1], run_id="run-write", now=runtime[-1], worker_terminated=True)
    assert result == {"status": "interrupted", "retryable": False}
    assert turn(runtime)["retryable"] is False
    assert (await append(client, capability, idempotency_key="another")).status_code == 404


async def test_replaced_lease_rejects_old_recovery(runtime, capability):
    runtime[2].update_item(
        Key=HEADER,
        UpdateExpression="SET chatLease.generation = :generation",
        ExpressionAttributeValues={":generation": 2},
    )
    with pytest.raises(ChatAuthorizationRefusedError):
        await run_in_threadpool(reconcile_lost_turn, runtime[1], run_id="run-write", now=runtime[-1], worker_terminated=True)
    assert turn(runtime)["status"] == "accepted"


async def test_history_commit_racing_recovery_cannot_be_marked_safe(runtime, capability, monkeypatch):
    original = runtime[1].store.client.transact_write_items

    def concurrent_commit(**request):
        runtime[2].update_item(
            Key=HEADER,
            UpdateExpression="SET historyVersion = historyVersion + :one, historyNextOrdinal = historyNextOrdinal + :one",
            ExpressionAttributeValues={":one": 1},
        )
        return original(**request)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", concurrent_commit)
    with pytest.raises(ChatAuthorizationRefusedError):
        await run_in_threadpool(reconcile_lost_turn, runtime[1], run_id="run-write", now=runtime[-1], worker_terminated=True)
    assert turn(runtime)["status"] == "accepted"
    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", original)
    assert await run_in_threadpool(reconcile_lost_turn, runtime[1], run_id="run-write", now=runtime[-1], worker_terminated=True) == {
        "status": "interrupted",
        "retryable": False,
    }
