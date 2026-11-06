"""Idle Crew streams keep bytes moving so no edge read/idle timeout cuts them (#7902)."""

import asyncio
import importlib
import inspect
import json
import time
from pathlib import Path
from threading import Event

import httpx
import pytest
import uvicorn
from starlette.websockets import WebSocketDisconnect

from src.crew import dashboard as dashboard_module
from src.crew import keepalive
from src.crew import websocket as websocket_module
from src.crew.dashboard import crew_identity
from src.crew.keepalive import DEFAULT_KEEPALIVE_SECONDS, SSE_KEEPALIVE, is_event_stream, keepalive_seconds, keepalive_stream
from tests.crew.test_dashboard import FakeCrewLease, FakeCrewWebSocket, identity, signed_cookie

GATEWAY = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 25.0), ("", 25.0), ("  ", 25.0), ("7", 7.0), ("12.5", 12.5), ("0", 0.0), ("-3", 0.0), ("abc", 25.0), ("inf", 25.0), ("nan", 25.0)],
)
def test_keepalive_seconds_defaults_to_25_and_zero_disables(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(keepalive.KEEPALIVE_ENV, raising=False)
    else:
        monkeypatch.setenv(keepalive.KEEPALIVE_ENV, raw)
    assert keepalive_seconds() == expected
    assert DEFAULT_KEEPALIVE_SECONDS == 25.0


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [("text/event-stream", True), ("Text/Event-Stream; charset=utf-8", True), ("application/json", False), ("", False), (None, False)],
)
def test_only_event_streams_are_eligible(content_type, expected):
    assert is_event_stream(content_type) is expected


async def _collect(stream, *, until=None):
    out = []
    async for chunk in stream:
        out.append((chunk, time.monotonic()))
        if until is not None and until(out):
            break
    return out


async def test_keepalive_comment_fills_only_idle_gaps_and_keeps_order():
    release = asyncio.Event()

    async def upstream():
        yield b"data: first\n\n"
        await release.wait()
        yield b"data: second\n\n"
        yield b"data: third\n\n"

    stream = keepalive_stream(upstream(), 0.05)
    results = []

    async def consume():
        async for chunk in stream:
            results.append(chunk)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.18)
    assert results[0] == b"data: first\n\n"
    assert 2 <= len(results) - 1 <= 4 and set(results[1:]) == {SSE_KEEPALIVE}
    release.set()
    await asyncio.wait_for(task, 1)
    assert results[-2:] == [b"data: second\n\n", b"data: third\n\n"]
    assert SSE_KEEPALIVE not in results[-2:]
    assert [chunk for chunk in results if chunk != SSE_KEEPALIVE] == [b"data: first\n\n", b"data: second\n\n", b"data: third\n\n"]


async def test_zero_interval_passes_bytes_through_untouched():
    async def upstream():
        yield b"a"
        await asyncio.sleep(0.05)
        yield b"b"

    assert [chunk for chunk, _ in await _collect(keepalive_stream(upstream(), 0))] == [b"a", b"b"]


async def test_upstream_failure_still_surfaces_after_keepalives():
    async def upstream():
        yield b"a"
        await asyncio.sleep(0.12)
        raise httpx.ReadError("pod went away")

    with pytest.raises(httpx.ReadError):
        await _collect(keepalive_stream(upstream(), 0.05))


async def test_closing_the_stream_cancels_the_held_upstream_read():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def upstream():
        yield b"a"
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    stream = keepalive_stream(upstream(), 0.05)
    assert await anext(stream) == b"a"
    assert await anext(stream) == SSE_KEEPALIVE
    assert started.is_set()
    await stream.aclose()
    await asyncio.wait_for(cancelled.wait(), 1)


async def _relay(app, pod_response, *, path="/api/stream", stop_after=None):
    """Drive one proxied GET through the Crew host app and return the forwarded body chunks."""
    received = []
    done = asyncio.Event()
    disconnected = asyncio.Event()
    request_started = False

    async def receive():
        nonlocal request_started
        if not request_started:
            request_started = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            received.append(message["body"])
            if stop_after is not None and stop_after(received):
                done.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "root_path": "",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"crew.example.test"), (b"cookie", signed_cookie().encode())],
        "client": ("127.0.0.1", 45000),
        "server": ("crew.example.test", 443),
    }
    task = asyncio.create_task(app(scope, receive, send))
    try:
        if stop_after is not None:
            await asyncio.wait_for(done.wait(), 2)
            disconnected.set()
        await asyncio.wait_for(task, 2)
    finally:
        disconnected.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    return received


def _install_pod(monkeypatch, lease, content_type, stream):
    class Registry:
        async def acquire(self, tenant_id, user_id):
            return lease

    monkeypatch.setattr(dashboard_module, "crew_connection_registry", Registry)
    monkeypatch.setattr(dashboard_module, "RENEW_SECONDS", 0.01)
    monkeypatch.setattr(
        dashboard_module,
        "crew_http_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, headers={"content-type": content_type}, stream=stream)),
            trust_env=False,
        ),
    )


