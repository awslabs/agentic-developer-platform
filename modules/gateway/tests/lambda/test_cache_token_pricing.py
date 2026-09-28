"""
Tests for prompt-cache token pricing and model resolution.

Issue #1486: Validates that cache_read_input_tokens and cache_creation_input_tokens
are correctly priced, that Opus 4.6 resolves to Opus pricing (not Sonnet default),
and that unknown models emit a WARNING.
"""

import logging
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from ._handler_loader import load_handler  # noqa: F401

load_handler  # force import side-effect (adds lambda/shared to sys.path)

import pricing_fallback  # noqa: E402
from pricing_fallback import (  # noqa: E402
    MODEL_PRICING,
    calculate_cost,
    get_model_pricing,
    resolve_model_id,
)


@pytest.fixture(autouse=True)
def _isolate_metric_emission(monkeypatch):
    """No test may publish a REAL CloudWatch datapoint.

    Several tests deliberately price unknown ids; with ambient AWS credentials
    an unmocked emit would land a live UnknownModelPricing datapoint and page
    whoever owns the #4592 alarm every time the suite runs. Seeding the module
    client with a mock also resets the per-container dedup set per test.
    """
    monkeypatch.setattr(pricing_fallback, "_cloudwatch_client", MagicMock())
    monkeypatch.setattr(pricing_fallback, "_emitted_unknown_ids", set())


class TestOpus46ModelResolution:
    """Issue #1486: Opus 4.6 must resolve to Opus pricing, not Sonnet default."""

    def test_opus_46_resolves_from_us_prefix(self):
        """us.anthropic.claude-opus-4-6-v1 → anthropic.claude-opus-4-6-v1."""
        resolved = resolve_model_id("us.anthropic.claude-opus-4-6-v1")
        assert resolved == "anthropic.claude-opus-4-6-v1"

    def test_opus_46_has_correct_pricing(self):
        """Opus 4.6 must have Opus rates ($5/M input, $25/M output) — #1622."""
        pricing = get_model_pricing("us.anthropic.claude-opus-4-6-v1")
        assert pricing["input"] == Decimal("0.005")
        assert pricing["output"] == Decimal("0.025")

    def test_opus_46_not_default_pricing(self):
        """Opus 4.6 must NOT fall back to default (Sonnet) pricing."""
        pricing = get_model_pricing("us.anthropic.claude-opus-4-6-v1")
        assert pricing != MODEL_PRICING["default"]

    def test_opus_46_has_cache_rates(self):
        """Opus 4.6 must have explicit cache pricing rates — #1622."""
        pricing = get_model_pricing("us.anthropic.claude-opus-4-6-v1")
        assert "cache_read_input" in pricing
        assert "cache_creation_input" in pricing
        # Cache read = 0.1x input rate ($0.005 × 0.1 = $0.0005)
        assert pricing["cache_read_input"] == Decimal("0.0005")
        # Cache creation = 1.25x input rate ($0.005 × 1.25 = $0.00625)
        assert pricing["cache_creation_input"] == Decimal("0.00625")

    def test_opus_47_resolves_from_us_prefix(self):
        """us.anthropic.claude-opus-4-7-v1 → anthropic.claude-opus-4-7-v1 — #1622."""
        resolved = resolve_model_id("us.anthropic.claude-opus-4-7-v1")
        assert resolved == "anthropic.claude-opus-4-7-v1"

    def test_opus_47_has_correct_pricing(self):
        """Opus 4.7 must have Opus rates ($5/M input, $25/M output) — #1622."""
        pricing = get_model_pricing("us.anthropic.claude-opus-4-7-v1")
        assert pricing["input"] == Decimal("0.005")
        assert pricing["output"] == Decimal("0.025")
        assert pricing["cache_read_input"] == Decimal("0.0005")
        assert pricing["cache_creation_input"] == Decimal("0.00625")

    def test_opus_48_resolves_from_us_prefix(self):
        """us.anthropic.claude-opus-4-8-v1 → anthropic.claude-opus-4-8-v1 — #1622."""
        resolved = resolve_model_id("us.anthropic.claude-opus-4-8-v1")
        assert resolved == "anthropic.claude-opus-4-8-v1"

    def test_opus_48_has_correct_pricing(self):
        """Opus 4.8 must have Opus rates ($5/M input, $25/M output) — #1622."""
        pricing = get_model_pricing("us.anthropic.claude-opus-4-8-v1")
        assert pricing["input"] == Decimal("0.005")
        assert pricing["output"] == Decimal("0.025")
        assert pricing["cache_read_input"] == Decimal("0.0005")
        assert pricing["cache_creation_input"] == Decimal("0.00625")

    def test_opus_47_not_default_pricing(self):
        """Opus 4.7 must NOT fall back to default (Sonnet) pricing — #1622."""
        pricing = get_model_pricing("us.anthropic.claude-opus-4-7-v1")
        assert pricing != MODEL_PRICING["default"]

    def test_opus_48_not_default_pricing(self):
        """Opus 4.8 must NOT fall back to default (Sonnet) pricing — #1622."""
        pricing = get_model_pricing("us.anthropic.claude-opus-4-8-v1")
        assert pricing != MODEL_PRICING["default"]

    def test_sonnet_46_resolves_correctly(self):
        """Sonnet 4.6 must resolve and have correct pricing."""
        pricing = get_model_pricing("us.anthropic.claude-sonnet-4-6-v1")
        assert pricing["input"] == Decimal("0.003")
        assert pricing["output"] == Decimal("0.015")

    def test_haiku_45_resolves_correctly(self):
        """Haiku 4.5 must resolve and have correct pricing."""
        pricing = get_model_pricing("us.anthropic.claude-haiku-4-5-20251001-v1:0")
        assert pricing["input"] == Decimal("0.0008")
        assert pricing["output"] == Decimal("0.004")


