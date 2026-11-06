"""Bounded client lifecycle and SDK-attempt classes without cloud credentials."""

import asyncio
import contextvars
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.pool.simple_pool import SDK_BEFORE_SEND, SimplePoolService
from src.proxy.exceptions import BedrockInvocationError
from src.proxy.schemas import BedrockInvokeRequest
from src.proxy.service import ProxyService


class Body:
    def __init__(self):
        self.closed = False

    def __iter__(self):
        yield {"chunk": {"bytes": json.dumps({"type": "message_start"}).encode()}}
        yield {"chunk": {"bytes": json.dumps({"type": "message_stop"}).encode()}}

    def close(self):
        self.closed = True


class LocalClient:
    def __init__(self, credentials_id):
        self.credentials_id = credentials_id
        self.registered = {}
        self.meta = SimpleNamespace(events=SimpleNamespace(register=lambda name, callback: self.registered.update({name: callback})))
        self.close = MagicMock()
        self.body = None
        self.error = None

    def invoke_model_with_response_stream(self, **kwargs):
        self.registered["before-send.bedrock-runtime.InvokeModelWithResponseStream"]()
        if self.error:
            raise self.error
        self.body = Body()
        return {"body": self.body}


@pytest.fixture
def local_clients(monkeypatch):
    built = []

    def create_client(service, **kwargs):
        assert service == "bedrock-runtime"
        client = LocalClient(kwargs.get("aws_access_key_id", "ambient"))
        built.append(client)
        return client

    monkeypatch.setattr("src.pool.simple_pool.boto3.client", create_client)
    return built


@pytest.mark.asyncio
async def test_ambient_cold_then_reused_client_and_shutdown(local_clients):
    pool = SimplePoolService(region="us-east-1")
    client = await pool.get_client()
    assert await pool.get_client() is client
    cold = SDK_BEFORE_SEND.labels(operation="stream", client_class="ambient_cold")._value.get()
    reused = SDK_BEFORE_SEND.labels(operation="stream", client_class="ambient_reused")._value.get()
    try:
        for _ in range(2):
            response = await client.invoke_model_with_response_stream(modelId="local-model", body=b"{}")
            response["body"].close()
        assert client._http_attempts["stream"] == 2
        assert SDK_BEFORE_SEND.labels(operation="stream", client_class="ambient_cold")._value.get() == cold + 1
        assert SDK_BEFORE_SEND.labels(operation="stream", client_class="ambient_reused")._value.get() == reused + 1
    finally:
        await pool.aclose()
        await pool.aclose()
    assert all(item.close.call_count == 1 for item in local_clients)
    assert (await pool.get_client()) is not client
    await pool.aclose()


@pytest.mark.asyncio
async def test_disjoint_concurrent_routed_clients_close_after_reader(local_clients):
    pool = SimplePoolService(region="us-east-1")
    proxy = ProxyService.__new__(ProxyService)
    request = BedrockInvokeRequest(max_tokens=10, messages=[])
    routed_attempts = SDK_BEFORE_SEND.labels(operation="stream", client_class="routed_disposable")._value.get()

    async def invoke(credentials_id):
        credentials = SimpleNamespace(
            region="us-east-1", as_boto3_kwargs=lambda: {"aws_access_key_id": credentials_id, "aws_secret_access_key": "local-only"}
        )
        client = await pool.get_client(credentials)
        generator = proxy._invoke_bedrock_stream(client, "local-model", request)
        assert json.loads(await anext(generator))["type"] == "message_start"
        await generator.aclose()
        assert client._disposable and client._closed
        return client

    first, second = await asyncio.gather(invoke("local-a"), invoke("local-b"))
    assert first is not second
    assert [item.credentials_id for item in local_clients] == ["local-a", "local-a", "local-b", "local-b"]
    assert all(item.close.call_count == 1 for item in local_clients)
    assert all(item.body.closed for item in local_clients if item.body is not None)
    assert SDK_BEFORE_SEND.labels(operation="stream", client_class="routed_disposable")._value.get() == routed_attempts + 2


