#!/usr/bin/env python3
"""Opt-in Delivery Flow read measurement; requires private fixture and auth files."""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import httpx
from benchmark_flow_reads import ApiExpectation, collect_api, evaluate_browser

GATEWAY = Path(__file__).resolve().parents[1]
FRONTEND = GATEWAY / "frontend"
MAX_ATTEMPTS = 3_000
MAX_ACTIVE_SECONDS = 20 * 60
DRAIN_SECONDS = 30


class RunBudget:
    def __init__(self, output: Path, manifest: Path | None = None, clock=time.monotonic):
        self.output = output
        self.manifest = manifest or output / "ownership.json"
        self.clock = clock
        self.started = clock()
        self.api_attempts = 0
        self.failures = 0
        self.run_id = str(uuid.uuid4())
        self.client_pid = None
        self.client_start = None
        self.drain_deadline = None
        self.state = "incomplete"
        self.workspace = output / "workspace"
        self.workspace.mkdir(mode=0o700)
        (self.workspace / "owner.json").write_text(json.dumps({"run_id": self.run_id}))
        self.browser_log = output / "browser-requests.log"
        self.browser_log.touch(mode=0o600, exist_ok=False)
        self.save()

    def browser_attempts(self):
        return self.browser_log.read_bytes().count(b"\n")

    def remaining_seconds(self):
        return MAX_ACTIVE_SECONDS - (self.clock() - self.started)

    def save(self):
        manifest = {"owned_output": str(self.output.resolve()), "api_attempts": self.api_attempts,
                    "browser_attempts": self.browser_attempts(), "attempt_limit": MAX_ATTEMPTS,
                    "active_seconds": self.clock() - self.started, "active_limit_seconds": MAX_ACTIVE_SECONDS,
                    "reader_limit": 5, "drain_limit_seconds": DRAIN_SECONDS, "consecutive_failures": self.failures,
                    "state": self.state, "run_id": self.run_id, "owned_workspace": str(self.workspace.resolve()),
                    "owned_fixtures": [], "client_pid": self.client_pid, "client_start": self.client_start,
                    "client_owner": self.run_id if self.client_pid is not None else None}
        temporary = self.manifest.with_name(self.manifest.name + ".tmp")
        temporary.write_text(json.dumps(manifest, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(self.manifest)

    def reserve(self):
        if self.api_attempts + self.browser_attempts() >= MAX_ATTEMPTS or self.remaining_seconds() <= 0 or self.failures >= 5:
            raise RuntimeError("run budget exhausted")
        self.api_attempts += 1
        self.save()

    def record(self, attempt):
        with (self.output / "api-attempts.jsonl").open("a") as journal:
            journal.write(json.dumps(asdict(attempt)) + "\n")
        (self.output / "api-attempts.jsonl").chmod(0o600)
        self.failures = self.failures + 1 if not attempt.success else 0
        self.save()


def process_start(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


CLIENT_OWNER_ENV = "ADP_FLOW_READ_BENCHMARK_OWNER"


def marked_clients(owner: str | None, start: str) -> list[tuple[int, str]]:
    """Find this launch's detached children even after their parent has exited."""
    if owner is None:
        return []
    marker = f"{CLIENT_OWNER_ENV}={owner}".encode()
    clients = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] == "Z" or int(fields[19]) < int(start):
                continue
            # The marker is passed only in the launched child's environment,
            # never added to this process or to unrelated operator commands.
            if marker in (entry / "environ").read_bytes().split(b"\0"):
                clients.append((int(entry.name), fields[19]))
        except (OSError, IndexError):
            continue
    return clients


def active_owned_group(pid: int, start: str, owner: str | None = None) -> bool:
    """An exited launcher does not imply its browser descendants have exited."""
    current = process_start(pid)
    if current is not None and current != start:
        raise ValueError("client process identity changed")
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if int(fields[2]) != pid:
            continue
        if int(fields[3]) != pid or int(fields[19]) < int(start):
            raise ValueError("client session identity changed")
        if fields[0] != "Z":
            return True
    return bool(marked_clients(owner, start))


def stop_owned_group(pid: int, start: str, *, owner: str | None = None, reap=lambda: None, deadline: float | None = None) -> bool:
    """Drain only the recorded session, including children after launcher exit."""
    deadline = deadline if deadline is not None else time.monotonic() + DRAIN_SECONDS
    for sig, until in ((signal.SIGTERM, deadline - min(5, DRAIN_SECONDS / 2)), (signal.SIGKILL, deadline)):
        reap()
        if not active_owned_group(pid, start, owner):
            return True
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass  # Detached children may still carry this launch's marker.
        signalled = set()
        while True:
            for child, identity in marked_clients(owner, start):
                if (child, identity) not in signalled and process_start(child) == identity:
                    try:
                        os.kill(child, sig)
                    except ProcessLookupError:
                        pass
                    signalled.add((child, identity))
            reap()
            if not active_owned_group(pid, start, owner):
                return True
            if time.monotonic() >= until:
                break
            time.sleep(min(0.1, max(0, until - time.monotonic())))
    reap()
    return not active_owned_group(pid, start, owner)


def cleanup_owned(manifest: Path, *, drain_deadline: float | None = None) -> dict:
    ownership = json.loads(manifest.read_text())
    output = Path(ownership["owned_output"])
    workspace = Path(ownership["owned_workspace"])
    run_id = ownership["run_id"]
    if workspace != output / "workspace" or manifest.is_symlink() or output.is_symlink() or not output.is_dir():
        raise ValueError("manifest does not identify an owned workspace")
    receipt = {"run_id": run_id, "removed_workspace": False, "client_stopped": False,
               "owned_fixture_remnants": ownership.get("owned_fixtures", []), "failures": []}
    pid, start = ownership.get("client_pid"), ownership.get("client_start")
    receipt["client_stopped"] = pid is None
    owner = ownership.get("client_owner")
    if owner is not None and owner != run_id:
        raise ValueError("client ownership marker mismatch")
    if pid is not None and start is None:
        receipt["failures"].append("client_identity_unavailable")
    if pid is not None and start is not None:
        try:
            receipt["client_stopped"] = stop_owned_group(pid, start, owner=owner, deadline=drain_deadline)
            if not receipt["client_stopped"]:
                receipt["failures"].append("client_not_stopped")
        except (OSError, ValueError):
            receipt["failures"].append("client_stop_failed")
    # A live writer can recreate the directory after deletion. Retain its
    # ownership marker until the whole session is confirmed stopped.
    if not receipt["failures"] and (workspace.exists() or workspace.is_symlink()):
        try:
            if workspace.is_symlink() or json.loads((workspace / "owner.json").read_text())["run_id"] != run_id:
                raise ValueError("workspace ownership mismatch")
            shutil.rmtree(workspace)
            receipt["removed_workspace"] = True
        except (OSError, ValueError, KeyError):
            receipt["failures"].append("workspace_cleanup_failed")
    receipt["workspace_remains"] = workspace.exists() or workspace.is_symlink()
    receipt["complete"] = not receipt["failures"] and not receipt["owned_fixture_remnants"] and not receipt["workspace_remains"]
    destination = output / "cleanup.json"
    destination.write_text(json.dumps(receipt, indent=2) + "\n")
    destination.chmod(0o600)
    journal = output / "cleanup-receipts.jsonl"
    with journal.open("a") as receipts:
        receipts.write(json.dumps(receipt) + "\n")
    journal.chmod(0o600)
    if not receipt["complete"]:
        ownership["state"] = "incomplete"
        temporary = manifest.with_name(manifest.name + ".tmp")
        temporary.write_text(json.dumps(ownership, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(manifest)
    return receipt


async def run_browser(environment: dict[str, str], budget: RunBudget) -> int:
    remaining = budget.remaining_seconds()
    if remaining <= 0:
        raise TimeoutError("active time limit reached")
    process = subprocess.Popen(
        ["npx", "playwright", "test", "--config", "tests/e2e/flow-read-benchmark.config.ts"],
        cwd=FRONTEND, env={**environment, CLIENT_OWNER_ENV: budget.run_id}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    budget.client_pid = process.pid
    budget.client_start = process_start(process.pid)
    budget.save()
    try:
        while process.poll() is None:
            if budget.remaining_seconds() <= 0:
                raise TimeoutError("active time limit reached")
            # Keep the event loop responsive to SIGINT cancellation. A blocking
            # wait here leaves the browser running until a second interrupt.
            await asyncio.sleep(0.1)
        return process.returncode
    finally:
        budget.drain_deadline = time.monotonic() + DRAIN_SECONDS
        if budget.client_start is None or not stop_owned_group(
            process.pid, budget.client_start, owner=budget.run_id, reap=process.poll, deadline=budget.drain_deadline
        ):
            raise RuntimeError("owned browser session did not stop")
        budget.client_pid = None
        budget.client_start = None
        budget.save()


def load_configuration(path: Path):
    config = json.loads(path.read_text())
    required = ("frontend_url", "api_base_url", "auth_file", "list_flow_ids", "graph_flow_id", "graph_title",
                "graph_node_ids", "graph_node_refs", "graph_edges")
    if any(not config.get(field) for field in required[:8]) or "graph_edges" not in config:
        raise ValueError("private configuration must specify both URLs, auth file and nonempty expected list/graph content")
    if not isinstance(config.get("list_query", "fixture-flow"), str):
        raise ValueError("list_query must be a string; use an empty string for the unfiltered page")
    auth = json.loads(Path(config["auth_file"]).read_text())
    if not auth.get("access_token") or not auth.get("id_token") or auth.get("expiry_ms", 0) <= time.time() * 1000:
        raise ValueError("a current private access and ID token are required")
    expected = ApiExpectation(tuple(config["list_flow_ids"]), config["graph_flow_id"], tuple(config["graph_node_ids"]),
                              tuple(tuple(edge) for edge in config["graph_edges"]), config.get("list_query", "fixture-flow"))
    safe_config = {key: value for key, value in config.items() if key not in ("frontend_url", "api_base_url", "auth_file")}
    return config, auth, expected, hashlib.sha256(json.dumps(safe_config, sort_keys=True).encode()).hexdigest()


def browser_reports(output: Path) -> list[dict]:
    reports = []
    for readers in (1, 5):
        capture = output / f"browser-{readers}.json"
        journal = output / f"browser-{readers}.jsonl"
        if capture.is_file():
            attempts = json.loads(capture.read_text())["attempts"]
        elif journal.is_file():
            attempts = [json.loads(line) for line in journal.read_text().splitlines()]
        else:
            continue
        reports.append(evaluate_browser(attempts, readers=readers))
    return reports


async def run_benchmark(config_path: Path, output: Path, manifest: Path | None = None) -> bool:
    config, auth, expected, config_revision = load_configuration(config_path)
    if manifest is not None and not cleanup_owned(manifest)["complete"]:
        return False
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    budget = RunBudget(output, manifest=manifest)
    report = {
        "source_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=GATEWAY, text=True).strip(),
        "config_revision": config_revision,
        "fixture_revision": hashlib.sha256((GATEWAY / "tests/orchestration/flow_read_fixture.py").read_bytes()).hexdigest(),
        "selection": {"list": {"q": expected.list_query, "sort": "created", "limit": 25, "offset": 0},
                      "graph": "private configured flow", "readers": [1, 5], "api_samples_per_operation": 100,
                      "browser_navigations_per_page": 20, "request_timeout_seconds": 15,
                      "attempt_limit": MAX_ATTEMPTS, "active_limit_seconds": MAX_ACTIVE_SECONDS,
                      "drain_limit_seconds": DRAIN_SECONDS},
        "modes": [], "complete": False,
    }
    try:
        async with httpx.AsyncClient(base_url=config["api_base_url"], headers={"Authorization": f"Bearer {auth['access_token']}"}) as client:
            for readers in (1, 5):
                report["modes"].append(await asyncio.wait_for(
                    collect_api(client, expected, readers=readers, reserve=budget.reserve, record=budget.record),
                    timeout=max(0.001, budget.remaining_seconds()),
                ))
        environment = {**os.environ, "FLOW_READ_CONFIG": str(config_path.resolve()), "FLOW_READ_CAPTURE_DIR": str(output.resolve()),
                       "FLOW_READ_BUDGET_LOG": str(budget.browser_log.resolve()),
                       "FLOW_READ_ARTIFACT_DIR": str((budget.workspace / "browser-artifacts").resolve()),
                       "FLOW_READ_REMAINING_ATTEMPTS": str(MAX_ATTEMPTS - budget.api_attempts),
                       "FLOW_READ_REMAINING_SECONDS": str(budget.remaining_seconds())}
        environment.pop("FLOW_READ_MOCK", None)
        completed = await run_browser(environment, budget)
        report["modes"].extend(browser_reports(output))
        report["complete"] = completed == 0 and len(report["modes"]) == 4 and all(
            mode["passed"] for mode in report["modes"]
        ) and budget.api_attempts + budget.browser_attempts() <= MAX_ATTEMPTS and budget.remaining_seconds() >= 0
        if not report["complete"]:
            report["incomplete_reason"] = "browser_runner_failed" if completed else "missing_or_failed_samples"
    except (TimeoutError, OSError, ValueError, RuntimeError, KeyboardInterrupt, asyncio.CancelledError) as exc:
        report["incomplete_reason"] = type(exc).__name__
    finally:
        # Read partial captures even when cancellation interrupted run_browser.
        if not any(mode.get("phase") == "browser" for mode in report["modes"]):
            try:
                report["modes"].extend(browser_reports(output))
            except (OSError, ValueError):
                report["complete"] = False
                report["incomplete_reason"] = "browser_capture_invalid"
        receipt = cleanup_owned(budget.manifest, drain_deadline=budget.drain_deadline)
        report["cleanup"] = receipt
        if not receipt["complete"]:
            report["complete"] = False
            report["incomplete_reason"] = "cleanup_failed"
        budget.state = "complete" if report["complete"] else "incomplete"
        budget.save()
        report["accounting"] = {"api_attempts": budget.api_attempts, "browser_attempts": budget.browser_attempts(),
                                "active_seconds": budget.clock() - budget.started}
        destination = output / "evidence.json"
        destination.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
        destination.chmod(0o600)
    return report["complete"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="private synthetic fixture configuration")
    parser.add_argument("--output", type=Path, help="new private result directory")
    parser.add_argument("--manifest", type=Path, help="existing private ownership manifest for a new output directory")
    parser.add_argument("--cleanup", action="store_true", help="retry cleanup without running a benchmark")
    arguments = parser.parse_args()
    if arguments.cleanup:
        if not arguments.manifest or arguments.config or arguments.output:
            parser.error("--cleanup requires only --manifest")
        return 0 if cleanup_owned(arguments.manifest)["complete"] else 1
    if not arguments.config or not arguments.output:
        parser.error("--config and --output are required for a run")
    return 0 if asyncio.run(run_benchmark(arguments.config, arguments.output, arguments.manifest)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
