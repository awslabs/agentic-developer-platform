"""Unit tests for S3 transcript upload (Issue #3057).

Verifies that _upload_transcript_to_s3:
  - Calls S3 PutObject with correct bucket/key/ContentType when env var set
  - Skips (no crash) when AGENT_RUN_LOGS_BUCKET is unset or empty
  - Skips (no crash) when final_text is empty
  - Does NOT change exit code on upload failure (fail-soft contract)
  - Constructs the correct key layout: {persona}/{org}/{repo}/issue-{N}/{timestamp}-{run_id}.md
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from entrypoint import _upload_transcript_to_s3


class TestTranscriptUpload:
    """Tests for _upload_transcript_to_s3()."""

    BUCKET = "adp-dev-agent-run-logs-123456789012"
    REPO = "acme-corp/flagship-app"
    ISSUE = 42
    MESSAGE_ID = "df24428c-1234-5678-9abc-def012345678"
    ARRIVED_AT = "2026-07-06T13:20:00Z"
    PERSONA = "developer"
    TRANSCRIPT = "# Agent Run Transcript\n\nStep 1: analyzed the issue...\n" * 100

    @patch.dict(os.environ, {"AGENT_RUN_LOGS_BUCKET": BUCKET, "AWS_REGION": "us-east-1"})
    @patch("entrypoint.boto3.client")
    def test_upload_called_with_correct_params(self, mock_boto_client):
        """S3 PutObject is called with correct bucket, key, and ContentType."""
        mock_s3 = MagicMock()
        mock_boto_client.return_value = mock_s3

        _upload_transcript_to_s3(
            self.TRANSCRIPT,
            self.REPO,
            self.ISSUE,
            self.MESSAGE_ID,
            self.ARRIVED_AT,
            self.PERSONA,
        )

        mock_boto_client.assert_called_once_with("s3", region_name="us-east-1")
        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args[1]

        assert call_kwargs["Bucket"] == self.BUCKET
        assert call_kwargs["ContentType"] == "text/markdown"
        assert call_kwargs["Body"] == self.TRANSCRIPT.encode("utf-8")

    @patch.dict(os.environ, {"AGENT_RUN_LOGS_BUCKET": BUCKET, "AWS_REGION": "us-east-1"})
    @patch("entrypoint.boto3.client")
    def test_key_layout(self, mock_boto_client):
        """Object key follows {persona}/{org}/{repo}/issue-{N}/{timestamp}-{run_id}.md."""
        mock_s3 = MagicMock()
        mock_boto_client.return_value = mock_s3

        _upload_transcript_to_s3(
            self.TRANSCRIPT,
            self.REPO,
            self.ISSUE,
            self.MESSAGE_ID,
            self.ARRIVED_AT,
            self.PERSONA,
        )

        call_kwargs = mock_s3.put_object.call_args[1]
        key = call_kwargs["Key"]

        # Key starts with persona/org/repo
        assert key.startswith("developer/acme-corp/flagship-app/issue-42/")
        # Key ends with .md
        assert key.endswith(".md")
        # Key contains run_id (first 8 chars of message_id)
        assert "df24428c" in key
        # Key contains timestamp derived from arrived_at
        assert "20260706T132000" in key

    @patch.dict(os.environ, {"AGENT_RUN_LOGS_BUCKET": "", "AWS_REGION": "us-east-1"})
    @patch("entrypoint.boto3.client")
    def test_skipped_when_bucket_env_empty(self, mock_boto_client):
        """Upload is skipped when AGENT_RUN_LOGS_BUCKET is empty string."""
        _upload_transcript_to_s3(
            self.TRANSCRIPT,
            self.REPO,
            self.ISSUE,
            self.MESSAGE_ID,
            self.ARRIVED_AT,
            self.PERSONA,
        )

        mock_boto_client.assert_not_called()

    @patch.dict(os.environ, {"AWS_REGION": "us-east-1"}, clear=False)
    @patch("entrypoint.boto3.client")
    def test_skipped_when_bucket_env_unset(self, mock_boto_client, monkeypatch):
        """Upload is skipped when AGENT_RUN_LOGS_BUCKET is not in environment."""
        monkeypatch.delenv("AGENT_RUN_LOGS_BUCKET", raising=False)

        _upload_transcript_to_s3(
            self.TRANSCRIPT,
            self.REPO,
            self.ISSUE,
            self.MESSAGE_ID,
            self.ARRIVED_AT,
            self.PERSONA,
        )

        mock_boto_client.assert_not_called()

    @patch.dict(os.environ, {"AGENT_RUN_LOGS_BUCKET": BUCKET, "AWS_REGION": "us-east-1"})
    @patch("entrypoint.boto3.client")
    def test_skipped_when_final_text_empty(self, mock_boto_client):
        """Upload is skipped when transcript text is empty."""
        _upload_transcript_to_s3(
            "", self.REPO, self.ISSUE, self.MESSAGE_ID, self.ARRIVED_AT, self.PERSONA
        )

        mock_boto_client.assert_not_called()

    @patch.dict(os.environ, {"AGENT_RUN_LOGS_BUCKET": BUCKET, "AWS_REGION": "us-east-1"})
    @patch("entrypoint.boto3.client")
    def test_exception_does_not_propagate(self, mock_boto_client):
        """S3 upload failure must NOT raise — fail-soft contract."""
        mock_s3 = MagicMock()
        mock_boto_client.return_value = mock_s3
        mock_s3.put_object.side_effect = Exception("AccessDenied: bucket policy")

        # Must not raise
        _upload_transcript_to_s3(
            self.TRANSCRIPT,
            self.REPO,
            self.ISSUE,
            self.MESSAGE_ID,
            self.ARRIVED_AT,
            self.PERSONA,
        )

    @patch.dict(os.environ, {"AGENT_RUN_LOGS_BUCKET": BUCKET, "AWS_REGION": "us-east-1"})
    @patch("entrypoint.boto3.client")
    def test_key_with_missing_arrived_at(self, mock_boto_client):
        """Handles empty arrived_at gracefully (uses 'unknown' timestamp)."""
        mock_s3 = MagicMock()
        mock_boto_client.return_value = mock_s3

        _upload_transcript_to_s3(
            self.TRANSCRIPT, self.REPO, self.ISSUE, self.MESSAGE_ID, "", self.PERSONA
        )

        # Empty arrived_at has no "T" so timestamp = "unknown"
        call_kwargs = mock_s3.put_object.call_args[1]
        key = call_kwargs["Key"]
        assert "unknown" in key
        assert key.startswith("developer/acme-corp/flagship-app/issue-42/")

    @patch.dict(os.environ, {"AGENT_RUN_LOGS_BUCKET": BUCKET, "AWS_REGION": "us-east-1"})
    @patch("entrypoint.boto3.client")
    def test_key_with_empty_message_id(self, mock_boto_client):
        """Handles empty message_id gracefully (uses 'norunid' fallback)."""
        mock_s3 = MagicMock()
        mock_boto_client.return_value = mock_s3

        _upload_transcript_to_s3(
            self.TRANSCRIPT, self.REPO, self.ISSUE, "", self.ARRIVED_AT, self.PERSONA
        )

        call_kwargs = mock_s3.put_object.call_args[1]
        key = call_kwargs["Key"]
        assert "norunid" in key


class TestIndependentTranscript:
    def test_new_archive_survives_github_clipping_and_uploads_in_full(self, tmp_path, monkeypatch):
        from entrypoint import _read_run_reports

        github = "Short GitHub display"
        transcript = "## Implementation walkthrough\n\n" + "Explanation 界🌍\n" * 10000 + "Final caveat."
        (tmp_path / "adp-check-run-final.md").write_text(github)
        (tmp_path / "adp-run-transcript.md").write_text(transcript)
        display, archive = _read_run_reports(str(tmp_path))
        assert display == github
        assert archive == transcript
        monkeypatch.setenv("AGENT_RUN_LOGS_BUCKET", "test-transcripts")
        with patch("entrypoint.boto3.client") as client:
            _upload_transcript_to_s3(archive, "org/repo", 42, "run-id", "2026-09-12T08:00:00Z", "developer")
            assert client.return_value.put_object.call_args.kwargs["Body"] == transcript.encode("utf-8")

    @pytest.mark.parametrize("archive_content", [None, "", "   "])
    def test_old_or_missing_archive_is_labeled_as_a_potentially_clipped_fallback(self, tmp_path, archive_content):
        from entrypoint import _read_run_reports

        (tmp_path / "adp-check-run-final.md").write_text("Old clipped output")
        if archive_content is not None:
            (tmp_path / "adp-run-transcript.md").write_text(archive_content)
        display, archive = _read_run_reports(str(tmp_path))
        assert display == "Old clipped output"
        assert "GitHub display fallback" in archive
        assert "may be truncated or incomplete" in archive
        assert archive.endswith(display)

    def test_archive_is_available_without_a_github_display(self, tmp_path):
        from entrypoint import _read_run_reports

        (tmp_path / "adp-run-transcript.md").write_text("Complete captured explanation")
        assert _read_run_reports(str(tmp_path)) == ("", "Complete captured explanation")

    def test_unreadable_archive_falls_back_without_hiding_the_github_display(self, tmp_path):
        from entrypoint import _read_run_reports

        (tmp_path / "adp-check-run-final.md").write_text("Available display")
        (tmp_path / "adp-run-transcript.md").mkdir()
        display, archive = _read_run_reports(str(tmp_path))
        assert display == "Available display"
        assert archive.endswith(display)
        assert "fallback" in archive

    def test_missing_reports_preserve_upload_skip_behavior(self, tmp_path):
        from entrypoint import _read_run_reports

        assert _read_run_reports(str(tmp_path)) == ("", "")
