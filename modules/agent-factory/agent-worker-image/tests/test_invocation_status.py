"""Unit tests for lib/invocation_status.py."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import invocation_status


class TestUpdateStatus:
    """Tests for update_status()."""

    def setup_method(self):
        # Reset the module-level client and cached table name between tests
        invocation_status._ddb = None
        invocation_status._table_name = ""

    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": ""})
    def test_no_op_when_table_not_configured(self):
        """Should silently return when table env var is empty."""
        # Should not raise
        invocation_status.update_status(
            event_id="msg-123",
            arrived_at="2026-06-13T22:00:00Z",
            status="in_progress",
        )

    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "test-table"})
    def test_no_op_when_event_id_empty(self):
        """Should silently return when event_id is empty."""
        invocation_status.update_status(
            event_id="",
            arrived_at="2026-06-13T22:00:00Z",
            status="in_progress",
        )

    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "test-table"})
    def test_no_op_when_arrived_at_empty(self):
        """Should silently return when arrived_at is empty."""
        invocation_status.update_status(
            event_id="msg-123",
            arrived_at="",
            status="in_progress",
        )

    @patch("lib.invocation_status._get_client")
    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "test-table"})
    def test_update_item_called_with_correct_key(self, mock_get_client):
        """UpdateItem uses event_id (PK) + arrived_at (SK) from envelope."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        invocation_status.update_status(
            event_id="msg-uuid-abc",
            arrived_at="2026-06-13T22:37:58Z",
            status="in_progress",
            run_id="agent-scaledjob-xyz-12345",
        )

        # in_progress gets 1 call on success (no retry needed)
        mock_client.update_item.assert_called_once()
        call_kwargs = mock_client.update_item.call_args[1]
        assert call_kwargs["TableName"] == "test-table"
        assert call_kwargs["Key"] == {
            "event_id": {"S": "msg-uuid-abc"},
            "arrived_at": {"S": "2026-06-13T22:37:58Z"},
        }
        # Verify ConditionExpression prevents orphan creates
        assert call_kwargs["ConditionExpression"] == "attribute_exists(event_id)"

    @patch("lib.invocation_status._get_client")
    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "test-table"})
    def test_update_sets_status_and_timestamp(self, mock_get_client):
        """UpdateItem sets status and status_updated_at."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        invocation_status.update_status(
            event_id="msg-123",
            arrived_at="2026-06-13T22:00:00Z",
            status="complete",
        )

        call_kwargs = mock_client.update_item.call_args[1]
        expr_values = call_kwargs["ExpressionAttributeValues"]
        assert expr_values[":status"] == {"S": "complete"}
        assert ":status_updated_at" in expr_values

    @patch("lib.invocation_status._get_client")
    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "test-table"})
    def test_update_includes_run_id_when_provided(self, mock_get_client):
        """run_id is included in the update expression when provided."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        invocation_status.update_status(
            event_id="msg-123",
            arrived_at="2026-06-13T22:00:00Z",
            status="in_progress",
            run_id="agent-scaledjob-xyz",
        )

        call_kwargs = mock_client.update_item.call_args[1]
        expr_values = call_kwargs["ExpressionAttributeValues"]
        assert ":run_id" in expr_values
        assert expr_values[":run_id"] == {"S": "agent-scaledjob-xyz"}
        assert "run_id" in call_kwargs["UpdateExpression"]

    @patch("lib.invocation_status._get_client")
    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "test-table"})
    def test_update_includes_summary_when_provided(self, mock_get_client):
        """summary is included in the update expression when provided."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        invocation_status.update_status(
            event_id="msg-123",
            arrived_at="2026-06-13T22:00:00Z",
            status="failed",
            summary="developer — exit code 1",
        )

        call_kwargs = mock_client.update_item.call_args[1]
        expr_values = call_kwargs["ExpressionAttributeValues"]
        assert ":summary" in expr_values
        assert expr_values[":summary"] == {"S": "developer — exit code 1"}

    @patch("lib.invocation_status.time.sleep")
    @patch("lib.invocation_status._get_client")
    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "test-table"})
    def test_conditional_check_failed_retries_for_in_progress(self, mock_get_client, mock_sleep):
        """in_progress status retries once on ConditionalCheckFailed before giving up."""
        mock_client = MagicMock()
        # Simulate ConditionalCheckFailedException on all attempts
        error_response = {"Error": {"Code": "ConditionalCheckFailedException"}}
        from botocore.exceptions import ClientError

        mock_client.exceptions.ConditionalCheckFailedException = type(
            "ConditionalCheckFailedException", (ClientError,), {}
        )
        mock_client.update_item.side_effect = (
            mock_client.exceptions.ConditionalCheckFailedException(error_response, "UpdateItem")
        )
        mock_get_client.return_value = mock_client

        # Should NOT raise — retries once then gives up silently
        invocation_status.update_status(
            event_id="nonexistent-msg",
            arrived_at="2026-06-13T22:00:00Z",
            status="in_progress",
        )
        # in_progress gets 2 attempts (1 initial + 1 retry)
        assert mock_client.update_item.call_count == 2
        mock_sleep.assert_called_once_with(2)

    @patch("lib.invocation_status._get_client")
    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "test-table"})
    def test_conditional_check_failed_no_retry_for_complete(self, mock_get_client):
        """complete status does NOT retry on ConditionalCheckFailed."""
        mock_client = MagicMock()
        error_response = {"Error": {"Code": "ConditionalCheckFailedException"}}
        from botocore.exceptions import ClientError

        mock_client.exceptions.ConditionalCheckFailedException = type(
            "ConditionalCheckFailedException", (ClientError,), {}
        )
        mock_client.update_item.side_effect = (
            mock_client.exceptions.ConditionalCheckFailedException(error_response, "UpdateItem")
        )
        mock_get_client.return_value = mock_client

        # Should NOT raise — gives up after 1 attempt (no retry for non-in_progress)
        invocation_status.update_status(
            event_id="nonexistent-msg",
            arrived_at="2026-06-13T22:00:00Z",
            status="complete",
        )
        mock_client.update_item.assert_called_once()

    @patch("lib.invocation_status._get_client")
    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "test-table"})
    def test_fail_soft_on_dynamodb_error(self, mock_get_client):
        """Should log warning but not raise on DDB errors."""
        mock_client = MagicMock()
        mock_client.update_item.side_effect = Exception("DDB unavailable")
        mock_client.exceptions.ConditionalCheckFailedException = type(
            "ConditionalCheckFailedException", (Exception,), {}
        )
        mock_get_client.return_value = mock_client

        # Should not raise
        invocation_status.update_status(
            event_id="msg-123",
            arrived_at="2026-06-13T22:00:00Z",
            status="complete",
        )

    @patch("lib.invocation_status._get_client")
    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "test-table"})
    def test_update_includes_transcript_key_when_provided(self, mock_get_client):
        """Issue #3069: transcript_key is included in the update expression when provided."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        invocation_status.update_status(
            event_id="msg-123",
            arrived_at="2026-06-13T22:00:00Z",
            status="complete",
            transcript_key="developer/org/repo/issue-42/20260706T150000Z-abc12345.md",
        )

        call_kwargs = mock_client.update_item.call_args[1]
        expr_values = call_kwargs["ExpressionAttributeValues"]
        assert ":transcript_key" in expr_values
        assert expr_values[":transcript_key"] == {"S": "developer/org/repo/issue-42/20260706T150000Z-abc12345.md"}
        assert "transcript_key" in call_kwargs["UpdateExpression"]

    @patch("lib.invocation_status._get_client")
    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "test-table"})
    def test_update_omits_transcript_key_when_none(self, mock_get_client):
        """Issue #3069: transcript_key is NOT included when None (back-compat)."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        invocation_status.update_status(
            event_id="msg-123",
            arrived_at="2026-06-13T22:00:00Z",
            status="complete",
        )

        call_kwargs = mock_client.update_item.call_args[1]
        expr_values = call_kwargs["ExpressionAttributeValues"]
        assert ":transcript_key" not in expr_values
        assert "transcript_key" not in call_kwargs["UpdateExpression"]
