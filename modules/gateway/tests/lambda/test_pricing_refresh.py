"""
Tests for Pricing Refresh Lambda Handler.

Issue #234: Budget Usage Tracking Lambda
"""

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from ._handler_loader import handler_module_name, load_handler

# Unique module name for the pricing-refresh handler — used both to load it and
# as the patch target, so it never collides with other lambdas' ``handler``.
_PRICING_HANDLER = handler_module_name("pricing-refresh")


def _has_psycopg2() -> bool:
    """Check if psycopg2 is installed."""
    try:
        import psycopg2  # noqa: F401

        return True
    except ImportError:
        return False


# Register the handler module under its unique name at import time when its
# deps are available, so the class-level @patch(f"{_PRICING_HANDLER}.…")
# decorators can resolve their target (patch imports the target before the
# test body runs). Skipped when psycopg2 is absent — those tests are skipped
# too, so the module is never needed.
if _has_psycopg2():
    load_handler("pricing-refresh")


from pricing_fallback import MODEL_PRICING  # noqa: E402


@pytest.mark.skipif(
    not _has_psycopg2(),
    reason="psycopg2 not installed (Lambda-only dependency)",
)
class TestPricingRefreshHandler:
    """Tests for pricing refresh Lambda handler."""

    def test_extract_model_pricing_from_product(self):
        """Test extracting model pricing from AWS Pricing API product data."""
        extract_model_pricing_from_product = load_handler("pricing-refresh").extract_model_pricing_from_product

        # Sample product data structure from AWS Pricing API
        product = {
            "product": {
                "attributes": {
                    "modelId": "anthropic.claude-3-5-sonnet-20241022-v2:0",
                }
            },
            "terms": {
                "OnDemand": {
                    "term1": {
                        "priceDimensions": {
                            "dim1": {
                                "description": "Input token price",
                                "unit": "1000 tokens",
                                "pricePerUnit": {"USD": "0.003"},
                            },
                            "dim2": {
                                "description": "Output token price",
                                "unit": "1000 tokens",
                                "pricePerUnit": {"USD": "0.015"},
                            },
                        }
                    }
                }
            },
        }

        result = extract_model_pricing_from_product(product)

        assert result is not None
        assert result["model_id"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"
        assert result["input_price"] == Decimal("0.003")
        assert result["output_price"] == Decimal("0.015")

    def test_extract_model_pricing_missing_model_id(self):
        """Test extracting pricing when model ID is missing."""
        extract_model_pricing_from_product = load_handler("pricing-refresh").extract_model_pricing_from_product

        product = {
            "product": {
                "attributes": {}  # No modelId
            },
            "terms": {},
        }

        result = extract_model_pricing_from_product(product)
        assert result is None

    def test_extract_model_pricing_missing_on_demand(self):
        """Test extracting pricing when OnDemand terms are missing."""
        extract_model_pricing_from_product = load_handler("pricing-refresh").extract_model_pricing_from_product

        product = {
            "product": {
                "attributes": {
                    "modelId": "anthropic.claude-3-5-sonnet-20241022-v2:0",
                }
            },
            "terms": {},  # No OnDemand
        }

        result = extract_model_pricing_from_product(product)
        assert result is None


class TestFallbackPricing:
    """Tests for fallback pricing logic."""

    def test_fallback_pricing_has_common_models(self):
        """Test that fallback pricing includes common models."""
        # Claude 3.5 Sonnet
        assert "anthropic.claude-3-5-sonnet-20241022-v2:0" in MODEL_PRICING
        # Claude 3.5 Haiku
        assert "anthropic.claude-3-5-haiku-20241022-v1:0" in MODEL_PRICING
        # Claude 3 Haiku
        assert "anthropic.claude-3-haiku-20240307-v1:0" in MODEL_PRICING
        # Amazon Titan
        assert "amazon.titan-text-express-v1" in MODEL_PRICING
        # Default fallback
        assert "default" in MODEL_PRICING

    def test_fallback_pricing_has_valid_structure(self):
        """Test that all pricing entries have valid structure."""
        for model_id, pricing in MODEL_PRICING.items():
            assert "input" in pricing, f"Missing 'input' for {model_id}"
            assert "output" in pricing, f"Missing 'output' for {model_id}"
            assert isinstance(pricing["input"], Decimal), f"'input' not Decimal for {model_id}"
            assert isinstance(pricing["output"], Decimal), f"'output' not Decimal for {model_id}"
            assert pricing["input"] >= 0, f"Negative input price for {model_id}"
            assert pricing["output"] >= 0, f"Negative output price for {model_id}"

    def test_default_pricing_is_reasonable(self):
        """Test that default pricing is a reasonable conservative estimate."""
        default = MODEL_PRICING["default"]
        # Default should be similar to Claude 3.5 Sonnet (a middle-tier model)
        assert default["input"] == Decimal("0.003")
        assert default["output"] == Decimal("0.015")


@pytest.mark.skipif(
    not _has_psycopg2(),
    reason="psycopg2 not installed (Lambda-only dependency)",
)
class TestHandlerIntegration:
    """Integration tests for the handler function."""

    @patch(f"{_PRICING_HANDLER}.get_db_connection")
    @patch(f"{_PRICING_HANDLER}.boto3.client")
    def test_handler_with_pricing_api_failure_uses_fallback(self, mock_boto_client, mock_get_db_connection):
        """Test that handler falls back to hardcoded pricing when API fails."""
        # Mock pricing client to raise exception
        mock_pricing_client = MagicMock()
        mock_pricing_client.get_paginator.return_value.paginate.side_effect = Exception("Pricing API throttled")
        mock_boto_client.return_value = mock_pricing_client

        # Mock database connection
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_db_connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_get_db_connection.return_value.__exit__ = MagicMock(return_value=False)

        handler = load_handler("pricing-refresh").handler

        event = {"source": "aws.events"}
        context = MagicMock()

        result = handler(event, context)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["source"] == "fallback"
        assert body["fallback_models_loaded"] > 0
        assert body["api_models_updated"] == 0
