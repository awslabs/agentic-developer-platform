"""DynamoDB and Redis emulators with a substituted provider, never live inference."""

import asyncio
import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import fakeredis.aioredis
import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth import chat_model, chat_model_execution, chat_model_provider
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatLaunchStore
from src.agentauth.workload import VerifiedPod, WorkloadRefusedError
from src.budget.config import budget_config
from src.budget.enforcement_service import BudgetEnforcementService
from src.budget.reservations import ReservationStore, ReservationTarget
from src.shared.schemas.auth import TokenContext
from tests.agentauth.test_chat_data_routes import exchange
from tests.agentauth.test_chat_model_journal import client as client_fixture
from tests.agentauth.test_chat_model_journal import retained_input_table as retained_input_table_fixture
from tests.agentauth.test_chat_model_journal import runtime as runtime_fixture
from tests.agentauth.test_chat_model_journal import store as store_fixture
from tests.agentauth.test_chat_model_journal import sts as sts_fixture
from tests.agentauth.test_chat_model_provider import events
from tests.agentauth.test_chat_model_provider import transport as transport_fixture
from tests.agentauth.test_chat_user_turn import accept, prepare

client = client_fixture
runtime = runtime_fixture
store = store_fixture
sts = sts_fixture
retained_input_table = retained_input_table_fixture
transport = transport_fixture

MODEL = "global.anthropic.claude-sonnet-5"
REQUEST = {"messages": [{"role": "user", "content": "Hello"}], "max_tokens": 16}
DIGEST_VECTORS = json.loads(Path(__file__).with_name("chat_model_digest_vectors.json").read_text())


def tool_request(tool_input):
    return {
        "messages": [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "bounded_tool", "input": tool_input}]}],
        "max_tokens": 16,
    }


@dataclass
class Price:
    ledger_cost_usd: str = "0.01"
    confidence: str = "verified"

    def to_dict(self):
        return {"ledger_cost_usd": self.ledger_cost_usd, "confidence": self.confidence}


