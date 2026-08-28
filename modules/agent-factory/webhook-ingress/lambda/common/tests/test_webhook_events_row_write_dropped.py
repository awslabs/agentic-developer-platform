"""Issue #4347 — a dropped webhook-event row write must be VISIBLE, not silent.

The row write in ``webhook_events.log_event`` is best-effort by design: the
``put_item`` is wrapped in a try/except that logs and swallows, so audit logging
can never block a webhook response. That is correct while the row is only an
audit record — losing one audit line beats failing a delivery.

Under #4187 enforce the SAME row becomes the run's **authorization** record
(``run_binding.verify_row_matches_caller`` reads it on the model-call path), and
the two roles have opposite failure requirements. A swallowed write then means
the run dispatches successfully and is denied on *every* model call for its whole
lifetime, with no retry path: ``unknown_run`` is deliberately not negative-cached,
but the row never appears either, so re-lookup never succeeds. Silently.

These tests pin the two properties that make the fix trustworthy:

1. A dropped write emits an alertable signal (#4337 T23) — that's the bug.
2. A successful write emits NOTHING, so the metric can be alarmed at
   "> 0" without a false positive on every healthy webhook.

Plus the two non-regressions that keep this an observability change rather than a
behavioural one: the drop still does not raise (option 1, fail-the-spawn, is
deliberately deferred), and a failure inside the metric path itself is swallowed
so the fix cannot become a worse outage than the silence it removes.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from common import webhook_events
from common.webhook_events import WebhookEventLogger


def _logger_with_failing_table(exc: Exception | None = None):
    """A logger whose put_item raises — the dropped-write condition under test."""
    with patch("common.webhook_events.boto3"):
        logger = WebhookEventLogger("adp-dev-webhook-events")
    mock_table = MagicMock()
    mock_table.put_item.side_effect = exc or RuntimeError("DDB unavailable")
    logger._table = mock_table
    return logger


def _logger_with_working_table():
    """A logger whose put_item succeeds — the happy path."""
    with patch("common.webhook_events.boto3"):
        logger = WebhookEventLogger("adp-dev-webhook-events")
    logger._table = MagicMock()
    return logger


def _log(logger, **overrides):
    """log_event with the boilerplate filled in, so tests show only what they test."""
    kwargs = {
        "event_id": "evt-1",
        "arrived_at": "2026-08-28T00:00:00Z",
        "tenant_id": "acme-corp",
        "channel": "github",
        "event_type": "issues",
        "action": "labeled",
        "status": "webhook_received",
    }
    kwargs.update(overrides)
    return logger.log_event(**kwargs)


class TestDroppedWriteIsVisible:
    """#4337 T23: the swallow must produce a signal an operator can alarm on."""

    def test_dropped_write_emits_metric(self) -> None:
        logger = _logger_with_failing_table()

        with patch.object(webhook_events, "_get_cloudwatch") as mock_get_cw:
            _log(logger)

            mock_get_cw.return_value.put_metric_data.assert_called_once()
            kwargs = mock_get_cw.return_value.put_metric_data.call_args.kwargs

        assert kwargs["Namespace"] == webhook_events.METRICS_NAMESPACE
        datum = kwargs["MetricData"][0]
        assert datum["MetricName"] == webhook_events.ROW_WRITE_DROPPED_METRIC
        assert datum["Value"] == 1
        assert datum["Unit"] == "Count"

    def test_metric_lands_in_the_shared_webhookingress_namespace(self) -> None:
        """Same namespace as the rest of ingress metrics, so it shares the dashboard."""
        assert webhook_events.METRICS_NAMESPACE == "WebhookIngress"

    def test_dimensions_carry_status_and_error_kind(self) -> None:
        """Status separates an authz-bearing drop from an audit-only one."""
        logger = _logger_with_failing_table(ValueError("boom"))

        with patch.object(webhook_events, "_get_cloudwatch") as mock_get_cw:
            _log(logger, status="webhook_received")
            kwargs = mock_get_cw.return_value.put_metric_data.call_args.kwargs

        dims = {d["Name"]: d["Value"] for d in kwargs["MetricData"][0]["Dimensions"]}
        assert dims["Status"] == "webhook_received"
        assert dims["ErrorKind"] == "ValueError"

    def test_terminal_status_drop_is_distinguishable(self) -> None:
        """A ``blocked`` row is audit-only; the Status dimension keeps it separable
        from the ``webhook_received`` drop that is the actual #4187 hazard."""
        logger = _logger_with_failing_table()

        with patch.object(webhook_events, "_get_cloudwatch") as mock_get_cw:
            _log(logger, status="blocked")
            kwargs = mock_get_cw.return_value.put_metric_data.call_args.kwargs

        dims = {d["Name"]: d["Value"] for d in kwargs["MetricData"][0]["Dimensions"]}
        assert dims["Status"] == "blocked"

    def test_drop_is_logged_at_error_with_the_event_id(self) -> None:
        """The log line stays the human-readable half of the signal."""
        logger = _logger_with_failing_table()

        with patch.object(webhook_events, "_get_cloudwatch"):
            with patch.object(webhook_events.logger, "error") as mock_error:
                _log(logger, event_id="evt-dropped")

        mock_error.assert_called_once()
        assert "evt-dropped" in mock_error.call_args[0]


class TestSuccessfulWriteEmitsNoSignal:
    """No false positives — otherwise the alarm is unusable and gets muted."""

    def test_successful_write_emits_no_metric(self) -> None:
        logger = _logger_with_working_table()

        with patch.object(webhook_events, "_get_cloudwatch") as mock_get_cw:
            _log(logger)

            mock_get_cw.return_value.put_metric_data.assert_not_called()

    def test_successful_write_logs_no_error(self) -> None:
        logger = _logger_with_working_table()

        with patch.object(webhook_events.logger, "error") as mock_error:
            _log(logger)

        mock_error.assert_not_called()


class TestBestEffortSemanticsPreserved:
    """Option 2 only: make the drop visible, do NOT change failure semantics."""

    def test_dropped_write_still_does_not_raise(self) -> None:
        """Option 1 (fail the spawn) is deliberately deferred — no behaviour change."""
        logger = _logger_with_failing_table()

        with patch.object(webhook_events, "_get_cloudwatch"):
            item = _log(logger, event_id="evt-still-returned")

        assert item["event_id"] == "evt-still-returned"

    def test_metric_emission_failure_is_swallowed(self) -> None:
        """A broken metric path must not crash the caller it was added to observe."""
        logger = _logger_with_failing_table()

        with patch.object(webhook_events, "_get_cloudwatch") as mock_get_cw:
            cw = mock_get_cw.return_value
            cw.put_metric_data.side_effect = RuntimeError("cw down")

            item = _log(logger, event_id="evt-metric-broke")

        assert item["event_id"] == "evt-metric-broke"

    def test_cloudwatch_client_construction_failure_is_swallowed(self) -> None:
        """Even a client that cannot be built must not surface to the caller."""
        logger = _logger_with_failing_table()

        with patch.object(
            webhook_events, "_get_cloudwatch", side_effect=RuntimeError("no creds")
        ):
            item = _log(logger, event_id="evt-no-client")

        assert item["event_id"] == "evt-no-client"
