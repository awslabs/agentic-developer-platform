"""
Tests for Budget Usage Tracker Lambda Handler.

Issue #234: Budget Usage Tracking Lambda
"""

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

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


from pricing_fallback import (  # noqa: E402
    MODEL_PRICING,
    calculate_cost,
    get_model_pricing,
    resolve_model_id,
)


class TestResolveModelId:
    """Tests for cross-region inference profile model ID resolution."""

    def test_resolve_us_prefix(self):
        """Test resolving us. prefix."""
        model_id = "us.anthropic.claude-3-5-sonnet-20241022-v2:0"
        assert resolve_model_id(model_id) == "anthropic.claude-3-5-sonnet-20241022-v2:0"

    def test_resolve_global_prefix(self):
        """Test resolving global. prefix."""
        model_id = "global.anthropic.claude-sonnet-4-20250514-v1:0"
        assert resolve_model_id(model_id) == "anthropic.claude-sonnet-4-20250514-v1:0"

    def test_resolve_eu_prefix(self):
        """Test resolving eu. prefix."""
        model_id = "eu.anthropic.claude-3-haiku-20240307-v1:0"
        assert resolve_model_id(model_id) == "anthropic.claude-3-haiku-20240307-v1:0"

    def test_resolve_apac_prefix(self):
        """Test resolving apac. prefix."""
        model_id = "apac.anthropic.claude-3-5-haiku-20241022-v1:0"
        assert resolve_model_id(model_id) == "anthropic.claude-3-5-haiku-20241022-v1:0"

    def test_no_prefix(self):
        """Test model ID without cross-region prefix."""
        model_id = "anthropic.claude-3-5-sonnet-20241022-v2:0"
        assert resolve_model_id(model_id) == model_id

    def test_amazon_model(self):
        """Test Amazon model ID."""
        model_id = "amazon.titan-text-express-v1"
        assert resolve_model_id(model_id) == model_id


class TestGetModelPricing:
    """Tests for pricing lookup."""

    def test_known_model(self):
        """Test getting pricing for a known model."""
        pricing = get_model_pricing("anthropic.claude-3-5-sonnet-20241022-v2:0")
        assert pricing["input"] == Decimal("0.003")
        assert pricing["output"] == Decimal("0.015")

    def test_haiku_model(self):
        """Test getting pricing for Haiku model."""
        pricing = get_model_pricing("anthropic.claude-3-5-haiku-20241022-v1:0")
        assert pricing["input"] == Decimal("0.0008")
        assert pricing["output"] == Decimal("0.004")

    def test_unknown_model_returns_default(self):
        """Test that unknown models return default pricing."""
        pricing = get_model_pricing("unknown.model-v1")
        assert pricing == MODEL_PRICING["default"]

    def test_cross_region_model(self):
        """Test that cross-region models are resolved correctly."""
        pricing = get_model_pricing("us.anthropic.claude-3-5-sonnet-20241022-v2:0")
        assert pricing["input"] == Decimal("0.003")
        assert pricing["output"] == Decimal("0.015")


