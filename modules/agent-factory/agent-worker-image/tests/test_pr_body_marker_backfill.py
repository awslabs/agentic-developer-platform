"""Tests for _ensure_pr_body_marker — issue #1721.

When an agent opens its own PR via the SDK, the entrypoint's marker-prepend is
skipped, so the PR body carries no adp-* correlation marker and the webhook's
marker-gated reviewer trigger (#1696) blocks it. _ensure_pr_body_marker
backfills the marker onto the existing PR body.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import entrypoint  # noqa: E402

CORR_ENV = {
    "ADP_CORRELATION_ID": "corr-xyz",
    "ADP_ROOT_HUMAN_ID": "user-human-1",
    "ADP_IS_HUMAN_ROOTED": "true",
    "ADP_MESSAGE_ID": "msg-dev-1",
}


class TestEnsurePrBodyMarker:
    @patch("entrypoint._write_outbound_correlation")
    @patch("entrypoint.run_cmd")
    @patch.dict(os.environ, CORR_ENV, clear=False)
    def test_backfills_marker_when_absent(self, mock_run, mock_outbound):
        # gh pr view returns an unmarked body; gh pr edit succeeds
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="## Summary\nSome agent PR body\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]
        entrypoint._ensure_pr_body_marker("aws-e/adp", "1720", "agent/issue-1719")

        # Second call must be `gh pr edit ... --body <marked>`
        edit_call = mock_run.call_args_list[1][0][0]
        assert edit_call[:4] == ["gh", "pr", "edit", "1720"]
        body_idx = edit_call.index("--body") + 1
        assert "<!-- adp-correlation:corr-xyz" in edit_call[body_idx]
        assert "## Summary" in edit_call[body_idx]  # original body preserved
        mock_outbound.assert_called_once()

    @patch("entrypoint._write_outbound_correlation")
    @patch("entrypoint.run_cmd")
    @patch.dict(os.environ, CORR_ENV, clear=False)
    def test_noop_when_marker_already_present(self, mock_run, mock_outbound):
        already = "<!-- adp-correlation:corr-xyz adp-root-human:user-human-1 adp-is-human-rooted:true -->\n## Summary\nx"
        mock_run.side_effect = [MagicMock(returncode=0, stdout=already, stderr="")]
        entrypoint._ensure_pr_body_marker("aws-e/adp", "1720", "agent/issue-1719")
        # Only the view call — no edit
        assert mock_run.call_count == 1
        mock_outbound.assert_not_called()

    @patch("entrypoint._write_outbound_correlation")
    @patch("entrypoint.run_cmd")
    @patch.dict(os.environ, {}, clear=True)
    def test_noop_when_no_correlation_context(self, mock_run, mock_outbound):
        # No ADP_* env → prepend_correlation_marker no-ops → no edit
        mock_run.side_effect = [MagicMock(returncode=0, stdout="## Summary\nx", stderr="")]
        entrypoint._ensure_pr_body_marker("aws-e/adp", "1720", "agent/issue-1719")
        assert mock_run.call_count == 1
        mock_outbound.assert_not_called()

    @patch("entrypoint._write_outbound_correlation")
    @patch("entrypoint.run_cmd")
    def test_noop_when_no_pr_number(self, mock_run, mock_outbound):
        entrypoint._ensure_pr_body_marker("aws-e/adp", "", "agent/issue-1719")
        mock_run.assert_not_called()
        mock_outbound.assert_not_called()


class TestFindOpenPr:
    @patch("entrypoint.run_cmd")
    def test_returns_pr_number(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="1726\n", stderr="")
        assert entrypoint._find_open_pr("aws-e/adp", "agent/issue-1725") == "1726"

    @patch("entrypoint.run_cmd")
    def test_returns_empty_when_none(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="\n", stderr="")
        assert entrypoint._find_open_pr("aws-e/adp", "agent/issue-1725") == ""

    @patch("entrypoint.run_cmd")
    def test_fail_soft_on_error(self, mock_run):
        import subprocess
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        assert entrypoint._find_open_pr("aws-e/adp", "agent/issue-1725") == ""
