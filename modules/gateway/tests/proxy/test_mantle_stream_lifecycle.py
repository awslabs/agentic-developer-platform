"""Regressions for upstream quiet periods and missing Responses terminal events."""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest
from pydantic import ValidationError

from src.proxy.bedrock_enforcement import RoutingDecision
from src.proxy.mantle_service import (
    _MAX_SNIFF_BUFFER_BYTES,
    MantlePassthroughService,
    MantleUpstreamError,
    RoutedMantleRequest,
    _StreamUsageSniffer,
    drain_usage_finalizers,
)
from src.proxy.metered_transport import MeteredAmbiguousSend, MeteredProvenUnsentError, MeteredUnsentCancelled
from src.proxy.stream_handler import merge_with_keepalive
from src.shared.config import Settings
from src.shared.schemas.auth import TokenContext

pytestmark = pytest.mark.usefixtures("unmapped_mantle_routing")

DELTA = b'data: {"type":"response.output_text.delta","delta":"ok","sequence_number":7}\n\n'
COMPLETED = b'data: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":2}}}\n\n'


class StubAuth:
    def sign(self, *args):
        return {}


@pytest.fixture
def context():
    return TokenContext(
        user_id="user",
        org_id="org",
        team_id="team",
        department_id="department",
        account_type="service",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


class ScriptedStream(httpx.AsyncByteStream):
    def __init__(self, chunks, error=None):
        self.chunks = chunks
        self.error = error
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed = True


def service_for(stream):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)))
    service = MantlePassthroughService(StubAuth(), "https://upstream.invalid", http_client=client)
    service._log_usage = AsyncMock()
    return service, client


async def open_stream(service, context):
    return await service.create_response(b"{}", context, stream=True, model="openai.gpt-6-astra", request_id="req-test")


