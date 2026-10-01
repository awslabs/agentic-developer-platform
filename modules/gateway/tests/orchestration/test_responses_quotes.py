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


@pytest.mark.parametrize("part", [{"type": "unknown_part"}, {"no_type": 1}])
async def test_a_part_that_is_neither_text_nor_media_is_refused(oracle, part):
    """#5227 admits media, but only the part types the route actually accepts."""
    oracle()
    raw = body(input=[{"role": "user", "content": [part]}])
    assert await refusal(raw) == (QuoteReason.NON_TEXT_CONTENT, Capability.MEDIA)


@pytest.mark.parametrize("part", [{"type": "input_image"}, {"type": "input_file"}, {"type": "input_audio"}])
async def test_a_media_part_carrying_no_payload_is_refused(oracle, part):
    """A media part we cannot read is unreadable, not unsupported — and still never forwarded."""
    oracle()
    raw = body(input=[{"role": "user", "content": [part]}])
    assert await refusal(raw) == (QuoteReason.MALFORMED_REQUEST, Capability.MEDIA)


@pytest.mark.parametrize(
    "part",
    [
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
        {"type": "input_file", "file_data": "data:application/pdf;base64,AAAA"},
        # The API spells audio as a nested object holding raw base64; there is no
        # `audio_url` field, so this is the only spec-compliant inline audio shape.
        {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}},
    ],
)
async def test_inline_media_is_admitted_inside_the_full_context_bound(oracle, part):
    """Media bytes are tokens against the same window already reserved in full.

    The total is identical to the text-only quote of the same shape: the bound
    already reserves the entire published context, so admitting media adds no
    cost and needs no count of the media itself. The capability records MEDIA so
    the quote's own evidence names the weakest guarantee behind it.
    """
    oracle()
    result = await quote(body(input=[{"role": "user", "content": [part]}]))
    assert result.total_usd == Decimal("3.795000")
    assert result.capability == Capability.MEDIA


@pytest.mark.parametrize(
    "part",
    [
        {"type": "input_image", "file_id": "file-1"},
        {"type": "input_file", "file_id": "file-1"},
        {"type": "input_file", "file_url": "https://example.invalid/a.pdf"},
        {"type": "input_image", "image_url": "https://example.invalid/a.png"},
        # A reference carried INSIDE the nested audio object, and beside it.
        {"type": "input_audio", "input_audio": {"data": "AAAA", "file_id": "file-1"}},
        {"type": "input_audio", "input_audio": {"data": "AAAA"}, "file_id": "file-1"},
    ],
)
async def test_media_named_by_reference_is_refused(oracle, part):
    """A reference is resolved outside the caller's authorization boundary.

    The provider dereferences it under the gateway's single shared principal, so
    one tenant could name another tenant's stored content. Refusing also means the
    gateway adds no URL fetcher of its own. Not a cost reason: the bound reserves
    the whole context window and counts nothing.
    """
    oracle()
    raw = body(input=[{"role": "user", "content": [part]}])
    assert await refusal(raw) == (QuoteReason.MUTABLE_MEDIA_REFERENCE, Capability.MEDIA)


@pytest.mark.parametrize(
    "part",
    [
        {"type": "input_audio", "input_audio": {"data": "AAAA"}, "image_url": "https://example.invalid/x.png"},
        {"type": "input_file", "image_url": "data:image/png;base64,AAAA", "file_data": "https://example.invalid/x.pdf"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "file_data": "s3://bucket/x.pdf"},
    ],
)
async def test_an_inline_payload_cannot_smuggle_a_reference_beside_it(oracle, part):
    """Every payload field must be inline, not merely the first one found.

    The body is forwarded byte-for-byte, so a reference in a second field still
    reaches the provider and is dereferenced under the shared principal. Accepting
    on the first inline hit let an inline decoy carry one past the check.
    """
    oracle()
    raw = body(input=[{"role": "user", "content": [part]}])
    assert await refusal(raw) == (QuoteReason.MUTABLE_MEDIA_REFERENCE, Capability.MEDIA)


