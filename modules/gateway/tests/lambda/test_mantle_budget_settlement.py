"""Responses usage must reach the settled ledger read by Budget & Spend."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.chat_logging.config import ScrubLevel
from src.chat_logging.service import ChatLoggingService
from src.proxy import mantle_service
from src.shared.schemas.auth import TokenContext

from ._handler_loader import load_handler


@pytest.fixture
def metering(monkeypatch):
    monkeypatch.setattr("boto3.client", MagicMock())
    monkeypatch.setenv("BG_CHAT_LOGGING_BUCKET", "test-chat-logs")
    writer = MagicMock()
    writer.write_log = AsyncMock(return_value=True)
    chat_logger = ChatLoggingService(s3_writer=writer, enabled=True, scrub_level=ScrubLevel.BASIC)
    monkeypatch.setattr(mantle_service, "ChatLoggingService", lambda: chat_logger, raising=False)
    usage_service = MagicMock()
    usage_service.log_request = AsyncMock()
    session = AsyncMock()
    monkeypatch.setattr(mantle_service, "get_session_factory", lambda: lambda: session)
    monkeypatch.setattr(mantle_service, "UsageService", lambda db: usage_service)
    monkeypatch.setattr(mantle_service, "reconcile_budget_reservation", AsyncMock())
    monkeypatch.setattr(mantle_service, "resolve_shadow_target", AsyncMock(return_value=None))
    return writer, usage_service, chat_logger


def context(hosted=False):
    return TokenContext(
        user_id="worker" if hosted else "cognito-sub",
        org_id="__platform__" if hosted else "tenant",
        attributed_org_id="tenant",
        attributed_user_id="canonical-human" if hosted else "cognito-sub",
        team_id="team",
        department_id="",
        account_type="service" if hosted else "human",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


async def flush_chat_logs():
    tasks = [task for task in asyncio.all_tasks() if task.get_name().startswith("chat_log_")]
    if tasks:
        await asyncio.gather(*tasks)


async def invoke(stream, *, usage=True):
    response = {"output": [{"text": "private response"}]}
    if usage:
        response["usage"] = {"input_tokens": 1000, "output_tokens": 1000}
    payload = json.dumps(response).encode()
    wire = b'data: {"type":"response.completed","response":' + payload + b"}\n\n" if stream else payload
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=wire)))
    auth = MagicMock()
    auth.sign.return_value = {"Authorization": "private-upstream-credential"}
    return mantle_service.MantlePassthroughService(auth, "https://upstream.test", http_client=client), wire, client


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("hosted", [False, True])
async def test_responses_settle_person_and_tenant_budgets(metering, monkeypatch, stream, hosted):
    writer, usage_service, _ = metering
    svc, wire, client = await invoke(stream)
    async with client:
        result = await svc.create_response(
            b'{"input":"private prompt"}',
            context(hosted),
            stream=stream,
            model="openai.gpt-5.5",
            request_id="req-budget",
            agent_run_id="run-1" if hosted else None,
        )
        actual = b"".join([chunk async for chunk in result]) if stream else result.content
    assert actual == wire
    await flush_chat_logs()

    # Exercise the actual S3 payload builder and tracker, not a fabricated event.
    writer.write_log.assert_awaited_once()
    event = writer.write_log.await_args.kwargs["log_data"]
    assert "private prompt" not in json.dumps(event)
    assert "private response" not in json.dumps(event)
    assert "private-upstream-credential" not in json.dumps(event)
    assert event["request_id"] == "req-budget"
    assert event["org_id"] == "tenant"
    assert event["account_type"] == ("service" if hosted else "human")

    with monkeypatch.context() as patcher:
        patcher.setattr("boto3.client", MagicMock())
        tracker = load_handler("budget-usage-tracker")
    settle = MagicMock()
    bridge = MagicMock()
    monkeypatch.setattr(tracker, "upsert_budget_usage", settle)
    monkeypatch.setattr(tracker, "bridge_cost_to_usage_logs", bridge)
    tracker.process_chat_log(MagicMock(), event, tracker.MODEL_PRICING, chat_log_s3_key="metered.json")

    expected_entities = {("org", "tenant"), ("team", "team"), ("user", "worker" if hosted else "cognito-sub")}
    if hosted:
        expected_entities.add(("root_user", "canonical-human"))
    assert len(settle.call_args_list) == len(expected_entities) * 3
    for period in ("daily", "weekly", "monthly"):
        calls = [call.args for call in settle.call_args_list if call.args[5] == period]
        assert {(args[2], args[3]) for args in calls} == expected_entities
        assert all(args[1] == "tenant" and args[6] == Decimal("0.0385") and args[7] == 2000 for args in calls)
    usage_service.log_request.assert_awaited_once()
    assert usage_service.log_request.await_args.kwargs["cost_usd"] == 0.0385
    bridge.assert_called_once()


@pytest.mark.parametrize("stream", [False, True])
async def test_absent_usage_does_not_fabricate_settled_zero(metering, stream):
    writer, usage_service, _ = metering
    svc, _, client = await invoke(stream, usage=False)
    async with client:
        result = await svc.create_response(b"{}", context(), stream=stream, model="openai.gpt-5.5", request_id="no-usage")
        if stream:
            _ = [chunk async for chunk in result]
    await flush_chat_logs()
    writer.write_log.assert_not_awaited()
    usage_service.log_request.assert_awaited_once()


async def test_usage_db_failure_still_emits_settlement_event(metering):
    writer, usage_service, _ = metering
    usage_service.log_request.side_effect = RuntimeError("database temporarily unavailable")
    svc, wire, client = await invoke(False)
    async with client:
        result = await svc.create_response(b"{}", context(), stream=False, model="openai.gpt-5.5", request_id="db-down")
    assert result.content == wire
    await flush_chat_logs()
    writer.write_log.assert_awaited_once()


@pytest.mark.parametrize("failure", ["disabled", "excluded", "scheduling", "s3"])
async def test_logging_failure_or_configuration_preserves_response_and_usage_log(metering, failure):
    writer, usage_service, chat_logger = metering
    if failure == "disabled":
        chat_logger._enabled = False
    elif failure == "excluded":
        chat_logger._exclude_models = ["openai.gpt-5.5"]
    elif failure == "scheduling":
        chat_logger.log_chat_async = MagicMock(side_effect=RuntimeError("cannot schedule"))
    else:
        writer.write_log.side_effect = RuntimeError("S3 unavailable")
    svc, wire, client = await invoke(False)
    async with client:
        result = await svc.create_response(b"{}", context(), stream=False, model="openai.gpt-5.5", request_id="logging-failure")
    await flush_chat_logs()
    assert result.content == wire
    usage_service.log_request.assert_awaited_once()
    if failure in ("disabled", "excluded", "scheduling"):
        writer.write_log.assert_not_awaited()
