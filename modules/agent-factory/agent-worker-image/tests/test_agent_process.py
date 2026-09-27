import sys
import time

from lib.agent_process import run_agent


def test_preserves_output_and_exit():
    result = run_agent([sys.executable, "-c", "import sys; print(input()); sys.exit(3)"],
                       input="payload", capture_output=True, text=True)
    assert result.returncode == 3
    assert result.stdout == "payload\n"


def test_deadline_kills_descendants_and_preserves_diagnostics(tmp_path):
    marker = tmp_path / "escaped"
    script = "import time; from pathlib import Path; time.sleep(0.5); Path(%r).touch()" % str(marker)
    leader = (
        "import subprocess,sys,time,signal; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"subprocess.Popen([sys.executable, '-c', {script!r}]); "
        "print('diagnostic',flush=True); time.sleep(30)"
    )
    started = time.monotonic()
    result = run_agent([sys.executable, "-c", leader], timeout=0.1, grace=0.1, capture_output=True, text=True)
    assert result.returncode == 124
    assert "deadline exceeded" in result.stderr
    assert "diagnostic" in result.stdout
    assert time.monotonic() - started < 3
    time.sleep(0.6)
    assert not marker.exists()
