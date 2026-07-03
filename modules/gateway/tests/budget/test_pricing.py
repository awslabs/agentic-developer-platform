"""Unit tests for PricingService cost computation.

Issue #2792: adds the OpenAI (bedrock-mantle) models to MODEL_PRICING so the
``/openai/v1/responses`` passthrough records real cost instead of $0. These
tests pin the known-token → known-cost mapping so a bad pricing edit fails loud.
"""

from decimal import Decimal

from src.budget.pricing import MODEL_PRICING, PricingService, pricing_service


class TestOpenAIPricing:
    """OpenAI-model pricing added for the mantle Responses passthrough."""

    def test_gpt55_entry_present(self):
        assert "openai.gpt-5.5" in MODEL_PRICING
        assert "openai.gpt-oss-120b" in MODEL_PRICING

    def test_gpt55_known_tokens_known_cost(self):
        # AWS Bedrock published rate: $5.50/1M in, $33.00/1M out (per-1000: 0.0055 / 0.033).
        # 1000 input + 1000 output → 0.0055 + 0.033 = 0.0385.
        cost = pricing_service.calculate_cost("openai.gpt-5.5", 1000, 1000)
        assert cost == Decimal("0.0385")

    def test_gpt55_fractional_tokens(self):
        # 500 in / 250 out → 0.5*0.0055 + 0.25*0.033 = 0.00275 + 0.00825 = 0.011.
        cost = pricing_service.calculate_cost("openai.gpt-5.5", 500, 250)
        assert cost == Decimal("0.011")

    def test_gpt_oss_120b_known_tokens_known_cost(self):
        # $0.1545/1M in, $0.6180/1M out (per-1000: 0.0001545 / 0.000618).
        # 10000 in + 10000 out → 10*0.0001545 + 10*0.000618 = 0.001545 + 0.00618 = 0.007725.
        cost = pricing_service.calculate_cost("openai.gpt-oss-120b", 10000, 10000)
        assert cost == Decimal("0.007725")

    def test_zero_tokens_zero_cost(self):
        assert pricing_service.calculate_cost("openai.gpt-5.5", 0, 0) == Decimal("0")

    def test_unknown_openai_model_uses_default(self):
        # A served-but-unpriced openai model falls back to the conservative default,
        # which is > 0 so cost is never silently zero.
        svc = PricingService()
        cost = svc.calculate_cost("openai.some-future-model", 1000, 1000)
        assert cost > Decimal("0")
