"""Pinned NDJSON frames are synthetic; no native provider calls are made."""

import json
from copy import deepcopy

import pytest

from l5_services.adapters.transform.execution import TransformAdapter
from l5_services.adapters.transform.ndjson import TransformConversation, TransformProtocolError
from l5_services.adapters.transform.provider_job import NDJSON_BINARY_SHA256, NDJSON_INTERFACE, finite_provider_job
from test_provider_job import admitted


def frame(**event):
    return (json.dumps(event) + "\n").encode()


@pytest.fixture
def conversation(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("original")
    return TransformConversation({"attempt_token": "a" * 64, "input_hash": "b" * 64,
                                  "read_paths": ["src"], "write_paths": ["src/app.py"],
                                  "excluded_paths": [".git", "src/private"]},
                                 workspace=tmp_path, agent_minutes=10)


def test_native_identity_and_usage_precede_completion(conversation):
    started = frame(type="session_started", conversationId="native-session")
    conversation.feed(started[:10])
    assert conversation.readback() is None
    conversation.feed(started[10:])
    conversation.feed(frame(type="text_delta", content="not a success receipt"))
    assert conversation.readback()["state"] == "running"
    conversation.feed(frame(type="agent_minutes_update", used=0.5))
    conversation.feed(frame(type="complete", status="completed"))
    assert conversation.readback() == {"native_id": "native-session", "attempt_token": "a" * 64,
                                       "input_hash": "b" * 64, "state": "completed"}


def test_clarification_is_inconclusive_not_native_completion(conversation):
    conversation.feed(frame(type="session_started", conversationId="native-session"))
    conversation.feed(frame(type="agent_minutes_update", used=0.5))
    conversation.feed(frame(type="text_delta", content="Which approach should I use?"))
    conversation.feed(frame(type="input_required", reason="agent_turn_complete"))
    assert conversation.readback()["state"] == "unknown"
    assert conversation.finished is True


@pytest.mark.parametrize("status", [None, "failed", "interrupted"])
def test_completion_requires_success_status(conversation, status):
    conversation.feed(frame(type="session_started", conversationId="native-session"))
    conversation.feed(frame(type="agent_minutes_update", used=0.5))
    with pytest.raises(TransformProtocolError):
        conversation.feed(frame(type="complete", status=status))


@pytest.mark.parametrize("name,parameters,decision", [
    ("file_read", {"path": "src/app.py"}, "approve"),
    ("file_write", {"path": "src/app.py", "content": "changed"}, "approve"),
    ("editor", {"path": "src/app.py", "command": "str_replace"}, "approve"),
    ("editor", {"path": "src/app.py", "command": "execute"}, "deny"),
    ("file_write", {"path": "src/app.py", "executable": True}, "deny"),
    ("file_write", {"path": "src/other.py"}, "deny"),
    ("file_read", {"path": "src/private/token"}, "deny"),
    ("file_read", {"path": "../secret"}, "deny"),
    ("file_read", {"path": "/proc/self/environ"}, "deny"),
    ("file_read", {"path": ".git/config"}, "deny"),
    ("shell", {"path": "src/app.py", "command": "env"}, "deny"),
    ("network", {"path": "src/app.py"}, "deny"),
])
def test_file_tool_mediation(conversation, name, parameters, decision):
    conversation.feed(frame(type="session_started", conversationId="native-session"))
    replies = conversation.feed(frame(type="tool_permission_required", toolCallId="tool-a",
                                       name=name, parameters=parameters))
    assert replies == [{"type": "tool_permission_response", "toolCallId": "tool-a", "decision": decision}]


def test_file_tool_rejects_symlink(conversation, tmp_path):
    (tmp_path / "src/app.py").unlink()
    (tmp_path / "src/app.py").symlink_to("/etc/passwd")
    conversation.feed(frame(type="session_started", conversationId="native-session"))
    reply = conversation.feed(frame(type="tool_permission_required", toolCallId="tool-a", name="file_read",
                                     parameters={"path": "src/app.py"}))[0]
    assert reply["decision"] == "deny"


@pytest.mark.parametrize("invalid", [
    frame(type="complete"), frame(type="session_started", conversationId="foreign-session"),
    frame(type="agent_minutes_update", used=11), frame(type="agent_minutes_update", used=True),
    frame(type="agent_minutes_update", used=float("nan")), frame(type="unknown_permission_frame"),
    b"not-json\n", b"[]\n",
])
def test_invalid_native_frames_fail_closed(conversation, invalid):
    conversation.feed(frame(type="session_started", conversationId="native-session"))
    with pytest.raises(TransformProtocolError):
        conversation.feed(invalid)


def test_duplicate_permission_request_is_rejected(conversation):
    conversation.feed(frame(type="session_started", conversationId="native-session"))
    request = frame(type="tool_permission_required", toolCallId="tool-a", name="shell", parameters={})
    assert conversation.feed(request)[0]["decision"] == "deny"
    with pytest.raises(TransformProtocolError):
        conversation.feed(request)


def test_vendor_tool_lifecycle_frames_do_not_bypass_permission(conversation):
    conversation.feed(frame(type="session_started", conversationId="native-session"))
    assert conversation.feed(frame(type="tool_call_start", toolCallId="tool-a", name="shell",
                                   parameters={"command": "env"}, trusted=False)) == []
    assert conversation.feed(frame(type="tool_permission_required", toolCallId="tool-a", name="shell",
                                   parameters={"command": "env"}))[0]["decision"] == "deny"
    for kind in ("tool_call_result", "tool_output_delta", "info", "warn", "transformation_complete"):
        conversation.feed(frame(type=kind, toolCallId="tool-a"))
    assert conversation.readback()["state"] == "running"
    with pytest.raises(TransformProtocolError, match="unmediated"):
        conversation.feed(frame(type="tool_call_start", toolCallId="tool-b", name="shell",
                                parameters={"command": "env"}, trusted=True))


def test_unterminated_output_cannot_grow_without_bound(conversation):
    conversation.max_bytes = 16
    with pytest.raises(TransformProtocolError):
        conversation.feed(b"x" * 17)


def test_ndjson_job_pins_executable_and_disables_automatic_tool_trust():
    run, manifest, attempt = admitted()
    run.admission = deepcopy(run.admission)
    qualification = run.admission["transform_qualification"]
    qualification.update(interface=NDJSON_INTERFACE, cli_version="3.19.0", binary_sha256=NDJSON_BINARY_SHA256)
    intent = TransformAdapter(object())._intent(run, attempt)
    job = finite_provider_job(intent, manifest, qualification, run.admission["limits"])
    assert job["argv"] == ["/usr/local/bin/atx", "json"]
    assert job["session_request"]["params"]["trustAllTools"] is False
    assert job["session_request"]["params"]["interactiveMode"] is True
    assert job["session_request"]["params"]["transformationVersion"] == "revision-a"
    qualification["cli_version"] = "other"
    with pytest.raises(PermissionError):
        finite_provider_job(intent, manifest, qualification, run.admission["limits"])