@pytest.fixture
async def model(client, runtime, monkeypatch):
    run_hash = hashlib.sha256(b"run-user").hexdigest()
    pod = VerifiedPod(
        "chat-pod",
        f"chat-turn-{run_hash[:12]}-abcde",
        "adp-gateway-agents",
        "adp-chat-sandbox",
        "127.0.0.1",
        image_digest=runtime[5].image_digest,
        run_hash=run_hash,
    )

    class Workloads:
        def verify(self, token):
            if token != "sandbox-token":
                raise WorkloadRefusedError("invalid fixture token")
            return pod

        def verify_bound(self, *, name, uid):
            if name != pod.name or uid != pod.uid:
                raise WorkloadRefusedError("invalid fixture pod")
            return pod

    monkeypatch.setattr(runtime[1], "workloads", Workloads())
    accepted = await accept(client, runtime, prepare(runtime), pod_name=pod.name)
    assert accepted.status_code == 200, accepted.text
    launch = ChatLaunchStore(runtime[1].store).load("run-user")
    bootstrap = await exchange(client, **{"X-Adp-Workload-Token": "sandbox-token"})
    assert bootstrap.status_code == 200, bootstrap.text
    client.headers.update({"Authorization": "Bearer " + bootstrap.json()["capability"], "X-Adp-Workload-Token": "sandbox-token"})
    redis = fakeredis.aioredis.FakeRedis()
    reservations = ReservationStore(None, ttl_seconds=3600, client=redis)
    target = ReservationTarget(
        org_id="tenant",
        entity_type="user",
        entity_id="human",
        period_type="daily",
        period_start="fixture",
        headroom_usd=Decimal("1"),
    )
    context = TokenContext(
        user_id="human",
        org_id="tenant",
        team_id="team",
        department_id="department",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    context._budget_enforcement_enabled = True
    enforcement = BudgetEnforcementService()
    monkeypatch.setattr(enforcement, "_get_reservations", lambda: reservations)
    monkeypatch.setattr(budget_config, "budget_reservation_enabled", True)
    enforcement.prepare_enforcement_context = AsyncMock(return_value=None)

    async def check(context, cost, *, request_id, run_id):
        context._budget_admission_targets = [target]
        result = await reservations.reserve(request_id, cost, [target])
        return SimpleNamespace(allowed=result.admitted)

    enforcement.check_budget_hierarchy = AsyncMock(side_effect=check)
    readiness = AsyncMock(return_value=(context, SimpleNamespace(is_platform=True, account_id=None, region="us-east-1")))
    provider = AsyncMock(
        return_value={
            "content": [{"type": "text", "text": "Hello back"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 4, "output_tokens": 2},
            "price": Price(),
            "provider_request_id": "fixture-provider-receipt",
        }
    )
    monkeypatch.setattr(chat_model_execution, "quote_request", AsyncMock(return_value=SimpleNamespace(total_usd=Decimal("0.1"))))
    monkeypatch.setattr(chat_model_execution, "confirm_quote_spendable", AsyncMock(return_value=None))
    service = chat_model_execution.ChatModelExecution(
        runtime[1],
        db=object(),
        readiness=readiness,
        provider=provider,
        enforcement=enforcement,
        usage_writer=AsyncMock(),
        event_writer=AsyncMock(),
        gap_writer=AsyncMock(),
        clock=lambda: runtime[-1],
    )
    monkeypatch.setattr(chat_model, "ChatModelExecution", lambda *_: service)
    policy = AsyncMock(
        return_value={
            "result": {
                "context": {"lease_generation": 1},
                "model_policy": {
                    "posture": "enforcing",
                    "posture_verified": True,
                    "status": "proposed",
                    "assertion": "fixture-policy",
                    "decision": {
                        "runtime_posture": "enforcing",
                        "invocation_id": "run-user",
                        "tenant_id": "tenant",
                        "principal_kind": "human",
                        "principal_id": "human",
                        "resolved_model_id": MODEL,
                    },
                },
            },
            "assertion": "fixture-response",
        }
    )
    monkeypatch.setattr(chat_model, "resolved_model_response", policy)
    yield SimpleNamespace(
        service=service,
        launch=launch,
        provider=provider,
        enforcement=enforcement,
        context=context,
        reservations=reservations,
        target=target,
        redis=redis,
        runtime=runtime,
        client=client,
        policy=policy,
    )
    await redis.aclose()


async def execute(model, *, request=None, authorize=None, operation_id="call-1", on_event=None):
    return await model.service.execute(
        launch=model.launch,
        operation_id=operation_id,
        model_id=MODEL,
        request=deepcopy(REQUEST) if request is None else request,
        authorize=authorize or AsyncMock(),
        on_event=on_event,
    )


async def invoke(model, **changes):
    return await model.client.post(
        "/v1/chat/model/invoke",
        json={
            "run_id": "run-user",
            "session_id": "session-a",
            "operation_id": "call-1",
            "request": REQUEST,
            **changes,
        },
    )


async def reserved_amount(model):
    operation = model.service.journal._read("run-user", "call-1")
    value = await model.redis.hget(model.target.key(), operation["accounting_id"])
    assert value is not None
    return Decimal(value.decode().split(":")[0])


async def test_owner_route_calls_provider_once_and_settles_measured_usage(model):
    response = await invoke(model)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["status"] == "confirmed" and receipt["reservation_status"] == "settled"
    assert receipt["usage"] == {"input_tokens": 4, "output_tokens": 2, "estimated_usd": "0.01"}
    assert response.headers["cache-control"] == "no-store"
    assert (await invoke(model)).json() == receipt
    model.provider.assert_awaited_once()
    call = model.provider.await_args.kwargs
    assert call["identity"].tenant == "tenant" and call["binding"]["model_id"] == MODEL
    assert call["operation_id"].startswith("chat-")
    assert call["operation_id"] != "call-1"
    assert model.service.usage_writer.await_args.kwargs["agent_run_id"] == "run-user"
    model.service.event_writer.assert_awaited_once()
    model.service.gap_writer.assert_not_awaited()
    stored = await model.redis.hget(model.target.key(), call["operation_id"])
    assert Decimal(stored.decode().split(":")[0]) == Decimal("0.01")
    assert "provider_request_id" not in receipt and "accounting_id" not in receipt


@pytest.mark.parametrize("vector", DIGEST_VECTORS, ids=lambda vector: vector["name"])
async def test_receipt_digest_matches_ecmascript_client_for_nested_tool_input(model, vector):
    wire_input = vector.get("wireInput", json.dumps(vector["input"]))
    parsed = chat_model.SandboxProviderRequest.model_validate_json(json.dumps(tool_request(json.loads(wire_input))))
    request = parsed.model_dump(exclude_none=True)
    response = await invoke(model, request=request)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["status"] == "confirmed" and receipt["reservation_status"] == "settled"
    assert receipt["request_digest"] == vector["digest"]
    assert (await invoke(model, request=request)).json() == receipt
    model.provider.assert_awaited_once()
    assert model.provider.await_args.kwargs["request"] == request


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf"), "\ud800", 10**400])
async def test_noncanonical_input_refused_before_model_claim_or_provider(model, invalid):
    with pytest.raises(ChatAuthorizationRefusedError, match="not canonical JSON"):
        await execute(model, request=tool_request({"value": invalid}))
    assert model.service.journal._read("run-user", "call-1") is None
    model.provider.assert_not_awaited()
    model.enforcement.check_budget_hierarchy.assert_not_awaited()


async def test_forwarded_request_uses_the_same_binary64_values_as_its_digest(model):
    request = tool_request({"amount": 2**53 + 1, "reference": "9007199254740993", "enabled": True})
    receipt = await execute(model, request=request)
    expected = tool_request({"amount": 2**53, "reference": "9007199254740993", "enabled": True})
    assert receipt["status"] == "confirmed" and receipt["reservation_status"] == "settled"
    assert model.provider.await_args.kwargs["request"] == expected
    assert receipt["request_digest"] == hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert await execute(model, request=expected) == receipt
    assert request["messages"][0]["content"][0]["input"]["amount"] == 2**53 + 1
    model.provider.assert_awaited_once()


@pytest.mark.parametrize("amount_literal", ["0.000001", "9007199254740992", "100000000000000000000", "999999999999999900000", "1e+21"])
@pytest.mark.parametrize("extra_bytes", [0, 1])
async def test_canonical_utf8_frame_bound_matches_client_before_claim(model, extra_bytes, amount_literal):
    empty = json.dumps(tool_request({"amount": "NUMBER", "padding": ""}), sort_keys=True, separators=(",", ":")).replace('"NUMBER"', amount_literal)
    remaining = 65_536 - len(empty.encode()) + extra_bytes
    request = tool_request({"amount": json.loads(amount_literal), "padding": "é" * (remaining // 2) + "a" * (remaining % 2)})
    if extra_bytes:
        with pytest.raises(ChatAuthorizationRefusedError, match="exceeds frame bound"):
            await execute(model, request=request)
        assert model.service.journal._read("run-user", "call-1") is None
        model.provider.assert_not_awaited()
        model.enforcement.check_budget_hierarchy.assert_not_awaited()
    else:
        response = await invoke(model, request=request)
        assert response.status_code == 200, response.text
        assert response.json()["reservation_status"] == "settled"
        model.provider.assert_awaited_once()


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"run_id": "other-run"}, 404),
        ({"session_id": "other-session"}, 404),
        ({"principal_id": "other-human"}, 422),
        ({"lease_generation": 2}, 422),
        ({"request": {**REQUEST, "model": "other-model"}}, 422),
        ({"request": {**REQUEST, "credentials": "forged"}}, 422),
        ({"request": {**REQUEST, "tools": [{"type": "computer_20250124", "name": "computer"}]}}, 422),
    ],
)
async def test_route_rejects_scope_model_credentials_and_server_tool_overrides(model, changes, status):
    assert (await invoke(model, **changes)).status_code == status
    model.provider.assert_not_awaited()


async def test_policy_failure_never_uses_direct_provider_fallback(model):
    model.policy.return_value["result"]["model_policy"]["posture"] = "report-only"
    assert (await invoke(model)).status_code == 503
    model.provider.assert_not_awaited()


async def test_changed_request_cannot_replay_operation(model):
    await execute(model)
    with pytest.raises(ChatAuthorizationRefusedError):
        await execute(model, request={**REQUEST, "max_tokens": 8})
    model.provider.assert_awaited_once()


async def test_concurrent_retry_cannot_authorize_a_second_handoff(model):
    started, release = asyncio.Event(), asyncio.Event()
    result = model.provider.return_value

    async def provider(*args, **kwargs):
        started.set()
        await release.wait()
        return result

    model.provider.side_effect = provider
    running = asyncio.create_task(execute(model))
    await asyncio.wait_for(started.wait(), timeout=10)
    duplicate = await execute(model)
    assert duplicate["status"] == "running" and duplicate["handoff"] == "prepared"
    release.set()
    assert (await running)["status"] == "confirmed"
    model.provider.assert_awaited_once()


@pytest.mark.parametrize("failure", ["budget", "quote", "lease", "reservation"])
async def test_pre_provider_denial_never_sends(model, monkeypatch, failure):
    if failure == "budget":
        model.enforcement.check_budget_hierarchy.side_effect = None
        model.enforcement.check_budget_hierarchy.return_value = SimpleNamespace(allowed=False)
    elif failure == "quote":
        chat_model_execution.confirm_quote_spendable.return_value = object()
    elif failure == "reservation":
        monkeypatch.setattr(model.enforcement, "_reserve_or_degrade", AsyncMock(return_value=object()))

    async def authorize():
        if failure == "lease":
            header = model.runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]
            header["chatLease"]["generation"] += 1
            model.runtime[2].put_item(Item=header)

    result = await execute(model, authorize=authorize)
    assert result["status"] == "rejected" and result["handoff"] == "not_started"
    model.provider.assert_not_awaited()


