"""Tests for rate limiting."""

import time
from unittest.mock import MagicMock, patch

from common.rate_limit import (
    DEFAULT_LIMIT_PER_WINDOW,
    RateLimiter,
    RateLimitResult,
)


class TestRateLimitResult:
    def test_allowed_result_headers(self) -> None:
        result = RateLimitResult(
            allowed=True, current_count=5, limit=50, window="2026-05-01T20:00"
        )
        headers = result.headers
        assert headers["X-RateLimit-Limit"] == "50"
        assert headers["X-RateLimit-Remaining"] == "45"
        assert headers["X-RateLimit-Window"] == "2026-05-01T20:00"
        assert "Retry-After" not in headers

    def test_denied_result_headers(self) -> None:
        result = RateLimitResult(
            allowed=False,
            current_count=50,
            limit=50,
            window="2026-05-01T20:00",
            retry_after_seconds=120,
        )
        headers = result.headers
        assert headers["X-RateLimit-Remaining"] == "0"
        assert headers["Retry-After"] == "120"


class TestRateLimiter:
    @patch("common.rate_limit.boto3")
    def test_allowed_first_request(self, mock_boto3: MagicMock) -> None:
        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table
        # get_item returns empty (no existing count)
        mock_table.get_item.return_value = {}
        # query returns empty (no hour history)
        mock_table.query.return_value = {"Items": []}
        # update_item returns new count of 1
        mock_table.update_item.return_value = {"Attributes": {"count": 1}}

        limiter = RateLimiter(table_name="test-rate-limits")
        now = time.time()
        result = limiter.check_and_increment("tenant-1", now=now)

        assert result.allowed is True
        assert result.current_count == 1
        assert result.limit == DEFAULT_LIMIT_PER_WINDOW

    @patch("common.rate_limit.boto3")
    def test_denied_over_window_limit(self, mock_boto3: MagicMock) -> None:
        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table
        # get_item returns count at limit
        mock_table.get_item.return_value = {"Item": {"count": 50}}

        limiter = RateLimiter(table_name="test-rate-limits")
        result = limiter.check_and_increment("tenant-1", now=time.time())

        assert result.allowed is False
        assert result.current_count == 50
        assert result.retry_after_seconds > 0

    @patch("common.rate_limit.boto3")
    def test_denied_over_hour_limit(self, mock_boto3: MagicMock) -> None:
        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table
        # Window is fine
        mock_table.get_item.return_value = {"Item": {"count": 10}}
        # But hour total exceeds limit
        mock_table.query.return_value = {"Items": [{"count": 500}]}

        limiter = RateLimiter(table_name="test-rate-limits", limit_per_hour=500)
        result = limiter.check_and_increment("tenant-1", now=time.time())

        assert result.allowed is False

    @patch("common.rate_limit.boto3")
    def test_custom_limits(self, mock_boto3: MagicMock) -> None:
        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table
        mock_table.get_item.return_value = {"Item": {"count": 10}}

        limiter = RateLimiter(table_name="test-rate-limits", limit_per_window=10)
        result = limiter.check_and_increment("tenant-1", now=time.time())

        assert result.allowed is False
        assert result.limit == 10

    @patch("common.rate_limit.boto3")
    def test_get_status(self, mock_boto3: MagicMock) -> None:
        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table
        mock_table.get_item.return_value = {"Item": {"count": 5}}
        mock_table.query.return_value = {"Items": [{"count": 20}]}

        limiter = RateLimiter(table_name="test-rate-limits")
        status = limiter.get_status("tenant-1")

        assert status["tenant_id"] == "tenant-1"
        assert status["window_count"] == 5
        assert status["hour_count"] == 20
        assert status["window_remaining"] == 45

    @patch("common.rate_limit.boto3")
    def test_ddb_error_fails_open(self, mock_boto3: MagicMock) -> None:
        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table
        # get_item fails
        mock_table.get_item.side_effect = Exception("DDB timeout")
        mock_table.query.return_value = {"Items": []}
        mock_table.update_item.return_value = {"Attributes": {"count": 1}}

        limiter = RateLimiter(table_name="test-rate-limits")
        result = limiter.check_and_increment("tenant-1", now=time.time())

        # Fails open — allows the request
        assert result.allowed is True
