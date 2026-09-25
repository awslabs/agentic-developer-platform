"""Trusted-host claim/execute/settle for Codex Task tools.

Owner tokens never cross IPC. An unconfirmed claim or settlement cannot authorize
an SDK continuation, and a duplicate claim never executes a tool again.
"""

from __future__ import annotations

import hashlib
import json
import re

import rfc8785

from lib.task_run_client import TaskRunClientError

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"


def _content(value):
    if not isinstance(value, dict) or set(value) != {"result", "artifact"} or not isinstance(value["result"], dict):
        raise TaskRunClientError("Codex tool result unavailable")
    artifact = value["artifact"]
    if (not isinstance(artifact, dict) or not re.fullmatch("art_" + _UUID, str(artifact.get("artifact_id", "")))
            or artifact.get("content_type") != "application/json"
            or not re.fullmatch(r"[a-f0-9]{64}", str(artifact.get("content_sha256", "")))
            or type(artifact.get("byte_length")) is not int or not 0 < artifact["byte_length"] <= 32768):
        raise TaskRunClientError("Codex tool artifact unavailable")
    content = rfc8785.dumps(value).decode()
    if len(content.encode()) > 32768:
        raise TaskRunClientError("Codex tool receipt exceeds bound")
    return content


def execute_codex_tool(client, *, task_id, attempt, frame, invoke):
    call = frame["model_call"]
    binding = {"turn_id": call["turn_id"], "call_id": call["call_id"], "tool": frame["tool"],
               "arguments_json": rfc8785.dumps(frame["payload"]).decode(),
               "invocation_id": attempt["run"]["invocation_id"], "generation": attempt["run"]["generation"],
               "runtime_attempt_id": attempt["runtime_attempt_id"]}
    digest = hashlib.sha256(rfc8785.dumps(binding)).hexdigest()
    base = {"protocol_version": frame["protocol_version"], "type": "tool.result", "task_id": task_id,
            "request_id": frame["request_id"], "tool": frame["tool"]}

    def receipt(value, action):
        if not isinstance(value, dict) or value.get("schema_version") != "1.0" or value.get("action") != action:
            raise TaskRunClientError("Codex tool journal response invalid")
        row = value.get("receipt")
        expected = {"schema_version": "1.0", "task_id": task_id, "turn_id": call["turn_id"], "call_id": call["call_id"],
                    "tool": frame["tool"], "request_digest": digest, "automatic_replay_permitted": False}
        if (not isinstance(row, dict) or any(row.get(key) != item for key, item in expected.items())
                or row.get("automatic_replay_permitted") is not False
                or set(row) - set(expected) - {"operation_status", "content", "is_error"}
                or row.get("operation_status") not in {"pending", "confirmed", "unknown", "rejected"}):
            raise TaskRunClientError("Codex tool receipt binding invalid")
        return row

    def result(row):
        if row["operation_status"] != "confirmed":
            return {**base, "operation_status": "rejected" if row["operation_status"] == "rejected" else "unknown", "result": {},
                    "error_code": "tool_outcome_unconfirmed"}
        if not isinstance(row.get("content"), str) or len(row["content"].encode()) > 32768 or type(row.get("is_error")) is not bool:
            raise TaskRunClientError("Codex confirmed tool content missing")
        try:
            value = json.loads(row["content"])
            if _content(value) != row["content"]:
                raise ValueError()
        except (ValueError, TypeError):
            raise TaskRunClientError("Codex confirmed tool content invalid") from None
        return {**base, "operation_status": "confirmed", **value, "content": row["content"], "is_error": row["is_error"]}

    claim = client.tool_operation({"schema_version": "1.0", "action": "claim", "attempt": attempt,
        "turn_id": call["turn_id"], "call_id": call["call_id"], "tool": frame["tool"], "arguments": frame["payload"]})
    claimed = receipt(claim, "claim")
    if type(claim.get("created")) is not bool or set(claim) - {"schema_version", "action", "receipt", "created", "owner_token"}:
        raise TaskRunClientError("Codex tool claim invalid")
    if not claim["created"]:
        if "owner_token" in claim:
            raise TaskRunClientError("Duplicate tool claim included ownership")
        return result(claimed)
    owner = claim.get("owner_token")
    if claimed["operation_status"] != "pending" or not isinstance(owner, str) or not re.fullmatch(_UUID, owner):
        raise TaskRunClientError("Codex tool claim ownership unavailable")

    def settle(status, content=None):
        return client.tool_operation({"schema_version": "1.0", "action": "settle", "attempt": attempt,
            "call_id": call["call_id"], "owner_token": owner, "status": status, "content": content, "is_error": False})

    try:
        produced = invoke()
        if (not isinstance(produced, dict) or any(produced.get(key) != value for key, value in base.items())
                or produced.get("operation_status") not in {"confirmed", "pending", "unknown", "rejected"}):
            raise TaskRunClientError("Codex tool execution receipt invalid")
        status = produced["operation_status"]
        status = "unknown" if status == "pending" else status
        content = _content({"result": produced.get("result"), "artifact": produced.get("artifact")}) if status == "confirmed" else None
    except Exception:
        # Execution may have happened. Retain an unknown result where current
        # authority still permits settlement; never retry the actual tool.
        try:
            settle("unknown")
        except Exception:
            pass
        return {**base, "operation_status": "unknown", "result": {}, "error_code": "tool_outcome_unconfirmed"}
    try:
        settled = settle(status, content)
        if set(settled) != {"schema_version", "action", "receipt"}:
            raise TaskRunClientError("Unexpected tool settlement fields")
        row = receipt(settled, "settle")
        if row["operation_status"] != status or row.get("content") != content or row.get("is_error") is not False:
            raise TaskRunClientError("Tool settlement differs from execution")
        return result(row)
    except Exception:
        # A lost ACK can hide a committed receipt. Leave it intact for a future
        # claim/read; do not overwrite it with unknown or execute again.
        return {**base, "operation_status": "unknown", "result": {}, "error_code": "tool_outcome_unconfirmed"}