@pytest.mark.parametrize("failure", ["timeout", "usage", "price", "receipt"])
async def test_uncertain_provider_retains_hold_and_never_replays(model, failure):
    if failure == "timeout":
        model.provider.side_effect = TimeoutError("private provider details")
    elif failure == "usage":
        model.provider.return_value["usage"] = {}
    elif failure == "price":
        model.provider.return_value["price"] = Price("2")
    else:
        model.provider.return_value["provider_request_id"] = None
    receipt = await execute(model)
    assert receipt["status"] == "unknown" and receipt["usage"] is None
    assert receipt["reservation_status"] == "unknown"
    assert await execute(model) == receipt
    model.provider.assert_awaited_once()
    model.service.usage_writer.assert_not_awaited()
    assert await reserved_amount(model) == Decimal("0.1")
    model.service.gap_writer.assert_awaited_once()


@pytest.mark.parametrize("failure", ["usage_log", "event", "settlement", "unverified_price"])
async def test_accounting_failure_keeps_receipt_and_does_not_claim_settled(model, monkeypatch, failure):
    if failure == "usage_log":
        model.service.usage_writer.side_effect = RuntimeError("usage unavailable")
    elif failure == "event":
        model.service.event_writer.side_effect = RuntimeError("event unavailable")
    elif failure == "settlement":
        monkeypatch.setattr(model.enforcement, "reconcile_reservation", AsyncMock())
    else:
        model.provider.return_value["price"] = Price("0", "unknown")
    receipt = await execute(model)
    assert receipt["status"] == "confirmed" and receipt["reservation_status"] == "reserved"
    assert receipt["usage"]["input_tokens"] == 4
    assert await reserved_amount(model) == Decimal("0.1")
    assert await execute(model) == receipt
    model.provider.assert_awaited_once()