@pytest.mark.parametrize(
    "part",
    [
        {"type": "input_image", "image_url": {"url": "https://example.invalid/secret"}},
        {"type": "input_image", "image_url": ""},
    ],
)
async def test_an_unreadable_payload_field_is_refused_not_skipped(oracle, part):
    """A structured or empty payload cannot be certified as covered by the digest.

    A dict failed the string check and was silently skipped, so the reference it
    held was never scheme-checked.

    The dict case carried a second, cross-type ``file_data`` key when written. That
    key is now refused earlier as an unrecognised field for an ``input_image`` part
    (``mutable_media_reference``), which masked the check this test exists to pin,
    so it is dropped here and covered on its own by
    ``test_no_unrecognised_field_rides_along_beside_a_valid_payload``. Both shapes
    are still refused; only which rule fires first changed.
    """
    oracle()
    raw = body(input=[{"role": "user", "content": [part]}])
    assert await refusal(raw) == (QuoteReason.MALFORMED_REQUEST, Capability.MEDIA)


async def test_audio_is_admitted_in_the_shape_the_api_actually_defines(oracle):
    """Audio's payload is a nested object, not a URL string.

    The Responses API defines ``input_audio`` as ``{"data": "<base64>", "format":
    "wav"|"mp3"}`` and defines no ``audio_url`` field at all. Scanning only
    data-URI *strings* refused the sole spec-compliant way to send audio while the
    capability matrix advertised audio as admitted, so a caller following the API
    got a 403 on a capability this issue promises. Pinned because the shape is an
    external contract no other test in this repo exercises.
    """
    oracle()
    part = {"type": "input_audio", "input_audio": {"data": "UklGRg==", "format": "wav"}}
    result = await quote(body(input=[{"role": "user", "content": [part]}]))
    assert result.capability == Capability.MEDIA
    # Same bound as text: the full context window is already reserved either way.
    assert result.total_usd == Decimal("3.795000")


@pytest.mark.parametrize("nested", [{"format": "wav"}, {"data": ""}, {"data": 1}])
async def test_nested_audio_without_readable_bytes_is_refused(oracle, nested):
    """Audio this adapter cannot read is unreadable, not silently free.

    Without the payload the adapter cannot certify the digest covers the media, so
    the part must not be admitted merely for having the right ``type``.
    """
    oracle()
    raw = body(input=[{"role": "user", "content": [{"type": "input_audio", "input_audio": nested}]}])
    assert await refusal(raw) == (QuoteReason.MALFORMED_REQUEST, Capability.MEDIA)


@pytest.mark.parametrize(
    "part",
    [
        # A reference as a sibling key INSIDE the nested payload object.
        {"type": "input_audio", "input_audio": {"data": "AAAA", "url": "https://example.invalid/x"}},
        {"type": "input_audio", "input_audio": {"data": "AAAA", "file_data": "https://example.invalid/x.pdf"}},
        {"type": "input_audio", "input_audio": {"data": "AAAA", "file_ids": ["victim-1"]}},
        # A nested block list one level down, the Anthropic-style nesting escape.
        {"type": "input_audio", "input_audio": {"data": "AAAA", "content": [{"type": "input_file", "file_id": "victim"}]}},
        # A payload field belonging to a DIFFERENT part type, beside a valid one.
        {"type": "input_audio", "input_audio": {"data": "AAAA"}, "audio_url": "https://example.invalid/x.wav"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "audio_url": "https://example.invalid/x.wav"},
    ],
)
async def test_no_unrecognised_field_rides_along_beside_a_valid_payload(oracle, part):
    """A media part may carry only the keys the API defines for it.

    Each shape here pairs a readable inline payload with an extra key. Because the
    body is forwarded byte-for-byte, that key still reaches the provider, where a
    reference is dereferenced under the gateway's single shared principal — the
    access ``MUTABLE_MEDIA_REFERENCE`` exists to prevent. Denylisting the two known
    reference names let every other ``*_url``/``*_id``/nested shape through, so the
    check is an allowlist and this test pins that.
    """
    oracle()
    raw = body(input=[{"role": "user", "content": [part]}])
    assert await refusal(raw) == (QuoteReason.MUTABLE_MEDIA_REFERENCE, Capability.MEDIA)


@pytest.mark.parametrize(
    "part",
    [
        {"type": "input_image", "input_audio": {"data": "AAAA"}},
        {"type": "input_file", "input_audio": {"data": "AAAA"}},
    ],
)
async def test_one_part_type_is_not_satisfied_by_another_types_payload(oracle, part):
    """The payload requirement is keyed to the part's declared type.

    An ``input_image``'s bytes are read by the provider from ``image_url``, so a
    nested audio object beside it certifies nothing about the image. A shared
    "something here looked inline" tally admitted these with no verified payload of
    their own.
    """
    oracle()
    raw = body(input=[{"role": "user", "content": [part]}])
    assert await refusal(raw) == (QuoteReason.MUTABLE_MEDIA_REFERENCE, Capability.MEDIA)


