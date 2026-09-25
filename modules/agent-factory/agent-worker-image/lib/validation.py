"""Commit-scoped local validation evidence; not an acceptance or CI authority.

Commands run in disposable detached worktrees. Include dependency setup in the
command; author-worktree virtualenvs/node_modules are deliberately not shared.
Receipts/logs live in the worktree's Git directory, never in the source tree.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import signal
import shutil
import subprocess
import sys
import tempfile
import time


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def state_dir(repo: Path) -> Path:
    folder = Path(git(repo, "rev-parse", "--absolute-git-dir")) / "adp-validation"
    folder.mkdir(mode=0o700, exist_ok=True)
    return folder


def environment_id(folder: Path) -> str:
    # Salt prevents stored hashes being used to guess low-entropy secrets.
    salt = folder / "salt"
    if not salt.exists():
        salt.write_bytes(os.urandom(32))
        salt.chmod(0o600)
    environment = {k: v for k, v in os.environ.items() if k not in {"PWD", "OLDPWD", "SHLVL", "_"}}
    # Runtime/toolchain installations are outside Git. Include their identity;
    # mutable external services still require --no-cache.
    binaries = {}
    for name in (sys.executable, "node", "npm", "git", "bash", "ruff", "terraform"):
        executable = shutil.which(name)
        if executable:
            stat = Path(executable).stat()
            binaries[executable] = [stat.st_size, stat.st_mtime_ns]
    packages = sorted(
        (d.metadata.get("Name", ""), d.version) for d in importlib.metadata.distributions()
    )
    identity = [environment, sys.version, sys.executable, platform.platform(), binaries, packages]
    return hashlib.sha256(
        salt.read_bytes() + json.dumps(identity, sort_keys=True).encode()
    ).hexdigest()


def load_manifest(folder: Path) -> list[dict]:
    file = folder / "commands.json"
    return json.loads(file.read_text()) if file.exists() else []


def atomic_json(file: Path, value: object) -> None:
    temporary = file.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(file)


def clean(repo: Path) -> bool:
    return not git(repo, "status", "--porcelain")


def key_for(head: str, spec: dict, environment: str) -> str:
    return hashlib.sha256(
        json.dumps([head, spec, environment], sort_keys=True).encode()
    ).hexdigest()


def verify(repo: Path, *, strict_environment: bool = True) -> tuple[bool, str]:
    """Inspect only: never rerun arbitrary long commands during finalization."""
    folder = state_dir(repo)
    with (folder / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        specs = load_manifest(folder)
        if not specs:
            return False, "No validation commands recorded; checks are unverified."
        if not clean(repo):
            return False, "Working tree has uncommitted files; final commit is unverified."
        head = git(repo, "rev-parse", "HEAD")
        environment = environment_id(folder) if strict_environment else None
        latest_path = folder / "latest.json"
        latest = json.loads(latest_path.read_text()) if latest_path.exists() else {}
        for spec in specs:
            key = latest.get(json.dumps(spec, sort_keys=True), "missing")
            receipt_path = folder / (key + ".json")
            receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
            if (
                not receipt.get("passed")
                or receipt.get("commit") != head
                or (strict_environment and key != key_for(head, spec, environment))
            ):
                return False, f"Missing passing validation for final commit {head}: {spec['argv']}"
        return (
            True,
            f"{len(specs)} recorded command(s) passed for {head}. Requirement coverage remains report-only.",
        )


def run(
    repo: Path, argv: list[str], cwd: str = ".", timeout: int = 3600, reuse: bool = True
) -> dict:
    if not argv or timeout <= 0:
        raise ValueError("A command and positive timeout are required")
    relative = Path(cwd)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("--cwd must be relative to the repository, without '..'")
    folder = state_dir(repo)
    with (folder / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not clean(repo):
            raise ValueError(
                "Commit intended changes first; validation requires a clean working tree"
            )
        head = git(repo, "rev-parse", "HEAD")
        spec = {"argv": argv, "cwd": str(relative), "timeout": timeout}
        specs = load_manifest(folder)
        if spec not in specs:
            specs.append(spec)
            atomic_json(folder / "commands.json", specs)
        environment = environment_id(folder)
        key = key_for(head, spec, environment)
        latest_path = folder / "latest.json"
        latest = json.loads(latest_path.read_text()) if latest_path.exists() else {}
        latest[json.dumps(spec, sort_keys=True)] = key
        atomic_json(latest_path, latest)
        receipt_path = folder / (key + ".json")
        if reuse and receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            if receipt.get("passed"):
                return {**receipt, "reused": True}
        # Invalidate old success before starting a forced rerun (including crashes).
        receipt_path.unlink(missing_ok=True)
        started = time.monotonic()
        log = folder / (key + ".log")
        with tempfile.TemporaryDirectory(prefix="adp-validation-") as temporary:
            snapshot = Path(temporary) / "source"
            git(repo, "worktree", "add", "--detach", str(snapshot), head)
            try:
                target = (snapshot / relative).resolve()
                if not target.is_relative_to(snapshot):
                    raise ValueError("Validation directory escapes the snapshot")
                print(f"Validating {head}; output: {log}", flush=True)
                with log.open("w") as output:
                    process = subprocess.Popen(
                        argv,
                        cwd=target,
                        env={**os.environ, "PWD": str(target)},
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    try:
                        code = process.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                        code = 124
                    finally:
                        # Do not leave background children modifying a deleted worktree.
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                unchanged = git(snapshot, "rev-parse", "HEAD") == head and not git(
                    snapshot, "status", "--porcelain", "--untracked-files=no"
                )
                receipt = {
                    "commit": head,
                    **spec,
                    "environment": environment,
                    "exit_code": code,
                    "inputs_unchanged": unchanged,
                    "passed": code == 0 and unchanged,
                    "log": str(log),
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "reused": False,
                }
                atomic_json(receipt_path, receipt)
                return receipt
            finally:
                git(repo, "worktree", "remove", "--force", str(snapshot))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["run", "verify", "reset"])
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--no-cache", action="store_true")
    # Parse options before '--'; preserve command arguments exactly.
    args_list = sys.argv[1:]
    separator = args_list.index("--") if "--" in args_list else len(args_list)
    args = parser.parse_args(args_list[:separator])
    argv = args_list[separator + 1 :]
    try:
        repo = Path(git(Path.cwd(), "rev-parse", "--show-toplevel"))
        if args.action == "reset":
            folder = state_dir(repo)
            with (folder / "lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                atomic_json(folder / "commands.json", [])
            print("Validation plan cleared. Run all checks in the replacement plan before verify.")
            return 0
        if args.action == "verify":
            passed, message = verify(repo)
            print(message)
        else:
            result = run(repo, argv, args.cwd, args.timeout, not args.no_cache)
            print(json.dumps(result, indent=2))
            passed = result["passed"]
        return 0 if passed else 1
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"Validation unavailable: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