@pytest.mark.parametrize(
    "error,status,code",
    [
        (httpx.ReadTimeout("private upstream details"), 504, "upstream_stream_timeout"),
        (httpx.RemoteProtocolError("private upstream details"), 502, "upstream_stream_error"),
        (None, 502, "upstream_stream_incomplete"),
    ],
)
async def test_interrupted_stream_reports_error_without_replay_or_false_completion(context, caplog, capsys, error, status, code):
    stream = ScriptedStream([DELTA], error)
    service, client = service_for(stream)
    with caplog.at_level(logging.INFO, logger="src.proxy.mantle_service"):
        async with client:
            received = [c async for c in await open_stream(service, context)]
    assert received[0] == DELTA
    assert len(received) == 2
    event = json.loads(received[1].split(b"data: ", 1)[1])
    assert event["type"] == "error"
    assert event["code"] == code
    assert event["sequence_number"] == 8
    assert "req-test" in event["message"]
    assert "private upstream details" not in event["message"]
    assert b"response.completed" not in b"".join(received)
    service._log_usage.assert_awaited_once()
    assert service._log_usage.call_args.args[4] == status
    assert service._log_usage.call_args.kwargs["retain_failed_bound"] is True
    assert stream.closed
    assert "mantle stream completed" not in caplog.text
    exported = [json.loads(line) for line in capsys.readouterr().out.splitlines() if '"StreamOutcome"' in line]
    assert len(exported) == 1
    expected = {"upstream_stream_timeout": "read_timeout", "upstream_stream_error": "transport_error", "upstream_stream_incomplete": "premature_eof"}
    assert exported[0]["stream_outcome"] == expected[code]
    assert exported[0]["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["stream_outcome"]]


async def test_incomplete_stream_metric_excludes_seeded_model_payload(context, caplog, capsys):
    private_delta = DELTA.replace(b'"ok"', b'"test-private-prompt test-private-token test-private-tenant"')
    stream = ScriptedStream([private_delta])
    service, client = service_for(stream)
    async with client:
        received = [chunk async for chunk in await open_stream(service, context)]

    assert received[0] == private_delta
    assert b"upstream_stream_incomplete" in received[1]
    metric_output = capsys.readouterr().out
    assert json.loads(metric_output)["stream_outcome"] == "premature_eof"
    for marker in ("test-private-prompt", "test-private-token", "test-private-tenant"):
        assert marker not in metric_output
        assert marker not in caplog.text


@pytest.mark.parametrize("error", [None, httpx.ReadTimeout("quiet upstream")])
async def test_partial_event_failure_does_not_inject_into_json(context, error):
    partial = b'data: {"type":"response.completed","response":'
    stream = ScriptedStream([partial], error)
    service, client = service_for(stream)
    received = []
    async with client:
        with pytest.raises(httpx.HTTPError):
            async for chunk in await open_stream(service, context):
                received.append(chunk)
    assert received == []  # a partial terminal event cannot escape the settlement barrier
    assert stream.closed
    assert service._log_usage.call_args.args[4] in (502, 504)
    assert service._log_usage.call_args.kwargs["retain_failed_bound"] is True


@pytest.mark.parametrize("kind,status", [("response.completed", 200), ("response.incomplete", 200), ("response.failed", 502), ("error", 502)])
async def test_upstream_terminal_events_are_preserved(context, caplog, kind, status):
    payload = f'data: {{"type":"{kind}"}}\n\n'.encode()
    stream = ScriptedStream([payload[:10], payload[10:]])
    service, client = service_for(stream)
    async with client:
        assert b"".join([c async for c in await open_stream(service, context)]) == payload
    assert service._log_usage.call_args.args[4] == status
    assert not service._log_usage.call_args.kwargs.get("retain_failed_bound", False)
    assert stream.closed


async def test_timeout_after_delivered_terminal_does_not_turn_success_into_failure(context):
    stream = ScriptedStream([COMPLETED], httpx.ReadTimeout("trailing connection failure"))
    service, client = service_for(stream)
    async with client:
        assert b"".join([c async for c in await open_stream(service, context)]) == COMPLETED
    assert service._log_usage.call_args.args[4] == 200
    assert not service._log_usage.call_args.kwargs.get("retain_failed_bound", False)


async def test_client_close_cancels_upstream_and_records_cancellation(context):
    stream = ScriptedStream([DELTA, COMPLETED])
    service, client = service_for(stream)
    async with client:
        result = await open_stream(service, context)
        assert await anext(result) == DELTA
        await result.aclose()
    assert stream.closed
    assert service._log_usage.call_args.args[4] == 499
    assert service._log_usage.call_args.kwargs["retain_failed_bound"] is True


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
def test_terminal_detection_requires_complete_record_across_byte_splits(newline):
    sniffer = _StreamUsageSniffer()
    payload = COMPLETED.replace(b"\n", newline)
    for value in payload[: -len(newline)]:
        sniffer.feed(bytes([value]))
    assert sniffer.terminal_event is None
    sniffer.feed(payload[-len(newline) :])
    assert sniffer.terminal_event == "response.completed"
    assert sniffer.usage == {"input_tokens": 1, "output_tokens": 2}


def test_large_terminal_record_can_be_identified_without_unbounded_usage_buffer():
    sniffer = _StreamUsageSniffer()
    payload = b'event: response.completed\ndata: {"output":"' + b"x" * (_MAX_SNIFF_BUFFER_BYTES + 1) + b'"}\n\n'
    for start in range(0, len(payload), 8192):
        sniffer.feed(payload[start : start + 8192])
        assert len(sniffer._buffer) <= _MAX_SNIFF_BUFFER_BYTES
    assert sniffer.terminal_event == "response.completed"
    assert sniffer.usage == {}


def test_eof_does_not_commit_an_unterminated_terminal_record():
    sniffer = _StreamUsageSniffer()
    sniffer.feed(COMPLETED.rstrip(b"\n"))
    sniffer.finish()
    assert sniffer.terminal_event is None


@pytest.mark.parametrize("line", [b" ", b"\t", b"\xff"])
def test_nonempty_line_is_not_a_terminal_delimiter(line):
    sniffer = _StreamUsageSniffer()
    sniffer.feed(COMPLETED[:-1] + line + b"\n")
    assert sniffer.terminal_event is None
    sniffer.feed(b"\n")
    assert sniffer.terminal_event == "response.completed"


def test_stream_read_timeout_is_configurable_and_bounded(monkeypatch):
    monkeypatch.delenv("BG_MANTLE_STREAM_READ_TIMEOUT_SECONDS", raising=False)
    assert Settings().mantle_stream_read_timeout_seconds == 600
    monkeypatch.setenv("BG_MANTLE_STREAM_READ_TIMEOUT_SECONDS", "900")
    assert Settings().mantle_stream_read_timeout_seconds == 900
    for value in ("0", "-1", "inf", "nan", "3601"):
        monkeypatch.setenv("BG_MANTLE_STREAM_READ_TIMEOUT_SECONDS", value)
        with pytest.raises(ValidationError):
            Settings()


def test_stream_finalization_timeout_is_configurable_and_bounded(monkeypatch):
    monkeypatch.delenv("BG_MANTLE_STREAM_FINALIZATION_TIMEOUT_SECONDS", raising=False)
    assert Settings().mantle_stream_finalization_timeout_seconds == 3
    monkeypatch.setenv("BG_MANTLE_STREAM_FINALIZATION_TIMEOUT_SECONDS", "0.05")
    assert Settings().mantle_stream_finalization_timeout_seconds == 0.05
    for value in ("0", "-1", "inf", "nan", "3.1"):
        monkeypatch.setenv("BG_MANTLE_STREAM_FINALIZATION_TIMEOUT_SECONDS", value)
        with pytest.raises(ValidationError):
            Settings()


@pytest.mark.parametrize("read_timeout,expected_status", [(0.5, 200), (0.03, 504)])
async def test_real_http_quiet_stream_uses_read_timeout_not_general_timeout(context, read_timeout, expected_status):
    """Use real HTTPX socket timeouts; MockTransport does not enforce them."""
    calls = 0
    handlers = set()

    async def upstream(reader, writer):
        nonlocal calls
        handlers.add(asyncio.current_task())
        try:
            await reader.readuntil(b"\r\n\r\n")
            calls += 1
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n" + DELTA)
            await writer.drain()
            await asyncio.sleep(0.12)  # longer than the general request timeout
            writer.write(COMPLETED)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(asyncio.current_task())

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    service = MantlePassthroughService(StubAuth(), f"http://127.0.0.1:{port}", timeout=0.02, stream_read_timeout=read_timeout)
    service._log_usage = AsyncMock()
    try:
        async with server:
            stream = await open_stream(service, context)
            received = [c async for c in merge_with_keepalive(stream, interval_seconds=0.01)]
        payload = b"".join(c for c in received if c != b": keep-alive\n\n")
        assert b": keep-alive\n\n" in received
        assert calls == 1
        assert service._log_usage.call_args.args[4] == expected_status
        if expected_status == 200:
            assert payload == DELTA + COMPLETED
        else:
            assert b"upstream_stream_timeout" in payload
            assert b"response.completed" not in payload
    finally:
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)


