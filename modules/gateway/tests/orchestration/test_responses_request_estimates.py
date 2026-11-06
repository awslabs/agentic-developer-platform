"""Request-sized reservations, retained ceilings and actual-spend enforcement."""

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from src.orchestration.provider_quotes import quote_request
from src.orchestration.responses_quotes import RESPONSES_PATH
from tests.budget.test_policy_reservation_backpressure import ledger, request  # noqa: F401


@pytest.fixture(autouse=True)
def estimates(monkeypatch):
    monkeypatch.setenv("BG_RESPONSES_REQUEST_ESTIMATES_ENABLED", "true")


async def quote(**overrides):
    body = {"model": "openai.gpt-6-astra", "input": "hello", "max_output_tokens": 8192, **overrides}
    return await quote_request(json.dumps(body).encode(), RESPONSES_PATH)


async def test_small_requests_no_longer_reserve_the_whole_context():
    result = await quote()
    assert result.input_count_method == "request_bytes_with_margin"
    assert 4096 < result.max_input_tokens < 5000
    assert result.total_usd < result.maximum_cost_usd / 10
    assert result.max_output_tokens == 8192


async def test_estimate_grows_with_instructions_tools_results_and_unicode():
    base = await quote()
    for extra in [
        {"instructions": "instruction " * 10000},
        {"tools": [{"type": "function", "name": "search", "parameters": {"description": "schema " * 10000}}]},
        {"input": [{"type": "function_call_output", "call_id": "call-1", "output": "result " * 10000}]},
        {"input": "日本語🙂" * 10000},
    ]:
        bigger = await quote(**extra)
        assert bigger.total_usd > base.total_usd
        assert bigger.maximum_cost_usd == base.maximum_cost_usd


async def test_json_escaping_and_spacing_do_not_change_estimate():
    body = {"model": "openai.gpt-6-astra", "input": "日本語🙂", "max_output_tokens": 8192}
    escaped = await quote_request(json.dumps(body, ensure_ascii=True, indent=4).encode(), RESPONSES_PATH)
    utf8 = await quote_request(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(), RESPONSES_PATH)
    assert escaped.total_usd == utf8.total_usd
    assert escaped.request_sha256 != utf8.request_sha256


async def test_large_and_media_inputs_keep_full_context_reservation():
    large = await quote(input="x" * 1_100_000)
    media = await quote(input=[{"role": "user", "content": [{"type": "input_image", "image_url": "data:image/png;base64,aGk="}]}])
    for result in (large, media):
        assert result.total_usd == result.maximum_cost_usd
        assert result.input_count_method == "full_context"


async def test_full_context_mode_remains_available(monkeypatch):
    monkeypatch.setenv("BG_RESPONSES_REQUEST_ESTIMATES_ENABLED", "false")
    result = await quote()
    assert result.max_input_tokens == 1_050_000
    assert result.total_usd == result.maximum_cost_usd


async def test_recorded_spend_near_70_can_continue_and_actual_usage_still_enforces_cap(ledger):  # noqa: F811
    ledger.run = replace(ledger.run, headroom_usd=Decimal(100))
    ledger.chain = replace(ledger.chain, headroom_usd=Decimal(2000))
    ledger.targets = [ledger.flow, ledger.run, ledger.chain]
    assert (await ledger.store.reserve("recorded", Decimal("69.846011"), [ledger.run])).admitted
    next_quote = await quote()
    assert (await request(ledger, quote=next_quote.total_usd, request_id="next")).status == 200
    # Provider usage above the estimate must be counted in full, not discarded.
    await ledger.store.reconcile("next", Decimal(31), ledger.targets)
    denied = await request(ledger, quote=next_quote.total_usd, request_id="after")
    assert denied.status == 402
    assert denied.body["details"]["scope"] == "run"
    assert denied.body["details"]["current_total_usd"] == 100.846011
    assert denied.body["details"]["request_estimate_usd"] == float(next_quote.total_usd)
