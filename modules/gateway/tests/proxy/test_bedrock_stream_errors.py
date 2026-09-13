"""#5025: real route/service/logging failures in both native wire formats."""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from botocore.eventstream import EventStream, EventStreamBuffer
from botocore.exceptions import ClientError, EventStreamError
from botocore.parsers import EventStreamJSONParser
from botocore.session import get_session
from fastapi import FastAPI
from starlette.requests import ClientDisconnect

from src.proxy import routes
from src.proxy.bedrock_enforcement import RoutingDecision
from src.proxy.bedrock_routing import BedrockTarget
from src.proxy.service import ProxyService
from src.proxy.stream_handler import merge_with_keepalive
from tests.proxy.conftest import MockPoolService
from tests.proxy.test_claude_pricing_decision import FORWARDED, USAGE, Client, flush_logs, metering  # noqa: F401

ACCOUNT = "605440105851"
DENIAL = ClientError(
    {"Error": {"Code": "AccessDeniedException", "Message": "Denied for arn:aws:iam::605440105851:role/private-role"}},
    "InvokeModelWithResponseStream",
)


class Body:
    def __init__(self, chunks=(), error=None):
        self.chunks = chunks
        self.error = error
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            yield {"chunk": {"bytes": json.dumps(chunk).encode()}}
        if self.error:
            raise self.error

    def close(self):
        self.closed = True


@pytest.fixture
def native(monkeypatch, metering):  # noqa: F811 - imported pytest fixture
    client = Client(USAGE)
    pool = MockPoolService(client)
    proxy = ProxyService(pool)
    decision = RoutingDecision(target=BedrockTarget(account_id=ACCOUNT, rung="team"), credentials=object())
    monkeypatch.setattr("src.proxy.service.resolve_routing_decision", AsyncMock(return_value=decision))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_token_context] = lambda: metering.context
    app.dependency_overrides[routes.get_proxy_service] = lambda: proxy
    return SimpleNamespace(app=app, client=client, pool=pool, decision=decision, metering=metering)


async def request(native, accept):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=native.app, raise_app_exceptions=False), base_url="http://test") as http:
        response = await http.post(
            f"/model/{FORWARDED}/invoke-with-response-stream",
            json={"max_tokens": 80, "messages": [{"role": "user", "content": "hello"}]},
            headers={"accept": accept},
        )
    await flush_logs()
    return response


def assert_account_error(detail):
    assert detail["error"] == "bedrock_account_unavailable"
    assert detail["details"]["account_id"] == ACCOUNT
    assert detail["details"]["scope"] == "team"
    assert detail["details"]["remediation"]
    assert "private-role" not in json.dumps(detail)


def assert_failed_usage(native):
    assert native.pool.get_client_credentials == [native.decision.credentials]
    native.metering.usage.log_request.assert_awaited_once()
    usage = native.metering.usage.log_request.await_args.kwargs
    assert usage["status_code"] == 502
    assert usage["bedrock_account_id"] == ACCOUNT
    assert usage["cost_usd"] == 0
    native.metering.writer.write_log.assert_not_awaited()


@pytest.mark.parametrize("accept", ["*/*", "text/event-stream"])
async def test_initial_destination_denial_is_structured_502(native, accept):
    native.client.error = DENIAL
    response = await request(native, accept)
    assert response.status_code == 502, response.content
    assert response.headers["content-type"] == "application/json"
    assert_account_error(response.json()["detail"])
    assert len(native.client.invoke_calls) == 1
    assert_failed_usage(native)


@pytest.mark.parametrize("accept", ["*/*", "text/event-stream"])
async def test_denial_after_first_chunk_is_terminal_wire_error(native, accept):
    body = Body([{"type": "message_start", "message": {"id": "partial", "usage": {"input_tokens": 10}}}], DENIAL)
    native.client.invoke_model_with_response_stream = AsyncMock(return_value={"body": body})
    response = await request(native, accept)
    assert response.status_code == 200
    if accept == "text/event-stream":
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert [event["type"] for event in events] == ["message_start", "error"]
        error = events[-1]["error"]
        assert_account_error({**error, "error": error["type"]})
    else:
        decoder = EventStreamBuffer()
        decoder.add_data(response.content)
        frames = list(decoder)
        assert len(frames) == 2
        assert frames[-1].headers[":message-type"] == "exception"
        assert frames[-1].headers[":exception-type"] == "modelStreamErrorException"
        payload = json.loads(frames[-1].payload)
        assert payload["originalStatusCode"] == 502
        assert_account_error(json.loads(payload["originalMessage"]))
        # Exercise the actual SDK iterator too: this frame must raise, not be
        # decoded as an ordinary empty model chunk. Botocore exposes Message;
        # the wire also retains the structured originalMessage for other SDKs.
        shape = get_session().get_service_model("bedrock-runtime").operation_model("InvokeModelWithResponseStream").output_shape.members["body"]
        sdk_stream = EventStream(
            SimpleNamespace(stream=lambda: iter([response.content])), shape, EventStreamJSONParser(), "InvokeModelWithResponseStream"
        )
        sdk_events = iter(sdk_stream)
        assert "chunk" in next(sdk_events)
        with pytest.raises(EventStreamError) as error:
            next(sdk_events)
        assert error.value.response["Error"]["Code"] == "modelStreamErrorException"
        assert ACCOUNT in str(error.value)
        assert "platform admin" in str(error.value)
    assert body.closed
    native.client.invoke_model_with_response_stream.assert_awaited_once()
    assert_failed_usage(native)