async def test_cancelled_connect_preserves_bound_and_propagates_cancellation(context):
    started = asyncio.Event()

    async def connect(request):
        started.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(connect)) as client:
        service = MantlePassthroughService(StubAuth(), "https://upstream.invalid", http_client=client)
        service._log_usage = AsyncMock()
        task = asyncio.create_task(open_stream(service, context))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    service._log_usage.assert_awaited_once()
    assert service._log_usage.call_args.args[4] == 499
    assert service._log_usage.call_args.kwargs["retain_failed_bound"] is True


async def test_disconnect_scope_does_not_cancel_stream_accounting(context):
    # Starlette cancels the streaming task's AnyIO scope on HTTP disconnect.
    class ClosingStream(ScriptedStream):
        async def aclose(self):
            await anyio.sleep(0)
            await super().aclose()

    stream = ClosingStream([DELTA, COMPLETED])
    service, client = service_for(stream)
    logged = []

    async def record(*args, **kwargs):
        await anyio.sleep(0)
        logged.append((args, kwargs))

    service._log_usage.side_effect = record
    async with client:
        result = await open_stream(service, context)
        assert await anext(result) == DELTA
        with anyio.CancelScope() as scope:
            scope.cancel()
            await result.aclose()
    assert stream.closed
    assert len(logged) == 1
    assert logged[0][0][4] == 499
    assert logged[0][1]["retain_failed_bound"] is True


