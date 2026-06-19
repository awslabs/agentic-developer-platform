"""
Tests for prompt-cache token pricing and model resolution.

Issue #1486: Validates that cache_read_input_tokens and cache_creation_input_tokens
are correctly priced, that Opus 4.6 resolves to Opus pricing (not Sonnet default),
and that unknown models emit a WARNING.
"""

import logging
from decimal import Decimal
from unittest.mock import patch

from ._handler_loader import load_handler  # noqa: F401

load_handler  # force import side-effect (adds lambda/shared to sys.path)

from pricing_fallback import (  # noqa: E402
    MODEL_PRICING,
    calculate_cost,
    get_model_pricing,
    resolve_model_id,
)


class TestOpus46ModelResolution:
    """Issue #1486: Opus 4.6 must resolve to Opus pricing, not Sonnet default."""

    def test_opus_46_resolves_from_us_prefix(self):
        """us.anthropic.claude-opus-4-6-v1 → anthropic.claude-opus-4-6-v1."""
        resolved = resolve_model_id("us.anthropic.claude-opus-4-6-v1")
        assert resolved == "anthropic.claude-opus-4-6-v1"

    def test_opus_46_has_correct_pricing(self):
        """Opus 4.6 must have Opus rates ($15/M input, $75/M output)."""
        pricing = get_model_pricing("us.anthropic.claude-opus-4-6-v1")
        assert pricing["input"] == Decimal("0.015")
        assert pricing["output"] == Decimal("0.075")

    def test_opus_46_not_default_pricing(self):
        """Opus 4.6 must NOT fall back to default (Sonnet) pricing."""
        pricing = get_model_pricing("us.anthropic.claude-opus-4-6-v1")
        assert pricing != MODEL_PRICING["default"]

    def test_opus_46_has_cache_rates(self):
        """Opus 4.6 must have explicit cache pricing rates."""
        pricing = get_model_pricing("us.anthropic.claude-opus-4-6-v1")
        assert "cache_read_input" in pricing
        assert "cache_creation_input" in pricing
        # Cache read = 0.1x input rate
        assert pricing["cache_read_input"] == Decimal("0.0015")
        # Cache creation = 1.25x input rate
        assert pricing["cache_creation_input"] == Decimal("0.01875")

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
        """Unknown model should attempt to emit UnknownModelPricing metric."""
        import boto3

        with patch.object(boto3, "client") as mock_client_factory:
            # boto3 is imported inside get_model_pricing via `import boto3`
            # Patch the actual boto3 module's client method
            mock_cw = mock_client_factory.return_value
            get_model_pricing("unknown.future-model-v1")

            mock_client_factory.assert_called_with("cloudwatch")
            mock_cw.put_metric_data.assert_called_once()
            call_kwargs = mock_cw.put_metric_data.call_args[1]
            assert call_kwargs["Namespace"] == "ADP/Gateway"
            metric = call_kwargs["MetricData"][0]
            assert metric["MetricName"] == "UnknownModelPricing"
            assert metric["Dimensions"][0]["Value"] == "unknown.future-model-v1"

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
        """Cache-read tokens priced at ~0.1x input rate."""
        cost = calculate_cost(
            "anthropic.claude-opus-4-6-v1",
            input_tokens=1,
            output_tokens=4000,
            cache_read_input_tokens=65000,
        )
        # input: 1 * 0.015 / 1000 = 0.000015
        # output: 4000 * 0.075 / 1000 = 0.3
        # cache_read: 65000 * 0.0015 / 1000 = 0.0975
        # total: 0.397515
        expected = Decimal("0.000015") + Decimal("0.3") + Decimal("0.0975")
        assert cost == round(expected, 6)

    def test_calculate_cost_with_cache_creation_tokens(self):
        """Cache-creation tokens priced at ~1.25x input rate."""
        cost = calculate_cost(
            "anthropic.claude-opus-4-6-v1",
            input_tokens=0,
            output_tokens=0,
            cache_creation_input_tokens=10000,
        )
        # cache_creation: 10000 * 0.01875 / 1000 = 0.1875
        assert cost == Decimal("0.1875")

    def test_calculate_cost_opus_46_hand_computed(self):
        """Hand-computed Opus 4.6 cost with typical cached agent request.

        Scenario: agent loop turn with ~65K cached input, 1 non-cached token,
        4K output tokens. This is the exact scenario from the issue evidence.
        """
        cost = calculate_cost(
            "us.anthropic.claude-opus-4-6-v1",
            input_tokens=1,
            output_tokens=4000,
            cache_read_input_tokens=65000,
            cache_creation_input_tokens=0,
        )
        # input: 1 * 0.015 / 1000 = 0.000015
        # output: 4000 * 0.075 / 1000 = 0.3
        # cache_read: 65000 * 0.0015 / 1000 = 0.0975
        # cache_creation: 0
        # total: 0.397515
        assert cost == Decimal("0.397515")

    def test_calculate_cost_with_both_cache_types(self):
        """Full scenario with both cache-read and cache-creation."""
        cost = calculate_cost(
            "anthropic.claude-opus-4-6-v1",
            input_tokens=100,
            output_tokens=2000,
            cache_read_input_tokens=50000,
            cache_creation_input_tokens=5000,
        )
        # input: 100 * 0.015 / 1000 = 0.0015
        # output: 2000 * 0.075 / 1000 = 0.15
        # cache_read: 50000 * 0.0015 / 1000 = 0.075
        # cache_creation: 5000 * 0.01875 / 1000 = 0.09375
        # total: 0.32025
        expected = Decimal("0.0015") + Decimal("0.15") + Decimal("0.075") + Decimal("0.09375")
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
