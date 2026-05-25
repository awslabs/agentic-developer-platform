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
