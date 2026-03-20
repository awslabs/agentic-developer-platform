"""Tests for structured JSON logging module."""

import io
import json
import logging

import pytest

from src.shared.logging import (
    LogContext,
    clear_request_context,
    configure_logging,
    get_json_formatter,
    get_logger,
    get_request_id,
    set_request_context,
)


class TestStructuredJsonFormatter:
    """Tests for StructuredJsonFormatter."""

    def test_formatter_includes_timestamp(self):
        """Test that formatter includes timestamp."""
        formatter = get_json_formatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )

        formatted = formatter.format(record)
        data = json.loads(formatted)

        assert "timestamp" in data
        assert data["timestamp"]  # Should not be empty

    def test_formatter_includes_level(self):
        """Test that formatter includes log level."""
        formatter = get_json_formatter()
        record = logging.LogRecord(
            name="test",
            level=logging.WARNING,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )

        formatted = formatter.format(record)
        data = json.loads(formatted)

        assert data["level"] == "WARNING"

    def test_formatter_includes_module(self):
        """Test that formatter includes module name."""
        formatter = get_json_formatter()
        record = logging.LogRecord(
            name="my.test.module",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )

        formatted = formatter.format(record)
        data = json.loads(formatted)

        assert data["module"] == "my.test.module"

    def test_formatter_includes_message(self):
        """Test that formatter includes message."""
        formatter = get_json_formatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="This is a test message",
            args=(),
            exc_info=None,
        )

        formatted = formatter.format(record)
        data = json.loads(formatted)

        assert data["message"] == "This is a test message"

    def test_formatter_includes_context_vars(self):
        """Test that formatter includes context variables when set."""
        # Set context
        set_request_context(
            request_id="test-request-123",
            org_id="test-org-456",
            user_id="test-user-789",
        )

        try:
            formatter = get_json_formatter()
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="test.py",
                lineno=1,
                msg="Test message",
                args=(),
                exc_info=None,
            )

            formatted = formatter.format(record)
            data = json.loads(formatted)

            assert data["request_id"] == "test-request-123"
            assert data["org_id"] == "test-org-456"
            assert data["user_id"] == "test-user-789"
        finally:
            clear_request_context()


class TestRequestContext:
    """Tests for request context functions."""

    def setup_method(self):
        """Clear context before each test."""
        clear_request_context()

    def teardown_method(self):
        """Clear context after each test."""
        clear_request_context()

    def test_set_and_get_request_id(self):
        """Test setting and getting request ID."""
        set_request_context(request_id="abc-123")
        assert get_request_id() == "abc-123"

    def test_set_multiple_context_fields(self):
        """Test setting multiple context fields."""
        set_request_context(
            request_id="req-1",
            org_id="org-1",
            user_id="user-1",
            team_id="team-1",
            department_id="dept-1",
        )

        assert get_request_id() == "req-1"
        # Test through the context vars directly
        from src.shared.logging import (
            department_id_var,
            org_id_var,
            team_id_var,
            user_id_var,
        )

        assert org_id_var.get() == "org-1"
        assert user_id_var.get() == "user-1"
        assert team_id_var.get() == "team-1"
        assert department_id_var.get() == "dept-1"

    def test_clear_request_context(self):
        """Test clearing request context."""
        set_request_context(request_id="abc-123", org_id="org-1")
        clear_request_context()

        assert get_request_id() is None
        from src.shared.logging import org_id_var

        assert org_id_var.get() is None


class TestLogContext:
    """Tests for LogContext context manager."""

    def setup_method(self):
        """Clear context before each test."""
        clear_request_context()

    def teardown_method(self):
        """Clear context after each test."""
        clear_request_context()

    def test_context_manager_sets_values(self):
        """Test that context manager sets values."""
        with LogContext(request_id="ctx-123", org_id="ctx-org"):
            assert get_request_id() == "ctx-123"
            from src.shared.logging import org_id_var

            assert org_id_var.get() == "ctx-org"

    def test_context_manager_restores_previous_values(self):
        """Test that context manager restores previous values on exit."""
        set_request_context(request_id="original-123")

        with LogContext(request_id="nested-456"):
            assert get_request_id() == "nested-456"

        assert get_request_id() == "original-123"

    def test_nested_context_managers(self):
        """Test nested context managers."""
        with LogContext(request_id="outer"):
            assert get_request_id() == "outer"

            with LogContext(request_id="inner"):
                assert get_request_id() == "inner"

            assert get_request_id() == "outer"


class TestConfigureLogging:
    """Tests for configure_logging function."""

    def test_configure_with_json_output(self):
        """Test configuring logging with JSON output."""
        stream = io.StringIO()
        configure_logging(level="DEBUG", json_output=True, stream=stream)

        logger = get_logger("test.json")
        logger.info("JSON test message")

        output = stream.getvalue()
        # Should be valid JSON
        data = json.loads(output.strip())
        assert data["message"] == "JSON test message"

    def test_configure_with_plain_output(self):
        """Test configuring logging with plain text output."""
        stream = io.StringIO()
        configure_logging(level="DEBUG", json_output=False, stream=stream)

        logger = get_logger("test.plain")
        logger.info("Plain text test")

        output = stream.getvalue()
        assert "Plain text test" in output
        # Should NOT be JSON
        with pytest.raises(json.JSONDecodeError):
            json.loads(output.strip())

    def test_configure_log_level(self):
        """Test that log level is respected."""
        stream = io.StringIO()
        configure_logging(level="WARNING", json_output=True, stream=stream)

        logger = get_logger("test.level")
        logger.debug("Debug message - should not appear")
        logger.info("Info message - should not appear")
        logger.warning("Warning message - should appear")

        output = stream.getvalue()
        assert "Debug message" not in output
        assert "Info message" not in output
        assert "Warning message" in output


class TestGetLogger:
    """Tests for get_logger function."""

    def test_get_logger_returns_logger(self):
        """Test that get_logger returns a logger instance."""
        logger = get_logger("test.module")
        assert isinstance(logger, logging.Logger)
        assert logger.name == "test.module"

    def test_get_logger_same_instance(self):
        """Test that get_logger returns same instance for same name."""
        logger1 = get_logger("same.name")
        logger2 = get_logger("same.name")
        assert logger1 is logger2
