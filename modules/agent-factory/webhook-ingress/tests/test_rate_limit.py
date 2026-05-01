"""Unit tests for per-tenant rate limiting."""

from __future__ import annotations

import time

import boto3
from moto import mock_aws

from common.rate_limit import (
    DEFAULT_LIMIT_PER_HOUR,
    DEFAULT_LIMIT_PER_WINDOW,
    WINDOW_SIZE_SECONDS,
    RateLimiter,
    RateLimitResult,
)


class TestRateLimitResult:
    """Tests for RateLimitResult dataclass."""

    def test_allowed_result_headers(self):
        result = RateLimitResult(allowed=True, current_count=5, limit=50, window="2026-05-01T20:00")
        headers = result.headers
        assert headers["X-RateLimit-Limit"] == "50"
        assert headers["X-RateLimit-Remaining"] == "45"
        assert headers["X-RateLimit-Window"] == "2026-05-01T20:00"
        assert "Retry-After" not in headers

    def test_denied_result_headers(self):
        result = RateLimitResult(
            allowed=False,
            current_count=50,
            limit=50,
            window="2026-05-01T20:00",
            retry_after_seconds=120,
        )
        headers = result.headers
        assert headers["X-RateLimit-Limit"] == "50"
        assert headers["X-RateLimit-Remaining"] == "0"
        assert headers["Retry-After"] == "120"