@pytest.mark.asyncio
async def test_routed_client_closes_when_sdk_fails_before_body(local_clients):
    pool = SimplePoolService(region="us-east-1")
    credentials = SimpleNamespace(
        region="us-east-1", as_boto3_kwargs=lambda: {"aws_access_key_id": "local-failure", "aws_secret_access_key": "local-only"}
    )
    client = await pool.get_client(credentials)
    client._streaming_client.error = RuntimeError("local socket closed")
    proxy = ProxyService.__new__(ProxyService)
    with pytest.raises(Exception):
        async for _ in proxy._invoke_bedrock_stream(client, "local-model", BedrockInvokeRequest(max_tokens=10, messages=[])):
            pass
    assert client._closed
    assert all(item.close.call_count == 1 for item in local_clients)


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_routed_token_count_always_closes_client(monkeypatch, fails):
    client = SimpleNamespace(
        _disposable=True,
        count_tokens=AsyncMock(side_effect=RuntimeError("local failure") if fails else None, return_value={"inputTokens": 3}),
        aclose=AsyncMock(),
    )
    proxy = ProxyService.__new__(ProxyService)
    monkeypatch.setattr(ProxyService, "_client_for_request", AsyncMock(return_value=client))
    monkeypatch.setattr("src.proxy.service.resolve_routing_decision", AsyncMock(return_value=SimpleNamespace(credentials=object())))
    if fails:
        with pytest.raises(RuntimeError, match="local failure"):
            await proxy.count_bedrock_tokens("local-model", {}, SimpleNamespace())
    else:
        assert await proxy.count_bedrock_tokens("local-model", {}, SimpleNamespace()) == 3
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_client_construction_closes_first_client(local_clients, monkeypatch):
    def fail_second(service, **kwargs):
        if local_clients:
            raise RuntimeError("local construction failure")
        client = LocalClient("local")
        local_clients.append(client)
        return client

    monkeypatch.setattr("src.pool.simple_pool.boto3.client", fail_second)
    with pytest.raises(RuntimeError, match="local construction failure"):
        await SimplePoolService(region="us-east-1").get_client()
    local_clients[0].close.assert_called_once()


@pytest.mark.asyncio
async def test_reader_deadline_closes_stalled_body_without_blocking_other_requests():
    reading = threading.Event()
    released = threading.Event()
    finished = threading.Event()
    close_finished = threading.Event()
    client_closed = asyncio.Event()

    close_started = threading.Event()

    class StalledBody:
        def __iter__(self):
            try:
                yield {"chunk": {"bytes": b'{"type":"message_start"}'}}
                reading.set()
                released.wait(2)
                return
            finally:
                finished.set()

        def close(self):
            close_started.set()
            time.sleep(0.22)
            released.set()
            close_finished.set()

    body = StalledBody()
    client = SimpleNamespace(
        _disposable=True,
        invoke_model_with_response_stream=AsyncMock(return_value={"body": body}),
        aclose=AsyncMock(side_effect=client_closed.set),
    )
    proxy = ProxyService.__new__(ProxyService)
    generator = proxy._invoke_bedrock_stream(client, "local-model", BedrockInvokeRequest(max_tokens=10, messages=[]), operation_timeout=0.06)
    assert json.loads(await anext(generator))["type"] == "message_start"
    reader_task = asyncio.create_task(anext(generator))
    assert await asyncio.to_thread(close_started.wait, 0.2)
    ticker = asyncio.create_task(asyncio.sleep(0.03))
    await asyncio.wait_for(ticker, 0.12)
    assert not close_finished.is_set()
    with pytest.raises(Exception):
        await asyncio.wait_for(reader_task, 1)
    assert await asyncio.to_thread(close_finished.wait, 0.5)
    assert await asyncio.to_thread(finished.wait, 0.5)
    await asyncio.wait_for(client_closed.wait(), 0.5)
    assert reading.is_set()
    client.invoke_model_with_response_stream.assert_awaited_once()
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("defer_close", [False, True])
async def test_continuous_chunks_cannot_extend_bedrock_deadline(defer_close):
    release_close = threading.Event()
    close_completed = threading.Event()
    if not defer_close:
        release_close.set()

    class ContinuousBody:
        def __init__(self):
            self.closed = False
            self.reads = 0

        def __iter__(self):
            while not self.closed:
                time.sleep(0.008)
                self.reads += 1
                yield {"chunk": {"bytes": b'{"type":"content_block_delta"}'}}

        def close(self):
            release_close.wait(2)
            self.closed = True
            close_completed.set()

    body = ContinuousBody()
    client = SimpleNamespace(_disposable=False, invoke_model_with_response_stream=AsyncMock(return_value={"body": body}))
    proxy = ProxyService.__new__(ProxyService)
    chunks = []
    try:
        with pytest.raises(BedrockInvocationError) as failure:
            async with asyncio.timeout(1):
                async for chunk in proxy._invoke_bedrock_stream(
                    client, "local-model", BedrockInvokeRequest(max_tokens=10, messages=[]), operation_timeout=0.07
                ):
                    chunks.append(chunk)
    finally:
        release_close.set()
        cleanup_finished = await asyncio.to_thread(close_completed.wait, 1)
    assert isinstance(failure.value.__cause__, TimeoutError)
    assert chunks and cleanup_finished and body.closed
    assert body.reads < 20
    client.invoke_model_with_response_stream.assert_awaited_once()


