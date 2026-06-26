"""Tests for common/marker_parse.py — shared marker format lockstep.

Issue #1696: The marker format produced by the worker's correlation_marker.py
must be parseable by the Lambda's marker_parse.py. These tests use a shared
fixture to ensure the two sides cannot drift.
"""

import sys
from pathlib import Path

# Add paths
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from common.marker_parse import has_valid_marker, parse_marker

# --- Shared fixture: canonical marker as produced by correlation_marker.py ---
# This exact string format is what the worker emits. If worker changes format,
# this fixture must change too — and these tests will enforce the parser updates.
MARKER_FULL = (
    "<!-- adp-correlation:corr-abc-123 adp-root-human:user-456 "
    "adp-is-human-rooted:true adp-invocation:msg-789 adp-chain-depth:3 -->"
)

MARKER_LEGACY = (
    "<!-- adp-correlation:corr-old-001 adp-root-human:user-old "
    "adp-is-human-rooted:false -->"
)

MARKER_PARTIAL = (
    "<!-- adp-correlation:corr-partial adp-root-human:user-p "
    "adp-is-human-rooted:true adp-invocation:msg-partial -->"
)


class TestParseMarker:
    def test_full_marker_round_trip(self):
        """Full marker with all fields parses correctly."""
        result = parse_marker(MARKER_FULL + "\nSome body content")
        assert result is not None
        assert result["correlation_id"] == "corr-abc-123"
        assert result["root_human_id"] == "user-456"
        assert result["is_human_rooted"] is True
        assert result["invocation_id"] == "msg-789"
        assert result["chain_depth"] == 3

    def test_legacy_marker_without_new_fields(self):
        """Old markers without adp-invocation/adp-chain-depth parse gracefully."""
        result = parse_marker(MARKER_LEGACY + "\nOld comment body")
        assert result is not None
        assert result["correlation_id"] == "corr-old-001"
        assert result["root_human_id"] == "user-old"
        assert result["is_human_rooted"] is False
        assert result["invocation_id"] is None
        assert result["chain_depth"] is None

    def test_partial_marker_missing_depth(self):
        """Marker with invocation but no depth parses — depth is None."""
        result = parse_marker(MARKER_PARTIAL + "\nBody")
        assert result is not None
        assert result["invocation_id"] == "msg-partial"
        assert result["chain_depth"] is None

    def test_no_marker_returns_none(self):
        """Text without a marker returns None."""
        result = parse_marker("Just a regular comment body")
        assert result is None

    def test_empty_text_returns_none(self):
        result = parse_marker("")
        assert result is None

    def test_none_text_returns_none(self):
        result = parse_marker(None)
        assert result is None

    def test_marker_not_in_first_1000_bytes(self):
        """Marker buried deep in text is not found (performance bound)."""
        padding = "x" * 1100
        result = parse_marker(padding + MARKER_FULL)
        assert result is None

    def test_marker_at_start_of_body(self):
        """Marker at the very start (typical case) is found."""
        result = parse_marker(MARKER_FULL)
        assert result is not None
        assert result["correlation_id"] == "corr-abc-123"

    def test_is_human_rooted_false(self):
        result = parse_marker(MARKER_LEGACY)
        assert result is not None
        assert result["is_human_rooted"] is False

    def test_invalid_chain_depth_non_numeric(self):
        """Non-numeric chain_depth in marker is treated as None."""
        bad_marker = (
            "<!-- adp-correlation:corr-x adp-root-human:user-y "
            "adp-is-human-rooted:true adp-chain-depth:not-a-number -->"
        )
        result = parse_marker(bad_marker)
        assert result is not None
        assert result["chain_depth"] is None

    def test_missing_correlation_id_returns_none(self):
        """Marker without correlation_id is not valid."""
        bad_marker = "<!-- adp-root-human:user-y adp-is-human-rooted:true -->"
        result = parse_marker(bad_marker)
        assert result is None


class TestHasValidMarker:
    def test_valid_marker(self):
        assert has_valid_marker(MARKER_FULL + "\nbody") is True

    def test_no_marker(self):
        assert has_valid_marker("just text") is False

    def test_empty(self):
        assert has_valid_marker("") is False

    def test_none(self):
        assert has_valid_marker(None) is False

    def test_legacy_marker(self):
        assert has_valid_marker(MARKER_LEGACY) is True


# --- Issue #2149: adp-dispatch field ---

MARKER_WITH_DISPATCH = (
    "<!-- adp-correlation:corr-dispatch-001 adp-root-human:user-d "
    "adp-is-human-rooted:true adp-invocation:msg-d adp-chain-depth:2 "
    "adp-dispatch:developer -->"
)

MARKER_WITH_DISPATCH_REVIEWER = (
    "<!-- adp-correlation:corr-dispatch-002 adp-root-human:user-r "
    "adp-is-human-rooted:true adp-dispatch:reviewer -->"
)


class TestDispatchPersonaField:
    """Issue #2149: parse adp-dispatch:<persona> from markers."""

    def test_dispatch_persona_parsed(self):
        """Marker with adp-dispatch:developer parses correctly."""
        result = parse_marker(MARKER_WITH_DISPATCH + "\nBody")
        assert result is not None
        assert result["dispatch_persona"] == "developer"
        # Other fields still work
        assert result["correlation_id"] == "corr-dispatch-001"
        assert result["chain_depth"] == 2

    def test_dispatch_persona_reviewer(self):
        """Marker with adp-dispatch:reviewer parses correctly."""
        result = parse_marker(MARKER_WITH_DISPATCH_REVIEWER + "\nBody")
        assert result is not None
        assert result["dispatch_persona"] == "reviewer"

    def test_dispatch_persona_absent_on_legacy_markers(self):
        """Old markers without adp-dispatch parse with dispatch_persona=None."""
        result = parse_marker(MARKER_FULL + "\nBody")
        assert result is not None
        assert result["dispatch_persona"] is None

    def test_dispatch_persona_absent_on_partial_markers(self):
        """Partial markers return dispatch_persona=None."""
        result = parse_marker(MARKER_PARTIAL + "\nBody")
        assert result is not None
        assert result["dispatch_persona"] is None

    def test_dispatch_field_with_hyphenated_persona(self):
        """Hyphenated persona names parse correctly."""
        marker = (
            "<!-- adp-correlation:corr-x adp-root-human:user-y "
            "adp-is-human-rooted:true adp-dispatch:malware-analysis-agent -->"
        )
        result = parse_marker(marker)
        assert result is not None
        assert result["dispatch_persona"] == "malware-analysis-agent"

    def test_dispatch_field_does_not_break_other_parsing(self):
        """Adding dispatch field doesn't interfere with other field parsing."""
        result = parse_marker(MARKER_WITH_DISPATCH)
        assert result is not None
        assert result["correlation_id"] == "corr-dispatch-001"
        assert result["root_human_id"] == "user-d"
        assert result["is_human_rooted"] is True
        assert result["invocation_id"] == "msg-d"
        assert result["chain_depth"] == 2
        assert result["dispatch_persona"] == "developer"
