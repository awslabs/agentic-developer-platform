"""Real streaming adapter with an in-process SDK event source, not live Bedrock."""

import asyncio
import json
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.agentauth import chat_model_provider as provider
from src.agentauth.chat_capability import ChatAuthorizationUnavailableError

MODEL = "global.anthropic.claude-sonnet-5"
OWNER = "00000000-0000-4000-8000-000000000001"
REQUEST = {"messages": [{"role": "user", "content": "Hello"}], "max_tokens": 16}
DIGEST_VECTORS = json.loads(Path(__file__).with_name("chat_model_digest_vectors.json").read_text())


def events(block=None, deltas=None, stop_reason="end_turn"):
    return [
        {"type": "message_start", "message": {"content": [], "usage": {"input_tokens": 4, "cache_read_input_tokens": 2}}},
        {"type": "content_block_start", "index": 0, "content_block": block or {"type": "text", "text": ""}},
        *[{"type": "content_block_delta", "index": 0, "delta": delta} for delta in (deltas or [{"type": "text_delta", "text": "Hello"}])],
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": 2}},
        {"type": "message_stop"},
    ]


class Stream:
    def __init__(self, documents):
        self.documents = iter(documents)
        self.closed = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        return {"chunk": {"bytes": json.dumps(next(self.documents)).encode()}}

    def close(self):
        self.closed.set()