class TestIssue4592MissingModelIds:
    """Issue #4592: three live model ids fell through to `default` pricing.

    `default` has no cache rates at all, and agent traffic is cache-dominated,
    so these misses mispriced the majority of every affected record's tokens.
    """

    # (model id, input, output, cache_read, cache_creation) per 1000 tokens.
    # Opus 5 is $5/$25 per MTok, verified against the published price list —
    # deliberately NOT copied from the Opus 4.6-4.8 rows.
    CASES = [
        ("anthropic.claude-opus-5", "0.005", "0.025", "0.0005", "0.00625"),
        (
            "anthropic.claude-sonnet-4-5-20250929-v1:0",
            "0.003",
            "0.015",
            "0.0003",
            "0.00375",
        ),
        ("anthropic.claude-sonnet-4-6", "0.003", "0.015", "0.0003", "0.00375"),
    ]

    def test_new_ids_have_exact_four_key_pricing(self):
        """Each newly added id prices at its published rate on all four keys."""
        for model_id, inp, out, c_read, c_create in self.CASES:
            pricing = get_model_pricing(model_id)
            assert pricing["input"] == Decimal(inp), model_id
            assert pricing["output"] == Decimal(out), model_id
            assert pricing["cache_read_input"] == Decimal(c_read), model_id
            assert pricing["cache_creation_input"] == Decimal(c_create), model_id

    def test_new_ids_not_default_pricing(self):
        """None of the new ids may resolve to the default fallback row."""
        for model_id, *_ in self.CASES:
            assert get_model_pricing(model_id) != MODEL_PRICING["default"], model_id

    def test_new_ids_do_not_warn(self, caplog):
        """A hit must not emit the unknown-model WARNING (nor the CW metric)."""
        for model_id, *_ in self.CASES:
            caplog.clear()
            with caplog.at_level(logging.WARNING, logger="pricing_fallback"):
                get_model_pricing(model_id)
            assert not any("Unknown model" in r.message for r in caplog.records), model_id

    def test_new_ids_resolve_through_cross_region_prefixes(self):
        """us./global. inference-profile prefixes must still land on the entry."""
        for model_id, inp, *_ in self.CASES:
            for prefix in ("us.", "global."):
                pricing = get_model_pricing(f"{prefix}{model_id}")
                assert pricing["input"] == Decimal(inp), f"{prefix}{model_id}"

    def test_bare_sonnet_46_matches_v1_suffixed_entry(self):
        """The bare id resolves via the suffix-variant retry to the -v1 entry."""
        assert get_model_pricing("anthropic.claude-sonnet-4-6") == get_model_pricing("anthropic.claude-sonnet-4-6-v1")

    def test_bare_opus_47_and_48_price_at_opus_rates(self):
        """The 'opus47'/'opus48' /model aliases emit bare ids (no -v1 suffix).

        These were the same #4592 miss class one family over: the table keys
        only the -v1 forms, so bare ids billed at the Sonnet-tier default.
        """
        for bare in (
            "anthropic.claude-opus-4-7",
            "anthropic.claude-opus-4-8",
            "global.anthropic.claude-opus-4-8",
        ):
            assert get_model_pricing(bare)["output"] == Decimal("0.025"), bare

    def test_dated_opus_45_prices_at_opus_rates(self):
        """The 'opus45' /model alias emits a dated id that had NO row at all."""
        pricing = get_model_pricing("global.anthropic.claude-opus-4-5-20251101-v1:0")
        assert pricing["input"] == Decimal("0.005")
        assert pricing["output"] == Decimal("0.025")

    def test_v1_suffixed_opus_5_form_resolves(self):
        """A '-v1'-suffixed Opus 5 id (the 4.x naming convention) still hits."""
        assert get_model_pricing("us.anthropic.claude-opus-5-v1")["output"] == Decimal("0.025")

    def test_opus_5_cache_heavy_turn_costed_correctly(self):
        """Hand-computed cache-dominated Opus 5 agent turn — the #4592 shape.

        65K cached input, 1 fresh input token, 4K output. This is where the
        default row did the most damage: it has no cache rates, so the 65K
        cache-read tokens were derived off the wrong $3 base rate.
        """
        cost = calculate_cost(
            "anthropic.claude-opus-5",
            input_tokens=1,
            output_tokens=4000,
            cache_read_input_tokens=65000,
        )
        # input:      1 * 0.005    / 1000 = 0.000005
        # output:  4000 * 0.025    / 1000 = 0.1
        # cache_r: 65000 * 0.0005  / 1000 = 0.0325
        assert cost == Decimal("0.132505")

    def test_opus_5_differs_from_default_rate_pricing(self):
        """Opus 5 must cost more than the default row charged for the same turn.

        Proves the fix actually changes the number, not just the table: the
        default row underpriced Opus 5 output at $15 instead of $25 per MTok.
        """
        default_priced = calculate_cost(
            "unknown.model-not-in-table",
            input_tokens=1,
            output_tokens=4000,
            cache_read_input_tokens=65000,
        )
        opus_5_priced = calculate_cost(
            "anthropic.claude-opus-5",
            input_tokens=1,
            output_tokens=4000,
            cache_read_input_tokens=65000,
        )
        assert opus_5_priced > default_priced

    def test_all_claude_4x_and_5_entries_carry_cache_rates(self):
        """Guard: no modern Claude entry may ship base rates only.

        A base-rate-only row is the bug half-fixed — cache tokens silently fall
        back to derived rates. Fails loudly if a future entry omits them.
        """
        modern = [
            k for k in MODEL_PRICING if k.startswith("anthropic.claude-") and any(marker in k for marker in ("-4-5", "-4-6", "-4-7", "-4-8", "-5"))
        ]
        assert modern, "expected to find modern Claude entries"
        for key in modern:
            assert "cache_read_input" in MODEL_PRICING[key], key
            assert "cache_creation_input" in MODEL_PRICING[key], key


