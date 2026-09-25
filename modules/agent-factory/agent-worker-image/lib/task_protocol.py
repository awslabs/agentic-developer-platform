"""Strict validators for the task host/child NDJSON boundary."""

from __future__ import annotations

import re
import uuid

SCHEMA_VERSION = "1.0"
PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 65536
PROGRESS_STAGES = frozenset({"evidence_inventory", "analysis", "clarification", "synthesis"})

_TASK_ID = re.compile(
    r"^tsk_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_ID = re.compile(
    r"^art_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class TaskProtocolError(Exception):
    """A gateway or child value violated the frozen task contract."""


def _uuid4(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.version == 4 and str(parsed) == value


def _exact(value: object, required: set[str], optional: set[str] = frozenset()) -> dict:
    if (
        not isinstance(value, dict)
        or not required.issubset(value)
        or set(value) - required - optional
    ):
        raise TaskProtocolError("task protocol object has an invalid shape")
    return value


def validate_bootstrap(value: object, assignment) -> dict:
    body = _exact(
        value,
        {
            "schema_version",
            "task_id",
            "invocation_id",
            "generation",
            "persona",
            "run_credential",
            "run_credential_expires_at",
            "input",
            "model_binding",
            "limits",
        },
        {"deadline_at", "capabilities"},
    )
    if (
        body["schema_version"] != SCHEMA_VERSION
        or body["task_id"] != assignment.task_id
        or body["invocation_id"] != assignment.invocation_id
        or body["generation"] != assignment.generation
        or body["persona"] != assignment.persona
    ):
        raise TaskProtocolError("task bootstrap identity mismatch")
    task_input = _exact(
        body["input"],
        {"instructions", "input_digest"},
        {"inputs", "acceptance_criteria", "artifacts"},
    )
    if (
        not isinstance(task_input["instructions"], str)
        or not 1 <= len(task_input["instructions"]) <= 16000
        or task_input["input_digest"] != assignment.input_digest
    ):
        raise TaskProtocolError("task bootstrap input mismatch")
    artifacts = task_input.get("artifacts", [])
    if not isinstance(artifacts, list) or len(artifacts) > 4:
        raise TaskProtocolError("task bootstrap artifacts are invalid")
    actual_refs = []
    for artifact in artifacts:
        item = _exact(
            artifact,
            {"artifact_id", "version", "content_sha256", "content_type"},
        )
        actual_refs.append(
            {
                "artifact_id": item["artifact_id"],
                "version": item["version"],
                "content_sha256": item["content_sha256"],
            }
        )
    if tuple(actual_refs) != assignment.artifact_refs:
        raise TaskProtocolError("task bootstrap artifact binding mismatch")
    if not isinstance(task_input.get("inputs", {}), dict):
        raise TaskProtocolError("task bootstrap inputs are invalid")
    criteria = task_input.get("acceptance_criteria", [])
    if not isinstance(criteria, list) or len(criteria) > 10 or any(
        not isinstance(item, str) or len(item) > 1000 for item in criteria
    ):
        raise TaskProtocolError("task bootstrap acceptance criteria are invalid")
    model = _exact(
        body["model_binding"],
        {
            "model_id",
            "transport",
            "model_policy_version",
            "request_shape_version",
            "pricing_evidence_version",
            "invocability_verified",
        },
    )
    if (
        not isinstance(model["model_id"], str)
        or not 1 <= len(model["model_id"]) <= 128
        or model["transport"] not in {"anthropic_messages", "openai_responses"}
        or model["invocability_verified"] is not True
    ):
        raise TaskProtocolError("task model binding is not invocable")
    limits = _exact(
        body["limits"],
        {"max_turns", "max_output_tokens_per_turn", "max_usd", "deadline_at"},
        {
            "max_provider_operation_seconds",
            "max_events",
            "max_result_artifact_bytes",
            "heartbeat_interval_seconds",
        },
    )
    if (
        type(limits["max_turns"]) is not int
        or not 1 <= limits["max_turns"] <= 8
        or type(limits["max_output_tokens_per_turn"]) is not int
        or not 1 <= limits["max_output_tokens_per_turn"] <= 4096
    ):
        raise TaskProtocolError("task limits are invalid")
    capabilities = body.get("capabilities", [])
    if not isinstance(capabilities, list) or any(
        item not in {"input", "cancel"} for item in capabilities
    ):
        raise TaskProtocolError("task capabilities are invalid")
    return body


def validate_child_frame(value: object, task_id: str) -> dict:
    if not isinstance(value, dict):
        raise TaskProtocolError("task child frame is not an object")
    common = {"protocol_version", "type", "request_id", "task_id"}
    if value.get("protocol_version") != PROTOCOL_VERSION or value.get("task_id") != task_id:
        raise TaskProtocolError("task child frame identity mismatch")
    if not _uuid4(value.get("request_id")):
        raise TaskProtocolError("task child request id is invalid")
    frame_type = value.get("type")
    if frame_type == "ready":
        body = _exact(value, common | {"capabilities"})
        capabilities = body["capabilities"]
        if not isinstance(capabilities, list) or not capabilities or any(
            item not in {"input", "cancel"} for item in capabilities
        ):
            raise TaskProtocolError("task child capabilities are invalid")
    elif frame_type == "progress":
        body = _exact(
            value,
            common | {"report_id", "message"},
            {"stage", "producer_timestamp"},
        )
        if (
            not _uuid4(body["report_id"])
            or not isinstance(body["message"], str)
            or not 1 <= len(body["message"]) <= 4000
            or body.get("stage", "analysis") not in PROGRESS_STAGES
            or any(key in body for key in ("reasoning", "thinking", "percentage"))
        ):
            raise TaskProtocolError("task progress frame is invalid")
    elif frame_type == "model.request":
        if "responses_request" in value:
            body = _exact(value, common | {"turn_id", "responses_request"})
            response = _exact(body["responses_request"], {"input", "reasoning", "max_output_tokens"}, {"instructions"})
            reasoning = _exact(response["reasoning"], {"effort"})
            if reasoning["effort"] not in {"minimal", "low", "medium", "high", "xhigh"}:
                raise TaskProtocolError("Responses effort is invalid")
            maximum = response["max_output_tokens"]
            if type(maximum) is not int or not 1 <= maximum <= 4096:
                raise TaskProtocolError("Responses output bound is invalid")
            if "instructions" in response and (not isinstance(response["instructions"], str) or len(response["instructions"]) > 32000):
                raise TaskProtocolError("Responses instructions exceed bound")
            inputs = response["input"]
            if isinstance(inputs, str):
                if not 1 <= len(inputs) <= 32000:
                    raise TaskProtocolError("Responses input exceeds bound")
            elif isinstance(inputs, list) and 1 <= len(inputs) <= 64:
                for item in inputs:
                    if isinstance(item, dict) and item.get("type") == "reasoning":
                        entry = _exact(item, {"type", "encrypted_content", "summary"}, {"status"})
                        if (not isinstance(entry["encrypted_content"], str)
                                or not 1 <= len(entry["encrypted_content"]) <= 32768
                                or entry.get("status", "completed") != "completed"
                                or not isinstance(entry["summary"], list) or len(entry["summary"]) > 16):
                            raise TaskProtocolError("Responses reasoning is invalid")
                        for part in entry["summary"]:
                            summary = _exact(part, {"type", "text"})
                            if (summary["type"] != "summary_text" or not isinstance(summary["text"], str)
                                    or len(summary["text"]) > 32000):
                                raise TaskProtocolError("Responses summary is invalid")
                        continue
                    entry = _exact(item, {"role", "content"}, {"type", "status", "phase"})
                    if (entry["role"] not in {"system", "developer", "user", "assistant"}
                            or entry.get("type", "message") != "message"
                            or entry.get("status", "completed") != "completed"
                            or ("phase" in entry and entry["phase"] not in {"commentary", "final_answer"})):
                        raise TaskProtocolError("Responses message is invalid")
                    content = entry["content"]
                    if isinstance(content, str):
                        if len(content) > 32000:
                            raise TaskProtocolError("Responses text exceeds bound")
                    elif isinstance(content, list) and len(content) <= 64:
                        for part in content:
                            text = _exact(part, {"type", "text"}, {"annotations"})
                            if (text["type"] not in {"input_text", "output_text"}
                                    or not isinstance(text["text"], str) or len(text["text"]) > 32000
                                    or text.get("annotations", []) != []):
                                raise TaskProtocolError("Responses content is invalid")
                    else:
                        raise TaskProtocolError("Responses content is invalid")
            else:
                raise TaskProtocolError("Responses input is invalid")
        elif "sdk_request" in value:
            body = _exact(value, common | {"turn_id", "sdk_request"}, {"max_tokens"})
            sdk = _exact(body["sdk_request"], {"messages"},
                         {"system", "tools", "tool_choice", "stop_sequences"})
            messages = sdk["messages"]
            if not isinstance(messages, list) or not 1 <= len(messages) <= 32:
                raise TaskProtocolError("SDK messages exceed bounds")
            for message in messages:
                item = _exact(message, {"role", "content"})
                if item["role"] not in {"user", "assistant"} or not isinstance(item["content"], (str, list)) or not item["content"]:
                    raise TaskProtocolError("SDK message is invalid")
        else:
            body = _exact(value, common | {"turn_id", "messages"}, {"max_tokens", "system"})
            messages = body["messages"]
            if not isinstance(messages, list) or not messages:
                raise TaskProtocolError("task model request is invalid")
            for message in messages:
                item = _exact(message, {"role", "content"})
                if item["role"] not in {"user", "assistant"} or not isinstance(item["content"], str) or not item["content"]:
                    raise TaskProtocolError("task model request is invalid")
        if not _uuid4(body["turn_id"]):
            raise TaskProtocolError("task model turn is invalid")
        max_tokens = body.get("max_tokens")
        if max_tokens is not None and (
            type(max_tokens) is not int or not 1 <= max_tokens <= 4096
        ):
            raise TaskProtocolError("task model request is invalid")
    elif frame_type == "tool.request":
        body = _exact(value, common | {"tool", "payload"})
        if (not isinstance(body["tool"], str)
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}\.[a-z][a-z0-9_]{0,63}", body["tool"])
                or not isinstance(body["payload"], dict)):
            raise TaskProtocolError("Invalid generic tool request")
    elif frame_type == "cyber.request":
        body = _exact(value, common | {"operation", "payload"})
        if body["operation"] not in {"triage", "static", "result", "url_analysis", "dynamic", "enrich"} or not isinstance(body["payload"], dict):
            raise TaskProtocolError("task cyber operation is invalid")
        operation = body["operation"]
        required = "sample_s3_uri" if operation in {"triage", "static", "dynamic"} else {"result": "job_id", "url_analysis": "url", "enrich": "sha256"}[operation]
        optional = {"focus", "yara_rules"} if operation in {"triage", "static", "dynamic"} else set()
        payload = _exact(body["payload"], {required}, optional)
        if not isinstance(payload[required], str) or not 1 <= len(payload[required]) <= 4096:
            raise TaskProtocolError("cyber payload identifier is invalid")
        for key in optional & payload.keys():
            if not isinstance(payload[key], list) or len(payload[key]) > 32 or any(not isinstance(item, str) or not 1 <= len(item) <= 4000 for item in payload[key]):
                raise TaskProtocolError("cyber payload options exceed bounds")
    elif frame_type == "input.required":
        body = _exact(value, common | {"input_request_id", "prompt"})
        if (
            not _uuid4(body["input_request_id"])
            or not isinstance(body["prompt"], str)
            or not 1 <= len(body["prompt"]) <= 4000
        ):
            raise TaskProtocolError("task input request is invalid")
    elif frame_type == "result":
        body = _exact(value, common | {"report"})
        validate_investigator_report(body["report"])
    elif frame_type == "cancelled":
        body = _exact(value, common | {"command_id"}, {"partial_findings"})
        partial = body.get("partial_findings")
        if not _uuid4(body["command_id"]) or (
            partial is not None and (type(partial) is not int or partial < 0)
        ):
            raise TaskProtocolError("task cancellation receipt is invalid")
    elif frame_type == "error":
        body = _exact(value, common | {"code", "message"})
        if body["code"] not in {
            "invalid_agent_output",
            "protocol_violation",
            "process_failed",
            "deadline_exceeded",
            "model_outcome_unknown",
        } or not isinstance(body["message"], str) or not 1 <= len(body["message"]) <= 1000:
            raise TaskProtocolError("task child error code is invalid")
    else:
        raise TaskProtocolError("task child frame type is unsupported")
    return value


def validate_investigator_report(value: object) -> dict:
    report = _exact(
        value,
        {"summary", "findings", "uncertainties", "recommendations", "evidence_refs"},
    )
    if not isinstance(report["summary"], str) or not 1 <= len(report["summary"]) <= 4000:
        raise TaskProtocolError("task result summary is invalid")
    for name in ("findings", "uncertainties", "recommendations", "evidence_refs"):
        if not isinstance(report[name], list):
            raise TaskProtocolError("task result report is invalid")
    for finding in report["findings"]:
        item = _exact(finding, {"statement", "evidence_refs"}, {"confidence"})
        if (
            not isinstance(item["statement"], str)
            or not 1 <= len(item["statement"]) <= 2000
            or not isinstance(item["evidence_refs"], list)
            or not item["evidence_refs"]
            or any(
                not isinstance(reference, str) or not 1 <= len(reference) <= 256
                for reference in item["evidence_refs"]
            )
            or (
                "confidence" in item
                and item["confidence"] not in {"low", "medium", "high"}
            )
        ):
            raise TaskProtocolError("task result finding is invalid")
    for name in ("uncertainties", "recommendations"):
        if any(
            not isinstance(item, str) or not 1 <= len(item) <= 1000 for item in report[name]
        ):
            raise TaskProtocolError("task result report is invalid")
    for evidence in report["evidence_refs"]:
        item = _exact(evidence, {"ref", "source"}, {"artifact_id"})
        if (
            not isinstance(item["ref"], str)
            or not 1 <= len(item["ref"]) <= 256
            or item["source"]
            not in {"inputs", "artifact", "instructions", "follow_up_input"}
            or (
                "artifact_id" in item
                and (
                    not isinstance(item["artifact_id"], str)
                    or not _ARTIFACT_ID.fullmatch(item["artifact_id"])
                )
            )
        ):
            raise TaskProtocolError("task result evidence is invalid")
    return report
