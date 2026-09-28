"""Unit tests for durable bootstrap logger (CloudWatch Logs handler).

Covers:
- CloudWatchBootstrapHandler initialization (group/stream creation)
- Step enter/success/error logging
- Graceful degradation when CloudWatch calls fail
- BootstrapLogger high-level API
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add parent to path so we can import the modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.bootstrap_logger import BootstrapLogger, CloudWatchBootstrapHandler


# --- Test: CloudWatchBootstrapHandler ---


class TestCloudWatchBootstrapHandler:
    @patch("lib.bootstrap_logger.boto3.client")
    def test_init_creates_log_group_and_stream(self, mock_boto_client):
        """Handler creates log group and stream on initialization."""
        mock_logs = MagicMock()
        mock_boto_client.return_value = mock_logs

        handler = CloudWatchBootstrapHandler(
            log_group="/adp/dev/agent-factory/bootstrap",
            log_stream="corr-abc-123",
            region="us-east-1",
        )

        mock_boto_client.assert_called_once_with("logs", region_name="us-east-1")
        mock_logs.create_log_group.assert_called_once_with(
            logGroupName="/adp/dev/agent-factory/bootstrap"
        )
        mock_logs.create_log_stream.assert_called_once_with(
            logGroupName="/adp/dev/agent-factory/bootstrap",
            logStreamName="corr-abc-123",
        )
        assert handler._initialized is True
        assert handler._failed is False

    @patch("lib.bootstrap_logger.boto3.client")
    def test_init_handles_existing_group(self, mock_boto_client):
        """Handler tolerates ResourceAlreadyExistsException for group."""
        from botocore.exceptions import ClientError

        mock_logs = MagicMock()
        mock_boto_client.return_value = mock_logs
        mock_logs.create_log_group.side_effect = ClientError(
            {"Error": {"Code": "ResourceAlreadyExistsException", "Message": "exists"}},
            "CreateLogGroup",
        )

        handler = CloudWatchBootstrapHandler(
            log_group="/adp/dev/agent-factory/bootstrap",
            log_stream="test-stream",
            region="us-east-1",
        )

        assert handler._initialized is True
        assert handler._failed is False

    @patch("lib.bootstrap_logger.boto3.client")
    def test_init_handles_existing_stream(self, mock_boto_client):
        """Handler tolerates ResourceAlreadyExistsException for stream."""
        from botocore.exceptions import ClientError

        mock_logs = MagicMock()
        mock_boto_client.return_value = mock_logs
        mock_logs.create_log_stream.side_effect = ClientError(
            {"Error": {"Code": "ResourceAlreadyExistsException", "Message": "exists"}},
            "CreateLogStream",
        )

        handler = CloudWatchBootstrapHandler(
            log_group="/adp/dev/agent-factory/bootstrap",
            log_stream="test-stream",
            region="us-east-1",
        )

        assert handler._initialized is True

    @patch("lib.bootstrap_logger.boto3.client")
    def test_init_fails_soft_on_boto_error(self, mock_boto_client):
        """Handler sets _failed=True and continues if boto3 init fails."""
        mock_boto_client.side_effect = Exception("No credentials")

        handler = CloudWatchBootstrapHandler(
            log_group="/adp/dev/agent-factory/bootstrap",
            log_stream="test-stream",
            region="us-east-1",
        )

        assert handler._failed is True
        assert handler._initialized is False

    @patch("lib.bootstrap_logger.boto3.client")
    def test_emit_buffers_log_events(self, mock_boto_client):
        """emit() buffers events without flushing until threshold."""
        mock_logs = MagicMock()
        mock_boto_client.return_value = mock_logs

        handler = CloudWatchBootstrapHandler(
            log_group="/adp/dev/agent-factory/bootstrap",
            log_stream="test-stream",
            region="us-east-1",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))

        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test message",
            args=(),
            exc_info=None,
        )
        handler.emit(record)

        assert len(handler._buffer) == 1
        assert handler._buffer[0]["message"] == "test message"
        # Not yet flushed
        mock_logs.put_log_events.assert_not_called()

    @patch("lib.bootstrap_logger.boto3.client")
    def test_flush_sends_buffered_events(self, mock_boto_client):
        """flush() calls PutLogEvents with buffered messages."""
        mock_logs = MagicMock()
        mock_boto_client.return_value = mock_logs
        mock_logs.put_log_events.return_value = {"nextSequenceToken": "token-1"}

        handler = CloudWatchBootstrapHandler(
            log_group="/adp/dev/agent-factory/bootstrap",
            log_stream="test-stream",
            region="us-east-1",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))

        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="step 1 enter",
            args=(),
            exc_info=None,
        )
        handler.emit(record)
        handler.flush()

        mock_logs.put_log_events.assert_called_once()
        call_kwargs = mock_logs.put_log_events.call_args[1]
        assert call_kwargs["logGroupName"] == "/adp/dev/agent-factory/bootstrap"
        assert call_kwargs["logStreamName"] == "test-stream"
        assert len(call_kwargs["logEvents"]) == 1
        assert call_kwargs["logEvents"][0]["message"] == "step 1 enter"
        # Buffer cleared
        assert len(handler._buffer) == 0

    @patch("lib.bootstrap_logger.boto3.client")
    def test_flush_handles_put_failure_gracefully(self, mock_boto_client):
        """flush() clears buffer and continues on PutLogEvents failure."""
        mock_logs = MagicMock()
        mock_boto_client.return_value = mock_logs
        mock_logs.put_log_events.side_effect = Exception("Throttled")

        handler = CloudWatchBootstrapHandler(
            log_group="/adp/dev/agent-factory/bootstrap",
            log_stream="test-stream",
            region="us-east-1",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))

        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test",
            args=(),
            exc_info=None,
        )
        handler.emit(record)
        # Should NOT raise
        handler.flush()

        # Buffer cleared even on failure (don't accumulate stale events)
        assert len(handler._buffer) == 0
        # Handler still active (might succeed next time)
        assert handler._failed is False

    @patch("lib.bootstrap_logger.boto3.client")
    def test_emit_skipped_when_failed(self, mock_boto_client):
        """emit() is a no-op when handler has failed."""
        mock_boto_client.side_effect = Exception("No creds")

        handler = CloudWatchBootstrapHandler(
            log_group="/adp/dev/agent-factory/bootstrap",
            log_stream="test-stream",
            region="us-east-1",
        )
        assert handler._failed is True

        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="should not buffer",
            args=(),
            exc_info=None,
        )
        handler.emit(record)
        assert len(handler._buffer) == 0


# --- Test: BootstrapLogger high-level API ---


class TestBootstrapLogger:
    @patch("lib.bootstrap_logger.boto3.client")
    def test_step_start_logs_enter_with_context(self, mock_boto_client):
        """step_start emits a log line with [bootstrap step=N name=...] ENTER."""
        mock_logs = MagicMock()
        mock_boto_client.return_value = mock_logs
        mock_logs.put_log_events.return_value = {"nextSequenceToken": "t1"}

        bl = BootstrapLogger(
            environment="dev",
            correlation_id="corr-123",
            region="us-east-1",
        )
        bl.step_start(2, "vault_fetch", secret="tenants/acme/github-app")
        bl.close()

        # Verify the flush happened
        mock_logs.put_log_events.assert_called_once()
        events = mock_logs.put_log_events.call_args[1]["logEvents"]
        assert len(events) == 1
        msg = events[0]["message"]
        assert "[bootstrap step=2 name=vault_fetch] ENTER" in msg
        assert "secret=tenants/acme/github-app" in msg
        assert "correlation_id=corr-123" in msg

    @patch("lib.bootstrap_logger.boto3.client")
    def test_step_success_logs_ok(self, mock_boto_client):
        """step_success emits a log line with OK status."""
        mock_logs = MagicMock()
        mock_boto_client.return_value = mock_logs
        mock_logs.put_log_events.return_value = {"nextSequenceToken": "t1"}

        bl = BootstrapLogger(
            environment="dev",
            correlation_id="corr-456",
            region="us-east-1",
        )
        bl.step_success(3, "mint_token", app_id="123")
        bl.close()

        events = mock_logs.put_log_events.call_args[1]["logEvents"]
        msg = events[0]["message"]
        assert "[bootstrap step=3 name=mint_token] OK" in msg
        assert "app_id=123" in msg
        assert "correlation_id=corr-456" in msg

    @patch("lib.bootstrap_logger.boto3.client")
    def test_step_error_logs_exception_and_traceback(self, mock_boto_client):
        """step_error emits step + exception type + message + traceback."""
        mock_logs = MagicMock()
        mock_boto_client.return_value = mock_logs
        mock_logs.put_log_events.return_value = {"nextSequenceToken": "t1"}

        bl = BootstrapLogger(
            environment="dev",
            correlation_id="corr-789",
            region="us-east-1",
        )

        try:
            raise ValueError("Secret 'tenants/bad/github-app' not found")
        except ValueError as exc:
            bl.step_error(2, "vault_fetch", exc)

        bl.close()

        events = mock_logs.put_log_events.call_args[1]["logEvents"]
        msg = events[0]["message"]
        assert "[bootstrap step=2 name=vault_fetch] FAILED" in msg
        assert "exception=ValueError" in msg
        assert "Secret 'tenants/bad/github-app' not found" in msg
        assert "correlation_id=corr-789" in msg
        # Traceback included
        assert "Traceback" in msg or "raise ValueError" in msg

    @patch("lib.bootstrap_logger.boto3.client")
    def test_log_fatal_flushes_and_closes(self, mock_boto_client):
        """log_fatal logs the current step error and flushes."""
        mock_logs = MagicMock()
        mock_boto_client.return_value = mock_logs
        mock_logs.put_log_events.return_value = {"nextSequenceToken": "t1"}

        bl = BootstrapLogger(
            environment="dev",
            correlation_id="corr-fatal",
            region="us-east-1",
        )
        bl.step_start(3, "mint_token")
        try:
            raise RuntimeError("Failed to mint token: 401")
        except RuntimeError as exc:
            bl.log_fatal(exc)

        # Should have flushed (called put_log_events)
        assert mock_logs.put_log_events.called
        events = mock_logs.put_log_events.call_args[1]["logEvents"]
        # Should contain both the ENTER line and the FAILED line
        messages = [e["message"] for e in events]
        assert any("ENTER" in m for m in messages)
        assert any("FAILED" in m and "RuntimeError" in m for m in messages)

    @patch("lib.bootstrap_logger.boto3.client")
    def test_stream_name_uses_correlation_id(self, mock_boto_client):
        """Log stream is named after correlation_id."""
        mock_logs = MagicMock()
        mock_boto_client.return_value = mock_logs

        BootstrapLogger(
            environment="dev",
            correlation_id="abc-def-123",
            region="us-east-1",
        )

        mock_logs.create_log_stream.assert_called_once_with(
            logGroupName="/adp/dev/agent-factory/bootstrap",
            logStreamName="abc-def-123",
        )

    @patch("lib.bootstrap_logger.boto3.client")
    def test_stream_name_falls_back_to_message_id(self, mock_boto_client):
        """Log stream uses message_id when correlation_id is empty."""
        mock_logs = MagicMock()
        mock_boto_client.return_value = mock_logs

        BootstrapLogger(
            environment="dev",
            correlation_id="",
            message_id="msg-xyz-789",
            region="us-east-1",
        )

        mock_logs.create_log_stream.assert_called_once_with(
            logGroupName="/adp/dev/agent-factory/bootstrap",
            logStreamName="msg-xyz-789",
        )

    @patch("lib.bootstrap_logger.boto3.client")
    def test_graceful_degradation_no_crash(self, mock_boto_client):
        """Logger degrades gracefully when CloudWatch is unavailable."""
        mock_boto_client.side_effect = Exception("No credentials")

        bl = BootstrapLogger(
            environment="dev",
            correlation_id="corr-no-cw",
            region="us-east-1",
        )

        # All operations should succeed (no-op) without raising
        bl.step_start(1, "parse_envelope")
        bl.step_success(1, "parse_envelope")
        try:
            raise ValueError("test")
        except ValueError as exc:
            bl.step_error(1, "parse_envelope", exc)
        bl.log_fatal(ValueError("fatal"))
        bl.close()

        # No crash — test passes by completing without exception
        assert bl.is_active is False

    @patch("lib.bootstrap_logger.boto3.client")
    def test_log_group_name_uses_environment(self, mock_boto_client):
        """Log group name incorporates the environment."""
        mock_logs = MagicMock()
        mock_boto_client.return_value = mock_logs

        BootstrapLogger(
            environment="prod",
            correlation_id="corr-prod",
            region="us-west-2",
        )

        mock_logs.create_log_group.assert_called_once_with(
            logGroupName="/adp/prod/agent-factory/bootstrap"
        )

    @patch("lib.bootstrap_logger.boto3.client")
    def test_colon_sanitized_in_stream_name(self, mock_boto_client):
        """Colons in correlation_id are replaced (CW doesn't allow them)."""
        mock_logs = MagicMock()
        mock_boto_client.return_value = mock_logs

        BootstrapLogger(
            environment="dev",
            correlation_id="67e8a9f2:abcd:1234",
            region="us-east-1",
        )

        mock_logs.create_log_stream.assert_called_once_with(
            logGroupName="/adp/dev/agent-factory/bootstrap",
            logStreamName="67e8a9f2-abcd-1234",
        )
