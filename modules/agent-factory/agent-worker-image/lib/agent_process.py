"""Bound the whole agent process tree, allowing a short checkpoint grace period."""

import os
import signal
import subprocess


DEFAULT_TIMEOUT_SECONDS = 2 * 60 * 60


def run_agent(command, *, timeout=DEFAULT_TIMEOUT_SECONDS, grace=30, **options):
    payload = options.pop("input", None)
    if options.pop("capture_output", False):
        options.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if payload is not None:
        options["stdin"] = subprocess.PIPE
    with subprocess.Popen(command, start_new_session=True, **options) as child:
        try:
            stdout, stderr = child.communicate(payload, timeout=timeout)
            return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
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
            suffix = "\nAgent wall-clock deadline exceeded"
            if isinstance(stderr, bytes):
                suffix = suffix.encode()
            return subprocess.CompletedProcess(command, 124, stdout, (stderr or (b"" if isinstance(suffix, bytes) else "")) + suffix)
