"""Real kernel negative probes. Linux CI must run these, without mocks/skips."""
import json
import os
from pathlib import Path
import sys
import subprocess

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from isolation import IsolationError, run_isolated


@pytest.fixture(autouse=True)
def trusted_fixture_diagnostics(monkeypatch):
    # Test fixtures contain no tenant data. Keep their stderr available on CI
    # failure without exposing arbitrary sample output in production errors.
    original = subprocess.Popen

    class DiagnosticProcess(original):
        def __init__(self, *args, **kwargs):
            self.diagnostic_file = kwargs.get("stderr")
            super().__init__(*args, **kwargs)

        def wait(self, *args, **kwargs):
            status = super().wait(*args, **kwargs)
            if status and hasattr(self.diagnostic_file, "seek"):
                self.diagnostic_file.seek(0)
                print(self.diagnostic_file.read(8192).decode(errors="replace"))
            return status

    monkeypatch.setattr(subprocess, "Popen", DiagnosticProcess)


def execute(tmp_path, source, *, timeout=10):
    script = tmp_path / "probe.py"
    script.write_text(source)
    return run_isolated([sys.executable, "-I", str(script)], [script], timeout=timeout)


def test_unsupported_platform_fails_closed(tmp_path):
    if sys.platform == "linux":
        assert execute(tmp_path, 'print("{}")') == {}
    else:
        with pytest.raises(IsolationError):
            execute(tmp_path, 'print("{}")')


@pytest.mark.skipif(sys.platform != "linux", reason="Linux kernel enforcement; required by cyber-security-ci")
class TestKernelConfinement:
    def test_legitimate_script_and_subprocess(self, tmp_path):
        result = execute(tmp_path, '''import hashlib, json, subprocess, sys, tempfile
from pathlib import Path
p = Path(tempfile.gettempdir()) / "out"
p.write_text("legitimate")
child = subprocess.run([sys.executable, "-I", "-c", "print(42)"], capture_output=True, text=True, check=True)
print(json.dumps({"value": p.read_text(), "child": child.stdout.strip()}))
''')
        assert result == {"value": "legitimate", "child": "42"}

    def test_no_token_environment_parent_memory_or_other_job(self, tmp_path, monkeypatch):
        # A harmless canary stands in for a projected token/another job's file.
        secret = tmp_path / "other-job-token"
        secret.write_text("must-not-be-readable")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-be-inherited")
        result = execute(tmp_path, f'''import json, os
from pathlib import Path
denied = []
for name in {json.dumps([str(secret), '/proc/self/environ', f'/proc/{os.getpid()}/mem', '/var/run/secrets/kubernetes.io/serviceaccount/token'])}:
    try:
        Path(name).read_bytes()
        denied.append(False)
    except OSError:
        denied.append(True)
print(json.dumps({{"denied": denied, "credential": os.environ.get("AWS_SECRET_ACCESS_KEY")}}))
''')
        assert result == {"denied": [True] * 4, "credential": None}

    @pytest.mark.parametrize("source", [
        "import socket; socket.socket()",
        "import os; os.setsid()",
        "import os; os.setpgid(0,0)",
        "import os; os.kill(os.getppid(), 0)",
        "import ctypes; c=ctypes.CDLL(None,use_errno=True); assert c.ptrace(0,0,0,0) == 0",
        "import pathlib; pathlib.Path('/app/isolation.py').write_text('modified')",
        "import subprocess,sys; subprocess.run([sys.executable,'-I','-c','import socket; socket.socket()'],check=True)",
    ])
    def test_escape_attempts_fail(self, tmp_path, source):
        with pytest.raises(IsolationError):
            execute(tmp_path, source + '\nprint("{}")')

    def test_output_and_wall_time_are_bounded(self, tmp_path):
        with pytest.raises(IsolationError):
            execute(tmp_path, 'print("x" * (2 * 1024 * 1024))')
        with pytest.raises(IsolationError, match="timeout"):
            execute(tmp_path, 'import time; time.sleep(60)', timeout=0.2)

    def test_background_descendant_cannot_outlive_success(self, tmp_path):
        result = execute(tmp_path, '''import json, os, time
pid = os.fork()
if pid == 0:
    time.sleep(60)
    os._exit(0)
print(json.dumps({"child": pid}), flush=True)
''')
        # A just-killed orphan can briefly remain a zombie until init reaps it.
        stat = Path(f"/proc/{result['child']}/stat")
        if stat.exists():
            assert stat.read_text().split()[2] == "Z"

    @pytest.mark.parametrize("mode", ["triage", "static"])
    def test_native_parser_modes_in_the_same_sandbox(self, tmp_path, mode):
        sample = tmp_path / "sample"
        sample.write_bytes(b"safe sample fixture with printable content")
        options = tmp_path / "options.json"
        options.write_text("{}")
        command = [sys.executable, "-I", str(Path(__file__).resolve().parents[1] / "analyze.py"), mode, str(sample), str(options)]
        result = run_isolated(command, [sample, options])
        assert ("hashes" if mode == "triage" else "sections") in result