async def test_nested_audio_data_must_be_base64_not_a_url(oracle):
    """A URL in the field documented as raw base64 is a reference, not bytes."""
    oracle()
    part = {"type": "input_audio", "input_audio": {"data": "s3://victim/other-tenant-secret.wav"}}
    raw = body(input=[{"role": "user", "content": [part]}])
    assert await refusal(raw) == (QuoteReason.MUTABLE_MEDIA_REFERENCE, Capability.MEDIA)


@pytest.mark.parametrize(
    "part",
    [
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "auto"},
        {"type": "input_file", "file_data": "data:application/pdf;base64,AAAA", "filename": "a.pdf"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "prompt_cache_breakpoint": {"mode": "explicit"}},
        # Wrapped base64: the newline is stripped before decoding, so a payload
        # split across lines still admits. (Written as "UklGRgAA==\n" when this test
        # was added, which is not valid base64 at all -- 10 characters cannot pad to
        # a 4-boundary -- and passed only while the check was a character-class test.)
        {"type": "input_audio", "input_audio": {"data": "UklGRgAA\nAAAA", "format": "mp3"}},
    ],
)
async def test_the_optional_fields_the_api_defines_are_still_admitted(oracle, part):
    """The allowlist must not refuse a caller following the API.

    ``detail``, ``filename`` and ``prompt_cache_breakpoint`` are documented on
    these parts, and base64 may carry padding and line breaks. Tightening the part
    shape is only correct if legitimate requests keep working.
    """
    oracle()
    result = await quote(body(input=[{"role": "user", "content": [part]}]))
    assert result.capability == Capability.MEDIA


async def test_media_in_instructions_is_still_refused(oracle):
    """``instructions`` is a plain string field; a structured part there is malformed input."""
    oracle()
    raw = body(instructions=[{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}])
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


async def test_an_inline_function_call_is_admitted(oracle):
    """Complete function arguments are inline input, not a history lookup."""
    oracle()
    raw = body(input=[{"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"}])
    assert (await quote(raw)).max_tool_tokens == 0


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


# ---------------------------------------------------------------------------
# The reference scan must not be confined to the media branch (reviewer round 2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["input_text", "text", "output_text", "summary_text", "refusal"])
@pytest.mark.parametrize("field", ["file_id", "file_url"])
async def test_a_reference_on_a_non_media_part_is_refused(oracle, kind, field):
    """A reference is refused on EVERY part, not only on parts treated as media.

    ``_inline_media_part`` holds the payload rules, but it is reached only for a
    media ``type``. So a part declaring itself ``input_text`` and hanging a
    ``file_id`` off it was never inspected at all, and the body is forwarded
    byte-for-byte — the provider would still dereference the name under the
    gateway's single shared IRSA principal. That is exactly the shape the matrix's
    ``media_by_reference`` row claims to refuse, so the scan has to happen before
    the type dispatch rather than inside one branch of it.
    """
    oracle()
    part = {"type": kind, "text": "describe this", field: "victim-tenant-b"}
    raw = body(input=[{"role": "user", "content": [part]}])
    assert await refusal(raw) == (QuoteReason.MUTABLE_MEDIA_REFERENCE, Capability.MEDIA)


async def test_a_reference_on_a_text_part_is_refused_in_instructions_too(oracle):
    """``instructions`` walks the same parts, so it needs the same guarantee."""
    oracle()
    raw = body(instructions=[{"type": "input_text", "text": "hi", "file_url": "https://example.invalid/x.pdf"}])
    assert await refusal(raw) == (QuoteReason.MUTABLE_MEDIA_REFERENCE, Capability.MEDIA)


@pytest.mark.parametrize(
    "image_url",
    [
        # `startswith("data:")` is a scheme test and nothing more. Each of these
        # opens with the right five characters while naming somewhere else.
        "data://example.invalid/probe.png",
        "data:,https://example.invalid/probe.png",
        "data:text/html,<script>x</script>",
        "data:image/png;base64,AA#https://example.invalid/x",
    ],
)
async def test_a_data_uri_must_be_the_whole_inline_form_not_just_the_scheme(oracle, image_url):
    """Admission requires ``data:<mediatype>;base64,<payload>`` that actually decodes."""
    oracle()
    raw = body(input=[{"role": "user", "content": [{"type": "input_image", "image_url": image_url}]}])
    assert await refusal(raw) == (QuoteReason.MUTABLE_MEDIA_REFERENCE, Capability.MEDIA)


