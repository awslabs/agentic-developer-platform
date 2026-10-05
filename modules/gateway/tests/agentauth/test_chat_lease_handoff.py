"""Terminal-turn lease handoff through HTTP and transactional DynamoDB emulation."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from threading import Barrier, Lock

import pytest
from botocore.exceptions import EndpointConnectionError
from starlette.concurrency import run_in_threadpool

from src.agentauth import chat_data_routes
from src.agentauth.bootstrap import envelope_digest
from src.agentauth.chat_admission import admit as admit_launch
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatLaunch
from src.agentauth.execution import ExecutionStatus
from src.agentauth.external_roots import provision_root
from tests.agentauth.test_chat_authority import POD
from tests.agentauth.test_chat_data_routes import ADMIT, admit, exchange
from tests.agentauth.test_chat_data_routes import client as client_fixture
from tests.agentauth.test_chat_data_routes import runtime as runtime_fixture
from tests.agentauth.test_chat_data_routes import store as store_fixture
from tests.agentauth.test_chat_data_routes import sts as sts_fixture
from tests.agentauth.test_work_producer import proof

client = client_fixture
runtime = runtime_fixture
store = store_fixture
sts = sts_fixture
HEADER = {"PK": "session#session-a", "SK": "header"}


def next_turn(runtime, run_id="run-b"):
    now = datetime.fromtimestamp(runtime[-1] + 10, UTC)
    envelope = {
        "message_id": run_id,
        "tenant_id": "tenant",
        "persona": "developer",
        "source_ref": {"repo": "chat/session-a"},
        "arrived_at": now.isoformat(),
    }
    provision_root(runtime[1].store, envelope, source="chat", human_id="human", now=now)
    return {"run_id": run_id, "envelope_digest": envelope_digest(envelope), "pod_name": "chat-a", "pod_uid": f"pod-{run_id}"}


async def admit_next(client, runtime, body):
    runtime[4]["uid"] = body["pod_uid"]
    return await client.post(ADMIT, json=body, headers={"X-Adp-Producer-Proof": proof(envelope_digest(body))})


def terminate(runtime, status=ExecutionStatus.COMPLETED):
    runtime[1].store.authority.set_execution_status(
        invocation_id="run-a", tenant_id="tenant", status=status, expected_attempt=1, expected_status=ExecutionStatus.ACTIVE
    )


@pytest.mark.parametrize("status", [ExecutionStatus.COMPLETED, ExecutionStatus.CANCELLED, ExecutionStatus.REVOKED])
async def test_terminal_turn_hands_off_immediately_and_old_capability_is_refused(client, runtime, monkeypatch, status):
    assert (await admit(client, runtime)).status_code == 200
    first = await exchange(client)
    assert first.status_code == 200
    old_capability = first.json()["capability"]
    terminate(runtime, status)
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] + 10)
    body = next_turn(runtime)
    response = await admit_next(client, runtime, body)
    assert response.status_code == 200, response.text
    assert response.json() == {"run_id": "run-b", "session_id": "session-a", "lease_generation": 2}
    assert (await admit_next(client, runtime, body)).json() == response.json()
    header = runtime[2].get_item(Key=HEADER)["Item"]
    assert header["chatLease"] == {"run_id": "run-b", "sandbox_uid": "pod-run-b", "generation": 2, "expires_at": runtime[-1] + 310}
    assert (await exchange(client)).status_code == 200
    header = runtime[2].get_item(Key=HEADER)["Item"]
    runtime[4]["uid"] = "chat-pod"
    assert (await exchange(client)).status_code == 404
    assert (await admit(client, runtime)).status_code == 404
    context_before = runtime[2].scan()["Items"]
    for operation, fields in (
        ("read", {}),
        (
            "append",
            {"idempotency_key": "late-write", "expected_version": 0, "content": "old turn", "tokens": 2, "user_turn_id": "user-turn-a"},
        ),
    ):
        response = await client.post(
            f"/v1/chat/data/history/{operation}",
            json={"run_id": "run-a", "session_id": "session-a", **fields},
            headers={"Authorization": f"Bearer {old_capability}"},
        )
        assert response.status_code == 404
        assert response.json() == {"detail": {"error": "chat_scope_refused"}}
    assert runtime[2].get_item(Key=HEADER)["Item"] == header
    assert runtime[2].scan()["Items"] == context_before


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "active"),
        ("status", "pending"),
        ("status", "unknown"),
        ("status", None),
        ("repo", "chat/another-session"),
        ("workload_binding", "another-pod"),
        (None, None),
    ],
)
async def test_handoff_checks_terminal_execution_at_commit(client, runtime, monkeypatch, field, value):
    assert (await admit(client, runtime)).status_code == 200
    terminate(runtime)
    protected = runtime[1].store
    original = protected.client.transact_write_items
    previous_key = {"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-a"}}
    header = runtime[2].get_item(Key=HEADER)["Item"]

    def change_execution(**kwargs):
        if any(action.get("Put", {}).get("Item", {}).get("pk") == {"S": "CHAT-LAUNCH#run-b"} for action in kwargs["TransactItems"]):
            if field is None:
                protected.client.delete_item(TableName=protected.table, Key=previous_key)
            else:
                execution = protected._read("TENANT#tenant", "EXEC#run-a")
                if value is None:
                    execution.pop(field)
                else:
                    execution[field] = {"S": value}
                protected.client.put_item(TableName=protected.table, Item=execution)
        return original(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", change_execution)
    response = await admit_next(client, runtime, next_turn(runtime))
    assert response.status_code == 404, response.text
    assert protected._read("CHAT-LAUNCH#run-b", "LAUNCH") is None
    assert runtime[2].get_item(Key=HEADER)["Item"] == header


@pytest.mark.parametrize("field", ["generation", "expires_at"])
async def test_handoff_does_not_overwrite_concurrently_changed_lease(client, runtime, monkeypatch, field):
    assert (await admit(client, runtime)).status_code == 200
    terminate(runtime)
    protected = runtime[1].store
    original = protected.client.transact_write_items
    header = runtime[2].get_item(Key=HEADER)["Item"]
    header["chatLease"][field] += 1

    def change_lease(**kwargs):
        if any(action.get("Put", {}).get("Item", {}).get("pk") == {"S": "CHAT-LAUNCH#run-b"} for action in kwargs["TransactItems"]):
            runtime[2].put_item(Item=header)
        return original(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", change_lease)
    body = next_turn(runtime)
    response = await admit_next(client, runtime, body)
    assert response.status_code == 404, response.text
    assert protected._read("CHAT-LAUNCH#run-b", "LAUNCH") is None
    assert runtime[2].get_item(Key=HEADER)["Item"] == header
    monkeypatch.setattr(protected.client, "transact_write_items", original)
    response = await admit_next(client, runtime, body)
    assert response.status_code == 200, response.text
    assert response.json()["lease_generation"] == header["chatLease"]["generation"] + 1


async def test_competing_handoffs_commit_exactly_one_launch(client, runtime, monkeypatch):
    assert (await admit(client, runtime)).status_code == 200
    terminate(runtime)
    candidates = [next_turn(runtime, run_id) for run_id in ("run-b", "run-c")]
    protected = runtime[1].store
    original = protected.client.transact_write_items
    barrier, lock = Barrier(2), Lock()

    def synchronize_handoffs(**kwargs):
        if any(action.get("Put", {}).get("Item", {}).get("pk", {}).get("S", "").startswith("CHAT-LAUNCH#") for action in kwargs["TransactItems"]):
            barrier.wait(timeout=10)
        with lock:
            return original(**kwargs)

    monkeypatch.setattr(protected.client, "transact_write_items", synchronize_handoffs)
    results = await asyncio.gather(
        *(
            run_in_threadpool(
                admit_launch,
                runtime[1],
                run_id=body["run_id"],
                digest=body["envelope_digest"],
                pod=replace(POD, uid=body["pod_uid"]),
                team_id="team",
                now=runtime[-1] + 10,
            )
            for body in candidates
        ),
        return_exceptions=True,
    )
    winners = [result for result in results if isinstance(result, ChatLaunch)]
    assert len(winners) == 1, results
    assert sum(isinstance(result, ChatAuthorizationRefusedError) for result in results) == 1
    winner = winners[0]
    assert winner.lease_generation == 2
    assert runtime[2].get_item(Key=HEADER)["Item"]["chatLease"]["run_id"] == winner.run_id
    for body in candidates:
        assert (protected._read(f"CHAT-LAUNCH#{body['run_id']}", "LAUNCH") is not None) == (body["run_id"] == winner.run_id)


async def test_lost_handoff_response_retries_without_advancing_generation(client, runtime, monkeypatch):
    assert (await admit(client, runtime)).status_code == 200
    terminate(runtime)
    protected = runtime[1].store
    original = protected.client.transact_write_items

    def lose_response(**kwargs):
        result = original(**kwargs)
        if any(action.get("Put", {}).get("Item", {}).get("pk") == {"S": "CHAT-LAUNCH#run-b"} for action in kwargs["TransactItems"]):
            raise EndpointConnectionError(endpoint_url="https://storage.test")
        return result

    monkeypatch.setattr(protected.client, "transact_write_items", lose_response)
    body = next_turn(runtime)
    assert (await admit_next(client, runtime, body)).status_code == 503
    saved = protected._read("CHAT-LAUNCH#run-b", "LAUNCH")
    assert saved is not None
    response = await admit_next(client, runtime, body)
    assert response.status_code == 200, response.text
    assert response.json()["lease_generation"] == 2
    assert protected._read("CHAT-LAUNCH#run-b", "LAUNCH") == saved