async def test_http_disconnect_finishes_accounting_through_keepalive(context):
    from src.proxy.routes import sse_streaming_response

    reading = asyncio.Event()
    logged = asyncio.Event()

    class WaitingStream(ScriptedStream):
        async def __aiter__(self):
            yield DELTA
            reading.set()
            await asyncio.Event().wait()

        async def aclose(self):
            await anyio.sleep(0)
            await super().aclose()

    stream = WaitingStream([])
    service, client = service_for(stream)

    async def record(*args, **kwargs):
        await anyio.sleep(0)
        logged.set()

    async def receive():
        await reading.wait()
        return {"type": "http.disconnect"}

    service._log_usage.side_effect = record
    async with client:
        response = sse_streaming_response(await open_stream(service, context))
        await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.0"}}, receive, AsyncMock()), 1)
        await asyncio.wait_for(logged.wait(), 1)
    assert stream.closed
    service._log_usage.assert_awaited_once()
    assert service._log_usage.call_args.args[4] == 499
    assert service._log_usage.call_args.kwargs["retain_failed_bound"] is True


@pytest.mark.parametrize("payload,status,usage", [(COMPLETED, 200, {"input_tokens": 1, "output_tokens": 2}), (DELTA, 499, {})])
async def test_disconnect_does_not_cancel_usage_settlement(context, payload, status, usage):
    """Cancel while pricing awaits I/O, after closing the HTTP response."""
    started = asyncio.Event()
    release = asyncio.Event()
    settled = asyncio.Event()
    stream = ScriptedStream([payload])
    service, client = service_for(stream)

    async def settle(*args, **kwargs):
        started.set()
        await release.wait()
        settled.set()
        return True

    service._log_usage.side_effect = settle
    async with asyncio.timeout(10), client:
        result = await open_stream(service, context)
        consumer = None
        try:
            if payload == COMPLETED:
                consumer = asyncio.create_task(anext(result))
            else:
                assert await anext(result) == payload
                consumer = asyncio.create_task(result.aclose())
            await started.wait()
            consumer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await consumer
            release.set()
            await settled.wait()
            if payload == COMPLETED:
                await result.aclose()
            service._log_usage.assert_awaited_once()
            assert service._log_usage.call_args.args[2] == usage
            assert service._log_usage.call_args.args[4] == status
            assert stream.closed
        finally:
            release.set()
            if consumer is not None:
                if not consumer.done():
                    consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
            await result.aclose()


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
@pytest.mark.parametrize("split", [False, True])
async def test_completed_event_waits_for_usage_receipt(context, newline, split):
    started = asyncio.Event()
    release = asyncio.Event()
    delta = DELTA.replace(b"\n", newline)
    completed = COMPLETED.replace(b"\n", newline)
    payload = delta + completed
    chunks = [payload[index : index + 1] for index in range(len(payload))] if split else [payload]
    service, client = service_for(ScriptedStream(chunks))

    async def persist(*args, **kwargs):
        started.set()
        await release.wait()
        return True

    service._log_usage.side_effect = persist
    async with client:
        result = await open_stream(service, context)
        assert await anext(result) == delta
        completion = asyncio.create_task(anext(result))
        await asyncio.wait_for(started.wait(), 1)
        assert not completion.done()
        release.set()
        assert await asyncio.wait_for(completion, 1) == completed
        await result.aclose()
    service._log_usage.assert_awaited_once()


