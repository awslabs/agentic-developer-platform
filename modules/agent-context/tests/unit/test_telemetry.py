"""Unit tests for telemetry.py — Knowledge Layer observability foundation.

Tests cover:
- JSON structured output with correlation fields
- Correlation context set/get/clear
- Fail-open behavior (telemetry errors don't crash ingestion)
- Kill switch (KNOWLEDGE_LAYER_TELEMETRY_ENABLED=false disables JSON output)
- Stage context updates in StageTracker integration
"""

from __future__ import annotations

import json
import logging
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add ingestion scripts to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "images" / "ingestion"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_telemetry():
    """Reset telemetry module state between tests."""
    import telemetry

    # Reset the _configured flag so each test can call configure_telemetry()
    telemetry._configured = False
    telemetry.clear_correlation_context()
    # Reset root logger handlers
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    yield
    # Cleanup after test
    telemetry._configured = False
    telemetry.clear_correlation_context()
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)


@pytest.fixture
def capture_logs():
    """Capture log output to a StringIO buffer."""
    buffer = StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    yield buffer
    root.removeHandler(handler)


# ---------------------------------------------------------------------------
# Tests: Correlation Context
# ---------------------------------------------------------------------------


class TestCorrelationContext:
    """Tests for the correlation context contextvars."""

    def test_set_and_get_all_fields(self):
        """All correlation fields are set and retrievable."""
        from telemetry import get_correlation_context, set_correlation_context

        set_correlation_context(
            asset_id="org/my-repo",
            owner_sub="user-abc-123",
            tenant_id="tenant-xyz",
            project_id="proj-001",
            run_id="run-uuid-here",
            stage="clone",
            asset_type="repo",
        )

        ctx = get_correlation_context()
        assert ctx.asset_id == "org/my-repo"
        assert ctx.owner_sub == "user-abc-123"
        assert ctx.tenant_id == "tenant-xyz"
        assert ctx.project_id == "proj-001"
        assert ctx.run_id == "run-uuid-here"
        assert ctx.stage == "clone"
        assert ctx.asset_type == "repo"

    def test_partial_set(self):
        """Setting some fields leaves others as None."""
        from telemetry import get_correlation_context, set_correlation_context

        set_correlation_context(asset_id="org/repo", asset_type="repo")

        ctx = get_correlation_context()
        assert ctx.asset_id == "org/repo"
        assert ctx.asset_type == "repo"
        assert ctx.owner_sub is None
        assert ctx.tenant_id is None

    def test_clear_resets_all(self):
        """clear_correlation_context resets all fields to None."""
        from telemetry import (
            clear_correlation_context,
            get_correlation_context,
            set_correlation_context,
        )

        set_correlation_context(asset_id="x", tenant_id="t")
        clear_correlation_context()

        ctx = get_correlation_context()
        assert ctx.asset_id is None
        assert ctx.tenant_id is None

    def test_as_dict_excludes_none(self):
        """CorrelationContext.as_dict() only includes non-None fields."""
        from telemetry import get_correlation_context, set_correlation_context

        set_correlation_context(asset_id="org/repo", stage="deepwiki")
        ctx = get_correlation_context()
        d = ctx.as_dict()

        assert d == {"asset_id": "org/repo", "stage": "deepwiki"}
        assert "owner_sub" not in d
        assert "tenant_id" not in d

    def test_overwrite_preserves_unset(self):
        """A second set_correlation_context call only updates specified fields."""
        from telemetry import get_correlation_context, set_correlation_context

        set_correlation_context(asset_id="org/repo", tenant_id="t1")
        set_correlation_context(stage="clone")  # only sets stage

        ctx = get_correlation_context()
        assert ctx.asset_id == "org/repo"
        assert ctx.tenant_id == "t1"
        assert ctx.stage == "clone"


# ---------------------------------------------------------------------------
# Tests: JSON Structured Logging
# ---------------------------------------------------------------------------


