"""Unit tests for subprocess streaming in sqs-worker.py (Story 2).

Tests cover:
- Streaming output appears in structured logs (not discarded)
- Timeout handling kills subprocess and raises TimeoutExpired
- Non-zero exit raises RuntimeError with output tail
- Empty output is handled gracefully
- Bookend events (start, complete/fail) are emitted with duration
- Telemetry env vars are propagated to child process
"""

from __future__ import annotations

import importlib.util
import logging
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest

# Add ingestion scripts to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "images" / "ingestion"))


def _load_sqs_worker():
    """Import sqs-worker.py (hyphenated filename requires importlib)."""
    spec = importlib.util.spec_from_file_location(
        "sqs_worker",
        Path(__file__).parent.parent.parent / "images" / "ingestion" / "sqs-worker.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Load the module once — _run_subprocess is what we test
_sqs_worker = _load_sqs_worker()
_run_subprocess = _sqs_worker._run_subprocess


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_telemetry():
    """Reset telemetry module state between tests."""
    import telemetry

    telemetry._configured = False
    telemetry.clear_correlation_context()
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    yield
    telemetry._configured = False
    telemetry.clear_correlation_context()
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)


@pytest.fixture
def capture_logs():
    """Capture structured log output to a StringIO buffer."""
    from telemetry import configure_telemetry

    buffer = StringIO()
    configure_telemetry(json_output=True)
    root = logging.getLogger()
    for h in root.handlers:
        h.stream = buffer
    yield buffer


# ---------------------------------------------------------------------------
# Tests: Streaming Output
# ---------------------------------------------------------------------------


class TestSubprocessStreaming:
    """Tests for _run_subprocess streaming behavior."""

    def test_streaming_output_appears_in_logs(self, capture_logs):
        """Child process stdout lines appear in parent's structured log output."""
        cmd = [sys.executable, "-c", "print('line-one'); print('line-two')"]
        _run_subprocess(cmd, timeout=30)

        output = capture_logs.getvalue()
        assert "line-one" in output
        assert "line-two" in output

    def test_streaming_includes_subprocess_start_event(self, capture_logs):
        """A subprocess.start bookend event is emitted."""
        cmd = [sys.executable, "-c", "pass"]
        _run_subprocess(cmd, timeout=30)

        output = capture_logs.getvalue()
        assert "subprocess.start" in output

    def test_streaming_includes_subprocess_complete_event(self, capture_logs):
        """A subprocess.complete bookend event is emitted on success."""
        cmd = [sys.executable, "-c", "pass"]
        _run_subprocess(cmd, timeout=30)

        output = capture_logs.getvalue()
        assert "subprocess.complete" in output
        assert "exit_code=0" in output

    def test_nonzero_exit_raises_runtime_error(self, capture_logs):
        """Non-zero exit code raises RuntimeError with output tail."""
        cmd = [sys.executable, "-c", "import sys; print('error-detail'); sys.exit(1)"]

        with pytest.raises(RuntimeError, match="Exit code 1"):
            _run_subprocess(cmd, timeout=30)

    def test_nonzero_exit_includes_tail_in_error(self, capture_logs):
        """RuntimeError message includes the last lines of output."""
        cmd = [
            sys.executable,
            "-c",
            "import sys; print('diagnostic info'); print('the actual error'); sys.exit(42)",
        ]

        with pytest.raises(RuntimeError) as exc_info:
            _run_subprocess(cmd, timeout=30)

        assert "42" in str(exc_info.value)
        assert "the actual error" in str(exc_info.value)

    def test_failed_subprocess_emits_error_event(self, capture_logs):
        """subprocess.failed bookend event is emitted on non-zero exit."""
        cmd = [sys.executable, "-c", "import sys; sys.exit(1)"]

        with pytest.raises(RuntimeError):
            _run_subprocess(cmd, timeout=30)

        output = capture_logs.getvalue()
        assert "subprocess.failed" in output
        assert "exit_code=1" in output

    def test_empty_output_handled_gracefully(self, capture_logs):
        """A subprocess that produces no output completes without error."""
        cmd = [sys.executable, "-c", "pass"]
        _run_subprocess(cmd, timeout=30)

        output = capture_logs.getvalue()
        assert "subprocess.complete" in output

    def test_stderr_merged_into_stdout_stream(self, capture_logs):
        """Stderr output is captured via STDOUT merge."""
        cmd = [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('stderr-line\\n')",
        ]
        _run_subprocess(cmd, timeout=30)

        output = capture_logs.getvalue()
        assert "stderr-line" in output