class TestUnknownModelWarning:
    """Issue #1486: Unknown models must emit a WARNING (not silently default)."""

    def test_unknown_model_returns_default(self):
        """Unknown models still return default pricing (graceful degradation)."""
        pricing = get_model_pricing("unknown.future-model-v1")
        assert pricing["input"] == MODEL_PRICING["default"]["input"]
        assert pricing["output"] == MODEL_PRICING["default"]["output"]

    def test_unknown_model_logs_warning(self, caplog):
        """Unknown model must emit a WARNING log."""
        with caplog.at_level(logging.WARNING, logger="pricing_fallback"):
            get_model_pricing("unknown.future-model-v1")

        assert any("Unknown model" in record.message for record in caplog.records)
        assert any("unknown.future-model-v1" in record.message for record in caplog.records)

    def test_unknown_model_emits_cloudwatch_metric(self):
        """Unknown model should attempt to emit UnknownModelPricing metric.

        The datapoint is deliberately DIMENSIONLESS: alarms cannot be built on
        SEARCH() expressions, so a ModelId dimension would make the metric
        un-alarmable (and mint one paid custom metric per garbage id). The id
        itself travels in the WARNING log.
        """
        get_model_pricing("unknown.future-model-v1")

        mock_cw = pricing_fallback._cloudwatch_client
        mock_cw.put_metric_data.assert_called_once()
        call_kwargs = mock_cw.put_metric_data.call_args[1]
        assert call_kwargs["Namespace"] == "ADP/Gateway"
        metric = call_kwargs["MetricData"][0]
        assert metric["MetricName"] == "UnknownModelPricing"
        assert "Dimensions" not in metric

    def test_unknown_model_metric_deduped_per_id(self):
        """Repeated misses for one id emit ONE datapoint; a new id emits again.

        Bounds the hot-path cost when an unknown model floods a batch — the
        alarm threshold is >0, so one datapoint carries the same signal.
        """
        for _ in range(3):
            get_model_pricing("unknown.future-model-v1")
        assert pricing_fallback._cloudwatch_client.put_metric_data.call_count == 1

        get_model_pricing("unknown.other-model-v9")
        assert pricing_fallback._cloudwatch_client.put_metric_data.call_count == 2

    def test_unknown_model_metric_failure_is_logged_not_raised(self, caplog):
        """A publish failure must not fail pricing — but must leave a trace.

        The silent except-pass variant is how the pre-#4592 metric died
        unnoticed for weeks; the failure now logs, and the id stays un-deduped
        so the next record retries.
        """
        pricing_fallback._cloudwatch_client.put_metric_data.side_effect = RuntimeError("cw down")
        with caplog.at_level(logging.WARNING, logger="pricing_fallback"):
            pricing = get_model_pricing("unknown.future-model-v1")
        assert pricing == MODEL_PRICING["default"]
        assert any("metric publish failed" in r.message for r in caplog.records)
        assert "unknown.future-model-v1" not in pricing_fallback._emitted_unknown_ids

    def test_known_model_does_not_warn(self, caplog):
        """Known models must NOT emit a warning."""
        with caplog.at_level(logging.WARNING, logger="pricing_fallback"):
            get_model_pricing("us.anthropic.claude-opus-4-6-v1")

        assert not any("Unknown model" in record.message for record in caplog.records)


