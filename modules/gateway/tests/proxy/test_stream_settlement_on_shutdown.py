"""Gateway shutdown retains completed-stream usage until the receipt is written."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import httpx
import pytest

from src.app import create_app, lifespan
from src.proxy.mantle_service import MantlePassthroughService, _usage_finalizers, drain_usage_finalizers, register_usage_finalizer
from src.proxy.pricing_capture import PricingCapture
from src.proxy.service import ProxyService
from tests.proxy.test_mantle_stream_lifecycle import COMPLETED, ScriptedStream, StubAuth

pytestmark = pytest.mark.usefixtures("unmapped_mantle_routing")


@pytest.fixture
def shutdown_app(monkeypatch):
    settings = MagicMock(db_auto_create=False, mantle_enabled=False)
    monkeypatch.setattr("src.app.get_settings", lambda: settings)
    monkeypatch.setattr("src.budget.pricing_decisions.refresh_pricing_cache", AsyncMock())
    ratelimit = SimpleNamespace(initialize=AsyncMock(), close=AsyncMock())
    return SimpleNamespace(state=SimpleNamespace(ratelimit_service=ratelimit))


@pytest.fixture
def context(token_context):
    return token_context


async def test_lifespan_waits_for_completed_stream_receipt_before_exit(shutdown_app, context):
    started = asyncio.Event()
    release = asyncio.Event()
    recorded = []

    async def persist(*args, **kwargs):
        started.set()
        await release.wait()
        recorded.append("receipt")
        return True

    async def close():
        recorded.append("close")

    shutdown_app.state.ratelimit_service.close.side_effect = close
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=ScriptedStream([COMPLETED]))))
    service = MantlePassthroughService(StubAuth(), "https://upstream.invalid", http_client=client)
    service._log_usage = AsyncMock(side_effect=persist)
    manager = lifespan(shutdown_app)
    async with client:
        await manager.__aenter__()
        stream = await service.create_response(b"{}", context, stream=True, model="openai.gpt-6-astra", request_id="shutdown-receipt")
        completion = asyncio.create_task(anext(stream))
        await asyncio.wait_for(started.wait(), 2)
        exiting = asyncio.create_task(manager.__aexit__(None, None, None))
        try:
            await asyncio.sleep(0)
            assert shutdown_app.state.shutting_down is True
            assert not completion.done()
            assert not exiting.done()
            release.set()
            assert await asyncio.wait_for(completion, 2) == COMPLETED
            await asyncio.wait_for(exiting, 2)
            assert recorded == ["receipt", "close"]
            assert not {task for task in _usage_finalizers if not task.done()}
        finally:
            release.set()
            if not exiting.done():
                await exiting
            await stream.aclose()


async def test_lifespan_drains_claude_finalizer_after_client_cancellation(shutdown_app):
    started = asyncio.Event()
    release = asyncio.Event()
    recorded = []

    async def persist(**kwargs):
        started.set()
        await release.wait()
        recorded.append("receipt")
        return True

    service = ProxyService(pool_service=MagicMock(), model_resolver=MagicMock())
    service._log_usage_impl = AsyncMock(side_effect=persist)
    capture = PricingCapture("shutdown-claude", "claude-model")
    capture.routing = MagicMock()
    manager = lifespan(shutdown_app)
    await manager.__aenter__()
    consumer = asyncio.create_task(service._log_usage(pricing_capture=capture))
    await asyncio.wait_for(started.wait(), 2)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    exiting = asyncio.create_task(manager.__aexit__(None, None, None))
    try:
        await asyncio.sleep(0)
        assert not exiting.done()
        assert not capture.finalization_task.done()
        release.set()
        await asyncio.wait_for(exiting, 2)
        assert recorded == ["receipt"]
        assert capture.finalization_task.done()
        assert shutdown_app.state.ratelimit_service.close.await_count == 1
    finally:
        release.set()
        if not exiting.done():
            await exiting


async def test_expired_drain_does_not_cancel_pending_settlement():
    release = asyncio.Event()
    pending = asyncio.create_task(release.wait(), name="pending_receipt")
    register_usage_finalizer(pending)
    try:
        assert await drain_usage_finalizers(timeout_seconds=0.001) == 1
        assert not pending.cancelled()
        release.set()
        await asyncio.wait_for(pending, 2)
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_new_requests_are_refused_during_shutdown():
    app = create_app()
    app.state.shutting_down = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post("/openai/v1/responses", json={"model": "openai.gpt-6-astra"}, headers={"x-request-id": "caller-correlation"})
    assert result.status_code == 503
    assert result.json()["error"] == "gateway_shutting_down"
    assert UUID(result.headers["x-request-id"])