@pytest.mark.parametrize("persist_result", [True, False])
async def test_completed_event_times_out_without_cancelling_settlement_or_reporting_success(context, persist_result):
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    stream = ScriptedStream([DELTA + COMPLETED])
    service, client = service_for(stream)
    service._stream_finalization_timeout = 0.03

    async def persist(*args, **kwargs):
        started.set()
        await release.wait()
        finished.set()
        return persist_result

    service._log_usage.side_effect = persist
    try:
        async with client:
            result = await open_stream(service, context)
            assert await anext(result) == DELTA
            start = asyncio.get_running_loop().time()
            response = b"".join([chunk async for chunk in result])
            elapsed = asyncio.get_running_loop().time() - start
        assert started.is_set()
        assert elapsed < 0.5
        assert response.startswith(b"event: error\n")
        assert b"usage_settlement_unconfirmed" in response
        assert b"response.completed" not in response
        assert not finished.is_set()
        assert stream.closed
        service._log_usage.assert_awaited_once()
    finally:
        release.set()
        assert await drain_usage_finalizers(1) == 0
    assert finished.is_set()


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
@pytest.mark.parametrize("split", [False, True])
async def test_failed_receipt_withholds_completed_event(context, newline, split):
    delta = DELTA.replace(b"\n", newline)
    completed = COMPLETED.replace(b"\n", newline)
    payload = delta + completed
    chunks = [payload[index : index + 1] for index in range(len(payload))] if split else [payload]
    service, client = service_for(ScriptedStream(chunks))
    service._log_usage.return_value = False
    async with client:
        result = await open_stream(service, context)
        payload = b"".join([chunk async for chunk in result])
    assert payload.startswith(delta)
    assert b"response.completed" not in payload
    assert b"usage_settlement_unconfirmed" in payload
    service._log_usage.assert_awaited_once()


@pytest.mark.parametrize("split", [False, True])
async def test_complete_oversized_event_is_refused_before_forwarding(context, split):
    payload = b'data: {"type":"response.output_text.delta","delta":"' + b"x" * _MAX_SNIFF_BUFFER_BYTES + b'"}\n\n'
    chunks = [payload[:100], payload[100:]] if split else [payload]
    service, client = service_for(ScriptedStream(chunks))
    received = []
    async with client:
        with pytest.raises(httpx.RemoteProtocolError, match="settlement buffer limit"):
            async for chunk in await open_stream(service, context):
                received.append(chunk)
    assert received == []
    assert service._log_usage.call_args.kwargs["retain_failed_bound"] is True


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure_kind", [MeteredProvenUnsentError, MeteredUnsentCancelled, MeteredAmbiguousSend])
async def test_serving_transport_only_cancels_proven_unsent_armed_attempt(context, monkeypatch, stream, failure_kind):
    permit = object()
    failure = failure_kind("ambiguous") if failure_kind is MeteredAmbiguousSend else failure_kind(permit)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    transport = SimpleNamespace(dispatch_once=AsyncMock(side_effect=failure))
    metering = SimpleNamespace(cancel_armed_unsent=AsyncMock())
    service = MantlePassthroughService(StubAuth(), "https://upstream.invalid", http_client=client, metering=metering, metered_transport=transport)
    service._arm_metering = AsyncMock(return_value=permit)
    monkeypatch.setattr("src.agentauth.native_test_model.begin_native_model_dispatch", AsyncMock())
    attempt = object()
    routed = RoutedMantleRequest(StubAuth(), "https://upstream.invalid", "", RoutingDecision())
    async with client:
        if failure_kind is MeteredUnsentCancelled:
            expected = MeteredUnsentCancelled
        elif failure_kind is MeteredAmbiguousSend and not stream:
            expected = MeteredAmbiguousSend
        else:
            expected = MantleUpstreamError
        with pytest.raises(expected) as outcome:
            if stream:
                async for _ in await service._stream(
                    b"{}",
                    context,
                    model="local-model",
                    request_id="local-request",
                    agent_run_id=None,
                    routed=routed,
                    attempt=attempt,
                    service_start=0,
                ):
                    pass
            else:
                await service._invoke(
                    b"{}",
                    context,
                    model="local-model",
                    request_id="local-request",
                    agent_run_id=None,
                    routed=routed,
                    attempt=attempt,
                    service_start=0,
                )
    if failure_kind is MeteredAmbiguousSend:
        metering.cancel_armed_unsent.assert_not_awaited()
    elif failure_kind is MeteredProvenUnsentError:
        assert outcome.value.status_code == 503
    else:
        metering.cancel_armed_unsent.assert_awaited_once_with(attempt, failure)
    transport.dispatch_once.assert_awaited_once()


