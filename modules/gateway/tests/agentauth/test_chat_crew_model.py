"""A verified persistent pod cannot present another pod's crew run for inference."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import boto3
import pytest
from sqlalchemy import select
from starlette.responses import JSONResponse

from src.agentauth import chat_model, chat_model_execution
from src.agentauth.chat_crew_run import CrewRunStore
from src.shared.models.usage import UsageLog
from src.shared.schemas.auth import TokenContext
from tests.agentauth import test_chat_turn_mailbox as fixtures
from tests.agentauth.test_chat_model_execution import Price
from tests.agentauth.test_chat_user_turn import retained_input_table as retained_input_table_fixture

client = fixtures.client
runtime = fixtures.runtime
store = fixtures.store
sts = fixtures.sts
retained_input_table = retained_input_table_fixture
mailbox = fixtures.mailbox

pytestmark = pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)


@pytest.fixture
async def bound(mailbox, monkeypatch):
    client, runtime, token, state, _ = mailbox
    registry = boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName="crew-model-runs",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}, {"AttributeName": "arrived_at", "AttributeType": "S"}],
    )
    monkeypatch.setattr(
        "src.proxy.bedrock_principal.get_settings",
        lambda: SimpleNamespace(webhook_events_table=registry.name, aws_region="us-east-1"),
    )
    monkeypatch.setattr("src.proxy.bedrock_principal._get_context_table", lambda: runtime[2])
    lease = runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"}, ConsistentRead=True)["Item"]["chatLease"]
    now = int(datetime.now(UTC).timestamp())
    crew = CrewRunStore(registry)
    run_id = crew.mint(
        tenant_id="tenant",
        user_id="human",
        session_id="session-a",
        lease_generation=int(lease["generation"]),
        lease_expires_at=int(lease["expires_at"]),
        target_kind="eks-pod",
        target_ref=state["pod"].name,
        now=now,
    )
    policy = AsyncMock(
        return_value=JSONResponse(
            {
                "result": {
                    "context": {"lease_generation": int(lease["generation"])},
                    "model_policy": {"decision": {"resolved_model_id": "test-model"}},
                }
            }
        )
    )
    provider = AsyncMock()

    async def execute(*, authorize, **kwargs):
        await authorize()
        await provider()
        return {"status": "confirmed"}

    monkeypatch.setattr(chat_model, "sandbox_model_decision", policy)
    monkeypatch.setattr(chat_model, "ChatModelExecution", lambda *_: SimpleNamespace(execute=execute))
    return SimpleNamespace(
        client=client,
        runtime=runtime,
        token=token,
        state=state,
        crew=crew,
        registry=registry,
        run_id=run_id,
        lease=lease,
        policy=policy,
        provider=provider,
    )


async def invoke(bound, run_id=None, *, streaming=False):
    headers = {"Authorization": f"Bearer {bound.token}", "X-Adp-Workload-Token": "sandbox-token"}
    if run_id is not None:
        headers["x-agent-runid"] = run_id
    if streaming:
        headers["accept"] = "application/x-ndjson"
    return await bound.client.post(
        "/v1/chat/model/invoke",
        json={
            "run_id": "run-user",
            "session_id": "session-a",
            "operation_id": "call-1",
            "request": {"messages": [{"role": "user", "content": "Hello"}], "max_tokens": 16},
        },
        headers=headers,
    )


async def test_own_pod_crew_run_authorizes_scoped_inference(bound):
    row = await chat_model.read_routing_run(bound.run_id, expected_sandbox_uid=bound.state["pod"].uid, expected_session_run_id="run-user")
    assert row is not None, bound.runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]
    assert row["target_ref"] == bound.state["pod"].name
    response = await invoke(bound, bound.run_id)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"
    bound.policy.assert_awaited_once()
    bound.provider.assert_awaited_once()


@pytest.mark.parametrize("streaming", [False, True])
async def test_scoped_execution_records_verified_crew_identity(bound, monkeypatch, db_session_factory, streaming):
    context = TokenContext(
        user_id="human",
        org_id="tenant",
        team_id="team",
        department_id="department",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    context._budget_enforcement_enabled = False
    enforcement = AsyncMock()
    enforcement.prepare_enforcement_context.return_value = None

    async def check(context, cost, **kwargs):
        context._budget_admission_targets = []
        return SimpleNamespace(allowed=True)

    enforcement.check_budget_hierarchy.side_effect = check
    monkeypatch.setattr(
        chat_model_execution, "quote_request", AsyncMock(return_value=SimpleNamespace(total_usd=Decimal("0.1"), maximum_cost_usd=Decimal("0.1")))
    )
    monkeypatch.setattr(chat_model_execution, "confirm_quote_spendable", AsyncMock(return_value=None))
    provider = AsyncMock(
        return_value={
            "content": [{"type": "text", "text": "Hello"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 4, "output_tokens": 2},
            "price": Price(),
            "provider_request_id": "provider-receipt",
        }
    )
    services = []

    def service(authority, db):
        execution = chat_model_execution.ChatModelExecution(
            authority,
            db,
            enforcement=enforcement,
            provider=provider,
            event_writer=AsyncMock(),
            readiness=AsyncMock(return_value=(context, SimpleNamespace(account_id=None, region="us-east-1"))),
            clock=lambda: bound.runtime[-1],
        )
        services.append(execution)
        return execution

    monkeypatch.setattr(chat_model, "ChatModelExecution", service)
    response = await invoke(bound, bound.run_id, streaming=streaming)
    assert response.status_code == 200, response.text
    async with db_session_factory() as db:
        usage = (await db.scalars(select(UsageLog))).one()
        assert usage.root_human_id == "human"
        assert usage.agent_run_id == bound.run_id
        assert usage.user_id == "human" and usage.org_id == "tenant"
    operation = services[0].journal._read("run-user", "call-1")
    assert operation["run_id"] == "run-user"
    assert operation["usage_logged"] is True
    assert operation["reservation_status"] == "settled"
    assert enforcement.check_budget_hierarchy.await_args.kwargs["run_id"] == "run-user"
    provider.assert_awaited_once()


async def test_persistent_model_requires_an_asserted_crew_run(bound):
    response = await invoke(bound)
    assert response.status_code == 404, response.text
    bound.policy.assert_not_awaited()
    bound.provider.assert_not_awaited()


@pytest.mark.parametrize("tenant,user", [("tenant", "another-human"), ("another-tenant", "another-human")])
async def test_other_pods_crew_run_cannot_charge_verified_pod(bound, tenant, user):
    header = bound.runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]
    bound.runtime[2].put_item(
        Item={
            **header,
            "PK": "session#session-b",
            "tenantId": tenant,
            "orgId": tenant,
            "ownerUserId": user,
            "chatLease": {**header["chatLease"], "run_id": "run-other", "sandbox_uid": "other-pod"},
        }
    )
    foreign = bound.crew.mint(
        tenant_id=tenant,
        user_id=user,
        session_id="session-b",
        lease_generation=int(bound.lease["generation"]),
        lease_expires_at=int(bound.lease["expires_at"]),
        target_kind="eks-pod",
        target_ref="other-pod-name",
        now=int(datetime.now(UTC).timestamp()),
    )
    response = await invoke(bound, foreign)
    assert response.status_code == 404, response.text
    bound.policy.assert_not_awaited()
    bound.provider.assert_not_awaited()


async def test_binding_to_another_target_pod_is_refused(bound):
    row = bound.crew.read_run(bound.run_id)
    bound.registry.update_item(
        Key={"event_id": bound.run_id, "arrived_at": row["arrived_at"]},
        UpdateExpression="SET target_ref = :other",
        ExpressionAttributeValues={":other": "other-pod-name"},
    )
    response = await invoke(bound, bound.run_id)
    assert response.status_code == 404, response.text
    bound.provider.assert_not_awaited()


async def test_revoked_binding_cannot_be_used_after_a_prior_success(bound):
    assert (await invoke(bound, bound.run_id)).status_code == 200
    assert bound.crew.revoke_for_lease(tenant_id="tenant", session_id="session-a", lease_generation=int(bound.lease["generation"]))
    response = await invoke(bound, bound.run_id)
    assert response.status_code == 404, response.text
    bound.provider.assert_awaited_once()


async def test_binding_is_rechecked_immediately_before_provider(bound, monkeypatch):
    async def execute(*, authorize, **kwargs):
        assert bound.crew.revoke_for_lease(tenant_id="tenant", session_id="session-a", lease_generation=int(bound.lease["generation"]))
        await authorize()
        await bound.provider()

    monkeypatch.setattr(chat_model, "ChatModelExecution", lambda *_: SimpleNamespace(execute=execute))
    response = await invoke(bound, bound.run_id)
    assert response.status_code == 404, response.text
    bound.provider.assert_not_awaited()


async def test_tool_events_are_ordered_and_attributed_by_gateway(bound, monkeypatch):
    published = []

    async def create(authority, launch, operation_id, authorize):
        await authorize()

        async def publish(event):
            await authorize()
            published.append(event)

        return SimpleNamespace(publish=publish)

    monkeypatch.setattr(chat_model.ChatDeliveryRelay, "create", create)
    response = await bound.client.post(
        "/v1/chat/model/tool-events",
        json={
            "run_id": "run-user",
            "session_id": "session-a",
            "operation_id": "tool_call-1",
            "tool_call_id": "call-1",
            "name": "write_file",
            "args": {"file_path": "/tmp/workspace/a.txt"},
        },
        headers={"Authorization": f"Bearer {bound.token}", "X-Adp-Workload-Token": "sandbox-token", "x-agent-runid": bound.run_id},
    )
    assert response.status_code == 200, response.text
    assert published == [
        {"event_type": "TOOL_CALL_START", "toolCallId": "call-1", "toolCallName": "write_file"},
        {"event_type": "TOOL_CALL_ARGS", "toolCallId": "call-1", "delta": '{"file_path":"/tmp/workspace/a.txt"}'},
        {"event_type": "TOOL_CALL_END", "toolCallId": "call-1"},
    ]
    assert "human" not in response.text


async def test_crew_text_events_are_lease_fenced_and_emit_ag_ui(bound, monkeypatch):
    published = []

    async def create(authority, launch, operation_id, authorize):
        await authorize()
        relay = SimpleNamespace(stream_id="a" * 64, delivery=SimpleNamespace(thread_id="thread-a", task_id="task-a"), sequence=0)

        async def publish(event):
            await authorize()
            published.append((relay.sequence, event))
            relay.sequence += 1

        relay.publish = publish
        return relay

    monkeypatch.setattr(chat_model.ChatDeliveryRelay, "create", create)
    headers = {"Authorization": f"Bearer {bound.token}", "X-Adp-Workload-Token": "sandbox-token", "x-agent-runid": bound.run_id}
    base = {"run_id": "run-user", "session_id": "session-a", "operation_id": "crew_" + "b" * 64}
    for sequence, event_type, delta in [(0, "start", ""), (2, "content", "Hello"), (3, "end", "")]:
        response = await bound.client.post(
            "/v1/chat/model/crew-events", json={**base, "sequence": sequence, "event_type": event_type, "delta": delta}, headers=headers
        )
        assert response.status_code == 200, response.text
    assert [(sequence, event["event_type"]) for sequence, event in published] == [
        (0, "RUN_STARTED"),
        (1, "TEXT_MESSAGE_START"),
        (2, "TEXT_MESSAGE_CONTENT"),
        (3, "TEXT_MESSAGE_END"),
    ]
    assert published[2][1]["delta"] == "Hello"
    rejected = await bound.client.post(
        "/v1/chat/model/crew-events",
        json={**base, "sequence": 4, "event_type": "content", "delta": "forged"},
        headers={**headers, "x-agent-runid": "other-run"},
    )
    assert rejected.status_code in (403, 404)
    assert len(published) == 4


@pytest.mark.parametrize("character", ["x", "界", "😀"])
async def test_tool_event_utf8_boundary_and_omission_preserve_order(bound, monkeypatch, character):
    published = []

    async def create(authority, launch, operation_id, authorize):
        await authorize()

        async def publish(event):
            await authorize()
            published.append(event)

        return SimpleNamespace(publish=publish)

    monkeypatch.setattr(chat_model.ChatDeliveryRelay, "create", create)
    headers = {"Authorization": f"Bearer {bound.token}", "X-Adp-Workload-Token": "sandbox-token", "x-agent-runid": bound.run_id}
    empty = {"file_path": "/tmp/workspace/a.txt", "content": ""}
    remaining = 16_000 - len(json.dumps(empty, separators=(",", ":")).encode())
    content = character * (remaining // len(character.encode())) + "x" * (remaining % len(character.encode()))
    for size in (15_999, 16_000, 16_001):
        published.clear()
        args = {**empty, "content": "x" * (remaining - 1) if size < 16_000 else content + "x" * (size - 16_000)}
        serialized = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
        assert len(serialized.encode()) == size
        response = await bound.client.post(
            "/v1/chat/model/tool-events",
            json={
                "run_id": "run-user",
                "session_id": "session-a",
                "operation_id": "tool_call-1",
                "tool_call_id": "call-1",
                "name": "write_file",
                "args": args,
            },
            headers=headers,
        )
        assert response.status_code == (200 if size <= 16_000 else 422), response.text
        if size <= 16_000:
            assert [event["event_type"] for event in published] == ["TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END"]
            assert published[1]["delta"] == serialized
        else:
            assert published == []

    omitted = {"_adp_tool_event": {"arguments_omitted": True, "utf8_bytes": 20_049, "limit_bytes": 16_000}}
    response = await bound.client.post(
        "/v1/chat/model/tool-events",
        json={
            "run_id": "run-user",
            "session_id": "session-a",
            "operation_id": "tool_call-2",
            "tool_call_id": "call-2",
            "name": "write_file",
            "args": omitted,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert [event["event_type"] for event in published] == ["TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END"]
    assert json.loads(published[1]["delta"]) == omitted


async def test_tool_events_reject_other_crew_run_and_revoked_lease(bound, monkeypatch):
    publish = AsyncMock()

    async def create(authority, launch, operation_id, authorize):
        await authorize()
        return SimpleNamespace(publish=publish)

    monkeypatch.setattr(chat_model.ChatDeliveryRelay, "create", create)

    async def send(run_id):
        return await bound.client.post(
            "/v1/chat/model/tool-events",
            json={
                "run_id": "run-user",
                "session_id": "session-a",
                "operation_id": "tool_call-1",
                "tool_call_id": "call-1",
                "name": "write_file",
                "args": {},
            },
            headers={"Authorization": f"Bearer {bound.token}", "X-Adp-Workload-Token": "sandbox-token", "x-agent-runid": run_id},
        )

    assert (await send("00000000-0000-4000-8000-000000000009")).status_code == 404
    assert bound.crew.revoke_for_lease(tenant_id="tenant", session_id="session-a", lease_generation=int(bound.lease["generation"]))
    assert (await send(bound.run_id)).status_code == 404
    publish.assert_not_awaited()


async def test_tool_events_reject_expired_lease_and_client_supplied_identity(bound, monkeypatch):
    publish = AsyncMock()
    monkeypatch.setattr(chat_model.ChatDeliveryRelay, "create", publish)
    headers = {"Authorization": f"Bearer {bound.token}", "X-Adp-Workload-Token": "sandbox-token", "x-agent-runid": bound.run_id}
    event = {
        "run_id": "run-user",
        "session_id": "session-a",
        "operation_id": "tool_call-1",
        "tool_call_id": "call-1",
        "name": "write_file",
        "args": {},
    }
    rejected = await bound.client.post("/v1/chat/model/tool-events", json={**event, "root_human_id": "forged"}, headers=headers)
    assert rejected.status_code == 422

    table = bound.runtime[2]
    header = table.get_item(Key={"PK": "session#session-a", "SK": "header"}, ConsistentRead=True)["Item"]
    header["chatLease"]["expires_at"] = int(datetime.now(UTC).timestamp()) - 1
    table.put_item(Item=header)
    expired = await bound.client.post("/v1/chat/model/tool-events", json=event, headers=headers)
    assert expired.status_code == 404
    publish.assert_not_awaited()
