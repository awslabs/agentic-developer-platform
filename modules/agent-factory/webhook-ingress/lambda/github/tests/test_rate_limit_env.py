"""Verify _get_rate_limiter() honors RATE_LIMIT_PER_WINDOW + RATE_LIMIT_PER_HOUR env vars."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

os.environ.setdefault("WEBHOOK_SECRET", "test-secret-123")
os.environ.setdefault("WEBHOOK_SECRET_ARN", "")
os.environ.setdefault("SUBMIT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/1/q.fifo")
os.environ.setdefault("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
os.environ.setdefault("RATE_LIMITS_TABLE", "adp-dev-rate-limits")
os.environ.setdefault("AWS_REGION", "us-east-1")


def _reset_cache():
    import handler

    handler._rate_limiter = None


class TestRateLimiterEnvOverrides:
    def test_defaults_when_env_unset(self):
        import handler
        from common.rate_limit import DEFAULT_LIMIT_PER_HOUR, DEFAULT_LIMIT_PER_WINDOW

        _reset_cache()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RATE_LIMIT_PER_WINDOW", None)
            os.environ.pop("RATE_LIMIT_PER_HOUR", None)
            limiter = handler._get_rate_limiter()
            assert limiter._limit_per_window == DEFAULT_LIMIT_PER_WINDOW
            assert limiter._limit_per_hour == DEFAULT_LIMIT_PER_HOUR

    def test_env_vars_override_defaults(self):
        import handler

        _reset_cache()
        with patch.dict(
            os.environ,
            {"RATE_LIMIT_PER_WINDOW": "5000", "RATE_LIMIT_PER_HOUR": "50000"},
        ):
            limiter = handler._get_rate_limiter()
            assert limiter._limit_per_window == 5000
            assert limiter._limit_per_hour == 50000

    def test_invalid_env_falls_back_to_default(self):
        import handler
        from common.rate_limit import DEFAULT_LIMIT_PER_HOUR, DEFAULT_LIMIT_PER_WINDOW

        _reset_cache()
        with patch.dict(
            os.environ,
            {"RATE_LIMIT_PER_WINDOW": "not-a-number", "RATE_LIMIT_PER_HOUR": "also-not"},
        ):
            limiter = handler._get_rate_limiter()
            assert limiter._limit_per_window == DEFAULT_LIMIT_PER_WINDOW
            assert limiter._limit_per_hour == DEFAULT_LIMIT_PER_HOUR

    def test_empty_env_falls_back_to_default(self):
        import handler
        from common.rate_limit import DEFAULT_LIMIT_PER_HOUR, DEFAULT_LIMIT_PER_WINDOW

        _reset_cache()
        with patch.dict(os.environ, {"RATE_LIMIT_PER_WINDOW": "", "RATE_LIMIT_PER_HOUR": ""}):
            limiter = handler._get_rate_limiter()
            assert limiter._limit_per_window == DEFAULT_LIMIT_PER_WINDOW
            assert limiter._limit_per_hour == DEFAULT_LIMIT_PER_HOUR
