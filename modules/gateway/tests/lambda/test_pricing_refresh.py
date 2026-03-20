"""
Tests for Pricing Refresh Lambda Handler.

Issue #234: Budget Usage Tracking Lambda
"""

import json
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add lambda shared directory to path
lambda_shared_path = Path(__file__).parent.parent.parent / "lambda" / "shared"
sys.path.insert(0, str(lambda_shared_path))


def _has_psycopg2() -> bool:
    """Check if psycopg2 is installed."""
    try:
        import psycopg2  # noqa: F401

        return True
    except ImportError:
        return False


from pricing_fallback import MODEL_PRICING  # noqa: E402


@pytest.mark.skipif(
    not _has_psycopg2(),
    reason="psycopg2 not installed (Lambda-only dependency)",
)
class TestPricingRefreshHandler:
    """Tests for pricing refresh Lambda handler."""

    def test_extract_model_pricing_from_product(self):
        """Test extracting model pricing from AWS Pricing API product data."""
        handler_path = Path(__file__).parent.parent.parent / "lambda" / "pricing-refresh"
        sys.path.insert(0, str(handler_path))

        from handler import extract_model_pricing_from_product

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
        handler_path = Path(__file__).parent.parent.parent / "lambda" / "pricing-refresh"
        sys.path.insert(0, str(handler_path))

        from handler import extract_model_pricing_from_product

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
        handler_path = Path(__file__).parent.parent.parent / "lambda" / "pricing-refresh"
        sys.path.insert(0, str(handler_path))

        from handler import extract_model_pricing_from_product

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

    @patch("handler.get_db_connection")
    @patch("handler.boto3.client")
    def test_handler_with_pricing_api_failure_uses_fallback(self, mock_boto_client, mock_get_db_connection):
        """Test that handler falls back to hardcoded pricing when API fails."""
        handler_path = Path(__file__).parent.parent.parent / "lambda" / "pricing-refresh"
        sys.path.insert(0, str(handler_path))

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

        from handler import handler

        event = {"source": "aws.events"}
        context = MagicMock()

        result = handler(event, context)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["source"] == "fallback"
        assert body["fallback_models_loaded"] > 0
        assert body["api_models_updated"] == 0
