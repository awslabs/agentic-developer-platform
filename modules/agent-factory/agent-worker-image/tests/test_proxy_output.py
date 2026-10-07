"""The model proxy must keep publishing diagnostics throughout a long run."""

import os
import subprocess
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import entrypoint


def test_proxy_output_larger_than_a_pipe_reaches_worker_logs(monkeypatch, tmp_path, capfd):
    ready = tmp_path / "ready"
    proxy = tmp_path / "proxy.py"
    proxy.write_text(
        "import pathlib, signal, sys\n"
        "sys.stdout.write('proxy-output-' * 20000 + '\\n')\n"
        "sys.stdout.flush()\n"
        "sys.stderr.write('proxy-error-visible\\n')\n"
        "sys.stderr.flush()\n"
        f"pathlib.Path({str(ready)!r}).touch()\n"
        "signal.pause()\n"
    )
    real_popen = subprocess.Popen
    children = []

    def launch(command, **kwargs):
        assert command == ["node", str(proxy)]
        child = real_popen([sys.executable, str(proxy)], **kwargs)
        children.append(child)
        return child

    def health(_url, **_kwargs):
        if not ready.exists():
            raise urllib.error.URLError("proxy has not finished writing diagnostics")
        return SimpleNamespace(status=200)

    monkeypatch.setattr(entrypoint, "SIGV4_PROXY_SCRIPT", str(proxy))
    monkeypatch.setattr(entrypoint, "SIGV4_PROXY_HEALTH_TIMEOUT", 2)
    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(entrypoint.urllib.request, "urlopen", health)
    try:
        process = entrypoint._start_sigv4_proxy(
            {**os.environ, "SIGV4_PROXY_TARGET": "https://gateway.example.invalid/agent"},
            "test-tenant",
        )
        assert process is not None, "proxy blocked writing into an unread log pipe"
        output = capfd.readouterr().out
        assert output.count("proxy-output-") == 20000
        assert "proxy-error-visible" in output
    finally:
        for child in children:
            entrypoint._stop_sigv4_proxy(child)