async def test_cancelled_request_records_uncertain_provider_outcome(model):
    model.provider.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await execute(model)
    assert (await execute(model))["status"] == "unknown"
    model.provider.assert_awaited_once()


async def test_session_end_during_provider_keeps_usage_but_withholds_response(model):
    result = model.provider.return_value

    async def end_session(*args, **kwargs):
        header = model.runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]
        header["status"] = "ended"
        model.runtime[2].put_item(Item=header)
        return result

    model.provider.side_effect = end_session
    assert (await invoke(model)).status_code == 404
    model.service.usage_writer.assert_awaited_once()
    assert model.service.journal._read("run-user", "call-1")["reservation_status"] == "settled"


async def test_actual_provider_adapter_transports_custom_tool_blocks_and_receipts(model, transport):
    document = {
        "content": [{"type": "tool_use", "id": "call_history", "name": "history_read", "input": {}}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 4, "output_tokens": 2},
    }
    transport.stream.documents = iter(events(document["content"][0], [{"type": "input_json_delta", "partial_json": "{}"}], "tool_use"))
    transport.pricing.return_value = Price()
    model.service.provider = chat_model_provider.invoke_chat_messages
    request = {**REQUEST, "tools": [{"name": "history_read", "input_schema": {"type": "object"}}]}
    receipt = await execute(model, request=request)
    assert receipt["status"] == "confirmed" and receipt["reservation_status"] == "settled"
    assert receipt["content"] == document["content"] and receipt["stop_reason"] == "tool_use"
    assert transport.factory.call_args.args == ("bedrock-runtime",)
    assert transport.factory.call_args.kwargs["config"].retries == {"total_max_attempts": 1}
    assert transport.upstream.invoke_model_with_response_stream.call_args.kwargs["modelId"] == MODEL
    sent = json.loads(transport.upstream.invoke_model_with_response_stream.call_args.kwargs["body"])
    assert sent == {"anthropic_version": "bedrock-2023-05-31", **request}
    assert transport.pricing.await_args.kwargs["org_id"] == "tenant"
    assert model.service.journal._read("run-user", "call-1")["provider_request_id"] == "fixture-upstream"


