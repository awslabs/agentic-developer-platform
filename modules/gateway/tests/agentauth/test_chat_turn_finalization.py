"""Trusted terminal reconciliation through real HTTP and transactional emulators."""

import json

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.agentauth.chat_model_journal import ChatModelJournal
from tests.agentauth import test_chat_history_write as history
from tests.agentauth import test_chat_turn_result as results
from tests.agentauth.test_chat_sandbox_exit import supervisor
from tests.agentauth.test_work_producer import proof

client = history.client
runtime = history.runtime
store = history.store
sts = history.sts
capability = history.capability
retained_input_table = history.retained_input_table
FINALIZE = "/internal/v1/agent/chat/data/finalize"
EXIT = "/internal/v1/agent/chat/data/exit"


def body(runtime):
    dispatch = runtime[1].store._read("INVOCATION#run-write", "DISPATCH")
    return {"run_id": "run-write", "envelope_digest": dispatch["envelope_digest"]["S"], "pod_name": "chat-a", "pod_uid": "chat-pod"}


async def request(client, runtime, path=FINALIZE, **changes):
    document = {**body(runtime), **changes}
    return await client.post(path, json=document, headers={"X-Adp-Producer-Proof": proof(envelope_digest(document))})


def execution(runtime):
    return runtime[1].store._read("TENANT#tenant", "EXEC#run-write")


def header(runtime):
    return runtime[2].get_item(Key=history.HEADER, ConsistentRead=True)["Item"]


@pytest.fixture
async def ready(client, runtime, capability, sts, monkeypatch):
    supervisor(sts, monkeypatch)
    monkeypatch.setattr(runtime[1].workloads, "has_exited", lambda **kwargs: True)
    monkeypatch.setattr(runtime[1].workloads, "is_absent", lambda **kwargs: True)
    assert (await request(client, runtime, EXIT)).json()["terminated"] is True
    return capability


def model_operation(runtime, **changes):
    operation = {
        "run_id": "run-write",
        "session_id": "session-a",
        "sandbox_uid": "chat-pod",
        "lease_generation": 1,
        "operation_id": "model-a",
        "status": "unknown",
        "reservation_status": "unknown",
        "automatic_replay_permitted": False,
        **changes,
    }
    protected = runtime[1].store
    item = {
        "pk": {"S": "CHAT-MODEL#run-write"},
        "sk": {"S": f"OP#{operation['operation_id']}"},
        "document": {"S": json.dumps(operation)},
    }
    protected.client.put_item(TableName=protected.table, Item=item)
    return item


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled", "interrupted"])
async def test_terminal_outcomes_are_durable_fenced_and_idempotent(client, runtime, ready, monkeypatch, outcome):
    message_id = None
    if outcome == "completed":
        message_id = (await history.append(client, ready)).json()["message_id"]
        assert (await results.commit(client, ready, outcome=outcome, message_id=message_id)).status_code == 200
    elif outcome == "failed":
        assert (await results.commit(client, ready)).status_code == 200
    elif outcome == "cancelled":
        results.cancel(runtime)
    original_header = header(runtime)
    response = await request(client, runtime)
    assert response.status_code == 200, response.text
    terminal = response.json()
    assert terminal == {
        "run_id": "run-write",
        "session_id": "session-a",
        "attempt": 1,
        "lease_generation": 1,
        "sandbox_uid": "chat-pod",
        "outcome": outcome,
        "message_id": message_id,
        "terminal": True,
        "retryable": outcome == "interrupted",
        "automatic_replay_permitted": False,
        "accounting_status": "not_used",
        "finalized_at": runtime[-1],
    }
    assert response.headers["cache-control"] == "no-store"
    assert results.turn(runtime)["terminal_result"] == terminal
    assert results.turn(runtime)["status"] == outcome
    assert execution(runtime)["status"] == {"S": "cancelled" if outcome == "cancelled" else "completed"}
    assert header(runtime) == {**original_header, "chatLease": {**original_header["chatLease"], "expires_at": 1}}
    assert (await history.append(client, ready)).status_code == 404
    assert (await results.commit(client, ready)).status_code == 404
    monkeypatch.setattr(runtime[1].workloads, "is_absent", lambda **kwargs: False)
    assert (await request(client, runtime)).json() == terminal


async def test_missing_teardown_never_seals_or_finalizes(client, runtime, capability, sts, monkeypatch):
    supervisor(sts, monkeypatch)
    before = header(runtime)
    assert (await request(client, runtime)).status_code == 404
    assert header(runtime) == before
    assert "chat_terminal" not in execution(runtime)


async def test_exit_without_removal_never_seals_or_finalizes(client, runtime, ready, monkeypatch):
    monkeypatch.setattr(runtime[1].workloads, "is_absent", lambda **kwargs: False)
    before = header(runtime)
    assert (await request(client, runtime)).status_code == 409
    assert header(runtime) == before
    assert results.turn(runtime)["status"] == "accepted"


