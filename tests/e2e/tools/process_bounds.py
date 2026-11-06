"""Finite subprocess I/O and lifetime for installed/native test clients.

This bounds the local process only. It is deliberately not a model-token,
provider-request, spend, or provider-session cleanup guarantee (#8078).
There is exactly one launch and no retry after an uncertain process outcome.
"""

from contextlib import ExitStack
from dataclasses import dataclass
import math
import os
import selectors
import signal
import subprocess
import time


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class ProcessLimitError(ValueError):
    """Keep bounded partial bytes privately; never include them in the message."""

    def __init__(self, reason, stdout=b"", stderr=b""):
        super().__init__(reason)
        self.stdout = stdout
        self.stderr = stderr
        self.outcome = "unknown"


def _stop_group(process):
    # The evaluator owns only this new session/process group. Do not enumerate
    # or signal unrelated CLI/auth-proxy processes on the runner.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=2)


def run_bounded(command, *, environ, timeout=120, cwd=None, input_data=b"",
                max_input_bytes=65536, max_output_bytes=1048576):
    """Run once; drain stdout/stderr together, enforcing an aggregate byte cap.

    A timeout or overflow can happen after the provider accepted work. Callers
    must preserve the original operation store and recover that request, rather
    than rerunning a start/search/execute. Killing this group does not attest
    provider cleanup or stop a separately daemonized process/session.
    """
    if (not isinstance(command, (list, tuple)) or not command or
            not all(isinstance(part, str) and part and "\0" not in part for part in command) or
            type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 600 or
            type(max_input_bytes) is not int or not 0 <= max_input_bytes <= 1048576 or
            type(max_output_bytes) is not int or not 0 < max_output_bytes <= 4194304 or
            not isinstance(input_data, bytes) or len(input_data) > max_input_bytes):
        raise ValueError("invalid bounded process envelope")
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    remaining_input = memoryview(input_data)
    selector = None
    started = time.monotonic()
    process = subprocess.Popen(command, env=environ, cwd=cwd, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        selector = selectors.DefaultSelector()
        for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        if remaining_input:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            process.stdin.close()
        while selector.get_map() or process.poll() is None:
            remaining_time = timeout - (time.monotonic() - started)
            if remaining_time <= 0:
                raise ProcessLimitError("client process deadline exceeded", **{
                    key: bytes(value) for key, value in captured.items()})
            for key, _ in selector.select(min(remaining_time, 0.1)):
                if key.data == "stdin":
                    try:
                        written = os.write(key.fd, remaining_input[:65536])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        raise ProcessLimitError("client closed input before consuming the fixture", **{
                            name: bytes(value) for name, value in captured.items()}) from None
                    remaining_input = remaining_input[written:]
                    if not remaining_input:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                available = max_output_bytes - sum(map(len, captured.values()))
                captured[key.data].extend(chunk[:available])
                if len(chunk) > available:
                    raise ProcessLimitError("client output byte bound exceeded", **{
                        name: bytes(value) for name, value in captured.items()})
        return ProcessResult(process.wait(timeout=2), bytes(captured["stdout"]), bytes(captured["stderr"]))
    finally:
        # Every post-launch failure owns the same teardown, including selector
        # construction (for example EMFILE). One cleanup failure must not skip
        # stopping the child or closing the remaining parent-side pipe handles.
        with ExitStack() as cleanup:
            for stream in (process.stdin, process.stdout, process.stderr):
                cleanup.callback(stream.close)
            cleanup.callback(_stop_group, process)
            if selector is not None:
                cleanup.callback(selector.close)