@pytest.mark.parametrize("accept", ["*/*", "text/event-stream"])
async def test_empty_upstream_is_failure_and_releases_body(native, accept):
    body = Body()
    native.client.invoke_model_with_response_stream = AsyncMock(return_value={"body": body})
    response = await request(native, accept)
    assert response.status_code == 502
    assert response.json()["detail"]["error"] == "bedrock_invocation_error"
    assert body.closed
    assert_failed_usage(native)


@pytest.mark.parametrize("accept", ["*/*", "text/event-stream"])
async def test_success_still_streams_and_records_one_measured_charge(native, accept):
    body = Body(native.client.stream_chunks)
    native.client.invoke_model_with_response_stream = AsyncMock(return_value={"body": body, "serviceTier": "standard"})
    response = await request(native, accept)
    assert response.status_code == 200
    if accept == "text/event-stream":
        assert response.headers["content-type"].startswith("text/event-stream")
        assert b"event: message_stop" in response.content
    else:
        assert response.headers["content-type"] == "application/vnd.amazon.eventstream"
        decoder = EventStreamBuffer()
        decoder.add_data(response.content)
        frames = list(decoder)
        assert len(frames) == len(body.chunks)
        assert all(frame.headers[":event-type"] == "chunk" for frame in frames)
    assert body.closed
    assert native.pool.get_client_credentials == [native.decision.credentials]
    native.metering.usage.log_request.assert_awaited_once()
    usage = native.metering.usage.log_request.await_args.kwargs
    assert usage["status_code"] == 200
    assert usage["cost_usd"] > 0
    assert usage["bedrock_account_id"] == ACCOUNT
    native.metering.writer.write_log.assert_awaited_once()


@pytest.mark.parametrize("accept", ["*/*", "text/event-stream"])
async def test_slow_initial_denial_after_keepalive_is_explicit(native, monkeypatch, accept):
    release = asyncio.Event()

    async def delayed_denial(**kwargs):
        await release.wait()
        raise DENIAL

    # Deterministically release the rejected call only after a real keepalive
    # has reached the wire; no timing race between the denial and heartbeat.
    async def keepalive(source, **kwargs):
        async for chunk in merge_with_keepalive(source, interval_seconds=0.001, **kwargs):
            yield chunk
            release.set()

    monkeypatch.setattr(routes, "merge_with_keepalive", keepalive)
    native.client.invoke_model_with_response_stream = AsyncMock(side_effect=delayed_denial)
    response = await request(native, accept)
    assert response.status_code == 200
    if accept == "text/event-stream":
        assert response.content.startswith(b": keep-alive\n\n")
        assert b"event: error\n" in response.content
        assert ACCOUNT.encode() in response.content
    else:
        decoder = EventStreamBuffer()
        decoder.add_data(response.content)
        frames = list(decoder)
        assert frames[0].headers[":event-type"] == "chunk"
        assert frames[-1].headers[":message-type"] == "exception"
        assert_account_error(json.loads(json.loads(frames[-1].payload)["originalMessage"]))
    assert_failed_usage(native)


class BlockingBody(Body):
    def __init__(self, *, first_chunk):
        super().__init__()
        self.first_chunk = first_chunk
        self.reading = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def __iter__(self):
        try:
            if self.first_chunk:
                yield {"chunk": {"bytes": b'{"type":"message_start","message":{"usage":{"input_tokens":10}}}'}}
            self.reading.set()
            if not self.release.wait(timeout=5):
                raise TimeoutError("SDK body was not closed on disconnect")
        finally:
            self.finished.set()

    def close(self):
        super().close()
        self.release.set()


@pytest.mark.parametrize("accept", ["*/*", "text/event-stream"])
@pytest.mark.parametrize("phase", ["before_first", "during_read", "during_send"])
async def test_disconnect_closes_sdk_body_and_does_not_log_success(native, accept, phase):
    body = BlockingBody(first_chunk=phase != "before_first")
    native.client.invoke_model_with_response_stream = AsyncMock(return_value={"body": body})
    disconnected = asyncio.Event()
    sent = []
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b'{"max_tokens":80,"messages":[]}', "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if phase == "during_send" and message["type"] == "http.response.body":
            raise OSError("client disconnected")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4" if phase == "during_send" else "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "query_string": b"",
        "path": f"/model/{FORWARDED}/invoke-with-response-stream",
        "headers": [(b"accept", accept.encode()), (b"content-type", b"application/json")],
    }
    task = asyncio.create_task(native.app(scope, receive, send))
    if phase == "during_send":
        with pytest.raises(ClientDisconnect):
            await asyncio.wait_for(task, timeout=3)
    else:
        assert await asyncio.to_thread(body.reading.wait, 3)
        disconnected.set()
        await asyncio.wait_for(task, timeout=3)
        assert await asyncio.to_thread(body.finished.wait, 3)
    await flush_logs()
    assert body.closed
    if phase == "before_first":
        assert sent == []  # No premature HTTP 200 while upstream is still pending.
    assert not any(b"event: error" in message.get("body", b"") for message in sent)
    native.metering.usage.log_request.assert_awaited_once()
    assert native.metering.usage.log_request.await_args.kwargs["status_code"] == 499
    native.metering.writer.write_log.assert_not_awaited()
