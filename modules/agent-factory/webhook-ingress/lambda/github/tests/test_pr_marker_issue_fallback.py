"""Tests for _pr_marker_text_with_issue_fallback — issue #1731/#1735.

The reviewer triggers on PR-open, but lineage must inherit from the ISSUE's
correlation pointer because the PR body has no valid marker at opened time
(the agent's "## Summary" body is non-empty but unmarked). This is the bug
that left parent_invocation_id null in smoke v5.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from handler import _pr_marker_text_with_issue_fallback  # noqa: E402


def _store_with_issue_pointer():
    store = MagicMock()
    store.channel_key.side_effect = lambda prov, repo, kind, num: f"{prov}:repo={repo},{kind}={num}"
    store.read_pointer.return_value = {
        "correlation_id": "corr-DEV",
        "root_human_id": "user-human",
        "is_human_rooted": True,
        "triggering_invocation_id": "inv-DEV",
        "chain_depth": 0,
    }
    return store


class TestPrMarkerIssueFallback:
    def test_nonempty_body_without_marker_falls_back_to_issue_pointer(self):
        """THE BUG: agent's '## Summary' body (no marker) → inherit from issue pointer."""
        store = _store_with_issue_pointer()
        result = _pr_marker_text_with_issue_fallback(
            store, "aws-e/adp", "## Summary\nSome PR description", "agent/issue-1733"
        )
        assert "adp-correlation:corr-DEV" in result
        assert "adp-invocation:inv-DEV" in result
        store.read_pointer.assert_called_once_with("github:repo=aws-e/adp,issue=1733")

    def test_empty_body_falls_back(self):
        store = _store_with_issue_pointer()
        result = _pr_marker_text_with_issue_fallback(store, "aws-e/adp", "", "agent/issue-1733")
        assert "adp-correlation:corr-DEV" in result

    def test_body_with_valid_marker_used_as_is(self):
        """If the PR body already has a valid marker, use it (no fallback)."""
        store = _store_with_issue_pointer()
        marked = (
            "<!-- adp-correlation:corr-PR adp-root-human:u adp-is-human-rooted:true -->\n## Summary"
        )
        result = _pr_marker_text_with_issue_fallback(store, "aws-e/adp", marked, "agent/issue-1733")
        assert result == marked
        store.read_pointer.assert_not_called()

    def test_non_agent_branch_no_fallback(self):
        store = _store_with_issue_pointer()
        result = _pr_marker_text_with_issue_fallback(
            store, "aws-e/adp", "## Summary", "feature/foo"
        )
        assert result == "## Summary"
        store.read_pointer.assert_not_called()

    def test_no_issue_pointer_returns_body(self):
        store = _store_with_issue_pointer()
        store.read_pointer.return_value = None
        result = _pr_marker_text_with_issue_fallback(
            store, "aws-e/adp", "## Summary", "agent/issue-1733"
        )
        assert result == "## Summary"
