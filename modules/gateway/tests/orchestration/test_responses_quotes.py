"""Bounded Responses quotes: fixed pricing oracles, refusals, trusted settlement.

Rates come from hand-built ``RateRow`` fixtures with exact ``Decimal`` values, so
each total asserts arithmetic rather than restating today's published prices. The
evidence-fixture tests are the exception: they check the REAL snapshot still
supports a bound, which is the property that would silently regress if a future
snapshot dropped a context ceiling or a rate.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from pricing_policy.policy import ContextTier, Geography, RateRow, ServiceTier
from src.orchestration.provider_quotes import (
    QUOTE_TTL_SECONDS,
    Capability,
    ProviderQuoteAdapter,
    QuoteReason,
    QuoteRefusedError,
    QuoteRequest,
    adapter_for,
    confirm_quote_spendable,
    quote_request,
    request_digest,
    revalidate_quote,
)
from src.orchestration.responses_quotes import RESPONSES_PATH, OpenAIResponsesQuoteAdapter

MODEL = "openai.gpt-6-astra"
SHORT_CONTEXT = 272_000
LONG_CONTEXT = 1_050_000
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

FIXTURE = Path(__file__).parent / "fixtures" / "responses-provider-contract-5226.json"


def _row(**overrides) -> RateRow:
    """One published rate. Prices are per 1k tokens, as ``RateRow`` defines them."""
    fields = {
        "model_id": MODEL,
        "geography": Geography.IN_REGION,
        "service_tier": ServiceTier.STANDARD,
        "context_tier": ContextTier.SHORT,
        "region": "us-east-1",
        "input_price_per_1k_tokens": Decimal("0.011"),
        "output_price_per_1k_tokens": Decimal("0.055"),
        "cache_read_price_per_1k_tokens": Decimal("0.0011"),
        "cache_write_price_per_1k_tokens": Decimal("0.01375"),
        "cache_write_policy": "full_rate",
        "source": "model_card",
        "source_url": "https://example.invalid/model-card",
        "source_content_sha256": "0" * 64,
        "verified_at": "2026-09-12T00:00:00+00:00",
        "max_input_tokens": SHORT_CONTEXT,
    }
    fields.update(overrides)
    return RateRow(**fields)


@pytest.fixture
def oracle(monkeypatch):
    """Pin the snapshot and rate state to fixed rates so totals are arithmetic."""
    import src.budget.pricing_v2_reader as reader

    state = SimpleNamespace(rows=(), source="test_generation:7", generation_id=7, pointer_revision=3)
    snapshot = SimpleNamespace(
        snapshot_version="test-snapshot",
        models={MODEL: {"context_tiers": {"short": SHORT_CONTEXT}}},
        rates=(_row(),),
        curated_non_openai={},
        alias_map={},
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
    monkeypatch.setattr("pricing_policy.policy.load_snapshot", lambda *a, **k: snapshot)
    monkeypatch.setattr(reader, "cached_rate_state", lambda: state)
    return configure


def body(**overrides) -> bytes:
    document = {"model": MODEL, "input": "hello", "max_output_tokens": 1000}
    document.update(overrides)
    return json.dumps(document).encode()


async def quote(raw: bytes | None = None, path: str = RESPONSES_PATH, now: datetime | None = NOW):
    return await quote_request(body() if raw is None else raw, path, now=now)


async def refusal(raw: bytes) -> tuple[str, str]:
    """The (reason, capability) a refused request reports."""
    with pytest.raises(QuoteRefusedError) as exc:
        await quote(raw)
    return exc.value.refusal.reason, exc.value.refusal.capability


# ---------------------------------------------------------------------------
# Routing: the adapter claims the route the middleware enforces
# ---------------------------------------------------------------------------


def test_the_responses_route_now_has_an_adapter():
    """Without this the middleware refuses every policy-governed Responses call."""
    adapter = adapter_for(RESPONSES_PATH)
    assert isinstance(adapter, OpenAIResponsesQuoteAdapter)
    assert adapter.provider == "openai" and adapter.capability == Capability.RESPONSES


def test_the_responses_adapter_satisfies_the_protocol():
    assert isinstance(OpenAIResponsesQuoteAdapter(), ProviderQuoteAdapter)


def test_registering_responses_did_not_capture_the_anthropic_routes():
    """Each adapter must keep owning exactly its own routes."""
    assert adapter_for("/v1/messages").provider == "anthropic"
    assert adapter_for(f"/model/{MODEL}/invoke").provider == "anthropic"


@pytest.mark.parametrize("path", ["/v1/responses", "/openai/v1/responses/", "/openai/v1/embeddings", "/openai/v1", ""])
def test_neighbouring_paths_are_not_claimed(path):
    """A near-miss path must not be quoted by this adapter's assumptions."""
    adapter = adapter_for(path)
    assert adapter is None or not isinstance(adapter, OpenAIResponsesQuoteAdapter)


