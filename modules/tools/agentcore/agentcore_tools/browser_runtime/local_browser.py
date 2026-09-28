"""Keep direct Playwright sessions alive across CLI calls in this worker pod.

A private Unix socket connects CLI invocations to their own session process.
There is no HTTP service, separate pod, alternate identity or browser proxy.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from agentcore_tools.browser_runtime.case_contract import MAX_RESPONSE_BYTES
from agentcore_tools.browser_runtime.runtime_limits import ACTION_SECONDS, LEASE_SECONDS, STARTUP_SECONDS

MAX_REQUEST_BYTES = 32768


def _root():
    root = Path(tempfile.gettempdir()) / f"adp-native-browser-{os.getuid()}"
    root.mkdir(mode=0o700, exist_ok=True)
    if (
        root.is_symlink()
        or root.stat().st_uid != os.getuid()
        or root.stat().st_mode & 0o077
    ):
        raise RuntimeError("Browser session directory must be private to this worker")
    return root


def _read(stream, limit):
    line = stream.readline(limit + 1)
    if not line.endswith(b"\n") or len(line) > limit:
        raise ValueError("Browser IPC message missing or exceeds byte budget")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("Browser IPC requires an object")
    return value


def _write(stream, value, limit):
    data = json.dumps(value).encode() + b"\n"
    if len(data) > limit:
        raise ValueError("Browser IPC message exceeds byte budget")
    stream.write(data)
    stream.flush()


def investigation_request(operation, payload):
    from agentcore_tools.browser_runtime.errors import BrowserBrokerError

    if operation == "start":
        from agentcore_tools.browser_runtime.investigation_browser import validate_start

        payload = validate_start(payload)
        directory = Path(tempfile.mkdtemp(prefix="native-", dir=_root()))
        token = directory.name
        process = subprocess.Popen(
            [sys.executable, "-m", "agentcore_tools.browser_runtime.local_browser", "--serve", str(directory)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    else:
        token = payload.get("session_token", "")
        if not re.fullmatch(r"native-[a-z0-9_]{8}", token):
            raise ValueError("Invalid native browser session token")
        directory = _root() / token
        process = None
        if operation == "close" and (directory / "cleanup.json").exists():
            return json.loads((directory / "cleanup.json").read_text())
    deadline = (
        time.monotonic()
        + (STARTUP_SECONDS if operation == "start" else ACTION_SECONDS)
        + 20
    )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        while True:
            try:
                connection.connect(str(directory / "session.sock"))
                break
            except (FileNotFoundError, ConnectionRefusedError) as error:
                cleanup_file = directory / "cleanup.json"
                cleanup = (
                    json.loads(cleanup_file.read_text())
                    if cleanup_file.exists()
                    else None
                )
                if operation == "close" and cleanup:
                    return cleanup
                if (
                    process is None
                    or process.poll() is not None
                    or time.monotonic() >= deadline
                ):
                    raise BrowserBrokerError(
                        "Direct browser session unavailable; retain evidence and do not replay",
                        code="session_ended",
                        cleanup=cleanup,
                        browser_start_unattempted=operation == "start",
                    ) from error
                time.sleep(0.05)  # Only connect retries, never resend browser actions.
        connection.settimeout(max(1, deadline - time.monotonic()))
        try:
            with connection.makefile("rwb") as stream:
                _write(
                    stream,
                    {"operation": operation, "payload": payload},
                    MAX_REQUEST_BYTES,
                )
                response = _read(stream, MAX_RESPONSE_BYTES)
        except (OSError, ValueError) as error:
            raise BrowserBrokerError(
                "Direct browser response unavailable; action may have run; do not replay",
                code="action_unknown",
            ) from error
    if "error" in response:
        raise BrowserBrokerError(**response["error"])
    return {
        **response["result"],
        **({"session_token": token} if operation == "start" else {}),
    }


def serve(directory):
    from agentcore_tools.browser_runtime.isolated_browser import ProcessActor

    actor = None
    deadline = time.monotonic() + LEASE_SECONDS + 10
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(directory / "session.sock"))
    listener.listen(1)
    listener.settimeout(0.5)

    def stop(*_):
        raise SystemExit()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while time.monotonic() < deadline:
            if actor is not None and actor.ended.is_set():
                break
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            with connection:
                connection.settimeout(5)
                with connection.makefile("rwb") as stream:
                    message = _read(stream, MAX_REQUEST_BYTES)
                    operation, payload = message["operation"], message["payload"]
                    try:
                        if operation == "start" and actor is None:
                            actor = ProcessActor(
                                payload,
                                None,
                                lambda _: None,
                                command=[
                                    sys.executable,
                                    "-m",
                                    "agentcore_tools.browser_runtime.isolated_browser",
                                    "--worker",
                                    "--native",
                                ],
                            )
                            result = actor.initial()
                            result["lease_seconds"] = LEASE_SECONDS
                        elif operation in {"step", "close"} and actor is not None:
                            result = actor.call(
                                {
                                    **payload,
                                    **(
                                        {"action": "close"}
                                        if operation == "close"
                                        else {}
                                    ),
                                }
                            )
                        else:
                            raise ValueError("Invalid browser session operation")
                        if operation == "close":
                            temporary = directory / "cleanup.tmp"
                            temporary.write_text(json.dumps(result))
                            temporary.replace(directory / "cleanup.json")
                        _write(stream, {"result": result}, MAX_RESPONSE_BYTES)
                    except Exception as error:
                        _write(
                            stream,
                            {
                                "error": {
                                    "message": str(error)[:500],
                                    "code": getattr(error, "code", "browser_failed"),
                                    "cleanup": getattr(error, "cleanup", None),
                                    "browser_start_unattempted": getattr(
                                        error, "browser_start_unattempted", False
                                    ),
                                }
                            },
                            MAX_RESPONSE_BYTES,
                        )
                        break
                    if operation == "close":
                        break
    finally:
        if actor is not None:
            actor.abort()
            (directory / "cleanup.json").write_text(json.dumps(actor.close_result))
        listener.close()
        (directory / "session.sock").unlink(missing_ok=True)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--serve":
        raise SystemExit("Use the browser client")
    serve(Path(sys.argv[2]))
