import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import ClientDisconnect, Request

from src.agentauth import chat_model, chat_model_stream
from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from tests.agentauth.test_chat_model_execution import REQUEST, reserved_amount
from tests.agentauth.test_chat_model_execution import client as client_fixture
from tests.agentauth.test_chat_model_execution import model as model_fixture
from tests.agentauth.test_chat_model_execution import retained_input_table as retained_input_table_fixture
from tests.agentauth.test_chat_model_execution import runtime as runtime_fixture
from tests.agentauth.test_chat_model_execution import store as store_fixture
from tests.agentauth.test_chat_model_execution import sts as sts_fixture

client = client_fixture
model = model_fixture
retained_input_table = retained_input_table_fixture
runtime = runtime_fixture
store = store_fixture
sts = sts_fixture
TEXT = {"type": "text_delta", "index": 0, "text": "Résumé 😀"}


def response(execute, authorize=None):
    return chat_model_stream.model_stream_response(
        SimpleNamespace(execute=execute),
        launch=SimpleNamespace(run_id="run-a", session_id="session-a", lease_generation=1),
        operation_id="call-a",
        model_id="selected-model",
        request=REQUEST,
        authorize=authorize or AsyncMock(),
    )


async def route(model):
    request = Request({"type": "http", "headers": [(b"accept", b"application/x-ndjson"), (b"x-adp-workload-token", b"sandbox-token")]})
    body = chat_model.SandboxModelInvocation(run_id="run-user", session_id="session-a", operation_id="call-1", request=REQUEST)
    return await chat_model.sandbox_model_invoke(
        body, request, model.client.headers["authorization"].removeprefix("Bearer "), (model.runtime[1], model.runtime[0]), object()
    )


async def test_route_streams_before_completion_and_replays_only_settled_receipt(model):
    release = asyncio.Event()
    result = model.provider.return_value

    async def provider(**kwargs):
        await kwargs["on_event"](TEXT)
        await release.wait()
        return result

    model.provider.side_effect = provider
    stream = await route(model)
    assert stream.headers["content-type"] == chat_model_stream.MEDIA_TYPE
    assert stream.headers["cache-control"] == "no-store" and stream.headers["x-accel-buffering"] == "no"
    first = json.loads(await asyncio.wait_for(anext(stream.body_iterator), 5))
    assert first == {
        **TEXT,
        "run_id": "run-user",
        "session_id": "session-a",
        "lease_generation": 1,
        "operation_id": "call-1",
        "request_digest": model.service.journal._read("run-user", "call-1")["request_digest"],
        "sequence": 0,
    }
    model.service.usage_writer.assert_not_awaited()
    release.set()
    frames = [json.loads(frame) async for frame in stream.body_iterator]
    assert len(frames) == 1 and frames[0]["type"] == "receipt" and frames[0]["sequence"] == 1
    receipt = frames[0]["receipt"]
    assert receipt["status"] == "confirmed" and receipt["reservation_status"] == "settled"
    assert receipt["usage"] == {"input_tokens": 4, "output_tokens": 2, "estimated_usd": "0.01"}
    replay = await route(model)
    replayed = [json.loads(frame) async for frame in replay.body_iterator]
    assert len(replayed) == 1 and replayed[0]["type"] == "receipt" and replayed[0]["receipt"] == receipt
    model.provider.assert_awaited_once()
    model.service.usage_writer.assert_awaited_once()
    assert await reserved_amount(model) == Decimal("0.01")