# ---------------------------------------------------------------------------
# Fixed pricing oracles
# ---------------------------------------------------------------------------


async def test_total_prices_full_context_at_worst_input_rate_plus_requested_output(oracle):
    oracle()
    result = await quote(body(max_output_tokens=1000))
    # Cache write (0.01375) beats input (0.011), so it sets the input-side rate:
    #   272000/1000 * 0.01375 + 1000/1000 * 0.055 = 3.74 + 0.055
    assert result.total_usd == Decimal("3.795000")
    assert result.max_input_tokens == SHORT_CONTEXT and result.max_output_tokens == 1000
    assert result.max_cache_write_tokens == SHORT_CONTEXT and result.max_tool_tokens == 0
    assert result.provider == "openai" and result.capability == Capability.RESPONSES
    assert result.endpoint == RESPONSES_PATH and result.currency == "USD"


async def test_the_longest_published_context_tier_sets_the_input_bound(oracle):
    """A long-context tier must raise the bound, never be averaged away."""
    oracle(
        models={MODEL: {"context_tiers": {"short": SHORT_CONTEXT, "long": LONG_CONTEXT}}},
        rates=[_row(), _row(context_tier=ContextTier.LONG, max_input_tokens=LONG_CONTEXT)],
    )
    result = await quote(body(max_output_tokens=1000))
    assert result.max_input_tokens == LONG_CONTEXT
    # 1050000/1000 * 0.01375 + 0.055
    assert result.total_usd == Decimal("14.492500")


async def test_a_rate_row_ceiling_is_used_when_the_model_card_publishes_none(oracle):
    """Either published source can supply the ceiling; the dearer/larger wins."""
    oracle(models={MODEL: {}}, rates=[_row(max_input_tokens=SHORT_CONTEXT)])
    result = await quote(body(max_output_tokens=1000))
    assert result.max_input_tokens == SHORT_CONTEXT


async def test_worst_rate_is_taken_across_geography_and_service_tier_variants(oracle):
    oracle(
        rates=[
            _row(),
            _row(geography=Geography.GLOBAL_CRIS, region="us-west-2", input_price_per_1k_tokens=Decimal("0.022")),
            _row(service_tier=ServiceTier.PRIORITY, region="eu-west-1", output_price_per_1k_tokens=Decimal("0.0825")),
        ]
    )
    result = await quote(body(max_output_tokens=1000))
    # Worst input-side across variants (0.022 > 0.01375) and worst output
    # (0.0825), mixed only because each is independently an upper bound:
    #   272000/1000 * 0.022 + 0.0825
    assert result.total_usd == Decimal("6.066500")


async def test_unpublished_cache_write_never_prices_as_zero(oracle):
    """A NULL cache-write rate must not drag the input-side maximum down."""
    oracle(rates=[_row(cache_write_price_per_1k_tokens=None, cache_write_policy="unpublished")])
    result = await quote(body(max_output_tokens=1000))
    # Falls back to the input rate: 272000/1000 * 0.011 + 0.055
    assert result.total_usd == Decimal("3.047000")


async def test_a_live_generation_row_can_raise_the_bound(oracle):
    """Rows added by the live rate generation are part of the worst case."""
    oracle(rate_state=[_row(region="us-west-2", output_price_per_1k_tokens=Decimal("0.110"))])
    result = await quote(body(max_output_tokens=1000))
    # 272000/1000 * 0.01375 + 1000/1000 * 0.110
    assert result.total_usd == Decimal("3.850000")


async def test_reasoning_output_needs_no_extra_headroom(oracle):
    """max_output_tokens already caps reasoning + visible output together."""
    oracle()
    result = await quote(body(max_output_tokens=4000, reasoning={"effort": "high"}))
    assert result.max_output_tokens == 4000
    # 272000/1000 * 0.01375 + 4000/1000 * 0.055
    assert result.total_usd == Decimal("3.960000")