async def test_cancellation_after_result_wins_before_terminal_commit(client, runtime, ready):
    message_id = (await history.append(client, ready)).json()["message_id"]
    assert (await results.commit(client, ready, outcome="completed", message_id=message_id)).status_code == 200
    results.cancel(runtime)
    terminal = (await request(client, runtime)).json()
    assert terminal["outcome"] == "cancelled" and terminal["message_id"] is None


async def test_partial_history_without_result_is_interrupted_without_safe_retry(client, runtime, ready):
    assert (await history.append(client, ready)).status_code == 200
    terminal = (await request(client, runtime)).json()
    assert terminal["outcome"] == "interrupted" and terminal["retryable"] is False


@pytest.mark.parametrize(
    "state,reservation,logged,expected",
    [
        ("pending", "unknown", False, "unresolved"),
        ("running", "reserved", False, "unresolved"),
        ("unknown", "unknown", False, "unresolved"),
        ("confirmed", "reserved", True, "unresolved"),
        ("confirmed", "settled", False, "unresolved"),
        ("confirmed", "settled", True, "settled"),
        ("rejected", "released", False, "settled"),
        ("rejected", "not_reserved", False, "settled"),
    ],
)
async def test_accounting_evidence_is_preserved_without_replay_or_release(client, runtime, ready, state, reservation, logged, expected):
    item = model_operation(runtime, status=state, reservation_status=reservation, usage_logged=logged)
    terminal = (await request(client, runtime)).json()
    assert terminal["outcome"] == "interrupted"
    assert terminal["accounting_status"] == expected
    assert terminal["retryable"] is False and terminal["automatic_replay_permitted"] is False
    assert runtime[1].store._read(item["pk"]["S"], item["sk"]["S"]) == item


async def test_accounting_paginates_and_never_hides_uncertain_operations(client, runtime, ready, monkeypatch):
    model_operation(runtime)
    model_operation(runtime, operation_id="model-b", status="confirmed", reservation_status="settled", usage_logged=True)
    original = runtime[1].store.client.query
    pages = []

    def query(**kwargs):
        pages.append(kwargs)
        return original(**{**kwargs, "Limit": 1})

    monkeypatch.setattr(runtime[1].store.client, "query", query)
    terminal = (await request(client, runtime)).json()
    assert terminal["accounting_status"] == "unresolved"
    assert len(pages) == 2 and all(page["ConsistentRead"] for page in pages)


@pytest.mark.parametrize("stage", ["seal", "terminal"])
async def test_lost_write_response_recovers_without_reexecution(client, runtime, ready, monkeypatch, stage):
    protected = runtime[1].store
    original = protected.client.transact_write_items

    def lost(**kwargs):
        original(**kwargs)
        updates = [item.get("Update", {}).get("UpdateExpression", "") for item in kwargs["TransactItems"]]
        if any(("chatLease.expires_at" if stage == "seal" else "chat_terminal") in expression for expression in updates):
            raise EndpointConnectionError(endpoint_url="https://storage.test")

    monkeypatch.setattr(protected.client, "transact_write_items", lost)
    assert (await request(client, runtime)).status_code == 503
    assert header(runtime)["chatLease"]["expires_at"] == 1
    assert ("chat_terminal" in execution(runtime)) is (stage == "terminal")
    monkeypatch.setattr(protected.client, "transact_write_items", original)
    retry = await request(client, runtime)
    assert retry.status_code == 200, retry.text
    assert retry.json()["outcome"] == "interrupted"


async def test_cancellation_racing_terminal_commit_is_not_lost(client, runtime, ready, monkeypatch):
    assert (await results.commit(client, ready)).status_code == 200
    original = runtime[1].store.client.transact_write_items

    def cancel_first(**kwargs):
        if any("chat_terminal" in item.get("Update", {}).get("UpdateExpression", "") for item in kwargs["TransactItems"]):
            results.cancel(runtime)
        return original(**kwargs)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", cancel_first)
    assert (await request(client, runtime)).status_code == 409
    assert results.turn(runtime)["status"] == "accepted"
    assert "chat_terminal" not in execution(runtime)
    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", original)
    assert (await request(client, runtime)).json()["outcome"] == "cancelled"


@pytest.mark.parametrize("field,value", [("generation", 2), ("run_id", "other"), ("sandbox_uid", "other")])
async def test_replaced_lease_rejects_stale_reconciliation(client, runtime, ready, field, value):
    runtime[2].update_item(Key=history.HEADER, UpdateExpression=f"SET chatLease.{field} = :value", ExpressionAttributeValues={":value": value})
    assert (await request(client, runtime)).status_code == 404
    assert "chat_terminal" not in execution(runtime)


