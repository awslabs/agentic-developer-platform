"""
Tests for the request timing utility module.

Issue #144: Phase 1 - Response Timing Headers

Tests the RequestTimings class and get_timings() helper function.
"""

import time
from unittest.mock import MagicMock

import pytest

from src.shared.timing import RequestTimings, get_timings


class TestRequestTimings:
    """Tests for RequestTimings class."""

    def test_record_timing(self):
        """Test manually recording a timing value."""
        timings = RequestTimings()
        timings.record("auth", 5.3)

        assert "auth" in timings
        assert timings.get("auth") == 5.3

    def test_record_rounds_to_one_decimal(self):
        """Test that recorded values are rounded to 1 decimal place."""
        timings = RequestTimings()
        timings.record("auth", 5.347)

        assert timings.get("auth") == 5.3

    def test_time_segment_context_manager(self):
        """Test that time_segment() correctly measures elapsed time."""
        timings = RequestTimings()

        with timings.time_segment("test_segment"):
            time.sleep(0.01)  # Sleep 10ms

        elapsed = timings.get("test_segment")
        assert elapsed is not None
        assert elapsed >= 5  # At least 5ms (allowing for scheduling variance)
        assert elapsed < 200  # But not unreasonably long

    def test_time_segment_records_on_exception(self):
        """Test that timing is recorded even if the code block raises."""
        timings = RequestTimings()

        with pytest.raises(ValueError):
            with timings.time_segment("failing_segment"):
                raise ValueError("test error")

        # Timing should still be recorded
        assert "failing_segment" in timings
        elapsed = timings.get("failing_segment")
        assert elapsed is not None
        assert elapsed >= 0

    def test_to_header_basic(self):
        """Test basic header formatting."""
        timings = RequestTimings()
        timings.record("auth", 5.0)
        timings.record("bedrock", 1847.0)
        timings.record("total", 1870.0)

        header = timings.to_header()
        assert "auth=5ms" in header
        assert "bedrock=1847ms" in header
        assert "total=1870ms" in header
        assert ";" in header

    def test_to_header_preferred_order(self):
        """Test that header segments follow preferred ordering."""
        timings = RequestTimings()
        # Record in reverse order
        timings.record("total", 100.0)
        timings.record("serialize", 2.0)
        timings.record("bedrock", 80.0)
        timings.record("budget_check", 5.0)
        timings.record("auth", 3.0)

        header = timings.to_header()
        parts = header.split(";")

        # Verify ordering follows preferred order
        segment_names = [p.split("=")[0] for p in parts]
        assert segment_names.index("auth") < segment_names.index("budget_check")
        assert segment_names.index("budget_check") < segment_names.index("bedrock")
        assert segment_names.index("bedrock") < segment_names.index("serialize")
        assert segment_names.index("serialize") < segment_names.index("total")

    def test_to_header_empty(self):
        """Test empty header."""
        timings = RequestTimings()
        assert timings.to_header() == ""

    def test_to_header_full_breakdown(self):
        """Test header with all segments matching issue spec."""
        timings = RequestTimings()
        timings.record("auth", 5.0)
        timings.record("model_resolve", 1.0)
        timings.record("budget_check", 12.0)
        timings.record("ratelimit_check", 3.0)
        timings.record("bedrock", 1847.0)
        timings.record("serialize", 2.0)
        timings.record("total", 1870.0)

        header = timings.to_header()
        expected = "auth=5ms;model_resolve=1ms;budget_check=12ms;ratelimit_check=3ms;bedrock=1847ms;serialize=2ms;total=1870ms"
        assert header == expected

    def test_to_dict(self):
        """Test dict conversion for JSON logging."""
        timings = RequestTimings()
        timings.record("auth", 5.0)
        timings.record("bedrock", 1847.0)

        d = timings.to_dict()
        assert isinstance(d, dict)
        assert d["auth"] == 5.0
        assert d["bedrock"] == 1847.0

    def test_to_dict_returns_copy(self):
        """Test that to_dict returns a separate dict."""
        inner = {"auth": 5.0}
        timings = RequestTimings(inner)
        d = timings.to_dict()

        # Modifying returned dict should not affect internal state
        d["new_key"] = 999
        assert "new_key" not in inner

    def test_contains(self):
        """Test __contains__ for 'in' operator."""
        timings = RequestTimings()
        timings.record("auth", 5.0)

        assert "auth" in timings
        assert "bedrock" not in timings

    def test_len(self):
        """Test __len__."""
        timings = RequestTimings()
        assert len(timings) == 0

        timings.record("auth", 5.0)
        assert len(timings) == 1

        timings.record("bedrock", 100.0)
        assert len(timings) == 2

    def test_get_nonexistent(self):
        """Test get for non-existent key returns None."""
        timings = RequestTimings()
        assert timings.get("nonexistent") is None

    def test_repr(self):
        """Test string representation."""
        timings = RequestTimings()
        timings.record("auth", 5.0)
        assert "RequestTimings" in repr(timings)
        assert "auth" in repr(timings)

    def test_wraps_existing_dict(self):
        """Test that RequestTimings wraps and mutates existing dict."""
        existing = {}
        timings = RequestTimings(existing)
        timings.record("auth", 5.0)

        # Should be reflected in the original dict
        assert existing["auth"] == 5.0

    def test_multiple_segments(self):
        """Test recording multiple segments."""
        timings = RequestTimings()
        timings.record("auth", 5.0)
        timings.record("model_resolve", 1.0)
        timings.record("budget_check", 12.0)
        timings.record("bedrock", 1847.0)
        timings.record("total", 1870.0)

        assert len(timings) == 5
        header = timings.to_header()
        for name in ["auth", "model_resolve", "budget_check", "bedrock", "total"]:
            assert name in header

    def test_custom_segments_in_header(self):
        """Test that custom segments (not in preferred order) appear at end."""
        timings = RequestTimings()
        timings.record("custom_segment", 42.0)
        timings.record("auth", 5.0)

        header = timings.to_header()
        parts = header.split(";")
        segment_names = [p.split("=")[0] for p in parts]

        # auth should come before custom_segment
        assert segment_names.index("auth") < segment_names.index("custom_segment")


class TestGetTimings:
    """Tests for get_timings() helper function."""

    def test_lazy_init(self):
        """Test that get_timings lazily initializes request.state.timings."""
        request = MagicMock()
        # Simulate no timings attribute
        del request.state.timings

        timings = get_timings(request)
        assert isinstance(timings, RequestTimings)
        assert hasattr(request.state, "timings")

    def test_returns_wrapper_for_existing_dict(self):
        """Test that get_timings wraps existing dict."""
        request = MagicMock()
        request.state.timings = {"auth": 5.0}

        timings = get_timings(request)
        assert timings.get("auth") == 5.0

    def test_mutations_reflect_in_state(self):
        """Test that mutations through wrapper reflect in request.state."""
        request = MagicMock()
        request.state.timings = {}

        timings = get_timings(request)
        timings.record("auth", 5.0)

        assert request.state.timings["auth"] == 5.0

    def test_multiple_calls_same_dict(self):
        """Test that multiple calls to get_timings share the same dict."""
        request = MagicMock()
        request.state.timings = {}

        timings1 = get_timings(request)
        timings1.record("auth", 5.0)

        timings2 = get_timings(request)
        assert timings2.get("auth") == 5.0
