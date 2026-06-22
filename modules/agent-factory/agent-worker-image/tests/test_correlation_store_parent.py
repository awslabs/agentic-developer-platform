"""Tests for triggering_invocation_id in worker lib/correlation_store.py.

Issue #1460: Worker outbound writes include the producing run's message_id
as triggering_invocation_id on the DDB pointer.

Issue #1661: Attribute names must match webhook reader (no latest_ prefix).

Coverage:
  - write_pointer includes triggering_invocation_id when provided
  - write_pointer omits triggering_invocation_id when None/empty
  - Fail-soft behavior preserved with new parameter
  - Round-trip: worker write produces item that webhook read_pointer can parse
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import correlation_store


class TestWritePointerTriggeringInvocationId:
    """Tests for triggering_invocation_id param on worker write_pointer."""

    def setup_method(self):
        correlation_store._ddb = None

    @patch("lib.correlation_store._get_client")
    @patch.dict(os.environ, {"CORRELATION_POINTERS_TABLE": "test-table"})
    def test_includes_triggering_invocation_id(self, mock_get_client):
        """write_pointer puts triggering_invocation_id in DDB item."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        correlation_store.write_pointer(
            channel_key="github:repo=org/repo,issue=42",
            correlation_id="corr-abc",
            root_human_id="user-xyz",
            is_human_rooted=True,
            triggering_invocation_id="msg-run-parent-123",
        )

        mock_client.put_item.assert_called_once()
        item = mock_client.put_item.call_args[1]["Item"]
        assert item["triggering_invocation_id"] == {"S": "msg-run-parent-123"}

    @patch("lib.correlation_store._get_client")
    @patch.dict(os.environ, {"CORRELATION_POINTERS_TABLE": "test-table"})
    def test_omits_triggering_invocation_id_when_none(self, mock_get_client):
        """write_pointer does not include triggering_invocation_id when None."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        correlation_store.write_pointer(
            channel_key="github:repo=org/repo,issue=43",
            correlation_id="corr-def",
            root_human_id="user-abc",
            is_human_rooted=False,
            triggering_invocation_id=None,
        )

        item = mock_client.put_item.call_args[1]["Item"]
        assert "triggering_invocation_id" not in item

    @patch("lib.correlation_store._get_client")
    @patch.dict(os.environ, {"CORRELATION_POINTERS_TABLE": "test-table"})
    def test_omits_triggering_invocation_id_when_empty(self, mock_get_client):
        """write_pointer does not include triggering_invocation_id when empty string."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        correlation_store.write_pointer(
            channel_key="github:repo=org/repo,issue=44",
            correlation_id="corr-ghi",
            root_human_id="user-def",
            is_human_rooted=True,
            triggering_invocation_id="",
        )

        item = mock_client.put_item.call_args[1]["Item"]
        assert "triggering_invocation_id" not in item

    @patch("lib.correlation_store._get_client")
    @patch.dict(os.environ, {"CORRELATION_POINTERS_TABLE": "test-table"})
    def test_fail_soft_with_triggering_invocation_id(self, mock_get_client):
        """DDB error with triggering_invocation_id still doesn't raise."""
        mock_client = MagicMock()
        mock_client.put_item.side_effect = Exception("DDB unavailable")
        mock_get_client.return_value = mock_client

        # Should not raise
        correlation_store.write_pointer(
            channel_key="github:repo=org/repo,issue=45",
            correlation_id="corr-jkl",
            root_human_id="user-ghi",
            is_human_rooted=True,
            triggering_invocation_id="msg-run-456",
        )


class TestRoundTripCompatibility:
    """Verify worker writes produce items the webhook reader can parse.

    Issue #1661: The webhook's read_pointer expects attributes named
    correlation_id, root_human_id, is_human_rooted (no 'latest_' prefix).
    This test simulates the round-trip: worker writes an item using boto3
    low-level client format, then we verify the attribute names match what
    the webhook's read_pointer would access.
    """

    def setup_method(self):
        correlation_store._ddb = None

    @patch("lib.correlation_store._get_client")
    @patch.dict(os.environ, {"CORRELATION_POINTERS_TABLE": "test-table"})
    def test_worker_item_has_webhook_compatible_attributes(self, mock_get_client):
        """Worker-written DDB item has exact attributes the webhook reads."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        correlation_store.write_pointer(
            channel_key="github:repo=aws-e/adp,issue=1320",
            correlation_id="chain-abc",
            root_human_id="user-human",
            is_human_rooted=True,
            triggering_invocation_id="inv-AAA",
        )

        item = mock_client.put_item.call_args[1]["Item"]

        # These are the exact attribute names the webhook's read_pointer accesses:
        # item["correlation_id"], item["root_human_id"], item.get("is_human_rooted"),
        # item.get("triggering_invocation_id")
        assert "correlation_id" in item
        assert "root_human_id" in item
        assert "is_human_rooted" in item
        assert "triggering_invocation_id" in item

        # Must NOT have the old prefixed names that would be invisible to webhook
        assert "latest_correlation_id" not in item
        assert "latest_root_human_id" not in item
        assert "latest_is_human_rooted" not in item

        # Verify values (low-level DDB format)
        assert item["correlation_id"] == {"S": "chain-abc"}
        assert item["root_human_id"] == {"S": "user-human"}
        assert item["is_human_rooted"] == {"BOOL": True}
        assert item["triggering_invocation_id"] == {"S": "inv-AAA"}