@pytest.mark.parametrize("changes", [{"outcome": "completed"}, {"terminal": True}, {"lease_generation": 2}, {"principal": "other"}])
async def test_caller_cannot_choose_outcome_or_authority(client, runtime, ready, changes):
    assert (await request(client, runtime, **changes)).status_code == 422
    assert "chat_terminal" not in execution(runtime)


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer sandbox-token"}, {"X-Adp-Producer-Proof": "forged"}])
async def test_finalization_requires_supervisor_proof(client, runtime, ready, headers):
    assert (await client.post(FINALIZE, json=body(runtime), headers=headers)).status_code == 403
    assert "chat_terminal" not in execution(runtime)


async def test_forged_binding_and_worker_role_cannot_finalize(client, runtime, ready, sts):
    assert (await request(client, runtime, pod_uid="other")).status_code == 404
    sts["role"] = "worker"
    assert (await request(client, runtime)).status_code == 403
    assert "chat_terminal" not in execution(runtime)


@pytest.mark.parametrize("field,value", [("ownerUserId", "foreign"), ("runId", "foreign"), ("leaseGeneration", 2), ("userTurnId", "foreign")])
async def test_success_rechecks_saved_reply_provenance(client, runtime, ready, field, value):
    message_id = (await history.append(client, ready)).json()["message_id"]
    assert (await results.commit(client, ready, outcome="completed", message_id=message_id)).status_code == 200
    runtime[2].update_item(
        Key={"PK": "session#session-a", "SK": f"msg#{message_id}"},
        UpdateExpression="SET #field = :value",
        ExpressionAttributeNames={"#field": field},
        ExpressionAttributeValues={":value": value},
    )
    assert (await request(client, runtime)).status_code == 404
    assert "chat_terminal" not in execution(runtime)


async def test_lease_is_sealed_before_journal_inspection_and_late_model_claims_fail(client, runtime, ready, monkeypatch):
    original = runtime[1].store.client.query
    launch = runtime[0].launches.load("run-write")
    journal = ChatModelJournal(runtime[1])
    inspected = []

    def query(**kwargs):
        assert header(runtime)["chatLease"]["expires_at"] == 1
        with pytest.raises(ChatAuthorizationRefusedError):
            journal.claim(launch, operation_id="late-model", request_digest="a" * 64, model_id="anthropic.test", now=runtime[-1])
        inspected.append(True)
        return original(**kwargs)

    monkeypatch.setattr(runtime[1].store.client, "query", query)
    assert (await request(client, runtime)).json()["retryable"] is True
    assert inspected == [True]
    assert runtime[1].store._read("CHAT-MODEL#run-write", "OP#late-model") is None


async def test_result_winning_seal_race_is_not_lost_as_interrupted(client, runtime, ready, monkeypatch):
    original = runtime[1].store.client.transact_write_items
    candidate = {
        "run_id": "run-write",
        "session_id": "session-a",
        "attempt": 1,
        "lease_generation": 1,
        "sandbox_uid": "chat-pod",
        "outcome": "failed",
        "message_id": None,
        "terminal": False,
    }

    def result_first(**kwargs):
        if any("chatLease.expires_at" in item.get("Update", {}).get("UpdateExpression", "") for item in kwargs["TransactItems"]):
            runtime[2].update_item(
                Key=results.TURN, UpdateExpression="SET result_candidate = :result", ExpressionAttributeValues={":result": candidate}
            )
        return original(**kwargs)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", result_first)
    assert (await request(client, runtime)).status_code == 409
    assert "chat_terminal" not in execution(runtime)
    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", original)
    assert (await request(client, runtime)).json()["outcome"] == "failed"


async def test_lease_replacement_racing_terminal_commit_cannot_finish_old_turn(client, runtime, ready, monkeypatch):
    original = runtime[1].store.client.transact_write_items

    def replaced(**kwargs):
        if any("chat_terminal" in item.get("Update", {}).get("UpdateExpression", "") for item in kwargs["TransactItems"]):
            runtime[2].update_item(Key=history.HEADER, UpdateExpression="SET chatLease.generation = :next", ExpressionAttributeValues={":next": 2})
        return original(**kwargs)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", replaced)
    assert (await request(client, runtime)).status_code == 409
    assert results.turn(runtime)["status"] == "accepted"
    assert "chat_terminal" not in execution(runtime)


async def test_corrupt_accounting_fails_closed_and_can_be_reconciled_after_sealing(client, runtime, ready):
    item = model_operation(runtime, run_id="foreign")
    assert (await request(client, runtime)).status_code == 503
    assert header(runtime)["chatLease"]["expires_at"] == 1
    assert "chat_terminal" not in execution(runtime)
    model_operation(runtime)
    assert (await request(client, runtime)).json()["accounting_status"] == "unresolved"
    assert runtime[1].store._read(item["pk"]["S"], item["sk"]["S"])["document"] != item["document"]
