"""Tests for correlation_store.py — DDB pointer read/write helpers.

Issue #785: Phase 2-b — correlation pointer client for provenance chains.

Coverage:
  - channel_key builds correct format
  - read_pointer returns None for missing key
  - read_pointer returns data for existing key with ConsistentRead=True
  - write_pointer is idempotent and sets TTL
  - DDB error on read returns None without raising
  - DDB error on write logs warning without raising
  - Table not configured returns None / no-op
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add common/ to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture(autouse=True)
def _reset_module(monkeypatch):
    """Reset module state before each test."""
    monkeypatch.setenv("CORRELATION_POINTERS_TABLE", "test-correlation-pointers")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    # Force re-import to reset cached table
    mods_to_remove = [
        k for k in sys.modules if k.startswith("common.correlation_store")
    ]
    for mod in mods_to_remove:
        del sys.modules[mod]
    yield
    mods_to_remove = [
        k for k in sys.modules if k.startswith("common.correlation_store")
    ]
    for mod in mods_to_remove:
        del sys.modules[mod]


class TestChannelKey:
    def test_builds_correct_format(self):
        """channel_key follows Phase 2-a spec format."""
        from common import correlation_store

        key = correlation_store.channel_key("github", "aws-e/adp", "issue", 783)
        assert key == "github:repo=aws-e/adp,issue=783"

    def test_pr_kind(self):
        """Works with pull_request kind."""
        from common import correlation_store

        key = correlation_store.channel_key("github", "org/repo", "pull_request", 42)
        assert key == "github:repo=org/repo,pull_request=42"


class TestReadPointer:
    def test_returns_none_for_missing_key(self):
        """No item in DDB -> returns None."""
        from common import correlation_store

        correlation_store._ddb_table = None  # reset

        mock_table = MagicMock()
        mock_table.get_item.return_value = {}  # no 'Item' key

        with patch("boto3.resource") as mock_resource:
            mock_resource.return_value.Table.return_value = mock_table
            result = correlation_store.read_pointer("github:repo=test/repo,issue=1")

        assert result is None
        # Verify ConsistentRead=True was used
        mock_table.get_item.assert_called_once_with(
            Key={"channel_key": "github:repo=test/repo,issue=1"},
            ConsistentRead=True,
        )

    def test_returns_data_for_existing_key(self):
        """Existing item -> returns correlation data."""
        from common import correlation_store

        correlation_store._ddb_table = None

        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            "Item": {
                "channel_key": "github:repo=aws-e/adp,issue=783",
                "correlation_id": "corr-abc",
                "root_human_id": "user-123",
                "is_human_rooted": True,
                "expires_at": 9999999999,
            }
        }

        with patch("boto3.resource") as mock_resource:
            mock_resource.return_value.Table.return_value = mock_table
            result = correlation_store.read_pointer("github:repo=aws-e/adp,issue=783")

        assert result == {
            "correlation_id": "corr-abc",
            "root_human_id": "user-123",
            "is_human_rooted": True,
            "triggering_invocation_id": None,
            "chain_depth": None,
        }

    def test_returns_none_on_ddb_error(self):
        """DDB ClientError -> returns None, does not raise."""
        from botocore.exceptions import ClientError

        from common import correlation_store

        correlation_store._ddb_table = None

        mock_table = MagicMock()
        mock_table.get_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "DDB down"}},
            "GetItem",
        )

        with patch("boto3.resource") as mock_resource:
            mock_resource.return_value.Table.return_value = mock_table
            result = correlation_store.read_pointer("github:repo=test/x,issue=1")

        assert result is None

    def test_returns_none_when_table_not_configured(self, monkeypatch):
        """CORRELATION_POINTERS_TABLE empty -> returns None."""
        monkeypatch.setenv("CORRELATION_POINTERS_TABLE", "")
        mods = [k for k in sys.modules if k.startswith("common.correlation_store")]
        for m in mods:
            del sys.modules[m]

        from common import correlation_store

        correlation_store._ddb_table = None
        result = correlation_store.read_pointer("github:repo=x/y,issue=1")
        assert result is None


class TestWritePointer:
    def test_writes_item_with_ttl(self):
        """write_pointer puts item with correct TTL."""
        from common import correlation_store

        correlation_store._ddb_table = None

        mock_table = MagicMock()

        with patch("boto3.resource") as mock_resource:
            mock_resource.return_value.Table.return_value = mock_table
            before = int(time.time())
            correlation_store.write_pointer(
                key="github:repo=aws-e/adp,issue=783",
                correlation_id="corr-xyz",
                root_human_id="user-456",
                is_human_rooted=False,
                ttl_days=7,
            )

        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["channel_key"] == "github:repo=aws-e/adp,issue=783"
        assert item["correlation_id"] == "corr-xyz"
        assert item["root_human_id"] == "user-456"
        assert item["is_human_rooted"] is False
        # TTL should be ~7 days from now
        expected_min = before + (7 * 86400)
        assert item["expires_at"] >= expected_min
        assert item["expires_at"] <= expected_min + 2  # within 2s tolerance

    def test_idempotent_overwrite(self):
        """Calling write_pointer twice overwrites (upsert semantics)."""
        from common import correlation_store

        correlation_store._ddb_table = None

        mock_table = MagicMock()

        with patch("boto3.resource") as mock_resource:
            mock_resource.return_value.Table.return_value = mock_table
            correlation_store.write_pointer("key1", "corr-1", "user-1", True)
            correlation_store.write_pointer("key1", "corr-2", "user-1", True)

        assert mock_table.put_item.call_count == 2

    def test_does_not_raise_on_ddb_error(self):
        """DDB error -> logs warning, does not raise."""
        from botocore.exceptions import ClientError

        from common import correlation_store

        correlation_store._ddb_table = None

        mock_table = MagicMock()
        mock_table.put_item.side_effect = ClientError(
            {
                "Error": {
                    "Code": "ProvisionedThroughputExceededException",
                    "Message": "throttled",
                }
            },
            "PutItem",
        )

        with (
            patch("boto3.resource") as mock_resource,
            patch.object(correlation_store, "_emit_write_failed_metric") as mock_metric,
        ):
            mock_resource.return_value.Table.return_value = mock_table
            # Should NOT raise
            correlation_store.write_pointer("key1", "corr-1", "user-1", True)

        mock_metric.assert_called_once()

    def test_no_op_when_table_not_configured(self, monkeypatch):
        """CORRELATION_POINTERS_TABLE empty -> no-op."""
        monkeypatch.setenv("CORRELATION_POINTERS_TABLE", "")
        mods = [k for k in sys.modules if k.startswith("common.correlation_store")]
        for m in mods:
            del sys.modules[m]

        from common import correlation_store

        correlation_store._ddb_table = None
        # Should not raise
        correlation_store.write_pointer("key1", "corr-1", "user-1", True)
