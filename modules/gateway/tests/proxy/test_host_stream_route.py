"""The Bedrock stream route admits a verified Dev Box host run under the buffered route's gate.

#8223 Dev Box latency: the host adapter moves from ``/model/{id}/invoke`` (buffered, 4–9 s per
call before a byte reached the box) to ``/model/{id}/invoke-with-response-stream``. That route
had no host gate and was not registered for host runs, so a host was refused. Proven here at
the handler level: a host run streams SSE through the same ``CrewModelUsage`` receipt
(settled at stream end), its prompt and headers are redacted from the chat log exactly as the
buffered route does, and a non-host caller is untouched.
"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from src.proxy import routes
from src.shared.schemas.auth import TokenContext

INSTANCE = "i-0123456789abcdef0"
RUN_ID = "run-1"
MODEL = "global.anthropic.claude-sonnet-5"
EVENTS = [
    b'data: {"type": "message_start", "message": {"usage": {"input_tokens": 115000, "output_tokens": 1}}}\n\n',
    b'data: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "pong"}}\n\n',
    b'data: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}}\n\n',
    b'data: {"type": "message_stop"}\n\n',
]


def _context(*, host: bool) -> TokenContext:
    context = TokenContext(
        user_id="dev-box-guest-adp-dev-dev-box" if host else "alice",
        org_id="__platform__" if host else "one",
        team_id="__agents__" if host else "team",
        department_id="",
        account_type="service" if host else "human",
        auth_source="iam" if host else "cognito",
        scope="internal",
        requires_host_identity=host,
        attributed_org_id="__platform__" if host else "one",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    if host:
        context._dev_box_instance_id = INSTANCE
    return context


def _request(body: dict, headers: dict[str, str]) -> Request:
    payload = json.dumps(body).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": f"/model/{MODEL}/invoke-with-response-stream",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "state": {},
    }
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, receive)


class Provider:
    def __init__(self):
        self.calls = []

    async def invoke_model(self, model_id, body, context, *, stream, request_id, agent_run_id, pricing_capture):
        self.calls.append({"model_id": model_id, "body": body, "stream": stream, "agent_run_id": agent_run_id})

        async def chunks():
            for event in EVENTS:
                yield event

        return chunks()


@pytest.fixture
def captured(monkeypatch):
    """Record what reaches the chat log wrapper and the host receipt; pass the stream through."""
    seen = {"wrapper": None, "receipts": []}

    def wrapper(stream, **kwargs):
        seen["wrapper"] = kwargs
        return stream

    async def invoke_stream(self, token, target, model_id, open_stream, *, now):
        seen["receipts"].append({"token": token, "target": target, "model_id": model_id})
        stream = await open_stream()

        async def accounted():
            async for chunk in stream:
                yield chunk
            seen["receipts"][-1]["settled"] = True

        return accounted()

    async def authorized(context, agent_run_id):
        seen["gate"] = agent_run_id
        return SimpleNamespace(name="run-store", table=SimpleNamespace(name="runs", meta=None)), SimpleNamespace(
            host_run_id=agent_run_id, host_instance_ref=INSTANCE
        )

    monkeypatch.setattr(routes, "create_streaming_logging_wrapper", wrapper)
    monkeypatch.setattr(routes, "get_chat_logging_service", lambda: SimpleNamespace())
    monkeypatch.setattr(routes, "_authorized_host_target", authorized)
    monkeypatch.setattr("src.crew.model_usage.CrewModelUsage.invoke_stream", invoke_stream)
    return seen


async def _collect(response):
    body = b""
    async for chunk in response.body_iterator:
        body += chunk if isinstance(chunk, bytes) else chunk.encode()
    return body


async def test_host_run_streams_sse_under_the_receipt_with_its_prompt_redacted(captured):
    provider = Provider()
    body = {
        "messages": [{"role": "user", "content": "Reply with the single word pong."}],
        "max_tokens": 64,
        "system": [{"type": "text", "text": "owner secret", "cache_control": {"type": "ephemeral"}}],
    }
    request = _request(
        body, {"accept": "text/event-stream", "x-agent-runid": RUN_ID, "x-adp-crew-model-token": "tok.sig", "authorization": "AWS4-HMAC-SHA256 ..."}
    )
    response = await routes.invoke_model_stream_by_path(MODEL, request, _context(host=True), provider, RUN_ID, None)
    assert response.media_type == "text/event-stream"
    streamed = await _collect(response)
    for event in EVENTS:
        assert event in streamed
    # The provider was opened exactly once, streaming, with the body untouched (cache markers included).
    assert provider.calls == [{"model_id": MODEL, "body": body, "stream": True, "agent_run_id": RUN_ID}]
    # Gate and receipt: same token, model and target identity as the buffered route; settled at stream end.
    assert captured["gate"] == RUN_ID
    assert captured["receipts"] == [{"token": "tok.sig", "target": captured["receipts"][0]["target"], "model_id": MODEL, "settled": True}]
    assert captured["receipts"][0]["target"].host_instance_ref == INSTANCE
    # The owner's prompt and the host's headers never reach the chat log.
    assert captured["wrapper"]["request_body"] == {} and captured["wrapper"]["headers"] is None
    assert captured["wrapper"]["model"] == MODEL and captured["wrapper"]["api_format"] == "bedrock"


async def test_a_refused_host_gate_opens_no_stream(captured, monkeypatch):
    from fastapi import HTTPException

    async def refused(context, agent_run_id):
        raise HTTPException(403, "Host run is not authorized")

    monkeypatch.setattr(routes, "_authorized_host_target", refused)
    provider = Provider()
    request = _request({"messages": [], "max_tokens": 1}, {"accept": "text/event-stream", "x-agent-runid": RUN_ID})
    with pytest.raises(HTTPException) as denied:
        await routes.invoke_model_stream_by_path(MODEL, request, _context(host=True), provider, RUN_ID, None)
    assert denied.value.status_code == 403 and provider.calls == [] and captured["receipts"] == []


async def test_a_non_host_caller_streams_exactly_as_before(captured):
    provider = Provider()
    body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}
    request = _request(body, {"accept": "text/event-stream", "authorization": "Bearer human-token"})
    response = await routes.invoke_model_stream_by_path(MODEL, request, _context(host=False), provider, None, None)
    streamed = await _collect(response)
    assert EVENTS[1] in streamed
    assert provider.calls[0]["stream"] is True and provider.calls[0]["agent_run_id"] is None
    assert captured["receipts"] == [] and "gate" not in captured
    assert captured["wrapper"]["request_body"] == body
    assert captured["wrapper"]["headers"]["authorization"] == "Bearer human-token"


def test_the_stream_route_is_registered_for_host_runs():
    from src.crew.host_routes import HOST_RUN_ROUTE_PATHS

    assert "/model/{model_id}/invoke-with-response-stream" in HOST_RUN_ROUTE_PATHS
    assert "/model/{model_id}/invoke" in HOST_RUN_ROUTE_PATHS
