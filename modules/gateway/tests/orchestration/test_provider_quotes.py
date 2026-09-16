"""Typed quote contracts: fixed pricing oracles, request binding, truthful outcomes.

Rates come from hand-built ``RateRow`` fixtures with exact ``Decimal`` values, not
from the live snapshot, so an oracle asserts arithmetic rather than restating
today's published prices. The equivalence tests are the exception: they compare
the ported adapter against the previous implementation on real snapshot rates,
which is the property that matters there.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from pricing_policy.policy import ContextTier, Geography, RateRow, ServiceTier
from src.orchestration import flow_meter
from src.orchestration.provider_quotes import (
    QUOTE_CONFIRM_TIMEOUT_SECONDS,
    QUOTE_TTL_SECONDS,
    AnthropicTextQuoteAdapter,
    Capability,
    ProviderQuote,
    ProviderQuoteAdapter,
    QuoteReason,
    QuoteRefusal,
    QuoteRefusedError,
    TrustedUsage,
    adapter_for,
    confirm_quote_spendable,
    quote_request,
    request_digest,
    revalidate_quote,
)

MODEL = "anthropic.claude-sonnet-4-6"
CONTEXT_LIMIT = 200_000
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def _row(**overrides) -> RateRow:
    """One published rate. Prices are per 1k tokens, as ``RateRow`` defines them."""
    fields = {
        "model_id": MODEL,
        "geography": Geography.IN_REGION,
        "service_tier": ServiceTier.STANDARD,
        "context_tier": ContextTier.FLAT,
        "region": "us-east-1",
        "input_price_per_1k_tokens": Decimal("0.003"),
        "output_price_per_1k_tokens": Decimal("0.015"),
        "cache_read_price_per_1k_tokens": Decimal("0.0003"),
        "cache_write_price_per_1k_tokens": Decimal("0.00375"),
        "cache_write_policy": "full_rate",
        "source": "pricing_page",
        "source_url": "https://example.invalid/pricing",
        "source_content_sha256": "0" * 64,
        "verified_at": "2026-09-12T00:00:00+00:00",
    }
    fields.update(overrides)
    return RateRow(**fields)


@pytest.fixture
def oracle(monkeypatch):
    """Pin the snapshot and rate state to fixed rates so totals are arithmetic."""
    from types import SimpleNamespace

    import src.budget.pricing_v2_reader as reader
    from src.orchestration import provider_quotes

    state = SimpleNamespace(rows=(), source="test_generation:7", generation_id=7, pointer_revision=3)
    snapshot = SimpleNamespace(
        snapshot_version="test-snapshot",
        models={MODEL: {"context_max_input_tokens": CONTEXT_LIMIT}},
        rates=(_row(),),
    )

    def configure(*, rates=None, models=None, rate_state=None):
        if rates is not None:
            snapshot.rates = tuple(rates)
        if models is not None:
            snapshot.models = models
        if rate_state is not None:
            state.rows = tuple(rate_state)
        return SimpleNamespace(snapshot=snapshot, state=state)

    monkeypatch.setattr("pricing_policy.load_snapshot", lambda *a, **k: snapshot)
    monkeypatch.setattr(reader, "cached_rate_state", lambda: state)
    assert provider_quotes.AnthropicTextQuoteAdapter  # module imported under test
    return configure


def body(**overrides) -> bytes:
    document = {"model": MODEL, "messages": [{"role": "user", "content": "hello"}], "max_tokens": 1000}
    document.update(overrides)
    return json.dumps(document).encode()


async def quote(raw: bytes = None, path: str = "/v1/messages", now: datetime = NOW) -> ProviderQuote:
    return await quote_request(body() if raw is None else raw, path, now=now)


# ---------------------------------------------------------------------------
# Fixed pricing oracles
# ---------------------------------------------------------------------------


async def test_total_prices_full_context_at_worst_input_rate_plus_requested_output(oracle):
    oracle()
    result = await quote(body(max_tokens=1000))
    # Cache write (0.00375) beats input (0.003), so it sets the input rate:
    #   200000/1000 * 0.00375 + 1000/1000 * 0.015 = 0.75 + 0.015
    assert result.total_usd == Decimal("0.765000")
    assert result.max_input_tokens == CONTEXT_LIMIT and result.max_output_tokens == 1000
    assert result.max_cache_write_tokens == CONTEXT_LIMIT and result.max_tool_tokens == 0


async def test_worst_rate_is_taken_across_cache_tiers(oracle):
    # A 1h cache-write tier above both input and the ordinary write rate must win.
    oracle(rates=[_row(cache_write_1h_price_per_1k_tokens=Decimal("0.006"))])
    result = await quote(body(max_tokens=1000))
    assert result.total_usd == Decimal("1.215000")  # 200*0.006 + 0.015


async def test_unpublished_cache_write_never_prices_as_zero(oracle):
    # cache_write None must not drag the max down; input rate still applies.
    oracle(rates=[_row(cache_write_price_per_1k_tokens=None, cache_write_policy="unpublished")])
    result = await quote(body(max_tokens=1000))
    assert result.total_usd == Decimal("0.615000")  # 200*0.003 + 0.015


async def test_worst_rate_is_taken_across_context_geography_and_service_tier_variants(oracle):
    oracle(
        rates=[
            _row(),
            _row(context_tier=ContextTier.LONG, region="us-west-2", input_price_per_1k_tokens=Decimal("0.006")),
            _row(geography=Geography.GEO_CRIS, region="eu-west-1", output_price_per_1k_tokens=Decimal("0.030")),
        ]
    )
    result = await quote(body(max_tokens=1000))
    # Worst input across variants (0.006) and worst output (0.030), mixed only
    # because each is independently an upper bound: 200*0.006 + 0.030
    assert result.total_usd == Decimal("1.230000")


async def test_offline_batch_rows_do_not_lower_an_online_bound(oracle):
    # Batch pricing is cheaper and must not be eligible for an online request.
    oracle(rates=[_row(), _row(service_tier=ServiceTier.BATCH, region="us-west-1", input_price_per_1k_tokens=Decimal("0.0001"))])
    assert (await quote(body(max_tokens=1000))).total_usd == Decimal("0.765000")


async def test_live_generation_rows_join_the_bound(oracle):
    # A live V2 row more expensive than the bundled snapshot must raise the bound.
    oracle(rate_state=[_row(region="us-east-2", output_price_per_1k_tokens=Decimal("0.045"))])
    assert (await quote(body(max_tokens=1000))).total_usd == Decimal("0.795000")  # 0.75 + 0.045


async def test_total_rounds_up_to_the_ledger_quantum(oracle):
    """A bound rounded DOWN would admit an under-reserved request, so it rounds up."""
    cheap = {"cache_write_price_per_1k_tokens": None, "cache_write_policy": "unpublished"}
    # 200 * 0.0000000001 + 0.001 * 0.015 = 0.0000150200 exactly.
    oracle(rates=[_row(input_price_per_1k_tokens=Decimal("0.0000000001"), **cheap)])
    assert (await quote(body(max_tokens=1))).total_usd == Decimal("0.000016") > Decimal("0.00001502")
    # A different sub-quantum remainder rounds to the same ceiling, never down.
    oracle(rates=[_row(input_price_per_1k_tokens=Decimal("0.0000000004"), **cheap)])
    assert (await quote(body(max_tokens=1))).total_usd == Decimal("0.000016")
    # A total far below one quantum becomes one quantum, never a free request.
    oracle(rates=[_row(input_price_per_1k_tokens=Decimal("0.0000000001"), output_price_per_1k_tokens=Decimal("0.0000000001"), **cheap)])
    assert (await quote(body(max_tokens=1))).total_usd == Decimal("0.000001")


@pytest.mark.parametrize(
    "models, reason",
    [
        ({MODEL: {}}, QuoteReason.UNPUBLISHED_CONTEXT_BOUND),
        ({MODEL: {"context_max_input_tokens": None}}, QuoteReason.UNPUBLISHED_CONTEXT_BOUND),
        ({MODEL: {"context_max_input_tokens": 0}}, QuoteReason.UNPUBLISHED_CONTEXT_BOUND),
        ({MODEL: {"context_max_input_tokens": -5}}, QuoteReason.UNPUBLISHED_CONTEXT_BOUND),
        ({MODEL: {"context_max_input_tokens": True}}, QuoteReason.UNPUBLISHED_CONTEXT_BOUND),
        ({MODEL: {"context_max_input_tokens": "200000"}}, QuoteReason.UNPUBLISHED_CONTEXT_BOUND),
        ({}, QuoteReason.UNPUBLISHED_CONTEXT_BOUND),
    ],
)
async def test_unknown_or_invalid_context_bound_is_refused_not_defaulted(oracle, models, reason):
    oracle(models=models)
    with pytest.raises(QuoteRefusedError) as exc:
        await quote()
    assert exc.value.refusal.reason == reason


async def test_missing_published_rates_are_refused_not_estimated(oracle):
    oracle(rates=[_row(model_id="anthropic.claude-other-model")])
    with pytest.raises(QuoteRefusedError) as exc:
        await quote()
    assert exc.value.refusal.reason == QuoteReason.UNPUBLISHED_MODEL_PRICE


async def test_non_finite_or_non_positive_price_is_refused(oracle):
    oracle(rates=[_row(input_price_per_1k_tokens=Decimal(0), output_price_per_1k_tokens=Decimal(0), cache_write_price_per_1k_tokens=Decimal(0))])
    with pytest.raises(QuoteRefusedError) as exc:
        await quote()
    assert exc.value.refusal.reason == QuoteReason.UNPUBLISHED_MODEL_PRICE
    oracle(rates=[_row(input_price_per_1k_tokens=Decimal("NaN"))])
    with pytest.raises(QuoteRefusedError):
        await quote()


@pytest.mark.parametrize("output", [None, 0, -1, True, False, "1000", 1.5])
async def test_output_maximum_must_be_explicit_and_positive(oracle, output):
    oracle()
    with pytest.raises(QuoteRefusedError) as exc:
        await quote(body(max_tokens=output))
    assert exc.value.refusal.reason == QuoteReason.UNBOUNDED_OUTPUT


async def test_explicit_output_maximum_scales_the_bound(oracle):
    oracle()
    small, large = await quote(body(max_tokens=1)), await quote(body(max_tokens=100_000))
    assert large.total_usd > small.total_usd > 0
    assert large.total_usd - small.total_usd == Decimal("0.015") * Decimal(99_999) / Decimal(1000)


async def test_byte_length_cannot_change_the_bound(oracle):
    """Hidden provider framing is unbounded by bytes, so the whole window is reserved."""
    oracle()
    short = await quote(body(messages=[{"role": "user", "content": "hi"}]))
    long = await quote(body(messages=[{"role": "user", "content": "long text " * 5000}]))
    assert short.total_usd == long.total_usd
    assert short.request_sha256 != long.request_sha256


# ---------------------------------------------------------------------------
# Refusals are values, never zero-cost quotes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides, reason, capability",
    [
        ({"mcp_servers": ["remote"]}, QuoteReason.STATEFUL_INPUT, Capability.HISTORY),
        ({"container": "c-1"}, QuoteReason.STATEFUL_INPUT, Capability.HISTORY),
        ({"context_management": {"strategy": "auto"}}, QuoteReason.STATEFUL_INPUT, Capability.HISTORY),
        ({"previous_response_id": "resp_1"}, QuoteReason.STATEFUL_INPUT, Capability.HISTORY),
        ({"tools": [{"type": "web_search"}]}, QuoteReason.SERVER_TOOL_COST, Capability.SERVER_TOOLS),
        ({"tools": [{"type": "code_execution"}]}, QuoteReason.SERVER_TOOL_COST, Capability.SERVER_TOOLS),
        ({"messages": [{"role": "user", "content": [{"type": "image"}]}]}, QuoteReason.NON_TEXT_CONTENT, Capability.MEDIA),
        ({"messages": [{"role": "user", "content": [{"type": "document"}]}]}, QuoteReason.NON_TEXT_CONTENT, Capability.MEDIA),
        ({"system": [{"type": "image"}]}, QuoteReason.NON_TEXT_CONTENT, Capability.MEDIA),
        ({"messages": []}, QuoteReason.MALFORMED_REQUEST, Capability.TEXT),
        ({"messages": "hello"}, QuoteReason.MALFORMED_REQUEST, Capability.TEXT),
        ({"messages": [{"role": "user"}]}, QuoteReason.MALFORMED_REQUEST, Capability.TEXT),
        ({"tools": "web_search"}, QuoteReason.MALFORMED_REQUEST, Capability.TEXT),
        ({"model": 42}, QuoteReason.UNPUBLISHED_MODEL_PRICE, Capability.TEXT),
        ({"model": "gpt-5"}, QuoteReason.UNPUBLISHED_MODEL_PRICE, Capability.TEXT),
    ],
)
async def test_unbounded_requests_are_refused_with_a_named_capability(oracle, overrides, reason, capability):
    oracle()
    with pytest.raises(QuoteRefusedError) as exc:
        await quote(body(**overrides))
    assert exc.value.refusal.reason == reason
    assert exc.value.refusal.capability == capability


async def test_custom_tools_and_text_blocks_remain_quotable(oracle):
    oracle()
    result = await quote(
        body(
            tools=[{"type": "custom", "name": "t"}, {"name": "bare"}],
            system=[{"type": "text", "text": "s"}],
            messages=[
                {"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "thinking", "thinking": "t"}]},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "1", "name": "t", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "content": [{"type": "text", "text": "ok"}]}]},
            ],
        )
    )
    assert result.total_usd > 0


async def test_nested_tool_result_media_is_still_refused(oracle):
    oracle()
    with pytest.raises(QuoteRefusedError) as exc:
        await quote(body(messages=[{"role": "user", "content": [{"type": "tool_result", "content": [{"type": "image"}]}]}]))
    assert exc.value.refusal.capability == Capability.MEDIA


@pytest.mark.parametrize("raw", [b"", b"not json", b"[1,2]", b'"text"', b"null", b'{"model": '])
async def test_malformed_bodies_are_refused_not_crashed(oracle, raw):
    oracle()
    with pytest.raises(QuoteRefusedError) as exc:
        await quote(raw)
    assert exc.value.refusal.reason == QuoteReason.MALFORMED_REQUEST


async def test_duplicate_fields_are_ambiguous_and_refused(oracle):
    oracle()
    with pytest.raises(QuoteRefusedError) as exc:
        await quote(b'{"model": "' + MODEL.encode() + b'", "max_tokens": 1, "max_tokens": 999999}')
    assert exc.value.refusal.reason == QuoteReason.MALFORMED_REQUEST


def test_a_refusal_cannot_be_read_as_a_zero_cost_quote():
    refusal = QuoteRefusal(reason=QuoteReason.NO_ADAPTER, capability=Capability.RESPONSES)
    assert not hasattr(refusal, "total_usd")
    assert not isinstance(refusal, ProviderQuote)
    for bad in ({"reason": "invented", "capability": Capability.TEXT}, {"reason": QuoteReason.NO_ADAPTER, "capability": "invented"}):
        with pytest.raises(ValueError):
            QuoteRefusal(**bad)


@pytest.mark.parametrize(
    "overrides",
    [
        {"total_usd": Decimal(0)},
        {"total_usd": Decimal(-1)},
        {"total_usd": Decimal("NaN")},
        {"total_usd": Decimal("Infinity")},
        {"currency": "EUR"},
        {"max_output_tokens": -1},
        {"max_input_tokens": True},
        {"expires_at": NOW},
        {"expires_at": NOW - timedelta(seconds=1)},
    ],
)
def test_a_quote_cannot_be_constructed_without_a_real_bound(overrides):
    fields = {
        "provider": "anthropic",
        "endpoint": "/v1/messages",
        "capability": Capability.TEXT,
        "billing_model_id": MODEL,
        "request_sha256": request_digest(b"{}"),
        "pricing_revision": "r1",
        "currency": "USD",
        "issued_at": NOW,
        "expires_at": NOW + timedelta(seconds=QUOTE_TTL_SECONDS),
        "max_input_tokens": CONTEXT_LIMIT,
        "max_output_tokens": 10,
        "max_tool_tokens": 0,
        "max_cache_write_tokens": CONTEXT_LIMIT,
        "total_usd": Decimal("1.5"),
        "evidence_source": "test",
    }
    with pytest.raises(ValueError):
        ProviderQuote(**{**fields, **overrides})


# ---------------------------------------------------------------------------
# Binding: a quote belongs to exactly one request
# ---------------------------------------------------------------------------


async def test_quote_carries_the_identity_of_what_it_priced(oracle):
    oracle()
    raw = body()
    result = await quote(raw)
    assert result.provider == "anthropic" and result.endpoint == "/v1/messages"
    assert result.capability == Capability.TEXT and result.currency == "USD"
    assert result.billing_model_id == MODEL
    assert result.request_sha256 == request_digest(raw)
    assert result.pricing_revision == "test-snapshot|test_generation:7|7.3"
    assert result.evidence_source == "test_generation:7"
    assert result.issued_at == NOW and result.expires_at == NOW + timedelta(seconds=QUOTE_TTL_SECONDS)


async def test_a_quote_is_immutable(oracle):
    oracle()
    result = await quote()
    with pytest.raises(Exception):
        result.total_usd = Decimal("0.01")


async def test_a_different_payload_cannot_consume_the_original_quote(oracle):
    oracle()
    held = await quote(body(max_tokens=1))
    # The cheap quote must not pay for the expensive request.
    with pytest.raises(QuoteRefusedError) as exc:
        await revalidate_quote(held, body(max_tokens=100_000), "/v1/messages", now=NOW)
    assert exc.value.refusal.reason == QuoteReason.REQUEST_CHANGED
    # Even a semantically identical but byte-different body is refused: the hash
    # is over the exact bytes forwarded upstream.
    with pytest.raises(QuoteRefusedError):
        await revalidate_quote(held, body(max_tokens=1) + b" ", "/v1/messages", now=NOW)
    # The unchanged request still binds.
    await revalidate_quote(held, body(max_tokens=1), "/v1/messages", now=NOW)


async def test_a_different_model_cannot_consume_the_original_quote(oracle):
    oracle(models={MODEL: {"context_max_input_tokens": CONTEXT_LIMIT}, "anthropic.claude-haiku-4-6": {"context_max_input_tokens": CONTEXT_LIMIT}})
    held = await quote()
    swapped = held.binds(
        request_sha256=held.request_sha256,
        billing_model_id="anthropic.claude-haiku-4-6",
        pricing_revision=held.pricing_revision,
        now=NOW,
    )
    assert swapped is not None and swapped.reason == QuoteReason.REQUEST_CHANGED


async def test_a_rate_revision_change_cannot_consume_the_original_quote(oracle):
    held = await quote()
    # A new published generation lands between the quote and the reservation.
    state = oracle(rate_state=[_row(region="us-east-2", output_price_per_1k_tokens=Decimal("0.9"))]).state
    state.generation_id, state.pointer_revision, state.source = 8, 4, "test_generation:8"
    with pytest.raises(QuoteRefusedError) as exc:
        await revalidate_quote(held, body(), "/v1/messages", now=NOW)
    assert exc.value.refusal.reason == QuoteReason.REQUEST_CHANGED


async def test_an_expired_quote_cannot_be_spent(oracle):
    oracle()
    held = await quote()
    assert not held.expired(NOW)
    assert held.expired(NOW + timedelta(seconds=QUOTE_TTL_SECONDS))
    with pytest.raises(QuoteRefusedError) as exc:
        await revalidate_quote(held, body(), "/v1/messages", now=NOW + timedelta(seconds=QUOTE_TTL_SECONDS + 1))
    assert exc.value.refusal.reason == QuoteReason.QUOTE_EXPIRED


# ---------------------------------------------------------------------------
# The confirmation at the spend boundary (body-free, bounded)
# ---------------------------------------------------------------------------


async def test_confirming_a_fresh_quote_returns_no_refusal(oracle):
    oracle()
    assert await confirm_quote_spendable(await quote(), now=NOW) is None


async def test_confirming_an_expired_quote_refuses(oracle):
    """A TTL lapsing while the budget check awaited its own I/O must not spend."""
    oracle()
    held = await quote()
    refusal = await confirm_quote_spendable(held, now=NOW + timedelta(seconds=QUOTE_TTL_SECONDS + 1))
    assert refusal is not None and refusal.reason == QuoteReason.QUOTE_EXPIRED


async def test_confirming_after_a_rate_generation_rollover_refuses(oracle):
    """A published generation moving between admission and the hold must not spend."""
    configured = oracle()
    held = await quote()
    configured.state.generation_id, configured.state.pointer_revision = 9, 5
    refusal = await confirm_quote_spendable(held, now=NOW)
    assert refusal is not None and refusal.reason == QuoteReason.REQUEST_CHANGED


async def test_confirming_refuses_when_the_adapter_does_not_answer_in_time(oracle, monkeypatch):
    """An adapter that cannot confirm is a refusal, never a pass."""
    import time

    oracle()
    held = await quote()

    def never_answers(self):
        time.sleep(QUOTE_CONFIRM_TIMEOUT_SECONDS * 3)
        return "too-late"

    monkeypatch.setattr(AnthropicTextQuoteAdapter, "current_pricing_revision", never_answers)
    refusal = await confirm_quote_spendable(held, now=NOW)
    assert refusal is not None and refusal.reason == QuoteReason.ADAPTER_TIMEOUT


async def test_confirming_refuses_when_the_adapter_faults(oracle, monkeypatch):
    """A fault leaves the quote unconfirmed, which is not the same as confirmed."""
    oracle()
    held = await quote()

    def raises(self):
        raise RuntimeError("pricing backend unavailable")

    monkeypatch.setattr(AnthropicTextQuoteAdapter, "current_pricing_revision", raises)
    refusal = await confirm_quote_spendable(held, now=NOW)
    assert refusal is not None and refusal.reason == QuoteReason.UNPUBLISHED_MODEL_PRICE


async def test_confirming_a_quote_for_an_unregistered_route_refuses(oracle):
    """No adapter for the quoted endpoint means no upstream effect."""
    oracle()
    held = await quote()
    orphaned = replace(held, endpoint="/v1/responses")
    refusal = await confirm_quote_spendable(orphaned, now=NOW)
    assert refusal is not None and refusal.reason == QuoteReason.NO_ADAPTER


async def test_a_refusal_never_carries_an_amount():
    """A refusal must be unusable as a cost, so no path can sum it as free."""
    refusal = QuoteRefusal(reason=QuoteReason.ADAPTER_TIMEOUT, capability=Capability.TEXT)
    assert not hasattr(refusal, "total_usd")


async def test_current_pricing_revision_tracks_both_snapshot_and_live_state(oracle):
    """Either source moving must change the revision, or a rollover goes unseen."""
    configured = oracle()
    adapter = AnthropicTextQuoteAdapter()
    baseline = adapter.current_pricing_revision()
    configured.state.generation_id = 11
    assert adapter.current_pricing_revision() != baseline
    configured.snapshot.snapshot_version = "test-snapshot-2"
    assert adapter.current_pricing_revision() not in (baseline, None)


async def test_binding_returns_none_only_when_every_identity_matches(oracle):
    oracle()
    held = await quote()
    assert (
        held.binds(
            request_sha256=held.request_sha256,
            billing_model_id=held.billing_model_id,
            pricing_revision=held.pricing_revision,
            now=NOW,
        )
        is None
    )


# ---------------------------------------------------------------------------
# Registry: no adapter means no upstream effect
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/v1/responses", "/v1/embeddings", "/v1/messages/count_tokens", "/", "/model", ""])
async def test_no_registered_adapter_refuses_rather_than_estimating(oracle, path):
    oracle()
    assert adapter_for(path) is None
    with pytest.raises(QuoteRefusedError) as exc:
        await quote_request(body(), path, now=NOW)
    assert exc.value.refusal.reason == QuoteReason.NO_ADAPTER
    with pytest.raises(QuoteRefusedError):
        await revalidate_quote(await quote(), body(), path, now=NOW)


async def test_bedrock_model_route_takes_the_model_from_the_server_derived_path(oracle):
    oracle()
    # The path wins over the body: routing is server-derived, so a body-supplied
    # model cannot redirect the quote to a cheaper model's rates.
    path = f"/model/{MODEL}/invoke"
    raw = json.dumps({"model": "gpt-5", "messages": [{"role": "user", "content": "x"}], "max_tokens": 1000}).encode()
    result = await quote_request(raw, path, now=NOW)
    assert result.billing_model_id == MODEL and result.endpoint == path
    assert adapter_for(path) is not None


def test_the_anthropic_adapter_satisfies_the_protocol():
    adapter = AnthropicTextQuoteAdapter()
    assert isinstance(adapter, ProviderQuoteAdapter)
    assert adapter.provider == "anthropic" and adapter.capability == Capability.TEXT


# ---------------------------------------------------------------------------
# Trusted usage: unknown is never zero
# ---------------------------------------------------------------------------


async def test_only_a_complete_provider_usage_block_is_trusted():
    adapter = AnthropicTextQuoteAdapter()
    trusted = await adapter.reconcile({"usage": {"input_tokens": 10, "output_tokens": 5}})
    assert trusted.known and trusted.input_tokens == 10 and trusted.output_tokens == 5
    # Cache fields are additional paid input and join the total.
    with_cache = await adapter.reconcile(
        {"usage": {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 2}}
    )
    assert with_cache.known and with_cache.input_tokens == 15


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"usage": None},
        {"usage": {}},
        {"usage": "10"},
        {"usage": {"input_tokens": 10}},
        {"usage": {"output_tokens": 5}},
        {"usage": {"input_tokens": -1, "output_tokens": 5}},
        {"usage": {"input_tokens": 10, "output_tokens": None}},
        {"usage": {"input_tokens": True, "output_tokens": 5}},
        {"usage": {"input_tokens": "10", "output_tokens": 5}},
        {"usage": {"input_tokens": 1.5, "output_tokens": 5}},
        {"usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": -3}},
        {"usage": {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": "3"}},
        None,
        "streamed",
    ],
)
async def test_missing_or_ambiguous_usage_is_unknown_never_zero(response):
    """The hold must be retained when the true total is unknown."""
    trusted = await AnthropicTextQuoteAdapter().reconcile(response)
    assert not trusted.known and trusted.reason
    # Explicitly NOT a settled zero: the caller must branch on `known`.
    assert TrustedUsage.unknown("x").known is False


# ---------------------------------------------------------------------------
# Equivalence with the previous implementation (real snapshot rates)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"max_tokens": 1},
        {"max_tokens": 64_000},
        {"messages": [{"role": "user", "content": "long text " * 2000}]},
        {"system": "you are helpful"},
        {"tools": [{"type": "custom", "name": "t"}]},
    ],
)
async def test_ported_adapter_totals_match_the_previous_estimator_exactly(overrides):
    """The ported bound must not lose a cent of the original safety margin."""
    raw = body(**overrides)
    assert (await quote_request(raw, "/v1/messages")).total_usd == flow_meter.estimate_policy_model_cost(raw, "/v1/messages")


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_tokens": None},
        {"model": "unknown"},
        {"mcp_servers": ["remote"]},
        {"messages": [{"role": "user", "content": [{"type": "image"}]}]},
        {"tools": [{"type": "web_search"}]},
        {"messages": []},
    ],
)
async def test_ported_adapter_refuses_exactly_what_the_previous_estimator_refused(overrides):
    raw = body(**overrides)
    with pytest.raises(ValueError):
        flow_meter.estimate_policy_model_cost(raw, "/v1/messages")
    with pytest.raises(QuoteRefusedError):
        await quote_request(raw, "/v1/messages")


async def test_the_amount_only_view_still_refuses_unroutable_paths():
    with pytest.raises(ValueError):
        flow_meter.estimate_policy_model_cost(body(), "/v1/responses")
