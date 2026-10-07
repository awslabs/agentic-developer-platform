import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth import chat_delivery
from src.agentauth.chat_delivery import protected_delivery
from src.agentauth.chat_event_journal import ChatEventJournal
from src.agentauth.chat_stream_journal import append_stream_event
from tests.agentauth import test_chat_model_execution as execution_fixtures

client = execution_fixtures.client
model = execution_fixtures.model
retained_input_table = execution_fixtures.retained_input_table
runtime = execution_fixtures.runtime
store = execution_fixtures.store
sts = execution_fixtures.sts
OWNER = '["tenant","tenant","team","human","webchat"]'
GENERATION = 1_700_000_000
ROUTING = {
    "channel": "webchat",
    "org_id": "tenant",
    "team_id": "team",
    "user_id": "human",
    "task_id": "task-a",
    "thread_id": "thread-a",
    "owner_principal": OWNER,
    "session_generation": GENERATION,
}
TEXT = {"type": "text_delta", "index": 0, "text": "Owner-only reply 😀"}


@pytest.fixture(autouse=True)
def protected_root(monkeypatch):
    prepare = execution_fixtures.prepare
    monkeypatch.setattr(execution_fixtures, "prepare", lambda runtime, **changes: prepare(runtime, **{**ROUTING, **changes}))


@pytest.fixture
def transport(runtime, monkeypatch):
    sessions = boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName="owner-delivery-sessions",
        KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    row = {
        "session_id": "session-a",
        "owner_principal": OWNER,
        "created_at": GENERATION,
        "channel": "webchat",
        "expires_at": int(time.time()) + 300,
        "threads": {"thread-a": {"processing_task_id": "task-a"}},
        "connection_id": "owner-connection",
    }
    sessions.put_item(Item=row)
    queue = Mock()
    queue.send_message.return_value = {"MessageId": "accepted"}
    queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/responses.fifo"
    monkeypatch.setattr(chat_delivery, "response_transport", lambda: (queue, queue_url, sessions))
    return SimpleNamespace(client=queue, sessions=sessions, row=row, url=queue_url)


async def invoke(model, **changes):
    return await model.client.post(
        "/v1/chat/model/invoke",
        json={
            "run_id": "run-user",
            "session_id": "session-a",
            "operation_id": "call-1",
            "request": execution_fixtures.REQUEST,
            "deliver_response": True,
            **changes,
        },
    )


def streaming_provider(model):
    result = model.provider.return_value

    async def provider(**kwargs):
        await kwargs["on_event"](TEXT)
        return result

    model.provider.side_effect = provider