async def test_disconnect_cancels_provider_and_records_unknown_accounting(model):
    stopped = asyncio.Event()

    async def provider(**kwargs):
        try:
            await kwargs["on_event"](TEXT)
            await asyncio.Event().wait()
        finally:
            stopped.set()

    model.provider.side_effect = provider
    stream = await route(model)
    disconnected = asyncio.Event()
    sent = []

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body":
            disconnected.set()

    await asyncio.wait_for(stream({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send), 5)
    assert stopped.is_set()
    assert len([message for message in sent if message["type"] == "http.response.body"]) == 1
    operation = model.service.journal._read("run-user", "call-1")
    assert operation["status"] == "unknown" and operation["reservation_status"] == "unknown"
    assert operation["automatic_replay_permitted"] is False
    assert await reserved_amount(model) == Decimal("0.1")
    model.service.usage_writer.assert_not_awaited()
    model.service.gap_writer.assert_awaited_once()


async def test_buffered_text_is_not_delivered_after_authority_revocation():
    stopped = asyncio.Event()

    async def execute(**kwargs):
        try:
            await kwargs["on_event"](TEXT)
            await kwargs["on_event"]({**TEXT, "text": "withheld"})
            await asyncio.Event().wait()
        finally:
            stopped.set()

    authorize = AsyncMock()
    stream = response(execute, authorize)
    assert json.loads(await anext(stream.body_iterator))["text"] == TEXT["text"]
    authorize.side_effect = ChatAuthorizationRefusedError("private refusal")
    terminal = json.loads(await anext(stream.body_iterator))
    assert terminal["type"] == "error" and terminal["code"] == "denied"
    assert stopped.is_set()
    assert "private" not in json.dumps(terminal) and "withheld" not in json.dumps(terminal)
    assert [frame async for frame in stream.body_iterator] == []


async def test_slow_consumer_has_bounded_queue_and_closes_producer():
    emitted = 0
    stopped = asyncio.Event()

    async def execute(**kwargs):
        nonlocal emitted
        try:
            while True:
                emitted += 1
                await kwargs["on_event"](TEXT)
        finally:
            stopped.set()

    stream = response(execute)
    await anext(stream.body_iterator)
    await asyncio.sleep(0.01)
    assert emitted <= 10
    await stream.body_iterator.aclose()
    assert stopped.is_set()


async def test_asgi_send_failure_closes_a_suspended_stream_without_waiting_for_garbage_collection():
    stopped = asyncio.Event()

    async def execute(**kwargs):
        try:
            await kwargs["on_event"](TEXT)
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("connection gone")

    stream = response(execute)
    with pytest.raises(ClientDisconnect):
        await asyncio.wait_for(stream({"type": "http", "asgi": {"spec_version": "2.4"}}, AsyncMock(), send), 1)
    assert stopped.is_set()


@pytest.mark.parametrize(
    "event",
    [{**TEXT, "type": "thinking"}, {**TEXT, "index": True}, {**TEXT, "index": 64}, {**TEXT, "secret": "private"}, {**TEXT, "text": "x" * 65_537}],
)
async def test_invalid_or_private_provider_events_fail_closed(event):
    async def execute(**kwargs):
        await kwargs["on_event"](event)

    frames = [json.loads(frame) async for frame in response(execute).body_iterator]
    assert len(frames) == 1 and frames[0]["type"] == "error" and frames[0]["code"] == "incomplete"
    assert "private" not in json.dumps(frames)


@pytest.mark.parametrize("bound", ["frame", "count", "total"])
async def test_stream_bounds_return_explicit_failure(monkeypatch, bound):
    if bound == "count":
        monkeypatch.setattr(chat_model_stream, "MAX_FRAMES", 2)
    elif bound == "total":
        monkeypatch.setattr(chat_model_stream, "MAX_STREAM_BYTES", 65_536)

    async def execute(**kwargs):
        await kwargs["on_event"](TEXT)
        return {"content": "x" * (65_536 if bound == "frame" else 1)}

    frames = [json.loads(frame) async for frame in response(execute).body_iterator]
    assert frames[-1]["type"] == "error" and frames[-1]["code"] == "incomplete"
    assert all(len(json.dumps(frame).encode()) <= 65_536 for frame in frames)


@pytest.mark.parametrize("changed", [{"run_id": "other"}, {"session_id": "other"}])
async def test_stream_route_rejects_forged_scope_before_provider(model, changed):
    result = await model.client.post(
        "/v1/chat/model/invoke",
        headers={"Accept": chat_model_stream.MEDIA_TYPE},
        json={"run_id": "run-user", "session_id": "session-a", "operation_id": "call-1", "request": REQUEST, **changed},
    )
    assert result.status_code == 404
    model.provider.assert_not_awaited()


async def test_http_negotiation_uses_existing_scoped_route(model):
    result = await model.client.post(
        "/v1/chat/model/invoke",
        headers={"Accept": chat_model_stream.MEDIA_TYPE},
        json={"run_id": "run-user", "session_id": "session-a", "operation_id": "call-1", "request": REQUEST},
    )
    assert result.status_code == 200 and result.headers["content-type"] == chat_model_stream.MEDIA_TYPE
    frames = [json.loads(line) for line in result.text.splitlines()]
    assert len(frames) == 1 and frames[0]["receipt"]["reservation_status"] == "settled"


@pytest.mark.parametrize("change", ["session_end", "lease_replaced", "abort"])
async def test_quiet_stream_is_fenced_when_authority_changes(model, change):
    stopped = asyncio.Event()

    async def provider(**kwargs):
        try:
            await kwargs["on_event"](TEXT)
            await asyncio.Event().wait()
        finally:
            stopped.set()

    model.provider.side_effect = provider
    stream = await route(model)
    await anext(stream.body_iterator)
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
    terminal = json.loads(await asyncio.wait_for(anext(stream.body_iterator), 5))
    assert terminal["type"] == "error" and terminal["code"] == "denied"
    assert stopped.is_set()
    await stream.body_iterator.aclose()
    operation = model.service.journal._read("run-user", "call-1")
    assert operation["status"] == "unknown" and operation["reservation_status"] == "unknown"
    assert await reserved_amount(model) == Decimal("0.1")
    model.service.usage_writer.assert_not_awaited()
