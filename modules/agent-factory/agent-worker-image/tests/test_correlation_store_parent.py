"""Tests for triggering_invocation_id in worker lib/correlation_store.py.

Issue #1460: Worker outbound writes include the producing run's message_id
as triggering_invocation_id on the DDB pointer.

Coverage:
  - write_pointer includes triggering_invocation_id when provided
  - write_pointer omits triggering_invocation_id when None/empty
  - Fail-soft behavior preserved with new parameter
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
            channel_key="github:org/repo:issue:42",
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
            channel_key="github:org/repo:issue:43",
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
            channel_key="github:org/repo:issue:44",
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
            channel_key="github:org/repo:issue:45",
            correlation_id="corr-jkl",
            root_human_id="user-ghi",
            is_human_rooted=True,
            triggering_invocation_id="msg-run-456",
        )
