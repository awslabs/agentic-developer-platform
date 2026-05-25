"""Unit tests for lib/correlation_store.py."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import correlation_store


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
            channel_key="github:org/repo:issue:1",
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
            channel_key="github:org/repo:issue:42",
            correlation_id="corr-abc",
            root_human_id="user-xyz",
            is_human_rooted=True,
            ttl_days=7,
        )

        mock_client.put_item.assert_called_once()
        call_kwargs = mock_client.put_item.call_args[1]
        assert call_kwargs["TableName"] == "test-table"
        item = call_kwargs["Item"]
        assert item["channel_key"] == {"S": "github:org/repo:issue:42"}
        assert item["latest_correlation_id"] == {"S": "corr-abc"}
        assert item["latest_root_human_id"] == {"S": "user-xyz"}
        assert item["latest_is_human_rooted"] == {"BOOL": True}
        assert "updated_at" in item
        assert "expires_at" in item

    @patch("lib.correlation_store._get_client")
    @patch.dict(os.environ, {"CORRELATION_POINTERS_TABLE": "test-table"})
    def test_fail_soft_on_dynamodb_error(self, mock_get_client):
        """Should log warning but not raise on DDB errors."""
        mock_client = MagicMock()
        mock_client.put_item.side_effect = Exception("DDB unavailable")
        mock_get_client.return_value = mock_client

        # Should not raise
        correlation_store.write_pointer(
            channel_key="github:org/repo:issue:1",
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
            channel_key="github:org/repo:issue:1",
            correlation_id="corr-123",
            root_human_id="user-456",
            is_human_rooted=True,
            ttl_days=14,
        )

        item = mock_client.put_item.call_args[1]["Item"]
        updated_at = int(item["updated_at"]["N"])
        expires_at = int(item["expires_at"]["N"])
        assert expires_at - updated_at == 14 * 86400
