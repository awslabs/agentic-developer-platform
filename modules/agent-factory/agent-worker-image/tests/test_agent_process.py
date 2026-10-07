import sys
import time
from unittest.mock import MagicMock

import pytest

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


def test_no_progress_stops_implementation_and_preserves_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr("lib.agent_process.repository_fingerprint", lambda cwd: b"unchanged")
    result = run_agent([sys.executable, "-c", "import time; print('investigating',flush=True); time.sleep(5)"],
                       cwd=tmp_path, first_progress_timeout=0.1, progress_poll_seconds=0.03,
                       grace=0.1, capture_output=True, text=True)
    assert result.returncode == 125
    assert "no repository change" in result.stderr
    assert "investigating" in result.stdout


def test_repository_progress_allows_validation_to_continue(tmp_path, monkeypatch):
    values = iter([b"baseline", b"change"])
    monkeypatch.setattr("lib.agent_process.repository_fingerprint", lambda cwd: next(values))
    result = run_agent([sys.executable, "-c", "import time; time.sleep(0.3); print('validated')"],
                       cwd=tmp_path, first_progress_timeout=0.1, progress_poll_seconds=0.03,
                       capture_output=True, text=True)
    assert result.returncode == 0
    assert "validated" in result.stdout


def test_unverifiable_workspace_does_not_claim_stalled_progress(monkeypatch):
    monkeypatch.setattr("lib.agent_process.repository_fingerprint", lambda cwd: None)
    result = run_agent([sys.executable, "-c", "import time; time.sleep(0.2)"],
                       first_progress_timeout=0.05, progress_poll_seconds=0.01)
    assert result.returncode == 0


def test_real_git_fingerprint_detects_changes_and_ignores_tmp(tmp_path):
    import subprocess
    from lib.agent_process import repository_fingerprint

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "tracked.py").write_text("before\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)
    baseline = repository_fingerprint(tmp_path)
    assert baseline is not None and repository_fingerprint(tmp_path) == baseline
    (tmp_path / "tracked.py").write_text("after\n")
    assert repository_fingerprint(tmp_path) != baseline


def test_operator_pause_does_not_spend_first_progress_budget(monkeypatch):
    monkeypatch.setattr("lib.agent_process.repository_fingerprint", lambda cwd: b"unchanged")
    result = run_agent([sys.executable, "-c", "import time; time.sleep(0.2)"],
                       first_progress_timeout=0.05, progress_poll_seconds=0.02,
                       progress_suspended=lambda: True)
    assert result.returncode == 0


def test_control_state_only_counts_running_work(monkeypatch):
    import io
    from lib.agent_process import progress_is_suspended

    class Opener:
        state = "paused"
        def open(self, request, timeout):
            assert request.full_url == "http://127.0.0.1:8765/agent/state"
            assert request.get_header("Authorization") == "Bearer test-token"
            return io.BytesIO(('{"state": "%s"}' % self.state).encode())

    opener = Opener()
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: opener)
    env = {"ADP_CONTROL_TOKEN": "test-token", "ADP_CONTROL_BIND_ADDRESS": "127.0.0.1",
           "ADP_CONTROL_PORT": "8765", "ADP_CONTROL_GENERATION": "1"}
    assert progress_is_suspended(env)
    opener.state = "running"
    assert not progress_is_suspended(env)
    opener.state = "unknown"
    assert progress_is_suspended(env)
    assert progress_is_suspended({"ADP_CONTROL_TOKEN": "test-token"})
    assert not progress_is_suspended({})


@pytest.mark.parametrize("persona,expected", [
    ("developer", 21600), ("reviewer", 21600),
    ("agent-codex-developer", 21600), ("agent-codex-reviewer", 21600),
    ("agent-codex-architect", 7200), ("architect", 7200), ("investigator", 7200),
    ("", 7200),
])
@pytest.mark.parametrize("explicit_timeout", [None, 12])
def test_persona_deadline_reaches_child(monkeypatch, persona, expected, explicit_timeout):
    child = MagicMock()
    child.__enter__.return_value = child
    child.communicate.return_value = ("finished", "")
    child.returncode = 0
    monkeypatch.setattr("lib.agent_process.subprocess.Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr("lib.agent_process.time.monotonic", lambda: 100)
    result = run_agent(["agent"], env={"AGENT_TYPE": persona}, timeout=explicit_timeout)
    child.communicate.assert_called_once_with(None, timeout=expected if explicit_timeout is None else explicit_timeout)
    assert result.returncode == 0


def test_inherited_persona_uses_six_hour_deadline(monkeypatch):
    monkeypatch.setenv("AGENT_TYPE", "agent-codex-developer")
    child = MagicMock()
    child.__enter__.return_value = child
    child.communicate.return_value = (None, None)
    monkeypatch.setattr("lib.agent_process.subprocess.Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr("lib.agent_process.time.monotonic", lambda: 100)
    run_agent(["agent"])
    child.communicate.assert_called_once_with(None, timeout=21600)