async def test_owner_turn_uses_registered_routing_and_existing_websocket_transport(model, transport):
    streaming_provider(model)
    result = await invoke(model)
    assert result.status_code == 200 and result.json()["reservation_status"] == "settled"
    calls = transport.client.send_message.call_args_list
    envelopes = [json.loads(call.kwargs["MessageBody"]) for call in calls]
    assert [value["event"]["event_type"] for value in envelopes] == ["RUN_STARTED", "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT"]
    assert [value["event"]["stream_sequence"] for value in envelopes] == [0, 1, 2]
    assert all(
        value["task_id"] == "task-a" and value["owner_principal"] == OWNER and value["session_generation"] == GENERATION for value in envelopes
    )
    assert all(value["strict_delivery"] is True and "connection_id" not in value and "channel_metadata" not in value for value in envelopes)
    assert all(call.kwargs["QueueUrl"] == transport.url and call.kwargs["MessageGroupId"] == "session-a" for call in calls)
    assert len({call.kwargs["MessageDeduplicationId"] for call in calls}) == 3
    assert "RUN_FINISHED" not in json.dumps(envelopes)
    spec = importlib.util.spec_from_file_location(
        "scoped_chat_websocket_fixture",
        Path(__file__).resolve().parents[4] / "modules/agent-factory/gateway/lambdas/response/routers/websocket.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    router = module.WebSocketRouter("https://websocket.example.test", transport.sessions)
    router._client = Mock()
    for envelope in envelopes:
        metadata = {**envelope, "response_type": "ag_ui", "ag_ui_payload": envelope["event"]}
        assert router.route(json.dumps(envelope["event"]), metadata, envelope["task_id"])
    sent = router._client.post_to_connection.call_args_list
    assert all(call.kwargs["ConnectionId"] == "owner-connection" for call in sent)
    assert json.loads(sent[-1].kwargs["Data"])["event"]["delta"] == TEXT["text"]
    assert (await invoke(model)).json() == result.json()
    assert transport.client.send_message.call_count == 3
    model.provider.assert_awaited_once()
    model.service.usage_writer.assert_awaited_once()


async def test_summarization_stays_private(model, transport):
    streaming_provider(model)
    result = await invoke(model, deliver_response=False)
    assert result.status_code == 200 and result.json()["status"] == "confirmed"
    transport.client.send_message.assert_not_called()


@pytest.mark.parametrize(
    "changed",
    [{"owner_principal": "other"}, {"created_at": GENERATION + 1}, {"expires_at": 1}, {"threads": {"thread-a": {"processing_task_id": "other"}}}],
)
async def test_stale_or_foreign_owner_session_refused_before_inference(model, transport, changed):
    transport.sessions.put_item(Item={**transport.row, **changed})
    result = await invoke(model)
    assert result.status_code == 404
    model.provider.assert_not_awaited()
    transport.client.send_message.assert_not_called()


@pytest.mark.parametrize("changed", [{"owner_principal": "other"}, {"created_at": GENERATION + 1}])
async def test_rebinding_during_inference_never_redirects_output(model, transport, changed):
    async def provider(**kwargs):
        transport.sessions.put_item(Item={**transport.row, **changed})
        await kwargs["on_event"](TEXT)

    model.provider.side_effect = provider
    result = await invoke(model)
    assert result.json()["status"] == "unknown" and result.json()["reservation_status"] == "unknown"
    transport.client.send_message.assert_not_called()
    model.service.usage_writer.assert_not_awaited()


@pytest.mark.parametrize("key", ["connection_id", "owner_principal", "session_generation", "task_id", "principal"])
async def test_sandbox_cannot_choose_delivery_fields(model, transport, key):
    assert (await invoke(model, **{key: "forged"})).status_code == 422
    model.provider.assert_not_awaited()
    transport.client.send_message.assert_not_called()


async def test_mutated_protected_routing_fails_integrity_check(model, transport):
    store = model.runtime[1].store
    raw = store._read("TENANT#tenant", "EXEC#run-user")
    modified = json.loads(raw["chat_delivery"]["S"])
    modified["task_id"] = "forged"
    store.client.update_item(
        TableName=store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-user"}},
        UpdateExpression="SET chat_delivery = :delivery",
        ExpressionAttributeValues={":delivery": {"S": json.dumps(modified)}},
    )
    result = await invoke(model)
    assert result.status_code in {404, 503}
    model.provider.assert_not_awaited()
    transport.client.send_message.assert_not_called()


async def test_ambiguous_queue_handoff_is_not_retried_or_accounted_as_zero(model, transport):
    streaming_provider(model)
    transport.client.send_message.side_effect = RuntimeError("private transport detail")
    result = await invoke(model)
    receipt = result.json()
    assert receipt["status"] == "unknown" and receipt["reservation_status"] == "unknown"
    assert receipt["automatic_replay_permitted"] is False
    assert "private" not in result.text
    assert (await invoke(model)).json() == receipt
    transport.client.send_message.assert_called_once()
    model.provider.assert_awaited_once()
    replay = stream_replay(model)
    assert [event["sequence"] for event in replay["events"]] == [1]
    assert replay["events"][0]["payload"]["event"]["event_type"] == "RUN_STARTED"


def stream_replay(model):
    return ChatEventJournal(model.runtime[2]).replay(
        session_id="session-a",
        owner=("tenant", "team", "human"),
        generation=GENERATION,
        cursor=None,
        limit=100,
        now=int(time.time()),
    )


async def test_stream_commits_before_transport_and_survives_new_reader(model, transport):
    streaming_provider(model)

    def send(**kwargs):
        payload = json.loads(kwargs["MessageBody"])
        stored = stream_replay(model)["events"][-1]
        assert payload["event"].pop("event_cursor") == stored["cursor"]
        assert stored["payload"] == payload
        return {"MessageId": "accepted"}

    transport.client.send_message.side_effect = send
    assert (await invoke(model)).status_code == 200
    assert [event["sequence"] for event in stream_replay(model)["events"]] == [1, 2, 3]


async def test_lost_stream_storage_reply_preserves_event_without_replaying_model(model, transport, monkeypatch):
    streaming_provider(model)
    store = model.runtime[1].store
    transact = store.client.transact_write_items

    def lose_response(**kwargs):
        response = transact(**kwargs)
        if any(item.get("Put", {}).get("Item", {}).get("SK") == {"S": "output-state"} for item in kwargs["TransactItems"]):
            raise EndpointConnectionError(endpoint_url="https://storage.example.test")
        return response

    monkeypatch.setattr(store.client, "transact_write_items", lose_response)
    response = await invoke(model)
    assert response.json()["status"] == "unknown"
    assert len(stream_replay(model)["events"]) == 1
    assert (await invoke(model)).json() == response.json()
    model.provider.assert_awaited_once()
    transport.client.send_message.assert_not_called()


@pytest.mark.parametrize("field", ["tenant_id", "team_id", "user_id", "session_id", "run_id"])
async def test_foreign_stream_delivery_cannot_append_to_journal(model, field):
    authority = model.runtime[1]
    delivery = chat_delivery.load_delivery(authority, model.launch).model_copy(update={field: "other"})
    with pytest.raises(chat_delivery.ChatAuthorizationRefusedError):
        append_stream_event(authority, model.launch, delivery, "foreign", {}, now=int(time.time()))
    assert stream_replay(model)["reason"] == "journal_unavailable"


@pytest.mark.parametrize("race", ["lease", "abort_command_id", "chat_turn_sealed"])
async def test_changed_authority_during_journal_commit_cannot_persist_or_send(model, transport, monkeypatch, race):
    streaming_provider(model)
    store = model.runtime[1].store
    transact = store.client.transact_write_items
    raced = False

    def replace(**kwargs):
        nonlocal raced
        if not raced and any(item.get("Put", {}).get("Item", {}).get("SK") == {"S": "output-state"} for item in kwargs["TransactItems"]):
            raced = True
            if race == "lease":
                model.runtime[2].update_item(
                    Key={"PK": "session#session-a", "SK": "header"},
                    UpdateExpression="SET chatLease.generation = :new",
                    ExpressionAttributeValues={":new": 2},
                )
            else:
                store.client.update_item(
                    TableName=store.table,
                    Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-user"}},
                    UpdateExpression="SET #field = :value",
                    ExpressionAttributeNames={"#field": race},
                    ExpressionAttributeValues={":value": {"S": "changed"}},
                )
        return transact(**kwargs)

    monkeypatch.setattr(store.client, "transact_write_items", replace)
    response = await invoke(model)
    assert raced
    if race == "lease":
        assert response.status_code == 404
    else:
        assert response.json()["status"] == "unknown"
    assert stream_replay(model)["reason"] == "journal_unavailable"
    transport.client.send_message.assert_not_called()


async def test_ndjson_path_also_relays_before_returning_receipt(model, transport):
    async def provider(**kwargs):
        await kwargs["on_event"](TEXT)
        assert transport.client.send_message.call_count == 3
        model.service.usage_writer.assert_not_awaited()
        return model.provider.return_value

    model.provider.side_effect = provider
    model.client.headers["Accept"] = "application/x-ndjson"
    response = await invoke(model)
    frames = [json.loads(line) for line in response.text.splitlines()]
    assert frames[0]["type"] == "text_delta" and frames[-1]["receipt"]["reservation_status"] == "settled"
    assert transport.client.send_message.call_count == 3


def test_registration_rejects_inconsistent_owner_and_keeps_canonical_identity():
    envelope = {**ROUTING, "message_id": "run-a", "session_id": "session-a", "tenant_id": "tenant"}
    retained = protected_delivery(envelope, "canonical-human")
    assert json.loads(retained["S"])["user_id"] == "canonical-human"
    with pytest.raises(ValueError):
        protected_delivery({**envelope, "owner_principal": "forged"}, "human")


async def test_large_text_is_split_into_bounded_browser_frames(model, transport):
    async def provider(**kwargs):
        await kwargs["on_event"]({**TEXT, "text": "😀" * 16_384})
        return model.provider.return_value

    model.provider.side_effect = provider
    assert (await invoke(model)).json()["status"] == "confirmed"
    payloads = [call.kwargs["MessageBody"] for call in transport.client.send_message.call_args_list]
    assert all(len(payload.encode()) < 24 * 1024 for payload in payloads)
    assert "".join(json.loads(payload)["event"].get("delta", "") for payload in payloads) == "😀" * 16_384


@pytest.mark.parametrize(
    "queue",
    [
        "",
        "__CHAT_RESPONSE_QUEUE_URL__",
        "http://sqs.us-east-1.amazonaws.com/123456789012/responses.fifo",
        "https://attacker.example.test/responses.fifo",
        "https://sqs.us-east-1.amazonaws.com/123456789012/tasks",
    ],
)
def test_transport_configuration_fails_closed_without_client_creation(monkeypatch, queue):
    chat_delivery.response_transport.cache_clear()
    monkeypatch.setenv("ADP_CHAT_RESPONSE_QUEUE_URL", queue)
    monkeypatch.setattr(chat_delivery, "_get_sessions_table", lambda: Mock())
    factory = Mock()
    monkeypatch.setattr(chat_delivery.boto3, "client", factory)
    with pytest.raises(chat_delivery.ChatAuthorizationUnavailableError, match="unconfigured"):
        chat_delivery.response_transport()
    factory.assert_not_called()


def test_transport_requires_session_store_and_has_no_automatic_queue_retry(monkeypatch):
    chat_delivery.response_transport.cache_clear()
    queue = "https://sqs.us-east-1.amazonaws.com/123456789012/responses.fifo"
    monkeypatch.setenv("ADP_CHAT_RESPONSE_QUEUE_URL", queue)
    monkeypatch.setattr(chat_delivery, "_get_sessions_table", lambda: None)
    factory = Mock()
    monkeypatch.setattr(chat_delivery.boto3, "client", factory)
    with pytest.raises(chat_delivery.ChatAuthorizationUnavailableError, match="unconfigured"):
        chat_delivery.response_transport()
    factory.assert_not_called()
    sessions = Mock()
    monkeypatch.setattr(chat_delivery, "_get_sessions_table", lambda: sessions)
    try:
        assert chat_delivery.response_transport() == (factory.return_value, queue, sessions)
        assert factory.call_args.kwargs["config"].retries == {"total_max_attempts": 1}
        assert factory.call_args.kwargs["region_name"] == "us-east-1"
    finally:
        chat_delivery.response_transport.cache_clear()
