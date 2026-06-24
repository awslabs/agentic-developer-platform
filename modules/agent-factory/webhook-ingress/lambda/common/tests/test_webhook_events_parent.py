"""Regression test for issue #1750 — parent_invocation_id must be PERSISTED.

determine_correlation computes parent_invocation_id and it flows to SQS +
provenance, but WebhookEventLogger.log_event never wrote it to the
webhook-events row — leaving parent_invocation_id null on EVERY run, every
path, all-time. The Activity chain view reads this row, so lineage never
appeared. This test guards the write.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from common.webhook_events import WebhookEventLogger


def _logger_with_mock_table():
    with patch("common.webhook_events.boto3"):
        logger = WebhookEventLogger("adp-dev-webhook-events")
    mock_table = MagicMock()
    logger._table = mock_table
    return logger, mock_table


class TestParentInvocationIdPersisted:
    def test_parent_invocation_id_written_to_item(self):
        logger, table = _logger_with_mock_table()
        logger.log_event(
            event_id="child-evt",
            arrived_at="2026-06-24T00:00:00Z",
            tenant_id="t",
            channel="github",
            event_type="pull_request",
            action="opened",
            status="webhook_received",
            correlation_id="chain-X",
            parent_invocation_id="parent-evt-123",
            chain_depth=1,
        )
        item = table.put_item.call_args[1]["Item"]
        assert item["parent_invocation_id"] == "parent-evt-123"
        assert item["chain_depth"] == 1
        assert item["correlation_id"] == "chain-X"

    def test_parent_absent_when_not_provided(self):
        """No parent (e.g. human-rooted run) → field omitted, not null-written."""
        logger, table = _logger_with_mock_table()
        logger.log_event(
            event_id="root-evt",
            arrived_at="2026-06-24T00:00:00Z",
            tenant_id="t",
            channel="github",
            event_type="issues",
            action="labeled",
            status="webhook_received",
            correlation_id="chain-Y",
        )
        item = table.put_item.call_args[1]["Item"]
        assert "parent_invocation_id" not in item

    def test_chain_depth_zero_is_written(self):
        """chain_depth=0 (root) must be written (it's not None)."""
        logger, table = _logger_with_mock_table()
        logger.log_event(
            event_id="e",
            arrived_at="2026-06-24T00:00:00Z",
            tenant_id="t",
            channel="github",
            event_type="issues",
            action="labeled",
            status="webhook_received",
            chain_depth=0,
        )
        item = table.put_item.call_args[1]["Item"]
        assert item["chain_depth"] == 0