@pytest.mark.parametrize(
    "data",
    [
        # The base64 alphabet contains `+`, `/` and `=`, so a character-class test
        # passes path- and key-shaped references naming content we do not hold.
        # Only the `:` in `s3://` was ever caught by it.
        "file/VICTIMTENANTB/secret==",
        "arn+aws+s3+++victim/secret",
        "AAAA\r\nhttpsXY",
    ],
)
async def test_a_nested_payload_must_actually_decode_as_base64(oracle, data):
    """Requiring the characters be *readable as* base64, not merely drawn from it."""
    oracle()
    part = {"type": "input_audio", "input_audio": {"data": data, "format": "wav"}}
    raw = body(input=[{"role": "user", "content": [part]}])
    assert await refusal(raw) == (QuoteReason.MUTABLE_MEDIA_REFERENCE, Capability.MEDIA)


@pytest.mark.parametrize(
    "part",
    [
        # An allowlisted KEY still needs a checked VALUE: each of these is
        # caller-controlled and forwarded byte-for-byte inside an admitted request.
        {"type": "input_audio", "input_audio": {"data": "UklGRg==", "format": "https://example.invalid/probe.wav"}},
        {"type": "input_file", "file_data": "data:application/pdf;base64,AAAA", "filename": "s3://victim-tenant-b/secret.pdf"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "https://example.invalid/x"},
    ],
)
async def test_an_allowlisted_key_still_has_its_value_checked(oracle, part):
    """Permitting a key is not permitting any value under it."""
    oracle()
    raw = body(input=[{"role": "user", "content": [part]}])
    assert await refusal(raw) == (QuoteReason.MUTABLE_MEDIA_REFERENCE, Capability.MEDIA)


async def test_an_unhashable_part_type_refuses_rather_than_raising(oracle):
    """A quote refusal, not a bare ``TypeError`` escaping the adapter.

    An unhashable ``type`` hit the set membership test and raised
    ``TypeError: unhashable type: 'list'``. It failed closed only because a caller
    catches ``TypeError`` broadly, which records no reason or capability — so the
    refusal was invisible in the evidence the quote layer exists to produce.
    """
    oracle()
    part = {"type": ["input_image"], "image_url": "https://example.invalid/x"}
    raw = body(input=[{"role": "user", "content": [part]}])
    assert await refusal(raw) == (QuoteReason.MALFORMED_REQUEST, Capability.RESPONSES)


async def test_kimi_profile_has_bounded_quote(oracle):
    model = "moonshotai.kimi-k3"
    oracle(rates=[_row(model_id=model, max_input_tokens=1000000, context_tier="flat")], models={model: {"context_tiers": {"flat": 1000000}}})
    result = await quote(body(model="global." + model, max_output_tokens=256))
    assert result.total_usd == Decimal("13.764080")


async def test_inline_encrypted_reasoning_uses_same_full_context_bound(oracle):
    oracle()
    item = {"type": "reasoning", "encrypted_content": "fixture-opaque-bytes", "summary": []}
    raw = body(input=[item, {"role": "user", "content": "continue"}])
    quoted = await quote(raw)
    assert quoted.total_usd == (await quote()).total_usd
    await revalidate_quote(quoted, raw, RESPONSES_PATH, now=NOW)
    changed = body(input=[{**item, "encrypted_content": "changed"}, {"role": "user", "content": "continue"}])
    with pytest.raises(QuoteRefusedError):
        await revalidate_quote(quoted, changed, RESPONSES_PATH, now=NOW)


@pytest.mark.parametrize(
    "extra",
    [
        {"id": {"reference": "foreign"}},
        {"content": "private plaintext"},
        {"encrypted_content": ""},
        {"encrypted_content": "x" * 65537},
        {"summary": [{"type": "summary_text", "text": "fixture", "file_id": "foreign"}]},
        {"status": "in_progress"},
    ],
)
async def test_reasoning_never_admits_server_references_or_partial_state(oracle, extra):
    oracle()
    raw = body(input=[{"type": "reasoning", "encrypted_content": "fixture-opaque-bytes", "summary": [], **extra}])
    assert await refusal(raw) == (QuoteReason.STATEFUL_INPUT, Capability.HISTORY)


