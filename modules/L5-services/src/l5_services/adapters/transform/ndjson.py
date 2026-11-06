"""Bounded frame mediation for the pinned Transform programmatic interface."""

import json
import math
from copy import deepcopy
from hashlib import sha256
from pathlib import Path, PurePosixPath


class TransformProtocolError(ValueError):
    pass


def source_path(value):
    if (not isinstance(value, str) or not value or "\\" in value or "\x00" in value
            or PurePosixPath(value).is_absolute()
            or any(part in {"", ".", "..", ".git"} for part in value.split("/"))):
        raise TransformProtocolError("Transform file path is outside the source projection")
    return value


def resume_checkpoint(native_id, checkpoint_id):
    if (not isinstance(checkpoint_id, str) or not checkpoint_id or len(checkpoint_id) > 1024
            or any(character in checkpoint_id for character in "\r\n\x00")):
        raise TransformProtocolError("Transform checkpoint identity is invalid")
    if "#" in checkpoint_id:
        owner, checkpoint = checkpoint_id.split("#", 1)
        if owner != native_id or not checkpoint or "#" in checkpoint:
            raise TransformProtocolError("Transform checkpoint belongs to another session")
        return checkpoint_id
    return f"{native_id}#{checkpoint_id}"


class TransformConversation:
    def __init__(self, intent, *, workspace, agent_minutes, max_bytes=8 * 1024 * 1024):
        self.intent = intent
        self.workspace = Path(workspace).resolve(strict=True)
        self.read_paths = {source_path(path) for path in intent["read_paths"]}
        self.write_paths = {source_path(path) for path in intent["write_paths"]}
        self.excluded_paths = {path if path == ".git" else source_path(path) for path in intent["excluded_paths"]}
        self.agent_minutes = agent_minutes
        self.max_bytes = max_bytes
        self.buffer = b""
        self.total_bytes = 0
        self.native_id = None
        self.minutes_used = None
        self.state = "unknown"
        self.finished = False
        self.tool_calls = set()
        self.checkpoint_id = None
        self.continuations = []
        self.pending_continuation = None
        self.continuation_usage_pending = False
        self.trace = []
        self.trace_bytes = 0

    def _record(self, value):
        self.trace_bytes += len(json.dumps(value, sort_keys=True).encode())
        if len(self.trace) >= 1024 or self.trace_bytes > 262144:
            raise TransformProtocolError("Transform capability trace exceeds its bound")
        self.trace.append(value)

    def begin_continuation(self, record):
        if (self.finished or self.buffer or self.continuations or self.pending_continuation
                or record.get("native_id") != self.native_id
                or record.get("checkpoint_id") != self.checkpoint_id):
            raise TransformProtocolError("Transform continuation is not the original checkpoint")
        self.continuations.append(deepcopy(record))
        self.pending_continuation = deepcopy(record)
        self.minutes_used = record["minutes_used"]
        self.continuation_usage_pending = True
        self._record({"type": "continuation_requested", **record})

    def capability_trace(self):
        return {"schema": "transform-provider-trace/v1", "frames": deepcopy(self.trace),
                "continuations": deepcopy(self.continuations), "agent_minutes_limit": self.agent_minutes}

    def _tool_record(self, event, decision):
        name = event.get("name")
        parameters = event.get("parameters")
        parameters = parameters if isinstance(parameters, dict) else {}
        writes = name == "file_write" or name == "editor" and parameters.get("command") not in {"view", "find_line"}
        if name not in {"file_read", "file_write", "editor"}:
            control = "denied_shell" if name == "shell" else "denied_other_tool"
        elif decision == "approve":
            control = "allowed_source_write" if writes else "allowed_source_read"
        else:
            without_execute = {**event, "parameters": {**parameters, "executable": False}}
            if (not isinstance(parameters.get("path"), str)
                    or name == "editor" and parameters.get("command") not in {"view", "find_line", "str_replace", "insert", "create"}
                    or writes and self._permission(without_execute)):
                control = "denied_other_tool"
            else:
                control = "denied_source_write" if writes else "denied_source_read"
        # Never retain content, arbitrary command text, or an attacker-chosen
        # path/name. The source-owned mediator records its actual decision and
        # a digest of only the permission inputs.
        selected = {key: parameters[key] for key in ("path", "executable") if key in parameters}
        if name == "editor":
            selected["command"] = parameters.get("command")
        self._record({"type": "tool_mediation", "decision": decision, "control": control,
                      "input_sha256": sha256(json.dumps([name, selected], sort_keys=True).encode()).hexdigest()})

    def _permission(self, event):
        name, parameters = event.get("name"), event.get("parameters")
        if name not in {"file_read", "file_write", "editor"} or not isinstance(parameters, dict):
            return False
        path = parameters.get("path")
        if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
            return False
        selected = Path(path)
        if not selected.is_absolute():
            selected = self.workspace / selected
        try:
            relative = selected.relative_to(self.workspace)
            source_path(relative.as_posix())
            current = self.workspace
            for component in relative.parts:
                current = current / component
                if current.is_symlink():
                    return False
            selected.resolve().relative_to(self.workspace)
        except (ValueError, OSError, RuntimeError):
            return False
        key = relative.as_posix()
        if any(key == excluded or key.startswith(excluded + "/") for excluded in self.excluded_paths):
            return False
        if name == "editor" and parameters.get("command") not in {
                "view", "find_line", "str_replace", "insert", "create"}:
            return False
        writes = name == "file_write" or name == "editor" and parameters["command"] not in {"view", "find_line"}
        selected_paths = self.write_paths if writes else self.read_paths | self.write_paths
        return (any(key == allowed or key.startswith(allowed + "/") for allowed in selected_paths)
                and (not writes or not parameters.get("executable", False)))

    def feed(self, chunk):
        if not isinstance(chunk, bytes):
            raise TransformProtocolError("Transform output must be bytes")
        self.total_bytes += len(chunk)
        self.buffer += chunk
        if self.total_bytes > self.max_bytes:
            raise TransformProtocolError("Transform output exceeds its bound")
        replies = []
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError) as error:
                raise TransformProtocolError("Malformed Transform frame") from error
            if not isinstance(event, dict) or not isinstance(event.get("type"), str) or self.finished:
                raise TransformProtocolError("Unexpected Transform frame")
            kind = event["type"]
            if self.pending_continuation and kind not in {"resume_status", "info", "warn", "error"}:
                raise TransformProtocolError("Transform continuation did not acknowledge the original session")
            if kind in {"session_started", "resume_status"}:
                native_id = event.get("conversationId")
                if (not isinstance(native_id, str) or not native_id or len(native_id) > 512
                        or self.native_id is not None and (native_id != self.native_id or kind == "session_started")):
                    raise TransformProtocolError("Transform conversation identity changed")
                if self.checkpoint_id is not None:
                    resume_checkpoint(native_id, self.checkpoint_id)
                self.native_id = native_id
                self.state = "running"
                if kind == "resume_status":
                    if (not self.pending_continuation
                            or event.get("checkpointId") != resume_checkpoint(native_id, self.pending_continuation["checkpoint_id"])):
                        raise TransformProtocolError("Transform resumed an unrequested checkpoint")
                    self.pending_continuation = None
                self._record({"type": kind, "native_id": native_id,
                              **({"checkpoint_id": event["checkpointId"]} if kind == "resume_status" else {})})
            elif kind == "checkpoint":
                checkpoint = event.get("checkpointId")
                if (not isinstance(checkpoint, str) or not checkpoint
                        or len(checkpoint) > 1024 or any(character in checkpoint for character in "\r\n\x00")):
                    raise TransformProtocolError("Transform checkpoint identity is invalid")
                if self.native_id is not None:
                    resume_checkpoint(self.native_id, checkpoint)
                self.checkpoint_id = checkpoint
                self._record({"type": kind, "checkpoint_id": checkpoint})
            elif kind == "agent_minutes_update":
                used = event.get("used")
                if (type(used) not in {int, float} or not math.isfinite(used)
                        or not 0 <= used <= self.agent_minutes
                        or self.minutes_used is not None and used < self.minutes_used):
                    raise TransformProtocolError("Transform usage exceeds the admitted budget")
                self.minutes_used = used
                self.continuation_usage_pending = False
                self._record({"type": kind, "used": used})
            elif kind == "tool_permission_required":
                call_id = event.get("toolCallId")
                if (not self.native_id or not isinstance(call_id, str) or not call_id
                        or len(call_id) > 512 or call_id in self.tool_calls):
                    raise TransformProtocolError("Transform tool request identity is invalid")
                self.tool_calls.add(call_id)
                decision = "approve" if self._permission(event) else "deny"
                replies.append({"type": "tool_permission_response", "toolCallId": call_id, "decision": decision})
                self._tool_record(event, decision)
            elif kind == "complete":
                if (event.get("status") != "completed" or not self.native_id or self.minutes_used is None
                        or self.continuation_usage_pending):
                    raise TransformProtocolError("Transform completion lacks native identity or usage")
                self.state, self.finished = "completed", True
                self._record({"type": kind, "status": "completed"})
            elif kind == "input_required":
                self.state, self.finished = "unknown", True
            elif kind == "error":
                self.state, self.finished = "failed", True
            elif kind == "tool_call_start":
                if event.get("trusted") is True and not self._permission(event):
                    raise TransformProtocolError("Transform attempted an unmediated tool outside the file policy")
                if event.get("trusted") is True:
                    self._tool_record(event, "approve")
            elif kind not in {"text_delta", "tool_call_result", "tool_output_delta",
                              "info", "warn", "transformation_complete"}:
                raise TransformProtocolError("Unrecognized Transform frame")
        return replies

    def readback(self):
        if self.native_id is None:
            return None
        return {"native_id": self.native_id, "attempt_token": self.intent["attempt_token"],
                "input_hash": self.intent["input_hash"], "state": self.state}
