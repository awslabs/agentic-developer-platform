"""Real child processes; no model/client/provider invocation."""

import errno
import os
from pathlib import Path
import signal
import sys
import time

import pytest

from tests.e2e.tools import process_bounds
from tests.e2e.tools.process_bounds import ProcessLimitError, run_bounded


def python(source):
    return [sys.executable, "-c", source]


def test_delivers_input_and_drains_both_pipes_without_deadlock():
    source = """import sys
sys.stderr.buffer.write(b'e' * 100000); sys.stderr.flush()
data = sys.stdin.buffer.read()
sys.stdout.buffer.write(data[::-1]); sys.stdout.flush()
"""
    result = run_bounded(python(source), environ=os.environ, timeout=3,
                         input_data=b"a" * 65536, max_output_bytes=200000)
    assert result.returncode == 0
    assert result.stdout == b"a" * 65536
    assert result.stderr == b"e" * 100000


def test_output_limit_counts_stdout_and_stderr_together():
    source = "import os; os.write(1, b'a' * 700); os.write(2, b'b' * 700)"
    with pytest.raises(ProcessLimitError, match="output byte bound") as error:
        run_bounded(python(source), environ=os.environ, timeout=3, max_output_bytes=1024)
    assert len(error.value.stdout) + len(error.value.stderr) == 1024
    assert error.value.outcome == "unknown"


def test_partial_output_does_not_turn_timeout_into_success_or_leak_in_exception():
    source = "import time; print('private-result-reference', flush=True); time.sleep(10)"
    with pytest.raises(ProcessLimitError, match="deadline") as error:
        run_bounded(python(source), environ=os.environ, timeout=0.2)
    assert b"private-result-reference" in error.value.stdout
    assert "private-result-reference" not in str(error.value)
    assert error.value.outcome == "unknown"


def process_running(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except FileNotFoundError:
        return False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux process-group assertion")
def test_timeout_kills_descendant_that_ignores_terminate_and_keeps_pipe_open():
    descendant = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    parent = f"import subprocess,sys; p=subprocess.Popen([sys.executable,'-c',{descendant!r}]); print(p.pid,flush=True)"
    started = time.monotonic()
    with pytest.raises(ProcessLimitError, match="deadline") as error:
        run_bounded(python(parent), environ=os.environ, timeout=0.3)
    pid = int(error.value.stdout.strip())
    for _ in range(20):
        if not process_running(pid):
            break
        time.sleep(0.01)
    assert not process_running(pid)
    assert time.monotonic() - started < 2


def test_closed_pipes_do_not_hide_a_running_child():
    with pytest.raises(ProcessLimitError, match="deadline"):
        run_bounded(python("import os,time; os.close(1); os.close(2); time.sleep(10)"),
                    environ=os.environ, timeout=0.2)


def test_nonzero_exit_is_returned_without_relaunch(tmp_path):
    marker = tmp_path / "launches"
    source = f"from pathlib import Path; p=Path({str(marker)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x'); raise SystemExit(7)"
    result = run_bounded(python(source), environ=os.environ, timeout=3)
    assert result.returncode == 7
    assert marker.read_text() == "x"


@pytest.mark.parametrize("stage", ["constructor", "set_blocking", "register", "close"])
def test_setup_failure_reaps_real_child_and_closes_all_pipes(monkeypatch, stage):
    launched = []
    original_popen = process_bounds.subprocess.Popen
    original_selector = process_bounds.selectors.DefaultSelector

    def tracked_popen(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        launched.append(child)
        return child

    def fail(*_args, **_kwargs):
        raise OSError(errno.EMFILE, f"injected {stage} failure")

    def selector_factory():
        if stage == "constructor":
            fail()
        selector = original_selector()
        if stage in ("register", "close"):
            monkeypatch.setattr(selector, "register", fail)
        if stage == "close":
            original_close = selector.close

            def failed_close():
                original_close()
                monkeypatch.setattr(selector, "close", original_close)
                fail()

            monkeypatch.setattr(selector, "close", failed_close)
        return selector

    monkeypatch.setattr(process_bounds.subprocess, "Popen", tracked_popen)
    monkeypatch.setattr(process_bounds.selectors, "DefaultSelector", selector_factory)
    if stage == "set_blocking":
        monkeypatch.setattr(process_bounds.os, "set_blocking", fail)
    try:
        with pytest.raises(OSError, match=f"injected {stage} failure") as error:
            run_bounded(python("import time; time.sleep(30)"), environ=os.environ, timeout=0.2)
        assert error.value.errno == errno.EMFILE
        assert len(launched) == 1
        child = launched[0]
        assert child.poll() is not None
        assert all(stream.closed for stream in (child.stdin, child.stdout, child.stderr))
    finally:
        # A regression must not leave the test's real process behind.
        for child in launched:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=2)
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()


@pytest.mark.parametrize("kwargs", [
    {"timeout": 0}, {"timeout": float("nan")}, {"timeout": 601}, {"timeout": True},
    {"input_data": b"ab", "max_input_bytes": 1}, {"max_output_bytes": 0},
    {"max_output_bytes": 4194305},
])
def test_invalid_envelope_refused_before_launch(tmp_path, kwargs):
    marker = tmp_path / "started"
    command = python(f"from pathlib import Path; Path({str(marker)!r}).touch()")
    with pytest.raises(ValueError, match="invalid bounded process envelope"):
        run_bounded(command, environ=os.environ, **kwargs)
    assert not marker.exists()