async def test_the_bound_rounds_up_to_the_ledger_quantum(oracle):
    """Rounding a bound DOWN would admit a request costing more than it reserved."""
    oracle(rates=[_row(input_price_per_1k_tokens=Decimal("0.0000001"), cache_write_price_per_1k_tokens=None, cache_write_policy="unpublished")])
    result = await quote(body(max_output_tokens=1))
    # 272000/1000 * 0.0000001 + 1/1000 * 0.055 = 0.0000272 + 0.000055 = 0.0000822,
    # which has more places than the ledger holds. Truncating would reserve
    # 0.000082 for a request that can cost 0.0000822, so it must round up.
    assert result.total_usd == Decimal("0.000083")


# ---------------------------------------------------------------------------
# Refusals: every one must land BEFORE the provider call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, 0, -1, "1000", 12.5, True, False])
async def test_a_missing_or_invalid_output_maximum_is_refused(oracle, value):
    """An unbounded output side cannot be reserved against a fixed budget."""
    oracle()
    document = {"model": MODEL, "input": "hello"}
    if value is not None:
        document["max_output_tokens"] = value
    assert await refusal(json.dumps(document).encode()) == (QuoteReason.UNBOUNDED_OUTPUT, Capability.RESPONSES)


async def test_a_model_with_no_published_context_ceiling_is_refused(oracle):
    """The gpt-oss family is priced but publishes no window: block, never guess."""
    oracle(models={MODEL: {"context_tiers": {"flat": None}}}, rates=[_row(max_input_tokens=None)])
    assert await refusal(body()) == (QuoteReason.UNPUBLISHED_CONTEXT_BOUND, Capability.RESPONSES)


async def test_a_model_with_no_published_rates_is_refused(oracle):
    oracle(rates=[_row(model_id="openai.some-other-model")])
    assert await refusal(body()) == (QuoteReason.UNPUBLISHED_MODEL_PRICE, Capability.RESPONSES)


async def test_a_non_openai_model_on_this_route_is_refused(oracle):
    """This route has no published Responses rate for a Claude id."""
    oracle()
    assert await refusal(body(model="anthropic.claude-sonnet-4-6")) == (QuoteReason.UNPUBLISHED_MODEL_PRICE, Capability.RESPONSES)


@pytest.mark.parametrize("model", [None, "", 5, {"id": MODEL}])
async def test_a_request_naming_no_usable_model_is_refused(oracle, model):
    oracle()
    document = {"input": "hello", "max_output_tokens": 10}
    if model is not None:
        document["model"] = model
    assert await refusal(json.dumps(document).encode()) == (QuoteReason.UNPUBLISHED_MODEL_PRICE, Capability.RESPONSES)


@pytest.mark.parametrize("field", ["previous_response_id", "conversation", "prompt"])
async def test_server_retained_history_is_refused(oracle, field):
    """Tokens the gateway never sees cannot be counted, so they cannot be bounded."""
    oracle()
    assert await refusal(body(**{field: "srv-1"})) == (QuoteReason.STATEFUL_INPUT, Capability.HISTORY)


async def test_background_execution_is_refused(oracle):
    """It settles out of band, so no trusted final usage arrives to reconcile."""
    oracle()
    assert await refusal(body(background=True)) == (QuoteReason.STATEFUL_INPUT, Capability.HISTORY)


@pytest.mark.parametrize("part", [{"type": "input_image"}, {"type": "input_file"}, {"type": "input_audio"}, {"no_type": 1}])
async def test_non_text_content_is_refused(oracle, part):
    oracle()
    raw = body(input=[{"role": "user", "content": [part]}])
    assert await refusal(raw) == (QuoteReason.NON_TEXT_CONTENT, Capability.MEDIA)


@pytest.mark.parametrize(
    "tool", [{"type": "web_search"}, {"type": "file_search"}, {"type": "code_interpreter"}, {"type": "mcp"}, {"type": "image_generation"}]
)
async def test_server_side_tools_are_refused(oracle, tool):
    """Hosted tools bill separately; that cost is outside this bound."""
    oracle()
    assert await refusal(body(tools=[tool])) == (QuoteReason.SERVER_TOOL_COST, Capability.SERVER_TOOLS)