# ---------------------------------------------------------------------------
# Tests: Timeout Handling
# ---------------------------------------------------------------------------


class TestSubprocessTimeout:
    """Tests for subprocess timeout behavior."""

    def test_timeout_kills_subprocess(self, capture_logs):
        """A subprocess exceeding timeout is killed and TimeoutExpired raised."""
        cmd = [sys.executable, "-c", "import time; time.sleep(60)"]

        with pytest.raises(subprocess.TimeoutExpired):
            _run_subprocess(cmd, timeout=1)

    def test_timeout_emits_error_event(self, capture_logs):
        """subprocess.timeout event is logged when timeout occurs."""
        cmd = [sys.executable, "-c", "import time; time.sleep(60)"]

        with pytest.raises(subprocess.TimeoutExpired):
            _run_subprocess(cmd, timeout=1)

        output = capture_logs.getvalue()
        assert "subprocess.timeout" in output


# ---------------------------------------------------------------------------
# Tests: Environment Propagation
# ---------------------------------------------------------------------------


class TestEnvPropagation:
    """Tests that telemetry env vars are passed to child processes."""

    def test_telemetry_env_propagated(self, capture_logs):
        """Child process receives KNOWLEDGE_LAYER_TELEMETRY_ENABLED env var."""
        cmd = [
            sys.executable,
            "-c",
            "import os; v = os.environ.get('KNOWLEDGE_LAYER_TELEMETRY_ENABLED', ''); print(f'GOT_TELEM={v}')",
        ]
        _run_subprocess(cmd, timeout=30)

        output = capture_logs.getvalue()
        # The child prints the env var value — verify it's non-empty
        assert "GOT_TELEM=true" in output or "GOT_TELEM=1" in output

    def test_log_format_env_propagated(self, capture_logs):
        """Child process receives LOG_FORMAT env var."""
        cmd = [
            sys.executable,
            "-c",
            "import os; v = os.environ.get('LOG_FORMAT', ''); print(f'GOT_FMT={v}')",
        ]
        _run_subprocess(cmd, timeout=30)

        output = capture_logs.getvalue()
        # LOG_FORMAT defaults to 'json'
        assert "GOT_FMT=json" in output


# ---------------------------------------------------------------------------
# Tests: Duration tracking
# ---------------------------------------------------------------------------


class TestDurationTracking:
    """Tests that duration is reported in bookend events."""

    def test_complete_event_includes_duration(self, capture_logs):
        """subprocess.complete includes a duration measurement."""
        cmd = [sys.executable, "-c", "pass"]
        _run_subprocess(cmd, timeout=30)

        output = capture_logs.getvalue()
        assert "duration=" in output

    def test_failed_event_includes_duration(self, capture_logs):
        """subprocess.failed includes a duration measurement."""
        cmd = [sys.executable, "-c", "import sys; sys.exit(1)"]

        with pytest.raises(RuntimeError):
            _run_subprocess(cmd, timeout=30)

        output = capture_logs.getvalue()
        assert "duration=" in output


# ---------------------------------------------------------------------------
# Tests: Large output handling
# ---------------------------------------------------------------------------


class TestLargeOutput:
    """Tests for handling large subprocess output."""

    def test_large_output_keeps_tail(self, capture_logs):
        """When subprocess fails after producing many lines, the tail is in the error."""
        script = (
            "for i in range(100): print(f'line-{i}')\n"
            "import sys; sys.exit(1)"
        )
        cmd = [sys.executable, "-c", script]

        with pytest.raises(RuntimeError) as exc_info:
            _run_subprocess(cmd, timeout=30)

        # The error should contain some of the last lines (tail)
        error_msg = str(exc_info.value)
        assert "line-99" in error_msg

    def test_many_lines_do_not_cause_memory_issue(self, capture_logs):
        """Streaming 10k lines doesn't accumulate unbounded memory (only keeps last 50)."""
        script = "for i in range(10000): print(f'data-{i}')"
        cmd = [sys.executable, "-c", script]

        # Should complete without memory issues
        _run_subprocess(cmd, timeout=30)

        output = capture_logs.getvalue()
        assert "subprocess.complete" in output