@pytest.fixture
def transport(monkeypatch):
    stream = Stream(events())
    upstream = SimpleNamespace(
        meta=SimpleNamespace(region_name="us-east-1", endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com"),
        invoke_model_with_response_stream=Mock(return_value={"body": stream, "ResponseMetadata": {"RequestId": "fixture-upstream"}}),
        invoke_model=Mock(side_effect=AssertionError("buffered fallback forbidden")),
        close=Mock(),
    )
    client_factory = Mock(return_value=upstream)
    monkeypatch.setattr(provider.boto3, "client", client_factory)
    pricing = AsyncMock(return_value=SimpleNamespace(ledger_cost_usd="0.01", confidence="verified"))
    monkeypatch.setattr(provider, "price_completed_usage", pricing)
    return SimpleNamespace(stream=stream, upstream=upstream, factory=client_factory, pricing=pricing)


async def invoke(**changes):
    return await provider.invoke_chat_messages(
        object(),
        **{
            "identity": SimpleNamespace(tenant="tenant", canonical_principal=f"human:{OWNER}"),
            "binding": {"model_id": MODEL},
            "target": SimpleNamespace(is_platform=True, region="us-east-1"),
            "request": deepcopy(REQUEST),
            "operation_id": "fixture-operation",
            **changes,
        },
    )


async def test_text_is_delivered_before_completion_with_measured_usage(transport):
    received = []

    async def on_event(event):
        assert not transport.stream.closed.is_set()
        transport.pricing.assert_not_awaited()
        received.append(event)

    receipt = await invoke(on_event=on_event)
    assert received == [{"type": "text_delta", "index": 0, "text": "Hello"}]
    assert receipt["content"] == [{"type": "text", "text": "Hello"}]
    assert receipt["usage"] == {"input_tokens": 4, "output_tokens": 2}
    assert receipt["provider_request_id"] == "fixture-upstream"
    assert transport.pricing.await_args.kwargs["raw_usage"]["cache_read_input_tokens"] == 2
    assert transport.factory.call_args.kwargs["config"].retries == {"total_max_attempts": 1}
    assert transport.upstream.invoke_model_with_response_stream.call_args.kwargs["modelId"] == MODEL
    transport.upstream.invoke_model.assert_not_called()
    assert transport.stream.closed.is_set()
    transport.upstream.close.assert_called_once()


@pytest.mark.parametrize(
    "block,deltas,stop,expected",
    [
        (
            {"type": "tool_use", "id": "call-1", "name": "history_read", "input": {}},
            [{"type": "input_json_delta", "partial_json": '{"amount":'}, {"type": "input_json_delta", "partial_json": '0.000001,"key":"界"}'}],
            "tool_use",
            {"type": "tool_use", "id": "call-1", "name": "history_read", "input": {"amount": 0.000001, "key": "界"}},
        ),
        (
            {"type": "thinking", "thinking": ""},
            [{"type": "thinking_delta", "thinking": "private reasoning"}, {"type": "signature_delta", "signature": "opaque-signature"}],
            "end_turn",
            {"type": "thinking", "thinking": "private reasoning", "signature": "opaque-signature"},
        ),
    ],
)
async def test_native_blocks_are_reassembled_without_exposing_arguments_or_thinking(transport, block, deltas, stop, expected):
    transport.stream.documents = iter(events(block, deltas, stop))
    on_event = AsyncMock()
    result = await invoke(request={**REQUEST, "tools": [{"name": "history_read", "input_schema": {"type": "object"}}]}, on_event=on_event)
    assert result["content"] == [expected]
    on_event.assert_not_awaited()


@pytest.mark.parametrize("vector", [vector for vector in DIGEST_VECTORS if "wireInput" in vector], ids=lambda vector: vector["name"])
async def test_large_finite_tool_arguments_survive_stream_frame_validation(transport, vector):
    block = {"type": "tool_use", "id": "call-numbers", "name": "history_read", "input": {}}
    transport.stream.documents = iter(events(block, [{"type": "input_json_delta", "partial_json": vector["wireInput"]}], "tool_use"))
    result = await invoke(request={**REQUEST, "tools": [{"name": "history_read", "input_schema": {"type": "object"}}]})
    assert result["content"] == [{**block, "input": json.loads(vector["wireInput"])}]
    transport.pricing.assert_awaited_once()
    assert transport.stream.closed.is_set()


@pytest.mark.parametrize(
    "failure", ["empty", "truncated", "wrong_index", "duplicate_start", "unknown_event", "large", "missing_usage", "excess_usage"]
)
async def test_malformed_or_unbounded_streams_never_produce_priced_receipts(transport, failure):
    documents = events()
    if failure == "empty":
        documents = []
    elif failure == "truncated":
        documents.pop()
    elif failure == "wrong_index":
        documents[2]["index"] = 1
    elif failure == "duplicate_start":
        documents.insert(2, documents[0])
    elif failure == "unknown_event":
        documents[2] = {"type": "unexpected"}
    elif failure == "large":
        documents[2]["delta"]["text"] = "x" * 65_537
    elif failure == "missing_usage":
        documents[-2]["usage"] = {}
    else:
        documents[-2]["usage"]["output_tokens"] = REQUEST["max_tokens"] + 1
    transport.stream.documents = iter(documents)
    with pytest.raises(ChatAuthorizationUnavailableError):
        await invoke()
    transport.pricing.assert_not_awaited()
    transport.upstream.invoke_model.assert_not_called()
    assert transport.stream.closed.is_set()


async def test_delivery_refusal_closes_stream_without_pricing_or_retry(transport):
    with pytest.raises(RuntimeError, match="consumer revoked"):
        await invoke(on_event=AsyncMock(side_effect=RuntimeError("consumer revoked")))
    assert transport.stream.closed.is_set()
    transport.pricing.assert_not_awaited()
    transport.upstream.invoke_model_with_response_stream.assert_called_once()


async def test_cancelling_a_blocked_read_closes_the_stream(transport):
    reading = threading.Event()

    class WaitingStream(Stream):
        def __next__(self):
            reading.set()
            if not self.closed.wait(timeout=5):
                raise AssertionError("cancel did not close provider body")
            raise StopIteration

    stream = WaitingStream([])
    transport.upstream.invoke_model_with_response_stream.return_value["body"] = stream
    running = asyncio.create_task(invoke())
    assert await asyncio.to_thread(reading.wait, 5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert stream.closed.is_set()
    transport.pricing.assert_not_awaited()


async def test_cancelling_before_response_headers_closes_the_late_response(transport):
    opening, release = threading.Event(), threading.Event()
    response = transport.upstream.invoke_model_with_response_stream.return_value

    def open_stream(**kwargs):
        opening.set()
        if not release.wait(timeout=5):
            raise AssertionError("fixture not released")
        return response

    transport.upstream.invoke_model_with_response_stream.side_effect = open_stream
    running = asyncio.create_task(invoke())
    try:
        assert await asyncio.to_thread(opening.wait, 5)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    finally:
        release.set()
    assert await asyncio.to_thread(transport.stream.closed.wait, 5)
    transport.pricing.assert_not_awaited()


async def test_unavailable_selected_credentials_never_fall_back(transport, monkeypatch):
    signer = AsyncMock(side_effect=RuntimeError("selected connection unavailable"))
    monkeypatch.setattr(provider.bedrock_destination_signer, "get_credentials", signer)
    with pytest.raises(RuntimeError, match="selected connection unavailable"):
        await invoke(target=SimpleNamespace(is_platform=False, region="us-east-1"))
    transport.factory.assert_not_called()
    assert signer.await_args.kwargs["user_id"] == OWNER


@pytest.mark.parametrize("credentials", [None, SimpleNamespace(access_key_id="synthetic-key", secret_access_key="", session_token="")])
async def test_missing_selected_credentials_never_use_ambient_credentials(transport, monkeypatch, credentials):
    monkeypatch.setattr(provider.bedrock_destination_signer, "get_credentials", AsyncMock(return_value=credentials))
    with pytest.raises(ChatAuthorizationUnavailableError):
        await invoke(target=SimpleNamespace(is_platform=False, region="us-east-1"))
    transport.factory.assert_not_called()


async def test_selected_credentials_are_bound_to_the_requested_destination(transport, monkeypatch):
    credentials = SimpleNamespace(access_key_id="synthetic-key", secret_access_key="synthetic-secret", session_token="synthetic-session")
    monkeypatch.setattr(provider.bedrock_destination_signer, "get_credentials", AsyncMock(return_value=credentials))
    await invoke(target=SimpleNamespace(is_platform=False, region="eu-west-1"))
    kwargs = transport.factory.call_args.kwargs
    assert kwargs["region_name"] == "eu-west-1"
    assert kwargs["aws_access_key_id"] == credentials.access_key_id
    assert kwargs["aws_secret_access_key"] == credentials.secret_access_key
    assert kwargs["aws_session_token"] == credentials.session_token