async def test_a_client_executed_function_tool_is_admitted(oracle):
    """A function declaration is ordinary input, already inside the context bound."""
    oracle()
    result = await quote(body(tools=[{"type": "function", "name": "lookup", "parameters": {}}]))
    assert result.total_usd == Decimal("3.795000") and result.max_tool_tokens == 0


@pytest.mark.parametrize("field", ["usage", "input_tokens", "max_input_tokens"])
async def test_a_client_supplied_token_count_is_refused(oracle, field):
    """A caller must never be able to price its own request."""
    oracle()
    assert await refusal(body(**{field: 5})) == (QuoteReason.MALFORMED_REQUEST, Capability.RESPONSES)


@pytest.mark.parametrize("value", [None, "", [], {}, 5])
async def test_a_request_without_explicit_input_is_refused(oracle, value):
    oracle()
    document = {"model": MODEL, "max_output_tokens": 10}
    if value is not None:
        document["input"] = value
    reason, _ = await refusal(json.dumps(document).encode())
    assert reason == QuoteReason.MALFORMED_REQUEST


async def test_a_non_message_input_item_is_refused(oracle):
    """Referenced prior output/tool state is not this capability's cost profile."""
    oracle()
    raw = body(input=[{"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"}])
    assert await refusal(raw) == (QuoteReason.STATEFUL_INPUT, Capability.HISTORY)


@pytest.mark.parametrize("kind", ["function_call", "function_call_output", "item_reference", "reasoning", "file_search_call", "computer_call"])
@pytest.mark.parametrize("extra", [{}, {"content": "hello"}, {"content": [{"type": "input_text", "text": "hello"}]}, {"content": ""}])
async def test_an_out_of_scope_input_item_is_refused_even_carrying_content(oracle, kind, extra):
    """A client-supplied ``content`` key must not admit a kind this refuses.

    The guard decides on the item TYPE, not on the presence of ``content``.
    Deciding on ``content`` let every kind above through — the caller simply added
    the field — which contradicts this capability's promise to refuse unsupported
    shapes BEFORE submission. #5227 owns admitting these kinds properly; until
    then each is a named refusal.
    """
    oracle()
    raw = body(input=[{"type": kind, "id": "existing_item", **extra}])
    assert await refusal(raw) == (QuoteReason.STATEFUL_INPUT, Capability.HISTORY)


@pytest.mark.parametrize(
    "item",
    [
        pytest.param({"type": "message", "role": "user", "content": "hello"}, id="explicit_message_string"),
        pytest.param({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}, id="explicit_message_parts"),
        pytest.param({"role": "user", "content": "hello"}, id="absent_type_defaults_to_message"),
        pytest.param({"role": "user", "content": [{"type": "input_text", "text": "hello"}]}, id="absent_type_with_parts"),
    ],
)
async def test_ordinary_message_items_are_still_admitted(oracle, item):
    """The positive path is unchanged: message items, typed or bare, still quote."""
    oracle()
    result = await quote(body(input=[item]))
    assert result.total_usd == Decimal("3.795000")


async def test_a_duplicated_request_field_is_refused(oracle):
    """Two readers could disagree about what was requested; that is not quotable."""
    oracle()
    raw = b'{"model": "openai.gpt-6-astra", "input": "a", "input": "b", "max_output_tokens": 10}'
    assert await refusal(raw) == (QuoteReason.MALFORMED_REQUEST, Capability.RESPONSES)


@pytest.mark.parametrize("raw", [b"", b"not json", b"[1,2]", b'"text"', b"null"])
async def test_an_unparseable_body_is_refused(oracle, raw):
    oracle()
    reason, _ = await refusal(raw)
    assert reason == QuoteReason.MALFORMED_REQUEST


async def test_structured_text_input_is_admitted(oracle):
    """The normal Responses message shape must still be quotable."""
    oracle()
    raw = body(input=[{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}], instructions="be brief")
    result = await quote(raw)
    assert result.total_usd == Decimal("3.795000")


async def test_media_in_instructions_is_refused(oracle):
    """The system-instruction field is counted the same way as user input."""
    oracle()
    assert await refusal(body(instructions=[{"type": "input_image"}])) == (QuoteReason.NON_TEXT_CONTENT, Capability.MEDIA)


# ---------------------------------------------------------------------------
# Binding: the quote describes the exact request, at the exact revision
# ---------------------------------------------------------------------------


