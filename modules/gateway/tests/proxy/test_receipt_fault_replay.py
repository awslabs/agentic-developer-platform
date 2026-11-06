"""Fault boundaries for completed Responses streams and idempotent SQL receipts."""

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import select

from src.budget import pricing_decisions
from src.proxy import mantle_service
from src.proxy.mantle_service import MantlePassthroughService, _usage_finalizers
from src.shared.models.budget import BudgetSettlementReceipt, BudgetUsage
from src.shared.models.usage import UsageLog
from src.shared.schemas.auth import TokenContext
from src.usage.service import UsageService
from tests.proxy.test_mantle_stream_lifecycle import ScriptedStream, StubAuth

pytestmark = pytest.mark.usefixtures("unmapped_mantle_routing")

MODEL = "openai.gpt-5.6-sol"
REQUEST_ID = "replayed-settlement"
USAGE = {"input_tokens": 10, "output_tokens": 2}
TERMINAL = b'data: {"type":"response.completed","response":{"usage":{"input_tokens":10,"output_tokens":2}}}\n\n'


@pytest.fixture
def accounting(monkeypatch, db_session_factory):
    monkeypatch.setattr(mantle_service, "get_session_factory", lambda: db_session_factory)
    monkeypatch.setattr(pricing_decisions, "get_session_factory", lambda: db_session_factory)
    monkeypatch.setattr(mantle_service, "reconcile_budget_reservation", AsyncMock())
    monkeypatch.setattr(
        mantle_service, "ChatLoggingService", lambda: MagicMock(log_chat_async=MagicMock(), write_usage_recovery=AsyncMock(return_value=True))
    )
    return db_session_factory


@pytest.fixture
def context():
    identity = TokenContext(
        org_id="tenant",
        user_id="owner",
        team_id="team",
        department_id="department",
        account_type="human",
        expires_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    identity._budget_request_timestamp = datetime(2026, 10, 13, tzinfo=UTC)
    return identity


async def persisted(accounting):
    async with accounting() as session:
        receipts = (await session.execute(select(BudgetSettlementReceipt))).scalars().all()
        usages = (await session.execute(select(BudgetUsage))).scalars().all()
        logs = (await session.execute(select(UsageLog))).scalars().all()
    return receipts, usages, logs


@pytest.mark.parametrize("lost_acks", [1, 4])
async def test_lost_commit_ack_retries_before_success_and_replay_debits_once(accounting, context, monkeypatch, lost_acks):
    original = UsageService.log_request
    provider_calls = []
    acknowledgements = 0

    async def lost_ack(self, **kwargs):
        nonlocal acknowledgements
        await original(self, **kwargs)
        acknowledgements += 1
        if acknowledgements <= lost_acks:
            raise OSError("database acknowledgement lost")

    monkeypatch.setattr(UsageService, "log_request", lost_ack)

    def upstream(request):
        provider_calls.append(request)
        return httpx.Response(200, stream=ScriptedStream([TERMINAL]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        service = MantlePassthroughService(StubAuth(), "https://upstream.invalid", http_client=client)
        request = json.dumps({"model": MODEL, "input": "hello"}).encode()
        stream = await service.create_response(request, context, stream=True, model=MODEL, request_id=REQUEST_ID)
        content = b"".join([chunk async for chunk in stream])
        if lost_acks == 1:
            assert content == TERMINAL
            assert acknowledgements == 2
        else:
            assert b"response.completed" not in content
            assert b"usage_settlement_unconfirmed" in content
            assert acknowledgements == 4
        assert len(provider_calls) == 1

        receipts, usages, logs = await persisted(accounting)
        assert len(receipts) == 1 and len(logs) == 1 and len(usages) == 12
        assert all(row.request_count == 1 for row in usages)
        assert receipts[0].org_id == "tenant" and receipts[0].request_id == REQUEST_ID

        capture = service._capture_usage(USAGE, request, MODEL, {}, base_url="https://upstream.invalid")
        assert await service._log_usage(context, MODEL, capture, 1, 200, REQUEST_ID, None)

    receipts, usages, logs = await persisted(accounting)
    assert len(provider_calls) == 1
    assert len(receipts) == 1 and len(logs) == 1 and len(usages) == 12
    assert all(row.request_count == 1 for row in usages)
    assert receipts[0].cost_usd > 0
    assert acknowledgements == (3 if lost_acks == 1 else 5)


async def test_interrupted_finalization_does_not_create_zero_usage_receipt(accounting, context, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    original = UsageService.log_request

    async def interrupted(self, **kwargs):
        started.set()
        await release.wait()
        await original(self, **kwargs)

    monkeypatch.setattr(UsageService, "log_request", interrupted)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=ScriptedStream([TERMINAL])))) as client:
        service = MantlePassthroughService(StubAuth(), "https://upstream.invalid", http_client=client)
        stream = await service.create_response(b'{"model":"openai.gpt-5.6-sol"}', context, stream=True, model=MODEL, request_id=REQUEST_ID)
        completion = asyncio.create_task(anext(stream))
        await asyncio.wait_for(started.wait(), 2)
        assert not completion.done()
        pending = next(task for task in _usage_finalizers if task.get_name() == f"mantle_usage_finalize_{REQUEST_ID}")
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await completion
        receipts, usages, logs = await persisted(accounting)
        assert receipts == usages == logs == []
        release.set()
        await stream.aclose()
        capture = service._capture_usage(USAGE, b'{"model":"openai.gpt-5.6-sol"}', MODEL, {}, base_url="https://upstream.invalid")
        assert await service._log_usage(context, MODEL, capture, 1, 200, REQUEST_ID, None)
    receipts, usages, logs = await persisted(accounting)
    assert len(receipts) == 1 and len(logs) == 1 and len(usages) == 12
    assert all(row.request_count == 1 for row in usages)
