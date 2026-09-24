"""Independent Task API process host for the shared worker image."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import rfc8785
from datetime import UTC, datetime
from pathlib import Path

from lib.run_identity import RunIdentityError
from lib.task_commands import UnknownTaskPersonaError, task_agent_command
from lib.task_gateway_client import TaskGatewayError
from lib.task_protocol import (
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    TaskProtocolError,
    validate_bootstrap,
    validate_child_frame,
)
from lib.task_run_client import TaskRunClient, TaskRunClientError, workload_identity

logger = logging.getLogger(__name__)
TASK_EXIT_RETRYABLE = 75
TASK_EXIT_FAILED = 1
_MIN_PROGRESS_MARKERS = 2
_CONTROL_POLL_SECONDS = 1.0
_TERM_AFTER_SECONDS = 20.0
_KILL_AFTER_SECONDS = 30.0


class TaskHostError(Exception):
    """The task could not safely continue."""

    def __init__(self, message: str, *, code: str = "process_failed") -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_message(value: object) -> str:
    text = str(value)
    return "".join(character if 32 <= ord(character) <= 126 else " " for character in text)[:1000]


def _request_id() -> str:
    return str(uuid.uuid4())


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()


def _stable_uuid4(namespace: str, value: str) -> str:
    raw = bytearray(hashlib.sha256(f"{namespace}:{value}".encode()).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def _write_frame(process: subprocess.Popen, frame: dict) -> None:
    assert process.stdin is not None
    encoded = json.dumps(frame, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    if len(encoded.encode("utf-8")) > MAX_FRAME_BYTES:
        raise TaskProtocolError("host frame exceeds the process byte limit")
    process.stdin.write(encoded)
    process.stdin.flush()


def _child_environment(workspace: Path) -> dict[str, str]:
    home = workspace / "home"
    temporary = workspace / "tmp"
    home.mkdir(mode=0o700)
    temporary.mkdir(mode=0o700)
    return {
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "ADP_TASK_PROTOCOL_VERSION": str(PROTOCOL_VERSION),
        "ADP_TASK_NETWORK": "disabled",
    }


def _network_wrapped_command(command: list[str]) -> list[str]:
    wrapper = Path(__file__).with_name("task_network_exec.py")
    return [sys.executable, str(wrapper), *command]


def _capture_stderr(stream, output: list[str]) -> None:
    total = 0
    for line in iter(stream.readline, ""):
        encoded = line.encode("utf-8", "replace")
        remaining = max(0, 8192 - total)
        if remaining:
            output.append(encoded[:remaining].decode("utf-8", "replace"))
            total += min(len(encoded), remaining)
    stream.close()


def _stop_process(process: subprocess.Popen, *, graceful_seconds: float = 0) -> int | None:
    if process.poll() is not None:
        return process.returncode
    deadline = time.monotonic() + graceful_seconds
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        terminate_deadline = time.monotonic() + max(0, _KILL_AFTER_SECONDS - _TERM_AFTER_SECONDS)
        while process.poll() is None and time.monotonic() < terminate_deadline:
            time.sleep(0.05)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        return process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        return None


class TaskHost:
    def __init__(
        self,
        *,
        client: TaskRunClient | None = None,
        work_root: Path | None = None,
        command_resolver=task_agent_command,
    ) -> None:
        self.client = client or TaskRunClient()
        self.work_root = work_root or Path(os.environ.get("ADP_TASK_WORK_ROOT", "/work"))
        self.command_resolver = command_resolver
        self._turn_number = 0
        self._turns: dict[str, dict] = {}
        self._pending_turn_id: str | None = None

    def _binding(self, assignment, runtime_attempt_id: str) -> dict:
        return {
            "run": {
                "task_id": assignment.task_id,
                "invocation_id": assignment.invocation_id,
                "generation": assignment.generation,
            },
            "runtime_attempt_id": runtime_attempt_id,
        }

    def _report(self, assignment, attempt: dict, frame: dict) -> None:
        event_type = "progress.updated" if frame["type"] == "progress" else "input.required"
        if event_type == "progress.updated":
            data = {"message": frame["message"], "stage": frame.get("stage", "analysis")}
            report_id = frame["report_id"]
        else:
            data = {
                "input_request_id": frame["input_request_id"],
                "prompt": frame["prompt"],
            }
            report_id = frame["request_id"]
        response = self.client.report(
            {
                "schema_version": SCHEMA_VERSION,
                "attempt": attempt,
                "report_id": report_id,
                "event_type": event_type,
                "producer_timestamp": frame.get("producer_timestamp"),
                "data": data,
            }
        )
        if (
            set(response) != {"schema_version", "report_id", "sequence", "event_id"}
            or response["schema_version"] != SCHEMA_VERSION
            or response["report_id"] != report_id
            or type(response["sequence"]) is not int
            or response["sequence"] < 1
        ):
            raise TaskRunClientError("task report receipt is invalid")
        return response

    def _turn(self, assignment, attempt: dict, request_id: str) -> dict | None:
        if request_id in self._turns:
            return self._turns[request_id]
        response = self.client.turn(
            {
                "schema_version": SCHEMA_VERSION,
                "attempt": attempt,
                "request_id": request_id,
                "expected_transcript_version": self._turn_number + 1,
            }
        )
        if (
            set(response)
            != {"schema_version", "operation_status", "turn", "pending_input_count", "messages"}
            or response["schema_version"] != SCHEMA_VERSION
            or type(response["pending_input_count"]) is not int
            or not 0 <= response["pending_input_count"] <= 10
            or not isinstance(response["messages"], list)
        ):
            raise TaskProtocolError("task turn response is invalid")
        turn = response["turn"]
        if response["operation_status"] == "waiting":
            if turn is not None or response["messages"]:
                raise TaskProtocolError("waiting turn exposes uncommitted input")
            return None
        if response["operation_status"] not in {"committed", "existing"} or not isinstance(
            turn, dict
        ):
            raise TaskProtocolError("task turn is not committed")
        if (
            turn.get("task_id") != assignment.task_id
            or turn.get("turn_id") != request_id
            or type(turn.get("turn_number")) is not int
            or turn["turn_number"] != self._turn_number + 1
            or not 1 <= turn["turn_number"] <= 8
            or turn.get("transcript_version") != turn["turn_number"] + 1
            or not isinstance(turn.get("command_ids"), list)
        ):
            raise TaskProtocolError("task turn identity or transcript is invalid")
        messages = response["messages"]
        if len(messages) > 10:
            raise TaskProtocolError("task turn contains too many commands")
        for message in messages:
            if (
                not isinstance(message, dict)
                or not {"command_id", "text"}.issubset(message)
                or set(message) - {"command_id", "text", "reply_to"}
                or not isinstance(message["text"], str)
                or not 1 <= len(message["text"]) <= 4000
            ):
                raise TaskProtocolError("task turn command text is invalid")
        for message in messages:
            for key in ("command_id", "reply_to"):
                if key not in message:
                    continue
                try:
                    identifier = uuid.UUID(message[key])
                    if identifier.version != 4 or str(identifier) != message[key]:
                        raise ValueError("invalid UUID")
                except (ValueError, TypeError, AttributeError):
                    raise TaskProtocolError("task turn command identifier is invalid") from None
        ids = [message["command_id"] for message in messages]
        if ids != turn["command_ids"] or len(set(ids)) != len(ids):
            raise TaskProtocolError("task turn command membership is invalid")
        self._turn_number = turn["turn_number"]
        self._turns[request_id] = response
        return response

    def _deliver_input(self, assignment, attempt: dict, process, control: dict) -> None:
        if (
            not control["pending_input_count"]
            or control["cancel_requested"]
            or self._turn_number == 0
            or self._pending_turn_id is not None
        ):
            return
        turn_id = _request_id()
        response = self._turn(assignment, attempt, turn_id)
        if response is None:
            return
        if not response["messages"]:
            raise TaskProtocolError("follow-up turn contains no committed input")
        _write_frame(
            process,
            {
                "protocol_version": PROTOCOL_VERSION,
                "type": "turn",
                "request_id": _request_id(),
                "task_id": assignment.task_id,
                "turn_id": turn_id,
                "turn_number": response["turn"]["turn_number"],
                "messages": response["messages"],
            },
        )
        self._pending_turn_id = turn_id

    def _model(self, assignment, attempt: dict, frame: dict, max_tokens: int) -> dict:
        if self._pending_turn_id is not None and frame["turn_id"] != self._pending_turn_id:
            raise TaskProtocolError("child model request skipped its assigned input turn")
        turn = self._turn(assignment, attempt, frame["turn_id"])
        if turn is None:
            raise TaskProtocolError("child requested a model without a committed turn")
        self._pending_turn_id = None
        request = {
            "messages": frame["messages"],
            "max_tokens": frame.get("max_tokens", max_tokens),
        }
        if "system" in frame:
            request["system"] = frame["system"]
        request_digest = _canonical_digest(request)
        response = self.client.model(
            {
                "schema_version": SCHEMA_VERSION,
                "attempt": attempt,
                "turn_id": frame["turn_id"],
                "request_digest": request_digest,
                **request,
            }
        )
        if (
            response.get("task_id") != assignment.task_id
            or response.get("turn_id") != frame["turn_id"]
        ):
            raise TaskRunClientError("task model receipt identity mismatch")
        if (
            response.get("schema_version") != SCHEMA_VERSION
            or response.get("request_digest") != request_digest
            or response.get("automatic_replay_permitted") is not False
        ):
            raise TaskHostError(
                "task model receipt request binding is invalid", code="protocol_violation"
            )
        status = response.get("operation_status")
        if status == "confirmed" and response.get("handoff") != "confirmed":
            raise TaskHostError(
                "task model receipt handoff is not confirmed", code="protocol_violation"
            )
        if status == "confirmed" and (
            not isinstance(response.get("content"), list)
            or not isinstance(response.get("stop_reason"), str)
            or not response["stop_reason"]
        ):
            raise TaskHostError(
                "confirmed model receipt has no stored result", code="protocol_violation"
            )
        if status != "confirmed" and (
            response.get("content") is not None or response.get("stop_reason") is not None
        ):
            raise TaskHostError(
                "unconfirmed model receipt exposes result content", code="protocol_violation"
            )
        if status not in {"confirmed", "pending", "unknown", "rejected"}:
            raise TaskRunClientError("task model receipt status is invalid")
        return {
            "protocol_version": PROTOCOL_VERSION,
            "type": "model.result",
            "request_id": _request_id(),
            "task_id": assignment.task_id,
            "turn_id": frame["turn_id"],
            "operation_status": status,
            "content": response.get("content"),
            "stop_reason": response.get("stop_reason"),
            "error_code": (
                "model_outcome_unknown" if status == "unknown" else response.get("error_code")
            ),
        }

    def _control(self, assignment, attempt: dict, cursor: str | None) -> dict:
        response = self.client.control(
            {
                "schema_version": SCHEMA_VERSION,
                "attempt": attempt,
                "last_receipt_cursor": cursor,
            }
        )
        required = {
            "schema_version",
            "task_id",
            "cancel_requested",
            "cancel_command_id",
            "pending_input_count",
            "last_receipt_cursor",
            "attempt_valid",
        }
        if set(response) != required or response.get("task_id") != assignment.task_id:
            raise TaskRunClientError("task control response is invalid")
        if response["attempt_valid"] is not True:
            raise TaskHostError(
                "task runtime attempt is no longer current", code="authority_revoked"
            )
        if type(response["pending_input_count"]) is not int or response["pending_input_count"] < 0:
            raise TaskRunClientError("task control response is invalid")
        return response

    def _finalize(
        self,
        assignment,
        attempt: dict,
        *,
        exit_code: int | None,
        outcome: str,
        report: dict | None,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        stopped_at = _now()
        result = None
        error = None
        if outcome == "completed":
            result = {
                "schema_version": SCHEMA_VERSION,
                "outcome": "completed",
                "report": report,
                "artifact_ids": [],
                "committed_at": stopped_at,
                "process_exit_validated": True,
            }
        else:
            error = {
                "schema_version": SCHEMA_VERSION,
                "outcome": outcome,
                "code": error_code,
                "message": (error_message or "Task execution failed")[:1024],
                "committed_at": stopped_at,
                "child_exit_confirmed": True,
                "recovery_required": False,
                "total_usd": None,
            }
        response = self.client.finalize(
            {
                "schema_version": SCHEMA_VERSION,
                "attempt": attempt,
                "final_report_id": _stable_uuid4("task-final", assignment.invocation_id),
                "child_exit": {
                    "confirmed": True,
                    "exit_code": exit_code if exit_code is not None and exit_code >= 0 else None,
                    "signal": str(-exit_code) if exit_code is not None and exit_code < 0 else None,
                    "stopped_at": stopped_at,
                },
                "outcome": outcome,
                "result": result,
                "error": error,
                "committed_result_refs": [],
            }
        )
        if (
            response.get("schema_version") != SCHEMA_VERSION
            or response.get("task_id") != assignment.task_id
            or response.get("status") != outcome
            or not response.get("terminal_event_id")
        ):
            raise TaskRunClientError("task finalization receipt is invalid")

    def _settle_unknown(self, assignment, *, child_exit_confirmed: bool) -> None:
        try:
            self.client.settlement(
                {
                    "schema_version": SCHEMA_VERSION,
                    "workload": workload_identity(),
                    "assignment": {
                        "grant_pk": assignment.grant_pk,
                        "grant_sk": assignment.grant_sk,
                        "generation": assignment.generation,
                    },
                    "stop_evidence": {
                        "child_exit_confirmed": child_exit_confirmed,
                        "workload_terminated": False,
                        "observed_at": _now(),
                    },
                    "queue_ack_status": "unknown",
                }
            )
        except (TaskRunClientError, RunIdentityError, OSError, ValueError):
            logger.warning("Task stop-only settlement unavailable")

    def _input_artifacts(self, assignment, bootstrap: dict) -> list[tuple[dict, bytes]]:
        artifacts = []
        for reference in bootstrap["input"].get("artifacts", []):
            run = {
                "task_id": assignment.task_id,
                "invocation_id": assignment.invocation_id,
                "generation": assignment.generation,
            }
            response = self.client.artifact(
                {
                    "schema_version": SCHEMA_VERSION,
                    "operation": "read",
                    "run": run,
                    "artifact_id": reference["artifact_id"],
                }
            )
            expected = {
                "schema_version",
                "operation",
                "run",
                "artifact_id",
                "content_type",
                "content_sha256",
                "byte_length",
                "content_base64",
            }
            if not isinstance(response, dict) or set(response) != expected:
                raise TaskProtocolError("artifact read response does not match contract")
            if (
                response["schema_version"] != SCHEMA_VERSION
                or response["operation"] != "read"
                or response["run"] != run
                or any(
                    response[key] != reference[key]
                    for key in ("artifact_id", "content_type", "content_sha256")
                )
                or type(response["byte_length"]) is not int
                or not 0 < response["byte_length"] <= 262144
                or not isinstance(response["content_base64"], str)
                or len(response["content_base64"]) > 349528
            ):
                raise TaskProtocolError("artifact read identity or length mismatch")
            try:
                content = base64.b64decode(response["content_base64"], validate=True)
                content.decode("utf-8")
            except (ValueError, binascii.Error, UnicodeDecodeError):
                raise TaskProtocolError("artifact content encoding invalid") from None
            if (
                len(content) != response["byte_length"]
                or hashlib.sha256(content).hexdigest() != response["content_sha256"]
            ):
                raise TaskProtocolError("artifact read integrity mismatch")
            artifacts.append(
                (
                    {
                        key: response[key]
                        for key in ("artifact_id", "content_type", "content_sha256", "byte_length")
                    },
                    content,
                )
            )
        return artifacts

    def _send_artifacts(self, process, assignment, artifacts) -> None:
        for reference, content in artifacts:
            for offset in range(0, len(content), 32768):
                chunk = content[offset : offset + 32768]
                _write_frame(
                    process,
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        "type": "artifact.chunk",
                        "request_id": _request_id(),
                        "task_id": assignment.task_id,
                        "artifact_id": reference["artifact_id"],
                        "content_type": reference["content_type"],
                        "content_sha256": reference["content_sha256"],
                        "sequence": offset // 32768 + 1,
                        "total_bytes": len(content),
                        "data_base64": base64.b64encode(chunk).decode(),
                        "last": offset + len(chunk) == len(content),
                    },
                )

    def run(self, assignment, envelope: dict, *, heartbeat, acknowledge) -> int:
        workspace: Path | None = None
        process: subprocess.Popen | None = None
        attempt: dict | None = None
        finalized = False
        heartbeat_stopped = False

        def stop_heartbeat() -> None:
            nonlocal heartbeat_stopped
            if not heartbeat_stopped:
                heartbeat.stop()
                heartbeat_stopped = True

        try:
            bootstrap = validate_bootstrap(
                self.client.bootstrap(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "task_id": assignment.task_id,
                        "invocation_id": assignment.invocation_id,
                        "envelope_digest": _canonical_digest(envelope),
                    }
                ),
                assignment,
            )
            runtime_attempt_id = _request_id()
            attempt = self._binding(assignment, runtime_attempt_id)
            attempt_response = self.client.attempt(
                {
                    "schema_version": SCHEMA_VERSION,
                    "task_id": assignment.task_id,
                    "invocation_id": assignment.invocation_id,
                    "generation": assignment.generation,
                    "runtime_attempt_id": runtime_attempt_id,
                    "protocol_version": PROTOCOL_VERSION,
                    "capabilities": bootstrap.get("capabilities") or ["cancel"],
                    "old_attempt_invalidated": True,
                }
            )
            if attempt_response.get("operation_status") not in {"confirmed", "pending"}:
                raise TaskRunClientError("task attempt was not registered")
            artifacts = self._input_artifacts(assignment, bootstrap)
            command = self.command_resolver(bootstrap["persona"])
            self.work_root.mkdir(parents=True, exist_ok=True)
            workspace = Path(
                tempfile.mkdtemp(prefix=f"task-{assignment.invocation_id[:8]}-", dir=self.work_root)
            )
            workspace.chmod(0o700)
            stderr: list[str] = []
            process = subprocess.Popen(
                _network_wrapped_command(command),
                cwd=workspace,
                env=_child_environment(workspace),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            assert process.stdout is not None and process.stderr is not None
            stderr_thread = threading.Thread(
                target=_capture_stderr, args=(process.stderr, stderr), daemon=True
            )
            stderr_thread.start()
            task_input = bootstrap["input"]
            self._send_artifacts(process, assignment, artifacts)
            _write_frame(
                process,
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "type": "start",
                    "request_id": _request_id(),
                    "task_id": assignment.task_id,
                    "invocation_id": assignment.invocation_id,
                    "generation": assignment.generation,
                    "runtime_attempt_id": runtime_attempt_id,
                    "instructions": task_input["instructions"],
                    "inputs": task_input.get("inputs", {}),
                    "acceptance_criteria": task_input.get("acceptance_criteria", []),
                    "artifacts": [reference for reference, _ in artifacts],
                    "limits": {
                        "max_turns": bootstrap["limits"]["max_turns"],
                        "max_output_tokens_per_turn": bootstrap["limits"][
                            "max_output_tokens_per_turn"
                        ],
                    },
                },
            )
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            stdout_buffer = bytearray()
            ready = False
            result_report = None
            progress_messages: set[str] = set()
            cursor = None
            next_control = time.monotonic()
            cancel_started: float | None = None
            cancel_command_id: str | None = None
            try:
                deadline = datetime.fromisoformat(bootstrap["limits"]["deadline_at"])
            except (AttributeError, TypeError, ValueError):
                raise TaskProtocolError("task deadline is invalid") from None
            while process.poll() is None or selector.get_map():
                now = time.monotonic()
                if process.poll() is None and datetime.now(UTC) >= deadline:
                    raise TaskHostError("Task deadline exceeded", code="deadline_exceeded")
                if process.poll() is None and now >= next_control:
                    control = self._control(assignment, attempt, cursor)
                    cursor = control["last_receipt_cursor"]
                    next_control = now + _CONTROL_POLL_SECONDS
                    self._deliver_input(assignment, attempt, process, control)
                    if control["cancel_requested"] and cancel_started is None:
                        cancel_command_id = control["cancel_command_id"]
                        if not isinstance(cancel_command_id, str):
                            raise TaskProtocolError("task cancellation command is invalid")
                        _write_frame(
                            process,
                            {
                                "protocol_version": PROTOCOL_VERSION,
                                "type": "cancel",
                                "request_id": _request_id(),
                                "task_id": assignment.task_id,
                                "command_id": cancel_command_id,
                                "intentional": True,
                            },
                        )
                        cancel_started = now
                    if cancel_started is not None and now - cancel_started >= _TERM_AFTER_SECONDS:
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                    if cancel_started is not None and now - cancel_started >= _KILL_AFTER_SECONDS:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                events = selector.select(timeout=0.1)
                for key, _ in events:
                    chunk = os.read(key.fileobj.fileno(), MAX_FRAME_BYTES + 1)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        if stdout_buffer:
                            raise TaskProtocolError("task child frame is not newline terminated")
                        continue
                    stdout_buffer.extend(chunk)
                    if b"\n" not in stdout_buffer and len(stdout_buffer) > MAX_FRAME_BYTES:
                        raise TaskProtocolError("task child frame exceeds 65536 bytes")
                    while b"\n" in stdout_buffer:
                        line, _, remainder = stdout_buffer.partition(b"\n")
                        stdout_buffer = bytearray(remainder)
                        if len(line) + 1 > MAX_FRAME_BYTES:
                            raise TaskProtocolError("task child frame exceeds 65536 bytes")
                        try:
                            frame = validate_child_frame(
                                json.loads(line.decode("utf-8")), assignment.task_id
                            )
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            raise TaskProtocolError("task child emitted invalid JSON") from None
                        if not ready and frame["type"] != "ready":
                            raise TaskProtocolError("task child emitted data before ready")
                        if frame["type"] == "ready":
                            if ready:
                                raise TaskProtocolError("task child emitted ready twice")
                            ready = True
                        elif frame["type"] in {"progress", "input.required"}:
                            receipt = self._report(assignment, attempt, frame)
                            if frame["type"] == "progress":
                                progress_messages.add(frame["message"])
                            _write_frame(
                                process,
                                {
                                    "protocol_version": PROTOCOL_VERSION,
                                    "type": "report.ack",
                                    "request_id": _request_id(),
                                    "task_id": assignment.task_id,
                                    "report_id": receipt["report_id"],
                                    "sequence": receipt["sequence"],
                                },
                            )
                        elif frame["type"] == "model.request":
                            model_result = self._model(
                                assignment,
                                attempt,
                                frame,
                                bootstrap["limits"]["max_output_tokens_per_turn"],
                            )
                            # Input arriving during the provider call must reach the
                            # child before it can finish from the returned answer.
                            control = self._control(assignment, attempt, cursor)
                            if control["cancel_requested"] and cancel_started is None:
                                cancel_command_id = control["cancel_command_id"]
                                if not isinstance(cancel_command_id, str):
                                    raise TaskProtocolError("task cancellation command is invalid")
                                _write_frame(
                                    process,
                                    {
                                        "protocol_version": PROTOCOL_VERSION,
                                        "type": "cancel",
                                        "request_id": _request_id(),
                                        "task_id": assignment.task_id,
                                        "command_id": cancel_command_id,
                                        "intentional": True,
                                    },
                                )
                                cancel_started = time.monotonic()
                            self._deliver_input(assignment, attempt, process, control)
                            _write_frame(process, model_result)
                        elif frame["type"] == "result":
                            if result_report is not None:
                                raise TaskProtocolError("task child emitted more than one result")
                            result_report = frame["report"]
                        elif frame["type"] == "cancelled":
                            if (
                                cancel_command_id is None
                                or frame["command_id"] != cancel_command_id
                            ):
                                raise TaskProtocolError(
                                    "task child acknowledged another cancellation"
                                )
                        elif frame["type"] == "error":
                            raise TaskHostError(frame["message"], code=frame["code"])
            exit_code = process.wait()
            stderr_thread.join(timeout=1)
            if cancel_started is not None:
                self._finalize(
                    assignment,
                    attempt,
                    exit_code=exit_code,
                    outcome="cancelled",
                    report=None,
                    error_code="cancelled_by_client",
                    error_message="Task cancelled by client",
                )
            elif exit_code != 0:
                raise TaskHostError(
                    "Task process failed" + (f": {''.join(stderr)[-512:]}" if stderr else ""),
                    code="process_failed",
                )
            elif result_report is None:
                raise TaskHostError(
                    "Task process exited without a valid result", code="invalid_agent_output"
                )
            elif len(progress_messages) < _MIN_PROGRESS_MARKERS:
                raise TaskHostError(
                    "Task process did not persist two distinct progress markers",
                    code="invalid_agent_output",
                )
            else:
                self._finalize(
                    assignment,
                    attempt,
                    exit_code=exit_code,
                    outcome="completed",
                    report=result_report,
                    error_code=None,
                    error_message=None,
                )
            finalized = True
            stop_heartbeat()
            try:
                acknowledge()
            except TaskGatewayError:
                self._settle_unknown(assignment, child_exit_confirmed=True)
                return TASK_EXIT_RETRYABLE
            return 0 if exit_code == 0 and cancel_started is None else TASK_EXIT_FAILED
        except TaskRunClientError as error:
            logger.error("Task runtime reporting unavailable: %s", error)
            if process is not None:
                _stop_process(process)
            self._settle_unknown(
                assignment, child_exit_confirmed=process is None or process.poll() is not None
            )
            return TASK_EXIT_RETRYABLE
        except (
            OSError,
            subprocess.SubprocessError,
            TaskHostError,
            TaskProtocolError,
            UnknownTaskPersonaError,
            ValueError,
        ) as error:
            if process is not None:
                _stop_process(process)
            if attempt is None:
                logger.error("Task bootstrap failed: %s", type(error).__name__)
                return TASK_EXIT_RETRYABLE
            code = (
                error.code
                if isinstance(error, TaskHostError)
                else "protocol_violation"
                if isinstance(error, TaskProtocolError)
                else "process_failed"
            )
            try:
                self._finalize(
                    assignment,
                    attempt,
                    exit_code=process.returncode if process is not None else None,
                    outcome="failed",
                    report=None,
                    error_code=code,
                    error_message=_safe_message(error),
                )
                finalized = True
                stop_heartbeat()
                acknowledge()
                return TASK_EXIT_FAILED
            except (TaskGatewayError, TaskProtocolError, TaskRunClientError):
                logger.error("Task failure could not be durably finalized")
                self._settle_unknown(
                    assignment, child_exit_confirmed=process is None or process.poll() is not None
                )
                return TASK_EXIT_RETRYABLE
        finally:
            stop_heartbeat()
            self.client.clear_credential()
            if process is not None and process.poll() is None:
                _stop_process(process)
            if workspace is not None:
                shutil.rmtree(workspace, ignore_errors=True)
            if not finalized:
                logger.info("Task assignment left unacknowledged for recovery")