async def test_the_quote_binds_the_exact_request_bytes(oracle):
    oracle()
    raw = body()
    result = await quote(raw)
    assert result.request_sha256 == request_digest(raw)
    assert result.billing_model_id == MODEL
    assert result.issued_at == NOW and result.expires_at == NOW + timedelta(seconds=QUOTE_TTL_SECONDS)


async def test_a_changed_payload_after_the_quote_does_not_bind(oracle):
    """The prompt cannot be swapped after pricing an earlier revision."""
    oracle()
    result = await quote(body())
    with pytest.raises(QuoteRefusedError) as exc:
        await revalidate_quote(result, body(input="a different, longer prompt"), RESPONSES_PATH, now=NOW)
    assert exc.value.refusal.reason == QuoteReason.REQUEST_CHANGED


async def test_an_unchanged_request_still_binds(oracle):
    oracle()
    raw = body()
    result = await quote(raw)
    await revalidate_quote(result, raw, RESPONSES_PATH, now=NOW)  # must not raise


async def test_a_changed_model_after_the_quote_does_not_bind(oracle):
    """A cheaper model's quote must not be spendable against a dearer request."""
    oracle(rates=[_row(), _row(model_id="openai.gpt-5.6-sol")], models={MODEL: {"context_tiers": {"short": SHORT_CONTEXT}}})
    result = await quote(body())
    with pytest.raises(QuoteRefusedError) as exc:
        await revalidate_quote(result, body(model="openai.gpt-5.6-sol"), RESPONSES_PATH, now=NOW)
    assert exc.value.refusal.reason == QuoteReason.REQUEST_CHANGED


async def test_an_expired_quote_does_not_bind(oracle):
    oracle()
    raw = body()
    result = await quote(raw)
    later = NOW + timedelta(seconds=QUOTE_TTL_SECONDS + 1)
    with pytest.raises(QuoteRefusedError) as exc:
        await revalidate_quote(result, raw, RESPONSES_PATH, now=later)
    assert exc.value.refusal.reason == QuoteReason.QUOTE_EXPIRED


async def test_a_rate_revision_rollover_is_not_spendable(oracle):
    """A published generation moving after the quote must requote, not spend."""
    configured = oracle()
    result = await quote(body())
    assert await confirm_quote_spendable(result, now=NOW) is None
    configured.state.generation_id = 8
    refused = await confirm_quote_spendable(result, now=NOW)
    assert refused is not None and refused.reason == QuoteReason.REQUEST_CHANGED


async def test_an_expired_quote_is_not_spendable_at_the_reservation_boundary(oracle):
    oracle()
    result = await quote(body())
    refused = await confirm_quote_spendable(result, now=NOW + timedelta(seconds=QUOTE_TTL_SECONDS + 1))
    assert refused is not None and refused.reason == QuoteReason.QUOTE_EXPIRED


# ---------------------------------------------------------------------------
# Trusted settlement: unknown is never zero, and cached input is never doubled
# ---------------------------------------------------------------------------


async def test_a_complete_usage_block_is_trusted():
    adapter = OpenAIResponsesQuoteAdapter()
    trusted = await adapter.reconcile({"usage": {"input_tokens": 120, "output_tokens": 40}})
    assert trusted.known and trusted.input_tokens == 120 and trusted.output_tokens == 40


async def test_cached_input_is_not_added_on_top_of_the_inclusive_total():
    """On this API input_tokens ALREADY includes cached input; adding it double-counts."""
    adapter = OpenAIResponsesQuoteAdapter()
    trusted = await adapter.reconcile({"usage": {"input_tokens": 120, "output_tokens": 40, "input_tokens_details": {"cached_tokens": 100}}})
    assert trusted.known and trusted.input_tokens == 120