@pytest.mark.asyncio
async def test_disposable_sdk_close_does_not_block_event_loop(local_clients):
    pool = SimplePoolService(region="us-east-1")
    credentials = SimpleNamespace(region="us-east-1", as_boto3_kwargs=lambda: {"aws_access_key_id": "local", "aws_secret_access_key": "local"})
    client = await pool.get_client(credentials)
    for raw in local_clients:
        raw.close.side_effect = lambda: time.sleep(0.22)
    ticker = asyncio.create_task(asyncio.sleep(0.03))
    closing = asyncio.create_task(client.aclose())
    await asyncio.wait_for(ticker, 0.12)
    assert not closing.done()
    await asyncio.wait_for(closing, 1)
    assert all(raw.close.call_count == 1 for raw in local_clients)
    local_clients[0].close.assert_called_once()


@pytest.mark.asyncio
async def test_late_sdk_response_body_is_closed_after_deadline():
    invoked = asyncio.Event()
    resume = asyncio.Event()
    body = Body()

    async def invoke(**kwargs):
        invoked.set()
        await resume.wait()
        return {"body": body}

    client = SimpleNamespace(_disposable=True, invoke_model_with_response_stream=AsyncMock(side_effect=invoke), aclose=AsyncMock())
    proxy = ProxyService.__new__(ProxyService)
    with pytest.raises(Exception):
        await asyncio.wait_for(
            anext(proxy._invoke_bedrock_stream(client, "local-model", BedrockInvokeRequest(max_tokens=10, messages=[]), operation_timeout=0.03)),
            1,
        )
    assert invoked.is_set()
    assert not body.closed
    resume.set()
    for _ in range(20):
        if body.closed:
            break
        await asyncio.sleep(0.01)
    assert body.closed
    client.invoke_model_with_response_stream.assert_awaited_once()
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation_timeout", [None, 0.03])
async def test_queued_sdk_submission_is_revoked_after_stream_deadline(local_clients, operation_timeout):
    loop = asyncio.get_running_loop()
    previous_executor = loop._default_executor
    release = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)
    blocked = executor.submit(release.wait)
    try:
        loop.set_default_executor(executor)
        pool = SimplePoolService(region="us-east-1")
        credentials = SimpleNamespace(region="us-east-1", as_boto3_kwargs=lambda: {"aws_access_key_id": "local", "aws_secret_access_key": "local"})
        client = await pool.get_client(credentials)
        proxy = ProxyService.__new__(ProxyService)
        generator = proxy._invoke_bedrock_stream(
            client, "local-model", BedrockInvokeRequest(max_tokens=10, messages=[]), operation_timeout=operation_timeout
        )
        with pytest.raises(Exception):
            await asyncio.wait_for(anext(generator), 0.4 if operation_timeout is not None else 0.03)
        assert not any(raw.body for raw in local_clients)
    finally:
        release.set()
        await asyncio.to_thread(blocked.result, 1)
        await asyncio.sleep(0.05)
        loop._default_executor = previous_executor
        executor.shutdown(wait=True)
    assert all(raw.body is None for raw in local_clients)
    assert client._closed


@pytest.mark.asyncio
async def test_stream_span_detaches_in_caller_task(monkeypatch):
    current_span = contextvars.ContextVar("local_stream_span", default="parent")

    class ContextSpan:
        def __enter__(self):
            self.token = current_span.set("stream")
            self.owner = asyncio.current_task()
            return self

        def __exit__(self, *args):
            assert asyncio.current_task() is self.owner
            current_span.reset(self.token)

        def set_attribute(self, *args):
            pass

        def record_exception(self, *args):
            pass

    monkeypatch.setattr("src.shared.tracing.get_tracer", lambda name: SimpleNamespace(start_as_current_span=lambda *args, **kwargs: ContextSpan()))
    body = Body()
    client = SimpleNamespace(_disposable=True, invoke_model_with_response_stream=AsyncMock(return_value={"body": body}), aclose=AsyncMock())
    proxy = ProxyService.__new__(ProxyService)
    generator = proxy._invoke_bedrock_stream(client, "local-model", BedrockInvokeRequest(max_tokens=10, messages=[]))
    assert current_span.get() == "parent"
    await anext(generator)
    assert current_span.get() == "stream"
    await generator.aclose()
    assert current_span.get() == "parent"
    assert body.closed