class TestCalculateCost:
    """Tests for cost calculation."""

    def test_calculate_cost_sonnet(self):
        """Test cost calculation for Sonnet model."""
        cost = calculate_cost(
            "anthropic.claude-3-5-sonnet-20241022-v2:0",
            input_tokens=1000,
            output_tokens=500,
        )
        # input: 1000 * 0.003 / 1000 = 0.003
        # output: 500 * 0.015 / 1000 = 0.0075
        # total: 0.0105
        assert cost == Decimal("0.0105")

    def test_calculate_cost_haiku(self):
        """Test cost calculation for Haiku model."""
        cost = calculate_cost(
            "anthropic.claude-3-5-haiku-20241022-v1:0",
            input_tokens=10000,
            output_tokens=2000,
        )
        # input: 10000 * 0.0008 / 1000 = 0.008
        # output: 2000 * 0.004 / 1000 = 0.008
        # total: 0.016
        assert cost == Decimal("0.016")

    def test_calculate_cost_cross_region(self):
        """Test cost calculation with cross-region model ID."""
        cost = calculate_cost(
            "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
            input_tokens=1000,
            output_tokens=500,
        )
        assert cost == Decimal("0.0105")

    def test_calculate_cost_zero_tokens(self):
        """Test cost calculation with zero tokens."""
        cost = calculate_cost(
            "anthropic.claude-3-5-sonnet-20241022-v2:0",
            input_tokens=0,
            output_tokens=0,
        )
        assert cost == Decimal("0")

    def test_calculate_cost_with_custom_pricing_table(self):
        """Test cost calculation with custom pricing table."""
        custom_pricing = {
            "custom-model": {
                "input": Decimal("0.01"),
                "output": Decimal("0.02"),
            }
        }
        cost = calculate_cost(
            "custom-model",
            input_tokens=1000,
            output_tokens=500,
            pricing_table=custom_pricing,
        )
        # input: 1000 * 0.01 / 1000 = 0.01
        # output: 500 * 0.02 / 1000 = 0.01
        # total: 0.02
        assert cost == Decimal("0.02")


class TestPeriodCalculation:
    """Tests for period start date calculation."""

    @pytest.mark.skipif(
        not _has_psycopg2(),
        reason="psycopg2 not installed (Lambda-only dependency)",
    )
    def test_import_handler(self):
        """Test that handler module can be imported."""
        # This tests the handler import works with the shared module path manipulation
        handler_path = Path(__file__).parent.parent.parent / "lambda" / "budget-usage-tracker"
        sys.path.insert(0, str(handler_path))

        # Import the get_period_starts function
        from handler import get_period_starts

        # Test with a known date: Wednesday, February 26, 2026
        timestamp = datetime(2026, 2, 26, 12, 30, 0, tzinfo=UTC)
        periods = get_period_starts(timestamp)

        # Daily: same day
        assert periods["daily"].day == 26
        assert periods["daily"].month == 2
        assert periods["daily"].year == 2026

        # Weekly: Monday of that week (Feb 23, 2026)
        assert periods["weekly"].weekday() == 0  # Monday
        assert periods["weekly"].day == 23
        assert periods["weekly"].month == 2

        # Monthly: first of month
        assert periods["monthly"].day == 1
        assert periods["monthly"].month == 2
        assert periods["monthly"].year == 2026