async def test_reasoning_tokens_are_not_added_on_top_of_the_output_total():
    adapter = OpenAIResponsesQuoteAdapter()
    trusted = await adapter.reconcile({"usage": {"input_tokens": 10, "output_tokens": 40, "output_tokens_details": {"reasoning_tokens": 30}}})
    assert trusted.known and trusted.output_tokens == 40


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"output_tokens": 5},
        {"input_tokens": 5},
        {"input_tokens": -1, "output_tokens": 5},
        {"input_tokens": 5, "output_tokens": -1},
        {"input_tokens": "5", "output_tokens": 5},
        {"input_tokens": 5.5, "output_tokens": 5},
        {"input_tokens": True, "output_tokens": 5},
        {"input_tokens": 5, "output_tokens": None},
    ],
)
async def test_missing_or_invalid_usage_is_unknown_never_zero(usage):
    """An unknown total retains the hold; a zero would release it as free."""
    adapter = OpenAIResponsesQuoteAdapter()
    trusted = await adapter.reconcile({"usage": usage} if usage is not None else {})
    assert not trusted.known and trusted.reason


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": 10, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 11}},
        {"input_tokens": 10, "output_tokens": 5, "input_tokens_details": {"cached_tokens": -1}},
        {"input_tokens": 10, "output_tokens": 5, "input_tokens_details": "nope"},
        {"input_tokens": 10, "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": 6}},
        {"input_tokens": 10, "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": "x"}},
    ],
)
async def test_self_contradictory_usage_is_unknown(usage):
    """A subset exceeding its total means the counts disagree; that is not settleable."""
    adapter = OpenAIResponsesQuoteAdapter()
    trusted = await adapter.reconcile({"usage": usage})
    assert not trusted.known and trusted.reason


