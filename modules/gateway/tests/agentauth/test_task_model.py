"""Real canonical turns and DynamoDB operations with substituted external inference."""

# ruff: noqa: F811
import uuid
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agentauth import task_model as module
from src.agentauth.task_turns import TaskTurnStore
from src.budget.reservations import ReservationTarget
from src.tasks.records import payload_digest
from src.tasks.store import TaskStoreError
from tests.agentauth.test_task_runtime import _attempt_identity, runtime  # noqa: F401
from tests.tasks.test_store import NOW, client, store  # noqa: F401


@dataclass
class Price:
    ledger_cost_usd: str = "0.001"
    confidence: str = "verified"

    def to_dict(self):
        return {"ledger_cost_usd": self.ledger_cost_usd, "confidence": self.confidence}


@pytest.fixture
def model(runtime, monkeypatch):
    identity = _attempt_identity(runtime)
    repository = runtime[0].repository
    turn_id = str(uuid.uuid4())
    TaskTurnStore(repository, clock=lambda: NOW).commit(identity=identity, request_id=turn_id, expected_transcript_version=1)
    grant = runtime[0]._grant(identity.tenant, identity.invocation_id, identity.generation)
    budget = SimpleNamespace(
        _target=lambda **kw: ReservationTarget(
            org_id="pilot", entity_type="run", entity_id="task", period_type="lifetime", period_start="now", headroom_usd=Decimal("1")
        ),
        _initialize=AsyncMock(),
        verify_settlement=AsyncMock(),
    )
    enforcement = SimpleNamespace(check_budget_hierarchy=AsyncMock(return_value=SimpleNamespace(allowed=True)), reconcile_reservation=AsyncMock())
    provider = AsyncMock(
        return_value={
            "content": [{"type": "text", "text": "Done"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 4, "output_tokens": 1},
            "price": Price(),
            "provider_request_id": "provider-actual",
        }
    )
    context = SimpleNamespace()
    readiness = AsyncMock(
        return_value=(grant["model_binding"], SimpleNamespace(context=context), SimpleNamespace(region="us-east-1", account_id="123456789012"))
    )
    monkeypatch.setattr(module, "quote_request", AsyncMock(return_value=SimpleNamespace(total_usd=Decimal("0.01"))))
    monkeypatch.setattr(module, "confirm_quote_spendable", AsyncMock(return_value=None))
    service = module.TaskModel(
        repository,
        db=object(),
        budget=budget,
        readiness=readiness,
        provider=provider,
        enforcement=enforcement,
        usage_writer=AsyncMock(),
        event_writer=AsyncMock(),
        clock=lambda: NOW,
    )
    request = {"messages": [{"role": "user", "content": [{"type": "text", "text": "Investigate"}]}], "max_tokens": 16}
    return SimpleNamespace(
        service=service, identity=identity, turn_id=turn_id, request=request, provider=provider, enforcement=enforcement, repository=repository
    )


async def execute(model, **kwargs):
    return await model.service.execute(
        identity=model.identity, turn_id=model.turn_id, request_digest=payload_digest(model.request), request=model.request, **kwargs
    )


@pytest.mark.asyncio
async def test_confirmed_operation_replay_never_calls_provider_twice(model):
    first = await execute(model)
    assert first["operation_status"] == "confirmed"
    assert first["usage"]["output_tokens"] == 1
    assert first["reservation_status"] == "settled"
    model.service.budget.verify_settlement.assert_awaited_once()
    assert await execute(model) == first
    model.provider.assert_awaited_once()
    model.service.usage_writer.assert_awaited_once()
    stored = model.service._read(model.identity.task_id, model.turn_id)
    assert stored["provider_request_id"] == "provider-actual" and stored["usage_logged"]
    assert stored["pricing_decision"]["ledger_cost_usd"] == "0.001"


@pytest.mark.asyncio
async def test_uncertain_provider_response_is_never_replayed_or_released(model):
    model.provider.side_effect = TimeoutError()
    first = await execute(model)
    assert first["operation_status"] == "unknown" and first["usage"] is None
    assert await execute(model) == first
    model.provider.assert_awaited_once()
    model.enforcement.reconcile_reservation.assert_not_awaited()


@pytest.mark.asyncio
async def test_budget_denial_prevents_send(model):
    model.enforcement.check_budget_hierarchy.return_value.allowed = False
    receipt = await execute(model)
    assert receipt["operation_status"] == "rejected"
    assert receipt["error_code"] == "budget_exceeded"
    model.provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_request_cannot_reuse_turn(model):
    await execute(model)
    model.request["max_tokens"] = 15
    with pytest.raises(TaskStoreError, match="another request"):
        await execute(model)
    model.provider.assert_awaited_once()


@pytest.mark.asyncio
async def test_usage_failure_preserves_confirmed_provider_receipt(model):
    model.service.usage_writer.side_effect = RuntimeError("ledger unavailable")
    first = await execute(model)
    assert first["operation_status"] == "confirmed" and first["reservation_status"] == "reserved"
    assert await execute(model) == first
    model.enforcement.reconcile_reservation.assert_not_awaited()
    model.provider.assert_awaited_once()


@pytest.mark.asyncio
async def test_uncommitted_turn_cannot_invoke(model):
    model.turn_id = str(uuid.uuid4())
    with pytest.raises(TaskStoreError, match="not been committed"):
        await execute(model)
    model.provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_failure_keeps_hold_without_fabricating_usage_settlement(model):
    model.service.event_writer.side_effect = RuntimeError("S3 unavailable")
    receipt = await execute(model)
    assert receipt["operation_status"] == "confirmed" and receipt["reservation_status"] == "reserved"
    assert not model.service._read(model.identity.task_id, model.turn_id).get("usage_logged")
    model.enforcement.reconcile_reservation.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciliation_failure_cannot_claim_settled(model):
    model.service.budget.verify_settlement.side_effect = RuntimeError("Redis unavailable")
    receipt = await execute(model)
    assert receipt["operation_status"] == "confirmed" and receipt["reservation_status"] == "reserved"
    assert model.service._read(model.identity.task_id, model.turn_id)["usage_logged"]


@pytest.mark.asyncio
async def test_terminal_admission_release_uses_only_durable_price(model):
    from src.agentauth.task_budget_settlement import settle_task_admission
    from src.tasks.records import task_partition
    from src.tasks.store import _serialize

    await execute(model)
    model.repository._client.update_item(
        TableName=model.repository.table_name,
        Key=_serialize({"event_id": task_partition(model.identity.task_id), "arrived_at": "META"}),
        UpdateExpression="SET #state = :terminal, child_exit = :stop, budget_reservation = :receipt",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues=_serialize({":terminal": "completed", ":stop": {"confirmed": True}, ":receipt": {"reservation_id": "admission"}}),
    )
    budget = SimpleNamespace(settle_admission=AsyncMock())
    assert await settle_task_admission(model.repository, model.identity, budget=budget)
    budget.settle_admission.assert_awaited_once_with({"reservation_id": "admission"}, actual_usd=Decimal("0.001"))


@pytest.mark.asyncio
async def test_terminal_unknown_model_preserves_admission_hold(model):
    from src.agentauth.task_budget_settlement import settle_task_admission
    from src.tasks.records import task_partition
    from src.tasks.store import _serialize

    model.provider.side_effect = TimeoutError()
    await execute(model)
    model.repository._client.update_item(
        TableName=model.repository.table_name,
        Key=_serialize({"event_id": task_partition(model.identity.task_id), "arrived_at": "META"}),
        UpdateExpression="SET #state = :terminal, child_exit = :stop, budget_reservation = :receipt",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues=_serialize({":terminal": "failed", ":stop": {"confirmed": True}, ":receipt": {"reservation_id": "admission"}}),
    )
    budget = SimpleNamespace(settle_admission=AsyncMock())
    assert not await settle_task_admission(model.repository, model.identity, budget=budget)
    budget.settle_admission.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_during_budget_check_prevents_provider_handoff(model):
    from src.tasks.records import task_partition
    from src.tasks.store import _serialize

    async def cancel_during_check(*args, **kwargs):
        model.repository._client.update_item(
            TableName=model.repository.table_name,
            Key=_serialize({"event_id": task_partition(model.identity.task_id), "arrived_at": "META"}),
            UpdateExpression="SET #state = :cancel ADD #version :one",
            ExpressionAttributeNames={"#state": "state", "#version": "version"},
            ExpressionAttributeValues=_serialize({":cancel": "cancel_requested", ":one": 1}),
        )
        return SimpleNamespace(allowed=True)

    model.enforcement.check_budget_hierarchy.side_effect = cancel_during_check
    assert (await execute(model))["operation_status"] == "rejected"
    model.provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_racing_prepared_transaction_blocks_send(model, monkeypatch):
    from src.tasks.records import task_partition
    from src.tasks.store import _serialize

    transact = model.repository._client.transact_write_items

    def race(**kwargs):
        if kwargs["TransactItems"][0].get("Put", {}).get("Item", {}).get("handoff") == {"S": "prepared"}:
            monkeypatch.setattr(model.repository._client, "transact_write_items", transact)
            model.repository._client.update_item(
                TableName=model.repository.table_name,
                Key=_serialize({"event_id": task_partition(model.identity.task_id), "arrived_at": "META"}),
                UpdateExpression="SET #state = :cancel ADD #version :one",
                ExpressionAttributeNames={"#state": "state", "#version": "version"},
                ExpressionAttributeValues=_serialize({":cancel": "cancel_requested", ":one": 1}),
            )
        return transact(**kwargs)

    monkeypatch.setattr(model.repository._client, "transact_write_items", race)
    assert (await execute(model))["operation_status"] == "rejected"
    model.provider.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["confirmed", "unknown", "version_race", "event_race"])
