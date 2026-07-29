"""Step 11 must not open PRs whose only content is review transcripts (#PR-noise cleanup 2026-07-29)."""

from unittest.mock import MagicMock, patch

import entrypoint


def _proc(stdout: str = "") -> MagicMock:
    m = MagicMock()
    m.stdout = stdout
    return m


class TestTranscriptOnlyDetection:
    @patch("entrypoint.run_cmd")
    def test_all_transcript_files_returns_true(self, mock_run):
        mock_run.side_effect = [
            _proc(),  # git fetch
            _proc("data/code-review/review-20260729-pr-123.md\n"),
        ]
        assert entrypoint._branch_changes_are_transcript_only("agent/issue-1") is True

    @patch("entrypoint.run_cmd")
    def test_mixed_files_returns_false(self, mock_run):
        mock_run.side_effect = [
            _proc(),
            _proc("data/code-review/review-20260729-pr-123.md\nsrc/app.py\n"),
        ]
        assert entrypoint._branch_changes_are_transcript_only("agent/issue-1") is False

    @patch("entrypoint.run_cmd")
    def test_code_only_returns_false(self, mock_run):
        mock_run.side_effect = [_proc(), _proc("src/app.py\n")]
        assert entrypoint._branch_changes_are_transcript_only("agent/issue-1") is False

    @patch("entrypoint.run_cmd")
    def test_empty_diff_returns_false(self, mock_run):
        mock_run.side_effect = [_proc(), _proc("")]
        assert entrypoint._branch_changes_are_transcript_only("agent/issue-1") is False

    @patch("entrypoint.run_cmd")
    def test_git_failure_fails_open(self, mock_run):
        import subprocess

        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        assert entrypoint._branch_changes_are_transcript_only("agent/issue-1") is False
