"""Unit tests for lib/inject_dispatch_marker.py (issue #2149 Story 3)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.inject_dispatch_marker import inject_dispatch


class TestInjectDispatch:
    """Tests for inject_dispatch() — the dispatch marker injection logic."""

    def test_injects_into_existing_marker_without_dispatch(self):
        """Injects adp-dispatch into an existing marker that lacks it."""
        body = (
            "<!-- adp-correlation:corr-001 adp-root-human:user-001 "
            "adp-is-human-rooted:true adp-invocation:msg-001 adp-chain-depth:2 -->\n"
            "@agent-developer please review this"
        )
        result = inject_dispatch(body, "developer")
        assert "adp-dispatch:developer" in result
        # Original fields preserved
        assert "adp-correlation:corr-001" in result
        assert "adp-root-human:user-001" in result
        assert "adp-chain-depth:2" in result

    def test_idempotent_when_dispatch_already_present(self):
        """No-op when the dispatch field is already in the marker."""
        body = (
            "<!-- adp-correlation:corr-001 adp-root-human:user-001 "
            "adp-is-human-rooted:true adp-dispatch:developer -->\n"
            "@agent-developer please review this"
        )
        result = inject_dispatch(body, "developer")
        assert result == body

    def test_does_not_override_different_persona(self):
        """Does not override an existing dispatch for a different persona."""
        body = (
            "<!-- adp-correlation:corr-001 adp-root-human:user-001 "
            "adp-is-human-rooted:true adp-dispatch:reviewer -->\n"
            "@agent-developer please review this"
        )
        result = inject_dispatch(body, "developer")
        # Should not change — an existing dispatch target should not be overridden
        assert result == body

    def test_prepends_full_marker_when_none_exists(self):
        """Prepends a full marker with dispatch when body has no marker."""
        env = {
            "ADP_CORRELATION_ID": "corr-new",
            "ADP_ROOT_HUMAN_ID": "user-new",
            "ADP_IS_HUMAN_ROOTED": "true",
            "ADP_MESSAGE_ID": "msg-new",
            "ADP_CHAIN_DEPTH": "1",
        }
        with patch.dict(os.environ, env, clear=False):
            result = inject_dispatch("@agent-developer please do this work", "developer")
        assert "adp-correlation:corr-new" in result
        assert "adp-dispatch:developer" in result
        assert "@agent-developer" in result

    def test_no_op_when_persona_is_empty(self):
        """Returns body unchanged when persona is empty."""
        body = "Some body text"
        result = inject_dispatch(body, "")
        assert result == body

    def test_no_op_when_persona_is_none_like(self):
        """Returns body unchanged when persona is falsy."""
        body = "Some body text"
        # Type-wise this is a string, but testing empty case
        result = inject_dispatch(body, "")
        assert result == body

    def test_preserves_body_content_after_injection(self):
        """Body content after the marker is preserved."""
        body = (
            "<!-- adp-correlation:corr-001 adp-root-human:user-001 "
            "adp-is-human-rooted:true -->\n"
            "## Task\n\n@agent-developer please implement feature X\n\n"
            "### Requirements\n- Item 1\n- Item 2"
        )
        result = inject_dispatch(body, "developer")
        assert "adp-dispatch:developer" in result
        assert "## Task" in result
        assert "### Requirements" in result
        assert "- Item 1" in result

    def test_marker_stays_single_line(self):
        """Injected marker remains on a single line."""
        body = (
            "<!-- adp-correlation:corr-001 adp-root-human:user-001 "
            "adp-is-human-rooted:true adp-invocation:msg-001 adp-chain-depth:3 -->\n"
            "@agent-reviewer please check"
        )
        result = inject_dispatch(body, "reviewer")
        first_line = result.split("\n")[0]
        assert first_line.startswith("<!--")
        assert first_line.endswith("-->")
        assert "adp-dispatch:reviewer" in first_line
