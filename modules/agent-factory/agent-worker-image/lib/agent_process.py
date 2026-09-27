"""Bound the whole agent process tree, allowing a short checkpoint grace period."""

import os
import hashlib
import json
import urllib.request
import time
import signal
import subprocess


DEFAULT_TIMEOUT_SECONDS = 2 * 60 * 60


def repository_fingerprint(cwd):
    """Observe work without staging, committing, or logging file contents."""
    try:
        parts = []
        for args in (["git", "rev-parse", "HEAD"], ["git", "diff", "--no-ext-diff", "HEAD"],
                     ["git", "ls-files", "--others", "--exclude-standard"]):
            result = subprocess.run(args, cwd=cwd, capture_output=True, timeout=5, check=True)
            parts.append(result.stdout)
        return hashlib.sha256(b"\0".join(parts)).digest()
    except (OSError, subprocess.SubprocessError):
        return None


def progress_is_suspended(env):
    """Do not count an operator pause as stalled work; no model calls or writes."""
    if not env.get("ADP_CONTROL_TOKEN"):
        return False
    try:
        address = env["ADP_CONTROL_BIND_ADDRESS"]
        host = f"[{address}]" if ":" in address else address
        request = urllib.request.Request(
            f"http://{host}:{int(env['ADP_CONTROL_PORT'])}/agent/state",
            headers={"Authorization": "Bearer " + env["ADP_CONTROL_TOKEN"],
                     "X-Adp-Control-Generation": env["ADP_CONTROL_GENERATION"]})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=2) as response:
            state = json.load(response).get("state")
        return state != "running"
    except (OSError, ValueError, KeyError):
        return True  # Unknown control state cannot establish active-work time.


def run_agent(command, *, timeout=DEFAULT_TIMEOUT_SECONDS, grace=30,
              first_progress_timeout=None, progress_poll_seconds=30, progress_suspended=None, **options):
    env = options.get("env") or {}
    if first_progress_timeout is None and env.get("ADP_REQUIRE_IMPLEMENTATION_PROGRESS") == "true":
        first_progress_timeout = 30 * 60
        progress_suspended = lambda: progress_is_suspended(env)
    payload = options.pop("input", None)
    if options.pop("capture_output", False):
        options.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if payload is not None:
        options["stdin"] = subprocess.PIPE
    started = time.monotonic()
    baseline = repository_fingerprint(options.get("cwd")) if first_progress_timeout else None
    observing_progress = baseline is not None
    stalled = False
    active_seconds = 0.0
    sampled_at = started
    was_suspended = False
    with subprocess.Popen(command, start_new_session=True, **options) as child:
        try:
            while True:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = child.communicate(payload, timeout=min(remaining, progress_poll_seconds) if observing_progress else remaining)
                    return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    payload = None  # communicate retains already-sent input/output.
                    if not observing_progress:
                        raise
                    now = time.monotonic()
                    suspended = bool(progress_suspended and progress_suspended())
                    if not suspended and not was_suspended:
                        active_seconds += now - sampled_at
                    sampled_at, was_suspended = now, suspended
                    current = repository_fingerprint(options.get("cwd"))
                    if current is None:
                        # A transient Git read failure cannot prove no progress.
                        observing_progress = False
                    elif current != baseline:
                        observing_progress = False
                    elif active_seconds >= first_progress_timeout:
                        stalled = True
                        raise
        except subprocess.TimeoutExpired:
            # A new session isolates the agent and its descendants from the worker
            # and credential sidecar. Keep those alive for terminal reporting.
            def stop(sig):
                try:
                    os.killpg(child.pid, sig)
                except ProcessLookupError:
                    pass

            stop(signal.SIGTERM)
            try:
                stdout, stderr = child.communicate(timeout=grace)
            except subprocess.TimeoutExpired:
                stop(signal.SIGKILL)
                stdout, stderr = child.communicate()
            finally:
                # Also reap descendants that ignored TERM after the leader exited.
                stop(signal.SIGKILL)
            suffix = ("\nDeveloper stalled: no repository change or commit within the first 30 minutes; "
                      "preserve investigation evidence and inspect before retrying" if stalled
                      else "\nAgent wall-clock deadline exceeded")
            if isinstance(stderr, bytes):
                suffix = suffix.encode()
            return subprocess.CompletedProcess(command, 125 if stalled else 124, stdout, (stderr or (b"" if isinstance(suffix, bytes) else "")) + suffix)
