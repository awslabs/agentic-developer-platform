"""Production response wiring: blocked start/body/end and disconnected sockets."""

import asyncio
from unittest.mock import Mock

import pytest

from src.tasks import response as transport
from src.tasks.streaming import StreamOutcome


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", ["2.3", "2.4"])
@pytest.mark.parametrize("blocked", ["start", "body", "end"])
async def test_blocked_send_releases_even_unstarted_iterator(monkeypatch, spec, blocked):
    monkeypatch.setattr(transport, "SSE_BLOCKED_WRITE_DISCONNECT_SECONDS", 0.01)
    outcome = StreamOutcome(task_id="owned")
    started, closed = [], []
    release = Mock()

    async def frames():
        started.append(True)
        try:
            yield b"event: event\n\n"
            outcome.last_sequence = 1
        finally:
            closed.append(True)

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        phase = "start" if message["type"] == "http.response.start" else ("body" if message.get("more_body") else "end")
        if phase == blocked:
            await asyncio.Event().wait()

    response = transport.TaskStreamingResponse(frames(), outcome=outcome, release=release)
    await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": spec}}, receive, send), 1)
    assert outcome.reason == "slow_consumer"
    assert outcome.last_sequence == (1 if blocked == "end" else 0)
    assert bool(started) == bool(closed) == (blocked != "start")
    release.assert_called_once_with()


@pytest.mark.asyncio
async def test_client_disconnect_closes_iterator_and_releases():
    first = asyncio.Event()
    closed = asyncio.Event()
    release = Mock()
    outcome = StreamOutcome(task_id="owned")

    async def frames():
        try:
            yield b"first"
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def receive():
        await first.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message.get("body"):
            first.set()

    response = transport.TaskStreamingResponse(frames(), outcome=outcome, release=release)
    await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send), 1)
    assert closed.is_set()
    assert outcome.reason == "client_disconnected"
    release.assert_called_once_with()


@pytest.mark.asyncio
async def test_transport_failure_releases_without_advancing_cursor():
    release = Mock()
    outcome = StreamOutcome(task_id="owned")
    closed = []

    async def frames():
        try:
            yield b"first"
            outcome.last_sequence = 1
        finally:
            closed.append(True)

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        if message.get("body"):
            raise OSError("controlled disconnected transport")

    response = transport.TaskStreamingResponse(frames(), outcome=outcome, release=release)
    with pytest.raises(Exception):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert closed and outcome.last_sequence == 0
    release.assert_called_once_with()
