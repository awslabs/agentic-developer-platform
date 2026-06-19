"""Tests for agent_run_id contextvar propagation to usage_logs.

Issue #1616: Verify that the X-Agent-RunId header reaches _log_usage
and that the contextvar mechanism correctly passes the value through.
"""

from src.proxy.service import _current_agent_run_id


class TestAgentRunIdContextvar:
    """Test the _current_agent_run_id contextvar mechanism."""

    def test_default_is_none(self):
        """Contextvar defaults to None when not set."""
        # Reset to default
        _current_agent_run_id.set(None)
        assert _current_agent_run_id.get() is None

    def test_set_and_get(self):
        """Setting the contextvar makes it readable."""
        _current_agent_run_id.set("inv-test-123")
        assert _current_agent_run_id.get() == "inv-test-123"
        # Clean up
        _current_agent_run_id.set(None)

    def test_empty_string_treated_as_falsy(self):
        """Empty string is set but evaluable as falsy for the header check."""
        _current_agent_run_id.set("")
        # The value is stored
        assert _current_agent_run_id.get() == ""
        # But it's falsy
        assert not _current_agent_run_id.get()
        _current_agent_run_id.set(None)


class TestSetAgentRunIdFromHeader:
    """Test the route-level dependency that extracts the header."""

    def test_extracts_header_value(self):
        """The dependency reads x-agent-runid header and sets contextvar."""
        from unittest.mock import MagicMock

        from src.proxy.routes import set_agent_run_id_from_header

        request = MagicMock()
        request.headers = {"x-agent-runid": "inv-from-header-456"}

        result = set_agent_run_id_from_header(request)

        assert result == "inv-from-header-456"
        assert _current_agent_run_id.get() == "inv-from-header-456"
        # Clean up
        _current_agent_run_id.set(None)

    def test_missing_header_sets_none(self):
        """No header → contextvar set to None, returns None."""
        from unittest.mock import MagicMock

        from src.proxy.routes import set_agent_run_id_from_header

        request = MagicMock()
        request.headers = {}

        result = set_agent_run_id_from_header(request)

        assert result is None
        assert _current_agent_run_id.get() is None

    def test_absent_header_no_error(self):
        """Missing header does not raise an error (graceful degrade)."""
        from unittest.mock import MagicMock

        from src.proxy.routes import set_agent_run_id_from_header

        request = MagicMock()
        request.headers = {"content-type": "application/json"}

        # Should not raise
        result = set_agent_run_id_from_header(request)
        assert result is None
        _current_agent_run_id.set(None)
