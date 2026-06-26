"""Unit tests for lib/correlation_store.py."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import correlation_store


class TestChannelKey:
    """Tests for channel_key() — must produce the exact same string as the webhook."""

    def test_issue_key_format(self):
        """channel_key matches Phase 2-a canonical format for issues."""
        key = correlation_store.channel_key("github", "aws-e/adp", "issue", 1320)
        assert key == "github:repo=aws-e/adp,issue=1320"

    def test_pr_key_format(self):
        """channel_key matches Phase 2-a canonical format for PRs."""
        key = correlation_store.channel_key("github", "aws-e/adp", "pull_request", 42)
        assert key == "github:repo=aws-e/adp,pull_request=42"

    def test_different_provider(self):
        """channel_key works with arbitrary providers."""
        key = correlation_store.channel_key("gitlab", "org/repo", "issue", 99)
        assert key == "gitlab:repo=org/repo,issue=99"

    def test_parity_with_webhook_format(self):
        """Pinned-string parity: worker produces the EXACT same key the webhook reads.

        This test pins the format to prevent drift between the two deploy bundles.
        If this test breaks, the webhook-ingress side MUST be checked/updated too.
        See issue #1661 for the original key-format mismatch bug.
        """
        # These are the exact strings the webhook's correlation_store.channel_key() produces
        assert (
            correlation_store.channel_key("github", "aws-e/adp", "issue", 783)
            == "github:repo=aws-e/adp,issue=783"
        )
        assert (
            correlation_store.channel_key("github", "org/repo", "pull_request", 42)
            == "github:repo=org/repo,pull_request=42"
        )


class TestWritePointer:
    """Tests for write_pointer()."""

    def setup_method(self):
        # Reset the module-level client between tests
        correlation_store._ddb = None

    @patch.dict(os.environ, {"CORRELATION_POINTERS_TABLE": ""})
    def test_no_op_when_table_not_configured(self):
        """Should silently return when table env var is empty."""
        # Should not raise
        correlation_store.write_pointer(
            channel_key="github:repo=org/repo,issue=1",
            correlation_id="corr-123",
            root_human_id="user-456",
            is_human_rooted=True,
        )

    @patch("lib.correlation_store._get_client")
    @patch.dict(os.environ, {"CORRELATION_POINTERS_TABLE": "test-table"})
    def test_writes_item_to_dynamodb(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        correlation_store.write_pointer(
            channel_key="github:repo=org/repo,issue=42",
            correlation_id="corr-abc",
            root_human_id="user-xyz",
            is_human_rooted=True,
            ttl_days=7,
        )

        # Issue #1716: write now uses update_item (not put_item) so it never
        # erases webhook-managed fields like last_triggered_persona.
        mock_client.update_item.assert_called_once()
        call_kwargs = mock_client.update_item.call_args[1]
        assert call_kwargs["TableName"] == "test-table"
        assert call_kwargs["Key"] == {"channel_key": {"S": "github:repo=org/repo,issue=42"}}
        vals = call_kwargs["ExpressionAttributeValues"]
        # Issue #1661: attribute names must match what the webhook reads
        assert vals[":cid"] == {"S": "corr-abc"}
        assert vals[":rh"] == {"S": "user-xyz"}
        assert vals[":hr"] == {"BOOL": True}
        assert ":ua" in vals  # updated_at
        assert ":ea" in vals  # expires_at
        # When last_triggered_persona is NOT passed, update_item must NOT include
        # it in the expression, so an existing value is preserved (issue #1716).
        assert "last_triggered_persona" not in call_kwargs["UpdateExpression"]

    @patch("lib.correlation_store._get_client")
    @patch.dict(os.environ, {"CORRELATION_POINTERS_TABLE": "test-table"})
    def test_writes_last_triggered_persona_when_passed(self, mock_get_client):
        """Issue #2149: write_pointer includes last_triggered_persona in the
        update expression when explicitly passed (cross-issue dispatch seeding)."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        correlation_store.write_pointer(
            channel_key="github:repo=org/repo,issue=42",
            correlation_id="corr-abc",
            root_human_id="user-xyz",
            is_human_rooted=True,
            last_triggered_persona="developer",
        )

        mock_client.update_item.assert_called_once()
        call_kwargs = mock_client.update_item.call_args[1]
        assert "last_triggered_persona" in call_kwargs["UpdateExpression"]
        vals = call_kwargs["ExpressionAttributeValues"]
        assert vals[":ltp"] == {"S": "developer"}

    @patch("lib.correlation_store._get_client")
    @patch.dict(os.environ, {"CORRELATION_POINTERS_TABLE": "test-table"})
    def test_fail_soft_on_dynamodb_error(self, mock_get_client):
        """Should log warning but not raise on DDB errors."""
        mock_client = MagicMock()
        mock_client.update_item.side_effect = Exception("DDB unavailable")
        mock_get_client.return_value = mock_client

        # Should not raise
        correlation_store.write_pointer(
            channel_key="github:repo=org/repo,issue=1",
            correlation_id="corr-123",
            root_human_id="user-456",
            is_human_rooted=False,
        )

    @patch("lib.correlation_store._get_client")
    @patch.dict(os.environ, {"CORRELATION_POINTERS_TABLE": "test-table"})
    def test_ttl_calculation(self, mock_get_client):
        """TTL should be now + ttl_days * 86400."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        correlation_store.write_pointer(
            channel_key="github:repo=org/repo,issue=1",
            correlation_id="corr-123",
            root_human_id="user-456",
            is_human_rooted=True,
            ttl_days=14,
        )

        vals = mock_client.update_item.call_args[1]["ExpressionAttributeValues"]
        updated_at = int(vals[":ua"]["N"])
        expires_at = int(vals[":ea"]["N"])
        assert expires_at - updated_at == 14 * 86400