class TestJsonLogging:
    """Tests for JSON structured log output."""

    def test_json_output_format(self):
        """Logs are valid JSON with expected fields."""
        from telemetry import configure_telemetry, get_logger

        buffer = StringIO()
        # Patch stdout to capture
        handler = logging.StreamHandler(buffer)
        handler.setLevel(logging.DEBUG)

        configure_telemetry(json_output=True)
        root = logging.getLogger()
        # Replace handler stream
        for h in root.handlers:
            h.stream = buffer

        logger = get_logger("test-module")
        logger.info("test message here")

        output = buffer.getvalue().strip()
        assert output, "No log output captured"

        parsed = json.loads(output)
        assert parsed["level"] == "INFO"
        assert parsed["module"] == "test-module"
        assert parsed["message"] == "test message here"
        assert "timestamp" in parsed

    def test_correlation_fields_in_json(self):
        """Correlation context fields appear in JSON log output."""
        from telemetry import configure_telemetry, get_logger, set_correlation_context

        buffer = StringIO()
        configure_telemetry(json_output=True)
        root = logging.getLogger()
        for h in root.handlers:
            h.stream = buffer

        set_correlation_context(
            asset_id="org/test-repo",
            tenant_id="tenant-123",
            run_id="run-abc",
            stage="zoekt",
        )

        logger = get_logger("test")
        logger.info("indexed successfully")

        output = buffer.getvalue().strip()
        parsed = json.loads(output)

        assert parsed["asset_id"] == "org/test-repo"
        assert parsed["tenant_id"] == "tenant-123"
        assert parsed["run_id"] == "run-abc"
        assert parsed["stage"] == "zoekt"
        assert parsed["message"] == "indexed successfully"

    def test_exception_included(self):
        """Exception info is captured in the JSON output."""
        from telemetry import configure_telemetry, get_logger

        buffer = StringIO()
        configure_telemetry(json_output=True)
        root = logging.getLogger()
        for h in root.handlers:
            h.stream = buffer

        logger = get_logger("test")
        try:
            raise ValueError("something broke")
        except ValueError:
            logger.exception("failed to process")

        output = buffer.getvalue().strip()
        parsed = json.loads(output)
        assert "exception" in parsed
        assert "ValueError" in parsed["exception"]
        assert "something broke" in parsed["exception"]

    def test_no_correlation_fields_when_unset(self):
        """When no context is set, correlation fields are absent (not null)."""
        from telemetry import configure_telemetry, get_logger

        buffer = StringIO()
        configure_telemetry(json_output=True)
        root = logging.getLogger()
        for h in root.handlers:
            h.stream = buffer

        logger = get_logger("test")
        logger.info("bare message")

        output = buffer.getvalue().strip()
        parsed = json.loads(output)

        # These fields should not be present at all when unset
        assert "asset_id" not in parsed
        assert "tenant_id" not in parsed
        assert "run_id" not in parsed


# ---------------------------------------------------------------------------
# Tests: Fail-Open Behavior
# ---------------------------------------------------------------------------


class TestFailOpen:
    """Tests ensuring telemetry never blocks ingestion."""

    def test_safe_emit_swallows_exceptions(self):
        """safe_emit() catches and discards exceptions from the wrapped function."""
        from telemetry import safe_emit

        def explode():
            raise RuntimeError("telemetry backend down")

        # Should not raise
        safe_emit(explode)

    def test_safe_emit_passes_args(self):
        """safe_emit() correctly passes arguments to the wrapped function."""
        from telemetry import safe_emit

        results = []

        def collector(a, b, key=None):
            results.append((a, b, key))

        safe_emit(collector, 1, 2, key="val")
        assert results == [(1, 2, "val")]

    def test_json_formatter_survives_broken_contextvars(self):
        """Even if get_correlation_context raises, the formatter still produces output."""
        from telemetry import configure_telemetry, get_logger

        buffer = StringIO()
        configure_telemetry(json_output=True)
        root = logging.getLogger()
        for h in root.handlers:
            h.stream = buffer

        # Patch get_correlation_context to raise
        with patch("telemetry.get_correlation_context", side_effect=RuntimeError("boom")):
            logger = get_logger("test")
            logger.info("should still work")

        output = buffer.getvalue().strip()
        parsed = json.loads(output)
        assert parsed["message"] == "should still work"
        assert parsed["level"] == "INFO"

    def test_ingestion_completes_when_telemetry_raises(self):
        """Simulate a full ingestion cycle where telemetry is broken — must complete."""
        from telemetry import safe_emit, set_correlation_context

        # Patch set_correlation_context to always raise
        with patch("telemetry.set_correlation_context", side_effect=Exception("otel down")):
            # This simulates what sqs-worker does: safe_emit wraps the call
            safe_emit(set_correlation_context, asset_id="test", tenant_id="t1")

        # If we reach here without exception, the test passes
        # The ingestion pipeline would continue normally