def test_whole_operation_timeout_is_configurable_and_bounded(monkeypatch):
    monkeypatch.delenv("BG_MANTLE_OPERATION_TIMEOUT_SECONDS", raising=False)
    assert Settings().mantle_operation_timeout_seconds is None
    monkeypatch.setenv("BG_MANTLE_OPERATION_TIMEOUT_SECONDS", "0.05")
    assert Settings().mantle_operation_timeout_seconds == 0.05
    for value in ("0", "-1", "inf", "nan", "1441"):
        monkeypatch.setenv("BG_MANTLE_OPERATION_TIMEOUT_SECONDS", value)
        with pytest.raises(ValidationError):
            Settings()


async def test_continuous_chunks_cannot_extend_whole_operation_deadline(context):
    class DrippingStream(httpx.AsyncByteStream):
        def __init__(self):
            self.closed = False

        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.005)
                yield DELTA

        async def aclose(self):
            self.closed = True

    upstream = DrippingStream()
    service, client = service_for(upstream)
    service._operation_timeout = 0.05
    service._stream_finalization_timeout = 0.03
    async with client:
        started = asyncio.get_running_loop().time()
        received = [chunk async for chunk in await open_stream(service, context)]
        assert asyncio.get_running_loop().time() - started < 0.3
    assert len(received) > 1
    assert all(chunk == DELTA for chunk in received[:-1])
    assert b'"code": "upstream_operation_timeout"' in received[-1]
    assert b"response.completed" not in b"".join(received)
    assert service._log_usage.call_args.args[4] == 504
    service._log_usage.assert_awaited_once()
    assert upstream.closed


async def test_partial_record_at_deadline_closes_without_fabricating_terminal(context):
    class PartialStream(ScriptedStream):
        async def __aiter__(self):
            yield DELTA + b'data: {"type":"response.output_text.delta"'
            await asyncio.sleep(0.1)
            yield COMPLETED

    upstream = PartialStream([])
    service, client = service_for(upstream)
    service._operation_timeout = 0.03
    service._stream_finalization_timeout = 0.02
    received = []
    async with client:
        with pytest.raises(httpx.ReadTimeout):
            async for chunk in await open_stream(service, context):
                received.append(chunk)
    assert b"response.completed" not in b"".join(received)
    assert b"event: error" not in b"".join(received)
    assert upstream.closed
    assert service._log_usage.call_args.args[4] == 504


@pytest.mark.parametrize("stream", [True, False])
async def test_precontent_operation_deadline_is_failure_with_one_attempt(context, stream):
    submitted = 0
    interrupted = asyncio.Event()

    async def never_finishes(request):
        nonlocal submitted
        submitted += 1
        try:
            await asyncio.Event().wait()
        finally:
            interrupted.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(never_finishes)) as client:
        service = MantlePassthroughService(StubAuth(), "https://upstream.invalid", http_client=client, operation_timeout=0.03)
        service._log_usage = AsyncMock()
        with pytest.raises(MantleUpstreamError) as failure:
            await service.create_response(b"{}", context, stream=stream, model="openai.gpt-6-astra")
    assert failure.value.status_code == 504
    assert b"upstream_operation_timeout" in failure.value.content
    assert submitted == 1
    await asyncio.wait_for(interrupted.wait(), 0.5)


async def test_slow_upstream_close_does_not_hold_interrupted_stream(context):
    close_started = asyncio.Event()

    class SlowCloseStream(ScriptedStream):
        async def aclose(self):
            close_started.set()
            await asyncio.sleep(0.08)
            await super().aclose()

    upstream = SlowCloseStream([DELTA])
    service, client = service_for(upstream)
    service._operation_timeout = 0.04
    service._stream_finalization_timeout = 0.02
    async with client:
        started = asyncio.get_running_loop().time()
        received = [chunk async for chunk in await open_stream(service, context)]
        assert asyncio.get_running_loop().time() - started < 0.3
    assert close_started.is_set()
    assert b"response.completed" not in b"".join(received)
    assert b"upstream_operation_timeout" in received[-1]
    assert await drain_usage_finalizers(0.2) == 0
    assert upstream.closed