async def test_codex_namespace_and_multiturn_client_tools_are_bounded(oracle):
    oracle()
    tools = [{"type": "namespace", "name": "multi_agent_v1", "tools": [{"type": "function", "name": "spawn_agent", "parameters": {}}]}]
    transcript = [
        {"role": "user", "content": "Inspect the checkout"},
        {"type": "function_call", "name": "exec_command", "call_id": "c1", "arguments": '{"cmd":"pwd"}'},
        {"type": "function_call_output", "call_id": "c1", "output": "/workspace"},
        {"type": "custom_tool_call", "name": "apply_patch", "call_id": "c2", "input": "patch text"},
        {"type": "custom_tool_call_output", "call_id": "c2", "output": "Done"},
    ]
    result = await quote(body(tools=tools, input=transcript))
    assert result.total_usd == Decimal("3.795000")
    assert result.max_tool_tokens == 0


@pytest.mark.parametrize("child", [{"type": "web_search"}, {"type": "mcp"}, {"type": "namespace", "tools": []}, {"type": []}])
async def test_namespace_cannot_hide_hosted_or_unknown_tools(oracle, child):
    oracle()
    assert await refusal(body(tools=[{"type": "namespace", "name": "hidden", "tools": [child]}])) == (
        QuoteReason.SERVER_TOOL_COST,
        Capability.SERVER_TOOLS,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"output": {"file_id": "remote"}},
        {"output": [{"type": "input_image", "image_url": "https://remote"}]},
        {"call_id": None},
        {"status": "in_progress"},
        {"content": "extra"},
    ],
)
async def test_client_tool_results_require_complete_inline_text(oracle, change):
    oracle()
    item = {"type": "function_call_output", "call_id": "c1", "output": "done", **change}
    assert await refusal(body(input=[item])) == (QuoteReason.STATEFUL_INPUT, Capability.HISTORY)


async def test_captured_codex_tool_roundtrip_quotes_and_binds_every_byte(oracle):
    """Codex 0.157.0 / worker 5812f3f, captured against a local SSE fixture.

    The fixture emitted only exec_command("printf probe") and opaque reasoning;
    no provider invocation or production credential was used. Keep the native
    tool declarations and second-turn item shapes, without system/user prompts.
    Namespace wire reference: https://developers.openai.com/api/reference/resources/responses/methods/create
    """
    oracle()
    fixture = Path(__file__).parent / "fixtures" / "codex-client-tool-roundtrip.json"
    document = json.loads(fixture.read_text())
    raw = body(**document)
    quoted = await quote(raw)
    assert quoted.total_usd == (await quote()).total_usd
    await revalidate_quote(quoted, raw, RESPONSES_PATH, now=NOW)
    document["input"][-1]["output"] = "changed tool result"
    with pytest.raises(QuoteRefusedError):
        await revalidate_quote(quoted, body(**document), RESPONSES_PATH, now=NOW)


async def test_structured_inline_tool_text_is_bounded(oracle):
    oracle()
    result = await quote(body(input=[{"type": "function_call_output", "call_id": "c1", "output": [{"type": "input_text", "text": "done"}]}]))
    assert result.max_tool_tokens == 0


@pytest.mark.parametrize("metadata", [None, {"file_id": "remote"}, {"turn_id": {}}, {"create_time": True}, {"create_time": "now"}])
async def test_inline_tool_metadata_is_not_an_escape_hatch(oracle, metadata):
    oracle()
    item = {"type": "function_call_output", "call_id": "c1", "output": "done", "internal_chat_message_metadata_passthrough": metadata}
    assert await refusal(body(input=[item])) == (QuoteReason.STATEFUL_INPUT, Capability.HISTORY)


@pytest.mark.parametrize(
    "item",
    [
        {"type": "reasoning", "id": "remote", "summary": []},
        {"type": "function_call", "id": "remote", "call_id": "c1", "name": "f"},
        {"type": "function_call_output", "id": "remote", "call_id": "c1"},
        {"type": "item_reference", "id": "remote"},
    ],
)
async def test_item_labels_without_complete_inline_payload_remain_refused(oracle, item):
    oracle()
    assert await refusal(body(input=[item])) == (QuoteReason.STATEFUL_INPUT, Capability.HISTORY)
