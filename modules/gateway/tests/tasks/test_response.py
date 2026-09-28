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


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", ["2.3", "2.4"])
@pytest.mark.parametrize("blocked", ["start", "body", "iterator"])
async def test_lease_loss_cancels_response_and_releases(monkeypatch, spec, blocked):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(transport, "RENEW_SECONDS", 0.01)
    outcome = StreamOutcome(task_id="owned")
    release = AsyncMock()
    lease = Mock()
    lease.remaining.return_value = 45
    lease.renew = AsyncMock(return_value=False)
    started, closed = [], []

    async def frames():
        started.append(True)
        try:
            if blocked == "iterator":
                await asyncio.Event().wait()
            yield b"first"
            outcome.last_sequence = 1
        finally:
            closed.append(True)

    async def send(message):
        if (blocked == "start" and message["type"] == "http.response.start") or (blocked == "body" and message.get("body")):
            await asyncio.Event().wait()

    async def receive():
        await asyncio.Event().wait()

    response = transport.TaskStreamingResponse(frames(), outcome=outcome, release=release, lease=lease)
    await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": spec}}, receive, send), 1)
    assert outcome.reason == "lease_lost"
    assert outcome.last_sequence == 0
    assert bool(started) == bool(closed) == (blocked != "start")
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_lease_sends_nothing():
    from unittest.mock import AsyncMock

    outcome = StreamOutcome(task_id="owned")
    release = AsyncMock()
    lease = Mock()
    lease.remaining.return_value = 0
    send = AsyncMock()

    async def frames():
        yield b"not allowed"

    async def receive():
        await asyncio.Event().wait()

    response = transport.TaskStreamingResponse(frames(), outcome=outcome, release=release, lease=lease)
    await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    send.assert_not_awaited()
    release.assert_awaited_once()
    assert outcome.reason == "lease_lost"


@pytest.mark.asyncio
async def test_outer_cancellation_releases_lease_before_unstarted_iterator():
    from unittest.mock import AsyncMock

    entered = asyncio.Event()
    release = AsyncMock()
    lease = Mock()
    lease.remaining.return_value = 45
    lease.renew = AsyncMock(return_value=True)

    async def frames():
        yield b"never started"

    async def send(message):
        entered.set()
        await asyncio.Event().wait()

    async def receive():
        await asyncio.Event().wait()

    response = transport.TaskStreamingResponse(frames(), outcome=StreamOutcome(task_id="owned"), release=release, lease=lease)
    task = asyncio.create_task(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.assert_awaited_once()
