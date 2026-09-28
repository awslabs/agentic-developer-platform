"""Supervise one guarded investigation per killable process group.

Only fixed broker code runs in children. Agent input is bounded JSON, never code.
The supervisor owns cancellation and can stop AgentCore even if Playwright hangs.
"""

from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, TimeoutError
from pathlib import Path

from case_contract import MAX_RESPONSE_BYTES, content_digest

from runtime_limits import STARTUP_SECONDS, ACTION_SECONDS


def stop_session(session_id, *, native=False):
    import boto3
    from botocore.config import Config

    config = Config(connect_timeout=2, read_timeout=3, retries={"max_attempts": 0})
    if native:
        from native_identity import cleanup_client

        client = cleanup_client(config)
    else:
        client = boto3.client(
            "bedrock-agentcore",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
            config=config,
        )
    client.stop_browser_session(
        browserIdentifier="aws.browser.v1", sessionId=session_id
    )
    return (
        client.get_browser_session(
            browserIdentifier="aws.browser.v1", sessionId=session_id
        )["status"]
        == "TERMINATED"
    )


class ProcessActor:
    def __init__(
        self,
        payload,
        factory,
        on_exit,
        *,
        command=None,
        stop=stop_session,
        startup_seconds=STARTUP_SECONDS,
        action_seconds=ACTION_SECONDS,
    ):
        from investigation_browser import LEASE_SECONDS

        self.payload, self.on_exit, self.stop = payload, on_exit, stop
        self.ready = Future()
        self.ended = threading.Event()
        self.deadline = time.monotonic() + LEASE_SECONDS
        self.startup_seconds, self.action_seconds = startup_seconds, action_seconds
        self.session_id = None
        self.close_result = None
        self.checkpoints = {}
        self.pending = {"start": self.ready}
        self.sequence = 0
        self.lock = threading.RLock()
        self.cleanup_lock = threading.Lock()
        self.call_lock = threading.Lock()
        self.failure = None
        child_env = os.environ.copy()
        if command and "--native" in command:
            from native_identity import browser_environment

            child_env = browser_environment(child_env)
            if stop is stop_session:
                self.stop = lambda sid: stop_session(sid, native=True)
        self.process = subprocess.Popen(
            command or [sys.executable, str(Path(__file__).resolve()), "--worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=child_env,
            text=True,
            start_new_session=True,
            bufsize=1,
        )
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        self._send({"id": "start", "payload": payload})
        threading.Thread(target=self._watch, daemon=True).start()

    def _send(self, value):
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()

    def _error(self, message, code="worker_failed"):
        from investigation_browser import InvestigationError

        return InvestigationError(
            message,
            code=code,
            cleanup=self.close_result,
            browser_start_unattempted=bool(
                self.failure and self.failure.get("browser_start_unattempted") is True
            ),
        )

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(MAX_RESPONSE_BYTES + 1)
                if not line:
                    break
                if len(line.encode()) > MAX_RESPONSE_BYTES or not line.endswith("\n"):
                    raise ValueError("Worker response exceeded protocol budget")
                event = json.loads(line)
                with self.lock:
                    kind = event.get("event")
                    if kind == "session":
                        self.session_id = event["session_id"]
                    elif kind == "checkpoint":
                        self.checkpoints[event["id"]] = event["packet"]
                    elif kind == "closed":
                        self.close_result = event["result"]
                    elif kind == "error":
                        self.failure = event
                    elif kind == "result":
                        future = self.pending.pop(event["id"], None)
                        if future and not future.done():
                            future.set_result(event["result"])
            self.abort()
        except Exception:
            self.abort()

    def _watch(self):
        while not self.ended.wait(0.2):
            if time.monotonic() >= self.deadline:
                self.abort()
                return

    def abort(self):
        """Kill the driver family first; AWS cleanup cannot depend on that driver."""
        with self.cleanup_lock:
            if self.ended.is_set():
                return
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Darwin can report EPERM for a group whose leader just exited.
                if self.process.poll() is None:
                    self.process.terminate()
            try:
                self.process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            # The parent may have exited while a descendant driver stayed alive.
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                if self.process.poll() is None:
                    self.process.kill()
            self.process.wait(timeout=2)
            if (
                not self.close_result
                or self.close_result.get("cleanup_status") != "stopped"
            ):
                stopped = False
                if self.session_id:
                    try:
                        stopped = self.stop(self.session_id)
                    except Exception:
                        pass
                self.close_result = {
                    "session_id": self.session_id,
                    "session_open": False,
                    "cleanup_status": "stopped" if stopped else "unknown",
                }
            self.ended.set()
            with self.lock:
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(self._failure_error())
                self.pending.clear()
            self.on_exit(self)

    def _failure_error(self):
        if self.failure and self.failure.get("reason_code"):
            from browser_guard import DestinationRefused
            from denylist import DenylistResult

            error = DestinationRefused(
                self.payload.get("url", ""),
                DenylistResult(
                    allowed=False,
                    reason=self.failure["message"],
                    reason_code=self.failure["reason_code"],
                ),
            )
            error.cleanup = self.close_result
            error.browser_start_unattempted = (
                self.failure.get("browser_start_unattempted") is True
            )
            return error
        return self._error(
            "Isolated browser worker ended before completing the operation"
        )

    def partial_packet(self, operation, reason="worker_deadline_exceeded"):
        packet = copy.deepcopy(self.checkpoints.get(operation))
        if not packet:
            return None
        packet["session_open"] = False
        packet["choices"] = []
        packet["manifest"]["cleanup_status"] = self.close_result["cleanup_status"]
        for observation in packet["observations"]:
            observation["status"] = "partial"
            observation["errors"].append(reason)
            observation["content_sha256"] = content_digest(observation)
        return packet

    def initial(self):
        from investigation_browser import InvestigationError

        try:
            return self.ready.result(timeout=self.startup_seconds)
        except TimeoutError:
            self.abort()
            partial = self.partial_packet("start")
            if partial:
                return partial
            raise self._error(
                "Browser startup deadline exceeded; worker stopped", "startup_timeout"
            )
        except InvestigationError as error:
            if error.code == "worker_failed":
                partial = self.partial_packet("start", "worker_failed")
                if partial:
                    return partial
            raise

    def call(self, payload):
        from investigation_browser import InvestigationError

        if self.ended.is_set():
            if payload.get("action") == "close":
                return self.close_result
            raise self._error(
                "Browser session ended; retain collected evidence", "session_ended"
            )
        if not self.call_lock.acquire(blocking=False):
            raise self._error("A browser action is already pending", "action_pending")
        try:
            with self.lock:
                self.sequence += 1
                ident = str(self.sequence)
                future = Future()
                self.pending[ident] = future
                self._send({"id": ident, "payload": payload})
            try:
                return future.result(timeout=self.action_seconds)
            except TimeoutError:
                self.abort()
                partial = self.partial_packet(ident)
                if partial:
                    return partial
                raise self._error(
                    "Browser action deadline exceeded; worker stopped", "action_timeout"
                )
            except InvestigationError as error:
                if error.code == "worker_failed":
                    partial = self.partial_packet(ident, "worker_failed")
                    if partial:
                        return partial
                raise
        finally:
            self.call_lock.release()


def worker():
    import select
    from functools import partial
    from playwright.sync_api import sync_playwright
    from bedrock_agentcore.tools.browser_client import BrowserClient
    from browser_guard import open_guarded_browser
    from native_browser import open_native_browser
    from case_capture import recorded_browser
    from investigation_browser import BrowserInvestigation

    protocol = sys.stdout
    sys.stdout = sys.stderr  # Libraries cannot corrupt the JSON transport.
    current = "start"
    browser = None

    def emit(value):
        protocol.write(json.dumps(value) + "\n")
        protocol.flush()

    class TrackedClient(BrowserClient):
        def start(self, *args, **kwargs):
            sid = super().start(*args, **kwargs)
            emit({"event": "session", "session_id": sid})
            return sid

    def checkpoint(packet):
        emit({"event": "checkpoint", "id": current, "packet": packet})

    try:
        initial = json.loads(sys.stdin.readline(32769))
        with sync_playwright() as playwright:
            opener = partial(
                open_native_browser if "--native" in sys.argv else open_guarded_browser,
                client_factory=TrackedClient,
            )
            if "_capture" in initial["payload"] and "--native" in sys.argv:
                recorder = recorded_browser(
                    initial["payload"]["_capture"], playwright, opener=opener
                )
                try:
                    result, _ = next(recorder)
                finally:
                    recorder.close()
                emit(
                    {
                        "event": "closed",
                        "result": {
                            "session_id": result["session_id"],
                            "cleanup_status": result["cleanup_status"],
                            "session_open": False,
                        },
                    }
                )
                emit({"event": "result", "id": "start", "result": result})
                return
            browser = BrowserInvestigation(
                initial["payload"],
                playwright,
                recorder_factory=partial(recorded_browser, opener=opener),
                on_observation=checkpoint,
            )
            emit({"event": "result", "id": "start", "result": browser.last})
            while not browser.closed:
                if not select.select([sys.stdin], [], [], 0.05)[0]:
                    browser.pump()
                    continue
                line = sys.stdin.readline(32769)
                if not line:
                    break
                message = json.loads(line)
                current = message["id"]
                if message["payload"]["action"] == "close":
                    result = browser.close()
                    emit({"event": "closed", "result": result})
                    emit({"event": "result", "id": current, "result": result})
                    break
                emit(
                    {
                        "event": "result",
                        "id": current,
                        "result": browser.step(message["payload"]),
                    }
                )
            emit({"event": "closed", "result": browser.close()})
    except Exception as error:
        emit(
            {
                "event": "error",
                "id": current,
                "message": str(error)[:500],
                "reason_code": getattr(error, "reason_code", None),
                "browser_start_unattempted": getattr(
                    error, "browser_start_unattempted", False
                ),
            }
        )
    finally:
        if browser is not None:
            try:
                emit({"event": "closed", "result": browser.close()})
            except Exception:
                pass


if __name__ == "__main__":
    worker()
