"""
Tests for prompt-cache token capture in chat logging.

Issue #1486: Validates that cache_read_input_tokens and cache_creation_input_tokens
are preserved in the ChatLog/UsageInfo written to S3.
"""

from src.chat_logging.schemas import UsageInfo


class TestUsageInfoCacheFields:
    """UsageInfo schema must carry cache token fields."""

    def test_cache_fields_default_to_zero(self):
        """Cache fields default to 0 for non-cached requests."""
        usage = UsageInfo(input_tokens=100, output_tokens=50)
        assert usage.cache_read_input_tokens == 0
        assert usage.cache_creation_input_tokens == 0

    def test_cache_fields_populated(self):
        """Cache fields accept nonzero values."""
        usage = UsageInfo(
            input_tokens=1,
            output_tokens=4000,
            cache_read_input_tokens=65000,
            cache_creation_input_tokens=5000,
        )
        assert usage.cache_read_input_tokens == 65000
        assert usage.cache_creation_input_tokens == 5000

    def test_model_dump_includes_cache_fields(self):
        """model_dump() must include cache fields (what goes to S3 as JSON)."""
        usage = UsageInfo(
            input_tokens=1,
            output_tokens=4000,
            cache_read_input_tokens=65000,
            cache_creation_input_tokens=5000,
        )
        dumped = usage.model_dump(mode="json")
        assert dumped["cache_read_input_tokens"] == 65000
        assert dumped["cache_creation_input_tokens"] == 5000
        assert dumped["input_tokens"] == 1
        assert dumped["output_tokens"] == 4000

    def test_usage_info_from_dict_with_cache(self):
        """UsageInfo can be constructed from a dict with cache fields (as in service.py)."""
        usage_data = {
            "input_tokens": 1,
            "output_tokens": 4000,
            "cache_read_input_tokens": 65000,
            "cache_creation_input_tokens": 5000,
        }
        usage = UsageInfo(
            input_tokens=usage_data.get("input_tokens", 0),
            output_tokens=usage_data.get("output_tokens", 0),
            cache_read_input_tokens=usage_data.get("cache_read_input_tokens", 0),
            cache_creation_input_tokens=usage_data.get("cache_creation_input_tokens", 0),
        )
        assert usage.cache_read_input_tokens == 65000
        assert usage.cache_creation_input_tokens == 5000

    def test_usage_info_backward_compatible(self):
        """Existing code that constructs UsageInfo without cache fields still works."""
        usage = UsageInfo(input_tokens=100, output_tokens=50)
        dumped = usage.model_dump(mode="json")
        assert dumped["cache_read_input_tokens"] == 0
        assert dumped["cache_creation_input_tokens"] == 0
        assert dumped["input_tokens"] == 100
        assert dumped["output_tokens"] == 50
