"""Real process regressions for interrupting the owned browser session."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

COMMAND = Path(__file__).resolve().parents[2] / "scripts/benchmark-flow-reads.py"


def alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


@pytest.mark.parametrize("stop_reason", ["interrupt", "timeout", "launcher_exit", "detached_launcher_exit"])
def test_interrupt_drains_browser_even_when_launcher_exits(tmp_path, stop_reason):
    leader_exits = stop_reason.endswith("launcher_exit")
    output = tmp_path / "run"
    output.mkdir()
    ready = tmp_path / "browser.pid"
    late = output / "workspace/late-artifact"
    binary = tmp_path / "npx"
    binary.write_text(f"""#!{sys.executable}
import os,signal,time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
if {leader_exits!r} and os.fork():
    time.sleep(.2)
    os._exit(0)
if {(stop_reason == "detached_launcher_exit")!r}: os.setsid()
pending=Path({str(ready.with_suffix(".pending"))!r})
pending.write_text(str(os.getpid()))
pending.replace({str(ready)!r})
time.sleep(3)
p=Path({str(late)!r});p.parent.mkdir(parents=True,exist_ok=True);p.write_text('late')
time.sleep(60)
""")
    binary.chmod(0o700)
    driver = tmp_path / "driver.py"
    driver.write_text(f"""
import asyncio,importlib.util,inspect,os,sys
from pathlib import Path
sys.path.insert(0,{str(COMMAND.parent)!r})
spec=importlib.util.spec_from_file_location('command',{str(COMMAND)!r})
command=importlib.util.module_from_spec(spec);spec.loader.exec_module(command)
command.DRAIN_SECONDS=.6
command.MAX_ACTIVE_SECONDS={0.4 if stop_reason == "timeout" else 1200}
budget=command.RunBudget(Path({str(output)!r}))
async def run():
    try:
        result=command.run_browser(dict(os.environ),budget)
        if inspect.isawaitable(result): await result
    finally:
        command.cleanup_owned(budget.manifest)
asyncio.run(run())
""")
    with (tmp_path / "driver.log").open("w") as log:
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            env={**os.environ, "ADP_FLOW_READ_BENCHMARK_OWNER": "different-owned-run"},
            start_new_session=True,
        )
        process = subprocess.Popen(
            [sys.executable, str(driver)],
            env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert ready.exists(), "Owned browser fixture did not start"
            pid = int(ready.read_text())
            if stop_reason == "interrupt":
                started = time.monotonic()
                process.send_signal(signal.SIGINT)
                process.wait(timeout=2)
                assert time.monotonic() - started < 2
            else:
                process.wait(timeout=2)
            assert not alive(pid), "Launcher exit must not leave a browser writing into the workspace"
            assert not (output / "workspace").exists()
            assert unrelated.poll() is None, "A different run's marker must never be signalled"
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=5)
            manifest = output / "ownership.json"
            if manifest.exists():
                group = json.loads(manifest.read_text()).get("client_pid")
                if group:
                    try:
                        os.killpg(group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            if ready.exists() and alive(int(ready.read_text())):
                os.kill(int(ready.read_text()), signal.SIGKILL)
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


@pytest.mark.asyncio
async def test_cancelled_browser_retains_partial_samples_without_accepting_them(tmp_path, monkeypatch):
    import asyncio
    import importlib.util

    from scripts.benchmark_flow_reads import evaluate_api
    from tests.orchestration.test_flow_read_benchmark import EXPECTED, attempts, browser_attempts

    monkeypatch.syspath_prepend(str(COMMAND.parent))
    spec = importlib.util.spec_from_file_location("abort_evidence", COMMAND)
    command = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(command)
    monkeypatch.setattr(
        command,
        "load_configuration",
        lambda path: ({"api_base_url": "https://example.invalid"}, {"access_token": "private-sentinel"}, EXPECTED, "fixture"),
    )
    monkeypatch.setattr(command.subprocess, "check_output", lambda *args, **kwargs: "0" * 40)

    async def collect(client, expected, *, readers, reserve, record):
        for attempt in attempts(readers=readers):
            reserve()
            record(attempt)
        return evaluate_api(attempts(readers=readers), readers=readers)

    async def browser(environment, budget):
        journal = budget.output / "browser-1.jsonl"
        journal.write_text("".join(json.dumps(row) + "\n" for row in browser_attempts()[:3]))
        budget.browser_log.write_text("1\n" * 6)
        raise asyncio.CancelledError()

    monkeypatch.setattr(command, "collect_api", collect)
    monkeypatch.setattr(command, "run_browser", browser)
    output = tmp_path / "evidence"
    assert not await command.run_benchmark(tmp_path / "config.json", output)
    result = json.loads((output / "evidence.json").read_text())
    assert not result["complete"] and result["incomplete_reason"] == "CancelledError"
    partial = next(mode for mode in result["modes"] if mode["phase"] == "browser")
    assert partial["operations"]["list"]["observed"] == 3
    assert partial["operations"]["graph"]["observed"] == 0
    assert not partial["passed"] and result["cleanup"]["complete"]
    assert result["accounting"]["browser_attempts"] == 6
    assert "private-sentinel" not in json.dumps(result)


def test_reused_client_identity_preserves_workspace_and_never_signals(tmp_path, monkeypatch):
    import importlib.util

    monkeypatch.syspath_prepend(str(COMMAND.parent))
    spec = importlib.util.spec_from_file_location("abort_identity", COMMAND)
    command = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(command)
    budget = command.RunBudget(tmp_path)
    budget.client_pid, budget.client_start = 999999, "1"
    budget.save()
    monkeypatch.setattr(command, "process_start", lambda pid: "2")

    def signal_refused(*args):
        pytest.fail("Must not signal a reused process identity")

    monkeypatch.setattr(command.os, "killpg", signal_refused)
    receipt = command.cleanup_owned(budget.manifest)
    assert not receipt["complete"] and "client_stop_failed" in receipt["failures"]
    assert budget.workspace.is_dir() and (tmp_path / "cleanup.json").is_file()