async def test_deadline_after_confirmed_terminal_does_not_report_new_failure(context):
    class AfterTerminal(ScriptedStream):
        async def __aiter__(self):
            yield COMPLETED
            while True:
                await asyncio.sleep(0.005)
                yield DELTA

    upstream = AfterTerminal([])
    service, client = service_for(upstream)
    service._operation_timeout = 0.04
    service._log_usage.return_value = True
    async with client:
        received = [chunk async for chunk in await open_stream(service, context)]
    assert received[0] == COMPLETED
    assert b"event: error" not in b"".join(received)
    assert b"".join(received).count(b"response.completed") == 1
    assert upstream.closed
    assert service._log_usage.call_args.args[4] == 200


@pytest.mark.parametrize("stream", [True, False])
async def test_owned_client_close_does_not_extend_configured_header_deadline(context, stream):
    submitted = 0
    closed = asyncio.Event()

    class OwnedClient:
        def build_request(self, method, url, **kwargs):
            return httpx.Request(method, url)

        async def send(self, *args, **kwargs):
            nonlocal submitted
            submitted += 1
            await asyncio.Event().wait()

        async def post(self, *args, **kwargs):
            nonlocal submitted
            submitted += 1
            await asyncio.Event().wait()

        async def aclose(self):
            await asyncio.sleep(0.3)
            closed.set()

    owned = OwnedClient()
    service = MantlePassthroughService(StubAuth(), "https://upstream.invalid", operation_timeout=0.03)
    service._client = lambda: owned
    service._log_usage = AsyncMock(return_value=True)
    started = asyncio.get_running_loop().time()
    with pytest.raises(MantleUpstreamError) as failure:
        await service.create_response(b"{}", context, stream=stream, model="openai.gpt-6-astra")
    assert asyncio.get_running_loop().time() - started < 0.15
    assert failure.value.status_code == 504
    assert submitted == 1
    assert await drain_usage_finalizers(1) == 0
    assert closed.is_set()
    assert service._log_usage.await_count == 1, [
        (call.args[4], call.kwargs.get("retain_failed_bound")) for call in service._log_usage.await_args_list
    ]
    assert service._log_usage.call_args.kwargs["retain_failed_bound"] is True


async def test_slow_read_cancellation_and_body_close_cannot_extend_stream_deadline(context):
    class SlowEofStream(httpx.AsyncByteStream):
        def __init__(self):
            self.closed = False

        async def __aiter__(self):
            yield DELTA
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.3)
                raise

        async def aclose(self):
            await asyncio.sleep(0.3)
            self.closed = True

    upstream = SlowEofStream()
    service, client = service_for(upstream)
    service._operation_timeout = 0.03
    started = asyncio.get_running_loop().time()
    received = []
    async with client:
        async for chunk in await open_stream(service, context):
            received.append(chunk)
    assert asyncio.get_running_loop().time() - started < 0.15
    assert b"response.completed" not in b"".join(received)
    assert b"upstream_operation_timeout" in received[-1]
    assert await drain_usage_finalizers(1) == 0
    assert upstream.closed


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("owns_client", [False, True])
async def test_late_stream_headers_close_unstarted_response_after_timeout_or_disconnect(context, cancel, owns_client):
    upstream = ScriptedStream([COMPLETED])
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def delayed_response(request):
        calls.append(request)
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
        return httpx.Response(200, stream=upstream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(delayed_response)) as client:
        service = MantlePassthroughService(
            StubAuth(), "https://upstream.invalid", http_client=client if not owns_client else None, operation_timeout=None if cancel else 0.03
        )
        service._log_usage = AsyncMock(return_value=True)
        if owns_client:
            service._client = lambda: client
        task = asyncio.create_task(service.create_response(b"{}", context, stream=True, model="openai.gpt-6-astra"))
        await asyncio.wait_for(started.wait(), 1)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        else:
            with pytest.raises(MantleUpstreamError) as failure:
                await asyncio.wait_for(task, 1)
            assert failure.value.status_code == 504
        release.set()
        assert await drain_usage_finalizers(1) == 0
        assert upstream.closed
        assert len(calls) == 1
        assert service._log_usage.await_count == 1
        assert service._log_usage.await_args.kwargs["retain_failed_bound"] is True


