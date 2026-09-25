"""Tool claim/receipt fences against actual DynamoDB transaction semantics."""

# ruff: noqa: F811
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.agentauth.task_tool_receipts import TaskToolReceipts
from src.agentauth.task_tool_routes import authorize_tool
from src.tasks.records import base_item, model_operation_sort_key, task_ops_partition, task_partition
from src.tasks.store import TaskStoreError, _serialize
from tests.tasks.test_store import NOW, _bind_attempt, _request, client, store  # noqa: F401


@pytest.fixture
def journal(store, monkeypatch):
    tool = "repository.read_change"
    request = _request(tool_grants=(tool,))
    store.accept(request)
    attempt = _bind_attempt(store, request.task_id, request.invocation_id)
    identity = SimpleNamespace(
        task_id=request.task_id,
        invocation_id=request.invocation_id,
        generation=1,
        runtime_attempt_id=attempt,
        tenant=request.tenant,
        canonical_principal=request.canonical_principal,
    )
    policy = {"status": "active", "allowed_personas": [request.persona], "allowed_tools": [tool]}
    policies = SimpleNamespace(get=lambda **kw: policy)
    env = {"ADP_TASK_PERSONA_TOOLS": json.dumps({request.persona: [tool]})}
    monkeypatch.setattr("src.agentauth.task_tool_routes.time.time", lambda: NOW.timestamp())
    service = TaskToolReceipts(
        store,
        authorize=lambda identity, tool: authorize_tool(store, policies, identity, tool, env=env),
        catalogue={tool: "read_change"},
        clock=lambda: NOW,
    )
    turn_id = str(uuid.uuid4())
    response = {
        "id": "response_1",
        "status": "completed",
        "output": [
            {
                "id": "item_1",
                "type": "function_call",
                "namespace": "mcp__adp",
                "call_id": "call_1",
                "name": "read_change",
                "arguments": '{"number":1}',
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }
    model = base_item(
        partition=task_ops_partition(request.task_id),
        sort_key=model_operation_sort_key(turn_id),
        record_type="TASK_OPS",
        scope={"tenant": request.tenant, "canonical_principal": request.canonical_principal},
    ) | {
        "task_id": request.task_id,
        "turn_id": turn_id,
        "invocation_id": identity.invocation_id,
        "generation": 1,
        "runtime_attempt_id": attempt,
        "operation_status": "confirmed",
        "request_digest": "a" * 64,
        "responses_response": response,
    }
    store._client.put_item(TableName=store.table_name, Item=_serialize(model))
    claim = dict(identity=identity, turn_id=turn_id, call_id="call_1", tool=tool, arguments={"number": 1})
    return SimpleNamespace(service=service, store=store, identity=identity, claim=claim, model=model, policy=policy)


def test_claim_and_confirmed_receipt_are_durable_and_never_authorize_replay(journal):
    row, created = journal.service.claim(**journal.claim)
    assert created and row["operation_status"] == "pending"
    duplicate, created = journal.service.claim(**journal.claim)
    assert not created and duplicate["owner_token"] == row["owner_token"]
    result = journal.service.settle(
        identity=journal.identity, call_id="call_1", owner_token=row["owner_token"], status="confirmed", content="verified"
    )
    assert result["automatic_replay_permitted"] is False
    assert journal.service.read(journal.identity.task_id, "call_1")["content"] == "verified"
    assert journal.service.claim(**journal.claim)[1] is False
    assert (
        journal.service.settle(identity=journal.identity, call_id="call_1", owner_token=row["owner_token"], status="confirmed", content="verified")
        == result
    )
    with pytest.raises(TaskStoreError, match="immutable"):
        journal.service.settle(identity=journal.identity, call_id="call_1", owner_token=row["owner_token"], status="confirmed", content="forged")


@pytest.mark.parametrize("change", [{"arguments": {"number": 2}}, {"call_id": "foreign"}, {"tool": "repository.merge_change"}])
def test_model_call_cannot_be_rebound_to_other_arguments_or_tools(journal, change):
    with pytest.raises(TaskStoreError):
        journal.service.claim(**{**journal.claim, **change})
    assert journal.service.read(journal.identity.task_id, "call_1") is None


def test_unknown_outcome_is_retained_and_cannot_be_upgraded_or_reexecuted(journal):
    row, _ = journal.service.claim(**journal.claim)
    journal.service.settle(identity=journal.identity, call_id="call_1", owner_token=row["owner_token"], status="unknown")
    existing, created = journal.service.claim(**journal.claim)
    assert not created and existing["operation_status"] == "unknown"
    with pytest.raises(TaskStoreError, match="immutable"):
        journal.service.settle(
            identity=journal.identity, call_id="call_1", owner_token=row["owner_token"], status="confirmed", content="claimed success"
        )


def test_foreign_owner_or_replaced_attempt_cannot_settle(journal):
    row, _ = journal.service.claim(**journal.claim)
    with pytest.raises(TaskStoreError, match="owner"):
        journal.service.settle(identity=journal.identity, call_id="call_1", owner_token="foreign", status="confirmed", content="forged")
    journal.store.bind_runtime_attempt(
        task_id=journal.identity.task_id,
        invocation_id=journal.identity.invocation_id,
        generation=1,
        runtime_attempt_id=str(uuid.uuid4()),
        expected_version=journal.store.read_task(journal.identity.task_id)["version"],
        expected_runtime_attempt_id=journal.identity.runtime_attempt_id,
    )
    with pytest.raises(HTTPException):
        journal.service.settle(identity=journal.identity, call_id="call_1", owner_token=row["owner_token"], status="confirmed", content="late")
    assert journal.service.read(journal.identity.task_id, "call_1")["operation_status"] == "pending"


def test_model_receipt_mutation_racing_claim_cannot_create_tool_operation(journal, monkeypatch):
    original = journal.store._client.transact_write_items

    def mutate(**kwargs):
        journal.store._client.update_item(
            TableName=journal.store.table_name,
            Key=_serialize({"event_id": task_ops_partition(journal.identity.task_id), "arrived_at": journal.model["arrived_at"]}),
            UpdateExpression="SET request_digest=:changed",
            ExpressionAttributeValues={":changed": {"S": "b" * 64}},
        )
        return original(**kwargs)

    monkeypatch.setattr(journal.store._client, "transact_write_items", mutate)
    with pytest.raises(TaskStoreError, match="authority"):
        journal.service.claim(**journal.claim)
    assert journal.service.read(journal.identity.task_id, "call_1") is None


def test_cancellation_racing_claim_is_atomically_fenced(journal, monkeypatch):
    original = journal.store._client.transact_write_items

    def cancel(**kwargs):
        journal.store._client.update_item(
            TableName=journal.store.table_name,
            Key=_serialize({"event_id": task_partition(journal.identity.task_id), "arrived_at": "META"}),
            UpdateExpression="SET #state=:cancel ADD #version :one",
            ExpressionAttributeNames={"#state": "state", "#version": "version"},
            ExpressionAttributeValues={":cancel": {"S": "cancel_requested"}, ":one": {"N": "1"}},
        )
        return original(**kwargs)

    monkeypatch.setattr(journal.store._client, "transact_write_items", cancel)
    with pytest.raises(TaskStoreError):
        journal.service.claim(**journal.claim)
    assert journal.service.read(journal.identity.task_id, "call_1") is None


def test_live_tool_revocation_refuses_claim_without_creating_a_receipt(journal):
    journal.policy["allowed_tools"] = []
    with pytest.raises(HTTPException) as error:
        journal.service.claim(**journal.claim)
    assert error.value.status_code == 403
    assert journal.service.read(journal.identity.task_id, "call_1") is None


@pytest.mark.parametrize("mutation", [{"operation_status": "unknown"}, {"runtime_attempt_id": "foreign"}])
def test_unconfirmed_or_foreign_model_receipt_cannot_authorize_a_tool(journal, mutation):
    journal.store._client.put_item(TableName=journal.store.table_name, Item=_serialize({**journal.model, **mutation}))
    with pytest.raises(TaskStoreError, match="confirmed model"):
        journal.service.claim(**journal.claim)
    assert journal.service.read(journal.identity.task_id, "call_1") is None


def test_catalogue_cannot_alias_different_permissions_to_one_model_function(journal):
    with pytest.raises(TaskStoreError, match="catalogue"):
        TaskToolReceipts(
            journal.store, authorize=lambda *args: None, catalogue={"repository.read_change": "read_change", "repository.merge_change": "read_change"}
        )
