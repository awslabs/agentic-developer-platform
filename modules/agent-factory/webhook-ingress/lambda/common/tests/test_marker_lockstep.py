"""Marker format lockstep test — worker write ↔ Lambda read.

Issue #1696: This test imports BOTH the worker's correlation_marker.py (write)
and the Lambda's marker_parse.py (read), and asserts round-trip fidelity.
If either side changes format without updating the other, this test breaks.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

# Add both module paths so we can import from both sides
WORKER_ROOT = (
    Path(__file__).resolve().parent.parent.parent.parent.parent / "agent-worker-image"
)
LAMBDA_COMMON = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(WORKER_ROOT))
sys.path.insert(0, str(LAMBDA_COMMON))

# Imports must follow the sys.path manipulation above (cross-module lockstep test).
from lib.correlation_marker import prepend_correlation_marker  # noqa: E402

from common.marker_parse import parse_marker  # noqa: E402


class TestMarkerLockstep:
    """Ensure worker-written markers can be parsed by the Lambda."""

    def test_full_marker_round_trip(self):
        """Full marker (all fields) produced by worker is parseable by Lambda."""
        env = {
            "ADP_CORRELATION_ID": "corr-lockstep-001",
            "ADP_ROOT_HUMAN_ID": "user-lockstep",
            "ADP_IS_HUMAN_ROOTED": "true",
            "ADP_MESSAGE_ID": "msg-lockstep-789",
            "ADP_CHAIN_DEPTH": "4",
        }
        with patch.dict(os.environ, env, clear=False):
            body_with_marker = prepend_correlation_marker("PR body text here")

        parsed = parse_marker(body_with_marker)
        assert parsed is not None
        assert parsed["correlation_id"] == "corr-lockstep-001"
        assert parsed["root_human_id"] == "user-lockstep"
        assert parsed["is_human_rooted"] is True
        assert parsed["invocation_id"] == "msg-lockstep-789"
        assert parsed["chain_depth"] == 4

    def test_legacy_marker_round_trip(self):
        """Legacy marker (no invocation/depth) produced by worker parses as expected."""
        env = {
            "ADP_CORRELATION_ID": "corr-legacy",
            "ADP_ROOT_HUMAN_ID": "user-legacy",
            "ADP_IS_HUMAN_ROOTED": "false",
        }
        with patch.dict(os.environ, env, clear=False):
            # Remove optional vars to simulate old behavior
            os.environ.pop("ADP_MESSAGE_ID", None)
            os.environ.pop("ADP_CHAIN_DEPTH", None)
            body_with_marker = prepend_correlation_marker("Old body")

        parsed = parse_marker(body_with_marker)
        assert parsed is not None
        assert parsed["correlation_id"] == "corr-legacy"
        assert parsed["root_human_id"] == "user-legacy"
        assert parsed["is_human_rooted"] is False
        assert parsed["invocation_id"] is None
        assert parsed["chain_depth"] is None

    def test_idempotent_marker_still_parseable(self):
        """Double-prepend attempt doesn't corrupt the marker."""
        env = {
            "ADP_CORRELATION_ID": "corr-idem",
            "ADP_ROOT_HUMAN_ID": "user-idem",
            "ADP_IS_HUMAN_ROOTED": "true",
            "ADP_MESSAGE_ID": "msg-idem",
            "ADP_CHAIN_DEPTH": "2",
        }
        with patch.dict(os.environ, env, clear=False):
            first = prepend_correlation_marker("Body")
            second = prepend_correlation_marker(first)  # Idempotent — no change

        assert first == second
        parsed = parse_marker(second)
        assert parsed is not None
        assert parsed["correlation_id"] == "corr-idem"
        assert parsed["chain_depth"] == 2

    def test_depth_zero_round_trip(self):
        """Depth 0 (chain root) round-trips correctly."""
        env = {
            "ADP_CORRELATION_ID": "corr-root",
            "ADP_ROOT_HUMAN_ID": "user-root",
            "ADP_IS_HUMAN_ROOTED": "true",
            "ADP_MESSAGE_ID": "msg-root",
            "ADP_CHAIN_DEPTH": "0",
        }
        with patch.dict(os.environ, env, clear=False):
            body = prepend_correlation_marker("Root run body")

        parsed = parse_marker(body)
        assert parsed is not None
        assert parsed["chain_depth"] == 0