class TestCalculateCostWithCacheTokens:
    """Issue #1486: calculate_cost must include cache token cost terms."""

    def test_calculate_cost_no_cache_tokens_unchanged(self):
        """Without cache tokens, behavior is unchanged from before."""
        cost = calculate_cost(
            "anthropic.claude-3-5-sonnet-20241022-v2:0",
            input_tokens=1000,
            output_tokens=500,
        )
        # input: 1000 * 0.003 / 1000 = 0.003
        # output: 500 * 0.015 / 1000 = 0.0075
        # total: 0.0105
        assert cost == Decimal("0.0105")

    def test_calculate_cost_with_cache_read_tokens(self):
        """Cache-read tokens priced at ~0.1x input rate — #1622 corrected."""
        cost = calculate_cost(
            "anthropic.claude-opus-4-6-v1",
            input_tokens=1,
            output_tokens=4000,
            cache_read_input_tokens=65000,
        )
        # input: 1 * 0.005 / 1000 = 0.000005
        # output: 4000 * 0.025 / 1000 = 0.1
        # cache_read: 65000 * 0.0005 / 1000 = 0.0325
        # total: 0.132505
        expected = Decimal("0.000005") + Decimal("0.1") + Decimal("0.0325")
        assert cost == round(expected, 6)

    def test_calculate_cost_with_cache_creation_tokens(self):
        """Cache-creation tokens priced at ~1.25x input rate — #1622 corrected."""
        cost = calculate_cost(
            "anthropic.claude-opus-4-6-v1",
            input_tokens=0,
            output_tokens=0,
            cache_creation_input_tokens=10000,
        )
        # cache_creation: 10000 * 0.00625 / 1000 = 0.0625
        assert cost == Decimal("0.0625")

    def test_calculate_cost_opus_46_hand_computed(self):
        """Hand-computed Opus 4.6 cost with typical cached agent request — #1622 corrected.

        Scenario: agent loop turn with ~65K cached input, 1 non-cached token,
        4K output tokens. This is the exact scenario from the issue evidence.
        At $5/$25 per MTok this should be ~1/3 of the pre-fix ($15/$75) figure.
        """
        cost = calculate_cost(
            "us.anthropic.claude-opus-4-6-v1",
            input_tokens=1,
            output_tokens=4000,
            cache_read_input_tokens=65000,
            cache_creation_input_tokens=0,
        )
        # input: 1 * 0.005 / 1000 = 0.000005
        # output: 4000 * 0.025 / 1000 = 0.1
        # cache_read: 65000 * 0.0005 / 1000 = 0.0325
        # cache_creation: 0
        # total: 0.132505
        assert cost == Decimal("0.132505")
        # Verify this is ~1/3 of the old ($15/$75) figure ($0.397515)
        old_rate_cost = Decimal("0.397515")
        assert cost < old_rate_cost / Decimal("2")  # strictly less than half

    def test_calculate_cost_with_both_cache_types(self):
        """Full scenario with both cache-read and cache-creation — #1622 corrected."""
        cost = calculate_cost(
            "anthropic.claude-opus-4-6-v1",
            input_tokens=100,
            output_tokens=2000,
            cache_read_input_tokens=50000,
            cache_creation_input_tokens=5000,
        )
        # input: 100 * 0.005 / 1000 = 0.0005
        # output: 2000 * 0.025 / 1000 = 0.05
        # cache_read: 50000 * 0.0005 / 1000 = 0.025
        # cache_creation: 5000 * 0.00625 / 1000 = 0.03125
        # total: 0.10675
        expected = Decimal("0.0005") + Decimal("0.05") + Decimal("0.025") + Decimal("0.03125")
        assert cost == round(expected, 6)

    def test_calculate_cost_cache_defaults_to_zero(self):
        """Existing callers without cache params still work (backward compatible)."""
        cost_without = calculate_cost(
            "anthropic.claude-3-5-sonnet-20241022-v2:0",
            input_tokens=1000,
            output_tokens=500,
        )
        cost_with_zeros = calculate_cost(
            "anthropic.claude-3-5-sonnet-20241022-v2:0",
            input_tokens=1000,
            output_tokens=500,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        assert cost_without == cost_with_zeros

    def test_calculate_cost_custom_pricing_table_with_cache(self):
        """Custom pricing table can include cache rates."""
        custom_pricing = {
            "custom-model": {
                "input": Decimal("0.01"),
                "output": Decimal("0.02"),
                "cache_read_input": Decimal("0.001"),
                "cache_creation_input": Decimal("0.0125"),
            }
        }
        cost = calculate_cost(
            "custom-model",
            input_tokens=1000,
            output_tokens=500,
            pricing_table=custom_pricing,
            cache_read_input_tokens=10000,
            cache_creation_input_tokens=2000,
        )
        # input: 1000 * 0.01 / 1000 = 0.01
        # output: 500 * 0.02 / 1000 = 0.01
        # cache_read: 10000 * 0.001 / 1000 = 0.01
        # cache_creation: 2000 * 0.0125 / 1000 = 0.025
        # total: 0.055
        assert cost == Decimal("0.055")

    def test_calculate_cost_custom_table_derives_cache_rate_if_missing(self):
        """When custom table lacks cache rates, derive from input rate."""
        custom_pricing = {
            "custom-model": {
                "input": Decimal("0.01"),
                "output": Decimal("0.02"),
                # No cache_read_input / cache_creation_input
            }
        }
        cost = calculate_cost(
            "custom-model",
            input_tokens=0,
            output_tokens=0,
            pricing_table=custom_pricing,
            cache_read_input_tokens=10000,
            cache_creation_input_tokens=0,
        )
        # cache_read: 10000 * (0.01 * 0.1) / 1000 = 10000 * 0.001 / 1000 = 0.01
        assert cost == Decimal("0.01")
