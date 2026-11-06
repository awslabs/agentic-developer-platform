"""Synthetic provider and gateway lifecycle measurements for Responses calls."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.proxy import mantle_service
from src.shared.schemas.auth import TokenContext

pytestmark = pytest.mark.usefixtures("unmapped_mantle_routing")

REQUEST_ID = "synthetic-lifecycle-request"
USAGE = b'{"input_tokens":1,"output_tokens":2}'
COMPLETED = b'data: {"type":"response.completed","response":{"usage":' + USAGE + b"}}\n\n"
DELTA = b'data: {"type":"response.output_text.delta","delta":"private-payload"}\n\n'


@pytest.fixture
def clock(monkeypatch):
    elapsed = [0]
    monkeypatch.setattr(mantle_service, "time", SimpleNamespace(monotonic=lambda: elapsed[0] / 1000))

    def advance(milliseconds):
        elapsed[0] += milliseconds

    return advance


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


class StubAuth:
    def __init__(self, advance):
        self.advance = advance

    def sign(self, *args):
        self.advance(5)
        return {}


class ScriptedStream(httpx.AsyncByteStream):
    def __init__(self, advance, chunks):
        self.advance = advance
        self.chunks = chunks

    async def __aiter__(self):
        for delay, chunk in self.chunks:
            self.advance(delay)
            yield chunk

    async def aclose(self):
        self.advance(4)


def lifecycle_record(caplog, message):
    records = [record for record in caplog.records if record.name == "src.proxy.mantle_service" and record.getMessage() == message]
    assert len(records) == 1
    assert records[0].request_id == REQUEST_ID
    return records[0]


@pytest.mark.parametrize("stream", [False, True])
async def test_completed_lifecycle_separates_upstream_wait_from_accounting(context, clock, caplog, stream):
    async def record_usage(*args, **kwargs):
        clock(17)
        return True

    def upstream(request):
        clock(20)
        if stream:
            return httpx.Response(200, stream=ScriptedStream(clock, [(25, DELTA), (15, COMPLETED[:20]), (20, COMPLETED[20:])]))
        return httpx.Response(200, content=b'{"usage":' + USAGE + b"}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        service = mantle_service.MantlePassthroughService(StubAuth(clock), "https://upstream.invalid", http_client=client)
        service._log_usage = AsyncMock(side_effect=record_usage)
        with caplog.at_level(logging.INFO, logger="src.proxy.mantle_service"):
            response = await service.create_response(b"{}", context, stream=stream, model="openai.gpt-6-astra", request_id=REQUEST_ID)
            content = b"".join([chunk async for chunk in response]) if stream else response.content

    assert b'"output_tokens":2' in content
    service._log_usage.assert_awaited_once()
    logged = service._log_usage.await_args.args
    assert logged[2]["input_tokens"] == 1
    assert logged[2]["output_tokens"] == 2
    assert logged[4] == 200
    assert logged[5] == REQUEST_ID
    record = (
        next(record for record in caplog.records if record.name == "src.proxy.mantle_service" and hasattr(record, "upstream_terminal_ms"))
        if stream
        else lifecycle_record(caplog, "mantle response lifecycle")
    )
    assert record.request_id == REQUEST_ID
    assert record.service_before_dispatch_ms == 5
    assert record.service_after_upstream_ms == (21 if stream else 17)
    if stream:
        assert record.upstream_headers_ms == 20
        assert record.upstream_terminal_ms == 80
        assert record.service_lifecycle_ms == 106
        assert record.terminal_event == "response.completed"
    else:
        assert record.upstream_response_ms == 20
        assert record.service_lifecycle_ms == 42
    assert "private-payload" not in caplog.text


async def test_missing_terminal_never_reports_provider_completion(context, clock, caplog):
    def upstream(request):
        clock(20)
        return httpx.Response(200, stream=ScriptedStream(clock, [(25, DELTA)]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        service = mantle_service.MantlePassthroughService(StubAuth(clock), "https://upstream.invalid", http_client=client)
        service._log_usage = AsyncMock(return_value=True)
        with caplog.at_level(logging.INFO, logger="src.proxy.mantle_service"):
            body = b"".join(
                [
                    chunk
                    async for chunk in await service.create_response(b"{}", context, stream=True, model="openai.gpt-6-astra", request_id=REQUEST_ID)
                ]
            )

    assert b"upstream_stream_incomplete" in body
    assert b"response.completed" not in body
    service._log_usage.assert_awaited_once()
    assert service._log_usage.await_args.args[4:6] == (502, REQUEST_ID)
    record = next(record for record in caplog.records if record.name == "src.proxy.mantle_service" and hasattr(record, "upstream_terminal_ms"))
    assert record.request_id == REQUEST_ID
    assert record.stream_outcome == "premature_eof"
    assert record.upstream_headers_ms == 20
    assert record.upstream_terminal_ms is None
    assert record.service_after_upstream_ms is None
    assert record.service_lifecycle_ms >= record.service_before_dispatch_ms + record.upstream_headers_ms
    assert "private-payload" not in caplog.text


@pytest.mark.parametrize("failure", ["transport", "settlement", "cancelled"])
async def test_nonstream_failure_does_not_hide_unobserved_upstream(context, clock, caplog, failure):
    def upstream(request):
        clock(20)
        if failure == "transport":
            raise httpx.ConnectError("private-provider-host")
        if failure == "cancelled":
            raise asyncio.CancelledError()
        return httpx.Response(200, content=b'{"usage":' + USAGE + b"}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        service = mantle_service.MantlePassthroughService(StubAuth(clock), "https://upstream.invalid", http_client=client)
        service._log_usage = AsyncMock(return_value=failure != "settlement")
        with caplog.at_level(logging.INFO, logger="src.proxy.mantle_service"):
            with pytest.raises((httpx.ConnectError, mantle_service.MantleUpstreamError, asyncio.CancelledError)):
                await service.create_response(b"{}", context, stream=False, model="openai.gpt-6-astra", request_id=REQUEST_ID)

    record = lifecycle_record(caplog, "mantle response lifecycle")
    assert record.service_complete is False
    assert record.service_before_dispatch_ms == 5
    assert record.status_code == (499 if failure == "cancelled" else 502)
    assert record.upstream_response_ms == (20 if failure == "settlement" else None)
    assert record.service_after_upstream_ms is not None if failure == "settlement" else record.service_after_upstream_ms is None
    assert "private-provider-host" not in record.getMessage()


@pytest.mark.parametrize("failure,status", [("transport", 502), ("upstream", 403), ("cancelled", 499)])
async def test_preheader_stream_failure_has_no_terminal_measurement(context, clock, caplog, failure, status):
    def upstream(request):
        clock(20)
        if failure == "transport":
            raise httpx.ConnectError("private-provider-host")
        if failure == "cancelled":
            raise asyncio.CancelledError()
        return httpx.Response(403, content=b'{"error":"denied"}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        service = mantle_service.MantlePassthroughService(StubAuth(clock), "https://upstream.invalid", http_client=client)
        service._log_usage = AsyncMock(return_value=True)
        with caplog.at_level(logging.WARNING, logger="src.proxy.mantle_service"):
            with pytest.raises((mantle_service.MantleUpstreamError, asyncio.CancelledError)):
                await service.create_response(b"{}", context, stream=True, model="openai.gpt-6-astra", request_id=REQUEST_ID)

    record = lifecycle_record(caplog, "mantle stream setup lifecycle")
    assert record.status_code == status
    assert record.stream_outcome == "setup_failed"
    assert record.upstream_headers_ms is None
    assert record.upstream_terminal_ms is None
    assert record.service_after_upstream_ms is None
    assert record.service_lifecycle_ms >= 25
    assert "private-provider-host" not in record.getMessage()