async def test_stream_events_precede_settlement_and_replay_never_reemits(model, transport):
    model.service.provider = chat_model_provider.invoke_chat_messages
    transport.pricing.return_value = Price()
    delivered = []

    async def receive(event):
        stored = model.service.journal._read("run-user", "call-1")
        assert stored["status"] == "running" and stored["reservation_status"] == "reserved"
        delivered.append(event)

    receipt = await execute(model, on_event=receive)
    assert receipt["status"] == "confirmed" and receipt["reservation_status"] == "settled"
    assert delivered == [{"type": "text_delta", "index": 0, "text": "Hello"}]
    assert await execute(model, on_event=receive) == receipt
    assert len(delivered) == 1
    transport.upstream.invoke_model_with_response_stream.assert_called_once()
    transport.upstream.invoke_model.assert_not_called()
    model.service.usage_writer.assert_awaited_once()


@pytest.mark.parametrize("change", ["session_end", "lease_replaced", "abort"])
async def test_current_route_authority_stops_silent_provider_and_preserves_spend_hold(model, change):
    started, stopped = asyncio.Event(), asyncio.Event()

    async def waiting_provider(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    model.provider.side_effect = waiting_provider
    running = asyncio.create_task(invoke(model))
    await asyncio.wait_for(started.wait(), 5)
    header = model.runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]
    if change == "abort":
        model.runtime[1].store.authority.record_abort_intent(
            invocation_id="run-user", tenant_id="tenant", attempt=1, command_id="abort-a", body_digest="a" * 64
        )
    elif change == "session_end":
        header["status"] = "ended"
    else:
        header["chatLease"]["generation"] += 1
    model.runtime[2].put_item(Item=header)
    response = await asyncio.wait_for(running, 5)
    assert response.status_code == 404
    assert stopped.is_set()
    stored = model.service.journal._read("run-user", "call-1")
    assert stored["status"] == "unknown" and stored["reservation_status"] == "unknown"
    assert stored["automatic_replay_permitted"] is False
    assert await reserved_amount(model) == Decimal("0.1")
    model.service.gap_writer.assert_awaited_once()
    model.service.usage_writer.assert_not_awaited()
    model.provider.assert_awaited_once()


async def test_stream_delivery_rechecks_authority_before_each_delta(model):
    delivered = AsyncMock()

    async def streaming_provider(**kwargs):
        await kwargs["on_event"]({"type": "text_delta", "index": 0, "text": "private"})

    model.provider.side_effect = streaming_provider
    authorize = AsyncMock(side_effect=[None, ChatAuthorizationRefusedError("revoked")])
    receipt = await execute(model, authorize=authorize, on_event=delivered)
    assert receipt["status"] == "unknown" and receipt["reservation_status"] == "unknown"
    delivered.assert_not_awaited()
    model.service.gap_writer.assert_awaited_once()