async def test_consumed_command_tracks_provider_handoff_without_replaying(model, monkeypatch, outcome):
    from datetime import timedelta

    from src.tasks.records import task_partition
    from src.tasks.store import _serialize
    from src.tasks.task_commands import TaskCommands

    commands = TaskCommands(model.repository)
    command_id = str(uuid.uuid4())
    commands.admit(
        task_id=model.identity.task_id,
        command_id=command_id,
        kind="input",
        payload={"text": "follow up"},
        principal=model.identity.canonical_principal,
        tenant=model.identity.tenant,
        expires_at=NOW + timedelta(minutes=5),
    )
    model.turn_id = str(uuid.uuid4())
    TaskTurnStore(model.repository, clock=lambda: NOW).commit(identity=model.identity, request_id=model.turn_id, expected_transcript_version=2)
    assert commands.commands(model.identity.task_id)[0]["handoff"] == "not_started"
    if outcome == "unknown":
        model.provider.side_effect = TimeoutError()
    if outcome == "event_race":
        from src.tasks.records import TaskState

        task = model.repository.read_task(model.identity.task_id)
        model.repository.transition(task_id=model.identity.task_id, expected_version=int(task["version"]), target_state=TaskState.RUNNING)
    if outcome in {"version_race", "event_race"}:
        original = model.repository._client.transact_write_items
        raced = False

        def write(**kwargs):
            nonlocal raced
            first = kwargs["TransactItems"][0].get("Put", {}).get("Item", {})
            if first.get("handoff") == {"S": "confirmed"} and not raced:
                raced = True
                if outcome == "version_race":
                    model.repository._client.update_item(
                        TableName=model.repository.table_name,
                        Key=_serialize({"event_id": task_partition(model.identity.task_id), "arrived_at": "META"}),
                        UpdateExpression="ADD #version :one",
                        ExpressionAttributeNames={"#version": "version"},
                        ExpressionAttributeValues=_serialize({":one": 1}),
                    )
                else:
                    model.repository.append_report(
                        task_id=model.identity.task_id,
                        invocation_id=model.identity.invocation_id,
                        generation=model.identity.generation,
                        runtime_attempt_id=model.identity.runtime_attempt_id,
                        report_id=str(uuid.uuid4()),
                        kind="progress.updated",
                        data={"message": "Still checking", "stage": "analysis"},
                    )
            return original(**kwargs)

        monkeypatch.setattr(model.repository._client, "transact_write_items", write)
    receipt = await execute(model)
    expected = "unknown" if outcome == "unknown" else "confirmed"
    assert receipt["handoff"] == expected
    assert commands.commands(model.identity.task_id)[0]["handoff"] == expected
    events = model.repository.read_events(task_id=model.identity.task_id)
    assert [e["data"]["handoff"] for e in events if e["type"] == "command.updated"] == ["prepared", expected]
    assert await execute(model) == receipt
    model.provider.assert_awaited_once()
    assert len(model.repository.read_events(task_id=model.identity.task_id)) == len(events)
    if outcome == "event_race":
        assert any(e["type"] == "progress.updated" for e in events)