@pytest.mark.skipif(
    not _has_psycopg2(),
    reason="psycopg2 not installed (Lambda-only dependency)",
)
class TestChatLogParsing:
    """Tests for chat log parsing."""

    def test_parse_valid_chat_log(self):
        """Test parsing a valid chat log."""
        handler_path = Path(__file__).parent.parent.parent / "lambda" / "budget-usage-tracker"
        sys.path.insert(0, str(handler_path))

        from handler import parse_chat_log

        chat_log = {
            "request_id": "test-123",
            "timestamp": "2026-02-26T12:38:54.589534Z",
            "org_id": "acme",
            "user_id": "94c8f418-90d1-701c-e93d-a65df61d91d9",
            "team_id": "platform-team",
            "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "response": {
                "usage": {
                    "input_tokens": 353,
                    "output_tokens": 32,
                }
            },
        }

        result = parse_chat_log(chat_log)

        assert result is not None
        assert result["org_id"] == "acme"
        assert result["user_id"] == "94c8f418-90d1-701c-e93d-a65df61d91d9"
        assert result["team_id"] == "platform-team"
        assert result["model"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        assert result["input_tokens"] == 353
        assert result["output_tokens"] == 32

    def test_parse_chat_log_missing_required_field(self):
        """Test parsing chat log with missing required field."""
        handler_path = Path(__file__).parent.parent.parent / "lambda" / "budget-usage-tracker"
        sys.path.insert(0, str(handler_path))

        from handler import parse_chat_log

        chat_log = {
            "request_id": "test-123",
            "org_id": "acme",
            # Missing user_id and model
            "response": {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                }
            },
        }

        result = parse_chat_log(chat_log)
        assert result is None

    def test_parse_chat_log_missing_usage(self):
        """Test parsing chat log with missing usage data."""
        handler_path = Path(__file__).parent.parent.parent / "lambda" / "budget-usage-tracker"
        sys.path.insert(0, str(handler_path))

        from handler import parse_chat_log

        chat_log = {
            "org_id": "acme",
            "user_id": "user-123",
            "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "response": {},  # No usage
        }

        result = parse_chat_log(chat_log)
        assert result is None

    def test_parse_chat_log_no_team_id(self):
        """Test parsing chat log without team_id."""
        handler_path = Path(__file__).parent.parent.parent / "lambda" / "budget-usage-tracker"
        sys.path.insert(0, str(handler_path))

        from handler import parse_chat_log

        chat_log = {
            "org_id": "acme",
            "user_id": "user-123",
            "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "response": {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                }
            },
        }

        result = parse_chat_log(chat_log)
        assert result is not None
        assert result["team_id"] is None

    def test_parse_chat_log_extracts_request_id(self):
        """Issue #1074: Test that request_id is extracted from chat log."""
        handler_path = Path(__file__).parent.parent.parent / "lambda" / "budget-usage-tracker"
        sys.path.insert(0, str(handler_path))

        from handler import parse_chat_log

        chat_log = {
            "request_id": "abc-123-def",
            "org_id": "acme",
            "user_id": "user-123",
            "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "response": {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                }
            },
        }

        result = parse_chat_log(chat_log)
        assert result is not None
        assert result["request_id"] == "abc-123-def"

    def test_parse_chat_log_missing_request_id_returns_none(self):
        """Issue #1074: request_id is optional, returns None if absent."""
        handler_path = Path(__file__).parent.parent.parent / "lambda" / "budget-usage-tracker"
        sys.path.insert(0, str(handler_path))

        from handler import parse_chat_log

        chat_log = {
            "org_id": "acme",
            "user_id": "user-123",
            "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "response": {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                }
            },
        }

        result = parse_chat_log(chat_log)
        assert result is not None
        assert result["request_id"] is None


@pytest.mark.skipif(
    not _has_psycopg2(),
    reason="psycopg2 not installed (Lambda-only dependency)",
)
class TestBridgeCostToUsageLogs:
    """Issue #1074: Tests for the bridge_cost_to_usage_logs function."""

    def test_bridge_updates_row(self):
        """Test that bridge updates usage_logs when request_id matches."""
        handler_path = Path(__file__).parent.parent.parent / "lambda" / "budget-usage-tracker"
        sys.path.insert(0, str(handler_path))

        from unittest.mock import MagicMock

        from handler import bridge_cost_to_usage_logs

        # Mock connection and cursor
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = bridge_cost_to_usage_logs(mock_conn, "req-123", Decimal("0.0105"))

        assert result is True
        mock_cursor.execute.assert_called_once()
        sql_call = mock_cursor.execute.call_args
        assert "UPDATE usage_logs" in sql_call[0][0]
        assert sql_call[0][1] == (0.0105, "req-123")

    def test_bridge_no_matching_row(self):
        """Test that bridge returns False when no matching row found."""
        handler_path = Path(__file__).parent.parent.parent / "lambda" / "budget-usage-tracker"
        sys.path.insert(0, str(handler_path))

        from unittest.mock import MagicMock

        from handler import bridge_cost_to_usage_logs

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 0
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = bridge_cost_to_usage_logs(mock_conn, "nonexistent-req", Decimal("0.01"))

        assert result is False

    def test_bridge_handles_exception(self):
        """Test that bridge handles exceptions gracefully."""
        handler_path = Path(__file__).parent.parent.parent / "lambda" / "budget-usage-tracker"
        sys.path.insert(0, str(handler_path))

        from unittest.mock import MagicMock

        from handler import bridge_cost_to_usage_logs

        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(side_effect=Exception("DB connection lost"))
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = bridge_cost_to_usage_logs(mock_conn, "req-123", Decimal("0.01"))

        assert result is False
