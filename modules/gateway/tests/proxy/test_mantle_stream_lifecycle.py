"""Regressions for upstream quiet periods and missing Responses terminal events."""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from src.proxy.mantle_service import _MAX_SNIFF_BUFFER_BYTES, MantlePassthroughService, _StreamUsageSniffer
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
async def test_interrupted_stream_reports_error_without_replay_or_false_completion(context, caplog, error, status, code):
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
    assert stream.closed
    assert "mantle stream completed" not in caplog.text


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
    assert received == [partial]
    assert stream.closed
    assert service._log_usage.call_args.args[4] in (502, 504)


@pytest.mark.parametrize("kind,status", [("response.completed", 200), ("response.incomplete", 200), ("response.failed", 502), ("error", 502)])
async def test_upstream_terminal_events_are_preserved(context, caplog, kind, status):
    payload = f'data: {{"type":"{kind}"}}\n\n'.encode()
    stream = ScriptedStream([payload[:10], payload[10:]])
    service, client = service_for(stream)
    async with client:
        assert b"".join([c async for c in await open_stream(service, context)]) == payload
    assert service._log_usage.call_args.args[4] == status
    assert stream.closed


async def test_timeout_after_delivered_terminal_does_not_turn_success_into_failure(context):
    stream = ScriptedStream([COMPLETED], httpx.ReadTimeout("trailing connection failure"))
    service, client = service_for(stream)
    async with client:
        assert b"".join([c async for c in await open_stream(service, context)]) == COMPLETED
    assert service._log_usage.call_args.args[4] == 200


async def test_client_close_cancels_upstream_and_records_cancellation(context):
    stream = ScriptedStream([DELTA, COMPLETED])
    service, client = service_for(stream)
    async with client:
        result = await open_stream(service, context)
        assert await anext(result) == DELTA
        await result.aclose()
    assert stream.closed
    assert service._log_usage.call_args.args[4] == 499


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