# ---------------------------------------------------------------------------
# Tests: Kill Switch
# ---------------------------------------------------------------------------


class TestKillSwitch:
    """Tests for KNOWLEDGE_LAYER_TELEMETRY_ENABLED=false."""

    def test_disabled_uses_text_format(self):
        """When telemetry is disabled, output is plain text (not JSON)."""
        import telemetry

        # Simulate disabled
        original = telemetry.TELEMETRY_ENABLED
        telemetry.TELEMETRY_ENABLED = False
        telemetry._configured = False

        try:
            buffer = StringIO()
            telemetry.configure_telemetry()
            root = logging.getLogger()
            for h in root.handlers:
                h.stream = buffer

            logger = telemetry.get_logger("test")
            logger.info("plain message")

            output = buffer.getvalue().strip()
            # Should NOT be valid JSON (it's plain text)
            with pytest.raises(json.JSONDecodeError):
                json.loads(output)
            # But should contain the message
            assert "plain message" in output
        finally:
            telemetry.TELEMETRY_ENABLED = original


# ---------------------------------------------------------------------------
# Tests: Text Format (development mode)
# ---------------------------------------------------------------------------


class TestTextFormat:
    """Tests for the human-readable text formatter."""

    def test_text_format_includes_context(self):
        """Text formatter includes correlation fields in brackets."""
        from telemetry import configure_telemetry, get_logger, set_correlation_context

        buffer = StringIO()
        configure_telemetry(json_output=False)
        root = logging.getLogger()
        for h in root.handlers:
            h.stream = buffer

        set_correlation_context(asset_id="org/repo", stage="clone")
        logger = get_logger("test")
        logger.info("cloning started")

        output = buffer.getvalue().strip()
        assert "asset_id=org/repo" in output
        assert "stage=clone" in output
        assert "cloning started" in output


# ---------------------------------------------------------------------------
# Tests: StageTracker Integration
# ---------------------------------------------------------------------------


class TestStageTrackerIntegration:
    """Tests that StageTracker updates correlation context."""

    def test_stage_tracker_sets_run_id(self):
        """StageTracker.__init__ sets run_id in correlation context."""
        from telemetry import get_correlation_context

        mock_conn = MagicMock()
        # Mock db.create_index_run to return a known run_id
        with patch("stage_tracker.stage_db.create_index_run", return_value="run-12345"):
            from stage_tracker import StageTracker

            StageTracker(mock_conn, "org/repo", "repo-id-1", "abc123")

        ctx = get_correlation_context()
        assert ctx.run_id == "run-12345"

    def test_stage_context_manager_sets_stage(self):
        """Entering a stage() block sets the stage in correlation context."""
        from telemetry import get_correlation_context

        mock_conn = MagicMock()
        with patch("stage_tracker.stage_db.create_index_run", return_value="run-999"):
            with patch("stage_tracker.stage_db.start_stage", return_value="stage-id-1"):
                with patch("stage_tracker.stage_db.fail_stage"):
                    from stage_tracker import StageTracker

                    tracker = StageTracker(mock_conn, "org/repo", "repo-id-1")

                    with tracker.stage("deepwiki") as _ctx:
                        # Inside the stage block, correlation context should have stage
                        current = get_correlation_context()
                        assert current.stage == "deepwiki"


# ---------------------------------------------------------------------------
# Tests: configure_telemetry idempotency
# ---------------------------------------------------------------------------


class TestConfigureIdempotency:
    """Tests that configure_telemetry is safe to call multiple times."""

    def test_double_configure_is_noop(self):
        """Second call to configure_telemetry doesn't add extra handlers."""
        from telemetry import configure_telemetry

        configure_telemetry()
        handler_count = len(logging.getLogger().handlers)

        configure_telemetry()  # second call
        assert len(logging.getLogger().handlers) == handler_count
