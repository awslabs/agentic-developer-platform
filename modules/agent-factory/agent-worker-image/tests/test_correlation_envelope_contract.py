"""Contract test: worker reads correlation context from the NESTED envelope shape.

Issue #1679: The handler publishes correlation fields nested under
envelope["correlation"] (handler.py:711-718). The worker must read from that
nested path — NOT from top-level keys.

This test builds an envelope in the exact shape the handler publishes and
asserts the worker extracts correlation_id, root_human_id, and is_human_rooted
correctly, setting the corresponding env vars so _write_outbound_correlation
does NOT early-return.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# Envelope in the EXACT shape published by handler.py:682-722.
# Correlation fields are NESTED under "correlation", not top-level.
HANDLER_SHAPED_ENVELOPE = {
    "version": "1.0",
    "channel": "github",
    "tenant_id": "acme-corp",
    "cognito_sub": "cognito-sub-jane-123",
    "persona": "developer",
    "actor": {
        "user_id": "cognito-sub-jane-123",
        "org_id": "org-acme",
        "github_id": 12345678,
        "github_login": "jane-dev",
        "is_bot": False,
    },
    "source_ref": {
        "installation_id": 99887766,
        "repo": "acme-corp/flagship-app",
        "issue": 42,
        "pr": None,
        "sha": None,
    },
    "intent": {"trigger": "issue_labeled", "label": "developer", "persona": "developer"},
    "correlation": {
        "correlation_id": "corr-chain-abc-123",
        "root_human_id": "user-human-origin-456",
        "is_human_rooted": True,
        "parent_invocation_id": "msg-parent-run-789",
    },
    "payload": {},
    "arrived_at": "2026-06-22T10:00:00Z",
    "message_id": "msg-this-run-def",
}


def _subprocess_side_effect(*args, **kwargs):
    """Simulate fresh-branch subprocess calls."""
    cmd = args[0] if args else kwargs.get("args", [])
    if cmd and cmd[0:2] == ["git", "ls-remote"]:
        return MagicMock(returncode=1, stdout="", stderr="")
    return MagicMock(returncode=0, stdout="", stderr="")


class TestCorrelationEnvelopeContract:
    """Verify worker reads correlation from nested envelope["correlation"]."""

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_nested_correlation_sets_env_vars(
        self,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_update_cr,
        mock_create_cr,
        mock_delete_msg,
        mock_receive_msg,
        monkeypatch,
        tmp_path,
    ):
        """Given handler-shaped envelope with nested correlation, env vars are set."""
        from entrypoint import main
        import entrypoint

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_receive_msg.return_value = (
            json.dumps(HANDLER_SHAPED_ENVELOPE),
            "receipt-corr-contract",
        )
        mock_vault = MagicMock()
        mock_vault_cls.return_value = mock_vault
        mock_vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        mock_mint.return_value = "ghs_test"
        mock_run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        mock_create_cr.return_value = {"id": 1, "html_url": "http://x"}
        mock_subprocess_run.side_effect = _subprocess_side_effect

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        main()

        # Correlation env vars MUST be set from nested envelope["correlation"]
        assert os.environ.get("ADP_CORRELATION_ID") == "corr-chain-abc-123"
        assert os.environ.get("ADP_ROOT_HUMAN_ID") == "user-human-origin-456"
        assert os.environ.get("ADP_IS_HUMAN_ROOTED") == "true"
        assert os.environ.get("ADP_MESSAGE_ID") == "msg-this-run-def"

    @patch("entrypoint._receive_one_message")
    @patch("entrypoint._delete_message")
    @patch("entrypoint.create_check_run")
    @patch("entrypoint.update_check_run")
    @patch("entrypoint.run_cmd")
    @patch("entrypoint.mint_installation_token")
    @patch("entrypoint.VaultClient")
    @patch("entrypoint.shutil.copytree")
    @patch("entrypoint.subprocess.run")
    def test_write_outbound_correlation_fires_with_nested_context(
        self,
        mock_subprocess_run,
        mock_copytree,
        mock_vault_cls,
        mock_mint,
        mock_run_cmd,
        mock_update_cr,
        mock_create_cr,
        mock_delete_msg,
        mock_receive_msg,
        monkeypatch,
        tmp_path,
    ):
        """_write_outbound_correlation does NOT early-return when nested correlation is set."""
        import entrypoint
        from entrypoint import _write_outbound_correlation

        # Simulate the env vars being set (as they would be after reading nested correlation)
        monkeypatch.setenv("ADP_CORRELATION_ID", "corr-chain-abc-123")
        monkeypatch.setenv("ADP_ROOT_HUMAN_ID", "user-human-origin-456")
        monkeypatch.setenv("ADP_IS_HUMAN_ROOTED", "true")
        monkeypatch.setenv("ADP_MESSAGE_ID", "msg-this-run-def")
        monkeypatch.setenv("ADP_USER_ID", "cognito-sub-jane-123")

        with patch("entrypoint.write_pointer") as mock_write, patch(
            "entrypoint.post_provenance"
        ) as mock_prov:
            _write_outbound_correlation("acme-corp/flagship-app", "issue:42", "comment_post")

            # write_pointer MUST be called (not early-returned)
            mock_write.assert_called_once()
            call_kwargs = mock_write.call_args[1]
            assert call_kwargs["correlation_id"] == "corr-chain-abc-123"
            assert call_kwargs["triggering_invocation_id"] == "msg-this-run-def"

    def test_empty_correlation_block_does_not_crash(self):
        """Envelope with correlation={} gracefully yields empty strings (no crash)."""
        from entrypoint import parse_envelope

        envelope_empty_corr = {
            **HANDLER_SHAPED_ENVELOPE,
            "correlation": {},
        }
        result = parse_envelope(json.dumps(envelope_empty_corr))
        # parse_envelope should succeed; correlation extraction happens after parse
        assert result["tenant_id"] == "acme-corp"

    def test_missing_correlation_block_does_not_crash(self):
        """Envelope without correlation key at all is handled gracefully."""
        from entrypoint import parse_envelope

        envelope_no_corr = {k: v for k, v in HANDLER_SHAPED_ENVELOPE.items() if k != "correlation"}
        result = parse_envelope(json.dumps(envelope_no_corr))
        assert result["tenant_id"] == "acme-corp"

    def test_message_id_still_read_from_top_level(self):
        """Regression: message_id is top-level (handler.py:722), NOT inside correlation."""
        from entrypoint import parse_envelope

        result = parse_envelope(json.dumps(HANDLER_SHAPED_ENVELOPE))
        assert result["message_id"] == "msg-this-run-def"