async def test_selected_destination_failure_has_no_platform_credential_fallback(model, monkeypatch):
    model.service.readiness = chat_model_execution.resolve_chat_provider
    policy = AsyncMock(return_value=SimpleNamespace(context=model.context, routing_user_id="human"))
    monkeypatch.setattr(chat_model_execution, "_resolve_active_allowlist_policy", policy)
    monkeypatch.setattr(chat_model_execution, "production_model_resolver", lambda _: SimpleNamespace(check_model_access=Mock()))
    destination = AsyncMock(side_effect=RuntimeError("destination unavailable"))
    monkeypatch.setattr(chat_model_execution.bedrock_routing_resolver, "resolve", destination)
    receipt = await execute(model)
    assert receipt["status"] == "rejected"
    assert policy.await_args.kwargs["tenant_id"] == "tenant" and policy.await_args.kwargs["principal_id"] == "human"
    assert destination.await_args.kwargs["user_id"] == "human"
    model.provider.assert_not_awaited()


async def test_lost_reservation_response_retains_unknown_accounting_not_zero(model, monkeypatch):
    original = model.enforcement.check_budget_hierarchy.side_effect

    async def lose_reply(*args, **kwargs):
        await original(*args, **kwargs)
        raise TimeoutError("reservation reply unavailable")

    model.enforcement.check_budget_hierarchy.side_effect = lose_reply
    monkeypatch.setattr(model.service, "_settle", AsyncMock(side_effect=RuntimeError("settlement unavailable")))
    receipt = await execute(model)
    assert receipt["status"] == "rejected" and receipt["handoff"] == "not_started"
    assert receipt["reservation_status"] == "unknown"
    assert model.service.journal._read("run-user", "call-1")["accounting_id"].startswith("chat-")
    assert await reserved_amount(model) == Decimal("0.1")
    model.provider.assert_not_awaited()


async def test_accounting_gap_survives_reservation_expiry(db_session):
    from src.budget.enforcement_settings import read_enforcement

    await chat_model_execution.record_chat_accounting_gap(db_session, "chat-uncertain", "global")
    await chat_model_execution.record_chat_accounting_gap(db_session, "chat-uncertain", "global")
    assert (await read_enforcement(db_session)).accounting_incomplete is True


async def test_lease_replacement_racing_handoff_transaction_prevents_spending(model, monkeypatch):
    original = model.runtime[1].store.client.transact_write_items

    def replace_lease(**kwargs):
        update = kwargs["TransactItems"][0].get("Update", {})
        document = update.get("ExpressionAttributeValues", {}).get(":updated", {}).get("S")
        if document and json.loads(document).get("handoff") == "prepared":
            header = model.runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]
            header["chatLease"]["generation"] += 1
            model.runtime[2].put_item(Item=header)
        return original(**kwargs)

    monkeypatch.setattr(model.runtime[1].store.client, "transact_write_items", replace_lease)
    result = await execute(model)
    assert result["status"] == "rejected" and result["handoff"] == "not_started"
    model.provider.assert_not_awaited()


async def test_lost_receipt_commit_reply_recovers_durable_result_without_replay(model, monkeypatch):
    original = model.runtime[1].store.client.transact_write_items

    def lose_reply(**kwargs):
        result = original(**kwargs)
        update = kwargs["TransactItems"][0].get("Update", {})
        document = update.get("ExpressionAttributeValues", {}).get(":updated", {}).get("S")
        if document and json.loads(document).get("status") == "confirmed":
            raise EndpointConnectionError(endpoint_url="https://storage.test")
        return result

    monkeypatch.setattr(model.runtime[1].store.client, "transact_write_items", lose_reply)
    result = await execute(model)
    assert result["status"] == "confirmed" and result["reservation_status"] == "reserved"
    assert result["usage"]["input_tokens"] == 4
    assert await execute(model) == result
    model.provider.assert_awaited_once()
    model.service.gap_writer.assert_awaited_once()
    model.service.usage_writer.assert_not_awaited()