async def test_completed_stream_does_not_cancel_tracked_upstream_close(context):
    class SlowCloseResponse(httpx.Response):
        def __init__(self):
            super().__init__(200, stream=ScriptedStream([COMPLETED]))
            self.close_cancelled = False
            self.close_finished = False

        async def aclose(self):
            try:
                await asyncio.sleep(0.08)
            except asyncio.CancelledError:
                self.close_cancelled = True
                raise
            await super().aclose()
            self.close_finished = True

    upstream = SlowCloseResponse()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: upstream)) as client:
        service = MantlePassthroughService(StubAuth(), "https://upstream.invalid", http_client=client, operation_timeout=0.5)
        service._stream_finalization_timeout = 0.02
        service._log_usage = AsyncMock(return_value=True)
        payload = b"".join([chunk async for chunk in await open_stream(service, context)])
        assert payload.count(b"response.completed") == 1
        assert await drain_usage_finalizers(1) == 0
    assert upstream.close_finished
    assert not upstream.close_cancelled


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("owns_client", [False, True])
@pytest.mark.parametrize("eof", [False, True])
async def test_interrupted_stream_retains_slow_body_close(context, cancel, owns_client, eof):
    operation_timeout = 5
    wait_timeout = operation_timeout + 5
    read_started = asyncio.Event()
    close_started = asyncio.Event()
    close_release = asyncio.Event()
    physical_calls = []

    class SlowCloseStream(ScriptedStream):
        close_cancelled = False

        async def __aiter__(self):
            yield DELTA
            read_started.set()
            if not eof:
                await asyncio.Event().wait()

        async def aclose(self):
            close_started.set()
            try:
                await close_release.wait()
                await asyncio.sleep(0.08)
            except asyncio.CancelledError:
                self.close_cancelled = True
                raise
            await super().aclose()

    upstream = SlowCloseStream([])

    def respond(request):
        physical_calls.append(request)
        return httpx.Response(200, stream=upstream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service = MantlePassthroughService(
            StubAuth(), "https://upstream.invalid", http_client=None if owns_client else client, operation_timeout=operation_timeout
        )
        if owns_client:
            service._client = lambda: client
        service._stream_finalization_timeout = 0.02
        service._log_usage = AsyncMock(return_value=True)
        result = await open_stream(service, context)
        assert await anext(result) == DELTA

        async def consume():
            return b"".join([chunk async for chunk in result])

        consumer = asyncio.create_task(consume())
        try:
            await asyncio.wait_for((close_started if eof else read_started).wait(), wait_timeout)
            if cancel:
                consumer.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(consumer, wait_timeout)
            else:
                payload = await asyncio.wait_for(consumer, wait_timeout)
                assert b"upstream_operation_timeout" in payload
                assert b"response.completed" not in payload
            await asyncio.wait_for(close_started.wait(), wait_timeout)
            assert not upstream.closed
            assert not upstream.close_cancelled
            assert not client.is_closed
            assert await drain_usage_finalizers(0.01) >= 1
        finally:
            close_release.set()
            if not consumer.done():
                consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            assert await drain_usage_finalizers(wait_timeout) == 0
        assert upstream.closed
        assert not upstream.close_cancelled
        assert client.is_closed is owns_client
    assert len(physical_calls) == 1
    service._log_usage.assert_awaited_once()
    assert service._log_usage.call_args.args[4] == (499 if cancel else 504)
    assert service._log_usage.call_args.kwargs["retain_failed_bound"] is True