class TestRateLimiter:
    """Tests for RateLimiter DDB operations."""

    @mock_aws
    def test_first_request_allowed(self, aws_credentials):
        """First request for a tenant is always allowed."""
        self._create_table()
        limiter = RateLimiter(table_name="adp-dev-rate-limits")

        result = limiter.check_and_increment("new-tenant")

        assert result.allowed is True
        assert result.current_count == 1
        assert result.limit == DEFAULT_LIMIT_PER_WINDOW

    @mock_aws
    def test_increment_is_atomic(self, aws_credentials):
        """Multiple increments produce sequential counts."""
        self._create_table()
        limiter = RateLimiter(table_name="adp-dev-rate-limits")
        now = time.time()

        r1 = limiter.check_and_increment("tenant-a", now=now)
        r2 = limiter.check_and_increment("tenant-a", now=now)
        r3 = limiter.check_and_increment("tenant-a", now=now)

        assert r1.current_count == 1
        assert r2.current_count == 2
        assert r3.current_count == 3

    @mock_aws
    def test_window_limit_enforced(self, aws_credentials):
        """Requests are blocked once the per-window limit is reached."""
        self._create_table()
        limiter = RateLimiter(table_name="adp-dev-rate-limits", limit_per_window=3)
        now = time.time()

        # First 3 requests pass
        for _ in range(3):
            result = limiter.check_and_increment("spammy", now=now)
            assert result.allowed is True

        # 4th request is blocked
        result = limiter.check_and_increment("spammy", now=now)
        assert result.allowed is False
        assert result.retry_after_seconds > 0
        assert result.retry_after_seconds <= WINDOW_SIZE_SECONDS

    @mock_aws
    def test_hourly_limit_enforced(self, aws_credentials):
        """Requests are blocked once the per-hour limit is reached."""
        self._create_table()
        # Set window limit high but hourly limit low
        limiter = RateLimiter(
            table_name="adp-dev-rate-limits",
            limit_per_window=100,
            limit_per_hour=5,
        )

        # Spread requests across different windows within the hour
        base_time = 1714600800.0  # Fixed base time
        for i in range(5):
            # Each request in the same window (easier for test)
            result = limiter.check_and_increment("hourly-tenant", now=base_time)
            assert result.allowed is True

        # 6th request should be blocked by hourly limit
        result = limiter.check_and_increment("hourly-tenant", now=base_time)
        assert result.allowed is False

    @mock_aws
    def test_different_tenants_independent(self, aws_credentials):
        """Rate limits are per-tenant — one tenant doesn't affect another."""
        self._create_table()
        limiter = RateLimiter(table_name="adp-dev-rate-limits", limit_per_window=2)
        now = time.time()

        # Exhaust tenant-a's limit
        limiter.check_and_increment("tenant-a", now=now)
        limiter.check_and_increment("tenant-a", now=now)
        result_a = limiter.check_and_increment("tenant-a", now=now)
        assert result_a.allowed is False

        # tenant-b is unaffected
        result_b = limiter.check_and_increment("tenant-b", now=now)
        assert result_b.allowed is True

    @mock_aws
    def test_new_window_resets_count(self, aws_credentials):
        """Moving to a new 5-min window resets the per-window count."""
        self._create_table()
        limiter = RateLimiter(table_name="adp-dev-rate-limits", limit_per_window=2)

        # Window 1: exhaust limit
        window1_time = 1714600800.0  # Aligned to 5-min boundary
        limiter.check_and_increment("tenant-x", now=window1_time)
        limiter.check_and_increment("tenant-x", now=window1_time)
        result = limiter.check_and_increment("tenant-x", now=window1_time)
        assert result.allowed is False

        # Window 2: new window, fresh quota
        window2_time = window1_time + WINDOW_SIZE_SECONDS
        result = limiter.check_and_increment("tenant-x", now=window2_time)
        assert result.allowed is True
        assert result.current_count == 1

    @mock_aws
    def test_custom_per_tenant_limits(self, aws_credentials):
        """Per-tenant limit overrides work via check_and_increment kwargs."""
        self._create_table()
        limiter = RateLimiter(table_name="adp-dev-rate-limits")
        now = time.time()

        # Enterprise tenant with higher limit
        result = limiter.check_and_increment("enterprise-corp", limit_per_window=1000, now=now)
        assert result.allowed is True
        assert result.limit == 1000

    @mock_aws
    def test_get_status(self, aws_credentials):
        """get_status returns diagnostic information."""
        self._create_table()
        limiter = RateLimiter(table_name="adp-dev-rate-limits")
        now = time.time()

        limiter.check_and_increment("status-tenant", now=now)
        limiter.check_and_increment("status-tenant", now=now)

        status = limiter.get_status("status-tenant", now=now)

        assert status["tenant_id"] == "status-tenant"
        assert status["window_count"] == 2
        assert status["window_limit"] == DEFAULT_LIMIT_PER_WINDOW
        assert status["window_remaining"] == DEFAULT_LIMIT_PER_WINDOW - 2
        assert status["hour_count"] == 2
        assert status["hour_limit"] == DEFAULT_LIMIT_PER_HOUR

    @mock_aws
    def test_window_key_format(self, aws_credentials):
        """Window keys are formatted as YYYY-MM-DDTHH:MM with 5-min alignment."""
        self._create_table()
        limiter = RateLimiter(table_name="adp-dev-rate-limits")

        # 2026-05-01T20:03:42 should round down to 2026-05-01T20:00
        import calendar
        from datetime import datetime, timezone

        dt = datetime(2026, 5, 1, 20, 3, 42, tzinfo=timezone.utc)
        ts = calendar.timegm(dt.timetuple())

        window = limiter._current_window(ts)
        assert window == "2026-05-01T20:00"

        # 2026-05-01T20:07:00 should round to 2026-05-01T20:05
        dt2 = datetime(2026, 5, 1, 20, 7, 0, tzinfo=timezone.utc)
        ts2 = calendar.timegm(dt2.timetuple())
        window2 = limiter._current_window(ts2)
        assert window2 == "2026-05-01T20:05"

    def _create_table(self):
        """Helper to create the rate-limits table in moto."""
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="adp-dev-rate-limits",
            KeySchema=[
                {"AttributeName": "tenant_id", "KeyType": "HASH"},
                {"AttributeName": "window", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "tenant_id", "AttributeType": "S"},
                {"AttributeName": "window", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