@pytest.mark.parametrize(
    "usage",
    [
        pytest.param({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 11}, id="top_level_read_exceeds_input"),
        pytest.param({"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 11}, id="top_level_write_exceeds_input"),
        pytest.param({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": -1}, id="top_level_read_negative"),
        pytest.param({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": "bad"}, id="top_level_read_not_a_count"),
        pytest.param({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": True}, id="top_level_read_boolean"),
        pytest.param({"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 2.5}, id="top_level_write_not_an_integer"),
        pytest.param({"input_tokens": 10, "output_tokens": 5, "input_tokens_details": {"cache_write_tokens": 11}}, id="nested_write_exceeds_input"),
    ],
)
async def test_a_contradictory_cache_counter_is_unknown_in_every_spelling(usage):
    """Which field name carries the contradiction must not change the verdict.

    Production pricing (``normalize_usage`` with ``api_format="openai"``) reads
    cache reads and writes from either the top-level counters or the nested
    details block, and clamps one that will not fit. Any spelling this adapter
    failed to check would let a clamped total release a real reservation.
    """
    adapter = OpenAIResponsesQuoteAdapter()
    trusted = await adapter.reconcile({"usage": usage})
    assert not trusted.known and trusted.reason


@pytest.mark.parametrize(
    "usage",
    [
        pytest.param({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 6, "cache_creation_input_tokens": 6}, id="top_level_pair"),
        pytest.param(
            {"input_tokens": 10, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 6, "cache_write_tokens": 6}}, id="nested_pair"
        ),
        pytest.param(
            {"input_tokens": 10, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 6}, "cache_creation_input_tokens": 6},
            id="mixed_spellings",
        ),
    ],
)
async def test_cache_reads_and_writes_must_fit_inside_the_input_total_together(usage):
    """Reads and writes are disjoint parts of one inclusive total.

    Each counter fits on its own here, so a per-counter check passes while the
    pair still cannot sit inside ``input_tokens`` — which is exactly what the
    pricing path clamps. The SUM is what has to fit.
    """
    adapter = OpenAIResponsesQuoteAdapter()
    trusted = await adapter.reconcile({"usage": usage})
    assert not trusted.known and trusted.reason


@pytest.mark.parametrize(
    "usage",
    [
        pytest.param({"input_tokens": 500, "output_tokens": 16, "cache_read_input_tokens": 300}, id="read_only"),
        pytest.param({"input_tokens": 500, "output_tokens": 16, "cache_read_input_tokens": 300, "cache_creation_input_tokens": 100}, id="read_write"),
        pytest.param({"input_tokens": 500, "output_tokens": 16, "cache_read_input_tokens": 500}, id="wholly_cached"),
        pytest.param({"input_tokens": 500, "output_tokens": 16, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}, id="measured_zeros"),
    ],
)
async def test_consistent_cache_counters_still_settle_without_double_counting(usage):
    """The stricter check must not refuse ordinary cache activity.

    Cache counts that fit inside the inclusive total are consistent evidence, and
    the settled input stays the reported total — cache is a subset here, never an
    addend, so a cache hit is not charged twice.
    """
    adapter = OpenAIResponsesQuoteAdapter()
    trusted = await adapter.reconcile({"usage": usage})
    assert trusted.known and trusted.input_tokens == 500 and trusted.output_tokens == 16


@pytest.mark.parametrize("response", [None, "text", 5, [], {"no_usage": 1}])
async def test_a_non_response_object_is_unknown(response):
    adapter = OpenAIResponsesQuoteAdapter()
    trusted = await adapter.reconcile(response)
    assert not trusted.known


async def test_a_measured_zero_output_is_still_a_known_total():
    """An explicitly measured zero differs from an absent count."""
    adapter = OpenAIResponsesQuoteAdapter()
    trusted = await adapter.reconcile({"usage": {"input_tokens": 7, "output_tokens": 0}})
    assert trusted.known and trusted.output_tokens == 0


# ---------------------------------------------------------------------------
# Provider evidence: the fixture records provenance, and real rates still bound
# ---------------------------------------------------------------------------


def test_the_evidence_fixture_records_the_deployed_contract_provenance():
    """Offline fixtures must say where each limit and rate came from."""
    evidence = json.loads(FIXTURE.read_text())
    assert evidence["capability"] == "responses" and evidence["provider"] == "openai"
    assert evidence["deployed_endpoint"]["path"] == RESPONSES_PATH
    # The deployed endpoint is Bedrock's OpenAI-compatible host, not api.openai.com,
    # and it publishes no token-count API — the reason the bound is full-context.
    assert "bedrock-runtime" in evidence["deployed_endpoint"]["upstream_host_template"]
    assert evidence["token_counting"]["count_endpoint_available"] is False
    assert evidence["pricing_snapshot_version"]
    for entry in evidence["models"].values():
        assert entry["source_url"] and entry["verified_at"]
        assert entry["quotable"] is (entry["full_context_ceiling_tokens"] is not None)


def test_the_real_snapshot_still_supports_a_bound_for_the_evidenced_models():
    """Guards the regression where a snapshot drops a ceiling and the route dies."""
    from pricing_policy import load_snapshot
    from pricing_policy.policy import model_rate_candidates
    from src.orchestration.responses_quotes import _published_context_ceiling

    evidence = json.loads(FIXTURE.read_text())
    snapshot = load_snapshot()
    quotable = [model for model, entry in evidence["models"].items() if entry["quotable"]]
    assert quotable, "the fixture must record at least one quotable model"
    for model in quotable:
        rows = model_rate_candidates(snapshot.rates, model)
        assert rows, f"{model} lost its published rates"
        ceiling = _published_context_ceiling(snapshot.models.get(model, {}), rows)
        assert ceiling == evidence["models"][model]["full_context_ceiling_tokens"]


async def test_models_without_a_published_ceiling_stay_refused_on_real_evidence():
    """gpt-oss is priced but unbounded; it must refuse against the real snapshot."""
    evidence = json.loads(FIXTURE.read_text())
    blocked = [model for model, entry in evidence["models"].items() if not entry["quotable"]]
    assert blocked, "the fixture must record the models this capability cannot bound"
    for model in blocked:
        raw = json.dumps({"model": model, "input": "hello", "max_output_tokens": 16}).encode()
        with pytest.raises(QuoteRefusedError) as exc:
            await quote_request(raw, RESPONSES_PATH, now=NOW)
        assert exc.value.refusal.reason == QuoteReason.UNPUBLISHED_CONTEXT_BOUND


async def test_a_real_evidenced_model_produces_a_positive_bound():
    """The end-to-end positive case on the actual published snapshot."""
    raw = json.dumps({"model": MODEL, "input": "hello", "max_output_tokens": 256}).encode()
    result = await quote_request(raw, RESPONSES_PATH, now=NOW)
    assert result.total_usd > 0 and result.max_output_tokens == 256
    # Priced off the model's own published window, not a default.
    assert result.max_input_tokens == LONG_CONTEXT
    assert result.request_sha256 == request_digest(raw)


async def test_the_adapter_reads_the_body_model_not_a_path_model(oracle):
    """This route carries no server-derived model in the path, unlike /model/{id}."""
    oracle()
    adapter = OpenAIResponsesQuoteAdapter()
    result = await adapter.quote(QuoteRequest(body=body(), path=RESPONSES_PATH, now=NOW))
    assert result.billing_model_id == MODEL
