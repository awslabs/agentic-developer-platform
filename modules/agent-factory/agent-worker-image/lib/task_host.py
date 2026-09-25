"""Independent Task API process host for the shared worker image."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import queue
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
from lib.task_run_client import TaskRunClient, TaskRunClientError, TaskRunClientUnavailable, workload_identity

logger = logging.getLogger(__name__)
TASK_EXIT_RETRYABLE = 75
TASK_EXIT_FAILED = 1
_MIN_PROGRESS_MARKERS = 2
_CONTROL_POLL_SECONDS = 1.0
_MODEL_POLL_SECONDS = 1.0
_MODEL_RECEIPT_SECONDS = 150.0
_TOOL_RECEIPT_SECONDS = 180.0
_TERM_AFTER_SECONDS = 20.0
_KILL_AFTER_SECONDS = 30.0
_REPORT_BUFFER_MAX_REPORTS = 128
_REPORT_BUFFER_MAX_BYTES = 262144
_REPORT_BUFFER_MAX_SECONDS = 10.0
_REPORT_RETRY_SECONDS = 0.25


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


def _child_environment(workspace: Path, *, sdk: bool = False) -> dict[str, str]:
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
        "ADP_TASK_NETWORK": "host-mediated-sdk" if sdk else "disabled",
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
        self._report_renderer = None
        self._report_context = {"steps": []}

    def _binding(self, assignment, runtime_attempt_id: str) -> dict:
        return {
            "run": {
                "task_id": assignment.task_id,
                "invocation_id": assignment.invocation_id,
                "generation": assignment.generation,
            },
            "runtime_attempt_id": runtime_attempt_id,
        }

    def _report_payload(self, attempt: dict, frame: dict) -> dict:
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
        return {
            "schema_version": SCHEMA_VERSION,
            "attempt": attempt,
            "report_id": report_id,
            "event_type": event_type,
            "producer_timestamp": frame.get("producer_timestamp"),
            "data": data,
        }

    def _report(self, assignment, attempt: dict, frame: dict) -> dict:
        payload = self._report_payload(attempt, frame)
        report_id = payload["report_id"]
        logger.info(
            "Task report emission task_id=%s report_id=%s event_type=%s emitted_at=%s",
            assignment.task_id, report_id, payload["event_type"],
            datetime.now(UTC).isoformat(timespec="milliseconds"),
        )
        response = self.client.report(payload)
        if (
            set(response) != {"schema_version", "report_id", "sequence", "event_id"}
            or response["schema_version"] != SCHEMA_VERSION
            or response["report_id"] != report_id
            or type(response["sequence"]) is not int
            or response["sequence"] < 1
        ):
            raise TaskRunClientError("task report receipt is invalid")
        return response

    def _turn(self, assignment, attempt: dict, request_id: str, *, allow_autonomous: bool = False) -> dict | None:
        if request_id in self._turns:
            return self._turns[request_id]
        response = self.client.turn(
            {
                "schema_version": SCHEMA_VERSION,
                "attempt": attempt,
                "request_id": request_id,
                "expected_transcript_version": self._turn_number + 1,
                **({"allow_autonomous": True} if allow_autonomous else {}),
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

    def _model_request(self, assignment, attempt: dict, frame: dict, max_tokens: int) -> dict:
        if self._pending_turn_id is not None and frame["turn_id"] != self._pending_turn_id:
            raise TaskProtocolError("child model request skipped its assigned input turn")
        if "responses_request" in frame:
            request = frame["responses_request"]
            if request["max_output_tokens"] > max_tokens:
                raise TaskProtocolError("Responses output bound exceeds grant")
        elif "sdk_request" in frame:
            request = {**frame["sdk_request"], "max_tokens": frame.get("max_tokens", max_tokens)}
        else:
            messages = frame["messages"]
            if not isinstance(messages, list) or not 1 <= len(messages) <= 32:
                raise TaskProtocolError("child model messages exceed gateway bounds")
            normalized = []
            for message in messages:
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, str) or not content or message.get("role") not in {"user", "assistant"}:
                    raise TaskProtocolError("child model message must contain text")
                blocks = [{"type": "text", "text": content[offset:offset + 32000]}
                          for offset in range(0, len(content), 32000)]
                if len(blocks) > 16:
                    raise TaskProtocolError("child model message exceeds text block limit")
                normalized.append({"role": message["role"], "content": blocks})
            request = {"messages": normalized, "max_tokens": frame.get("max_tokens", max_tokens)}
            if "system" in frame:
                request["system"] = frame["system"]
        prepared = {
            "schema_version": SCHEMA_VERSION,
            "attempt": attempt,
            "turn_id": frame["turn_id"],
            "request_digest": _canonical_digest(request),
            **request,
        }
        if "responses_request" in frame:
            prepared = {"schema_version": SCHEMA_VERSION, "attempt": attempt,
                        "turn_id": frame["turn_id"], "request_digest": _canonical_digest(request),
                        "responses_request": request}
        elif "sdk_request" in frame:
            prepared = {"schema_version": SCHEMA_VERSION, "attempt": attempt,
                        "turn_id": frame["turn_id"], "request_digest": _canonical_digest(request),
                        "max_tokens": request["max_tokens"], "sdk_request": frame["sdk_request"]}
        # Match both the transport bytes and the gateway's invocation-size check.
        # The child reserves wrapper headroom while selecting labelled excerpts.
        if (len(json.dumps(prepared, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_FRAME_BYTES
                or len(json.dumps(request, ensure_ascii=False).encode("utf-8")) > MAX_FRAME_BYTES):
            raise TaskProtocolError("wrapped model request exceeds 65536-byte bound")
        turn = self._turn(assignment, attempt, frame["turn_id"], allow_autonomous="sdk_request" in frame or "responses_request" in frame)
        if turn is None:
            raise TaskProtocolError("child requested a model without a committed turn")
        self._pending_turn_id = None
        return prepared

    def _model(self, assignment, attempt: dict, frame: dict, max_tokens: int, *, prepared: dict | None = None) -> dict:
        prepared = prepared if prepared is not None else self._model_request(assignment, attempt, frame, max_tokens)
        request_digest = prepared["request_digest"]
        response = self.client.model(prepared)
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
        if status == "confirmed" and "responses_request" in prepared and (
                not isinstance(response.get("responses_response"), dict)
                or response["responses_response"].get("status") != "completed"):
            raise TaskHostError("confirmed Responses receipt has no complete result", code="protocol_violation")
        if status != "confirmed" and (
            response.get("content") is not None or response.get("stop_reason") is not None or response.get("responses_response") is not None
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
            **({"usage": response["usage"]} if isinstance(response.get("usage"), dict) else {}),
            **({"responses_response": response["responses_response"]} if "responses_request" in prepared and status == "confirmed" else {}),
            "content": response.get("content"),
            "stop_reason": response.get("stop_reason"),
            "error_code": (
                "model_outcome_unknown" if status == "unknown" else response.get("error_code")
            ),
        }

    def _cyber(self, assignment, attempt: dict, frame: dict) -> dict:
        if "model_call" in frame:
            from lib.task_codex_tools import execute_codex_tool
            return execute_codex_tool(self.client, task_id=assignment.task_id, attempt=attempt, frame=frame,
                invoke=lambda: self._cyber(assignment, attempt, {key: value for key, value in frame.items() if key != "model_call"}))
        generic = frame["type"] == "tool.request"
        operation = frame["tool"].split(".", 1)[1] if generic else frame["operation"]
        body = {"schema_version": SCHEMA_VERSION, "attempt": attempt,
                "operation_id": frame["request_id"], "operation": operation, "payload": frame["payload"]}
        started_at = _now()
        response = self.client.tool(frame["tool"], body) if generic else self.client.cyber(body)
        if (response.get("schema_version") != SCHEMA_VERSION or response.get("task_id") != assignment.task_id
                or response.get("operation_id") != frame["request_id"]
                or response.get("operation_status") not in {"confirmed", "pending", "unknown", "rejected"}):
            raise TaskRunClientError("cyber receipt identity or status is invalid")
        if response["operation_status"] == "confirmed" and (
                not isinstance(response.get("result"), dict) or not isinstance(response.get("artifact"), dict)):
            raise TaskRunClientError("confirmed cyber receipt lacks durable artifact")
        if response["operation_status"] == "confirmed":
            artifact = response["artifact"]
            if (not isinstance(artifact.get("artifact_id"), str) or not artifact["artifact_id"].startswith("art_")
                    or artifact.get("content_type") != "application/json"
                    or not isinstance(artifact.get("content_sha256"), str) or len(artifact["content_sha256"]) != 64
                    or type(artifact.get("byte_length")) is not int or not 0 < artifact["byte_length"] <= 32768):
                raise TaskRunClientError("cyber artifact metadata is invalid")
        if self._report_renderer is not None:
            steps = self._report_context["steps"]
            if len(steps) < 128:
                steps.append({"tool": frame.get("tool", "cyber." + operation),
                              "payload": frame["payload"], "started_at": started_at,
                              "finished_at": _now(), "operation_status": response["operation_status"],
                              "result": response.get("result") if isinstance(response.get("result"), dict) else None,
                              "artifact": response.get("artifact")})
            else:
                self._report_context["steps_truncated"] = True
        return {"protocol_version": PROTOCOL_VERSION, "type": "tool.result" if generic else "cyber.result",
            **({"tool": frame["tool"]} if generic else {"operation": operation}),
            "request_id": frame["request_id"], "task_id": assignment.task_id,
            "operation_status": response["operation_status"], "result": response.get("result", {}),
            **({"artifact": response["artifact"]} if "artifact" in response else {}),
            **({"error_code": response["error_code"]} if "error_code" in response else {})}

    def _cancel_cyber_jobs(self, assignment, attempt: dict) -> None:
        # A stable UUID4-shaped identity permits cleanup receipt checks without
        # replaying normal tools. Stopping the SDK process alone is insufficient.
        operation_id = str(uuid.UUID(bytes=hashlib.sha256(
            f"{attempt['runtime_attempt_id']}:cyber.cancel_jobs".encode()
        ).digest()[:16], version=4))
        response = self.client.cyber({"schema_version": SCHEMA_VERSION, "attempt": attempt,
            "operation_id": operation_id, "operation": "cancel_jobs", "payload": {}})
        if (response.get("schema_version") != SCHEMA_VERSION or response.get("task_id") != assignment.task_id
                or response.get("operation_id") != operation_id or response.get("operation_status") != "confirmed"
                or not isinstance(response.get("result"), dict)
                or response["result"].get("status") != "confirmed"
                or response["result"].get("pending_jobs") != []):
            raise TaskRunClientError("cyber downstream cancellation remains unconfirmed")

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

    def _result_artifact(self, assignment, report: dict) -> str:
        """Publish the validated report before completion, replaying identical bytes."""
        try:
            content = json.dumps(report, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise TaskProtocolError("result report is not valid JSON") from None
        if not isinstance(report, dict) or not 0 < len(content) <= 1048576:
            raise TaskProtocolError("result artifact exceeds its fixed bound")
        return self._output_artifact(assignment, content, "application/json")

    def _output_artifact(self, assignment, content: bytes, content_type: str) -> str:
        if content_type not in {"text/plain", "application/json", "text/html"} or not isinstance(content, bytes) or not 0 < len(content) <= 1048576:
            raise TaskProtocolError("Invalid rendered output artifact")
        digest = hashlib.sha256(content).hexdigest()
        expected_id = "art_" + str(uuid.UUID(bytes=hashlib.sha256(
            f"{assignment.task_id}:{content_type}:{digest}".encode()
        ).digest()[:16], version=4))
        body = {
            "schema_version": SCHEMA_VERSION,
            "run": {"task_id": assignment.task_id, "invocation_id": assignment.invocation_id,
                    "generation": assignment.generation},
            "content_type": content_type,
            "content_sha256": digest,
            "content_base64": base64.b64encode(content).decode("ascii"),
        }
        for attempt_number in range(3):
            try:
                receipt = self.client.artifact(body)
                break
            except TaskRunClientUnavailable:
                # The gateway derives immutable artifact identity from task/type/
                # digest. A lost response retries the same write, not model work.
                if attempt_number == 2:
                    raise
                time.sleep(_REPORT_RETRY_SECONDS)
        if (
            receipt.get("schema_version") != SCHEMA_VERSION
            or receipt.get("artifact_id") != expected_id
            or type(receipt.get("version")) is not int
            or receipt["version"] != 1
            or receipt.get("content_type") != content_type
            or receipt.get("content_sha256") != digest
            or receipt.get("expires_at", "missing") is not None
        ):
            raise TaskRunClientError("result artifact receipt integrity check failed")
        return expected_id

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
        result_refs = []
        if outcome == "completed":
            if exit_code != 0 or not isinstance(report, dict):
                raise TaskProtocolError("completed result requires a validated report and zero child exit")
            result_refs = [self._result_artifact(assignment, report)]
            if self._report_renderer is not None:
                try:
                    rendered = self._report_renderer(report=report, context=self._report_context)
                    if not isinstance(rendered, dict) or set(rendered) != {"content", "content_type"}:
                        raise ValueError("Invalid renderer output")
                except Exception as exc:
                    raise TaskHostError("Task report rendering failed") from exc
                result_refs.append(self._output_artifact(assignment, rendered["content"], rendered["content_type"]))
            result = {
                "schema_version": SCHEMA_VERSION,
                "outcome": "completed",
                "report": report,
                "artifact_ids": result_refs,
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
                "committed_result_refs": result_refs,
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
        sdk = False
        responses = False
        cyber_cleanup_confirmed = False

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
            self._report_renderer = None
            self._report_context = {"task_id": assignment.task_id, "started_at": _now(),
                                    "inputs": bootstrap["input"].get("inputs", {}), "steps": []}
            if os.environ.get("ADP_TASK_REPORT_RENDERERS"):
                from adp_tools.reports import report_renderer
                self._report_renderer = report_renderer(bootstrap["persona"])
            attempt_response = self.client.attempt(
                {
                    "schema_version": SCHEMA_VERSION,
                    "task_id": assignment.task_id,
                    "invocation_id": assignment.invocation_id,
                    "generation": assignment.generation,
                    "runtime_attempt_id": runtime_attempt_id,
                    "protocol_version": PROTOCOL_VERSION,
                    "capabilities": ["cancel"],
                    "old_attempt_invalidated": True,
                }
            )
            if (not isinstance(attempt_response, dict) or attempt_response.get("schema_version") != SCHEMA_VERSION
                    or attempt_response.get("operation_status") != "confirmed"
                    or attempt_response.get("request_id") != runtime_attempt_id):
                raise TaskRunClientError("task attempt was not registered")
            artifacts = self._input_artifacts(assignment, bootstrap)
            command = self.command_resolver(bootstrap["persona"])
            sdk = bootstrap["persona"] == "agent-task-cyber"
            responses = bootstrap["model_binding"]["transport"] == "openai_responses"
            self.work_root.mkdir(parents=True, exist_ok=True)
            workspace = Path(
                tempfile.mkdtemp(prefix=f"task-{assignment.invocation_id[:8]}-", dir=self.work_root)
            )
            workspace.chmod(0o700)
            stderr: list[str] = []
            process = subprocess.Popen(
                command if sdk or responses else _network_wrapped_command(command),
                cwd=workspace,
                env=_child_environment(workspace, sdk=sdk or responses),
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
                    **({"model_binding": bootstrap["model_binding"], "persona": bootstrap["persona"],
                        "deadline_at": bootstrap["deadline_at"],
                        **({"harness": bootstrap["harness"]} if "harness" in bootstrap else {})} if responses else {}),
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
            pending_reports: list[dict] = []
            pending_report_bytes = 0
            report_outage_started: float | None = None
            next_report_retry = 0.0
            deferred_model: dict | None = None

            model_job = None
            cyber_job = None
            cyber_started = None
            cyber_ids: set[str] = set()

            def launch_model_call(job):
                def call():
                    try:
                        value = self._model(assignment, attempt, job["frame"],
                            bootstrap["limits"]["max_output_tokens_per_turn"], prepared=job["body"])
                    except Exception as exc:
                        value = exc
                    job["responses"].put(value)
                job["inflight"] = True
                threading.Thread(target=call, daemon=True, name="task-model-receipt").start()

            def deliver_model(model_frame: dict) -> None:
                nonlocal model_job
                if model_job is not None or cyber_job is not None:
                    raise TaskProtocolError("task child emitted concurrent operations")
                if ("responses_request" in model_frame) != responses:
                    raise TaskProtocolError("model transport does not match bootstrap binding")
                if "sdk_request" in model_frame and not sdk:
                    raise TaskProtocolError("SDK model transport is not authorized for persona")
                admission = self._control(assignment, attempt, cursor)
                if admission["cancel_requested"] or cancel_started is not None:
                    return
                body = self._model_request(assignment, attempt, model_frame,
                    bootstrap["limits"]["max_output_tokens_per_turn"])
                model_job = {"frame": model_frame, "body": body, "responses": queue.Queue(maxsize=1),
                    "inflight": False, "started": time.monotonic(), "next_poll": 0.0}
                launch_model_call(model_job)

            def finish_model(model_result: dict, *, control: dict | None = None) -> None:
                nonlocal cancel_started, cancel_command_id
                # Input arriving during the provider call must reach the
                # child before it can finish from the returned answer.
                control = self._control(assignment, attempt, cursor) if control is None else control
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

            def acknowledge_report(frame: dict, receipt: dict) -> None:
                if frame["type"] == "progress":
                    progress_messages.add(frame["message"])
                # The process protocol permits progress followed by result/exit
                # without waiting for report.ack. A durable report stays valid
                # if the child has already closed its input; drain stdout and
                # validate the result and exit status before deciding success.
                if process.stdin is None or process.stdin.closed:
                    return
                try:
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
                except BrokenPipeError:
                    logger.info("Task child closed input before durable report acknowledgement")
                    try:
                        process.stdin.close()
                    except BrokenPipeError:
                        pass


            def buffer_report(frame: dict, now: float) -> None:
                nonlocal pending_report_bytes, report_outage_started
                report_bytes = len(
                    json.dumps(
                        self._report_payload(attempt, frame),
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                if (
                    len(pending_reports) >= _REPORT_BUFFER_MAX_REPORTS
                    or pending_report_bytes + report_bytes > _REPORT_BUFFER_MAX_BYTES
                ):
                    raise TaskRunClientError("task report recovery buffer exhausted")
                if report_outage_started is None:
                    report_outage_started = now
                pending_reports.append(frame)
                pending_report_bytes += report_bytes
            try:
                deadline = datetime.fromisoformat(bootstrap["limits"]["deadline_at"])
            except (AttributeError, TypeError, ValueError):
                raise TaskProtocolError("task deadline is invalid") from None
            while process.poll() is None or selector.get_map() or pending_reports:
                now = time.monotonic()
                if report_outage_started is not None:
                    if now - report_outage_started >= _REPORT_BUFFER_MAX_SECONDS:
                        raise TaskRunClientError("task report storage did not recover in time")
                    if now >= next_report_retry:
                        try:
                            while pending_reports:
                                frame = pending_reports[0]
                                receipt = self._report(assignment, attempt, frame)
                                pending_report_bytes -= len(
                                    json.dumps(
                                        self._report_payload(attempt, frame),
                                        sort_keys=True,
                                        separators=(",", ":"),
                                    ).encode("utf-8")
                                )
                                pending_reports.pop(0)
                                acknowledge_report(frame, receipt)
                            report_outage_started = None
                        except TaskRunClientError:
                            next_report_retry = now + _REPORT_RETRY_SECONDS
                        else:
                            if deferred_model is not None:
                                deliver_model(deferred_model)
                                deferred_model = None
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
                if model_job is not None and process.poll() is None and cancel_started is None:
                    if now - model_job["started"] >= _MODEL_RECEIPT_SECONDS:
                        raise TaskHostError("Model receipt remained unavailable", code="model_outcome_unknown")
                    if model_job["inflight"]:
                        try:
                            response = model_job["responses"].get_nowait()
                        except queue.Empty:
                            pass
                        else:
                            model_job["inflight"] = False
                            if isinstance(response, TaskRunClientUnavailable):
                                # Same immutable turn+digest only: gateway's durable
                                # claim returns the existing operation, never re-infers.
                                model_job["next_poll"] = now + _MODEL_POLL_SECONDS
                            elif isinstance(response, Exception):
                                raise response
                            elif response["operation_status"] == "pending":
                                # A pending receipt is not a child result. Keep control
                                # polling and wait for actual durable provider evidence.
                                model_job["next_poll"] = now + _MODEL_POLL_SECONDS
                            else:
                                finish_model(response)
                                model_job = None
                    if model_job is not None and not model_job["inflight"] and now >= model_job["next_poll"]:
                        launch_model_call(model_job)
                if cyber_job is not None and process.poll() is None and cancel_started is None:
                    if now - cyber_started >= _TOOL_RECEIPT_SECONDS:
                        raise TaskHostError("Cyber broker receipt remained unavailable", code="process_failed")
                    try:
                        cyber_response = cyber_job.get_nowait()
                    except queue.Empty:
                        pass
                    else:
                        cyber_job = None
                        if isinstance(cyber_response, Exception):
                            raise cyber_response
                        finish_model(cyber_response)
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
                            if report_outage_started is not None:
                                buffer_report(frame, now)
                            else:
                                try:
                                    receipt = self._report(assignment, attempt, frame)
                                except TaskRunClientError:
                                    buffer_report(frame, now)
                                    next_report_retry = now + _REPORT_RETRY_SECONDS
                                else:
                                    acknowledge_report(frame, receipt)
                        elif frame["type"] == "control.request":
                            if not responses:
                                raise TaskProtocolError("child control receipt requires Responses runtime")
                            current = self._control(assignment, attempt, cursor)
                            finish_model({
                                "protocol_version": PROTOCOL_VERSION, "type": "control.result",
                                "task_id": assignment.task_id, "request_id": frame["request_id"],
                                "current": current["attempt_valid"] and not current["cancel_requested"],
                            }, control=current)
                        elif frame["type"] == "model.request":
                            if report_outage_started is not None:
                                if deferred_model is not None:
                                    raise TaskProtocolError(
                                        "task child emitted concurrent model requests"
                                    )
                                deferred_model = frame
                            else:
                                deliver_model(frame)

                        elif frame["type"] in {"cyber.request", "tool.request"}:
                            if "model_call" in frame and not responses:
                                raise TaskProtocolError("Codex tool claim requires Responses profile")
                            if responses and (frame["type"] != "tool.request" or "model_call" not in frame):
                                raise TaskProtocolError("Responses tools require a confirmed model-call binding")
                            if not sdk or cyber_job is not None or model_job is not None or deferred_model is not None or report_outage_started is not None:
                                raise TaskProtocolError("cyber operation is not admitted")
                            if frame["request_id"] in cyber_ids or len(cyber_ids) >= 128:
                                raise TaskProtocolError("cyber operation request identity reused or limit exceeded")
                            control = self._control(assignment, attempt, cursor)
                            if cancel_started is not None or control["cancel_requested"]:
                                continue
                            cyber_ids.add(frame["request_id"])
                            cyber_started = time.monotonic()
                            cyber_job = queue.Queue(maxsize=1)
                            def call_cyber(job=cyber_job, request=frame):
                                try:
                                    value = self._cyber(assignment, attempt, request)
                                except Exception as exc:
                                    value = exc
                                job.put(value)
                            threading.Thread(target=call_cyber, daemon=True, name="task-cyber-broker").start()

                        elif frame["type"] == "result":
                            if cancel_started is None and (cyber_job is not None or model_job is not None or deferred_model is not None):
                                raise TaskProtocolError("child finished with an operation in flight")
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
            if sdk:
                self._cancel_cyber_jobs(assignment, attempt)
                cyber_cleanup_confirmed = True
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
            stopped_exit_code = _stop_process(process) if process is not None else None
            self._settle_unknown(
                assignment,
                child_exit_confirmed=process is None or stopped_exit_code is not None,
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
            stopped_exit_code = _stop_process(process) if process is not None else None
            if attempt is None:
                logger.error("Task bootstrap failed: %s", type(error).__name__)
                return TASK_EXIT_RETRYABLE
            if process is not None and stopped_exit_code is None:
                logger.error("Task child exit could not be confirmed; deferring finalization")
                self._settle_unknown(assignment, child_exit_confirmed=False)
                return TASK_EXIT_RETRYABLE
            code = (
                error.code
                if isinstance(error, TaskHostError)
                else "protocol_violation"
                if isinstance(error, TaskProtocolError)
                else "process_failed"
            )
            try:
                if sdk and not cyber_cleanup_confirmed:
                    self._cancel_cyber_jobs(assignment, attempt)
                self._finalize(
                    assignment,
                    attempt,
                    exit_code=stopped_exit_code,
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
                    assignment,
                    child_exit_confirmed=process is None or stopped_exit_code is not None,
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