class PausingStream(httpx.AsyncByteStream):
    def __init__(self, pause):
        self.pause = pause
        self.closed = False

    async def __aiter__(self):
        yield b"data: first\n\n"
        await asyncio.sleep(self.pause)
        yield b"data: second\n\n"

    async def aclose(self):
        self.closed = True


@pytest.mark.timeout(5)
async def test_sse_relay_sends_keepalive_comments_while_the_pod_is_silent(dashboard, monkeypatch):
    app, host_app, _, _ = dashboard
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    monkeypatch.setenv(keepalive.KEEPALIVE_ENV, "0.05")
    lease = FakeCrewLease()
    upstream = PausingStream(0.18)
    _install_pod(monkeypatch, lease, "text/event-stream", upstream)

    received = await _relay(app, upstream)

    assert received[0] == b"data: first\n\n" and received[-1] == b"data: second\n\n"
    comments = received[1:-1]
    assert 2 <= len(comments) <= 4 and set(comments) == {SSE_KEEPALIVE}
    assert upstream.closed and lease.released


@pytest.mark.timeout(5)
async def test_non_sse_relay_never_receives_keepalive_bytes(dashboard, monkeypatch):
    app, host_app, _, _ = dashboard
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    monkeypatch.setenv(keepalive.KEEPALIVE_ENV, "0.05")
    upstream = PausingStream(0.15)
    _install_pod(monkeypatch, FakeCrewLease(), "application/octet-stream", upstream)

    assert await _relay(app, upstream) == [b"data: first\n\n", b"data: second\n\n"]


@pytest.mark.timeout(5)
async def test_sse_relay_with_keepalive_disabled_forwards_only_pod_bytes(dashboard, monkeypatch):
    app, host_app, _, _ = dashboard
    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    monkeypatch.setenv(keepalive.KEEPALIVE_ENV, "0")
    upstream = PausingStream(0.15)
    _install_pod(monkeypatch, FakeCrewLease(), "text/event-stream", upstream)

    assert await _relay(app, upstream) == [b"data: first\n\n", b"data: second\n\n"]


@pytest.mark.parametrize(("raw", "expected"), [(None, 25.0), ("0", None), ("40", 40.0)])
def test_websocket_pod_hop_ping_interval_follows_the_keepalive_setting(dashboard, monkeypatch, raw, expected):
    from fastapi.testclient import TestClient

    app, host_app, _, _ = dashboard
    websocket_finished = Event()

    async def tracked_app(scope, receive, send):
        await app(scope, receive, send)
        if scope["type"] == "websocket":
            websocket_finished.set()

    host_app.dependency_overrides[crew_identity] = lambda: identity("user-a")
    if raw is None:
        monkeypatch.delenv(keepalive.KEEPALIVE_ENV, raising=False)
    else:
        monkeypatch.setenv(keepalive.KEEPALIVE_ENV, raw)
    connections = []

    class Registry:
        async def acquire(self, tenant_id, user_id):
            return FakeCrewLease()

    def fake_connect(uri, **kwargs):
        connections.append(kwargs)
        return FakeCrewWebSocket()

    monkeypatch.setattr(websocket_module, "crew_connection_registry", Registry)
    monkeypatch.setattr(websocket_module, "crew_websocket_connect", fake_connect)
    with TestClient(tracked_app, base_url="https://crew.example.test") as client:
        with client.websocket_connect("ws://crew.example.test/api/live", headers={"Cookie": signed_cookie()}) as browser:
            browser.send_text("hello")
            assert browser.receive_text() == "hello"
            browser.send_text("close")
            with pytest.raises(WebSocketDisconnect) as ended:
                browser.receive_text()
            assert ended.value.code == 1000
            assert websocket_finished.wait(timeout=5), "WebSocket application did not finish before client teardown"
    assert len(connections) == 1 and connections[0]["ping_interval"] == expected
    assert inspect.signature(uvicorn.Config.__init__).parameters["ws_ping_interval"].default is not None


def test_browser_hop_ping_is_owned_by_uvicorn_and_stays_inside_the_edge_timeout():
    """ASGI has no ping event, so the server pings the browser; the image must leave that on."""
    dockerfile = (GATEWAY / "Dockerfile").read_text()
    cmd_line = next(line for line in dockerfile.splitlines() if line.startswith("CMD "))
    cmd = json.loads(cmd_line[len("CMD ") :])
    assert cmd[0] == "uvicorn"
    assert not [arg for arg in cmd if arg.startswith("--ws")], "a --ws* flag in CMD would override UVICORN_WS_PING_INTERVAL"
    default_ping = inspect.signature(uvicorn.Config.__init__).parameters["ws_ping_interval"].default
    assert default_ping is not None and 0 < default_ping <= DEFAULT_KEEPALIVE_SECONDS < 60
    # `uvicorn.main` the attribute is the click command (the package re-exports it).
    cli = importlib.import_module("uvicorn.main").main
    assert cli.context_settings.get("auto_envvar_prefix") == "UVICORN"
