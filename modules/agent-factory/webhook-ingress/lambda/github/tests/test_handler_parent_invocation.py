"""Tests for parent_invocation_id propagation in handler.py.

Issue #1460: determine_correlation returns parent_invocation_id from DDB pointer
for bot events inheriting a chain.

Coverage:
  - Human sender: parent_invocation_id is None (chain root)
  - Bot sender inheriting pointer with triggering_invocation_id: gets parent_invocation_id
  - Bot sender inheriting pointer without triggering_invocation_id (old pointer): None
  - Bot sender with no pointer: parent_invocation_id is None (new bot chain)
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch


# Add parent directories to path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Set required env vars before importing handler
os.environ.setdefault("WEBHOOK_SECRET", "test-secret-123")
os.environ.setdefault("WEBHOOK_SECRET_ARN", "")
os.environ.setdefault("SUBMIT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q.fifo")
os.environ.setdefault("IDENTITY_INDEX_TABLE", "test-identity-index")
os.environ.setdefault("RATE_LIMITS_TABLE", "test-rate-limits")
os.environ.setdefault("AWS_REGION", "us-east-1")


class _MockIdentity:
    """Minimal mock for resolved identity."""

    def __init__(self, user_id: str, user_kind: str):
        self.user_id = user_id
        self.user_kind = user_kind


class TestDetermineCorrelationParentInvocation:
    """Tests for parent_invocation_id in determine_correlation."""

    def test_human_sender_parent_is_none(self):
        """Human senders always start new chains with no parent."""
        from handler import determine_correlation

        identity = _MockIdentity(user_id="user-human-1", user_kind="human")
        result = determine_correlation(
            payload={},
            resolved_identity=identity,
            channel_key="github:repo=org/repo,issue=1",
        )

        assert result["parent_invocation_id"] is None
        assert result["is_new_chain"] is True
        assert result["is_human_rooted"] is True

    def test_bot_inherits_triggering_invocation_id_from_pointer(self):
        """Bot sender with pointer carrying triggering_invocation_id gets parent."""
        from handler import determine_correlation

        identity = _MockIdentity(user_id="user-bot-1", user_kind="bot")

        mock_pointer = {
            "correlation_id": "corr-chain-abc",
            "root_human_id": "user-human-1",
            "is_human_rooted": True,
            "triggering_invocation_id": "msg-upstream-run-xyz",
        }

        with patch("handler._get_correlation_store") as mock_store:
            mock_store.return_value.read_pointer.return_value = mock_pointer
            result = determine_correlation(
                payload={},
                resolved_identity=identity,
                channel_key="github:repo=org/repo,issue=1",
            )

        assert result["parent_invocation_id"] == "msg-upstream-run-xyz"
        assert result["correlation_id"] == "corr-chain-abc"
        assert result["is_new_chain"] is False
        assert result["triggered_by"] == "user-bot-1"

    def test_bot_inherits_none_parent_from_old_pointer(self):
        """Bot sender with old pointer (no triggering_invocation_id) gets None parent."""
        from handler import determine_correlation

        identity = _MockIdentity(user_id="user-bot-2", user_kind="bot")

        # Old pointer format without triggering_invocation_id
        mock_pointer = {
            "correlation_id": "corr-old-chain",
            "root_human_id": "user-human-2",
            "is_human_rooted": True,
            "triggering_invocation_id": None,
        }

        with patch("handler._get_correlation_store") as mock_store:
            mock_store.return_value.read_pointer.return_value = mock_pointer
            result = determine_correlation(
                payload={},
                resolved_identity=identity,
                channel_key="github:repo=org/repo,issue=2",
            )

        assert result["parent_invocation_id"] is None
        assert result["correlation_id"] == "corr-old-chain"
        assert result["is_new_chain"] is False

    def test_bot_no_pointer_parent_is_none(self):
        """Bot sender with no DDB pointer starts new bot chain with no parent."""
        from handler import determine_correlation

        identity = _MockIdentity(user_id="user-bot-3", user_kind="bot")

        with patch("handler._get_correlation_store") as mock_store:
            mock_store.return_value.read_pointer.return_value = None
            result = determine_correlation(
                payload={},
                resolved_identity=identity,
                channel_key="github:repo=org/repo,issue=3",
            )

        assert result["parent_invocation_id"] is None
        assert result["is_new_chain"] is True
        assert result["is_human_rooted"] is False
