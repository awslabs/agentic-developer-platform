"""Unit tests for lib/correlation_marker.py."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.correlation_marker import prepend_correlation_marker


class TestPrependCorrelationMarker:
    """Tests for prepend_correlation_marker()."""

    def test_prepends_marker_when_env_vars_set(self):
        env = {
            "ADP_CORRELATION_ID": "corr-123",
            "ADP_ROOT_HUMAN_ID": "user-456",
            "ADP_IS_HUMAN_ROOTED": "true",
        }
        with patch.dict(os.environ, env, clear=False):
            result = prepend_correlation_marker("Hello world")
        assert result.startswith("<!-- adp-correlation:corr-123")
        assert "adp-root-human:user-456" in result
        assert "adp-is-human-rooted:true" in result
        assert result.endswith("\nHello world")

    def test_no_op_when_correlation_id_missing(self):
        env = {
            "ADP_ROOT_HUMAN_ID": "user-456",
            "ADP_IS_HUMAN_ROOTED": "true",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("ADP_CORRELATION_ID", None)
            result = prepend_correlation_marker("Hello world")
        assert result == "Hello world"

    def test_no_op_when_root_human_id_missing(self):
        env = {
            "ADP_CORRELATION_ID": "corr-123",
            "ADP_IS_HUMAN_ROOTED": "true",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("ADP_ROOT_HUMAN_ID", None)
            result = prepend_correlation_marker("Hello world")
        assert result == "Hello world"

    def test_idempotent_does_not_double_prepend(self):
        env = {
            "ADP_CORRELATION_ID": "corr-123",
            "ADP_ROOT_HUMAN_ID": "user-456",
            "ADP_IS_HUMAN_ROOTED": "true",
        }
        with patch.dict(os.environ, env, clear=False):
            first = prepend_correlation_marker("Hello world")
            second = prepend_correlation_marker(first)
        assert first == second

    def test_idempotent_detects_existing_marker_in_first_500_bytes(self):
        body = "<!-- adp-correlation:old-corr adp-root-human:old-user adp-is-human-rooted:true -->\nSome content"
        env = {
            "ADP_CORRELATION_ID": "new-corr",
            "ADP_ROOT_HUMAN_ID": "new-user",
            "ADP_IS_HUMAN_ROOTED": "false",
        }
        with patch.dict(os.environ, env, clear=False):
            result = prepend_correlation_marker(body)
        # Should NOT replace or add a new marker
        assert result == body

    def test_defaults_is_human_rooted_to_false(self):
        env = {
            "ADP_CORRELATION_ID": "corr-123",
            "ADP_ROOT_HUMAN_ID": "user-456",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("ADP_IS_HUMAN_ROOTED", None)
            result = prepend_correlation_marker("Hello")
        assert "adp-is-human-rooted:false" in result

    def test_empty_body(self):
        env = {
            "ADP_CORRELATION_ID": "corr-123",
            "ADP_ROOT_HUMAN_ID": "user-456",
            "ADP_IS_HUMAN_ROOTED": "true",
        }
        with patch.dict(os.environ, env, clear=False):
            result = prepend_correlation_marker("")
        assert result.startswith("<!-- adp-correlation:corr-123")
        assert result.endswith("-->\n")

    # --- Issue #1696: adp-invocation and adp-chain-depth ---

    def test_includes_invocation_when_message_id_set(self):
        """Marker includes adp-invocation when ADP_MESSAGE_ID is set."""
        env = {
            "ADP_CORRELATION_ID": "corr-123",
            "ADP_ROOT_HUMAN_ID": "user-456",
            "ADP_IS_HUMAN_ROOTED": "true",
            "ADP_MESSAGE_ID": "msg-789",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("ADP_CHAIN_DEPTH", None)
            result = prepend_correlation_marker("Body")
        assert "adp-invocation:msg-789" in result
        assert result.startswith("<!--")
        assert result.count("-->") == 1

    def test_includes_chain_depth_when_set(self):
        """Marker includes adp-chain-depth when ADP_CHAIN_DEPTH is set."""
        env = {
            "ADP_CORRELATION_ID": "corr-123",
            "ADP_ROOT_HUMAN_ID": "user-456",
            "ADP_IS_HUMAN_ROOTED": "true",
            "ADP_CHAIN_DEPTH": "3",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("ADP_MESSAGE_ID", None)
            result = prepend_correlation_marker("Body")
        assert "adp-chain-depth:3" in result

    def test_full_marker_with_all_fields(self):
        """Marker includes all fields when all env vars are set."""
        env = {
            "ADP_CORRELATION_ID": "corr-full",
            "ADP_ROOT_HUMAN_ID": "user-full",
            "ADP_IS_HUMAN_ROOTED": "true",
            "ADP_MESSAGE_ID": "msg-full",
            "ADP_CHAIN_DEPTH": "5",
        }
        with patch.dict(os.environ, env, clear=False):
            result = prepend_correlation_marker("Test body")
        assert "adp-correlation:corr-full" in result
        assert "adp-root-human:user-full" in result
        assert "adp-is-human-rooted:true" in result
        assert "adp-invocation:msg-full" in result
        assert "adp-chain-depth:5" in result
        # Single-line marker (one opening <!--, one closing -->)
        marker_line = result.split("\n")[0]
        assert marker_line.startswith("<!--")
        assert marker_line.endswith("-->")

    def test_no_invocation_when_message_id_empty(self):
        """No adp-invocation field when ADP_MESSAGE_ID is empty."""
        env = {
            "ADP_CORRELATION_ID": "corr-123",
            "ADP_ROOT_HUMAN_ID": "user-456",
            "ADP_IS_HUMAN_ROOTED": "true",
            "ADP_MESSAGE_ID": "",
            "ADP_CHAIN_DEPTH": "",
        }
        with patch.dict(os.environ, env, clear=False):
            result = prepend_correlation_marker("Body")
        assert "adp-invocation" not in result
        assert "adp-chain-depth" not in result

    def test_marker_is_single_line(self):
        """Marker MUST be single-line (GitHub Markdown rendering requirement)."""
        env = {
            "ADP_CORRELATION_ID": "corr-123",
            "ADP_ROOT_HUMAN_ID": "user-456",
            "ADP_IS_HUMAN_ROOTED": "true",
            "ADP_MESSAGE_ID": "msg-789",
            "ADP_CHAIN_DEPTH": "2",
        }
        with patch.dict(os.environ, env, clear=False):
            result = prepend_correlation_marker("Body")
        lines = result.split("\n")
        # First line is the marker, second line onward is the body
        assert lines[0].startswith("<!--")
        assert lines[0].endswith("-->")
        assert lines[1] == "Body"

    # --- Issue #2149: adp-dispatch marker ---

    def test_includes_dispatch_persona_when_set(self):
        """Marker includes adp-dispatch:<persona> when dispatch_persona is passed."""
        env = {
            "ADP_CORRELATION_ID": "corr-123",
            "ADP_ROOT_HUMAN_ID": "user-456",
            "ADP_IS_HUMAN_ROOTED": "true",
        }
        with patch.dict(os.environ, env, clear=False):
            result = prepend_correlation_marker(
                "@agent-developer please implement", dispatch_persona="developer"
            )
        assert "adp-dispatch:developer" in result
        assert "adp-correlation:corr-123" in result
        assert "adp-root-human:user-456" in result

    def test_no_dispatch_when_persona_not_set(self):
        """No adp-dispatch field when dispatch_persona is not passed (status comments)."""
        env = {
            "ADP_CORRELATION_ID": "corr-123",
            "ADP_ROOT_HUMAN_ID": "user-456",
            "ADP_IS_HUMAN_ROOTED": "true",
        }
        with patch.dict(os.environ, env, clear=False):
            result = prepend_correlation_marker("## @agent-developer Started")
        assert "adp-dispatch" not in result

    def test_no_dispatch_when_persona_empty_string(self):
        """No adp-dispatch field when dispatch_persona is empty string."""
        env = {
            "ADP_CORRELATION_ID": "corr-123",
            "ADP_ROOT_HUMAN_ID": "user-456",
            "ADP_IS_HUMAN_ROOTED": "true",
        }
        with patch.dict(os.environ, env, clear=False):
            result = prepend_correlation_marker("body", dispatch_persona="")
        assert "adp-dispatch" not in result

    def test_full_marker_with_dispatch_persona(self):
        """Full marker with all fields including dispatch_persona is single-line."""
        env = {
            "ADP_CORRELATION_ID": "corr-full",
            "ADP_ROOT_HUMAN_ID": "user-full",
            "ADP_IS_HUMAN_ROOTED": "true",
            "ADP_MESSAGE_ID": "msg-full",
            "ADP_CHAIN_DEPTH": "3",
        }
        with patch.dict(os.environ, env, clear=False):
            result = prepend_correlation_marker(
                "@agent-reviewer review", dispatch_persona="reviewer"
            )
        lines = result.split("\n")
        marker_line = lines[0]
        assert marker_line.startswith("<!--")
        assert marker_line.endswith("-->")
        assert "adp-correlation:corr-full" in marker_line
        assert "adp-root-human:user-full" in marker_line
        assert "adp-is-human-rooted:true" in marker_line
        assert "adp-invocation:msg-full" in marker_line
        assert "adp-chain-depth:3" in marker_line
        assert "adp-dispatch:reviewer" in marker_line
        # Body on next line
        assert lines[1] == "@agent-reviewer review"
