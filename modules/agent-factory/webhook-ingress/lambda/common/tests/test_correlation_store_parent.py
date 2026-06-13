"""Tests for triggering_invocation_id in correlation_store.py.

Issue #1460: Parent edge for agent-to-agent lineage — DDB pointer
carries triggering_invocation_id so the next inbound webhook can set
parent_invocation_id on the child run's provenance.

Coverage:
  - write_pointer includes triggering_invocation_id when provided
  - write_pointer omits triggering_invocation_id when None
  - read_pointer returns triggering_invocation_id from item
  - read_pointer returns None triggering_invocation_id when field absent (old pointer)
  - All writes remain fail-soft
"""

import sys
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


class TestWritePointerTriggeringInvocationId:
    def test_includes_triggering_invocation_id_when_provided(self):
        """write_pointer puts triggering_invocation_id in DDB item."""
        from common import correlation_store

        correlation_store._ddb_table = None

        mock_table = MagicMock()

        with patch("boto3.resource") as mock_resource:
            mock_resource.return_value.Table.return_value = mock_table
            correlation_store.write_pointer(
                key="github:repo=aws-e/adp,issue=100",
                correlation_id="corr-xyz",
                root_human_id="user-456",
                is_human_rooted=True,
                triggering_invocation_id="msg-parent-run-123",
            )

        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["triggering_invocation_id"] == "msg-parent-run-123"
        # Other fields still present
        assert item["correlation_id"] == "corr-xyz"
        assert item["root_human_id"] == "user-456"

    def test_omits_triggering_invocation_id_when_none(self):
        """write_pointer does not include triggering_invocation_id when None."""
        from common import correlation_store

        correlation_store._ddb_table = None

        mock_table = MagicMock()

        with patch("boto3.resource") as mock_resource:
            mock_resource.return_value.Table.return_value = mock_table
            correlation_store.write_pointer(
                key="github:repo=aws-e/adp,issue=101",
                correlation_id="corr-abc",
                root_human_id="user-789",
                is_human_rooted=False,
                triggering_invocation_id=None,
            )

        item = mock_table.put_item.call_args[1]["Item"]
        assert "triggering_invocation_id" not in item

    def test_omits_triggering_invocation_id_when_empty_string(self):
        """write_pointer does not include triggering_invocation_id when empty."""
        from common import correlation_store

        correlation_store._ddb_table = None

        mock_table = MagicMock()

        with patch("boto3.resource") as mock_resource:
            mock_resource.return_value.Table.return_value = mock_table
            correlation_store.write_pointer(
                key="github:repo=aws-e/adp,issue=102",
                correlation_id="corr-def",
                root_human_id="user-000",
                is_human_rooted=True,
                triggering_invocation_id="",
            )

        item = mock_table.put_item.call_args[1]["Item"]
        assert "triggering_invocation_id" not in item

    def test_write_with_triggering_invocation_id_is_fail_soft(self):
        """DDB error with triggering_invocation_id still doesn't raise."""
        from botocore.exceptions import ClientError

        from common import correlation_store

        correlation_store._ddb_table = None

        mock_table = MagicMock()
        mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "DDB down"}},
            "PutItem",
        )

        with (
            patch("boto3.resource") as mock_resource,
            patch.object(correlation_store, "_emit_write_failed_metric"),
        ):
            mock_resource.return_value.Table.return_value = mock_table
            # Should NOT raise
            correlation_store.write_pointer(
                "key1",
                "corr-1",
                "user-1",
                True,
                triggering_invocation_id="msg-abc",
            )


class TestReadPointerTriggeringInvocationId:
    def test_returns_triggering_invocation_id_from_item(self):
        """read_pointer includes triggering_invocation_id when present in DDB."""
        from common import correlation_store

        correlation_store._ddb_table = None

        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            "Item": {
                "channel_key": "github:repo=aws-e/adp,issue=100",
                "correlation_id": "corr-abc",
                "root_human_id": "user-123",
                "is_human_rooted": True,
                "triggering_invocation_id": "msg-upstream-run-456",
                "expires_at": 9999999999,
            }
        }

        with patch("boto3.resource") as mock_resource:
            mock_resource.return_value.Table.return_value = mock_table
            result = correlation_store.read_pointer("github:repo=aws-e/adp,issue=100")

        assert result is not None
        assert result["triggering_invocation_id"] == "msg-upstream-run-456"
        assert result["correlation_id"] == "corr-abc"

    def test_returns_none_triggering_invocation_id_when_absent(self):
        """read_pointer returns None for triggering_invocation_id on old pointers."""
        from common import correlation_store

        correlation_store._ddb_table = None

        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            "Item": {
                "channel_key": "github:repo=aws-e/adp,issue=200",
                "correlation_id": "corr-old",
                "root_human_id": "user-old",
                "is_human_rooted": True,
                "expires_at": 9999999999,
                # No triggering_invocation_id — pre-#1460 pointer
            }
        }

        with patch("boto3.resource") as mock_resource:
            mock_resource.return_value.Table.return_value = mock_table
            result = correlation_store.read_pointer("github:repo=aws-e/adp,issue=200")

        assert result is not None
        assert result["triggering_invocation_id"] is None
